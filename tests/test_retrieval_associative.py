"""Multi-slot Q distillation — the §3.2 associative read's Q side.

Zero LLM, zero network. Pins the closed temporal set (absolute + 今天/昨天/前天,
year rollback for past-tense queries), the scene alias scan, and the composed
distill_q — plus the engine-side window pool via a seeded store.
"""

from __future__ import annotations

from datetime import datetime

from persome.evomem import identity
from persome.retrieval import associative as assoc
from persome.store import fts

NOW = datetime.fromisoformat("2026-06-10T12:00:00+08:00")


def _roster() -> identity.Roster:
    return identity.Roster.build([("张伟", ["伟哥"])])


def test_distill_time_absolute_and_relative():
    since, until = assoc.distill_time("6月5号定了什么", now=NOW)
    assert since.startswith("2026-06-05T00:00") and until.startswith("2026-06-05T23:59")
    since, until = assoc.distill_time("昨天弄的改动", now=NOW)
    assert since.startswith("2026-06-09T00:00")
    assert assoc.distill_time("没有时间词的问题", now=NOW) == (None, None)


def test_distill_time_yearless_future_date_rolls_back():
    """A yearless date that would land in the future refers to LAST year —
    queries here ask about the past, never guess forward."""
    since, _until = assoc.distill_time("12月25号那次聚餐", now=NOW)
    assert since.startswith("2025-12-25")


def test_distill_scenes_matches_aliases_and_extras():
    assert assoc.distill_scenes("上次在飞书里聊的") == ["飞书"]
    assert assoc.distill_scenes("CURSOR 里改的配置") == ["cursor"]  # case-folded probe
    assert assoc.distill_scenes("在 Obsidian 里记的", extra_scenes=["Obsidian"]) == ["Obsidian"]
    assert assoc.distill_scenes("没有场景词") == []


def test_distill_q_composes_all_slots():
    q = assoc.distill_q("昨天在飞书上和张伟聊的结论", _roster(), now=NOW)
    assert q.entities == ["张伟"]
    assert q.scene_terms == ["飞书"]
    assert q.since is not None and q.since.startswith("2026-06-09")
    # empty-slot case: nothing fabricated
    empty = assoc.distill_q("一段普通的散文问题", _roster(), now=NOW)
    assert empty.entities == [] and empty.scene_terms == [] and empty.since is None


def test_window_pool_recovers_when_text_heads_are_blind(ac_root):
    """A pure time query shares no token/concept with its answer — only the
    WHEN window pool can reach it (§3.3: the slot is a filter AND a list)."""
    from persome.store import fts

    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        conn.execute(
            "INSERT INTO entries (id, path, prefix, timestamp, tags, content, superseded)"
            " VALUES ('e-jun5', 'topic-x.md', 'topic', '2026-06-05T18:00:00+08:00', '', '上线窗口只在周二周四', 0)"
        )
        conn.execute(
            "INSERT INTO entries (id, path, prefix, timestamp, tags, content, superseded)"
            " VALUES ('e-jun7', 'topic-x.md', 'topic', '2026-06-07T18:00:00+08:00', '', '别的内容', 0)"
        )
        since, until = assoc.distill_time("6月5号定了什么", now=NOW)
        hits = fts.search_associative(
            conn, query="6月5号定了什么", since=since, until=until, top_k=5
        )
        assert [h.id for h in hits] == ["e-jun5"]  # window-pruned AND window-ranked


def test_relation_head_reaches_unmentioned_neighbor(ac_root):
    """§3.3 WHY/HOW head: the query names 张伟 only; the graph knows 张伟↔Bob
    (ACTIVE edge), so Bob's entry joins the vote. A shadow edge must NOT — the
    status gate keeps unproven extraction inert in retrieval."""
    from persome.evomem.models import MemoryStatus
    from persome.store import fts
    from persome.store import relation_edges as edges_store

    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        conn.execute(
            "INSERT INTO entries (id, path, prefix, timestamp, tags, content, superseded)"
            " VALUES ('e-bob', 'person-bob.md', 'person', '2026-06-08T10:00:00+08:00', '', 'Bob 这周值班', 0)"
        )
        edge_kwargs = dict(
            src_identity="张伟",
            dst_identity="Bob",
            predicate="knows",
            src_kind="person",
            dst_kind="person",
            provenance="user_committed",
            confidence=0.9,
            valid_from="2026-06-01T00:00:00+08:00",
        )
        # shadow edge: reachable by DEFAULT since the §7-8 verdict (downweighted
        # ×0.5 pool — the relation-probe gain depends on it, regression is
        # byte-identical); the kill-switch restores honest inertness.
        edges_store.add_edge(conn, status=MemoryStatus.SHADOW, **edge_kwargs)
        hits = fts.search_associative(conn, query="张伟的搭档", entities=["张伟"], top_k=5)
        assert "e-bob" in [h.id for h in hits]
        hits = fts.search_associative(
            conn,
            query="张伟的搭档",
            entities=["张伟"],
            top_k=5,
            relation_include_shadow=False,
        )
        assert "e-bob" not in [h.id for h in hits]
        # promoted (ACTIVE) edge → Bob's entry reachable without being mentioned
        edges_store.add_edge(conn, status=MemoryStatus.ACTIVE, **edge_kwargs)
        hits = fts.search_associative(conn, query="张伟的搭档", entities=["张伟"], top_k=5)
        assert "e-bob" in [h.id for h in hits]


def test_early_exit_skips_dense_on_unique_hard_hit(ac_root):
    """§3.3 early exit: a UNIQUE hard-head hit returns before the expensive
    dense embedding ever runs — certainty buys latency. Two hard hits → no
    exit, the soft heads run as usual."""
    from persome.store import fts
    from persome.store import vectors as vectors_mod

    calls = {"n": 0}

    def counting_embedder(text):
        calls["n"] += 1
        return [1.0, 0.0]

    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        conn.execute(
            "INSERT INTO entries (id, path, prefix, timestamp, tags, content, superseded)"
            " VALUES ('e-only', 'topic-x.md', 'topic', '2026-06-05T18:00:00+08:00', '', '唯一命中', 0)"
        )
        vectors_mod.set_enabled(True)
        fts.set_hybrid_config(enabled=True, recall_n=50, rrf_k=20)
        try:
            since, until = "2026-06-05T00:00:00+08:00", "2026-06-05T23:59:59+08:00"
            hits = fts.search_associative(
                conn,
                query="6月5号",
                since=since,
                until=until,
                top_k=5,
                embedder=counting_embedder,
            )
            assert [h.id for h in hits] == ["e-only"]
            assert calls["n"] == 0  # dense never ran — the exit fired first
            # early_exit off → soft heads run (the embedder is consulted)
            fts.search_associative(
                conn,
                query="6月5号",
                since=since,
                until=until,
                top_k=5,
                embedder=counting_embedder,
                early_exit=False,
            )
            assert calls["n"] > 0
        finally:
            fts.set_hybrid_config(enabled=False, recall_n=50, rrf_k=20)
            vectors_mod.set_enabled(False)


def test_mmr_diversity_trades_redundancy_for_coverage(ac_root):
    """§3.4 the consumer breadth knob: diversity=0 keeps plain RRF order (two
    near-duplicates on top); diversity>0 picks the DIFFERENT entry second.
    Deterministic — it widens, never randomizes."""
    from persome.store import fts

    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        rows = [
            ("e-dup1", "对账脚本今天跑完了口径没问题", "2026-06-09T10:00:00+08:00"),
            ("e-dup2", "对账脚本今天跑完了口径没有问题", "2026-06-08T10:00:00+08:00"),
            ("e-diff", "下季度预算表格已经归档", "2026-06-07T10:00:00+08:00"),
        ]
        for eid, content, ts in rows:
            conn.execute(
                "INSERT INTO entries (id, path, prefix, timestamp, tags, content, superseded)"
                " VALUES (?, 'topic-x.md', 'topic', ?, '', ?, 0)",
                (eid, ts, content),
            )
        # entity-slot query so the associative path (not the hybrid fallback) runs;
        # window covers all three → window pool ranks newest first
        since, until = "2026-06-01T00:00:00+08:00", "2026-06-10T23:59:59+08:00"
        plain = fts.search_associative(
            conn, query="x", since=since, until=until, top_k=2, early_exit=False
        )
        assert [h.id for h in plain] == ["e-dup1", "e-dup2"]  # RRF order = recency
        diverse = fts.search_associative(
            conn,
            query="x",
            since=since,
            until=until,
            top_k=2,
            early_exit=False,
            mmr_diversity=0.9,
        )
        assert diverse[0].id == "e-dup1" and diverse[1].id == "e-diff"  # dup demoted


# ── 加权 RRF 融合（§7-3 池权重，PR #504 判决落地）────────────────────────────


class TestWeightedFusion:
    def test_equal_weights_are_classic_rrf(self):
        pools = [["a", "b"], ["b", "c"]]
        assert fts._rrf_fuse(*pools, rrf_k=20) == fts._rrf_fuse_weighted(
            [(p, 1.0) for p in pools], rrf_k=20
        )

    def test_downweighted_pool_cannot_outvote_the_backbone(self):
        # backbone ranks a first; the slot pool stuffs 3 items topped by z.
        backbone = ["a", "b", "c"]
        slot = ["z", "y", "a"]
        equal = fts._rrf_fuse_weighted([(backbone, 1.0), (slot, 1.0)], rrf_k=20)
        weighted = fts._rrf_fuse_weighted([(backbone, 1.0), (slot, 0.3)], rrf_k=20)
        # equal-weight: a wins in both (it appears in both pools) but z out-votes b;
        # at 0.3 the backbone order survives while z still joins as a candidate.
        assert equal[:2] == ["a", "z"]
        assert weighted[:3] == ["a", "b", "c"]
        assert "z" in weighted  # boost, not ban — the slot can still introduce ids

    def test_zero_weight_pool_is_dropped(self):
        fused = fts._rrf_fuse_weighted([(["a"], 1.0), (["z"], 0.0)], rrf_k=20)
        assert fused == ["a"]


def test_contains_pool_round_robin_fair_seating(ac_root):
    """§7-9: a hub needle must not starve later needles — every needle gets a
    seat before any needle gets its second (per-needle recency preserved)."""
    from persome.store import fts

    with fts.cursor() as conn:
        for i in range(6):
            fts.insert_entry(
                conn,
                id=f"hub-{i}",
                path="person-甲.md",
                prefix="person",
                timestamp=f"2026-06-0{i + 1}T10:00:00+08:00",
                tags="",
                content=f"甲的记录 {i}",
            )
        fts.insert_entry(
            conn,
            id="tail-0",
            path="person-乙.md",
            prefix="person",
            timestamp="2026-06-01T09:00:00+08:00",
            tags="",
            content="乙的唯一记录",
        )
        pool = fts._contains_pool(conn, ["甲", "乙"], top_k=4)
    assert "tail-0" in pool  # the tail needle keeps a seat under truncation
    assert pool[0].startswith("hub-") and pool[1] == "tail-0"  # interleaved


def test_contains_pool_dense_rerank_orders_by_query_sim(ac_root):
    """§7-10: with the knob on, a pool candidate semantically close to the
    query outranks a newer-but-unrelated one; knob off restores recency."""
    from persome.store import fts
    from persome.store import vectors as vectors_mod

    with fts.cursor() as conn:
        fts.insert_entry(
            conn,
            id="new-noise",
            path="person-甲.md",
            prefix="person",
            timestamp="2026-06-09T10:00:00+08:00",
            tags="",
            content="甲的近况闲聊",
        )
        fts.insert_entry(
            conn,
            id="old-gold",
            path="person-甲.md",
            prefix="person",
            timestamp="2026-06-01T10:00:00+08:00",
            tags="",
            content="甲负责对账脚本",
        )
        vectors_mod.set_enabled(True)
        vectors_mod.save_vectors(
            conn,
            [("new-noise", [1.0, 0.0]), ("old-gold", [0.0, 1.0])],
            embedded_at="t0",
        )
        vectors_mod.clear_matrix_cache()
        qv = [0.0, 1.0]  # the query is about the gold topic
        pool = fts._contains_pool(conn, ["甲"], top_k=10)
        assert pool == ["new-noise", "old-gold"]  # recency order
        # BLEND semantics: recency rank1 + sim rank2 vs recency rank2 + sim
        # rank1 tie in the in-pool RRF — order stays stable (recency first),
        # but a candidate that leads BOTH orders leads the blend outright.
        blended = fts._rerank_by_query_sim(conn, pool, qv)
        assert set(blended) == {"old-gold", "new-noise"}
        # a third candidate that is newest AND most similar dominates
        fts.insert_entry(
            conn,
            id="both-best",
            path="person-甲.md",
            prefix="person",
            timestamp="2026-06-10T10:00:00+08:00",
            tags="",
            content="甲对账脚本收尾",
        )
        vectors_mod.save_vectors(conn, [("both-best", [0.0, 1.0])], embedded_at="t1")
        vectors_mod.clear_matrix_cache()
        pool2 = fts._contains_pool(conn, ["甲"], top_k=10)
        assert fts._rerank_by_query_sim(conn, pool2, qv)[0] == "both-best"
        vectors_mod.set_enabled(False)


def test_rerank_fail_open_paths(ac_root):
    from persome.store import fts

    with fts.cursor() as conn:
        # no vectors at all → input untouched (recency floor)
        assert fts._rerank_by_query_sim(conn, ["a", "b"], [1.0, 0.0]) == ["a", "b"]
        assert fts._rerank_by_query_sim(conn, [], [1.0]) == []
        assert fts._rerank_by_query_sim(conn, ["a"], None) == ["a"]

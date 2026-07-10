"Tests for test retrieval associative."

from __future__ import annotations

from datetime import datetime

from persome.evomem import identity
from persome.retrieval import associative as assoc
from persome.store import fts

NOW = datetime.fromisoformat("2026-06-10T12:00:00+08:00")


def _roster() -> identity.Roster:
    return identity.Roster.build([("\u5f20\u4f1f", ["\u4f1f\u54e5"])])


def test_distill_time_absolute_and_relative():
    since, until = assoc.distill_time("6\u67085\u53f7\u5b9a\u4e86\u4ec0\u4e48", now=NOW)
    assert since.startswith("2026-06-05T00:00") and until.startswith("2026-06-05T23:59")
    since, until = assoc.distill_time("\u6628\u5929\u5f04\u7684\u6539\u52a8", now=NOW)
    assert since.startswith("2026-06-09T00:00")
    assert assoc.distill_time("\u6ca1\u6709\u65f6\u95f4\u8bcd\u7684\u95ee\u9898", now=NOW) == (
        None,
        None,
    )


def test_distill_time_yearless_future_date_rolls_back():
    """A yearless date that would land in the future refers to LAST year —
    queries here ask about the past, never guess forward."""
    since, _until = assoc.distill_time("12\u670825\u53f7\u90a3\u6b21\u805a\u9910", now=NOW)
    assert since.startswith("2025-12-25")


def test_distill_scenes_matches_aliases_and_extras():
    assert assoc.distill_scenes("\u4e0a\u6b21\u5728\u98de\u4e66\u91cc\u804a\u7684") == [
        "\u98de\u4e66"
    ]
    assert assoc.distill_scenes("CURSOR \u91cc\u6539\u7684\u914d\u7f6e") == [
        "cursor"
    ]  # case-folded probe
    assert assoc.distill_scenes(
        "\u5728 Obsidian \u91cc\u8bb0\u7684", extra_scenes=["Obsidian"]
    ) == ["Obsidian"]
    assert assoc.distill_scenes("\u6ca1\u6709\u573a\u666f\u8bcd") == []


def test_distill_q_composes_all_slots():
    q = assoc.distill_q(
        "\u6628\u5929\u5728\u98de\u4e66\u4e0a\u548c\u5f20\u4f1f\u804a\u7684\u7ed3\u8bba",
        _roster(),
        now=NOW,
    )
    assert q.entities == ["\u5f20\u4f1f"]
    assert q.scene_terms == ["\u98de\u4e66"]
    assert q.since is not None and q.since.startswith("2026-06-09")
    # empty-slot case: nothing fabricated
    empty = assoc.distill_q(
        "\u4e00\u6bb5\u666e\u901a\u7684\u6563\u6587\u95ee\u9898", _roster(), now=NOW
    )
    assert empty.entities == [] and empty.scene_terms == [] and empty.since is None


def test_window_pool_recovers_when_text_heads_are_blind(ac_root):
    """A pure time query shares no token/concept with its answer — only the
    WHEN window pool can reach it (§3.3: the slot is a filter AND a list)."""
    from persome.store import fts

    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        conn.execute(
            "INSERT INTO entries (id, path, prefix, timestamp, tags, content, superseded)"
            " VALUES ('e-jun5', 'topic-x.md', 'topic', '2026-06-05T18:00:00+08:00', '', '\u4e0a\u7ebf\u7a97\u53e3\u53ea\u5728\u5468\u4e8c\u5468\u56db', 0)"
        )
        conn.execute(
            "INSERT INTO entries (id, path, prefix, timestamp, tags, content, superseded)"
            " VALUES ('e-jun7', 'topic-x.md', 'topic', '2026-06-07T18:00:00+08:00', '', '\u522b\u7684\u5185\u5bb9', 0)"
        )
        since, until = assoc.distill_time("6\u67085\u53f7\u5b9a\u4e86\u4ec0\u4e48", now=NOW)
        hits = fts.search_associative(
            conn, query="6\u67085\u53f7\u5b9a\u4e86\u4ec0\u4e48", since=since, until=until, top_k=5
        )
        assert [h.id for h in hits] == ["e-jun5"]  # window-pruned AND window-ranked


def test_relation_head_reaches_unmentioned_neighbor(ac_root):
    from persome.evomem.models import MemoryStatus
    from persome.store import fts
    from persome.store import relation_edges as edges_store

    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        conn.execute(
            "INSERT INTO entries (id, path, prefix, timestamp, tags, content, superseded)"
            " VALUES ('e-bob', 'person-bob.md', 'person', '2026-06-08T10:00:00+08:00', '', 'Bob \u8fd9\u5468\u503c\u73ed', 0)"
        )
        edge_kwargs = dict(
            src_identity="\u5f20\u4f1f",
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
        hits = fts.search_associative(
            conn, query="\u5f20\u4f1f\u7684\u642d\u6863", entities=["\u5f20\u4f1f"], top_k=5
        )
        assert "e-bob" in [h.id for h in hits]
        hits = fts.search_associative(
            conn,
            query="\u5f20\u4f1f\u7684\u642d\u6863",
            entities=["\u5f20\u4f1f"],
            top_k=5,
            relation_include_shadow=False,
        )
        assert "e-bob" not in [h.id for h in hits]
        # promoted (ACTIVE) edge → Bob's entry reachable without being mentioned
        edges_store.add_edge(conn, status=MemoryStatus.ACTIVE, **edge_kwargs)
        hits = fts.search_associative(
            conn, query="\u5f20\u4f1f\u7684\u642d\u6863", entities=["\u5f20\u4f1f"], top_k=5
        )
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
            " VALUES ('e-only', 'topic-x.md', 'topic', '2026-06-05T18:00:00+08:00', '', '\u552f\u4e00\u547d\u4e2d', 0)"
        )
        vectors_mod.set_enabled(True)
        fts.set_hybrid_config(enabled=True, recall_n=50, rrf_k=20)
        try:
            since, until = "2026-06-05T00:00:00+08:00", "2026-06-05T23:59:59+08:00"
            hits = fts.search_associative(
                conn,
                query="6\u67085\u53f7",
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
                query="6\u67085\u53f7",
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
            (
                "e-dup1",
                "\u5bf9\u8d26\u811a\u672c\u4eca\u5929\u8dd1\u5b8c\u4e86\u53e3\u5f84\u6ca1\u95ee\u9898",
                "2026-06-09T10:00:00+08:00",
            ),
            (
                "e-dup2",
                "\u5bf9\u8d26\u811a\u672c\u4eca\u5929\u8dd1\u5b8c\u4e86\u53e3\u5f84\u6ca1\u6709\u95ee\u9898",
                "2026-06-08T10:00:00+08:00",
            ),
            (
                "e-diff",
                "\u4e0b\u5b63\u5ea6\u9884\u7b97\u8868\u683c\u5df2\u7ecf\u5f52\u6863",
                "2026-06-07T10:00:00+08:00",
            ),
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
                path="person-\u7532.md",
                prefix="person",
                timestamp=f"2026-06-0{i + 1}T10:00:00+08:00",
                tags="",
                content=f"\u7532\u7684\u8bb0\u5f55 {i}",
            )
        fts.insert_entry(
            conn,
            id="tail-0",
            path="person-\u4e59.md",
            prefix="person",
            timestamp="2026-06-01T09:00:00+08:00",
            tags="",
            content="\u4e59\u7684\u552f\u4e00\u8bb0\u5f55",
        )
        pool = fts._contains_pool(conn, ["\u7532", "\u4e59"], top_k=4)
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
            path="person-\u7532.md",
            prefix="person",
            timestamp="2026-06-09T10:00:00+08:00",
            tags="",
            content="\u7532\u7684\u8fd1\u51b5\u95f2\u804a",
        )
        fts.insert_entry(
            conn,
            id="old-gold",
            path="person-\u7532.md",
            prefix="person",
            timestamp="2026-06-01T10:00:00+08:00",
            tags="",
            content="\u7532\u8d1f\u8d23\u5bf9\u8d26\u811a\u672c",
        )
        vectors_mod.set_enabled(True)
        vectors_mod.save_vectors(
            conn,
            [("new-noise", [1.0, 0.0]), ("old-gold", [0.0, 1.0])],
            embedded_at="t0",
        )
        vectors_mod.clear_matrix_cache()
        qv = [0.0, 1.0]  # the query is about the gold topic
        pool = fts._contains_pool(conn, ["\u7532"], top_k=10)
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
            path="person-\u7532.md",
            prefix="person",
            timestamp="2026-06-10T10:00:00+08:00",
            tags="",
            content="\u7532\u5bf9\u8d26\u811a\u672c\u6536\u5c3e",
        )
        vectors_mod.save_vectors(conn, [("both-best", [0.0, 1.0])], embedded_at="t1")
        vectors_mod.clear_matrix_cache()
        pool2 = fts._contains_pool(conn, ["\u7532"], top_k=10)
        assert fts._rerank_by_query_sim(conn, pool2, qv)[0] == "both-best"
        vectors_mod.set_enabled(False)


def test_rerank_fail_open_paths(ac_root):
    from persome.store import fts

    with fts.cursor() as conn:
        # no vectors at all → input untouched (recency floor)
        assert fts._rerank_by_query_sim(conn, ["a", "b"], [1.0, 0.0]) == ["a", "b"]
        assert fts._rerank_by_query_sim(conn, [], [1.0]) == []
        assert fts._rerank_by_query_sim(conn, ["a"], None) == ["a"]

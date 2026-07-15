"Tests for test recall recency and labels."

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from persome.mcp import server as mcp_server
from persome.store import fts


@pytest.fixture()
def _restore_fts_gates():
    """Snapshot/restore ALL fts module-level read gates (+ the vectors write
    gate) around a test — wire_read_path touches every one of them."""
    from persome.store import vectors as vectors_mod

    match_before = dict(fts._MATCH)
    recency_before = dict(fts._RECENCY)
    hybrid_before = dict(fts._HYBRID)
    pool_before = dict(fts._POOL_WEIGHTS)
    vectors_before = vectors_mod.is_enabled()
    yield
    fts._MATCH.update(match_before)
    fts._RECENCY.update(recency_before)
    fts._HYBRID.update(hybrid_before)
    fts._POOL_WEIGHTS.update(pool_before)
    vectors_mod.set_enabled(vectors_before)


def _insert(conn, *, id: str, ts: str, content: str, tags: str = "", path: str = "topic-x.md"):
    conn.execute(
        "INSERT INTO entries (id, path, prefix, timestamp, tags, content, superseded)"
        " VALUES (?, ?, 'topic', ?, ?, ?, 0)",
        (id, path, ts, tags, content),
    )


def test_tag_only_token_no_longer_matches(ac_root, _restore_fts_gates):
    """A query token living ONLY in an entry's tags must not recall it — the
    real-store failure was 251 live 'intent' hits with the token only in tags."""
    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        _insert(
            conn,
            id="e-cand",
            ts="2026-06-27T22:43",
            tags="#intent #kind:meeting #scope:fast-K1",
            content="[\u4f1a\u8bae] when=\u660e\u5929 with=\u5f20\u4e09",
        )
        assert fts.search(conn, query="meeting") == []
        # kill-switch restores the legacy label-matchable behaviour
        fts.set_tags_matchable(True)
        assert [h.id for h in fts.search(conn, query="meeting")] == ["e-cand"]


def test_content_matching_and_multi_token_or_unaffected(ac_root, _restore_fts_gates):
    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        _insert(conn, id="e-a", ts="2026-06-27T10:00", content="meeting with Alice about roadmap")

        # run into one token (pre-existing tokenizer behaviour, not this change).
        _insert(
            conn,
            id="e-b",
            ts="2026-06-27T11:00",
            content="\u5496\u5561\u504f\u597d\u662f \u624b\u51b2",
        )
        assert [h.id for h in fts.search(conn, query="meeting")] == ["e-a"]
        # OR semantics across tokens survives the {content}: wrapper
        ids = {h.id for h in fts.search(conn, query="roadmap \u624b\u51b2")}
        assert ids == {"e-a", "e-b"}


def _seed_old_vs_fresh(conn):
    conn.executescript(fts.SCHEMA)
    _insert(conn, id="e-old", ts="2026-06-01T10:00", content="\u53d1\u7248 \u53d1\u7248 0.3.9")
    _insert(
        conn,
        id="e-fresh",
        ts="2026-07-01T10:00",
        content="\u53d1\u7248 0.4.2 \u5df2\u7ecf \u51fa\u5305",
    )


def test_decay_prefers_fresh_over_slightly_better_old(ac_root, _restore_fts_gates):
    with fts.cursor() as conn:
        _seed_old_vs_fresh(conn)
        # decay off → pure BM25 order: the old, lexically-stronger entry wins
        fts.set_recency_decay(half_life_days=0.0, floor=0.2)
        ids = [h.id for h in fts.search_hybrid(conn, query="\u53d1\u7248", top_k=2)]
        assert ids == ["e-old", "e-fresh"]
        # decay on (defaults) → the 30-day-older entry decays past the fresh one
        fts.set_recency_decay(half_life_days=14.0, floor=0.2)
        ids = [h.id for h in fts.search_hybrid(conn, query="\u53d1\u7248", top_k=2)]
        assert ids == ["e-fresh", "e-old"]


def test_decay_anchors_at_until_for_as_of_queries(ac_root, _restore_fts_gates):
    """As-of queries decay relative to their own clock: at until=2026-06-02 the
    'old' entry IS the fresh one and must stay on top."""
    with fts.cursor() as conn:
        _seed_old_vs_fresh(conn)
        fts.set_recency_decay(half_life_days=14.0, floor=0.2)
        ids = [
            h.id
            for h in fts.search_hybrid(
                conn, query="\u53d1\u7248", until="2026-06-02T00:00", top_k=2
            )
        ]
        assert ids == ["e-old"]  # e-fresh is outside the window entirely
        # both inside the window, anchored at until near the newer one; the
        # 43-days-stale (→floor) stronger match loses to the day-fresh one
        _insert(
            conn,
            id="e-older",
            ts="2026-04-20T10:00",
            content="\u53d1\u7248 \u53d1\u7248 \u53d1\u7248 0.3.8",
        )
        ids = [
            h.id
            for h in fts.search_hybrid(
                conn, query="\u53d1\u7248", until="2026-06-02T00:00", top_k=3
            )
        ]
        assert ids[0] == "e-old"  # newest-before-until wins over the stronger older match


def test_uniform_age_keeps_bm25_order(ac_root, _restore_fts_gates):
    """Same-day candidates get a (near-)uniform factor — order is BM25's."""
    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        _insert(
            conn,
            id="e-strong",
            ts="2026-06-01T10:00",
            content="\u90e8\u7f72 \u6d41\u7a0b \u90e8\u7f72 \u68c0\u67e5\u5355",
        )
        _insert(conn, id="e-weak", ts="2026-06-01T11:00", content="\u90e8\u7f72 \u8bf4\u660e")
        ids = [h.id for h in fts.search_hybrid(conn, query="\u90e8\u7f72 \u6d41\u7a0b", top_k=2)]
        assert ids[0] == "e-strong"


def test_search_associative_applies_decay(ac_root, _restore_fts_gates):
    """The associative entrance (slot pools + RRF) also decays its fusion."""
    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        _insert(
            conn,
            id="e-old",
            ts="2026-06-01T10:00",
            content="\u5f20\u4f1f \u53d1\u7248 \u53d1\u7248 0.3.9",
        )
        _insert(
            conn,
            id="e-fresh",
            ts="2026-07-01T10:00",
            content="\u5f20\u4f1f \u53d1\u7248 0.4.2 \u5df2\u7ecf \u51fa\u5305",
        )
        fts.set_recency_decay(half_life_days=0.0, floor=0.2)
        ids = [
            h.id
            for h in fts.search_associative(
                conn, query="\u53d1\u7248", entities=["\u5f20\u4f1f"], top_k=2, early_exit=False
            )
        ]
        assert ids[0] == "e-old"
        fts.set_recency_decay(half_life_days=14.0, floor=0.2)
        ids = [
            h.id
            for h in fts.search_associative(
                conn, query="\u53d1\u7248", entities=["\u5f20\u4f1f"], top_k=2, early_exit=False
            )
        ]
        assert ids[0] == "e-fresh"


def test_decay_never_changes_membership(ac_root, _restore_fts_gates):
    with fts.cursor() as conn:
        _seed_old_vs_fresh(conn)
        fts.set_recency_decay(half_life_days=14.0, floor=0.2)
        with_decay = {h.id for h in fts.search_hybrid(conn, query="\u53d1\u7248", top_k=5)}
        fts.set_recency_decay(half_life_days=0.0, floor=0.2)
        without = {h.id for h in fts.search_hybrid(conn, query="\u53d1\u7248", top_k=5)}
        assert with_decay == without


def test_mcp_search_results_carry_age_days(ac_root, _restore_fts_gates):
    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        old_ts = (datetime.now().astimezone() - timedelta(days=40)).isoformat()
        _insert(conn, id="e-old", ts=old_ts, content="\u5f53\u524d \u7248\u672c 0.3.9")
        out = mcp_server._search(conn, query="\u7248\u672c", top_k=3)
        assert out["results"], "expected a hit"
        age = out["results"][0]["age_days"]
        assert isinstance(age, int) and 39 <= age <= 41


def test_verify_fact_flags_stale_evidence(ac_root, _restore_fts_gates):
    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        old_ts = (datetime.now().astimezone() - timedelta(days=40)).isoformat()
        _insert(conn, id="e-old", ts=old_ts, content="\u5f53\u524d \u7248\u672c 0.3.9")
        out = mcp_server._verify_fact(conn, claim="\u5f53\u524d \u7248\u672c 0.3.9")
        assert out["stale"] is True
        assert out["freshest_age_days"] >= 39
        assert out["evidence"][0]["id"] == "e-old"
        assert "stale" in out["note"] or "verify" in out["note"]


def test_verify_fact_passes_fresh_evidence(ac_root, _restore_fts_gates):
    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        fresh_ts = (datetime.now().astimezone() - timedelta(days=1)).isoformat()
        _insert(conn, id="e-new", ts=fresh_ts, content="\u5f53\u524d \u7248\u672c 0.4.2")
        out = mcp_server._verify_fact(conn, claim="\u5f53\u524d \u7248\u672c")
        assert out["stale"] is False
        assert out["freshest_age_days"] <= 2


def test_verify_fact_explains_open_contradictions(ac_root, _restore_fts_gates):
    from persome.store import contradictions as contradictions_store

    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        fresh_ts = (datetime.now().astimezone() - timedelta(days=1)).isoformat()
        _insert(conn, id="e-a", ts=fresh_ts, content="current version 0.4.2")
        _insert(conn, id="e-b", ts=fresh_ts, content="current version 0.5.0")
        key = contradictions_store.record(
            conn,
            a_id="e-a",
            b_id="e-b",
            path="topic-x.md",
            a_body="current version 0.4.2",
            b_body="current version 0.5.0",
            reason="both claim to be the current version",
        )

        out = mcp_server._verify_fact(conn, claim="current version")

    by_id = {item["id"]: item for item in out["evidence"]}
    assert by_id["e-a"]["conflicted"] is True
    assert by_id["e-a"]["contradictions"] == [
        {
            "pair_key": key,
            "reason": "both claim to be the current version",
            "competing_id": "e-b",
            "competing_claim": "current version 0.5.0",
        }
    ]
    assert by_id["e-b"]["contradictions"][0]["competing_id"] == "e-a"
    assert "unresolved contradiction" in out["note"]


def test_verify_fact_does_not_replay_closed_contradictions(ac_root, _restore_fts_gates):
    from persome.store import contradictions as contradictions_store

    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        fresh_ts = (datetime.now().astimezone() - timedelta(days=1)).isoformat()
        _insert(conn, id="e-a", ts=fresh_ts, content="current version 0.4.2")
        key = contradictions_store.record(
            conn,
            a_id="e-a",
            b_id="e-retired",
            path="topic-x.md",
            a_body="current version 0.4.2",
            b_body="current version 0.3.9",
            reason="a newer version superseded the old claim",
        )
        contradictions_store.close(conn, key, status="resolved", keep_id="e-a")

        out = mcp_server._verify_fact(conn, claim="current version")

    item = next(evidence for evidence in out["evidence"] if evidence["id"] == "e-a")
    assert item["conflicted"] is False
    assert "contradictions" not in item
    assert "unresolved contradiction" not in out["note"]


def test_verify_fact_no_evidence_is_honest(ac_root, _restore_fts_gates):
    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        out = mcp_server._verify_fact(
            conn, claim="\u4ece\u672a\u51fa\u73b0\u8fc7\u7684\u4e3b\u9898xyzq"
        )
        assert out["evidence"] == []
        assert out["stale"] is True
        assert "No related evidence" in out["note"]


def test_verify_fact_undated_evidence_notes_unknown_freshness(ac_root, _restore_fts_gates):
    """Evidence whose timestamps all fail to parse is 'freshness unknown', not
    'old' \u2014 the stale branch used to interpolate the Python literal None into
    the note (\"... is None day(s) old\") and misstate unknown age as staleness."""
    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        _insert(
            conn, id="e-undated", ts="not-a-timestamp", content="\u5f53\u524d \u7248\u672c 0.3.9"
        )
        out = mcp_server._verify_fact(conn, claim="\u5f53\u524d \u7248\u672c 0.3.9")
        assert out["evidence"], "expected the undated entry to be recalled"
        assert out["freshest_age_days"] is None
        assert out["stale"] is True
        assert "None" not in out["note"]
        assert "cannot be judged" in out["note"]


def test_wire_read_path_sets_every_gate_from_config(ac_root, _restore_fts_gates, monkeypatch):
    """One wiring call must configure ALL module-level read gates — a spawn
    path that calls it serves the full-power stack, never import-time defaults."""
    from persome.config import Config

    cfg = Config()
    cfg.search.hybrid_enabled = True
    cfg.search.hybrid_recall_n = 33
    cfg.search.hybrid_rrf_k = 7
    cfg.search.slot_pool_weight = 0.7
    cfg.search.relation_pool_weight = 0.4
    cfg.search.relation_include_shadow = False
    cfg.search.contains_pool_rerank = False
    cfg.search.tags_matchable = True
    cfg.search.recency_half_life_days = 3.0
    cfg.search.recency_decay_floor = 0.5
    monkeypatch.setattr("persome.writer.embeddings_client.available", lambda: True)
    fts.wire_read_path(cfg)
    assert fts._HYBRID == {"enabled": True, "recall_n": 33, "rrf_k": 7}
    assert fts._POOL_WEIGHTS["slot"] == 0.7
    assert fts._POOL_WEIGHTS["relation"] == 0.4
    assert fts._POOL_WEIGHTS["relation_shadow"] is False
    assert fts._POOL_WEIGHTS["contains_rerank"] is False
    assert fts._MATCH["tags_matchable"] is True
    assert fts._RECENCY == {"half_life_days": 3.0, "floor": 0.5}
    # no embeddings endpoint → dense stays off (BM25-only degrade is explicit)
    monkeypatch.setattr("persome.writer.embeddings_client.available", lambda: False)
    fts.wire_read_path(cfg)
    assert fts._HYBRID["enabled"] is False


def test_build_server_wires_the_full_read_path(ac_root, _restore_fts_gates, monkeypatch):
    """#557 principle pinned at the entrance: EVERY MCP spawn path (stdio and
    in-daemon both go through build_server) must wire the read gates — skipping
    it is how the stdio server silently served a degraded BM25-only memory."""
    from persome.config import Config

    calls: list[object] = []
    monkeypatch.setattr(fts, "wire_read_path", lambda cfg: calls.append(cfg))
    cfg = Config()
    mcp_server.build_server(cfg, auth_enabled=False)
    assert calls == [cfg]


def test_behavior_patterns_exposes_root_and_faces(ac_root):
    from persome.store import entries as entries_store
    from persome.store import schema_faces as faces_store

    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        # cold start: honest empties, never an error
        out = mcp_server._behavior_patterns(conn)
        assert out == {"root": None, "faces": [], "skills": [], "rendered": ""}
        entries_store.create_file(
            conn,
            name="skills/skill-morning-triage",
            description="Triage communication before opening the active project.",
            tags=["skill", "observed"],
        )
        skill_id = entries_store.append_entry(
            conn,
            name="skills/skill-morning-triage.md",
            content=(
                "stage: observed\n\n"
                "**Trigger**: weekday start\n"
                "**Steps**: 1. Mail. 2. Slack. 3. Project.\n"
                "**Boundaries**: observed ordering only"
            ),
            tags=["pattern", "observed"],
        )
        echo_id = entries_store.append_entry(
            conn,
            name="skills/skill-morning-triage.md",
            content="Triggered with confidence 0.91: Mail and Slack were opened.",
            tags=["triggered", "echo"],
        )
        faces_store.upsert_root(
            conn,
            signature="\u9ad8\u5ea6\u7cfb\u7edf\u5316\u7684\u5f00\u53d1\u8005\uff0c\u6df1\u591c\u9ad8\u4ea7",
            members=[],
            confidence=0.9,
        )
        out = mcp_server._behavior_patterns(conn)
        assert out["root"]["signature"].startswith("\u9ad8\u5ea6\u7cfb\u7edf\u5316")
        assert "Root" in out["rendered"] and "\u9ad8\u5ea6\u7cfb\u7edf\u5316" in out["rendered"]
        assert out["skills"][0]["entry_id"] == skill_id
        assert out["skills"][0]["entry_id"] != echo_id
        assert out["skills"][0]["path"] == "skills/skill-morning-triage.md"
        assert "**Steps**: 1. Mail. 2. Slack. 3. Project." in out["skills"][0]["playbook"]
        assert "Triggered with confidence" not in out["skills"][0]["playbook"]
        assert "Observed workflow" in out["rendered"]


def test_behavior_patterns_excludes_skills_without_live_observed_playbook(ac_root):
    from persome.store import entries as entries_store

    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        entries_store.create_file(
            conn,
            name="skill-echo-only",
            description="Has an activation echo but no admitted playbook.",
            tags=["skill"],
        )
        entries_store.append_entry(
            conn,
            name="skill-echo-only.md",
            content="Triggered with confidence 0.88: matching activity.",
            tags=["triggered", "echo"],
        )
        entries_store.create_file(
            conn,
            name="skills/skill-unobserved",
            description="A candidate that has not passed observation gates.",
            tags=["skill"],
        )
        entries_store.append_entry(
            conn,
            name="skills/skill-unobserved.md",
            content="stage: candidate\n\n**Pattern**: one possible sequence",
            tags=["pattern"],
        )
        entries_store.create_file(
            conn,
            name="skills/skill-dormant",
            description="An observed playbook that is no longer active.",
            tags=["skill", "observed"],
            status="dormant",
        )
        entries_store.append_entry(
            conn,
            name="skills/skill-dormant.md",
            content="stage: observed\n\n**Pattern**: retired workflow",
            tags=["pattern", "observed"],
        )

        out = mcp_server._behavior_patterns(conn)

    assert out["skills"] == []
    assert out["rendered"] == ""

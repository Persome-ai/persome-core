"""Session memory-delta extraction, gates, persistence, and apply.

Covers the deterministic gates (quote evidence / roster multiple-choice /
closed predicate set / confidence floor), persistence, and safe degradation.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from persome import config as config_mod
from persome.store import fts
from persome.store import memory_deltas as deltas_store
from persome.timeline import store as timeline_store
from persome.timeline.store import TimelineBlock
from persome.writer import memory_delta as delta_mod


def _block(start: datetime, entries: list[str], apps: list[str]) -> TimelineBlock:
    return TimelineBlock(
        start_time=start,
        end_time=start + timedelta(minutes=1),
        entries=entries,
        apps_used=apps,
        capture_count=len(entries),
    )


def _seed_session_blocks(entries: list[str]) -> tuple[datetime, datetime]:
    start = datetime(2026, 7, 2, 9, 0).astimezone()
    with fts.cursor() as conn:
        timeline_store.ensure_schema(conn)
        for i, entry in enumerate(entries):
            timeline_store.insert(conn, _block(start + timedelta(minutes=i), [entry], ["Feishu"]))
    return start, start + timedelta(minutes=len(entries) + 1)


def _cfg(enabled: bool = True) -> config_mod.Config:
    cfg = config_mod.Config()
    cfg.memory_delta.enabled = enabled
    return cfg


def _ref(name: str) -> dict:
    return {"new_entity": name}


def _payload(**overrides) -> str:
    base = {
        "owner_alias_candidates": [],
        "entities": [
            {
                "new_entity": "\u5f20\u4e09",
                "kind": "person",
                "quote": "\u548c\u5f20\u4e09\u786e\u8ba4\u4e86\u8bc4\u5ba1\u7ed3\u8bba",
                "confidence": 0.9,
            }
        ],
        "assertions": [
            {
                "subject": _ref("\u5f20\u4e09"),
                "text": "\u5f20\u4e09\u786e\u8ba4\u4e86\u8bc4\u5ba1\u7ed3\u8bba",
                "quote": "\u548c\u5f20\u4e09\u786e\u8ba4\u4e86\u8bc4\u5ba1\u7ed3\u8bba",
                "confidence": 0.8,
            }
        ],
        "relations": [],
        "events": [],
    }
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


SESSION_ENTRY = '[Feishu] \u804a\u5929: \u548c\u5f20\u4e09\u786e\u8ba4\u4e86\u8bc4\u5ba1\u7ed3\u8bba\u3002"\u5468\u4e94\u7248\u672c\u53ef\u4ee5\u53d1"'


def test_flag_off_is_a_strict_noop(ac_root, fake_llm) -> None:
    start, end = _seed_session_blocks([SESSION_ENTRY])
    result = delta_mod.run_after_session(
        _cfg(enabled=False), session_id="s1", start_time=start, end_time=end
    )
    assert result.skipped_reason == "disabled" and not result.written
    assert fake_llm.calls == []  # no LLM call, no row
    with fts.cursor() as conn:
        assert deltas_store.recent(conn) == []


def test_delta_persisted_shadow_with_counts(ac_root, fake_llm) -> None:
    start, end = _seed_session_blocks([SESSION_ENTRY])
    fake_llm.set_default(delta_mod.STAGE, _payload())
    result = delta_mod.run_after_session(_cfg(), session_id="s2", start_time=start, end_time=end)
    assert result.written and result.counts["entities"] == 1 and result.counts["assertions"] == 1
    with fts.cursor() as conn:
        row = deltas_store.latest_for_session(conn, "s2")
    assert row is not None and row["status"] == "shadow"
    delta = json.loads(row["payload"])
    assert delta["entities"][0]["new_entity"] == "\u5f20\u4e09"
    # the LLM actually received roster + session_events sections (now cache-control blocks)
    blocks = fake_llm.calls[0]["messages"][1]["content"]
    sent = "".join(b["text"] for b in blocks)
    assert "<roster>" in sent and "<session_events>" in sent and "\u5f20\u4e09" in sent

    sys_blocks = fake_llm.calls[0]["messages"][0]["content"]
    assert sys_blocks[0].get("cache_control") == {"type": "ephemeral"}
    assert blocks[0].get("cache_control") == {"type": "ephemeral"}


def test_roster_reserves_self_and_owner_aliases(ac_root) -> None:
    cfg = _cfg()
    cfg.memory_delta.owner_aliases = [
        "Casey Example",
        "\u793a\u4f8b\u7532",
        "Casey-Example",
    ]

    roster = delta_mod._load_roster(cfg)

    assert roster[0] == (
        "self",
        ["Casey Example", "\u793a\u4f8b\u7532", "Casey-Example"],
    )
    rendered = delta_mod._render_roster(roster)
    assert "self" in rendered and "memory owner" in rendered


def test_owner_alias_canonicalizes_to_self_but_never_mints_person(ac_root) -> None:
    from persome.evomem import identity as identity_mod

    roster = identity_mod.Roster.build([("self", ["Casey-Example"]), ("Kevin", [])])
    quote = "Casey-Example reviewed the launch plan with Kevin"
    raw = {
        "entities": [
            {"ref": "Casey-Example", "kind": "person", "quote": quote, "confidence": 0.9},
            {"ref": "Kevin", "kind": "person", "quote": quote, "confidence": 0.9},
        ],
        "assertions": [],
        "relations": [
            {
                "src": {"ref": "Casey-Example"},
                "dst": {"ref": "Kevin"},
                "predicate": "knows",
                "label": "teammates",
                "quote": quote,
                "confidence": 0.9,
            }
        ],
        "events": [],
    }

    clean, dropped = delta_mod.gate_delta(
        raw, roster=roster, session_text=quote, min_confidence=0.5
    )

    assert dropped == 1
    assert [entity["ref"] for entity in clean["entities"]] == ["Kevin"]
    assert clean["relations"][0]["src"] == {"ref": "self"}
    assert clean["relations"][0]["dst"] == {"ref": "Kevin"}


def test_ai_owner_alias_evidence_promotes_without_user_config(ac_root, fake_llm) -> None:
    quote = "Opened the user's own GitHub account Casey-Example with Kevin"
    start, end = _seed_session_blocks([f"[Chrome] {quote}"])
    fake_llm.set_default(
        delta_mod.STAGE,
        _payload(
            owner_alias_candidates=[
                {
                    "alias": "Casey-Example",
                    "source_kind": "owned_account",
                    "quote": quote,
                    "confidence": 0.94,
                }
            ],
            entities=[
                {
                    "new_entity": "Casey-Example",
                    "kind": "person",
                    "quote": quote,
                    "confidence": 0.94,
                },
                {
                    "new_entity": "Kevin",
                    "kind": "person",
                    "quote": quote,
                    "confidence": 0.9,
                },
            ],
            assertions=[],
            relations=[
                {
                    "src": {"new_entity": "Casey-Example"},
                    "dst": {"new_entity": "Kevin"},
                    "predicate": "knows",
                    "label": "collaborators",
                    "quote": quote,
                    "confidence": 0.9,
                }
            ],
        ),
    )
    cfg = _cfg()

    first = delta_mod.run_after_session(
        cfg, session_id="owner-session-1", start_time=start, end_time=end
    )
    assert first.counts["owner_alias_candidates"] == 1
    with fts.cursor() as conn:
        assert (
            conn.execute(
                "SELECT status FROM owner_aliases WHERE alias_key='casey-example'"
            ).fetchone()[0]
            == "pending"
        )
        payload = json.loads(deltas_store.latest_for_session(conn, "owner-session-1")["payload"])
    assert all(entity.get("new_entity") != "Casey-Example" for entity in payload["entities"])
    assert payload["relations"] == []

    second = delta_mod.run_after_session(
        cfg, session_id="owner-session-2", start_time=start, end_time=end
    )
    assert second.counts["owner_alias_candidates"] == 1
    with fts.cursor() as conn:
        row = conn.execute(
            "SELECT status, evidence_count FROM owner_aliases WHERE alias_key='casey-example'"
        ).fetchone()
        payload = json.loads(deltas_store.latest_for_session(conn, "owner-session-2")["payload"])
        owner_points = conn.execute(
            "SELECT COUNT(*) FROM evo_nodes WHERE file_name='person-casey-example.md'"
            " AND is_latest=1 AND status='active'"
        ).fetchone()[0]
    assert tuple(row) == ("active", 2)
    assert payload["relations"][0]["src"] == {"ref": "self"}
    assert owner_points == 0
    assert delta_mod._load_roster(cfg)[0] == ("self", ["Casey-Example"])


def test_render_blocks_excludes_local_model_output_and_mixed_focus(ac_root) -> None:
    start = datetime(2026, 7, 12, 9, 0).astimezone()
    block = TimelineBlock(
        start_time=start,
        end_time=start + timedelta(minutes=1),
        entries=[
            "[Google Chrome] Persome Personal Model (http://127.0.0.1:8742/model): "
            "read Root claiming Kevin is the owner.",
            "[Feishu] Project chat: Kevin said the release is ready.",
        ],
        apps_used=["Google Chrome", "Feishu"],
        capture_count=2,
        focus_excerpt="Root: Kevin is the owner",
    )

    rendered = delta_mod._render_blocks([block])

    assert "release is ready" in rendered
    assert "127.0.0.1:8742/model" not in rendered
    assert "Root: Kevin is the owner" not in rendered


def test_quote_evidence_gate_drops_unquoted_items(ac_root, fake_llm) -> None:
    """No verbatim quote from the session text → the item never lands (§4.1)."""
    start, end = _seed_session_blocks([SESSION_ENTRY])
    fake_llm.set_default(
        delta_mod.STAGE,
        _payload(
            entities=[
                {
                    "new_entity": "\u5f20\u4e09",
                    "kind": "person",
                    "quote": "\u8fd9\u53e5\u8bdd\u4e0d\u5728\u4f1a\u8bdd\u91cc",
                    "confidence": 0.9,
                }
            ],
            assertions=[],
        ),
    )
    result = delta_mod.run_after_session(_cfg(), session_id="s3", start_time=start, end_time=end)
    assert result.written and result.counts["entities"] == 0 and result.dropped == 1


def test_identity_gate_rejects_bare_store_probing_strings(ac_root, fake_llm) -> None:
    start, end = _seed_session_blocks([SESSION_ENTRY])
    fake_llm.set_default(
        delta_mod.STAGE,
        _payload(
            entities=[
                {
                    "new_entity": "\u51ed\u7a7a\u634f\u9020\u7684\u4eba",
                    "kind": "person",
                    "quote": "\u548c\u5f20\u4e09\u786e\u8ba4\u4e86\u8bc4\u5ba1\u7ed3\u8bba",
                    "confidence": 0.9,
                }
            ],
            assertions=[],
        ),
    )
    result = delta_mod.run_after_session(_cfg(), session_id="s4", start_time=start, end_time=end)
    assert result.counts["entities"] == 0 and result.dropped == 1


def test_relation_gate_enforces_closed_predicate_set(ac_root, fake_llm) -> None:
    start, end = _seed_session_blocks([SESSION_ENTRY])
    fake_llm.set_default(
        delta_mod.STAGE,
        _payload(
            entities=[],
            assertions=[],
            relations=[
                {
                    "src": _ref("\u5f20\u4e09"),
                    "dst": _ref("\u5f20\u4e09"),
                    "predicate": "loves",  # not in the 6-predicate closed set
                    "label": "",
                    "quote": "\u548c\u5f20\u4e09\u786e\u8ba4\u4e86\u8bc4\u5ba1\u7ed3\u8bba",
                    "confidence": 0.9,
                },
                {
                    "src": _ref("\u5f20\u4e09"),
                    "dst": _ref("\u5f20\u4e09"),
                    "predicate": "knows",
                    "label": "\u540c\u4e8b",
                    "quote": "\u548c\u5f20\u4e09\u786e\u8ba4\u4e86\u8bc4\u5ba1\u7ed3\u8bba",
                    "confidence": 0.9,
                },
            ],
        ),
    )
    result = delta_mod.run_after_session(_cfg(), session_id="s5", start_time=start, end_time=end)
    assert result.counts["relations"] == 1 and result.dropped == 1


def test_confidence_floor_drops_hedges(ac_root, fake_llm) -> None:
    start, end = _seed_session_blocks([SESSION_ENTRY])
    fake_llm.set_default(
        delta_mod.STAGE,
        _payload(
            entities=[
                {
                    "new_entity": "\u5f20\u4e09",
                    "kind": "person",
                    "quote": "\u548c\u5f20\u4e09\u786e\u8ba4\u4e86\u8bc4\u5ba1\u7ed3\u8bba",
                    "confidence": 0.2,
                }
            ],
            assertions=[],
        ),
    )
    result = delta_mod.run_after_session(_cfg(), session_id="s6", start_time=start, end_time=end)
    assert result.counts["entities"] == 0 and result.dropped == 1


def test_malformed_llm_output_fails_open(ac_root, fake_llm) -> None:
    start, end = _seed_session_blocks([SESSION_ENTRY])
    fake_llm.set_default(delta_mod.STAGE, "not json at all {{{")
    result = delta_mod.run_after_session(_cfg(), session_id="s7", start_time=start, end_time=end)
    assert not result.written and result.skipped_reason == "unparseable"
    with fts.cursor() as conn:
        assert deltas_store.latest_for_session(conn, "s7") is None


def test_no_blocks_skips_without_llm(ac_root, fake_llm) -> None:
    now = datetime.now().astimezone()
    result = delta_mod.run_after_session(
        _cfg(), session_id="s8", start_time=now - timedelta(minutes=5), end_time=now
    )
    assert result.skipped_reason == "no_blocks" and fake_llm.calls == []


def test_short_session_reads_the_containing_timeline_block(ac_root, fake_llm) -> None:
    block_start = datetime(2026, 7, 2, 9, 0).astimezone()
    with fts.cursor() as conn:
        timeline_store.insert(conn, _block(block_start, [SESSION_ENTRY], ["Feishu"]))
    fake_llm.set_default(delta_mod.STAGE, _payload())

    result = delta_mod.run_after_session(
        _cfg(),
        session_id="short-session",
        start_time=block_start + timedelta(seconds=20),
        end_time=block_start + timedelta(seconds=40),
    )

    assert result.written
    assert len(fake_llm.calls) == 1


def test_session_window_uses_strict_overlap_at_both_boundaries(ac_root, fake_llm) -> None:
    session_start = datetime(2026, 7, 2, 9, 0, 30).astimezone()
    session_end = session_start + timedelta(minutes=1)
    blocks = (
        _block(session_start - timedelta(minutes=1), ["touches before"], ["Feishu"]),
        _block(session_start.replace(second=0), ["overlaps start"], ["Feishu"]),
        _block(session_end.replace(second=0), ["overlaps end"], ["Feishu"]),
        _block(session_end, ["touches after"], ["Feishu"]),
    )
    with fts.cursor() as conn:
        for block in blocks:
            timeline_store.insert(conn, block)
    fake_llm.set_default(
        delta_mod.STAGE,
        _payload(entities=[], assertions=[], relations=[], events=[]),
    )

    result = delta_mod.run_after_session(
        _cfg(),
        session_id="boundary-session",
        start_time=session_start,
        end_time=session_end,
    )

    assert result.written
    sent = "".join(block["text"] for block in fake_llm.calls[0]["messages"][1]["content"])
    assert "overlaps start" in sent
    assert "overlaps end" in sent
    assert "touches before" not in sent
    assert "touches after" not in sent


def test_overlap_limit_keeps_latest_blocks_then_returns_them_chronologically(
    ac_root, fake_llm
) -> None:
    first_start = datetime(2026, 7, 2, 9, 0).astimezone()
    with fts.cursor() as conn:
        for index in range(121):
            timeline_store.insert(
                conn,
                _block(
                    first_start + timedelta(minutes=index),
                    [f"block-{index:03d}"],
                    ["Feishu"],
                ),
            )
    fake_llm.set_default(
        delta_mod.STAGE,
        _payload(entities=[], assertions=[], relations=[], events=[]),
    )
    cfg = _cfg()
    cfg.memory_delta.max_blocks = 120

    result = delta_mod.run_after_session(
        cfg,
        session_id="bounded-overlap-session",
        start_time=first_start + timedelta(seconds=30),
        end_time=first_start + timedelta(minutes=121, seconds=-30),
    )

    assert result.written
    sent = "".join(block["text"] for block in fake_llm.calls[0]["messages"][1]["content"])
    assert sum(f"block-{index:03d}" in sent for index in range(121)) == 120
    assert "block-000" not in sent
    assert sent.index("block-001") < sent.index("block-120")


def test_apply_result_errors_leave_delta_failed(ac_root, fake_llm, monkeypatch) -> None:
    from persome.writer import delta_apply

    start, end = _seed_session_blocks([SESSION_ENTRY])
    fake_llm.set_default(delta_mod.STAGE, _payload())
    monkeypatch.setattr(
        delta_apply,
        "apply_delta",
        lambda *args, **kwargs: delta_apply.ApplyResult(errors=["synthetic apply failure"]),
    )

    result = delta_mod.run_after_session(
        _cfg(), session_id="failed-apply", start_time=start, end_time=end
    )

    assert result.written
    assert not result.applied
    assert result.skipped_reason == "apply_failed"
    with fts.cursor() as conn:
        row = deltas_store.latest_for_session(conn, "failed-apply")
    assert row is not None and row["apply_status"] == "failed"


def test_stats_aggregates_latest_per_session(ac_root, fake_llm) -> None:
    start, end = _seed_session_blocks([SESSION_ENTRY])
    fake_llm.set_default(delta_mod.STAGE, _payload())
    delta_mod.run_after_session(_cfg(), session_id="s9", start_time=start, end_time=end)
    delta_mod.run_after_session(_cfg(), session_id="s9", start_time=start, end_time=end)
    with fts.cursor() as conn:
        agg = deltas_store.stats(conn)
    assert agg["rows"] == 2 and agg["sessions"] == 1  # latest-per-session, not double-counted
    assert agg["heads"]["entities"] == 1


def test_active_windows_are_incremental_and_idempotent(ac_root, fake_llm) -> None:
    start, end = _seed_session_blocks([SESSION_ENTRY, SESSION_ENTRY])
    middle = start + timedelta(minutes=1)
    fake_llm.set_default(delta_mod.STAGE, _payload())
    cfg = _cfg()

    first = delta_mod.ensure_active_window(
        cfg,
        session_id="s-live",
        start_time=start,
        end_time=middle,
    )
    duplicate = delta_mod.ensure_active_window(
        cfg,
        session_id="s-live",
        start_time=start,
        end_time=middle,
    )
    second = delta_mod.ensure_active_window(
        cfg,
        session_id="s-live",
        start_time=middle,
        end_time=end,
    )

    assert first.written and first.applied
    assert duplicate.skipped_reason == "already_processed"
    assert second.written and second.applied
    assert len(fake_llm.calls) == 2
    with fts.cursor() as conn:
        rows = conn.execute(
            "SELECT window_start, window_end, is_final FROM memory_deltas"
            " WHERE session_id=? ORDER BY id",
            ("s-live",),
        ).fetchall()
    assert [(row["window_start"], row["window_end"], row["is_final"]) for row in rows] == [
        (start.isoformat(), middle.isoformat(), 0),
        (middle.isoformat(), end.isoformat(), 0),
    ]


def test_gate_canonicalizes_honorific_ref_through_the_funnel(ac_root) -> None:
    from persome.evomem import identity as identity_mod

    roster = identity_mod.Roster.build([("\u5f20\u4f1f", ["\u4f1f\u54e5"])])
    session_text = "[Feishu] \u804a\u5929: \u5f20\u603b\u786e\u8ba4\u4e86\u5bf9\u8d26\u65b9\u6848"
    raw = {
        "entities": [
            {
                "ref": "\u5f20\u603b",
                "kind": "person",
                "quote": "\u5f20\u603b\u786e\u8ba4\u4e86\u5bf9\u8d26\u65b9\u6848",
                "confidence": 0.9,
            }
        ],
        "assertions": [],
        "relations": [],
        "events": [],
    }
    clean, dropped = delta_mod.gate_delta(
        raw, roster=roster, session_text=session_text, min_confidence=0.5
    )
    assert dropped == 0
    assert clean["entities"][0]["ref"] == "\u5f20\u4f1f"  # canonicalized, not the raw mention
    assert "new_entity" not in clean["entities"][0]


def test_gate_adds_deterministic_cooccurrence_knows(ac_root) -> None:
    from persome.evomem import identity as identity_mod

    roster = identity_mod.Roster.build(
        [("\u5f20\u4f1f", []), ("\u674e\u56db", []), ("\u738b\u4e94", [])]
    )
    session_text = "[Feishu] \u7fa4\u804a: \u5f20\u4f1f\u3001\u674e\u56db\u3001\u738b\u4e94 \u4e09\u4eba\u4e00\u8d77\u8fc7\u4e86\u65b9\u6848"
    q = "\u5f20\u4f1f\u3001\u674e\u56db\u3001\u738b\u4e94 \u4e09\u4eba\u4e00\u8d77\u8fc7\u4e86\u65b9\u6848"
    raw = {
        "entities": [
            {"ref": "\u5f20\u4f1f", "kind": "person", "quote": q, "confidence": 0.9},
            {"ref": "\u674e\u56db", "kind": "person", "quote": q, "confidence": 0.9},
            {"ref": "\u738b\u4e94", "kind": "person", "quote": q, "confidence": 0.9},
        ],
        "relations": [],
        "events": [],
        "assertions": [],
    }
    clean, _ = delta_mod.gate_delta(
        raw, roster=roster, session_text=session_text, min_confidence=0.5
    )
    knows = {
        frozenset((r["src"]["ref"], r["dst"]["ref"]))
        for r in clean["relations"]
        if r["predicate"] == "knows"
    }
    assert knows == {
        frozenset(("\u5f20\u4f1f", "\u674e\u56db")),
        frozenset(("\u5f20\u4f1f", "\u738b\u4e94")),
        frozenset(("\u674e\u56db", "\u738b\u4e94")),
    }


def test_gate_cooccurrence_off_is_noop(ac_root) -> None:
    from persome.evomem import identity as identity_mod

    roster = identity_mod.Roster.build([("\u5f20\u4f1f", []), ("\u674e\u56db", [])])
    raw = {
        "entities": [
            {
                "ref": "\u5f20\u4f1f",
                "kind": "person",
                "quote": "\u5f20\u4f1f \u548c \u674e\u56db",
                "confidence": 0.9,
            },
            {
                "ref": "\u674e\u56db",
                "kind": "person",
                "quote": "\u5f20\u4f1f \u548c \u674e\u56db",
                "confidence": 0.9,
            },
        ],
        "relations": [],
        "events": [],
        "assertions": [],
    }
    clean, _ = delta_mod.gate_delta(
        raw,
        roster=roster,
        session_text="[Feishu] \u5f20\u4f1f \u548c \u674e\u56db",
        min_confidence=0.5,
        cooccurrence=False,
    )
    assert clean["relations"] == []


def test_gate_folds_known_name_posing_as_new_entity(ac_root) -> None:
    """A new_entity whose name resolves to a known identity folds to its ref —
    the LLM cannot re-mint an existing person as a fresh node."""
    from persome.evomem import identity as identity_mod

    roster = identity_mod.Roster.build([("\u5f20\u4f1f", ["\u4f1f\u54e5"])])
    session_text = "[Feishu] \u804a\u5929: \u4f1f\u54e5\u786e\u8ba4\u4e86\u5bf9\u8d26\u65b9\u6848"
    raw = {
        "entities": [
            {
                "new_entity": "\u4f1f\u54e5",
                "kind": "person",
                "quote": "\u4f1f\u54e5\u786e\u8ba4\u4e86\u5bf9\u8d26\u65b9\u6848",
                "confidence": 0.9,
            }
        ],
        "assertions": [],
        "relations": [],
        "events": [],
    }
    clean, dropped = delta_mod.gate_delta(
        raw, roster=roster, session_text=session_text, min_confidence=0.5
    )
    assert dropped == 0
    assert (
        clean["entities"][0].get("ref") == "\u5f20\u4f1f"
        and "new_entity" not in clean["entities"][0]
    )


def test_gate_rejects_unknown_ref_but_keeps_genuine_new_entity(ac_root) -> None:
    from persome.evomem import identity as identity_mod

    roster = identity_mod.Roster.build([("\u5f20\u4f1f", [])])
    session_text = (
        "[Feishu] \u804a\u5929: \u738b\u4e94\u63d0\u4ea4\u4e86\u65b0\u7684\u63a5\u53e3\u6587\u6863"
    )
    raw = {
        "entities": [
            {
                "ref": "\u738b\u4e94",
                "kind": "person",
                "quote": "\u738b\u4e94\u63d0\u4ea4\u4e86\u65b0\u7684\u63a5\u53e3\u6587\u6863",
                "confidence": 0.9,
            },
            {
                "new_entity": "\u738b\u4e94",
                "kind": "person",
                "quote": "\u738b\u4e94\u63d0\u4ea4\u4e86\u65b0\u7684\u63a5\u53e3\u6587\u6863",
                "confidence": 0.9,
            },
        ],
        "assertions": [],
        "relations": [],
        "events": [],
    }
    clean, dropped = delta_mod.gate_delta(
        raw, roster=roster, session_text=session_text, min_confidence=0.5
    )
    assert dropped == 1  # the bare ref probing the store
    assert clean["entities"] == [
        {
            "kind": "person",
            "quote": "\u738b\u4e94\u63d0\u4ea4\u4e86\u65b0\u7684\u63a5\u53e3\u6587\u6863",
            "confidence": 0.9,
            "new_entity": "\u738b\u4e94",
            "ended": False,
        }
    ]


def test_relation_polarity_and_ended_normalize(ac_root, fake_llm) -> None:
    start, end = _seed_session_blocks([SESSION_ENTRY])
    fake_llm.set_default(
        delta_mod.STAGE,
        _payload(
            entities=[],
            assertions=[],
            relations=[
                {
                    "src": _ref("\u5f20\u4e09"),
                    "dst": _ref("\u5f20\u4e09"),
                    "predicate": "knows",
                    "polarity": "positive",  # off-set → coerced to "0"
                    "ended": "yes",  # non-bool → False
                    "quote": "\u548c\u5f20\u4e09\u786e\u8ba4\u4e86\u8bc4\u5ba1\u7ed3\u8bba",
                    "confidence": 0.9,
                },
                {
                    "src": _ref("\u5f20\u4e09"),
                    "dst": _ref("\u5f20\u4e09"),
                    "predicate": "knows",
                    "polarity": "-",
                    "ended": True,
                    "quote": "\u548c\u5f20\u4e09\u786e\u8ba4\u4e86\u8bc4\u5ba1\u7ed3\u8bba",
                    "confidence": 0.9,
                },
            ],
        ),
    )
    result = delta_mod.run_after_session(_cfg(), session_id="s9", start_time=start, end_time=end)
    assert result.counts["relations"] == 2
    import json as _json

    from persome.store import fts as fts_store
    from persome.store import memory_deltas as deltas_store

    with fts_store.cursor() as conn:
        row = deltas_store.latest_for_session(conn, "s9")
    rels = _json.loads(row["payload"])["relations"]
    assert (rels[0]["polarity"], rels[0]["ended"]) == ("0", False)
    assert (rels[1]["polarity"], rels[1]["ended"]) == ("-", True)


def test_entity_ended_defaults_false(ac_root, fake_llm) -> None:
    start, end = _seed_session_blocks([SESSION_ENTRY])
    fake_llm.set_default(
        delta_mod.STAGE,
        _payload(
            entities=[
                {
                    "new_entity": "\u5f20\u4e09",
                    "kind": "person",
                    "quote": "\u548c\u5f20\u4e09\u786e\u8ba4\u4e86\u8bc4\u5ba1\u7ed3\u8bba",
                    "confidence": 0.9,
                }
            ],
            assertions=[],
            relations=[],
        ),
    )
    result = delta_mod.run_after_session(_cfg(), session_id="sa", start_time=start, end_time=end)
    assert result.counts["entities"] == 1
    import json as _json

    from persome.store import fts as fts_store
    from persome.store import memory_deltas as deltas_store

    with fts_store.cursor() as conn:
        row = deltas_store.latest_for_session(conn, "sa")
    assert _json.loads(row["payload"])["entities"][0]["ended"] is False

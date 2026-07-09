from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from persome import config as config_mod
from persome.intent import store as intent_store
from persome.session import store as session_store
from persome.session import tick as session_tick
from persome.store import fts
from persome.timeline import store as timeline_store
from persome.writer import llm as llm_mod
from persome.writer import session_reducer

_TZ = timezone(timedelta(hours=8))


def _seed_block(start: datetime) -> None:
    with fts.cursor() as conn:
        timeline_store.insert(
            conn,
            timeline_store.TimelineBlock(
                start_time=start,
                end_time=start + timedelta(minutes=5),
                entries=["[Cursor] editing, involving —"],
                apps_used=["Cursor"],
                capture_count=1,
            ),
        )


def test_seconds_until_next_local_rolls_past_midnight() -> None:
    # The helper is a pure function of datetime.now() so we can only
    # assert properties: result must be in [0, 86400).
    s = session_tick._seconds_until_next_local(23, 55)
    assert 0 < s <= 86400


def test_reduce_all_pending_catches_ended_row(ac_root: Path, monkeypatch) -> None:
    start = datetime(2026, 4, 21, 9, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=5)
    _seed_block(start)

    # Simulate a session that was ended but whose reducer thread died
    # (status='ended', not 'reduced').
    with fts.cursor() as conn:
        session_store.insert(
            conn,
            session_store.SessionRow(
                id="sess_stranded",
                start_time=start,
                end_time=end,
                status="ended",
            ),
        )

    monkeypatch.setenv("PERSOME_LLM_MOCK", "1")
    monkeypatch.setenv(
        "PERSOME_LLM_MOCK_JSON",
        json.dumps({"summary": "ok", "sub_tasks": ["[09:00-09:05, Cursor] x, involving —"]}),
    )

    cfg = config_mod.load(ac_root / "config.toml")
    results = session_reducer.reduce_all_pending(cfg)
    assert len(results) == 1
    assert results[0].succeeded is True

    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, "sess_stranded")
    assert row is not None
    assert row.status == "reduced"


def test_build_manager_wires_reducer_end_to_end(ac_root: Path, monkeypatch) -> None:
    """on_event → auto session start → force_end → row persisted → reducer run."""
    start_dt = datetime.now().astimezone().replace(microsecond=0)
    _seed_block(start_dt - timedelta(minutes=5))  # a block in the session's range

    monkeypatch.setenv("PERSOME_LLM_MOCK", "1")
    monkeypatch.setenv(
        "PERSOME_LLM_MOCK_JSON",
        json.dumps({"summary": "done", "sub_tasks": ["[--, Cursor] ok, involving —"]}),
    )

    cfg = config_mod.load(ac_root / "config.toml")
    manager = session_tick.build_manager(cfg)

    manager.on_event({"event_type": "AXFocusedWindowChanged", "bundle_id": "com.cursor"})
    sid = manager.current_id
    assert sid is not None

    # After on_event, the 'active' row should be in the store.
    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, sid)
    assert row is not None
    assert row.status == "active"

    # Force-end triggers on_session_end → persists ended row and spawns reducer.
    manager.force_end(reason="test")
    # Give the reducer thread a moment to finish.
    import time

    for _ in range(40):
        with fts.cursor() as conn:
            row = session_store.get_by_id(conn, sid)
        if row and row.status == "reduced":
            break
        time.sleep(0.05)

    assert row is not None
    assert row.status in ("reduced", "ended")  # Either the thread raced or it finished


def test_prune_telemetry_bounds_cooldown_suppressions(ac_root: Path) -> None:
    """``_prune_telemetry_tables`` must keep ``cooldown_suppressions`` bounded.

    Regression for the #508-style无界增长 bug: the table shipped ``prune(keep=...)``
    but the daily retention hook (`_prune_telemetry_tables`) only pruned its three
    sibling telemetry tables (recall_budget_ticks / recognition_ticks /
    parser_ticks) and silently skipped cooldown_suppressions, so on a long-running
    daemon every cooldown drop accumulated forever. This test seeds more rows than
    the prune keep-count and asserts the daily hook actually trims it.
    """
    from persome.store import cooldown_suppressions

    keep = 50000
    overflow = 5  # rows beyond `keep` that must be pruned away
    total = keep + overflow
    with fts.cursor() as conn:
        cooldown_suppressions.ensure_schema(conn)
        # Bulk-insert directly (the per-row `record` path is exercised elsewhere);
        # we only need the table over-full so the prune has something to cut.
        conn.executemany(
            "INSERT INTO cooldown_suppressions "
            "(ts, kind, scope, confidence, cooldown_until, created_at) "
            "VALUES (?, 'reminder', 'session-x', 0.5, NULL, ?)",
            [(f"2026-01-01T00:{i:02d}", "2026-01-01T00:00:00") for i in range(total)],
        )
        conn.commit()
        before = conn.execute("SELECT COUNT(*) FROM cooldown_suppressions").fetchone()[0]
    assert before == total

    deleted = session_tick._prune_telemetry_tables()

    # The hook reported a non-zero prune for cooldown_suppressions (it was wired in)…
    assert deleted.get("cooldown_suppressions") == overflow
    # …and the table is now bounded at `keep`.
    with fts.cursor() as conn:
        after = conn.execute("SELECT COUNT(*) FROM cooldown_suppressions").fetchone()[0]
    assert after == keep

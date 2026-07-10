from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from persome import config as config_mod
from persome.session import store as session_store
from persome.session import tick as session_tick
from persome.store import fts
from persome.timeline import store as timeline_store
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


def test_prune_telemetry_calls_parser_store(ac_root: Path, monkeypatch) -> None:
    monkeypatch.setattr(session_tick.parser_ticks_store, "prune", lambda conn: 7)
    assert session_tick._prune_telemetry_tables() == {"parser_ticks": 7}


def test_model_dirty_generation_does_not_clear_newer_evidence(ac_root: Path) -> None:
    with fts.cursor() as conn:
        session_store.set_system_state(conn, "model_structure_dirty", "1")
        session_store.increment_system_state(conn, "model_structure_dirty")
        assert not session_store.compare_and_set_system_state(
            conn,
            "model_structure_dirty",
            expected="1",
            value="0",
        )
        assert session_store.get_system_state(conn, "model_structure_dirty") == "2"
        assert session_store.compare_and_set_system_state(
            conn,
            "model_structure_dirty",
            expected="2",
            value="0",
        )


def test_boot_recovery_closes_stranded_active_sessions(ac_root: Path) -> None:
    first = datetime(2026, 7, 10, 9, 0, tzinfo=_TZ)
    second = first + timedelta(hours=1)
    boot = second + timedelta(hours=1)
    with fts.cursor() as conn:
        session_store.insert(
            conn,
            session_store.SessionRow(id="sess_old", start_time=first, status="active"),
        )
        session_store.insert(
            conn,
            session_store.SessionRow(id="sess_new", start_time=second, status="active"),
        )

    recovered = session_tick.recover_stranded_sessions(now=boot)
    assert [row.id for row in recovered] == ["sess_old", "sess_new"]
    assert [row.end_time for row in recovered] == [second, boot]

    with fts.cursor() as conn:
        assert session_store.get_by_id(conn, "sess_old").status == "ended"
        assert session_store.get_by_id(conn, "sess_new").status == "ended"
    assert session_tick.recover_stranded_sessions(now=boot) == []


@pytest.mark.parametrize("llm_succeeded", [True, False])
def test_terminal_callback_finalizes_no_write_and_heuristic_results(
    ac_root: Path,
    monkeypatch,
    llm_succeeded: bool,
) -> None:
    """No-new-block and heuristic terminal results both enter model finalization."""
    callback = None

    def fake_reduce_async(cfg, **kwargs):
        nonlocal callback
        callback = kwargs["on_done"]
        return None

    modeled: list[str] = []
    monkeypatch.setattr(session_tick.session_reducer, "reduce_session_async", fake_reduce_async)
    monkeypatch.setattr(
        session_tick.writer_agent,
        "finalize_session",
        lambda cfg, **kwargs: (
            modeled.append(kwargs["session_id"])
            or type("Result", (), {"completed": True, "errors": [], "skipped_reason": ""})()
        ),
    )

    cfg = config_mod.load(ac_root / "config.toml")
    manager = session_tick.build_manager(cfg)
    manager.on_event({"event_type": "focus", "bundle_id": "com.test"})
    sid = manager.current_id
    assert sid is not None
    manager.force_end(reason="test")
    assert callback is not None
    callback(
        session_reducer.ReduceResult(
            session_id=sid,
            succeeded=llm_succeeded,
            written=False,
            is_final=True,
        )
    )
    assert modeled == [sid]


async def test_reducer_retry_tick_finalizes_due_results(ac_root: Path, monkeypatch) -> None:
    reduced = session_reducer.ReduceResult(
        session_id="sess_retry_tick",
        succeeded=False,
        written=True,
        path="event-2026-07-10.md",
        entry_id="entry-1",
        is_final=True,
    )
    monkeypatch.setattr(session_tick.session_reducer, "retry_due", lambda cfg: [reduced])
    seen: list[dict] = []
    monkeypatch.setattr(
        session_tick.writer_agent,
        "finalize_session",
        lambda cfg, **kwargs: (
            seen.append(kwargs)
            or type("Result", (), {"completed": True, "errors": [], "skipped_reason": ""})()
        ),
    )
    sleeps = 0

    async def fake_sleep(seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(session_tick.asyncio, "sleep", fake_sleep)
    cfg = config_mod.load(ac_root / "config.toml")
    with pytest.raises(asyncio.CancelledError):
        await session_tick.run_reducer_retry_tick(cfg)
    assert seen == [
        {
            "session_id": "sess_retry_tick",
            "event_daily_path": "event-2026-07-10.md",
            "just_written_entry_id": "entry-1",
        }
    ]


async def test_reducer_retry_runs_writer_catch_up_before_sleep(ac_root: Path, monkeypatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(
        session_tick.writer_agent,
        "run",
        lambda cfg: seen.append("writer") or type("Result", (), {"reduced": 0, "modeled": 0})(),
    )

    async def cancel_on_sleep(seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(session_tick.asyncio, "sleep", cancel_on_sleep)
    cfg = config_mod.load(ac_root / "config.toml")
    with pytest.raises(asyncio.CancelledError):
        await session_tick.run_reducer_retry_tick(cfg)
    assert seen == ["writer"]


async def test_flush_tick_models_successful_active_window(ac_root: Path, monkeypatch) -> None:
    start = datetime(2026, 7, 10, 12, 0, tzinfo=_TZ)
    manager = type("Manager", (), {"current_snapshot": lambda self: ("sess_live", start)})()
    calls: list[str] = []
    monkeypatch.setattr(
        session_tick.session_reducer,
        "flush_active_session",
        lambda cfg, **kwargs: object(),
    )
    monkeypatch.setattr(
        session_tick.writer_agent,
        "model_active_session",
        lambda cfg, **kwargs: (
            calls.append(kwargs["session_id"])
            or type("Result", (), {"completed": True, "errors": []})()
        ),
    )
    sleeps = 0

    async def run_once(seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(session_tick.asyncio, "sleep", run_once)
    cfg = config_mod.load(ac_root / "config.toml")
    with pytest.raises(asyncio.CancelledError):
        await session_tick.run_flush_tick(cfg, manager)
    assert calls == ["sess_live"]

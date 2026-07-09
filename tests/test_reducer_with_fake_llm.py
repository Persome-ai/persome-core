"""Reducer tests exercising the ``fake_llm`` fixture and JSON fixtures.

These complement ``test_session_reducer.py`` by demonstrating the
fixture-file workflow: load a canned JSON response from
``tests/fixtures/llm/reducer/*.json``, feed it to ``fake_llm``, and
assert the downstream behaviour.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from persome import config as config_mod
from persome import paths
from persome.session import store as session_store
from persome.store import fts
from persome.timeline import store as timeline_store
from persome.writer import session_reducer

_TZ = timezone(timedelta(hours=8))


def _seed_blocks(start: datetime) -> list[timeline_store.TimelineBlock]:
    bs: list[timeline_store.TimelineBlock] = []
    with fts.cursor() as conn:
        for i in range(3):
            b = timeline_store.TimelineBlock(
                start_time=start + timedelta(minutes=5 * i),
                end_time=start + timedelta(minutes=5 * (i + 1)),
                timezone="+08:00",
                entries=[f"[Cursor] edited file_{i}.py, involving nothing"],
                apps_used=["Cursor"],
                capture_count=6,
            )
            timeline_store.insert(conn, b)
            bs.append(b)
    return bs


def test_reducer_fixture_happy_path(ac_root: Path, fake_llm, load_llm_fixture) -> None:
    """Load a fixture JSON file and assert the reducer writes a valid entry."""
    start = datetime(2026, 4, 21, 10, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=15)
    _seed_blocks(start)

    with fts.cursor() as conn:
        session_store.insert(
            conn,
            session_store.SessionRow(
                id="sess_fixture",
                start_time=start,
                end_time=end,
                status="ended",
            ),
        )

    fake_llm.set_default("reducer", load_llm_fixture("reducer", "happy_path"))

    cfg = config_mod.load(ac_root / "config.toml")
    result = session_reducer.reduce_session(
        cfg,
        session_id="sess_fixture",
        start_time=start,
        end_time=end,
    )

    assert result.succeeded is True
    assert result.written is True
    assert result.path == "event-2026-04-21.md"

    md = (paths.memory_dir() / "event-2026-04-21.md").read_text()
    assert "Session sess_fixture" in md
    assert "[10:00-10:05, Cursor]" in md


def test_reducer_fixture_malformed_keys_falls_back_to_heuristic(
    ac_root: Path,
    fake_llm,
    load_llm_fixture,
) -> None:
    """A fixture with wrong keys (valid JSON, wrong schema) → heuristic sub_tasks
    but the reducer still marks the session as succeeded because the JSON
    itself parsed successfully.
    """
    start = datetime(2026, 4, 21, 11, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=15)
    _seed_blocks(start)

    with fts.cursor() as conn:
        session_store.insert(
            conn,
            session_store.SessionRow(
                id="sess_malformed",
                start_time=start,
                end_time=end,
                status="ended",
            ),
        )

    fake_llm.set_default("reducer", load_llm_fixture("reducer", "malformed_fields"))

    cfg = config_mod.load(ac_root / "config.toml")
    result = session_reducer.reduce_session(
        cfg,
        session_id="sess_malformed",
        start_time=start,
        end_time=end,
    )

    # Valid JSON with wrong keys → heuristic fallback, but succeeded=True
    # because the JSON parse itself did not fail.
    assert result.succeeded is True
    assert result.written is True
    assert "active during the session" in result.sub_tasks[0]


def test_reducer_fixture_invalid_json_schedules_retry(
    ac_root: Path,
    fake_llm,
) -> None:
    """Truly invalid JSON triggers JSONDecodeError → retry scheduled."""
    start = datetime(2026, 4, 21, 11, 30, tzinfo=_TZ)
    end = start + timedelta(minutes=15)
    _seed_blocks(start)

    with fts.cursor() as conn:
        session_store.insert(
            conn,
            session_store.SessionRow(
                id="sess_bad_json",
                start_time=start,
                end_time=end,
                status="ended",
            ),
        )

    fake_llm.set_default("reducer", "this is not json {")

    cfg = config_mod.load(ac_root / "config.toml")
    result = session_reducer.reduce_session(
        cfg,
        session_id="sess_bad_json",
        start_time=start,
        end_time=end,
    )

    assert result.succeeded is False
    assert result.written is False

    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, "sess_bad_json")
    assert row is not None
    assert row.status == "failed"
    assert row.retry_count == 1


def test_reducer_fixture_empty_blocks_no_op(
    ac_root: Path,
    fake_llm,
    load_llm_fixture,
) -> None:
    """An empty-blocks fixture still passes through the happy path because
    the reducer returns early when there are zero timeline blocks.
    """
    start = datetime(2026, 4, 21, 12, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=5)

    with fts.cursor() as conn:
        session_store.insert(
            conn,
            session_store.SessionRow(
                id="sess_empty",
                start_time=start,
                end_time=end,
                status="ended",
            ),
        )

    # Even though we set a default, no LLM call happens because blocks=0.
    fake_llm.set_default("reducer", load_llm_fixture("reducer", "empty_blocks"))

    cfg = config_mod.load(ac_root / "config.toml")
    result = session_reducer.reduce_session(
        cfg,
        session_id="sess_empty",
        start_time=start,
        end_time=end,
    )

    assert result.succeeded is True
    assert result.written is False
    assert fake_llm.calls == []  # no LLM invocation

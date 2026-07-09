"""Unit tests for the dream_runs / dream_events DAO."""

from __future__ import annotations

import pytest

from persome.store import dream_runs, fts


@pytest.fixture
def conn(ac_root):
    """Provide a connection to a fresh sqlite DB under a tmp PERSOME_ROOT.

    ``fts.connect`` runs ``ensure_schema`` for all DAOs (incl. dream_runs)
    so each test starts with empty but well-formed tables.
    """
    with fts.cursor() as c:
        yield c


# ─── start_run ────────────────────────────────────────────────────────────


def test_start_run_inserts_running_row(conn) -> None:
    run_id = dream_runs.start_run(conn, trigger="manual")
    row = dream_runs.get_run(conn, run_id)

    assert row is not None
    assert row.id == run_id
    assert row.trigger == "manual"
    assert row.status == "running"
    assert row.ended_at is None
    assert row.summary == ""
    assert row.written_count == 0
    assert row.iterations == 0
    assert row.error == ""
    assert row.skipped_reason == ""
    assert row.written_ids == []
    assert row.created_paths == []


def test_start_run_returns_distinct_ids(conn) -> None:
    a = dream_runs.start_run(conn, trigger="manual")
    b = dream_runs.start_run(conn, trigger="daily-tick")
    assert a != b


# ─── end_run ──────────────────────────────────────────────────────────────


def test_end_run_committed_marks_committed_and_persists_outputs(conn) -> None:
    run_id = dream_runs.start_run(conn, trigger="manual")
    dream_runs.end_run(
        conn,
        run_id,
        committed=True,
        summary="Wrote 2 entries",
        written_ids=["20260526-1430-aaa", "20260526-1430-bbb"],
        created_paths=["user-preferences.md"],
        iterations=12,
        skipped_reason="",
    )

    row = dream_runs.get_run(conn, run_id)
    assert row is not None
    assert row.status == "committed"
    assert row.ended_at is not None
    assert row.summary == "Wrote 2 entries"
    assert row.written_count == 2  # derived from len(written_ids)
    assert row.written_ids == ["20260526-1430-aaa", "20260526-1430-bbb"]
    assert row.created_paths == ["user-preferences.md"]
    assert row.iterations == 12
    assert row.skipped_reason == ""


def test_end_run_not_committed_marks_skipped(conn) -> None:
    run_id = dream_runs.start_run(conn, trigger="daily-tick")
    dream_runs.end_run(
        conn,
        run_id,
        committed=False,
        summary="",
        written_ids=[],
        created_paths=[],
        iterations=0,
        skipped_reason="loop_exhausted",
    )

    row = dream_runs.get_run(conn, run_id)
    assert row is not None
    assert row.status == "skipped"
    assert row.skipped_reason == "loop_exhausted"
    assert row.written_count == 0


# ─── fail_run ─────────────────────────────────────────────────────────────


def test_fail_run_marks_failed_and_records_error(conn) -> None:
    run_id = dream_runs.start_run(conn, trigger="manual")
    dream_runs.fail_run(conn, run_id, error="LLM provider returned 500")

    row = dream_runs.get_run(conn, run_id)
    assert row is not None
    assert row.status == "failed"
    assert row.error == "LLM provider returned 500"
    assert row.ended_at is not None


# ─── mark_orphans_failed ──────────────────────────────────────────────────


def test_mark_orphans_failed_flips_running_rows(conn) -> None:
    a = dream_runs.start_run(conn, trigger="manual")
    b = dream_runs.start_run(conn, trigger="daily-tick")
    # A third row finished cleanly — must NOT be touched.
    c = dream_runs.start_run(conn, trigger="manual")
    dream_runs.end_run(
        conn,
        c,
        committed=True,
        summary="ok",
        written_ids=[],
        created_paths=[],
        iterations=1,
        skipped_reason="",
    )

    flipped = dream_runs.mark_orphans_failed(conn)
    assert flipped == 2

    assert dream_runs.get_run(conn, a).status == "failed"
    assert dream_runs.get_run(conn, a).error == "daemon restarted"
    assert dream_runs.get_run(conn, b).status == "failed"
    assert dream_runs.get_run(conn, c).status == "committed"  # untouched


def test_mark_orphans_failed_is_idempotent(conn) -> None:
    dream_runs.start_run(conn, trigger="manual")
    assert dream_runs.mark_orphans_failed(conn) == 1
    # Second call: nothing left to flip.
    assert dream_runs.mark_orphans_failed(conn) == 0


# ─── events ───────────────────────────────────────────────────────────────


def test_append_event_persists_payload_as_json(conn) -> None:
    run_id = dream_runs.start_run(conn, trigger="manual")
    dream_runs.append_event(
        conn, run_id, "tool_call", {"name": "read_memory", "arguments": {"path": "x.md"}}
    )
    dream_runs.append_event(conn, run_id, "llm_text", {"text": "thinking…", "reasoning": "r1"})

    events = dream_runs.list_events(conn, run_id)
    assert len(events) == 2
    # Insert order preserved (ORDER BY id ASC).
    assert events[0].type == "tool_call"
    assert events[0].payload == {"name": "read_memory", "arguments": {"path": "x.md"}}
    assert events[1].type == "llm_text"
    assert events[1].payload["text"] == "thinking…"


def test_list_events_scoped_to_run(conn) -> None:
    run_a = dream_runs.start_run(conn, trigger="manual")
    run_b = dream_runs.start_run(conn, trigger="daily-tick")
    dream_runs.append_event(conn, run_a, "tool_call", {"name": "a"})
    dream_runs.append_event(conn, run_b, "tool_call", {"name": "b"})

    assert [e.payload["name"] for e in dream_runs.list_events(conn, run_a)] == ["a"]
    assert [e.payload["name"] for e in dream_runs.list_events(conn, run_b)] == ["b"]


# ─── list_runs / get_run ──────────────────────────────────────────────────


def test_list_runs_orders_newest_first(conn) -> None:
    ids = [dream_runs.start_run(conn, trigger="manual") for _ in range(3)]
    runs = dream_runs.list_runs(conn)
    assert [r.id for r in runs] == list(reversed(ids))


def test_list_runs_respects_limit(conn) -> None:
    for _ in range(5):
        dream_runs.start_run(conn, trigger="manual")
    assert len(dream_runs.list_runs(conn, limit=2)) == 2


def test_get_run_missing_returns_none(conn) -> None:
    assert dream_runs.get_run(conn, 9999) is None

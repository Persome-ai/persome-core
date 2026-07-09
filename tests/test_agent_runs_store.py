"""Unit tests for the agent_runs DAO."""

from __future__ import annotations

from datetime import datetime, timedelta

from persome.store import agent_runs as store
from persome.store import fts


def _iso(dt: datetime) -> str:
    return dt.astimezone().isoformat()


def test_insert_and_window_filter(ac_root) -> None:
    now = datetime.now().astimezone()
    with fts.cursor() as conn:
        store.ensure_schema(conn)
        # in-window run (today)
        in_id = store.insert_run(
            conn,
            kind="dream",
            title="今日整理",
            status="committed",
            trigger="daily-tick",
            dispatch_source="system",
            enqueued_at=_iso(now),
            started_at=_iso(now),
            ended_at=_iso(now),
        )
        # out-of-window run (10 days ago)
        store.insert_run(
            conn,
            kind="dream",
            title="旧整理",
            status="committed",
            trigger="daily-tick",
            dispatch_source="system",
            enqueued_at=_iso(now - timedelta(days=10)),
            started_at=_iso(now - timedelta(days=10)),
            ended_at=_iso(now - timedelta(days=10)),
        )
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        rows = store.list_runs_in_window(conn, start=start, end=end)

    assert [r.id for r in rows] == [in_id]
    assert rows[0].title == "今日整理"
    assert rows[0].kind == "dream"


def test_status_filter_and_queued_anchor(ac_root) -> None:
    now = datetime.now().astimezone()
    with fts.cursor() as conn:
        store.ensure_schema(conn)
        # queued run: started_at is NULL → window anchors on enqueued_at
        q_id = store.insert_run(
            conn,
            kind="bootstrap",
            title="排队中",
            status="queued",
            trigger="user",
            dispatch_source="user",
            enqueued_at=_iso(now),
            started_at=None,
            ended_at=None,
        )
        store.insert_run(
            conn,
            kind="dream",
            title="跑完",
            status="committed",
            trigger="daily-tick",
            dispatch_source="system",
            enqueued_at=_iso(now),
            started_at=_iso(now),
            ended_at=_iso(now),
        )
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        queued = store.list_runs_in_window(conn, start=start, end=end, statuses=["queued"])

    assert [r.id for r in queued] == [q_id]
    assert queued[0].started_at is None


def test_schema_auto_created_by_connect(ac_root) -> None:
    """A fresh fts.cursor() connection must already have agent_runs — no manual
    ensure_schema. Proves it's wired into connect()."""
    with fts.cursor() as conn:
        names = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert "agent_runs" in names
    assert "agent_run_events" in names

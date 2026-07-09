"""DAO for ``dream_runs`` + ``dream_events`` — recordings of each dream
stage execution.

Markdown files under ``~/.persome/memory/`` remain the canonical
knowledge output of dream; this module only persists the "agent recording
tape" so the UI can show which tools the model called and surface history
of past runs. Schema is intentionally narrow and append-only.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..logger import get

logger = get("persome.store.dream_runs")

SCHEMA = """
CREATE TABLE IF NOT EXISTS dream_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    trigger         TEXT NOT NULL,              -- 'manual' | 'daily-tick'
    status          TEXT NOT NULL,              -- 'running' | 'committed' | 'skipped' | 'failed'
    summary         TEXT NOT NULL DEFAULT '',
    written_count   INTEGER NOT NULL DEFAULT 0,
    iterations      INTEGER NOT NULL DEFAULT 0,
    error           TEXT NOT NULL DEFAULT '',
    skipped_reason  TEXT NOT NULL DEFAULT '',
    written_ids     TEXT NOT NULL DEFAULT '[]', -- JSON array of memory entry ids
    created_paths   TEXT NOT NULL DEFAULT '[]'  -- JSON array of file paths
);
CREATE INDEX IF NOT EXISTS idx_dream_runs_started
    ON dream_runs(started_at DESC);

CREATE TABLE IF NOT EXISTS dream_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  INTEGER NOT NULL REFERENCES dream_runs(id) ON DELETE CASCADE,
    ts      TEXT NOT NULL,
    type    TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dream_events_run
    ON dream_events(run_id, id);
"""


@dataclass
class DreamRun:
    id: int
    started_at: datetime
    ended_at: datetime | None
    trigger: str
    status: str
    summary: str = ""
    written_count: int = 0
    iterations: int = 0
    error: str = ""
    skipped_reason: str = ""
    written_ids: list[str] = field(default_factory=list)
    created_paths: list[str] = field(default_factory=list)


@dataclass
class DreamEvent:
    id: int
    run_id: int
    ts: datetime
    type: str
    payload: dict[str, Any]


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


# ── writes ────────────────────────────────────────────────────────────────


def start_run(conn: sqlite3.Connection, *, trigger: str) -> int:
    """Insert a new running row and return its id."""
    cur = conn.execute(
        """
        INSERT INTO dream_runs (started_at, trigger, status)
        VALUES (?, ?, 'running')
        """,
        (_now_iso(), trigger),
    )
    conn.commit()
    run_id = int(cur.lastrowid)  # type: ignore[arg-type]
    logger.info("dream_runs: started run %s (trigger=%s)", run_id, trigger)
    return run_id


def end_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    committed: bool,
    summary: str,
    written_ids: list[str],
    created_paths: list[str],
    iterations: int,
    skipped_reason: str,
) -> None:
    """Mark a run finished. ``committed=True`` → 'committed'; ``committed=False``
    with a ``skipped_reason`` → 'skipped'; otherwise → 'skipped' with empty
    reason (no-op run)."""
    status = "committed" if committed else "skipped"
    conn.execute(
        """
        UPDATE dream_runs SET
            ended_at = ?, status = ?, summary = ?, written_count = ?,
            iterations = ?, skipped_reason = ?, written_ids = ?, created_paths = ?
         WHERE id = ?
        """,
        (
            _now_iso(),
            status,
            summary,
            len(written_ids),
            iterations,
            skipped_reason,
            json.dumps(written_ids, ensure_ascii=False),
            json.dumps(created_paths, ensure_ascii=False),
            run_id,
        ),
    )
    conn.commit()
    logger.info(
        "dream_runs: ended run %s (status=%s, written=%d, iters=%d)",
        run_id,
        status,
        len(written_ids),
        iterations,
    )


def fail_run(conn: sqlite3.Connection, run_id: int, *, error: str) -> None:
    conn.execute(
        """
        UPDATE dream_runs SET ended_at = ?, status = 'failed', error = ?
         WHERE id = ?
        """,
        (_now_iso(), error, run_id),
    )
    conn.commit()
    logger.warning("dream_runs: failed run %s (%s)", run_id, error)


def mark_orphans_failed(conn: sqlite3.Connection) -> int:
    """Run on daemon startup — any row still status='running' is a leftover
    from a previous process that died mid-dream. Flip it to failed so the UI
    doesn't show a forever-pending row.
    Returns count of rows updated."""
    cur = conn.execute(
        """
        UPDATE dream_runs SET
            ended_at = ?, status = 'failed', error = 'daemon restarted'
         WHERE status = 'running'
        """,
        (_now_iso(),),
    )
    conn.commit()
    n = int(cur.rowcount)
    if n:
        logger.info("dream_runs: marked %d orphan running rows as failed", n)
    return n


def append_event(
    conn: sqlite3.Connection,
    run_id: int,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO dream_events (run_id, ts, type, payload)
        VALUES (?, ?, ?, ?)
        """,
        (run_id, _now_iso(), event_type, json.dumps(payload, ensure_ascii=False)),
    )
    conn.commit()


# ── reads ─────────────────────────────────────────────────────────────────


def list_runs(conn: sqlite3.Connection, *, limit: int = 20) -> list[DreamRun]:
    rows = conn.execute(
        "SELECT * FROM dream_runs ORDER BY started_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_row_to_run(r) for r in rows]


def get_run(conn: sqlite3.Connection, run_id: int) -> DreamRun | None:
    row = conn.execute(
        "SELECT * FROM dream_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    return _row_to_run(row) if row else None


def list_events(
    conn: sqlite3.Connection, run_id: int, *, tail: int | None = None
) -> list[DreamEvent]:
    """Events for one run, chronological (id ASC).

    ``tail=N`` returns only the most recent N events (still in chronological
    order) — for callers that just want the latest activity without loading a
    long run's full event tape. Default (None) returns all events.
    """
    if tail is not None:
        rows = conn.execute(
            "SELECT * FROM ("
            "  SELECT * FROM dream_events WHERE run_id = ? ORDER BY id DESC LIMIT ?"
            ") ORDER BY id ASC",
            (run_id, tail),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM dream_events WHERE run_id = ? ORDER BY id ASC",
            (run_id,),
        ).fetchall()
    return [
        DreamEvent(
            id=r["id"],
            run_id=r["run_id"],
            ts=datetime.fromisoformat(r["ts"]),
            type=r["type"],
            payload=json.loads(r["payload"] or "{}"),
        )
        for r in rows
    ]


def _row_to_run(r: sqlite3.Row) -> DreamRun:
    def _dt(v: str | None) -> datetime | None:
        return datetime.fromisoformat(v) if v else None

    started = _dt(r["started_at"])
    assert started is not None  # NOT NULL in schema
    return DreamRun(
        id=r["id"],
        started_at=started,
        ended_at=_dt(r["ended_at"]),
        trigger=r["trigger"],
        status=r["status"],
        summary=r["summary"] or "",
        written_count=r["written_count"] or 0,
        iterations=r["iterations"] or 0,
        error=r["error"] or "",
        skipped_reason=r["skipped_reason"] or "",
        written_ids=json.loads(r["written_ids"] or "[]"),
        created_paths=json.loads(r["created_paths"] or "[]"),
    )

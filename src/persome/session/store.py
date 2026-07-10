"""SQLite-backed store for work sessions.

Lives in the shared ``index.db`` alongside ``timeline_blocks`` and
``entries``. A session row tracks when a user was actively working and
carries the S2-reducer retry state (so retries survive a daemon
restart and the daily 23:55 safety-net can pick up unfinished work).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

SessionStatus = Literal["active", "ended", "reduced", "failed"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    start_time TEXT NOT NULL,
    end_time TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    flush_end TEXT,
    classified_end TEXT,
    pattern_detected_end TEXT,
    delta_end TEXT,
    modeled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_start ON sessions(start_time);
CREATE INDEX IF NOT EXISTS idx_sessions_retry ON sessions(next_retry_at)
    WHERE status = 'failed';

CREATE TABLE IF NOT EXISTS system_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Backfill columns added after initial schema."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
    if "flush_end" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN flush_end TEXT")
    if "classified_end" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN classified_end TEXT")
    if "pattern_detected_end" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN pattern_detected_end TEXT")
    if "delta_end" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN delta_end TEXT")
    if "modeled_at" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN modeled_at TEXT")
        # Existing reduced sessions were finalized by the pre-column callback.
        # Do not replay a user's whole history merely because they upgraded.
        conn.execute(
            "UPDATE sessions SET modeled_at=updated_at"
            " WHERE status='reduced' AND modeled_at IS NULL"
        )


@dataclass
class SessionRow:
    id: str
    start_time: datetime
    end_time: datetime | None = None
    status: SessionStatus = "active"
    retry_count: int = 0
    next_retry_at: datetime | None = None
    last_error: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    flush_end: datetime | None = None
    classified_end: datetime | None = None
    pattern_detected_end: datetime | None = None
    delta_end: datetime | None = None
    modeled_at: datetime | None = None


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)


def insert(conn: sqlite3.Connection, row: SessionRow) -> None:
    now = datetime.now().astimezone().isoformat()
    conn.execute(
        """
        INSERT OR IGNORE INTO sessions
            (id, start_time, end_time, status, retry_count, next_retry_at,
             last_error, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row.id,
            row.start_time.isoformat(),
            row.end_time.isoformat() if row.end_time else None,
            row.status,
            row.retry_count,
            row.next_retry_at.isoformat() if row.next_retry_at else None,
            row.last_error,
            (row.created_at or datetime.now().astimezone()).isoformat(),
            (row.updated_at or datetime.now().astimezone()).isoformat() or now,
        ),
    )


def mark_ended(conn: sqlite3.Connection, session_id: str, end_time: datetime) -> None:
    conn.execute(
        """
        UPDATE sessions
           SET end_time=?, status='ended', updated_at=?
         WHERE id=? AND status='active'
        """,
        (end_time.isoformat(), datetime.now().astimezone().isoformat(), session_id),
    )


def recover_active(conn: sqlite3.Connection, *, recovered_at: datetime) -> list[SessionRow]:
    """Close sessions left active by a previous daemon process.

    A later session start is the safest boundary for an older stranded row. The
    newest row closes at daemon boot. The status guard in ``mark_ended`` keeps
    this recovery idempotent across repeated starts.
    """
    rows = conn.execute("SELECT * FROM sessions ORDER BY start_time ASC, created_at ASC").fetchall()
    sessions = [_to_row(row) for row in rows]
    recovered: list[SessionRow] = []
    for index, row in enumerate(sessions):
        if row.status != "active":
            continue
        next_start = next(
            (
                candidate.start_time
                for candidate in sessions[index + 1 :]
                if candidate.start_time > row.start_time
            ),
            None,
        )
        end_time = min(recovered_at, next_start) if next_start else recovered_at
        end_time = max(row.start_time, end_time)
        mark_ended(conn, row.id, end_time)
        row.end_time = end_time
        row.status = "ended"
        recovered.append(row)
    return recovered


def mark_reduced(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute(
        "UPDATE sessions SET status='reduced', updated_at=? WHERE id=?",
        (datetime.now().astimezone().isoformat(), session_id),
    )


def mark_failed(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    error: str,
    next_retry_at: datetime | None,
) -> None:
    conn.execute(
        """
        UPDATE sessions
           SET status='failed',
               retry_count = retry_count + 1,
               next_retry_at=?,
               last_error=?,
               updated_at=?
         WHERE id=?
        """,
        (
            next_retry_at.isoformat() if next_retry_at else None,
            error,
            datetime.now().astimezone().isoformat(),
            session_id,
        ),
    )


def get_by_id(conn: sqlite3.Connection, session_id: str) -> SessionRow | None:
    r = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    return _to_row(r) if r else None


def set_flush_end(
    conn: sqlite3.Connection,
    session_id: str,
    flush_end: datetime,
) -> None:
    conn.execute(
        "UPDATE sessions SET flush_end=?, updated_at=? WHERE id=?",
        (
            flush_end.isoformat(),
            datetime.now().astimezone().isoformat(),
            session_id,
        ),
    )


def set_classified_end(
    conn: sqlite3.Connection,
    session_id: str,
    classified_end: datetime,
) -> None:
    conn.execute(
        "UPDATE sessions SET classified_end=?, updated_at=? WHERE id=?",
        (
            classified_end.isoformat(),
            datetime.now().astimezone().isoformat(),
            session_id,
        ),
    )


def set_pattern_detected_end(
    conn: sqlite3.Connection,
    session_id: str,
    pattern_detected_end: datetime,
) -> None:
    conn.execute(
        "UPDATE sessions SET pattern_detected_end=?, updated_at=? WHERE id=?",
        (
            pattern_detected_end.isoformat(),
            datetime.now().astimezone().isoformat(),
            session_id,
        ),
    )


def set_delta_end(conn: sqlite3.Connection, session_id: str, delta_end: datetime) -> None:
    """Advance the successfully applied Point/Line modeling watermark."""
    conn.execute(
        "UPDATE sessions SET delta_end=?, updated_at=? WHERE id=?",
        (
            delta_end.isoformat(),
            datetime.now().astimezone().isoformat(),
            session_id,
        ),
    )


def mark_modeled(conn: sqlite3.Connection, session_id: str, modeled_at: datetime) -> None:
    """Mark terminal classifier/pattern/delta processing complete."""
    conn.execute(
        "UPDATE sessions SET modeled_at=?, updated_at=? WHERE id=?",
        (
            modeled_at.isoformat(),
            datetime.now().astimezone().isoformat(),
            session_id,
        ),
    )


def list_due_for_retry(conn: sqlite3.Connection, *, now: datetime) -> list[SessionRow]:
    rows = conn.execute(
        """
        SELECT * FROM sessions
         WHERE status='failed'
           AND (next_retry_at IS NULL OR next_retry_at <= ?)
         ORDER BY start_time ASC
        """,
        (now.isoformat(),),
    ).fetchall()
    return [_to_row(r) for r in rows]


def list_pending_modeling(conn: sqlite3.Connection) -> list[SessionRow]:
    """Reduced sessions whose terminal model stages have not completed."""
    rows = conn.execute(
        """
        SELECT * FROM sessions
         WHERE status = 'reduced'
           AND end_time IS NOT NULL
           AND modeled_at IS NULL
         ORDER BY start_time ASC
        """
    ).fetchall()
    return [_to_row(r) for r in rows]


def list_pending_reduction(conn: sqlite3.Connection) -> list[SessionRow]:
    """All non-reduced, non-active rows — the safety-net retry universe.

    Picks up ``ended`` rows whose reducer thread was killed mid-run
    (daemon shutdown) as well as ``failed`` rows regardless of
    ``next_retry_at`` (the daily cron is an unconditional catch-up
    pass, not the scheduled retry tick).
    """
    rows = conn.execute(
        """
        SELECT * FROM sessions
         WHERE status IN ('ended', 'failed')
           AND end_time IS NOT NULL
         ORDER BY start_time ASC
        """,
    ).fetchall()
    return [_to_row(r) for r in rows]


def get_system_state(conn: sqlite3.Connection, key: str, default: str = "0") -> str:
    """Return the persisted value for ``key`` (or ``default`` if absent)."""
    r = conn.execute("SELECT value FROM system_state WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_system_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Upsert a ``key=value`` pair into the persistent ``system_state`` KV."""
    conn.execute(
        """
        INSERT INTO system_state(key, value) VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (key, value),
    )


def increment_system_state(conn: sqlite3.Connection, key: str) -> None:
    """Atomically increment an integer-valued state generation."""
    conn.execute(
        """
        INSERT INTO system_state(key, value) VALUES(?, '1')
        ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER) + 1
        """,
        (key,),
    )


def compare_and_set_system_state(
    conn: sqlite3.Connection,
    key: str,
    *,
    expected: str,
    value: str,
) -> bool:
    """Update state only if no concurrent writer changed its generation."""
    cur = conn.execute(
        "UPDATE system_state SET value=? WHERE key=? AND value=?",
        (value, key, expected),
    )
    return cur.rowcount == 1


def _to_row(r: sqlite3.Row) -> SessionRow:
    def _dt(v: str | None) -> datetime | None:
        if not v:
            return None
        try:
            return datetime.fromisoformat(v)
        except (TypeError, ValueError):
            return None

    # Older rows may not have flush_end / classified_end columns; PRAGMA
    # migration adds them but existing rows default to NULL (→ None).
    flush_end: datetime | None = None
    try:
        flush_end = _dt(r["flush_end"])
    except (IndexError, KeyError):
        flush_end = None
    classified_end: datetime | None = None
    try:
        classified_end = _dt(r["classified_end"])
    except (IndexError, KeyError):
        classified_end = None
    pattern_detected_end: datetime | None = None
    try:
        pattern_detected_end = _dt(r["pattern_detected_end"])
    except (IndexError, KeyError):
        pattern_detected_end = None
    delta_end: datetime | None = None
    try:
        delta_end = _dt(r["delta_end"])
    except (IndexError, KeyError):
        delta_end = None
    modeled_at: datetime | None = None
    try:
        modeled_at = _dt(r["modeled_at"])
    except (IndexError, KeyError):
        modeled_at = None
    return SessionRow(
        id=r["id"],
        start_time=_dt(r["start_time"]) or datetime.now().astimezone(),
        end_time=_dt(r["end_time"]),
        status=r["status"] or "active",
        retry_count=r["retry_count"] or 0,
        next_retry_at=_dt(r["next_retry_at"]),
        last_error=r["last_error"] or "",
        created_at=_dt(r["created_at"]),
        updated_at=_dt(r["updated_at"]),
        flush_end=flush_end,
        classified_end=classified_end,
        pattern_detected_end=pattern_detected_end,
        delta_end=delta_end,
        modeled_at=modeled_at,
    )

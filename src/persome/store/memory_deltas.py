"""DAO for windowed structured memory deltas and their apply audit.

One LLM reading of each newly flushed window emits a structured
``memory_delta {owner_alias_candidates, entities, assertions, relations, events}``.
The post-gate
payload is persisted here before deterministic application mints Points and
Lines. ``apply_status`` makes interrupted application retryable without
spending another LLM call or reinforcing an edge twice.

Rows are append-only; active-session flushes and terminal finalization create
one row per non-overlapping window and resume application from that row. The
payload column stores
the POST-GATE delta (after the deterministic quote/roster/predicate/confidence
gates in ``writer/memory_delta.py``), plus a ``dropped`` audit count so gate
strictness stays observable.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import cast

from ..logger import get

logger = get("persome.store.memory_deltas")

STATUS_SHADOW = "shadow"

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_deltas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    created_at TEXT NOT NULL,            -- ISO8601 consolidation time
    model TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'shadow',
    payload TEXT NOT NULL DEFAULT '{}',  -- post-gate delta JSON
    dropped INTEGER NOT NULL DEFAULT 0,  -- items removed by the deterministic gates
    apply_status TEXT NOT NULL DEFAULT 'unknown',
    window_start TEXT NOT NULL DEFAULT '',
    window_end TEXT NOT NULL DEFAULT '',
    is_final INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_memory_deltas_session ON memory_deltas(session_id);
CREATE INDEX IF NOT EXISTS idx_memory_deltas_created ON memory_deltas(created_at DESC);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(memory_deltas)")}
    if "apply_status" not in columns:
        # Old rows may or may not have been applied. Treat them as processed;
        # only rows written by the new code carry a retryable failed state.
        conn.execute(
            "ALTER TABLE memory_deltas ADD COLUMN apply_status TEXT NOT NULL DEFAULT 'unknown'"
        )
    if "window_start" not in columns:
        conn.execute("ALTER TABLE memory_deltas ADD COLUMN window_start TEXT NOT NULL DEFAULT ''")
    if "window_end" not in columns:
        conn.execute("ALTER TABLE memory_deltas ADD COLUMN window_end TEXT NOT NULL DEFAULT ''")
    if "is_final" not in columns:
        conn.execute("ALTER TABLE memory_deltas ADD COLUMN is_final INTEGER NOT NULL DEFAULT 1")


def insert(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    payload: dict,
    model: str = "",
    dropped: int = 0,
    status: str = STATUS_SHADOW,
    apply_status: str = "not_requested",
    created_at: datetime | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    is_final: bool = True,
) -> int:
    ensure_schema(conn)
    ts = (created_at or datetime.now().astimezone()).isoformat()
    cur = conn.execute(
        "INSERT INTO memory_deltas"
        " (session_id, created_at, model, status, payload, dropped, apply_status,"
        " window_start, window_end, is_final)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            ts,
            model,
            status,
            json.dumps(payload, ensure_ascii=False),
            dropped,
            apply_status,
            window_start.isoformat() if window_start else "",
            window_end.isoformat() if window_end else "",
            1 if is_final else 0,
        ),
    )
    return int(cur.lastrowid or 0)


def recent(conn: sqlite3.Connection, *, limit: int = 20) -> list[sqlite3.Row]:
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    return list(
        conn.execute(
            "SELECT * FROM memory_deltas ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        )
    )


def latest_for_session(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM memory_deltas WHERE session_id = ?"
        " ORDER BY created_at DESC, id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return cast(sqlite3.Row | None, row)


def latest_for_window(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    window_start: datetime,
    window_end: datetime,
) -> sqlite3.Row | None:
    """Return the newest attempt for one exact incremental modeling window."""
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM memory_deltas WHERE session_id=? AND window_start=? AND window_end=?"
        " ORDER BY id DESC LIMIT 1",
        (session_id, window_start.isoformat(), window_end.isoformat()),
    ).fetchone()
    return cast(sqlite3.Row | None, row)


def next_for_session_start(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    window_start: datetime,
    through: datetime,
) -> sqlite3.Row | None:
    """Return the earliest persisted window beginning at one watermark."""
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM memory_deltas m WHERE session_id=? AND window_start=?"
        " AND window_end>window_start AND window_end<=?"
        " AND id=(SELECT MAX(id) FROM memory_deltas WHERE session_id=m.session_id"
        " AND window_start=m.window_start AND window_end=m.window_end)"
        " ORDER BY window_end ASC LIMIT 1",
        (session_id, window_start.isoformat(), through.isoformat()),
    ).fetchone()
    return cast(sqlite3.Row | None, row)


def set_apply_status(conn: sqlite3.Connection, delta_id: int, status: str) -> None:
    ensure_schema(conn)
    conn.execute("UPDATE memory_deltas SET apply_status=? WHERE id=?", (status, delta_id))


def stats(conn: sqlite3.Connection) -> dict:
    """Aggregate the latest attempt for every distinct session window."""
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT session_id, payload, dropped FROM memory_deltas m"
        " WHERE id = (SELECT MAX(id) FROM memory_deltas"
        " WHERE session_id=m.session_id AND window_start=m.window_start"
        " AND window_end=m.window_end)"
    ).fetchall()
    heads = {
        "owner_alias_candidates": 0,
        "entities": 0,
        "assertions": 0,
        "relations": 0,
        "events": 0,
    }
    dropped = 0
    for _sid, payload, drop in rows:
        dropped += int(drop or 0)
        try:
            delta = json.loads(payload)
        except (TypeError, ValueError):
            continue
        for head in heads:
            items = delta.get(head)
            if isinstance(items, list):
                heads[head] += len(items)
    total = conn.execute("SELECT COUNT(*) FROM memory_deltas").fetchone()[0]
    return {
        "rows": int(total),
        "sessions": len({row[0] for row in rows}),
        "heads": heads,
        "dropped_by_gates": dropped,
    }

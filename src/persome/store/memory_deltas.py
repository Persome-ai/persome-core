"""DAO for ``memory_deltas`` — the session-end consolidator's SHADOW output.

One LLM reading of a just-ended session emits a single
structured ``memory_delta {entities, assertions, relations, events}``. Phase 0
persists that delta here VERBATIM with ``status='shadow'`` — nothing downstream
consumes it except the parity report (``persome delta-report``) and the Phase-1
dual-run eval. Only after dual-run parity do the four scattered extractors
(person name-source / relation LLM pass / case extraction / classifier
attribution) retire and the delta becomes the write path's real input.

One row per consolidator run; append-only (a re-run for the same session adds a
new row — the report reads the latest per session). The payload column stores
the POST-GATE delta (after the deterministic quote/roster/predicate/confidence
gates in ``writer/memory_delta.py``), plus a ``dropped`` audit count so gate
strictness stays observable.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

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
    dropped INTEGER NOT NULL DEFAULT 0   -- items removed by the deterministic gates
);
CREATE INDEX IF NOT EXISTS idx_memory_deltas_session ON memory_deltas(session_id);
CREATE INDEX IF NOT EXISTS idx_memory_deltas_created ON memory_deltas(created_at DESC);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def insert(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    payload: dict,
    model: str = "",
    dropped: int = 0,
    status: str = STATUS_SHADOW,
    created_at: datetime | None = None,
) -> int:
    ensure_schema(conn)
    ts = (created_at or datetime.now().astimezone()).isoformat()
    cur = conn.execute(
        "INSERT INTO memory_deltas (session_id, created_at, model, status, payload, dropped)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, ts, model, status, json.dumps(payload, ensure_ascii=False), dropped),
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
    return conn.execute(
        "SELECT * FROM memory_deltas WHERE session_id = ?"
        " ORDER BY created_at DESC, id DESC LIMIT 1",
        (session_id,),
    ).fetchone()


def stats(conn: sqlite3.Connection) -> dict:
    """Aggregate shape for ``persome delta-report``: rows, sessions covered, per-head
    item counts across the latest delta of each session, total gate drops."""
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT session_id, payload, dropped FROM memory_deltas m"
        " WHERE id = (SELECT MAX(id) FROM memory_deltas WHERE session_id = m.session_id)"
    ).fetchall()
    heads = {"entities": 0, "assertions": 0, "relations": 0, "events": 0}
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
        "sessions": len(rows),
        "heads": heads,
        "dropped_by_gates": dropped,
    }

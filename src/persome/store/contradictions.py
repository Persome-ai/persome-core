"Persistence for human-adjudicated memory contradictions."

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_contradictions (
    pair_key    TEXT PRIMARY KEY,   -- sorted "a_id|b_id" — unordered-pair identity
    a_id        TEXT NOT NULL,
    b_id        TEXT NOT NULL,
    path        TEXT NOT NULL,      -- the memory file both facts live in
    a_body      TEXT NOT NULL,
    b_body      TEXT NOT NULL,
    reason      TEXT NOT NULL,      -- the judge's one-line why
    status      TEXT NOT NULL,      -- open | resolved | dismissed
    keep_id     TEXT,               -- resolve verdict: the entry the human kept
    created_at  TEXT NOT NULL,
    resolved_at TEXT
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def pair_key(a_id: str, b_id: str) -> str:
    lo, hi = sorted((a_id, b_id))
    return f"{lo}|{hi}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def seen_pairs(conn: sqlite3.Connection) -> set[str]:
    """Every pair ever judged — any status. The nightly check must never
    re-spend an LLM call (or re-nag a human) on an adjudicated pair."""
    ensure_schema(conn)
    return {r[0] for r in conn.execute("SELECT pair_key FROM memory_contradictions")}


def record(
    conn: sqlite3.Connection,
    *,
    a_id: str,
    b_id: str,
    path: str,
    a_body: str,
    b_body: str,
    reason: str,
) -> str:
    """Insert one flagged pair (idempotent: an existing row wins). Returns the
    pair_key."""
    ensure_schema(conn)
    key = pair_key(a_id, b_id)
    conn.execute(
        "INSERT OR IGNORE INTO memory_contradictions"
        " (pair_key, a_id, b_id, path, a_body, b_body, reason, status, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)",
        (key, a_id, b_id, path, a_body, b_body, reason, _now()),
    )
    return key


def list_rows(conn: sqlite3.Connection, *, status: str | None = "open") -> list[sqlite3.Row]:
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    if status is None:
        return list(conn.execute("SELECT * FROM memory_contradictions ORDER BY created_at DESC"))
    return list(
        conn.execute(
            "SELECT * FROM memory_contradictions WHERE status = ? ORDER BY created_at DESC",
            (status,),
        )
    )


def close(
    conn: sqlite3.Connection,
    key: str,
    *,
    status: str,
    keep_id: str | None = None,
) -> sqlite3.Row | None:
    """Record the human verdict (``resolved`` with the kept entry, or
    ``dismissed`` = not actually a contradiction). Returns the closed row, or
    None when the pair_key is unknown. The caller clears the two entries'
    ``conflicted`` metadata — this DAO only owns the ledger row."""
    assert status in ("resolved", "dismissed")
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM memory_contradictions WHERE pair_key = ?", (key,)).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE memory_contradictions SET status = ?, keep_id = ?, resolved_at = ?"
        " WHERE pair_key = ?",
        (status, keep_id, _now(), key),
    )
    return row

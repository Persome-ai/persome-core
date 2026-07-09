"""DAO for the ``highlights`` table — Book Phase 2.1 manual pull-quotes.

A highlight is a quote the user hand-picks (划词存) from a Book page or a chat
session. It is durable, user-authored, and append-only with explicit delete —
so SQLite is the DB-of-record (markdown isn't, unlike compressed memory).

Schema is intentionally narrow: a quote, the source it came from (a page id or
chat session id, stored verbatim as a string), and a creation timestamp. The
``time_label`` shown in the UI (``MON D · HH:MM``) is derived from
``created_at`` at read time, not stored.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from ..logger import get

logger = get("persome.store.highlights")

SCHEMA = """
CREATE TABLE IF NOT EXISTS highlights (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    quote       TEXT NOT NULL,
    source_ref  TEXT NOT NULL DEFAULT '',  -- source page id or chat session id
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_highlights_created
    ON highlights(created_at DESC);
"""

_MONTHS = [
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
]  # fmt: skip


@dataclass
class Highlight:
    id: int
    quote: str
    source_ref: str
    created_at: datetime

    def time_label(self) -> str:
        """``MON D · HH:MM`` derived from :attr:`created_at` (e.g. ``JUL 8 · 19:02``)."""
        dt = self.created_at
        return f"{_MONTHS[dt.month - 1]} {dt.day} · {dt.hour:02d}:{dt.minute:02d}"


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def insert(conn: sqlite3.Connection, *, quote: str, source_ref: str = "") -> Highlight:
    """Insert a new highlight and return the persisted row (newest values)."""
    created_at = _now_iso()
    cur = conn.execute(
        """
        INSERT INTO highlights (quote, source_ref, created_at)
        VALUES (?, ?, ?)
        """,
        (quote, source_ref, created_at),
    )
    conn.commit()
    new_id = int(cur.lastrowid)  # type: ignore[arg-type]
    logger.info("highlights: inserted highlight %s (source_ref=%s)", new_id, source_ref or "-")
    return Highlight(
        id=new_id,
        quote=quote,
        source_ref=source_ref,
        created_at=datetime.fromisoformat(created_at),
    )


def list_recent(conn: sqlite3.Connection, *, limit: int = 20) -> list[Highlight]:
    """Return highlights newest-first, capped at *limit*."""
    rows = conn.execute(
        """
        SELECT * FROM highlights
         ORDER BY created_at DESC, id DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [_row_to_highlight(r) for r in rows]


def delete(conn: sqlite3.Connection, highlight_id: int) -> bool:
    """Delete a highlight by id. Returns ``True`` if a row was removed."""
    deleted = conn.execute(
        "DELETE FROM highlights WHERE id = ?",
        (highlight_id,),
    ).rowcount
    conn.commit()
    if deleted:
        logger.info("highlights: deleted highlight %s", highlight_id)
    return bool(deleted)


def _row_to_highlight(r: sqlite3.Row) -> Highlight:
    return Highlight(
        id=r["id"],
        quote=r["quote"] or "",
        source_ref=r["source_ref"] or "",
        created_at=datetime.fromisoformat(r["created_at"]),
    )

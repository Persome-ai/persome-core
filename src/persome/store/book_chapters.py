"""DAO for the ``book_chapters`` table — Book Phase 2.2 chapter titles.

A *chapter* is a theme-clustered group of chat sessions distilled into a
literary headline. The Dream sub-step (:mod:`persome.writer.book_chapters`)
reads the recent non-archived chat sessions, clusters them into 0–N themes, and
writes one row per theme: a title, a short subtitle, and the backing
``session_ids`` so the Book → Sessions reader can pull each chapter's messages.

SQLite is the DB-of-record (markdown isn't — these are derived, regenerable
groupings, not user-authored memory). Schema is narrow:

    id          autoincrement, the stable PATCH-addressing id
    title       the literary headline (also the front-end selection key)
    subtitle    short caption (e.g. ``YOU + MENS``), may be empty
    session_ids JSON array of backing chat session ids
    edited      1 once the user renamed it — regeneration must not clobber it
    created_at  ISO8601 creation time

**Regeneration contract** (:func:`replace_generated`): the daily Dream run
re-clusters from scratch, so it wipes the previous *generated* rows and inserts
the fresh set — but rows the user has renamed (``edited=1``) are preserved
verbatim. "Guess wrong, costs nothing": a bad title is one tap to fix, and the
fix sticks across every future regeneration.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from ..logger import get

logger = get("persome.store.book_chapters")

SCHEMA = """
CREATE TABLE IF NOT EXISTS book_chapters (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    subtitle    TEXT NOT NULL DEFAULT '',
    session_ids TEXT NOT NULL DEFAULT '[]',  -- JSON array of chat session ids
    edited      INTEGER NOT NULL DEFAULT 0,  -- 1 once the user renamed it
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_book_chapters_created
    ON book_chapters(created_at DESC);
"""


@dataclass
class Chapter:
    title: str
    subtitle: str = ""
    session_ids: list[str] = field(default_factory=list)
    edited: bool = False
    id: int = 0
    created_at: str = ""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _decode_ids(raw: str | None) -> list[str]:
    """Parse the stored ``session_ids`` JSON; ``[]`` on any malformation."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(x) for x in parsed]


def _row_to_chapter(r: sqlite3.Row) -> Chapter:
    return Chapter(
        id=int(r["id"]),
        title=r["title"] or "",
        subtitle=r["subtitle"] or "",
        session_ids=_decode_ids(r["session_ids"]),
        edited=bool(r["edited"]),
        created_at=r["created_at"] or "",
    )


def list_chapters(conn: sqlite3.Connection) -> list[Chapter]:
    """Return all chapters, newest-created first (ties broken by id desc)."""
    rows = conn.execute(
        """
        SELECT * FROM book_chapters
         ORDER BY created_at DESC, id DESC
        """
    ).fetchall()
    return [_row_to_chapter(r) for r in rows]


def replace_generated(
    conn: sqlite3.Connection,
    chapters: list[Chapter] | list[dict[str, object]],
    *,
    now: str | None = None,
) -> int:
    """Replace all *generated* chapters with ``chapters``; keep edited ones.

    Deletes every row with ``edited=0`` (the previous generation's output) and
    inserts the freshly-clustered ``chapters`` as new generated rows. Rows the
    user has renamed (``edited=1``) are never touched — they survive every
    regeneration. Returns the number of rows inserted.

    Accepts either :class:`Chapter` instances or plain dicts with ``title`` /
    ``subtitle`` / ``session_ids`` keys (the writer's convenience shape).
    """
    created_at = now or _now_iso()
    conn.execute("DELETE FROM book_chapters WHERE edited = 0")

    inserted = 0
    for ch in chapters:
        if isinstance(ch, Chapter):
            title = ch.title
            subtitle = ch.subtitle
            session_ids = ch.session_ids
        else:
            title = str(ch.get("title") or "")
            subtitle = str(ch.get("subtitle") or "")
            raw_ids = ch.get("session_ids")
            session_ids = [str(x) for x in raw_ids] if isinstance(raw_ids, list) else []
        title = title.strip()
        if not title:
            continue  # never store a titleless chapter
        conn.execute(
            """
            INSERT INTO book_chapters (title, subtitle, session_ids, edited, created_at)
            VALUES (?, ?, ?, 0, ?)
            """,
            (title, subtitle.strip(), json.dumps(session_ids), created_at),
        )
        inserted += 1

    conn.commit()
    logger.info("book_chapters: replaced generated rows, inserted %d", inserted)
    return inserted


def mark_edited(conn: sqlite3.Connection, chapter_id: int, title: str) -> bool:
    """Rename a chapter and flag it ``edited`` so regeneration won't clobber it.

    Returns ``False`` if no chapter with ``chapter_id`` exists.
    """
    cleaned = title.strip()
    if not cleaned:
        return False
    updated = conn.execute(
        "UPDATE book_chapters SET title = ?, edited = 1 WHERE id = ?",
        (cleaned, chapter_id),
    ).rowcount
    conn.commit()
    if updated:
        logger.info("book_chapters: renamed chapter %s -> %r (edited)", chapter_id, cleaned)
    return bool(updated)

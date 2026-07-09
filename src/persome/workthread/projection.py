"""Markdown projection ``thread-*.md`` (spec §五).

Rides the existing markdown-SSOT write path (``entries.create_file`` /
``append_entry`` → ``validate_prefix`` → FTS) so threads are immediately
keyword-searchable by chat / search_memory, exactly like the intent stream's
``intent-*.md`` projection. Best-effort: a projection failure never breaks the
executor (the ``work_threads`` table is the record of truth).

evomem 归宿：projection entries are appended with the ``working_state`` layer
tag, which :meth:`evomem.models.MemoryLayer.from_string` aliases onto
``L3_SUMMARY``（不动七层枚举——spec §五）。
"""

from __future__ import annotations

import sqlite3

from ..logger import get
from ..store import entries as entries_mod
from .model import WorkThread

logger = get("persome.workthread.projection")


def _file_name(thread: WorkThread) -> str:
    return f"thread-{thread.id}.md"


def project_event(conn: sqlite3.Connection, thread: WorkThread, event: str) -> None:
    """Append one lifecycle event entry to the thread's projection file.

    ``event`` is a short human line ("opened", "progress: …", "completed", …).
    """
    name = _file_name(thread)
    content = (
        f"**{thread.title}** [{thread.status}] {event}\n"
        f"- origin: {thread.origin_type}"
        + (f" by {thread.origin_actor}" if thread.origin_actor else "")
        + f"\n- total_active_minutes: {thread.total_active_minutes}"
        + (" (approximate)" if thread.approximate else "")
    )
    tags = ["#workthread", f"#status:{thread.status}", "#layer:working_state"]
    try:
        try:
            entries_mod.append_entry(conn, name=name, content=content, tags=tags)
        except FileNotFoundError:
            entries_mod.create_file(
                conn,
                name=name,
                description=f"Work thread: {thread.title} — 进行中工作线的生命周期投影。",
                tags=["workthread"],
            )
            entries_mod.append_entry(conn, name=name, content=content, tags=tags)
    except Exception as exc:  # noqa: BLE001 — projection is best-effort
        logger.warning("thread projection failed for %s (state kept): %s", thread.id, exc)

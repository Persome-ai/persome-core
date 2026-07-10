"Tests for markdown landing."

from __future__ import annotations

import sqlite3

from persome.evomem.models import ReconcileOp
from persome.store import entries as entries_mod


def apply_update(
    conn: sqlite3.Connection, op: ReconcileOp, *, file_name: str, tags: list[str] | None = None
) -> str:
    assert op.target_id is not None
    return entries_mod.supersede_entry(
        conn,
        name=file_name,
        old_entry_id=op.target_id,
        new_content=op.content,
        reason=op.reason,
        tags=tags or None,
        refined_from=op.target_id,
    )


def apply_abstract(
    conn: sqlite3.Connection, op: ReconcileOp, *, file_name: str, tags: list[str] | None = None
) -> str:
    assert len(op.source_ids) >= 2
    entry_tags = list(tags or []) + ["abstracted-from:" + ",".join(op.source_ids)]
    new_id = entries_mod.append_entry(conn, name=file_name, content=op.content, tags=entry_tags)
    for source_id in op.source_ids:
        entries_mod.mark_entry_deleted(conn, name=file_name, entry_id=source_id)
    return new_id

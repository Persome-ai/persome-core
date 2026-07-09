"""bug_009 + merged_bug_002 — body strike must be anchored to the entry id.

``supersede_entry`` / ``mark_entry_deleted`` retire an entry by wrapping its body
in ``~~...~~`` in the markdown. The old implementation did a **content-blind**
``text.replace(target.body, striked, 1)`` over the WHOLE file, which strikes the
*first* byte-identical match — so retiring a later entry whose body equals an
earlier one's struck the EARLIER (still-live) entry instead. The next
``rebuild_index`` then re-derives that wrong entry as superseded, breaking the
``增量判定 ≡ rebuild`` invariant.

The fix anchors the strike to the unique ``{id: <entry_id>}`` heading marker and
only replaces inside that entry's own body region.

merged_bug_002 hardens ``mark_entry_deleted`` further:
  (a) an empty body still leaves a durable strike sentinel (``~~~~``) so a rebuild
      keeps it superseded (no #superseded-by successor tag exists to fall back on);
  (b) the frontmatter ``updated`` is bumped and the files-table row refreshed, so
      ``list_files`` ordering and the FileRow don't drift.
"""

from __future__ import annotations

import sqlite3

from persome.store import entries as entries_mod
from persome.store import files as files_mod
from persome.store import fts


def _superseded(conn: sqlite3.Connection, entry_id: str) -> int:
    row = conn.execute("SELECT superseded FROM entries WHERE id=?", (entry_id,)).fetchone()
    return int(row["superseded"]) if row else -1


def _body_of(name: str, entry_id: str) -> str:
    parsed = files_mod.read_file(files_mod.memory_path(name))
    target = next(e for e in parsed.entries if e.id == entry_id)
    return target.body


def _is_struck(body: str) -> bool:
    s = body.strip()
    return s.startswith("~~") and s.endswith("~~")


# ── bug_009: duplicate-body entries, strike the RIGHT one ────────────────────

_DUP = "the user prefers uv over pip"


def test_supersede_duplicate_body_strikes_only_target(ac_root):
    """A/B/C where A.body == C.body; superseding C must strike ONLY C, leave A live."""
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-dup.md", description="d", tags=["t"])
        a = entries_mod.append_entry(conn, name="project-dup.md", content=_DUP, tags=["t"])
        entries_mod.append_entry(conn, name="project-dup.md", content="middle fact", tags=["t"])
        c = entries_mod.append_entry(conn, name="project-dup.md", content=_DUP, tags=["t"])

        entries_mod.supersede_entry(
            conn, name="project-dup.md", old_entry_id=c, new_content="v2", reason="r", tags=["t"]
        )

        # Only C's body is struck; A's body is untouched.
        assert _is_struck(_body_of("project-dup.md", c))
        assert not _is_struck(_body_of("project-dup.md", a))
        assert _body_of("project-dup.md", a) == _DUP

        # The live index agrees: A live, C superseded.
        assert _superseded(conn, a) == 0
        assert _superseded(conn, c) == 1

        # Invariant survives a rebuild round-trip: A stays live, C stays retired.
        entries_mod.rebuild_index(conn)
        assert _superseded(conn, a) == 0
        assert _superseded(conn, c) == 1


def test_delete_duplicate_body_strikes_only_target(ac_root):
    """Same duplicate-body hazard via mark_entry_deleted (no successor)."""
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-dup2.md", description="d", tags=["t"])
        a = entries_mod.append_entry(conn, name="project-dup2.md", content=_DUP, tags=["t"])
        entries_mod.append_entry(conn, name="project-dup2.md", content="middle fact", tags=["t"])
        c = entries_mod.append_entry(conn, name="project-dup2.md", content=_DUP, tags=["t"])

        entries_mod.mark_entry_deleted(conn, name="project-dup2.md", entry_id=c)

        assert _is_struck(_body_of("project-dup2.md", c))
        assert not _is_struck(_body_of("project-dup2.md", a))
        assert _body_of("project-dup2.md", a) == _DUP
        assert _superseded(conn, a) == 0
        assert _superseded(conn, c) == 1

        # Rebuild round-trip: A still superseded=0/is_latest=1, C still retired.
        entries_mod.rebuild_index(conn)
        assert _superseded(conn, a) == 0
        assert _superseded(conn, c) == 1


def test_single_entry_supersede_unchanged(ac_root):
    """bug_009 helper is equivalent to the old path for the common single-entry case."""
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-single.md", description="s", tags=["t"])
        old = entries_mod.append_entry(
            conn, name="project-single.md", content="only fact", tags=["t"]
        )
        new = entries_mod.supersede_entry(
            conn,
            name="project-single.md",
            old_entry_id=old,
            new_content="v2",
            reason="r",
            tags=["t"],
        )
        assert _is_struck(_body_of("project-single.md", old))
        assert _superseded(conn, old) == 1
        assert _superseded(conn, new) == 0
        entries_mod.rebuild_index(conn)
        assert _superseded(conn, old) == 1
        assert _superseded(conn, new) == 0


# ── merged_bug_002: mark_entry_deleted durability ────────────────────────────


def test_delete_empty_body_keeps_superseded_after_rebuild(ac_root):
    """(a) An entry with an empty body, when deleted, must stay superseded across a
    rebuild. With no #superseded-by successor and an empty (un-strikable) body, the
    only durable markdown signal is a sentinel strike (``~~~~``)."""
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-empty.md", description="e", tags=["t"])
        # An empty-body entry: append strips to "" .
        empty = entries_mod.append_entry(conn, name="project-empty.md", content="", tags=["t"])
        entries_mod.append_entry(conn, name="project-empty.md", content="real fact", tags=["t"])

        entries_mod.mark_entry_deleted(conn, name="project-empty.md", entry_id=empty)
        assert _superseded(conn, empty) == 1

        # The killer: a rebuild must NOT revive the empty-body entry.
        entries_mod.rebuild_index(conn)
        assert _superseded(conn, empty) == 1


def test_delete_bumps_updated_and_refreshes_files_row(ac_root):
    """(b) mark_entry_deleted must bump frontmatter ``updated`` and refresh the
    files-table FileRow (mirroring supersede_entry), so list ordering / FileRow
    don't go stale."""
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-stale.md", description="s", tags=["t"])
        eid = entries_mod.append_entry(
            conn, name="project-stale.md", content="fact one", tags=["t"]
        )
        entries_mod.append_entry(conn, name="project-stale.md", content="fact two", tags=["t"])

        # Force the on-disk ``updated`` to a clearly-stale value so the bump is observable.
        files_mod.update_frontmatter(
            files_mod.memory_path("project-stale.md"), {"updated": "2000-01-01"}
        )
        before = fts.get_file(conn, "project-stale.md")
        assert before is not None

        entries_mod.mark_entry_deleted(conn, name="project-stale.md", entry_id=eid)

        # Frontmatter ``updated`` was bumped to today on disk.
        parsed = files_mod.read_file(files_mod.memory_path("project-stale.md"))
        assert parsed.updated == files_mod.today()

        # The files-table FileRow was refreshed to match (no drift vs a rebuild).
        after = fts.get_file(conn, "project-stale.md")
        assert after is not None
        assert after.updated == files_mod.today()
        entries_mod.rebuild_index(conn)
        rebuilt = fts.get_file(conn, "project-stale.md")
        assert rebuilt is not None
        assert after.entry_count == rebuilt.entry_count
        assert after.updated == rebuilt.updated

"""Retrieval count tracking for load-bearing entry detection."""

from __future__ import annotations

from pathlib import Path

from persome.store import entries as entries_mod
from persome.store import fts


def _new_count(conn, entry_id: str) -> int:
    return fts.get_retrieval_count(conn, entry_id)


def test_untracked_entries_report_zero(ac_root: Path) -> None:
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn, name="user-profile.md", description="identity", tags=["identity"]
        )
        eid = entries_mod.append_entry(
            conn,
            name="user-profile.md",
            content="User is a data scientist.",
            tags=["identity"],
        )
        assert _new_count(conn, eid) == 0


def test_search_increments_retrieval_count(ac_root: Path) -> None:
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn,
            name="project-persome.md",
            description="Persome OSS project",
            tags=["project"],
        )
        eid = entries_mod.append_entry(
            conn,
            name="project-persome.md",
            content="User chose Python CLI + daemon form factor.",
            tags=["project"],
        )

        hits = fts.search(conn, query="daemon", top_k=5)
        assert any(h.id == eid for h in hits)
        assert _new_count(conn, eid) == 1

        fts.search(conn, query="daemon", top_k=5)
        assert _new_count(conn, eid) == 2


def test_search_records_last_retrieved_at(ac_root: Path) -> None:
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn, name="topic-rust.md", description="Rust notes", tags=["topic"]
        )
        eid = entries_mod.append_entry(
            conn,
            name="topic-rust.md",
            content="Tokio select polls all branches.",
            tags=["topic"],
        )
        fts.search(conn, query="Tokio", top_k=5)
        row = conn.execute(
            "SELECT last_retrieved_at FROM entry_retrieval_stats WHERE entry_id=?",
            (eid,),
        ).fetchone()
        assert row is not None
        assert row["last_retrieved_at"]  # non-empty ISO string


def test_supersede_copies_retrieval_count(ac_root: Path) -> None:
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn, name="tool-cursor.md", description="Cursor editor", tags=["tool"]
        )
        old = entries_mod.append_entry(
            conn,
            name="tool-cursor.md",
            content="User prefers VSCode as primary editor.",
            tags=["editor"],
        )
        # Drive the count up via two retrievals
        fts.search(conn, query="VSCode", top_k=5)
        fts.search(conn, query="VSCode", top_k=5)
        fts.search(conn, query="VSCode", top_k=5)
        assert _new_count(conn, old) == 3

        new_id = entries_mod.supersede_entry(
            conn,
            name="tool-cursor.md",
            old_entry_id=old,
            new_content="User switched from VSCode to Cursor for AI integration.",
            reason="editor switch",
            tags=["editor"],
        )
        assert _new_count(conn, new_id) == 3
        # Old row still exists with its history (audit trail)
        assert _new_count(conn, old) == 3


def test_supersede_with_no_prior_retrievals_does_not_create_row(ac_root: Path) -> None:
    """Carry-over copies the predecessor's stats verbatim — including 'no row'."""
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="person-bob.md", description="Bob", tags=["person"])
        old = entries_mod.append_entry(
            conn,
            name="person-bob.md",
            content="Bob at OpenAI.",
            tags=["person"],
        )
        new_id = entries_mod.supersede_entry(
            conn,
            name="person-bob.md",
            old_entry_id=old,
            new_content="Bob moved to Anthropic.",
            reason="role change",
            tags=["person"],
        )
        # Both report 0; carry-over of an absent row leaves the new entry
        # also without a row.
        assert _new_count(conn, old) == 0
        assert _new_count(conn, new_id) == 0


def test_increment_retrieval_counts_noop_on_empty(ac_root: Path) -> None:
    with fts.cursor() as conn:
        # Should not raise, should not create rows
        fts.increment_retrieval_counts(conn, [])
        fts.increment_retrieval_counts(conn, ["", None, ""])  # type: ignore[list-item]
        n = conn.execute("SELECT COUNT(*) AS c FROM entry_retrieval_stats").fetchone()
        assert n["c"] == 0

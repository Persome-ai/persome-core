"Tests for test compact chain p3."

from __future__ import annotations

import sqlite3

from persome import config as config_mod
from persome.store import entries as entries_mod
from persome.store import files as files_mod
from persome.store import fts
from persome.writer import compact as compact_mod
from persome.writer import llm as llm_mod


def _superseded(conn: sqlite3.Connection, entry_id: str) -> int:
    row = conn.execute("SELECT superseded FROM entries WHERE id=?", (entry_id,)).fetchone()
    return int(row["superseded"]) if row else -1


def _read_raw(name: str) -> str:
    return files_mod.memory_path(name).read_text()


def _mock_compact_returns(monkeypatch, new_markdown: str) -> None:
    """Stub the compact LLM call to return ``new_markdown`` verbatim."""
    monkeypatch.setattr(llm_mod, "call_llm", lambda *a, **k: object())
    monkeypatch.setattr(llm_mod, "extract_text", lambda resp: new_markdown)


def _compact_via_run_pending(cfg, conn, name: str):
    """Drive compaction through the production caller (bug_012): the single
    ``rebuild_index`` now lives in ``run_pending``, not ``compact_file``, so the
    A1 post-rebuild assertions below must go through it. Flag the file, then run
    the pending sweep; return its single CompactResult."""
    fts.set_needs_compact(conn, name, True)
    results = compact_mod.run_pending(cfg, conn)
    return next(r for r in results if r.path == name)


def test_compact_fixes_struck_orphan_superseded_bug(ac_root, monkeypatch):
    """Regression: a whole-body-struck orphan (no #superseded-by) must read
    superseded=1 after compact. Pre-A1 compact judged it 0 (the latent bug)."""
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-x.md", description="x", tags=["t"])
        live = entries_mod.append_entry(conn, name="project-x.md", content="live fact", tags=["t"])
        orphan = entries_mod.append_entry(
            conn, name="project-x.md", content="dead fact", tags=["t"]
        )
        # Strike the orphan's body in the markdown WITHOUT a #superseded-by tag.
        entries_mod.mark_entry_deleted(conn, name="project-x.md", entry_id=orphan)

    # The "compacted" markdown the LLM returns = the current file verbatim (so the
    # struck orphan survives the compaction, letting us observe the judgment).
    compacted = _read_raw("project-x.md")
    _mock_compact_returns(monkeypatch, compacted)

    cfg = config_mod.Config()
    with fts.cursor() as conn:
        res = _compact_via_run_pending(cfg, conn, "project-x.md")
        assert res.accepted
        # A1: the struck orphan is now correctly superseded (three-way judgment).
        assert _superseded(conn, orphan) == 1
        assert _superseded(conn, live) == 0


def test_compact_keeps_supersede_judgment_fresh(ac_root, monkeypatch):
    """A supersede chain's fold judgment stays consistent across a compact (the
    rebuild re-ingests the final on-disk state instead of leaving FTS stale)."""
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-y.md", description="y", tags=["t"])
        old = entries_mod.append_entry(conn, name="project-y.md", content="v1", tags=["t"])
        new = entries_mod.supersede_entry(
            conn, name="project-y.md", old_entry_id=old, new_content="v2", reason="r", tags=["t"]
        )

    compacted = _read_raw("project-y.md")
    _mock_compact_returns(monkeypatch, compacted)

    cfg = config_mod.Config()
    with fts.cursor() as conn:
        res = _compact_via_run_pending(cfg, conn, "project-y.md")
        assert res.accepted
        assert _superseded(conn, old) == 1
        assert _superseded(conn, new) == 0


def test_compact_preserves_reconciliation_invariant(ac_root, monkeypatch):
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-z.md", description="z", tags=["t"])
        old = entries_mod.append_entry(conn, name="project-z.md", content="v1", tags=["t"])
        entries_mod.supersede_entry(
            conn, name="project-z.md", old_entry_id=old, new_content="v2", reason="r", tags=["t"]
        )
        entries_mod.append_entry(conn, name="project-z.md", content="standalone", tags=["t"])

    compacted = _read_raw("project-z.md")
    _mock_compact_returns(monkeypatch, compacted)

    cfg = config_mod.Config()
    with fts.cursor() as conn:
        _compact_via_run_pending(cfg, conn, "project-z.md")
        live = {
            r["id"] for r in conn.execute("SELECT id FROM entries WHERE superseded=0").fetchall()
        }
    assert old not in live  # the superseded old entry stays excluded post-compact


def test_compact_rejected_does_not_rebuild(ac_root, monkeypatch):
    """A rejected compact (low preservation) must NOT alter the index — the file is
    untouched, so no rebuild side effects."""
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-r.md", description="r", tags=["t"])
        entries_mod.append_entry(
            conn, name="project-r.md", content="alpha beta gamma delta", tags=["t"]
        )
    # The LLM "compaction" drops almost all tokens → preservation below threshold.
    _mock_compact_returns(monkeypatch, "---\ndescription: r\n---\n\n## [t] {id: x}\nzzz\n")

    cfg = config_mod.Config()
    with fts.cursor() as conn:
        res = compact_mod.compact_file(cfg, conn, name="project-r.md")
    assert not res.accepted

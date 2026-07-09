"""bug_012 — compact ``run_pending`` rebuilds the index ONCE, not per file.

``compact_file`` used to call the full ``rebuild_index`` on every accept; with K
flagged files, ``run_pending`` triggered K full rebuilds per tick (O(K·N)). The
fix moves the rebuild out of ``compact_file`` (which now only does the markdown
write + clears the needs_compact flag) into ``run_pending``, which rebuilds once
after the loop iff any file was accepted. A1 correctness is preserved — the
three-way superseded judgment still runs, just batched.
"""

from __future__ import annotations

import sqlite3

from persome import config as config_mod
from persome.store import entries as entries_mod
from persome.store import fts
from persome.writer import compact as compact_mod
from persome.writer import llm as llm_mod


def _seed_flagged(conn: sqlite3.Connection, name: str) -> None:
    """Create a file, seed an entry, and flag it needs_compact."""
    entries_mod.create_file(conn, name=name, description=name, tags=["t"])
    entries_mod.append_entry(conn, name=name, content="alpha beta gamma delta", tags=["t"])
    # Flag the file for compaction (what append_entry's soft-limit would do).
    fts.set_needs_compact(conn, name, True)


def _mock_identity_compact(monkeypatch) -> None:
    """The LLM 'compaction' returns each file verbatim → always accepted (full
    token preservation), so every flagged file is an accept."""

    def _extract(_resp):
        return _extract.current  # set per-file below

    # call_llm returns a sentinel; extract_text returns the current file verbatim.
    def _call(_cfg, _stage, *, messages, **_kw):
        # The user message embeds the original file inside a ```markdown fence.
        user = messages[-1]["content"]
        start = user.index("```markdown\n") + len("```markdown\n")
        end = user.rindex("\n```")
        _extract.current = user[start:end]
        return object()

    monkeypatch.setattr(llm_mod, "call_llm", _call)
    monkeypatch.setattr(llm_mod, "extract_text", _extract)


def test_run_pending_rebuilds_index_once_for_many_files(ac_root, monkeypatch):
    cfg = config_mod.Config()
    with fts.cursor() as conn:
        for nm in ("project-a.md", "project-b.md", "project-c.md"):
            _seed_flagged(conn, nm)

    _mock_identity_compact(monkeypatch)

    calls = {"n": 0}
    real_rebuild = entries_mod.rebuild_index

    def _counting_rebuild(conn):
        calls["n"] += 1
        return real_rebuild(conn)

    monkeypatch.setattr(entries_mod, "rebuild_index", _counting_rebuild)
    # compact.py imports rebuild_index via the entries module, so patch there too
    # if it holds its own ref. It calls ``entries_mod.rebuild_index`` so the above
    # patch is sufficient; assert the count.
    with fts.cursor() as conn:
        results = compact_mod.run_pending(cfg, conn)

    accepted = [r for r in results if r.accepted]
    assert len(accepted) == 3  # all three flagged files compacted (identity = accept)
    assert calls["n"] == 1  # ONE rebuild for the whole batch, not three


def test_run_pending_no_accept_skips_rebuild(ac_root, monkeypatch):
    """If nothing is accepted, run_pending must not rebuild at all."""
    cfg = config_mod.Config()
    with fts.cursor() as conn:
        _seed_flagged(conn, "project-x.md")

    # A 'compaction' that drops nearly all tokens → rejected (below preservation).
    monkeypatch.setattr(llm_mod, "call_llm", lambda *a, **k: object())
    monkeypatch.setattr(
        llm_mod, "extract_text", lambda resp: "---\ndescription: x\n---\n\n## [t] {id: y}\nzzz\n"
    )

    calls = {"n": 0}
    real_rebuild = entries_mod.rebuild_index
    monkeypatch.setattr(
        entries_mod,
        "rebuild_index",
        lambda conn: (calls.__setitem__("n", calls["n"] + 1), real_rebuild(conn))[1],
    )
    with fts.cursor() as conn:
        results = compact_mod.run_pending(cfg, conn)

    assert not any(r.accepted for r in results)
    assert calls["n"] == 0  # no accept → no rebuild


def test_compact_file_alone_does_not_rebuild(ac_root, monkeypatch):
    """A single ``compact_file`` no longer rebuilds — the caller (run_pending) owns it."""
    cfg = config_mod.Config()
    with fts.cursor() as conn:
        _seed_flagged(conn, "project-solo.md")

    _mock_identity_compact(monkeypatch)

    calls = {"n": 0}
    real_rebuild = entries_mod.rebuild_index
    monkeypatch.setattr(
        entries_mod,
        "rebuild_index",
        lambda conn: (calls.__setitem__("n", calls["n"] + 1), real_rebuild(conn))[1],
    )
    with fts.cursor() as conn:
        res = compact_mod.compact_file(cfg, conn, name="project-solo.md")

    assert res.accepted
    assert calls["n"] == 0  # compact_file itself does not rebuild

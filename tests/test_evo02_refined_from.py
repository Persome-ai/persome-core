"""EVO-02 — refined-from rebuild 三分判定 + UPDATE 保留旧条目在链.

``rebuild_index``'s superseded judgment becomes three-way (addendum EVO-02):

    if superseded_by:  superseded = 1
    elif refined_from: superseded = 0   # forced live, even if body is struck
    else:              superseded = 1 if _body_is_striked(body) else 0

The ``refined-from → 0`` branch short-circuits BEFORE the strike fallback, and
``_strip_strike`` runs only on entries judged superseded — so a refined-from entry
keeps its body verbatim and stays a live chain head. Existing data carries no
``refined-from`` tag → branch ② is dead code → byte-for-byte equivalent to before
(the zero-regression guarantee, pinned by the round-trip idempotency test).

The three-way rebuild judgment + refined-from PARSING are unchanged by 任务1; what
flips is the UPDATE *write口* semantics: a refinement now退役 the old version via a
supersede chain head carrying refined-from (双标签法, 对齐上游/engine.py), folding the
old fact out of recall while keeping the provenance distinguishable from a
contradiction in the evolution trail (``← [精炼自]`` vs ``← [曾]``).
"""

from __future__ import annotations

import sqlite3

from persome.evomem.models import ReconcileAction, ReconcileOp, ReconcileResult
from persome.store import entries as entries_mod
from persome.store import files as files_mod
from persome.store import fts
from tests.markdown_landing import apply_update


def _superseded(conn: sqlite3.Connection, entry_id: str) -> int:
    row = conn.execute("SELECT superseded FROM entries WHERE id=?", (entry_id,)).fetchone()
    return int(row["superseded"]) if row else -1


def _content(conn: sqlite3.Connection, entry_id: str) -> str:
    row = conn.execute("SELECT content FROM entries WHERE id=?", (entry_id,)).fetchone()
    return (row["content"] if row else "") or ""


def _superseded_map(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        r["id"]: int(r["superseded"])
        for r in conn.execute("SELECT id, superseded FROM entries").fetchall()
    }


# ── _parse_entries: refined-from tag parsing ──


def test_parse_entries_extracts_refined_from(ac_root):
    """A heading with #refined-from:OLD sets ParsedEntry.refined_from."""
    body = "## [2026-06-07T10:00] {id: new1} #refined-from:old1 #t\nrefined body\n"
    parsed = files_mod._parse_entries(body)
    assert len(parsed) == 1
    assert parsed[0].refined_from == "old1"
    assert parsed[0].superseded_by is None


def test_parse_entries_distinguishes_superseded_by_from_refined_from(ac_root):
    body = (
        "## [2026-06-07T10:00] {id: a} #superseded-by:b\n~~old~~\n\n"
        "## [2026-06-07T10:01] {id: c} #refined-from:a\nrefined\n"
    )
    parsed = files_mod._parse_entries(body)
    by_id = {e.id: e for e in parsed}
    assert by_id["a"].superseded_by == "b"
    assert by_id["a"].refined_from is None
    assert by_id["c"].refined_from == "a"
    assert by_id["c"].superseded_by is None


# ── rebuild three-way judgment ──


def _write_raw_file(name: str, body: str) -> None:
    """Write a memory file with a hand-crafted body (frontmatter auto-added)."""
    path = files_mod.memory_path(name)
    fm = files_mod.default_frontmatter(description="evo02 fixture", tags=["t"])
    files_mod.write_file(path, fm, body)


def test_fixture_a_refined_from_with_strike_stays_live(ac_root):
    """Fixture A: a refined-from entry whose body is ALSO whole-struck → rebuild
    judges it superseded=0 (refined-from short-circuits the strike fallback) and
    its body is NOT stripped."""
    body = "## [2026-06-07T10:00] {id: ra} #refined-from:old #t\n~~refined but struck~~\n"
    _write_raw_file("project-a.md", body)
    with fts.cursor() as conn:
        entries_mod.rebuild_index(conn)
        assert _superseded(conn, "ra") == 0
        # body kept verbatim (strike NOT stripped because not superseded)
        assert _content(conn, "ra") == "~~refined but struck~~"


def test_fixture_b_legit_inline_strike_preserved(ac_root):
    """Fixture B: a live entry with a legitimate inline ~~strike~~ (not whole-body)
    is NOT mis-flagged and its content keeps the strike markers."""
    body = "## [2026-06-07T10:00] {id: rb} #t\nkeep ~~this~~ inline strike and more\n"
    _write_raw_file("project-b.md", body)
    with fts.cursor() as conn:
        entries_mod.rebuild_index(conn)
        assert _superseded(conn, "rb") == 0
        # _body_is_striked is False (not whole-body) → not superseded → body kept
        assert _content(conn, "rb") == "keep ~~this~~ inline strike and more"


def test_fixture_b_prime_whole_strike_orphan_stays_superseded(ac_root):
    """Fixture B': a whole-body-struck entry with NO refined-from tag keeps the
    current behavior → superseded=1, strike stripped from content."""
    body = "## [2026-06-07T10:00] {id: rbp} #t\n~~dead orphan~~\n"
    _write_raw_file("project-c.md", body)
    with fts.cursor() as conn:
        entries_mod.rebuild_index(conn)
        assert _superseded(conn, "rbp") == 1
        assert _content(conn, "rbp") == "dead orphan"  # strike stripped


# ── zero-regression: rebuild idempotency on existing supersede data ──


def test_rebuild_idempotent_superseded_column(ac_root):
    """Round-trip invariant: running rebuild_index twice leaves the (id, superseded)
    column identical — and a real supersede chain keeps its judgment unchanged."""
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-x.md", description="x", tags=["t"])
        old_id = entries_mod.append_entry(conn, name="project-x.md", content="v1", tags=["t"])
        entries_mod.supersede_entry(
            conn, name="project-x.md", old_entry_id=old_id, new_content="v2", reason="r", tags=["t"]
        )
        entries_mod.append_entry(conn, name="project-x.md", content="standalone", tags=["t"])

        entries_mod.rebuild_index(conn)
        first = _superseded_map(conn)
        entries_mod.rebuild_index(conn)
        second = _superseded_map(conn)
    assert first == second
    # the superseded old entry stays superseded across rebuilds
    assert first[old_id] == 1


# ── UPDATE semantics (双标签法): refinement RETIRES the old version + tags head ──


def test_update_retires_old_and_tags_head_refined_from(ac_root):
    """UPDATE 的 legacy markdown 落地 now退役 the old version (folds it out) while stamping the
    new chain head with refined-from:OLD (双标签法, 对齐上游/engine.py). This是任务1
    翻转后的关键断言: OLD superseded=1 (folded), NEW superseded=0 (head)，且该
    判定 survives a rebuild round-trip（链指针真相在 evo_nodes，entry_chain 已退役）。"""
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-x.md", description="x", tags=["t"])
        old_id = entries_mod.append_entry(conn, name="project-x.md", content="v1", tags=["t"])
        result = ReconcileResult(
            ops=[
                ReconcileOp(
                    action=ReconcileAction.UPDATE,
                    content="v2 refined",
                    target_id=old_id,
                    reason="refine",
                )
            ]
        )
        new_id = apply_update(conn, result.ops[0], file_name="project-x.md")
        # post-apply: OLD retired (superseded), NEW is the live head
        assert _superseded(conn, old_id) == 1
        assert _superseded(conn, new_id) == 0

        entries_mod.rebuild_index(conn)
        # DURABLE after rebuild: OLD folded (superseded=1), NEW head live
        assert _superseded(conn, old_id) == 1
        assert _superseded(conn, new_id) == 0
        # OLD carries #superseded-by (retired); NEW carries refined-from provenance
        parsed = files_mod.read_file(files_mod.memory_path("project-x.md"))
        old_entry = next(e for e in parsed.entries if e.id == old_id)
        new_entry = next(e for e in parsed.entries if e.id == new_id)
        assert old_entry.superseded_by == new_id
        assert new_entry.refined_from == old_id
        assert new_entry.superseded_by is None

"Tests for test write02 abstract."

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

from persome.evomem.models import (
    MemoryLayer,
    MemoryNode,
    ReconcileAction,
    ReconcileOp,
    ReconcileResult,
)
from persome.evomem.reconciler import Reconciler
from persome.store import entries as entries_mod
from persome.store import files as files_mod
from persome.store import fts
from tests.markdown_landing import apply_abstract

# ── markdown landing / store: the ABSTRACT landing path ──


def _superseded(conn: sqlite3.Connection, entry_id: str) -> int:
    row = conn.execute("SELECT superseded FROM entries WHERE id=?", (entry_id,)).fetchone()
    return int(row["superseded"]) if row else -1


def _superseded_all(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        r["id"]: int(r["superseded"])
        for r in conn.execute("SELECT id, superseded FROM entries").fetchall()
    }


def test_abstract_two_sources_lands_and_survives_rebuild(ac_root):
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-x.md", description="x", tags=["t"])
        a = entries_mod.append_entry(conn, name="project-x.md", content="fact A", tags=["t"])
        b = entries_mod.append_entry(conn, name="project-x.md", content="fact B", tags=["t"])
        result = ReconcileResult(
            ops=[
                ReconcileOp(
                    action=ReconcileAction.ABSTRACT,
                    content="merged A+B",
                    source_ids=[a, b],
                    reason="synthesize",
                )
            ]
        )
        c = apply_abstract(conn, result.ops[0], file_name="project-x.md")
        # post-apply: sources retired, C live
        assert _superseded(conn, a) == 1
        assert _superseded(conn, b) == 1
        assert _superseded(conn, c) == 0

        entries_mod.rebuild_index(conn)
        # DURABLE: sources still superseded, C still live
        assert _superseded(conn, a) == 1
        assert _superseded(conn, b) == 1
        assert _superseded(conn, c) == 0
        # C carries the abstracted-from provenance tag for both sources
        parsed = files_mod.read_file(files_mod.memory_path("project-x.md"))
        c_entry = next(e for e in parsed.entries if e.id == c)
        assert set(c_entry.abstracted_from) == {a, b}


def test_abstract_chain_semantics_option_two_no_supersede_pointers(ac_root):
    """Chain semantics ②: provenance only — no #superseded-by pointer on C/A/B."""
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-x.md", description="x", tags=["t"])
        a = entries_mod.append_entry(conn, name="project-x.md", content="fact A", tags=["t"])
        b = entries_mod.append_entry(conn, name="project-x.md", content="fact B", tags=["t"])
        result = ReconcileResult(
            ops=[
                ReconcileOp(
                    action=ReconcileAction.ABSTRACT,
                    content="merged",
                    source_ids=[a, b],
                )
            ]
        )
        c = apply_abstract(conn, result.ops[0], file_name="project-x.md")
        entries_mod.rebuild_index(conn)
        parsed = files_mod.read_file(files_mod.memory_path("project-x.md"))
        by_id = {e.id: e for e in parsed.entries}
        assert by_id[a].superseded_by is None
        assert by_id[b].superseded_by is None
        assert by_id[c].superseded_by is None
        assert _superseded(conn, a) == 1 and _superseded(conn, b) == 1
        assert _superseded(conn, c) == 0


def test_abstract_preserves_reconciliation_invariant(ac_root):
    """The ② selling point: the superseded fold judgment is consistent after ABSTRACT."""
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-x.md", description="x", tags=["t"])
        a = entries_mod.append_entry(conn, name="project-x.md", content="A", tags=["t"])
        b = entries_mod.append_entry(conn, name="project-x.md", content="B", tags=["t"])
        # an unrelated live entry, to make the set non-trivial
        entries_mod.append_entry(conn, name="project-x.md", content="unrelated", tags=["t"])
        result = ReconcileResult(
            ops=[ReconcileOp(action=ReconcileAction.ABSTRACT, content="m", source_ids=[a, b])]
        )
        apply_abstract(conn, result.ops[0], file_name="project-x.md")
        before = _superseded_all(conn)
        entries_mod.rebuild_index(conn)
        after = _superseded_all(conn)
    assert before == after
    live = {eid for eid, s in after.items() if s == 0}
    assert a not in live and b not in live  # both absorbed


# ── files.py: abstracted-from multi-value tag parsing ──


def test_parse_entries_extracts_abstracted_from_multivalue(ac_root):
    body = "## [2026-06-07T10:00] {id: c} #abstracted-from:a1,b2,c3 #t\nmerged body\n"
    parsed = files_mod._parse_entries(body)
    assert len(parsed) == 1
    assert parsed[0].abstracted_from == ["a1", "b2", "c3"]
    assert parsed[0].superseded_by is None


def test_parse_entries_abstracted_from_empty_by_default(ac_root):
    body = "## [2026-06-07T10:00] {id: c} #t\nplain\n"
    parsed = files_mod._parse_entries(body)
    assert parsed[0].abstracted_from == []


# ── reconciler: ABSTRACT iron-law handling ──


def _resp(payload: dict):
    msg = SimpleNamespace(content=json.dumps(payload, ensure_ascii=False), tool_calls=[])
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason="stop")],
        usage=SimpleNamespace(total_tokens=0),
    )


def _fake(payload: dict):
    def call(_messages):
        return _resp(payload)

    return call


def _nodes(*ids: str) -> list[MemoryNode]:
    return [MemoryNode(node_id=i, content=f"c{i}", layer=MemoryLayer.L2_FACT) for i in ids]


def test_abstract_with_two_known_sources_not_demoted():
    r = Reconciler(
        llm_call=_fake(
            {"ops": [{"action": "ABSTRACT", "content": "merged", "source_ids": ["a", "b"]}]}
        )
    )
    res = r.reconcile(["merged"], candidates=_nodes("a", "b"))
    op = res.ops[0]
    assert op.action is ReconcileAction.ABSTRACT
    assert set(op.source_ids) == {"a", "b"}


def test_abstract_with_fewer_than_two_sources_demoted_to_add():
    r = Reconciler(
        llm_call=_fake({"ops": [{"action": "ABSTRACT", "content": "m", "source_ids": ["a"]}]})
    )
    res = r.reconcile(["m"], candidates=_nodes("a", "b"))
    assert res.ops[0].action is ReconcileAction.ADD


def test_abstract_with_unknown_source_demoted_to_add():
    r = Reconciler(
        llm_call=_fake(
            {"ops": [{"action": "ABSTRACT", "content": "m", "source_ids": ["a", "ghost"]}]}
        )
    )
    res = r.reconcile(["m"], candidates=_nodes("a", "b"))
    assert res.ops[0].action is ReconcileAction.ADD


def test_abstract_exempt_from_anti_fork_second_supersede():
    """ABSTRACT absorbing a source that another op also targets is a controlled
    N→1 convergence — it must NOT be dropped by the anti-fork (1→N) rule."""
    r = Reconciler(
        llm_call=_fake(
            {
                "ops": [
                    {"action": "ABSTRACT", "content": "m1", "source_ids": ["a", "b"]},
                    {"action": "ABSTRACT", "content": "m2", "source_ids": ["a", "b"]},
                ]
            }
        )
    )
    res = r.reconcile(["m1", "m2"], candidates=_nodes("a", "b"))
    abstracts = [o for o in res.ops if o.action is ReconcileAction.ABSTRACT]
    # both ABSTRACTs survive — the anti-fork rule is supersede-specific, not applied
    # to the inverse N→1 convergence.
    assert len(abstracts) == 2

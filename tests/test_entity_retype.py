"""Entity adjudication verbs — the §1.2 dimension-criterion executor.

Deterministic, zero-LLM. Pins: retype renames across evo_nodes + entries/files
projections + the markdown receipt (kind SSOT = file prefix); shadow retires
without deleting; alias merge folds through person_graph's own write path and
the person roster (person- prefix filter) stops listing retyped/shadowed
entities — so knows-edge extraction excludes them by construction.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from persome import paths
from persome.evomem import retype
from persome.evomem.engine import EvoMemory
from persome.evomem.person_graph import PersonEvent, PersonGraph
from persome.evomem.reconciler import Reconciler
from persome.store import fts


def _no_llm(messages):
    raise AssertionError("deterministic path must not call LLM")


def _mem() -> EvoMemory:
    return EvoMemory(user_id="u1", reconciler=Reconciler(llm_call=_no_llm))


class _Source:
    def __init__(self, events):
        self._events = events

    def events(self):
        return list(self._events)


def _seed_person(mem, name, when=None):
    PersonGraph(
        mem,
        cfg=SimpleNamespace(person_graph_enabled=True),
        name_source=_Source(
            [
                PersonEvent(
                    name=name,
                    summary="出现过",
                    occurred_at=when or datetime(2026, 6, 20, tzinfo=UTC),
                    confidence=0.9,
                )
            ]
        ),
    ).ingest()


def _persons(mem):
    return [p.canonical for p in PersonGraph(mem, cfg=SimpleNamespace()).list_persons()]


def test_retype_renames_across_stores_and_leaves_roster(ac_root):
    mem = _mem()
    _seed_person(mem, "研发群")
    md = paths.memory_dir()
    md.mkdir(parents=True, exist_ok=True)
    with fts.cursor() as conn:
        old = retype._find_entity_file(conn, "研发群")
    assert old is not None
    (md / old).write_text("# receipts")
    res = retype.retype_entity("研发群", "org")
    assert res.new_file.startswith("org-") and res.evo_rows > 0 and res.md_renamed
    with fts.cursor() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) FROM evo_nodes WHERE file_name = ?", (res.old_file,)
        ).fetchone()[0]
    assert rows == 0
    # the kind SSOT is the prefix: the person roster no longer lists it
    assert "研发群" not in _persons(mem)


def test_retype_rejects_unknown_kind_and_missing_entity(ac_root):
    mem = _mem()
    _seed_person(mem, "张伟")
    with pytest.raises(ValueError, match="kind"):
        retype.retype_entity("张伟", "group_chat")
    with pytest.raises(ValueError, match="no person entity"):
        retype.retype_entity("不存在", "org")


def test_shadow_entity_retires_without_deleting(ac_root):
    mem = _mem()
    _seed_person(mem, "客户")
    res = retype.shadow_entity("客户")
    assert res.shadowed > 0
    with fts.cursor() as conn:
        left = conn.execute(
            "SELECT COUNT(*) FROM evo_nodes WHERE file_name = ? AND status = 'active'",
            (res.old_file,),
        ).fetchone()[0]
        total = conn.execute(
            "SELECT COUNT(*) FROM evo_nodes WHERE file_name = ?", (res.old_file,)
        ).fetchone()[0]
    assert left == 0 and total > 0  # retired, never deleted
    assert "客户" not in _persons(mem)


def test_merge_alias_folds_and_shadows_duplicate(ac_root):
    mem = _mem()
    _seed_person(mem, "沈砚舟")
    _seed_person(mem, "singularity-沈砚舟")
    cfg = SimpleNamespace(person_graph_enabled=True)
    res = retype.merge_alias("singularity-沈砚舟", "沈砚舟", cfg, memory=mem)
    assert res.alias_folded and res.shadowed > 0
    pg = PersonGraph(mem, cfg=SimpleNamespace())
    people = {p.canonical: p for p in pg.list_persons()}
    assert "singularity-沈砚舟" not in people
    assert "singularity-沈砚舟" in people["沈砚舟"].aliases

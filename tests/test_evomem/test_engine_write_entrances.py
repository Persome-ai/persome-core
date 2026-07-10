"Tests for test engine write entrances."

import re

import pytest

from persome.evomem.engine import EvoMemory, _validated_file_name
from persome.evomem.models import (
    MemoryLayer,
    MemoryNode,
    MemoryStatus,
    ReconcileAction,
    ReconcileOp,
)
from persome.evomem.reconciler import Reconciler
from persome.evomem.store import NodeStore

_MAKE_ID_RE = re.compile(r"^\d{8}-\d{4}-[0-9a-f]{6}$")


def _llm_must_not_be_called(messages):
    raise AssertionError(
        "\u786e\u5b9a\u6027\u8def\u5f84\u4e0d\u8bb8\u8c03 LLM\uff08\u7eb2\u9886\u4e0d\u53d8\u5f0f\u4e09\uff09"
    )


def _mem(store: NodeStore | None = None) -> EvoMemory:
    return EvoMemory(
        user_id="u1",
        reconciler=Reconciler(llm_call=_llm_must_not_be_called),
        store=store or NodeStore(user_id="u1"),
    )


def test_add_direct_lands_head_without_llm(ac_root):
    mem = _mem()
    nid = mem.add_direct(
        "\u7528\u6237\u504f\u597d uv",
        layer=MemoryLayer.L5_KNOWLEDGE,
        file_name="tool-uv",
        tags="tooling stable",
    )
    node = mem.store.get(nid)
    assert node is not None
    assert node.layer is MemoryLayer.L5_KNOWLEDGE
    assert node.file_name == "tool-uv.md"
    assert node.tags == "tooling stable"
    assert node.is_latest and node.status is MemoryStatus.ACTIVE


def test_node_id_uses_make_id_shape(ac_root):

    mem = _mem()
    nid = mem.add_direct("x")
    assert _MAKE_ID_RE.match(nid), nid


def test_apply_ops_supersede_routes_file_name(ac_root):
    store = NodeStore(user_id="u1")
    store.save(MemoryNode(node_id="old", content="\u559d\u5496\u5561", layer=MemoryLayer.L2_FACT))
    mem = _mem(store)
    op = ReconcileOp(action=ReconcileAction.SUPERSEDE, content="\u559d\u8336", target_id="old")
    (new_id,) = mem.apply_ops([op], file_name="user-preferences.md", tags="taste")
    head = store.get(new_id)
    assert head.file_name == "user-preferences.md" and head.tags == "taste"
    assert store.get("old").superseded_by == [new_id]


def test_file_name_prefix_validation_at_write_entrance(ac_root):
    mem = _mem()
    with pytest.raises(ValueError, match="must start with one of"):
        mem.add_direct("x", file_name="diary-2026.md")


def test_event_fence_rejected_at_write_entrance(ac_root):

    mem = _mem()
    with pytest.raises(ValueError, match="event"):
        mem.apply_ops(
            [ReconcileOp(action=ReconcileAction.ADD, content="x")],
            file_name="event-2026-06-10.md",
        )


def test_validated_file_name_empty_passthrough():
    assert _validated_file_name("") == ""


def test_update_records_refined_from_provenance(ac_root):
    store = NodeStore(user_id="u1")
    store.save(MemoryNode(node_id="old", content="\u559d\u5496\u5561", layer=MemoryLayer.L2_FACT))
    mem = _mem(store)
    op = ReconcileOp(
        action=ReconcileAction.UPDATE, content="\u559d\u7f8e\u5f0f\u5496\u5561", target_id="old"
    )
    (new_id,) = mem.apply_ops([op])

    head, old = store.get(new_id), store.get("old")

    assert old.status is MemoryStatus.SHADOW and old.superseded_by == []
    assert head.supersedes == [] and head.is_latest

    assert head.refined_from == "old"


def test_abstract_provenance_edge_not_linear_chain(ac_root):
    store = NodeStore(user_id="u1")
    for nid, content in [("a", "\u559d\u7f8e\u5f0f"), ("b", "\u559d\u62ff\u94c1")]:
        store.save(MemoryNode(node_id=nid, content=content, layer=MemoryLayer.L2_FACT))
    mem = _mem(store)
    op = ReconcileOp(
        action=ReconcileAction.ABSTRACT,
        content="\u7231\u559d\u5496\u5561",
        source_ids=["a", "b"],
        layer=MemoryLayer.L3_SUMMARY,
    )
    (new_id,) = mem.apply_ops([op])

    head = store.get(new_id)

    assert head.abstracted_from == ["a", "b"]
    assert head.supersedes == []
    for sid in ("a", "b"):
        src = store.get(sid)

        assert src.status is MemoryStatus.SHADOW and src.is_latest is False
        assert src.superseded_by == []
    actives = store.all_latest()
    assert len(actives) == 1 and actives[0].node_id == new_id


def test_save_and_retire_sources_skips_missing_source(ac_root):
    store = NodeStore(user_id="u1")
    store.save(MemoryNode(node_id="a", content="x", layer=MemoryLayer.L2_FACT))
    syn = MemoryNode(
        node_id="s", content="y", layer=MemoryLayer.L3_SUMMARY, abstracted_from=["a", "missing"]
    )
    store.save_and_retire_sources(syn, source_ids=["a", "missing"])
    assert store.get("a").is_latest is False
    assert store.get("s").is_latest is True


def _prepared(node_id: str, content: str, **kw) -> MemoryNode:
    kw.setdefault("layer", MemoryLayer.L2_FACT)
    kw.setdefault("file_name", "project-x.md")
    return MemoryNode(node_id=node_id, content=content, **kw)


def test_reconciler_is_optional_but_add_requires_it(ac_root):
    mem = EvoMemory(user_id="u1")
    nid = mem.add_direct("x", file_name="project-x")
    assert mem.store.get(nid) is not None
    with pytest.raises(RuntimeError, match="Reconciler"):
        mem.add("y")


def test_commit_node_lands_prepared_node_and_validates_file_name(ac_root):
    mem = EvoMemory(user_id="u1")
    nid = mem.commit_node(_prepared("20260611-1000-aaaaaa", "fact", valid_from="2026-06-11T10:00"))
    node = mem.store.get(nid)
    assert node is not None and node.valid_from == "2026-06-11T10:00"

    with pytest.raises(ValueError, match="event"):
        mem.commit_node(_prepared("20260611-1001-bbbbbb", "e", file_name="event-2026-06-11.md"))


def test_commit_supersede_atomic_with_old_valid_until(ac_root):
    mem = EvoMemory(user_id="u1")
    old = mem.commit_node(_prepared("20260611-1002-cccccc", "v1"))
    new = mem.commit_supersede(
        _prepared("20260611-1003-dddddd", "v2", supersedes=[old]),
        old_id=old,
        old_valid_until="2026-06-11T10:03",
    )
    old_node, new_node = mem.store.get(old), mem.store.get(new)
    assert old_node.superseded_by == [new] and old_node.status is MemoryStatus.SHADOW
    assert old_node.valid_until == "2026-06-11T10:03"
    assert new_node.supersedes == [old] and new_node.is_latest

    mem.store.shadow(old, valid_until="2026-06-11T23:59")
    assert mem.store.get(old).valid_until == "2026-06-11T10:03"


def test_commit_retire_stamps_valid_until_once(ac_root):
    mem = EvoMemory(user_id="u1")
    nid = mem.commit_node(_prepared("20260611-1004-eeeeee", "stale"))
    mem.commit_retire(nid, valid_until="2026-06-11T12:34")
    node = mem.store.get(nid)
    assert node.status is MemoryStatus.SHADOW and not node.is_latest
    assert node.valid_until == "2026-06-11T12:34"
    mem.commit_retire(nid, valid_until="2026-06-12T00:00")
    assert mem.store.get(nid).valid_until == "2026-06-11T12:34"


def test_store_shadow_without_valid_until_is_unchanged(ac_root):

    store = NodeStore(user_id="u1")
    store.save(MemoryNode(node_id="n1", content="x", layer=MemoryLayer.L2_FACT))
    store.shadow("n1")
    node = store.get("n1")
    assert node.status is MemoryStatus.SHADOW and node.valid_until is None

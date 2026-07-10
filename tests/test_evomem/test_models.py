from persome.evomem.models import (
    MemoryLayer,
    MemoryNode,
    MemoryStatus,
    ReconcileAction,
    ReconcileOp,
)


def test_layer_from_string_canonical_and_alias():
    assert MemoryLayer.from_string("l2_fact") is MemoryLayer.L2_FACT
    assert MemoryLayer.from_string("PROFILE") is MemoryLayer.L4_IDENTITY  # v1 alias
    assert MemoryLayer.from_string("raw") is MemoryLayer.L1_RAW


def test_node_defaults_are_a_fresh_head():
    n = MemoryNode(node_id="a", content="\u559c\u6b22\u5496\u5561", layer=MemoryLayer.L2_FACT)
    assert n.is_latest is True
    assert n.status is MemoryStatus.ACTIVE
    assert n.supersedes == [] and n.superseded_by == []
    assert n.is_on_chain() is False


def test_node_on_chain_when_linked():
    n = MemoryNode(
        node_id="b", content="\u559c\u6b22\u8336", layer=MemoryLayer.L2_FACT, supersedes=["a"]
    )
    assert n.is_on_chain() is True


def test_reconcile_op_roundtrip():
    op = ReconcileOp(
        action=ReconcileAction.SUPERSEDE,
        content="\u559d\u8336",
        target_id="a",
        reason="\u53e3\u5473\u53d8\u4e86",
    )
    assert op.action is ReconcileAction.SUPERSEDE
    assert op.enters_chain() is True
    assert (
        ReconcileOp(action=ReconcileAction.UPDATE, content="x", target_id="a").enters_chain()
        is False
    )
    assert (
        ReconcileOp(action=ReconcileAction.ADD, content="\u65b0\u4e8b\u5b9e").enters_chain()
        is False
    )

import json
from types import SimpleNamespace

from persome.evomem.models import MemoryLayer, MemoryNode, ReconcileAction
from persome.evomem.reconciler import Reconciler


def _resp(payload: dict):
    msg = SimpleNamespace(content=json.dumps(payload, ensure_ascii=False), tool_calls=[])
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason="stop")],
        usage=SimpleNamespace(total_tokens=0),
    )


def _fake(payload: dict):
    """Return a fake llm_call that always replies with ``payload`` (OpenAI-shaped)."""

    def call(_messages):
        return _resp(payload)

    return call


def test_novel_info_becomes_add():
    r = Reconciler(
        llm_call=_fake(
            {"ops": [{"action": "ADD", "content": "\u7528\u6237\u4f4f\u5728\u4e0a\u6d77"}]}
        )
    )
    res = r.reconcile(["\u7528\u6237\u4f4f\u5728\u4e0a\u6d77"], candidates=[])
    assert len(res.ops) == 1 and res.ops[0].action is ReconcileAction.ADD
    assert res.ops[0].target_id is None


def test_contradiction_becomes_supersede_targeting_candidate():
    old = MemoryNode(
        node_id="a", content="\u7528\u6237\u559d\u5496\u5561", layer=MemoryLayer.L2_FACT
    )
    r = Reconciler(
        llm_call=_fake(
            {
                "ops": [
                    {
                        "action": "SUPERSEDE",
                        "content": "\u7528\u6237\u559d\u8336",
                        "target_id": "a",
                        "reason": "\u53e3\u5473\u53d8\u5316",
                    }
                ]
            }
        )
    )
    res = r.reconcile(["\u7528\u6237\u73b0\u5728\u559d\u8336"], candidates=[old])
    op = res.ops[0]
    assert op.action is ReconcileAction.SUPERSEDE and op.target_id == "a"
    assert op.enters_chain() is True


def test_supersede_with_unknown_target_is_demoted_to_add():
    r = Reconciler(
        llm_call=_fake({"ops": [{"action": "SUPERSEDE", "content": "x", "target_id": "ghost"}]})
    )
    res = r.reconcile(["x"], candidates=[])
    assert res.ops[0].action is ReconcileAction.ADD


def test_no_forked_chain_second_supersede_on_same_target_dropped():
    old = MemoryNode(node_id="a", content="\u65e7", layer=MemoryLayer.L2_FACT)
    r = Reconciler(
        llm_call=_fake(
            {
                "ops": [
                    {"action": "SUPERSEDE", "content": "\u65b01", "target_id": "a"},
                    {"action": "SUPERSEDE", "content": "\u65b02", "target_id": "a"},
                ]
            }
        )
    )
    res = r.reconcile(["\u65b01", "\u65b02"], candidates=[old])
    supersedes = [o for o in res.ops if o.action is ReconcileAction.SUPERSEDE]
    assert len(supersedes) == 1
    assert any(o.action is ReconcileAction.ADD for o in res.ops)


def test_supersede_then_update_same_target_demotes_update_to_add():
    old = MemoryNode(node_id="a", content="\u65e7", layer=MemoryLayer.L2_FACT)
    r = Reconciler(
        llm_call=_fake(
            {
                "ops": [
                    {"action": "SUPERSEDE", "content": "\u53d6\u4ee3", "target_id": "a"},
                    {"action": "UPDATE", "content": "\u7cbe\u70bc", "target_id": "a"},
                ]
            }
        )
    )
    res = r.reconcile(["\u53d6\u4ee3", "\u7cbe\u70bc"], candidates=[old])
    assert res.ops[0].action is ReconcileAction.SUPERSEDE and res.ops[0].target_id == "a"

    assert res.ops[1].action is ReconcileAction.ADD
    assert res.ops[1].target_id is None


def test_update_then_supersede_same_target_demotes_supersede_to_add():
    old = MemoryNode(node_id="a", content="\u65e7", layer=MemoryLayer.L2_FACT)
    r = Reconciler(
        llm_call=_fake(
            {
                "ops": [
                    {"action": "UPDATE", "content": "\u7cbe\u70bc", "target_id": "a"},
                    {"action": "SUPERSEDE", "content": "\u53d6\u4ee3", "target_id": "a"},
                ]
            }
        )
    )
    res = r.reconcile(["\u7cbe\u70bc", "\u53d6\u4ee3"], candidates=[old])
    assert res.ops[0].action is ReconcileAction.UPDATE and res.ops[0].target_id == "a"
    assert res.ops[1].action is ReconcileAction.ADD
    assert res.ops[1].target_id is None


def test_delete_then_targeted_op_same_target_demotes_to_add():
    old = MemoryNode(node_id="a", content="\u65e7", layer=MemoryLayer.L2_FACT)
    r = Reconciler(
        llm_call=_fake(
            {
                "ops": [
                    {"action": "DELETE", "target_id": "a", "reason": "obsolete"},
                    {"action": "SUPERSEDE", "content": "\u590d\u6d3b?", "target_id": "a"},
                ]
            }
        )
    )
    res = r.reconcile(["x", "y"], candidates=[old])
    assert res.ops[0].action is ReconcileAction.DELETE and res.ops[0].target_id == "a"
    assert res.ops[1].action is ReconcileAction.ADD
    assert res.ops[1].target_id is None


def test_abstract_sources_enter_touched_set_blocking_later_targeted_op():
    a = MemoryNode(node_id="a", content="\u6e901", layer=MemoryLayer.L2_FACT)
    b = MemoryNode(node_id="b", content="\u6e902", layer=MemoryLayer.L2_FACT)
    r = Reconciler(
        llm_call=_fake(
            {
                "ops": [
                    {"action": "ABSTRACT", "content": "\u5408\u5e76", "source_ids": ["a", "b"]},
                    {"action": "SUPERSEDE", "content": "\u518d\u52a8\u6e90a", "target_id": "a"},
                ]
            }
        )
    )
    res = r.reconcile(["\u5408\u5e76", "\u518d\u52a8\u6e90a"], candidates=[a, b])
    assert res.ops[0].action is ReconcileAction.ABSTRACT
    assert sorted(res.ops[0].source_ids) == ["a", "b"]

    assert res.ops[1].action is ReconcileAction.ADD
    assert res.ops[1].target_id is None


def test_supersede_then_abstract_same_target_demotes_abstract_to_add():
    a = MemoryNode(node_id="a", content="\u65e7a", layer=MemoryLayer.L2_FACT)
    b = MemoryNode(node_id="b", content="\u65e7b", layer=MemoryLayer.L2_FACT)
    r = Reconciler(
        llm_call=_fake(
            {
                "ops": [
                    {"action": "SUPERSEDE", "content": "\u53d6\u4ee3a", "target_id": "a"},
                    {"action": "ABSTRACT", "content": "\u5408\u5e76ab", "source_ids": ["a", "b"]},
                ]
            }
        )
    )
    res = r.reconcile(["\u53d6\u4ee3a", "\u5408\u5e76ab"], candidates=[a, b])
    assert res.ops[0].action is ReconcileAction.SUPERSEDE and res.ops[0].target_id == "a"

    assert res.ops[1].action is ReconcileAction.ADD
    assert res.ops[1].target_id is None
    assert res.ops[1].source_ids == []


def test_update_then_update_same_target_demotes_second_to_add():
    old = MemoryNode(node_id="a", content="\u65e7", layer=MemoryLayer.L2_FACT)
    r = Reconciler(
        llm_call=_fake(
            {
                "ops": [
                    {"action": "UPDATE", "content": "\u7cbe\u70bc1", "target_id": "a"},
                    {"action": "UPDATE", "content": "\u7cbe\u70bc2", "target_id": "a"},
                ]
            }
        )
    )
    res = r.reconcile(["\u7cbe\u70bc1", "\u7cbe\u70bc2"], candidates=[old])
    assert res.ops[0].action is ReconcileAction.UPDATE and res.ops[0].target_id == "a"
    assert res.ops[1].action is ReconcileAction.ADD
    assert res.ops[1].target_id is None


def test_update_then_delete_same_target_demotes_delete_to_add():
    old = MemoryNode(node_id="a", content="\u65e7", layer=MemoryLayer.L2_FACT)
    r = Reconciler(
        llm_call=_fake(
            {
                "ops": [
                    {"action": "UPDATE", "content": "\u7cbe\u70bc", "target_id": "a"},
                    {"action": "DELETE", "target_id": "a", "reason": "obsolete"},
                ]
            }
        )
    )
    res = r.reconcile(["\u7cbe\u70bc", "\u5220\u9664"], candidates=[old])
    assert res.ops[0].action is ReconcileAction.UPDATE and res.ops[0].target_id == "a"
    assert res.ops[1].action is ReconcileAction.ADD
    assert res.ops[1].target_id is None

import json
from types import SimpleNamespace

from persome.evomem.engine import EvoMemory
from persome.evomem.models import MemoryLayer
from persome.evomem.reconciler import Reconciler


def _resp(payload):
    msg = SimpleNamespace(content=json.dumps(payload, ensure_ascii=False), tool_calls=[])
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason="stop")],
        usage=SimpleNamespace(total_tokens=0),
    )


def test_coffee_then_tea_recall_returns_one_evolved_result(ac_root):

    calls = iter(
        [
            _resp({"ops": [{"action": "ADD", "content": "\u7528\u6237\u559d\u5496\u5561"}]}),
            _resp(
                {
                    "ops": [
                        {
                            "action": "SUPERSEDE",
                            "content": "\u7528\u6237\u559d\u8336",
                            "target_id": None,
                            "reason": "\u53e3\u5473\u53d8\u5316",
                        }
                    ]
                }
            ),
        ]
    )

    def fake(messages):
        return next(calls)

    mem = EvoMemory(user_id="u1", reconciler=Reconciler(llm_call=fake))
    ids1 = mem.add("\u7528\u6237\u559d\u5496\u5561")
    assert len(ids1) == 1

    coffee_id = ids1[0]
    calls2 = iter(
        [
            _resp(
                {
                    "ops": [
                        {
                            "action": "SUPERSEDE",
                            "content": "\u7528\u6237\u559d\u8336",
                            "target_id": coffee_id,
                            "reason": "\u53e3\u5473\u53d8\u5316",
                        }
                    ]
                }
            )
        ]
    )
    mem2 = EvoMemory(
        user_id="u1",
        reconciler=Reconciler(llm_call=lambda m: next(calls2)),
        store=mem._store,
    )
    mem2.add("\u7528\u6237\u73b0\u5728\u559d\u8336")

    out = mem2.search("\u559d\u4ec0\u4e48")
    evolved = [h for h in out if h.get("is_evolved")]
    assert len(evolved) == 1
    assert evolved[0]["node"].content == "\u7528\u6237\u559d\u8336"
    assert [c["content"] for c in evolved[0]["evolution_chain"]] == [
        "\u7528\u6237\u559d\u8336",
        "\u7528\u6237\u559d\u5496\u5561",
    ]


def test_apply_op_abstract_retires_all_sources(ac_root):

    from persome.evomem.models import MemoryNode, ReconcileAction, ReconcileOp
    from persome.evomem.store import NodeStore

    store = NodeStore(user_id="u1")
    store.save(MemoryNode(node_id="a", content="\u559d\u7f8e\u5f0f", layer=MemoryLayer.L2_FACT))
    store.save(MemoryNode(node_id="b", content="\u559d\u62ff\u94c1", layer=MemoryLayer.L2_FACT))
    mem = EvoMemory(
        user_id="u1",
        reconciler=Reconciler(llm_call=lambda m: _resp({"ops": []})),
        store=store,
    )

    op = ReconcileOp(
        action=ReconcileAction.ABSTRACT,
        content="\u7231\u559d\u5496\u5561",
        source_ids=["a", "b"],
        layer=MemoryLayer.L3_SUMMARY,
    )
    new_id = mem._apply_op(op, layer=MemoryLayer.L2_FACT)

    assert new_id is not None
    assert store.get("a").is_latest is False
    assert store.get("b").is_latest is False
    heads = store.all_latest()
    assert len(heads) == 1 and heads[0].node_id == new_id


def test_apply_op_update_atomic_single_active_head(ac_root):

    from persome.evomem.models import MemoryNode, ReconcileAction, ReconcileOp
    from persome.evomem.store import NodeStore

    store = NodeStore(user_id="u1")
    store.save(MemoryNode(node_id="a", content="\u559d\u5496\u5561", layer=MemoryLayer.L2_FACT))
    mem = EvoMemory(
        user_id="u1",
        reconciler=Reconciler(llm_call=lambda m: _resp({"ops": []})),
        store=store,
    )

    op = ReconcileOp(
        action=ReconcileAction.UPDATE,
        content="\u559d\u7f8e\u5f0f\u5496\u5561",
        target_id="a",
        layer=MemoryLayer.L2_FACT,
    )
    new_id = mem._apply_op(op, layer=MemoryLayer.L2_FACT)

    assert new_id is not None
    assert store.get("a").is_latest is False
    heads = store.all_latest()
    assert len(heads) == 1 and heads[0].node_id == new_id

    assert store.get(new_id).supersedes == []

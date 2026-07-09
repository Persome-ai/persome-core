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
    # 第一次写：候选空 → ADD
    calls = iter(
        [
            _resp({"ops": [{"action": "ADD", "content": "用户喝咖啡"}]}),
            _resp(
                {
                    "ops": [
                        {
                            "action": "SUPERSEDE",
                            "content": "用户喝茶",
                            "target_id": None,
                            "reason": "口味变化",
                        }
                    ]
                }
            ),
        ]
    )

    # SUPERSEDE 的 target_id 在 add() 内由候选解析补齐：reconciler 拿到候选后 LLM 回 target
    def fake(messages):
        return next(calls)

    mem = EvoMemory(user_id="u1", reconciler=Reconciler(llm_call=fake))
    ids1 = mem.add("用户喝咖啡")
    assert len(ids1) == 1
    # 第二次写时让 LLM 看到候选并回真实 target_id
    coffee_id = ids1[0]
    calls2 = iter(
        [
            _resp(
                {
                    "ops": [
                        {
                            "action": "SUPERSEDE",
                            "content": "用户喝茶",
                            "target_id": coffee_id,
                            "reason": "口味变化",
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
    )  # 复用同一 store
    mem2.add("用户现在喝茶")

    out = mem2.search("喝什么")
    evolved = [h for h in out if h.get("is_evolved")]
    assert len(evolved) == 1
    assert evolved[0]["node"].content == "用户喝茶"  # 链头
    assert [c["content"] for c in evolved[0]["evolution_chain"]] == ["用户喝茶", "用户喝咖啡"]


def test_system2_writes_schema_node(ac_root):
    from persome.evomem.schema_miner import SchemaMiner

    sch = _resp(
        {
            "central_proposition": "偏好极简",
            "supporting_summary": "s",
            "expected_inferences": ["拒大依赖"],
            "confidence": 0.7,
        }
    )
    mem = EvoMemory(
        user_id="u1",
        reconciler=Reconciler(llm_call=lambda m: _resp({"ops": []})),
        schema_miner=SchemaMiner(llm_call=lambda m: sch),
    )
    res = mem.run_system2(["用 uv", "用 ruff", "拒 litellm"])
    assert res is not None and res.central_proposition == "偏好极简"
    schemas = [n for n in mem._store.all_latest() if n.layer is MemoryLayer.L6_SCHEMA]
    assert len(schemas) == 1


def test_apply_op_abstract_retires_all_sources(ac_root):
    # issue #416：ABSTRACT op 必须收编 source_ids，否则落兜底 ADD → 源节点仍活跃，
    # N→1 退化成 N+1 并存。
    from persome.evomem.models import MemoryNode, ReconcileAction, ReconcileOp
    from persome.evomem.store import NodeStore

    store = NodeStore(user_id="u1")
    store.save(MemoryNode(node_id="a", content="喝美式", layer=MemoryLayer.L2_FACT))
    store.save(MemoryNode(node_id="b", content="喝拿铁", layer=MemoryLayer.L2_FACT))
    mem = EvoMemory(
        user_id="u1",
        reconciler=Reconciler(llm_call=lambda m: _resp({"ops": []})),
        store=store,
    )

    op = ReconcileOp(
        action=ReconcileAction.ABSTRACT,
        content="爱喝咖啡",
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
    # issue #448：UPDATE op 必须原子落新 + shadow 旧，结束后唯一活跃链头；不进演化链。
    from persome.evomem.models import MemoryNode, ReconcileAction, ReconcileOp
    from persome.evomem.store import NodeStore

    store = NodeStore(user_id="u1")
    store.save(MemoryNode(node_id="a", content="喝咖啡", layer=MemoryLayer.L2_FACT))
    mem = EvoMemory(
        user_id="u1",
        reconciler=Reconciler(llm_call=lambda m: _resp({"ops": []})),
        store=store,
    )

    op = ReconcileOp(
        action=ReconcileAction.UPDATE,
        content="喝美式咖啡",
        target_id="a",
        layer=MemoryLayer.L2_FACT,
    )
    new_id = mem._apply_op(op, layer=MemoryLayer.L2_FACT)

    assert new_id is not None
    assert store.get("a").is_latest is False
    heads = store.all_latest()
    assert len(heads) == 1 and heads[0].node_id == new_id
    # UPDATE 不建链：新节点不 supersede 旧。
    assert store.get(new_id).supersedes == []

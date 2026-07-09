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
    r = Reconciler(llm_call=_fake({"ops": [{"action": "ADD", "content": "用户住在上海"}]}))
    res = r.reconcile(["用户住在上海"], candidates=[])
    assert len(res.ops) == 1 and res.ops[0].action is ReconcileAction.ADD
    assert res.ops[0].target_id is None


def test_contradiction_becomes_supersede_targeting_candidate():
    old = MemoryNode(node_id="a", content="用户喝咖啡", layer=MemoryLayer.L2_FACT)
    r = Reconciler(
        llm_call=_fake(
            {
                "ops": [
                    {
                        "action": "SUPERSEDE",
                        "content": "用户喝茶",
                        "target_id": "a",
                        "reason": "口味变化",
                    }
                ]
            }
        )
    )
    res = r.reconcile(["用户现在喝茶"], candidates=[old])
    op = res.ops[0]
    assert op.action is ReconcileAction.SUPERSEDE and op.target_id == "a"
    assert op.enters_chain() is True


def test_supersede_with_unknown_target_is_demoted_to_add():
    r = Reconciler(
        llm_call=_fake({"ops": [{"action": "SUPERSEDE", "content": "x", "target_id": "ghost"}]})
    )
    res = r.reconcile(["x"], candidates=[])  # 候选为空 → ghost 非法
    assert res.ops[0].action is ReconcileAction.ADD  # 降级，避免悬空指针


def test_no_forked_chain_second_supersede_on_same_target_dropped():
    old = MemoryNode(node_id="a", content="旧", layer=MemoryLayer.L2_FACT)
    r = Reconciler(
        llm_call=_fake(
            {
                "ops": [
                    {"action": "SUPERSEDE", "content": "新1", "target_id": "a"},
                    {"action": "SUPERSEDE", "content": "新2", "target_id": "a"},
                ]
            }
        )
    )
    res = r.reconcile(["新1", "新2"], candidates=[old])
    supersedes = [o for o in res.ops if o.action is ReconcileAction.SUPERSEDE]
    assert len(supersedes) == 1  # 第二条同 target 被丢弃（不分叉）；第二条降级 ADD
    assert any(o.action is ReconcileAction.ADD for o in res.ops)


# ── 统一 touched set：SUPERSEDE/UPDATE/DELETE 共享一张表（任务2，对齐上游 Pass2） ──


def test_supersede_then_update_same_target_demotes_update_to_add():
    """同一 target 先被 SUPERSEDE 命中后，再来的 UPDATE 命中同 target → 降级 ADD
    （统一 touched 表，与上游 SUPERSEDE ∪ UPDATE 互斥对齐）。"""
    old = MemoryNode(node_id="a", content="旧", layer=MemoryLayer.L2_FACT)
    r = Reconciler(
        llm_call=_fake(
            {
                "ops": [
                    {"action": "SUPERSEDE", "content": "取代", "target_id": "a"},
                    {"action": "UPDATE", "content": "精炼", "target_id": "a"},
                ]
            }
        )
    )
    res = r.reconcile(["取代", "精炼"], candidates=[old])
    assert res.ops[0].action is ReconcileAction.SUPERSEDE and res.ops[0].target_id == "a"
    # 第二条 UPDATE 同 target 被降级 ADD（清空 target_id），不会二次 retire 旧节点。
    assert res.ops[1].action is ReconcileAction.ADD
    assert res.ops[1].target_id is None


def test_update_then_supersede_same_target_demotes_supersede_to_add():
    """对称方向：UPDATE 先动了 target，后续 SUPERSEDE 命中同 target → 降级 ADD。"""
    old = MemoryNode(node_id="a", content="旧", layer=MemoryLayer.L2_FACT)
    r = Reconciler(
        llm_call=_fake(
            {
                "ops": [
                    {"action": "UPDATE", "content": "精炼", "target_id": "a"},
                    {"action": "SUPERSEDE", "content": "取代", "target_id": "a"},
                ]
            }
        )
    )
    res = r.reconcile(["精炼", "取代"], candidates=[old])
    assert res.ops[0].action is ReconcileAction.UPDATE and res.ops[0].target_id == "a"
    assert res.ops[1].action is ReconcileAction.ADD
    assert res.ops[1].target_id is None


def test_delete_then_targeted_op_same_target_demotes_to_add():
    """DELETE 纳入统一 touched 表：retire 后再来的 SUPERSEDE 命中同 target → 降级 ADD。"""
    old = MemoryNode(node_id="a", content="旧", layer=MemoryLayer.L2_FACT)
    r = Reconciler(
        llm_call=_fake(
            {
                "ops": [
                    {"action": "DELETE", "target_id": "a", "reason": "obsolete"},
                    {"action": "SUPERSEDE", "content": "复活?", "target_id": "a"},
                ]
            }
        )
    )
    res = r.reconcile(["x", "y"], candidates=[old])
    assert res.ops[0].action is ReconcileAction.DELETE and res.ops[0].target_id == "a"
    assert res.ops[1].action is ReconcileAction.ADD
    assert res.ops[1].target_id is None


def test_abstract_sources_enter_touched_set_blocking_later_targeted_op():
    """ABSTRACT 吸收的每个 source 也进 touched 表：之后 targeted op 命中刚被吸收的源
    → 降级 ADD（防止命中已被合并的源造成悬空/分叉）。"""
    a = MemoryNode(node_id="a", content="源1", layer=MemoryLayer.L2_FACT)
    b = MemoryNode(node_id="b", content="源2", layer=MemoryLayer.L2_FACT)
    r = Reconciler(
        llm_call=_fake(
            {
                "ops": [
                    {"action": "ABSTRACT", "content": "合并", "source_ids": ["a", "b"]},
                    {"action": "SUPERSEDE", "content": "再动源a", "target_id": "a"},
                ]
            }
        )
    )
    res = r.reconcile(["合并", "再动源a"], candidates=[a, b])
    assert res.ops[0].action is ReconcileAction.ABSTRACT
    assert sorted(res.ops[0].source_ids) == ["a", "b"]
    # 第二条命中已被 ABSTRACT 吸收的源 a → 降级 ADD。
    assert res.ops[1].action is ReconcileAction.ADD
    assert res.ops[1].target_id is None


def test_supersede_then_abstract_same_target_demotes_abstract_to_add():
    """对称缺口（bug_006）：SUPERSEDE 先动了 target a，后续 ABSTRACT 把 a 当 source 吸收
    → ABSTRACT 也要查 touched，命中已 retire 的 a 则降级 ADD。

    反向（ABSTRACT→targeted）已有守卫；这里堵 ABSTRACT 分支自己不查 touched 的漏洞：
    否则 a 会同时背上 ``#superseded-by:new`` 和在合并节点 C 里被 ``abstracted-from`` 引用，
    产生自相矛盾的 provenance。"""
    a = MemoryNode(node_id="a", content="旧a", layer=MemoryLayer.L2_FACT)
    b = MemoryNode(node_id="b", content="旧b", layer=MemoryLayer.L2_FACT)
    r = Reconciler(
        llm_call=_fake(
            {
                "ops": [
                    {"action": "SUPERSEDE", "content": "取代a", "target_id": "a"},
                    {"action": "ABSTRACT", "content": "合并ab", "source_ids": ["a", "b"]},
                ]
            }
        )
    )
    res = r.reconcile(["取代a", "合并ab"], candidates=[a, b])
    assert res.ops[0].action is ReconcileAction.SUPERSEDE and res.ops[0].target_id == "a"
    # 第二条 ABSTRACT 吸收了已被 SUPERSEDE retire 的源 a → 整条降级 ADD（清空 source_ids）。
    assert res.ops[1].action is ReconcileAction.ADD
    assert res.ops[1].target_id is None
    assert res.ops[1].source_ids == []


def test_update_then_update_same_target_demotes_second_to_add():
    """漂移审计 3.2 复核补漏：UPDATE 也是 targeted retire，UPDATE×UPDATE 同 target
    同样会双重退役（两条 ``#refined``/``#superseded-by`` 边 = 分叉）→ 第二条降级 ADD。"""
    old = MemoryNode(node_id="a", content="旧", layer=MemoryLayer.L2_FACT)
    r = Reconciler(
        llm_call=_fake(
            {
                "ops": [
                    {"action": "UPDATE", "content": "精炼1", "target_id": "a"},
                    {"action": "UPDATE", "content": "精炼2", "target_id": "a"},
                ]
            }
        )
    )
    res = r.reconcile(["精炼1", "精炼2"], candidates=[old])
    assert res.ops[0].action is ReconcileAction.UPDATE and res.ops[0].target_id == "a"
    assert res.ops[1].action is ReconcileAction.ADD
    assert res.ops[1].target_id is None


def test_update_then_delete_same_target_demotes_delete_to_add():
    """UPDATE 先动了 target，后续 DELETE 命中同 target → 降级 ADD（统一 touched 表
    覆盖 SUPERSEDE/UPDATE/DELETE 全部两两组合，不只 SUPERSEDE 维度）。"""
    old = MemoryNode(node_id="a", content="旧", layer=MemoryLayer.L2_FACT)
    r = Reconciler(
        llm_call=_fake(
            {
                "ops": [
                    {"action": "UPDATE", "content": "精炼", "target_id": "a"},
                    {"action": "DELETE", "target_id": "a", "reason": "obsolete"},
                ]
            }
        )
    )
    res = r.reconcile(["精炼", "删除"], candidates=[old])
    assert res.ops[0].action is ReconcileAction.UPDATE and res.ops[0].target_id == "a"
    assert res.ops[1].action is ReconcileAction.ADD
    assert res.ops[1].target_id is None

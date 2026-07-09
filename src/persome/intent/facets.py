"""快路意图的正交 facet 表示 + 确定性装配。

spec: ``docs/superpowers/specs/2026-06-26-faceted-fast-path-intent-schema-design.md``。

把扁平、混轴的 ``kind`` 拆成正交元组（Telos / Object / Temporality / Provenance /
Outwardness）。**强确定的 facet 在这里零 LLM 算出来**——时间性的表面核（``when_text``
在不在）、来源方向校验、Telos→外向 映射；**语义核（telos）+ 潜在槽（recurrence /
condition / object）由 LLM 给**。``assemble`` 把两者装配成完整元组。

``project_kind`` 是 **迁移桥**：把元组投影回旧 ``kind`` 字符串，让 ``sink`` /
``dedup_key`` / ``stamp_temporal`` / app 侧 sentinel·follow-up 全部不动照常工作。
Phase B 下游改成直接按 facet 消费后，投影随之退役。

纯函数、零 LLM、零网络、零 I/O —— intent-golden 确定性档直接驱动它。
"""

from __future__ import annotations

from dataclasses import dataclass

# ── facet 全域（元组末值 = 兜底，spec §facet 全域）────────────────────────── #
# telos = the OUTCOME axis (5). "Who executes" is NOT a telos — it rides the `with`/object
# facets (§9 completeness audit, 2026-06-30: the old 6th value `delegate` mixed the executor
# axis into the outcome axis and already collapsed to produce's kind downstream). Legacy /
# drift `delegate` is normalized to `produce` in `_norm_telos` (same kind=assignment), so old
# rows and a model that still emits it stay byte-compatible.
TELOS = ("commit", "acquire", "produce", "transact", "monitor")
OBJECT = ("self", "person", "org", "agent", "ambient")
TEMPORALITY = ("immediate", "scheduled", "conditional", "recurring", "open")
PROVENANCE = ("committed", "proposed", "inferred")
OUTWARDNESS = ("internal", "outward_reversible", "outward_irreversible")

# ⑤ 外向 = 动作类型属性（纯数据表；focus 不匹配的降级在 sink 侧，不在此）。
TELOS_OUTWARDNESS: dict[str, str] = {
    "commit": "outward_reversible",  # 摆链接 / 草稿回对方，用户按 Enter
    "acquire": "outward_reversible",  # 取到的产物（链接/答案）摆回
    "produce": "internal",  # 产出 / 交办落本地任务（delegate 归一到 produce 后走这条）
    "transact": "outward_irreversible",  # 发 / 交 / 付 / publish
    "monitor": "internal",  # 盯状态，零打扰
}

# facet provenance → 旧 payload provenance 词表（下游读这个，保持不变）。
_PROV_TO_PAYLOAD: dict[str, str] = {
    "committed": "user_committed",
    "proposed": "counterpart_proposed",
    "inferred": "inferred",
}

# 单条快路 LLM 只出言语行为两值；inferred 由 surfacing 推断，不从这里出。
_LLM_PROVENANCE = frozenset({"committed", "proposed"})
# committed 在 received 方向上的最低置信门：低于则压回 proposed（防误动作，§装配规则④）。
_COMMIT_CONF_FLOOR = 0.9
# object_hint 表示"自我指向"的标记值（→ object=self，区分 reminder vs meeting）。
_SELF_TOKENS = frozenset({"self", "自己", "我", "me", "本人"})


@dataclass(frozen=True)
class Facets:
    """一个意图在五个正交轴上各取一值 + 三个潜在槽。"""

    telos: str
    object: str
    temporality: str
    provenance: str
    outwardness: str
    condition: str | None = None  # condition_hint：触发事件逐字（temporality=conditional 时）
    recurrence: str | None = None  # recurrence_hint：daily/weekly…（temporality=recurring 时）
    object_entity: str | None = None  # 对象实体名（object=person/org/agent 时）

    @property
    def payload_provenance(self) -> str:
        """映射到旧 payload provenance 词表（下游不动）。"""
        return _PROV_TO_PAYLOAD.get(self.provenance, "user_committed")


def _clean(v: object) -> str | None:
    """空白 / None → None；否则返回 strip 后的字符串。"""
    s = str(v or "").strip()
    return s or None


def _norm_telos(v: object) -> str:
    s = str(v or "").strip().lower()
    # `delegate` 退役（§9 审计：executor 轴混入 outcome 轴）→ 归一到 `produce`（同 kind=assignment），
    # 旧库行 / 模型偶尔仍 emit 它都字节兼容。
    if s == "delegate":
        return "produce"
    # 未知语义核 → 最保守的"获取"（内部可降级、低打扰），永不 KeyError（R6 降级）。
    return s if s in TELOS else "acquire"


def assemble(
    llm_out: dict,
    *,
    direction: str,
    counterpart: str,
    when_text: str,
    confidence: float = 0.0,
) -> Facets:
    """LLM 语义核 + 潜在槽 ⊕ 确定性表面信号 → 完整 facet 元组。

    确定性层给默认/先验，LLM 的潜在槽在它"表面层会算错"处 override：

    - ③ 时间性：``when_text`` 在→``scheduled``、不在→``open``；``condition_hint`` →
      ``conditional``、``recurrence_hint`` → ``recurring``（hint 覆盖默认）。
    - ④ 来源：LLM 言语行为读数为主；缺失时用方向先验（outgoing=承诺 / incoming=提议）；
      ``committed`` 但方向 incoming 且 ``confidence`` < 0.9 → 压回 ``proposed``。
    - ② 对象：``object_hint`` 优先（``self`` 特判 → object=self）；否则会话对手方；皆无→ambient。
    - ⑤ 外向：``TELOS_OUTWARDNESS[telos]``。
    """
    telos = _norm_telos(llm_out.get("telos"))

    # ③ 时间性
    condition = _clean(llm_out.get("condition_hint"))
    recurrence = _clean(llm_out.get("recurrence_hint"))
    if condition is not None:
        temporality = "conditional"
    elif recurrence is not None:
        temporality = "recurring"
    elif _clean(when_text) is not None:
        temporality = "scheduled"
    else:
        temporality = "open"

    # ④ 来源
    prov = str(llm_out.get("provenance") or "").strip().lower()
    if prov not in _LLM_PROVENANCE:
        prov = "committed" if direction == "outgoing" else "proposed"
    if prov == "committed" and direction == "incoming" and confidence < _COMMIT_CONF_FLOOR:
        prov = "proposed"

    # ② 对象
    obj_hint = _clean(llm_out.get("object_hint"))
    if obj_hint is not None and obj_hint.lower() in _SELF_TOKENS:
        obj, obj_entity = "self", None
    elif obj_hint is not None:
        obj, obj_entity = "person", obj_hint
    elif _clean(counterpart) is not None:
        obj, obj_entity = "person", _clean(counterpart)
    else:
        obj, obj_entity = "ambient", None

    # ⑤ 外向
    outwardness = TELOS_OUTWARDNESS.get(telos, "internal")

    return Facets(
        telos=telos,
        object=obj,
        temporality=temporality,
        provenance=prov,
        outwardness=outwardness,
        condition=condition,
        recurrence=recurrence,
        object_entity=obj_entity,
    )


def project_kind(f: Facets) -> str:
    """迁移桥：facet 元组 → 旧 ``kind`` 字符串。

    让 ``dedup_key`` / ``stamp_temporal``（按 kind 的 grace）/ app 侧 sentinel
    （``richProposalKinds``）/ follow-up eligibility 全部不动。Phase B 下游改为
    直接按 facet 消费后退役。
    """
    if f.telos == "commit":
        if f.object == "self":
            return "reminder"
        if f.object == "person":
            return "meeting"
        return "calendar"  # 无具体人的日程/事件
    if f.telos == "produce":  # 产出 / 交办（delegate 已归一到 produce）→ 落 in-app 任务
        return "assignment"
    # acquire / monitor / transact / 兜底
    return "info_need"

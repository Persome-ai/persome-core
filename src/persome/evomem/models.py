"""evomem 核心数据模型（clean-room，参见 docs/research/2026-06-06-hy-memory-teardown.md §1-§2）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class MemoryLayer(StrEnum):
    L0_BASIC_INFO = "l0_basic_info"
    L1_RAW = "l1_raw"
    L2_FACT = "l2_fact"
    L3_SUMMARY = "l3_summary"
    L4_IDENTITY = "l4_identity"
    L5_KNOWLEDGE = "l5_knowledge"
    L6_SCHEMA = "l6_schema"
    L7_INTENTION = "l7_intention"

    @classmethod
    def from_string(cls, value: str) -> MemoryLayer:
        v = value.lower().strip()
        alias = {
            "profile": cls.L4_IDENTITY,
            "identity": cls.L4_IDENTITY,
            "dialogue": cls.L2_FACT,
            "fact": cls.L2_FACT,
            "summary": cls.L3_SUMMARY,
            "knowledge": cls.L5_KNOWLEDGE,
            "schema": cls.L6_SCHEMA,
            "intention": cls.L7_INTENTION,
            "raw": cls.L1_RAW,
            # WorkThread 归宿（spec 2026-06-12 §五）：进行中工作线的投影按 L3
            # 收纳——alias 机制，不动七层枚举本身。
            "working_state": cls.L3_SUMMARY,
        }
        if v in alias:
            return alias[v]
        for layer in cls:
            if layer.value == v:
                return layer
        raise ValueError(f"Invalid memory layer: {value}")


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    SHADOW = "shadow"
    ARCHIVED = "archived"


class ReconcileAction(StrEnum):
    ADD = "ADD"
    UPDATE = "UPDATE"
    SUPERSEDE = "SUPERSEDE"
    DELETE = "DELETE"
    # WRITE-02: N→1 多源合成 — 一条新合成节点吸收 ``source_ids`` 里的 N(≥2) 条旧
    # 条目。与分叉(1→N，禁止)相反，是受控的收敛。
    ABSTRACT = "ABSTRACT"


@dataclass
class ReconcileOp:
    """Reconciler 的单条操作指令（teardown §4）。"""

    action: ReconcileAction
    content: str = ""
    target_id: str | None = None  # UPDATE/SUPERSEDE/DELETE 的目标（恰好一个）
    reason: str = ""
    layer: MemoryLayer = MemoryLayer.L2_FACT
    # WRITE-02: ABSTRACT 的成分来源（N≥2 条旧条目 id）。其它 op 留空。
    source_ids: list[str] = field(default_factory=list)

    def enters_chain(self) -> bool:
        """只有 SUPERSEDE 进演化链。"""
        return self.action is ReconcileAction.SUPERSEDE


@dataclass
class ReconcileResult:
    ops: list[ReconcileOp] = field(default_factory=list)


@dataclass
class MemoryNode:
    node_id: str
    content: str
    layer: MemoryLayer
    supersedes: list[str] = field(default_factory=list)
    superseded_by: list[str] = field(default_factory=list)
    is_latest: bool = True
    status: MemoryStatus = MemoryStatus.ACTIVE
    memory_at: datetime | None = None
    gmt_created: datetime | None = None
    user_id: str = "default"
    agent_id: str = "default"
    # ── SSOT 升格扩展（切换设计稿 §1.2 + Q8，PR-2）────────────────────────────
    # evo_nodes 升格为链 SSOT 后必须承载现由 markdown tag / 旁挂表侧载的语义。
    # 全部可选缺省（None/''/[]），既有调用方（engine/reconciler）零改动。
    # 时间类字段（occurred_at/valid_from/valid_until）保持原始字符串形态——
    # 旁挂表存的是分钟级 ISO 文本，经 datetime round-trip 会引入 ``:00`` 秒尾
    # 破坏与 entry_temporal/entry_metadata 的字节级对账（PR-7 退役前提）。
    file_name: str = ""  # markdown 投影路由（现 entries.path）
    tags: str = ""  # 语义 tag（空格分隔；不含链 tag——链由指针列表达）
    refined_from: str | None = None  # UPDATE 同向精炼出处（现 #refined-from tag）
    abstracted_from: list[str] = field(default_factory=list)  # ABSTRACT N→1 多源出处
    confidence: str | None = None  # 元认知层（现 entry_metadata 旁挂表）
    conflicted: bool = False
    occurred_at: str | None = None
    schema_summary: str | None = None  # L6_SCHEMA 四元组：supporting_summary
    schema_inferences: list[str] | None = None  # L6_SCHEMA 四元组：expected_inferences
    schema_confidence: float | None = None  # L6_SCHEMA 四元组：confidence
    valid_from: str | None = None  # Q8：收编 entry_temporal（旁挂表 PR-7 退役）
    valid_until: str | None = None

    def is_on_chain(self) -> bool:
        return bool(self.supersedes) or bool(self.superseded_by)

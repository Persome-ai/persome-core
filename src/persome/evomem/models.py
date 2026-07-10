"""Core data models for the versioned evomem graph."""

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

    ABSTRACT = "ABSTRACT"


@dataclass
class ReconcileOp:
    action: ReconcileAction
    content: str = ""
    target_id: str | None = None
    reason: str = ""
    layer: MemoryLayer = MemoryLayer.L2_FACT

    source_ids: list[str] = field(default_factory=list)

    def enters_chain(self) -> bool:
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

    file_name: str = ""
    tags: str = ""
    refined_from: str | None = None
    abstracted_from: list[str] = field(default_factory=list)
    confidence: str | None = None
    conflicted: bool = False
    occurred_at: str | None = None
    schema_summary: str | None = None
    schema_inferences: list[str] | None = None
    schema_confidence: float | None = None
    valid_from: str | None = None
    valid_until: str | None = None

    def is_on_chain(self) -> bool:
        return bool(self.supersedes) or bool(self.superseded_by)

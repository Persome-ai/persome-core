"""Reconciler —— 四操作契约（重实现的灵魂，参见 teardown §4）。

对每条新记忆，结合候选旧记忆，让 LLM 决策 ADD/UPDATE/SUPERSEDE/DELETE，
并在代码侧强制铁律（不接悬空指针、不分叉演化链）。LLM 走依赖注入的
``llm_call``（OpenAI 形返回），测试直接注入 fake，不依赖网络。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ._json import parse_json_object
from .models import MemoryLayer, MemoryNode, ReconcileAction, ReconcileOp, ReconcileResult

LLMCall = Callable[[list[dict]], Any]

_PROMPT_PATH = Path(__file__).parent / "prompts" / "reconciler.md"

# 这三种操作必须指向恰好一个真实候选；ADD 不需要 target。
_TARGETED = {ReconcileAction.UPDATE, ReconcileAction.SUPERSEDE, ReconcileAction.DELETE}


def _load_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _content_of(resp: Any) -> str:
    """从 OpenAI 形返回里取 message.content（容错空串）。"""
    try:
        return resp.choices[0].message.content or ""
    except (AttributeError, IndexError):
        return ""


class Reconciler:
    def __init__(self, llm_call: LLMCall, prompt: str | None = None) -> None:
        self._llm_call = llm_call
        self._prompt = prompt if prompt is not None else _load_prompt()

    def reconcile(self, new_memories: list[str], candidates: list[MemoryNode]) -> ReconcileResult:
        """对一批新记忆调和出操作列表，并强制铁律。

        铁律由代码兜底，不信任 LLM：
        - UPDATE/SUPERSEDE/DELETE 的 ``target_id`` 必须在候选集中，否则降级 ADD
          （避免悬空指针）。
        - 同一 target 只允许被一条 targeted op（SUPERSEDE/UPDATE/DELETE）命中：后续
          任意 targeted op 命中**已被触碰**的同一 target 一律降级 ADD（永不分叉演化链）。
          这与上游 ``_parse_ops Pass2`` 的 ``SUPERSEDE ∪ UPDATE`` 互斥对齐，并把 DELETE
          也纳入统一 touched 表（一次 retire 后不再二次 retire）。
        - ABSTRACT（N→1 收敛）豁免二次-supersede 规则，但它吸收的每个 ``source_id``
          也登记进 touched 表，防止之后的 targeted op 再去命中刚被吸收的源（悬空/分叉）。
        """
        messages = self._build_messages(new_memories, candidates)
        parsed = parse_json_object(_content_of(self._llm_call(messages)))
        raw_ops = (parsed or {}).get("ops") or []

        candidate_ids = {c.node_id for c in candidates}
        # Unified touched set: any target a prior SUPERSEDE/UPDATE/DELETE已经动过，
        # 或任一 ABSTRACT 已吸收过的 source。后续 targeted op 再命中即降级 ADD。
        touched: set[str] = set()
        # Subset of ``touched`` written ONLY by a targeted retire (SUPERSEDE/UPDATE/
        # DELETE). The ABSTRACT guard (bug_006) reads this, NOT the unified set: a
        # source retired by a targeted op can't also be absorbed by ABSTRACT
        # (conflicting provenance), but two ABSTRACTs sharing sources is a legal
        # N→1 convergence and must NOT block each other (WRITE-02 exemption).
        targeted_touched: set[str] = set()
        ops: list[ReconcileOp] = []
        for raw in raw_ops:
            op = self._coerce_op(raw)
            if op is None:
                continue
            op = self._enforce_iron_laws(op, candidate_ids, touched, targeted_touched)
            # Register what this op touched so a later op can't double-retire it.
            if op.action is ReconcileAction.ABSTRACT:
                touched.update(op.source_ids)
            elif op.action in _TARGETED and op.target_id is not None:
                touched.add(op.target_id)
                targeted_touched.add(op.target_id)
            ops.append(op)
        return ReconcileResult(ops=ops)

    # -- internals -------------------------------------------------------

    def _build_messages(self, new_memories: list[str], candidates: list[MemoryNode]) -> list[dict]:
        if candidates:
            cand_lines = "\n".join(f"- id={c.node_id}: {c.content}" for c in candidates)
        else:
            cand_lines = "（无候选旧记忆）"
        new_lines = "\n".join(f"- {m}" for m in new_memories)
        user = (
            f"## 新记忆\n{new_lines}\n\n"
            f"## 候选旧记忆\n{cand_lines}\n\n"
            "请按系统提示输出 JSON 操作列表。"
        )
        return [
            {"role": "system", "content": self._prompt},
            {"role": "user", "content": user},
        ]

    @staticmethod
    def _coerce_op(raw: Any) -> ReconcileOp | None:
        """把一个 LLM dict 映射成 ReconcileOp；非法 action 丢弃。"""
        if not isinstance(raw, dict):
            return None
        action_raw = str(raw.get("action", "")).strip().upper()
        try:
            action = ReconcileAction(action_raw)
        except ValueError:
            return None
        target = raw.get("target_id")
        target_id = str(target) if target not in (None, "", "null") else None
        layer = raw.get("layer")
        try:
            mem_layer = MemoryLayer.from_string(layer) if layer else MemoryLayer.L2_FACT
        except ValueError:
            mem_layer = MemoryLayer.L2_FACT
        # WRITE-02: ABSTRACT carries N≥2 source ids; coerce to a clean str list.
        raw_sources = raw.get("source_ids") or []
        source_ids = (
            [str(s) for s in raw_sources if str(s).strip()] if isinstance(raw_sources, list) else []
        )
        return ReconcileOp(
            action=action,
            content=str(raw.get("content", "")),
            target_id=target_id,
            reason=str(raw.get("reason", "")),
            layer=mem_layer,
            source_ids=source_ids,
        )

    @staticmethod
    def _enforce_iron_laws(
        op: ReconcileOp,
        candidate_ids: set[str],
        touched: set[str],
        targeted_touched: set[str],
    ) -> ReconcileOp:
        """把违反铁律的 op 降级为 ADD（清空 target）。

        ``touched`` is the unified set of every target a prior SUPERSEDE/UPDATE/DELETE
        already retired, plus every source a prior ABSTRACT absorbed.
        ``targeted_touched`` is the subset written only by a targeted retire; the
        ABSTRACT guard reads it so two ABSTRACTs over shared sources don't block.
        """
        # WRITE-02: ABSTRACT is a controlled N→1 convergence (the inverse of a
        # 1→N fork), so it is EXEMPT from the anti-fork second-supersede rule.
        # Its own guard: every source must exist and there must be ≥2 of them —
        # otherwise it is a degenerate merge and we demote to ADD.
        if op.action is ReconcileAction.ABSTRACT:
            if len(op.source_ids) < 2 or any(s not in candidate_ids for s in op.source_ids):
                return _demote_to_add(op)
            # bug_006: symmetry with the ABSTRACT→targeted guard. A prior
            # SUPERSEDE/UPDATE/DELETE that already retired one of these sources
            # means absorbing it here would give that source two conflicting
            # provenance edges (``#superseded-by:new`` AND ``abstracted-from`` in
            # the merged node). Demote to ADD so we never double-touch a source.
            # Checked against ``targeted_touched`` only — a source merged by an
            # earlier ABSTRACT may still be merged again (N→1 convergence is exempt
            # from the anti-fork rule), but a targeted-retired source may not.
            if any(s in targeted_touched for s in op.source_ids):
                return _demote_to_add(op)
            return op
        if op.action not in _TARGETED:
            return op
        # 铁律 1：target 必须真实存在。
        if op.target_id is None or op.target_id not in candidate_ids:
            return _demote_to_add(op)
        # 铁律 2：同一 target 不可被二次触碰（防分叉/二次 retire）。任一 targeted op
        # （SUPERSEDE/UPDATE/DELETE）命中已 touched 的 target 一律降级 ADD——与上游
        # ``SUPERSEDE ∪ UPDATE`` 互斥对齐，并把 DELETE 一并纳入统一 touched 表。
        if op.target_id in touched:
            return _demote_to_add(op)
        return op


def _demote_to_add(op: ReconcileOp) -> ReconcileOp:
    return ReconcileOp(
        action=ReconcileAction.ADD,
        content=op.content,
        target_id=None,
        reason=op.reason,
        layer=op.layer,
    )

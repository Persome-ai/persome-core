"LLM-assisted reconciliation constrained by deterministic chain invariants."

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ._json import parse_json_object
from .models import MemoryLayer, MemoryNode, ReconcileAction, ReconcileOp, ReconcileResult

LLMCall = Callable[[list[dict]], Any]

_PROMPT_PATH = Path(__file__).parent / "prompts" / "reconciler.md"


_TARGETED = {ReconcileAction.UPDATE, ReconcileAction.SUPERSEDE, ReconcileAction.DELETE}


def _load_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _content_of(resp: Any) -> str:
    try:
        return resp.choices[0].message.content or ""
    except (AttributeError, IndexError):
        return ""


class Reconciler:
    def __init__(self, llm_call: LLMCall, prompt: str | None = None) -> None:
        self._llm_call = llm_call
        self._prompt = prompt if prompt is not None else _load_prompt()

    def reconcile(self, new_memories: list[str], candidates: list[MemoryNode]) -> ReconcileResult:
        messages = self._build_messages(new_memories, candidates)
        parsed = parse_json_object(_content_of(self._llm_call(messages)))
        raw_ops = (parsed or {}).get("ops") or []

        candidate_ids = {c.node_id for c in candidates}

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
            cand_lines = "(no candidate memories)"
        new_lines = "\n".join(f"- {m}" for m in new_memories)
        user = (
            f"## New memories\n{new_lines}\n\n"
            f"## Candidate memories\n{cand_lines}\n\n"
            "Return the JSON operation list defined by the system prompt."
        )
        return [
            {"role": "system", "content": self._prompt},
            {"role": "user", "content": user},
        ]

    @staticmethod
    def _coerce_op(raw: Any) -> ReconcileOp | None:
        """Map an LLM dictionary to a ReconcileOp and discard invalid actions."""
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

        if op.target_id is None or op.target_id not in candidate_ids:
            return _demote_to_add(op)

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

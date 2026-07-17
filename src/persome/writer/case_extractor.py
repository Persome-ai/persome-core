"Extraction of reusable problem-solution cards from activity history."

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ..evomem.engine import EvoMemory
from ..evomem.models import MemoryLayer
from ..logger import get
from ..store import fts
from ..timeline import store as tl_store
from . import llm as llm_mod

logger = get("persome.writer")

# L5 knowledge cards route here (``topic-`` is a VALID_PREFIXES entry; there is
# no dedicated ``case-``/``knowledge-`` prefix, and adding one would touch
# files.py which is out of scope for this stage).
CASE_FILE = "topic-cases.md"

# Deterministic error/resolution discriminators. Kept narrow + bilingual so plain

# must actually look like a reported error / an actual fix.
_ERROR_RE = re.compile(
    r"error|exception|failed|failure|traceback|"
    r"\u62a5\u9519|\u5931\u8d25|\u5f02\u5e38|\u51fa\u9519|\u5d29\u6e83|"
    r"\u65e0\u6cd5|\u4e0d\u80fd",
    re.IGNORECASE,
)
_RESOLUTION_RE = re.compile(
    r"resolved|fixed|fix|solved|works?\s+now|passing|succeed|"
    r"\u89e3\u51b3|\u4fee\u597d|\u4fee\u590d|\u641e\u5b9a|\u8dd1\u901a|"
    r"\u901a\u8fc7|\u6210\u529f|\u597d\u4e86|\u53ef\u4ee5\u4e86",
    re.IGNORECASE,
)

# How many lines after an error line we will look ahead for a resolution signal.
_RESOLUTION_WINDOW = 8
# Hard cap on candidates distilled per run (bounds LLM cost on a noisy day).
_MAX_CANDIDATES = 12


@dataclass
class CaseCandidate:
    """A deterministically pre-filtered error→resolution span (pre-LLM)."""

    error_text: str
    resolution_text: str
    context: list[str] = field(default_factory=list)

    def to_prompt_block(self) -> str:
        lines = [f"Error signal: {self.error_text}", f"Resolution signal: {self.resolution_text}"]
        if self.context:
            lines.append("Context:")
            lines.extend(f"- {c}" for c in self.context)
        return "\n".join(lines)


@dataclass
class CaseResult:
    committed: bool = False
    summary: str = ""
    created_ids: list[str] = field(default_factory=list)
    candidates: int = 0
    skipped_reason: str = ""


# Injectable LLM seam: ``(cfg, stage, messages) -> response``. Live default is
# ``llm_mod.call_llm``; tests pass a fake. We resolve it lazily at call time so a
# monkeypatched ``llm_mod.call_llm`` (the ``fake_llm`` fixture) is picked up.
LlmCallFn = Callable[..., Any]


def _default_llm_call(cfg: Any, stage: str, messages: list[dict[str, Any]]) -> Any:
    return llm_mod.call_llm(cfg, stage, messages=messages, json_mode=True)


def _iter_block_lines(block: tl_store.TimelineBlock) -> list[str]:
    """Flatten a block into ordered text lines for scanning.

    Uses the LLM-normalized ``entries`` plus any ``action_trace`` action strings
    (action traces capture command/tool steps where errors and fixes surface).
    """
    lines: list[str] = []
    for entry in block.entries:
        text = str(entry).strip()
        if text:
            lines.append(text)
    for action in block.action_trace:
        if not isinstance(action, dict):
            continue
        # action_trace dicts are free-form; pull the human-readable fields.
        for key in ("action", "text", "detail", "result", "outcome"):
            val = action.get(key)
            if isinstance(val, str) and val.strip():
                lines.append(val.strip())
    return lines


def find_candidates(blocks: list[tl_store.TimelineBlock]) -> list[CaseCandidate]:
    """Deterministic pre-filter: pair each error line with a nearby resolution.

    Walks all lines across blocks in chronological order. An error line with no
    resolution signal within the next ``_RESOLUTION_WINDOW`` lines is dropped (no
    half cards). Each error consumes its matched resolution so two distinct fixes
    don't both bind to the same later line.
    """
    lines: list[str] = []
    for block in blocks:
        lines.extend(_iter_block_lines(block))

    candidates: list[CaseCandidate] = []
    consumed_resolution: set[int] = set()
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if _ERROR_RE.search(line):
            # Look ahead a bounded window for a resolution signal.
            res_idx = None
            end = min(n, i + 1 + _RESOLUTION_WINDOW)
            for j in range(i + 1, end):
                if j in consumed_resolution:
                    continue
                if _RESOLUTION_RE.search(lines[j]):
                    res_idx = j
                    break
            if res_idx is not None:
                consumed_resolution.add(res_idx)
                context = lines[i + 1 : res_idx]
                candidates.append(
                    CaseCandidate(
                        error_text=line,
                        resolution_text=lines[res_idx],
                        context=context[:_RESOLUTION_WINDOW],
                    )
                )
                if len(candidates) >= _MAX_CANDIDATES:
                    break
                # Resume scanning after the matched resolution.
                i = res_idx + 1
                continue
        i += 1
    return candidates


_SYSTEM_PROMPT = (
    "You are the user's private assistant. The following activity span was "
    "deterministically selected because it appears to contain a problem and its resolution. "
    "Distill it into a reusable problem-solution card.\n"
    "Requirements:\n"
    "1. Return JSON only, shaped as "
    '{"problem": "...", "solution": "..."}\n'
    "2. problem concisely describes the failure; solution gives reusable resolution steps.\n"
    "3. If the span is noise or has no real resolution, return "
    '{"problem": "", "solution": ""}.\n'
    "4. Generalize reusable knowledge instead of repeating the raw log."
)


def _distill_one(
    cfg: Any, candidate: CaseCandidate, *, llm_call: LlmCallFn
) -> dict[str, str] | None:
    """One LLM call → ``{problem, solution}`` card, or None to drop the candidate.

    Fault-tolerant: any failure / unparseable / empty card → None (drop this
    candidate only).
    """
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": candidate.to_prompt_block()},
    ]
    try:
        resp = llm_call(cfg, "case_extractor", messages)
        text = llm_mod.extract_text(resp).strip()
    except Exception as exc:  # noqa: BLE001 — never let one candidate sink the run
        logger.warning("case_extractor: LLM call failed for a candidate: %s", exc)
        return None
    if not text:
        return None
    try:
        data = json.loads(_unfence(text))
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("case_extractor: malformed JSON card: %s", exc)
        return None
    if not isinstance(data, dict):
        return None
    problem = str(data.get("problem") or "").strip()
    solution = str(data.get("solution") or "").strip()
    if not problem or not solution:
        # Model judged this not a real problem→solution, or gave half a card.
        return None
    return {"problem": problem, "solution": solution}


def _unfence(text: str) -> str:
    """Strip a ```json fence if the model wrapped its JSON."""
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s)
    return s.strip()


def _card_content(card: dict[str, str]) -> str:
    return f"Problem: {card['problem']}\nSolution: {card['solution']}"


def run_case_extraction(
    cfg: Any,
    *,
    on_event: llm_mod.OnEventFn | None = None,
    llm_call: LlmCallFn | None = None,
    lookback_hours: int = 24,
    evidence_as_of: datetime | None = None,
    memory: EvoMemory | None = None,
) -> CaseResult:
    """Extract ``{problem, solution}`` cards from the recent timeline → L5 evomem.

    Off by default: a no-op (``skipped_reason='disabled'``) unless
    ``cfg.case_extraction_enabled`` is truthy.

    ``evidence_as_of`` owns the causal read boundary. The selected source
    interval is the preceding ``lookback_hours`` and never includes a timeline
    block that straddles or follows that cutoff. It defaults to the current
    aware wall clock for ordinary production calls. It does not change card
    persistence timestamps: evomem still records the real processing time.

    ``llm_call`` / ``memory`` are injectable seams for testing; the live defaults
    use provider-aware ``llm_mod.call_llm`` and a fresh ``EvoMemory`` (its
    deterministic ``add_direct`` write entrance — no reconciler/LLM needed).
    """

    def _emit(kind: str, payload: dict[str, Any]) -> None:
        if on_event is not None:
            on_event(kind, payload)

    if not getattr(cfg, "case_extraction_enabled", False):
        return CaseResult(
            committed=False, summary="Case extraction is disabled", skipped_reason="disabled"
        )

    llm_call = llm_call or _default_llm_call

    _emit("progress", {"value": 0.1, "label": "Collecting recent timeline"})
    cutoff = evidence_as_of or datetime.now().astimezone()
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("case extraction evidence_as_of must be timezone-aware")
    cutoff = cutoff.astimezone(UTC)
    since = cutoff - timedelta(hours=lookback_hours)
    with fts.cursor() as conn:
        blocks = tl_store.query_since(conn, since, until=cutoff)

    candidates = find_candidates(blocks)
    if not candidates:
        return CaseResult(
            committed=False,
            summary="No recent problem-solution candidates",
            candidates=0,
            skipped_reason="no candidates",
        )

    _emit("progress", {"value": 0.4, "label": f"Distilling {len(candidates)} candidates"})

    # ``add_direct`` is the deterministic public write entrance (no reconciler /
    # no LLM); a default-constructed EvoMemory provides it.
    mem = memory if memory is not None else EvoMemory()

    created_ids: list[str] = []
    for candidate in candidates:
        card = _distill_one(cfg, candidate, llm_call=llm_call)
        if card is None:
            continue
        node_id = mem.add_direct(
            _card_content(card),
            layer=MemoryLayer.L5_KNOWLEDGE,
            file_name=CASE_FILE,
            tags="case problem-solution",
        )
        created_ids.append(node_id)

    if not created_ids:
        return CaseResult(
            committed=False,
            summary="No candidate produced a valid case card",
            candidates=len(candidates),
            skipped_reason="no cards",
        )

    _emit("progress", {"value": 1.0, "label": f"Writing {len(created_ids)} case cards"})
    return CaseResult(
        committed=True,
        summary=f"Extracted {len(created_ids)} problem-solution cards",
        created_ids=created_ids,
        candidates=len(candidates),
    )

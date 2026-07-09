"""Case extraction (慢回路 E2 / 问题→解法卡).

A slow-loop stage that distills *reusable* ``error → resolution`` cards from the
activity timeline. The慢回路 matrix already covers workflows / habits /
automations / mental models (Dream · Pattern Detector · Consolidator ·
SchemaMiner); ``procedure`` is deliberately out of scope (it is subsumed by
Dream / Pattern Detector). What is still missing is "I hit a problem — here is
how I solved it", a card the user can re-apply next time the same wall appears.

Pipeline (deterministic pre-filter → one LLM call per candidate):

1. **Deterministic pre-filter** (``find_candidates``) — no LLM. Walk the
   timeline's per-line text in chronological order, mark a line that matches an
   *error* signal (``error|失败|异常|exception|failed|报错…``), then look ahead a
   bounded window for a *resolution* signal (``解决|修好|fixed|resolved…``). An
   error with **no** trailing resolution within the window is dropped (we never
   mint half a card / hallucinate a fix). Plain log noise that matches neither
   signal never enters the candidate set.

2. **LLM distillation** (one call per candidate, stage ``consolidator``) — turn
   the windowed error+resolution text into a ``{problem, solution}`` card. A
   throwing / unparseable / empty LLM reply drops *that* candidate only (the
   stage is fault-tolerant; one bad candidate never sinks the run).

3. **Sink** — each card lands through evomem's public deterministic write
   entrance (``EvoMemory(...).add_direct``) at ``MemoryLayer.L5_KNOWLEDGE``,
   routed to ``topic-cases.md`` (an L5 knowledge file under a VALID prefix). We
   only *import* evomem's public API — never edit it.

The whole stage is behind ``getattr(cfg, "case_extraction_enabled", False)``
(default OFF). The LLM call is an injectable seam (``llm_call``) so tests run
with the ``fake_llm`` fixture / ``PERSOME_LLM_MOCK=1`` without a network.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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
# log noise ("opened Safari", "已读消息") never trips the pre-filter — an anchor
# must actually look like a reported error / an actual fix.
_ERROR_RE = re.compile(
    r"error|exception|failed|failure|traceback|报错|失败|异常|出错|崩溃|无法|不能",
    re.IGNORECASE,
)
_RESOLUTION_RE = re.compile(
    r"resolved|fixed|fix|solved|works?\s+now|passing|succeed|"
    r"解决|修好|修复|搞定|跑通|通过|成功|好了|可以了",
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
        lines = [f"报错信号: {self.error_text}", f"解决信号: {self.resolution_text}"]
        if self.context:
            lines.append("上下文片段:")
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
    "你是用户的私人助理。下面是从用户的活动时间线里确定性筛出的一段"
    "「遇到问题→解决问题」的片段。请把它蒸馏成一张可复用的「问题→解法卡」。\n"
    "要求：\n"
    "1. 只输出 JSON，形如 "
    '{"problem": "...", "solution": "..."}\n'
    "2. problem 简洁描述遇到的问题/报错；solution 描述是怎么解决的（可复用的步骤）。\n"
    "3. 如果片段其实并不构成一个有效的「问题→解法」（比如只是噪音、没真正解决），"
    '返回 {"problem": "", "solution": ""}。\n'
    "4. 不要复述原始日志，要归纳成下次能照着做的知识。"
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
        resp = llm_call(cfg, "consolidator", messages)
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
    return f"问题：{card['problem']}\n解法：{card['solution']}"


def run_case_extraction(
    cfg: Any,
    *,
    on_event: llm_mod.OnEventFn | None = None,
    llm_call: LlmCallFn | None = None,
    lookback_hours: int = 24,
    memory: EvoMemory | None = None,
) -> CaseResult:
    """Extract ``{problem, solution}`` cards from the recent timeline → L5 evomem.

    Off by default: a no-op (``skipped_reason='disabled'``) unless
    ``cfg.case_extraction_enabled`` is truthy.

    ``llm_call`` / ``memory`` are injectable seams for testing; the live defaults
    use ``llm_mod.call_llm`` (Anthropic) and a fresh ``EvoMemory`` (its
    deterministic ``add_direct`` write entrance — no reconciler/LLM needed).
    """

    def _emit(kind: str, payload: dict[str, Any]) -> None:
        if on_event is not None:
            on_event(kind, payload)

    if not getattr(cfg, "case_extraction_enabled", False):
        return CaseResult(committed=False, summary="案例抽取未启用", skipped_reason="disabled")

    llm_call = llm_call or _default_llm_call

    _emit("progress", {"value": 0.1, "label": "收集近期时间线"})
    since = datetime.now().astimezone() - timedelta(hours=lookback_hours)
    with fts.cursor() as conn:
        blocks = tl_store.query_since(conn, since)

    candidates = find_candidates(blocks)
    if not candidates:
        return CaseResult(
            committed=False,
            summary="近期无「问题→解法」候选",
            candidates=0,
            skipped_reason="no candidates",
        )

    _emit("progress", {"value": 0.4, "label": f"蒸馏 {len(candidates)} 个候选"})

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
            summary="候选均未蒸馏出有效案例卡",
            candidates=len(candidates),
            skipped_reason="no cards",
        )

    _emit("progress", {"value": 1.0, "label": f"写入 {len(created_ids)} 张案例卡"})
    return CaseResult(
        committed=True,
        summary=f"抽取 {len(created_ids)} 张「问题→解法卡」",
        created_ids=created_ids,
        candidates=len(candidates),
    )

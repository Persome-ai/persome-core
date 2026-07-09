"""Session-end memory_delta consolidator — Memory-rebuild Phase 0 shadow channel.

Spec docs/superpowers/specs/2026-07-02-memory-rebuild-design.md §4.1/§6.2: ONE
LLM reading of the just-ended session, multiple structured heads —
``memory_delta {entities, assertions, relations, events}`` — the channel that
will (Phase 1, after dual-run parity) retire the four scattered extractors
(person name-source / relation LLM pass / case extraction / classifier
attribution). Phase 0 lands it SHADOW: the gated delta is persisted verbatim
into ``memory_deltas`` (status='shadow'); consumers are ``persome delta-report``
and the Phase-1 parity eval only — nothing writes memory from it yet.

§4.1 discipline — judgment belongs to the LLM (this one call), identity and
gating to code:

- **Roster multiple-choice**: known identities are injected as ``<roster>``;
  the model outputs roster ``ref``s or explicit ``new_entity`` strings — bare
  store-probing strings are rejected by the gate.
- **Evidence gate**: every item must quote the session text verbatim (same
  three-gate discipline as the relation extractor); no quote → dropped.
- **Closed predicate set**: relations must use the 6-predicate set from
  ``store/relation_edges.py``; anything else is dropped.
- **Confidence floor**: items below ``memory_delta.min_confidence`` drop.

Everything here is fail-open: an LLM error, malformed JSON, or a store error
logs a warning and returns ``written=False`` — the session-end chain
(classifier → pattern detector → workthread) is never disturbed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .. import events as events_mod
from .. import prompts
from ..evomem import identity as identity_mod
from ..logger import get
from ..store import fts
from ..store import memory_deltas as deltas_store
from ..store import relation_edges as edges_store
from ..timeline import store as tl_store
from . import llm as llm_mod

logger = get("persome.writer.memory_delta")

STAGE = "memory_delta"

_HEADS = ("entities", "assertions", "relations", "events")
_ENTITY_KINDS = frozenset({"person", "org", "project", "artifact"})
_PREDICATES = frozenset(p.value for p in edges_store.Predicate)
_POLARITIES = frozenset({"+", "-", "0"})  # §4.1 极性闭集（默认 0；quote 明确带极性才 ±）


def _norm_polarity(item: dict) -> str:
    v = item.get("polarity")
    return v if v in _POLARITIES else "0"


def _norm_ended(item: dict) -> bool:
    return item.get("ended") is True


# Injectable LLM seam (mirrors case_extractor): resolved lazily so the
# ``fake_llm`` fixture's monkeypatched ``llm_mod.call_llm`` is picked up.
LlmCallFn = Callable[..., Any]


def _default_llm_call(cfg: Any, stage: str, messages: list[dict[str, Any]]) -> Any:
    return llm_mod.call_llm(cfg, stage, messages=messages, json_mode=True)


@dataclass
class DeltaResult:
    session_id: str
    written: bool = False
    applied: bool = False  # §4.2 apply 通道跑了（apply_enabled）
    delta_id: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    dropped: int = 0
    skipped_reason: str = ""


def _safe_json(text: str) -> dict:
    """Parse the LLM reply into a dict; tolerate ```json fences and junk."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else ""
        if text.endswith("```"):
            text = text[:-3]
    try:
        data = json.loads(text) if text else {}
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _load_roster(cfg: Any) -> list[tuple[str, list[str]]]:
    """Known identities (canonical, aliases) from person_graph — the 选择题 menu.

    Fail-open: any read error → empty roster (the delta then only carries
    ``new_entity`` items; nothing breaks).
    """
    try:
        from ..evomem.engine import EvoMemory
        from ..evomem.person_graph import PersonGraph

        persons = PersonGraph(EvoMemory(), cfg=cfg).list_persons()
        limit = int(getattr(cfg.memory_delta, "roster_max", 60))
        return [(p.canonical, list(getattr(p, "aliases", []))) for p in persons[:limit]]
    except Exception:  # noqa: BLE001 — roster is best-effort
        logger.debug("memory_delta: roster load failed, empty", exc_info=True)
        return []


def _render_roster(roster: list[tuple[str, list[str]]]) -> str:
    if not roster:
        return "(no known identities yet)"
    lines = []
    for canonical, aliases in roster:
        alias_part = f"（aliases: {', '.join(aliases)}）" if aliases else ""
        lines.append(f"- {canonical}{alias_part}")
    return "\n".join(lines)


def _render_blocks(blocks: list[tl_store.TimelineBlock]) -> str:
    """Render session blocks as the ordered event log the prompt reads.

    Entries carry the timeline stage's verbatim-preserving normalization;
    ``focus_structured``/``focus_excerpt`` add the lossless backstop for chat
    text — the evidence gate checks quotes against THIS rendered text, so
    everything quotable must be in it.
    """
    parts: list[str] = []
    for b in blocks:
        window = f"[{b.start_time:%H:%M}-{b.end_time:%H:%M}] apps: {', '.join(b.apps_used)}"
        lines = [window]
        lines.extend(f"  - {e}" for e in b.entries)
        focus = (b.focus_structured or b.focus_excerpt or "").strip()
        if focus:
            lines.append("  focus:")
            lines.extend(f"    {ln}" for ln in focus.splitlines())
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


_WS_RE = re.compile(r"\s+")


def _norm_ws(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def _quote_ok(item: dict, session_text_norm: str) -> bool:
    quote = item.get("quote")
    if not isinstance(quote, str) or not quote.strip():
        return False
    return _norm_ws(quote) in session_text_norm


def _canonicalize(
    obj: Any, roster: identity_mod.Roster, session_text_norm: str
) -> dict[str, str] | None:
    """§4.1/§4.3: resolve one identity object through the ONE funnel.

    Returns the CANONICALIZED identity — ``{"ref": canonical}`` (a "张总" ref is
    rewritten to the roster canonical it resolved to; a ``new_entity`` that
    turns out to be a known identity is folded to its ``ref``) or
    ``{"new_entity": name}`` for a genuinely unknown name that appears verbatim
    in the session text. ``None`` = invalid (unknown ref / store-probing name).
    """
    if not isinstance(obj, dict):
        return None
    ref = obj.get("ref")
    if isinstance(ref, str) and ref.strip():
        res = identity_mod.resolve_identity(ref, roster)
        return {"ref": res.canonical} if res.matched else None
    new = obj.get("new_entity")
    if isinstance(new, str) and new.strip() and _norm_ws(new) in session_text_norm:
        res = identity_mod.resolve_identity(new, roster)
        if res.matched:
            return {"ref": res.canonical}  # "new" name is a known identity — fold
        return {"new_entity": new.strip()}
    return None


def gate_delta(
    raw: dict,
    *,
    roster: identity_mod.Roster,
    session_text: str,
    min_confidence: float,
    cooccurrence: bool = True,
) -> tuple[dict, int]:
    """Deterministic gates over the LLM's delta. Returns (clean, dropped).

    Identity fields pass through the ONE funnel (§4.3) and come out
    CANONICALIZED — a "张总" ref lands as the roster canonical, a known name
    posing as ``new_entity`` folds to its ``ref`` — so everything downstream
    (the parity eval, the future apply path) reads one identity space.
    """
    text_norm = _norm_ws(session_text)
    clean: dict[str, list[dict]] = {h: [] for h in _HEADS}
    dropped = 0

    def conf_ok(item: dict) -> bool:
        try:
            return float(item.get("confidence", 0.0)) >= min_confidence
        except (TypeError, ValueError):
            return False

    def ident(obj: Any) -> dict[str, str] | None:
        return _canonicalize(obj, roster, text_norm)

    for item in raw.get("entities") or []:
        who = ident(item) if isinstance(item, dict) else None
        if (
            isinstance(item, dict)
            and item.get("kind") in _ENTITY_KINDS
            and who is not None
            and _quote_ok(item, text_norm)
            and conf_ok(item)
        ):
            clean["entities"].append(
                {
                    **{k: v for k, v in item.items() if k not in ("ref", "new_entity")},
                    **who,
                    "ended": _norm_ended(item),
                }
            )
        else:
            dropped += 1

    for item in raw.get("assertions") or []:
        subject = ident(item.get("subject")) if isinstance(item, dict) else None
        if (
            isinstance(item, dict)
            and isinstance(item.get("text"), str)
            and item["text"].strip()
            and subject is not None
            and _quote_ok(item, text_norm)
            and conf_ok(item)
        ):
            clean["assertions"].append({**item, "subject": subject})
        else:
            dropped += 1

    for item in raw.get("relations") or []:
        src = ident(item.get("src")) if isinstance(item, dict) else None
        dst = ident(item.get("dst")) if isinstance(item, dict) else None
        if (
            isinstance(item, dict)
            and item.get("predicate") in _PREDICATES
            and src is not None
            and dst is not None
            and _quote_ok(item, text_norm)
            and conf_ok(item)
        ):
            clean["relations"].append(
                {
                    **item,
                    "src": src,
                    "dst": dst,
                    "polarity": _norm_polarity(item),
                    "ended": _norm_ended(item),
                }
            )
        else:
            dropped += 1

    for item in raw.get("events") or []:
        participants = item.get("participants") if isinstance(item, dict) else None
        resolved = [ident(p) for p in participants] if isinstance(participants, list) else None
        if (
            isinstance(item, dict)
            and isinstance(item.get("title"), str)
            and item["title"].strip()
            and resolved is not None
            and all(p is not None for p in resolved)
            and _quote_ok(item, text_norm)
            and conf_ok(item)
        ):
            clean["events"].append({**item, "participants": resolved})
        else:
            dropped += 1

    # ② 确定性共现 knows —— subsume legacy relation_extractor 的确定性腿：同一 session 出现的
    # 每对 person 互相 knows。live LLM 抽共现关系不稳（--real parity 实测 relations 漏 <legacy
    # 1.0），而共现是确定性、免费、高召回——补进 payload 保证 delta relations ⊇ legacy，退役无
    # 召回损失（LLM 只加富关系 reports_to/part_of/directed）。门控（默认 on）。
    if cooccurrence:
        persons = sorted(
            {c for e in clean["entities"] if e.get("kind") == "person"
             and (c := (e.get("ref") or e.get("new_entity")))}
        )
        have = {
            frozenset((
                r["src"].get("ref") or r["src"].get("new_entity") or "",
                r["dst"].get("ref") or r["dst"].get("new_entity") or "",
            ))
            for r in clean["relations"]
            if r.get("predicate") == "knows"
        }
        for i in range(len(persons)):
            for j in range(i + 1, len(persons)):
                if frozenset((persons[i], persons[j])) in have:
                    continue
                clean["relations"].append({
                    "src": {"ref": persons[i]}, "dst": {"ref": persons[j]},
                    "predicate": "knows", "label": "共现", "quote": "",
                    "confidence": 0.6, "polarity": "0", "ended": False, "cooccurrence": True,
                })

    return clean, dropped


def run_after_session(
    cfg: Any,
    *,
    session_id: str,
    start_time: datetime | None,
    end_time: datetime | None,
    llm_call: LlmCallFn | None = None,
) -> DeltaResult:
    """Consolidate one ended session into a shadow memory_delta row."""
    result = DeltaResult(session_id=session_id)
    if not getattr(cfg.memory_delta, "enabled", False):
        result.skipped_reason = "disabled"
        return result
    if start_time is None or end_time is None:
        result.skipped_reason = "no_window"
        return result

    max_blocks = int(getattr(cfg.memory_delta, "max_blocks", 120))
    try:
        with fts.cursor() as conn:
            # newest-first + limit → the session's most recent max_blocks;
            # reversed back to chronological order for the event log.
            blocks = list(
                reversed(tl_store.query_range(conn, start_time, end_time, limit=max_blocks))
            )
    except Exception:  # noqa: BLE001 — fail-open, never disturb the session-end chain
        logger.warning("memory_delta %s: block read failed", session_id, exc_info=True)
        result.skipped_reason = "block_read_failed"
        return result
    if not blocks:
        result.skipped_reason = "no_blocks"
        return result

    roster_entries = _load_roster(cfg)
    roster = identity_mod.Roster.build(roster_entries)
    session_text = _render_blocks(blocks)
    # prompt cache（§ 缓存自动生效）：system prompt 跨全部 session 恒定 → 挂 cache_control
    # 跨 772 次命中；roster 短窗内稳定（随抽取缓慢增长）→ 单独一块，前缀 system+roster
    # 一起缓存；session_events 每场变，独立成块不缓存。热路缓存把重放的输入 token 时间砍掉。
    _cache = {"type": "ephemeral"}
    messages = [
        {
            "role": "system",
            "content": [
                {"type": "text", "text": prompts.load("memory_delta.md"), "cache_control": _cache}
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": f"<roster>\n{_render_roster(roster_entries)}\n</roster>",
                    "cache_control": _cache,
                },
                {"type": "text", "text": f"<session_events>\n{session_text}\n</session_events>"},
            ],
        },
    ]

    events_mod.publish(STAGE, "stage_start", {"session_id": session_id})
    try:
        call = llm_call or _default_llm_call
        response = call(cfg, STAGE, messages)
        raw = _safe_json(llm_mod.extract_text(response))
    except Exception:  # noqa: BLE001 — LLM errors never disturb the chain
        logger.warning("memory_delta %s: LLM call failed", session_id, exc_info=True)
        events_mod.publish(STAGE, "stage_end", {"session_id": session_id, "written": 0})
        result.skipped_reason = "llm_failed"
        return result

    if not raw:
        events_mod.publish(STAGE, "stage_end", {"session_id": session_id, "written": 0})
        result.skipped_reason = "unparseable"
        return result

    clean, dropped = gate_delta(
        raw,
        roster=roster,
        session_text=session_text,
        min_confidence=float(getattr(cfg.memory_delta, "min_confidence", 0.5)),
        cooccurrence=bool(getattr(cfg.memory_delta, "cooccurrence_knows", True)),
    )
    try:
        with fts.cursor() as conn:
            delta_id = deltas_store.insert(
                conn,
                session_id=session_id,
                payload=clean,
                model=cfg.model_for(STAGE).model,
                dropped=dropped,
            )
    except Exception:  # noqa: BLE001
        logger.warning("memory_delta %s: persist failed", session_id, exc_info=True)
        events_mod.publish(STAGE, "stage_end", {"session_id": session_id, "written": 0})
        result.skipped_reason = "persist_failed"
        return result

    # §4.2 确定性 apply：shadow 存档之后（保住平价双跑），把 gated delta 铸成真实点/边。
    # 三段分离（LLM 提取 → shadow insert → 确定性 apply）；fail-open：apply 失败只记日志，
    # 绝不扰动 session 末链，也不影响已存的 shadow 行。
    if getattr(cfg.memory_delta, "apply_enabled", False):
        try:
            from . import delta_apply

            with fts.cursor() as conn:
                ar = delta_apply.apply_delta(conn, cfg, clean)
            logger.info(
                "memory_delta %s: applied (entities +%d/=%d, edges +%d~%d closed %d, events %d)",
                session_id,
                ar.entities_minted,
                ar.entities_seen,
                ar.edges_new,
                ar.edges_reinforced,
                ar.edges_closed,
                ar.events_minted,
            )
            result.applied = True
        except Exception:  # noqa: BLE001 — apply 失败绝不冒泡到 session 末链
            logger.warning("memory_delta %s: apply failed", session_id, exc_info=True)

    result.written = True
    result.delta_id = delta_id
    result.dropped = dropped
    result.counts = {h: len(clean[h]) for h in _HEADS}
    logger.info(
        "memory_delta %s: shadow row %d (%s; %d dropped by gates)",
        session_id,
        delta_id,
        ", ".join(f"{h}={n}" for h, n in result.counts.items()),
        dropped,
    )
    events_mod.publish(
        STAGE,
        "stage_end",
        {"session_id": session_id, "written": sum(result.counts.values()), "dropped": dropped},
    )
    return result

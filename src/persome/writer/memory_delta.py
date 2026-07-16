"""Windowed structured modeling: observations to auditable Points and Lines.

One LLM reading of each newly flushed session window, multiple structured heads —
``memory_delta {owner_alias_candidates, entities, assertions, relations, events}``.
The gated delta is
persisted first, then ``delta_apply`` deterministically mints or reinforces
evomem Points and relation Lines. Persist-before-apply makes the windowed path
auditable and retryable.

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
logs a warning and returns ``written=False``. The active watermark or terminal
marker remains unchanged so recovery can retry it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .. import prompts
from ..evomem import identity as identity_mod
from ..evomem import owner_identity
from ..logger import get
from ..store import fts
from ..store import memory_deltas as deltas_store
from ..store import owner_aliases as owner_alias_store
from ..store import relation_edges as edges_store
from ..timeline import store as tl_store
from . import llm as llm_mod

logger = get("persome.writer.memory_delta")

STAGE = "memory_delta"

_HEADS = ("owner_alias_candidates", "entities", "assertions", "relations", "events")
_ENTITY_KINDS = frozenset({"person", "org", "project", "artifact"})
_PREDICATES = frozenset(p.value for p in edges_store.Predicate)
_POLARITIES = frozenset({"+", "-", "0"})
_SELF_IDENTITY = "self"
_LOCAL_MODEL_URL_RE = re.compile(
    r"https?://(?:127\.0\.0\.1|localhost)(?::\d+)?/model(?:[/?#]|\b)", re.IGNORECASE
)


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
    applied: bool = False
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
    """Return known canonical identities and aliases as the resolution menu.

    Fail-open: any read error → empty roster (the delta then only carries
    ``new_entity`` items; nothing breaks).
    """
    try:
        limit = int(getattr(cfg.memory_delta, "roster_max", 60))
        return identity_mod.load_roster_entries(cfg, limit=limit)
    except Exception:  # noqa: BLE001 — roster is best-effort
        logger.debug("memory_delta: roster load failed, empty", exc_info=True)
        return [(_SELF_IDENTITY, [])]


def _render_roster(roster: list[tuple[str, list[str]]]) -> str:
    if not roster:
        return "(no known identities yet)"
    lines = []
    for canonical, aliases in roster:
        alias_part = f" (aliases: {', '.join(aliases)})" if aliases else ""
        owner_part = (
            " (memory owner; relation endpoint only)" if canonical == _SELF_IDENTITY else ""
        )
        lines.append(f"- {canonical}{alias_part}{owner_part}")
    return "\n".join(lines)


def _is_local_model_output(entry: str) -> bool:
    """Match Persome's rendered model, not a typed mention of its URL."""
    text = str(entry or "")
    return bool(_LOCAL_MODEL_URL_RE.search(text)) and (
        "Persome Personal Model" in text or "[Google Chrome]" in text or "[Chrome]" in text
    )


def _render_blocks(blocks: list[tl_store.TimelineBlock]) -> str:
    """Render session blocks as the ordered event log the prompt reads.

    Entries carry the timeline stage's verbatim-preserving normalization;
    ``focus_structured``/``focus_excerpt`` add the lossless backstop for chat
    text — the evidence gate checks quotes against THIS rendered text, so
    everything quotable must be in it.
    """
    parts: list[str] = []
    for b in blocks:
        raw_entries = list(b.entries or [])
        entries = [entry for entry in raw_entries if not _is_local_model_output(entry)]
        if not entries:
            continue
        window = f"[{b.start_time:%H:%M}-{b.end_time:%H:%M}] apps: {', '.join(b.apps_used)}"
        lines = [window]
        lines.extend(f"  - {e}" for e in entries)
        focus = (b.focus_structured or b.focus_excerpt or "").strip()
        if focus and len(entries) == len(raw_entries):
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

    Returns the canonical identity in ``{"ref": canonical}`` form (an honorific
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
        if not res.matched:
            return None
        assert res.canonical is not None
        return {"ref": res.canonical}
    new = obj.get("new_entity")
    if isinstance(new, str) and new.strip() and _norm_ws(new) in session_text_norm:
        res = identity_mod.resolve_identity(new, roster)
        if res.matched:
            assert res.canonical is not None
            return {"ref": res.canonical}  # "new" name is a known identity — fold
        return {"new_entity": new.strip()}
    return None


def gate_owner_alias_candidates(
    raw: dict,
    *,
    session_text: str,
) -> tuple[list[dict], int]:
    """Validate probabilistic owner recognition before it reaches the identity store."""
    text_norm = _norm_ws(session_text)
    clean: list[dict] = []
    dropped = 0
    for item in raw.get("owner_alias_candidates") or []:
        if not isinstance(item, dict):
            dropped += 1
            continue
        alias = owner_alias_store.clean_alias(str(item.get("alias") or ""))
        quote = str(item.get("quote") or "").strip()
        source_kind = str(item.get("source_kind") or "")
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        if (
            alias is None
            or source_kind
            not in {
                owner_alias_store.SOURCE_OWNED_ACCOUNT,
                owner_alias_store.SOURCE_EXPLICIT_SELF,
            }
            or confidence < owner_alias_store.MIN_CANDIDATE_CONFIDENCE
            or _norm_ws(quote) not in text_norm
            or identity_mod.norm(alias) not in identity_mod.norm(quote)
        ):
            dropped += 1
            continue
        clean.append(
            {
                "alias": alias,
                "source_kind": source_kind,
                "quote": quote,
                "confidence": confidence,
            }
        )
    return clean, dropped


def gate_delta(
    raw: dict,
    *,
    roster: identity_mod.Roster,
    session_text: str,
    min_confidence: float,
    cooccurrence: bool = True,
    owner_candidates: list[dict] | None = None,
    protected_owner_aliases: list[str] | None = None,
) -> tuple[dict, int]:
    """Deterministic gates over the LLM's delta. Returns (clean, dropped).

    Identity fields pass through the ONE funnel (§4.3) and come out
    canonicalized: an honorific reference lands on the roster canonical, a known name
    posing as ``new_entity`` folds to its ``ref`` — so everything downstream
    (the parity eval, the future apply path) reads one identity space.
    """
    text_norm = _norm_ws(session_text)
    clean: dict[str, list[dict]] = {h: [] for h in _HEADS}
    clean["owner_alias_candidates"] = list(owner_candidates or [])
    dropped = 0
    protected_owner_keys = {
        identity_mod.norm(alias) for alias in (protected_owner_aliases or []) if alias
    }

    def conf_ok(item: dict) -> bool:
        try:
            return float(item.get("confidence", 0.0)) >= min_confidence
        except (TypeError, ValueError):
            return False

    def ident(obj: Any) -> dict[str, str] | None:
        canonical = _canonicalize(obj, roster, text_norm)
        if canonical is None:
            return None
        unresolved = canonical.get("new_entity")
        if unresolved and identity_mod.norm(unresolved) in protected_owner_keys:
            return None
        return canonical

    for item in raw.get("entities") or []:
        who = ident(item) if isinstance(item, dict) else None
        if (
            isinstance(item, dict)
            and item.get("kind") in _ENTITY_KINDS
            and who is not None
            and who.get("ref") != _SELF_IDENTITY
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

    if cooccurrence:
        persons = sorted(
            {
                c
                for e in clean["entities"]
                if e.get("kind") == "person" and (c := (e.get("ref") or e.get("new_entity")))
            }
        )
        have = {
            frozenset(
                (
                    r["src"].get("ref") or r["src"].get("new_entity") or "",
                    r["dst"].get("ref") or r["dst"].get("new_entity") or "",
                )
            )
            for r in clean["relations"]
            if r.get("predicate") == "knows"
        }
        for i in range(len(persons)):
            for j in range(i + 1, len(persons)):
                if frozenset((persons[i], persons[j])) in have:
                    continue
                clean["relations"].append(
                    {
                        "src": {"ref": persons[i]},
                        "dst": {"ref": persons[j]},
                        "predicate": "knows",
                        "label": "co-occurs",
                        "quote": "",
                        "confidence": 0.6,
                        "polarity": "0",
                        "ended": False,
                        "cooccurrence": True,
                    }
                )

    return clean, dropped


def run_after_session(
    cfg: Any,
    *,
    session_id: str,
    start_time: datetime | None,
    end_time: datetime | None,
    llm_call: LlmCallFn | None = None,
    is_final: bool = True,
) -> DeltaResult:
    """Consolidate one bounded session window into a shadow memory_delta row."""
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
            # Session boundaries are event timestamps while timeline blocks are
            # minute-aligned. Strict overlap keeps the partial first/last minute
            # without admitting blocks that only touch either boundary.
            blocks = tl_store.query_overlapping_latest(
                conn,
                start_time,
                end_time,
                limit=max_blocks,
            )
    except Exception:  # noqa: BLE001 — fail-open, never disturb the writer chain
        logger.warning("memory_delta %s: block read failed", session_id, exc_info=True)
        result.skipped_reason = "block_read_failed"
        return result
    if not blocks:
        result.skipped_reason = "no_blocks"
        return result

    roster_entries = _load_roster(cfg)
    session_text = _render_blocks(blocks)
    if not session_text.strip():
        result.skipped_reason = "model_output_only"
        return result

    _cache = {"type": "ephemeral"}
    messages: list[dict[str, Any]] = [
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

    try:
        call = llm_call or _default_llm_call
        response = call(cfg, STAGE, messages)
        raw = _safe_json(llm_mod.extract_text(response))
    except Exception:  # noqa: BLE001 — LLM errors never disturb the chain
        logger.warning("memory_delta %s: LLM call failed", session_id, exc_info=True)
        result.skipped_reason = "llm_failed"
        return result

    if not raw:
        result.skipped_reason = "unparseable"
        return result

    owner_candidates, candidate_dropped = gate_owner_alias_candidates(
        raw, session_text=session_text
    )
    try:
        with fts.cursor() as conn:
            for candidate in owner_candidates:
                owner_identity.record_candidate(
                    conn,
                    alias=candidate["alias"],
                    session_id=session_id,
                    source_kind=candidate["source_kind"],
                    quote=candidate["quote"],
                    confidence=candidate["confidence"],
                )

            # Rebuild after recording: a second independent session can promote
            # the alias during this very window, so its relation endpoints must
            # canonicalize to self immediately.
            roster = identity_mod.load_roster(cfg)
            clean, gated_dropped = gate_delta(
                raw,
                roster=roster,
                session_text=session_text,
                min_confidence=float(getattr(cfg.memory_delta, "min_confidence", 0.5)),
                cooccurrence=bool(getattr(cfg.memory_delta, "cooccurrence_knows", True)),
                owner_candidates=owner_candidates,
                protected_owner_aliases=owner_identity.reserved_aliases(cfg, conn=conn),
            )
            dropped = candidate_dropped + gated_dropped
            delta_id = deltas_store.insert(
                conn,
                session_id=session_id,
                payload=clean,
                model=cfg.model_for(STAGE).model,
                dropped=dropped,
                apply_status=(
                    "pending"
                    if getattr(cfg.memory_delta, "apply_enabled", False)
                    else "not_requested"
                ),
                window_start=start_time,
                window_end=end_time,
                is_final=is_final,
            )
    except Exception:  # noqa: BLE001
        logger.warning("memory_delta %s: persist failed", session_id, exc_info=True)
        result.skipped_reason = "persist_failed"
        return result

    if getattr(cfg.memory_delta, "apply_enabled", False):
        try:
            from . import delta_apply

            with fts.cursor() as conn:
                ar = delta_apply.apply_delta(conn, cfg, clean)
                if ar.errors:
                    raise RuntimeError(f"delta apply reported {len(ar.errors)} item error(s)")
                deltas_store.set_apply_status(conn, delta_id, "applied")
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
        except Exception:  # noqa: BLE001 — apply
            with fts.cursor() as conn:
                deltas_store.set_apply_status(conn, delta_id, "failed")
            result.skipped_reason = "apply_failed"
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
    return result


def ensure_after_session(
    cfg: Any,
    *,
    session_id: str,
    start_time: datetime | None,
    end_time: datetime | None,
    llm_call: LlmCallFn | None = None,
) -> DeltaResult:
    """Run one terminal delta window once, with legacy-row compatibility."""
    return _ensure_window(
        cfg,
        session_id=session_id,
        start_time=start_time,
        end_time=end_time,
        llm_call=llm_call,
        is_final=True,
        allow_legacy=True,
    )


def ensure_active_window(
    cfg: Any,
    *,
    session_id: str,
    start_time: datetime | None,
    end_time: datetime | None,
    llm_call: LlmCallFn | None = None,
) -> DeltaResult:
    """Apply one active-session window exactly once."""
    return _ensure_window(
        cfg,
        session_id=session_id,
        start_time=start_time,
        end_time=end_time,
        llm_call=llm_call,
        is_final=False,
        allow_legacy=False,
    )


def _ensure_window(
    cfg: Any,
    *,
    session_id: str,
    start_time: datetime | None,
    end_time: datetime | None,
    llm_call: LlmCallFn | None,
    is_final: bool,
    allow_legacy: bool,
) -> DeltaResult:
    """Run one delta window once, retrying only a previously failed apply.

    Active and terminal modeling are retryable. A retry must not spend another
    LLM call or reinforce relation observations twice after a successful run.
    """
    if start_time is None or end_time is None or start_time >= end_time:
        return DeltaResult(session_id=session_id, skipped_reason="no_window")
    with fts.cursor() as conn:
        existing = deltas_store.latest_for_window(
            conn,
            session_id,
            window_start=start_time,
            window_end=end_time,
        )
        if existing is None and allow_legacy:
            legacy = deltas_store.latest_for_session(conn, session_id)
            if legacy is not None and not str(legacy["window_end"] or ""):
                existing = legacy
    if existing is None:
        return run_after_session(
            cfg,
            session_id=session_id,
            start_time=start_time,
            end_time=end_time,
            llm_call=llm_call,
            is_final=is_final,
        )

    result = DeltaResult(
        session_id=session_id,
        delta_id=int(existing["id"]),
        skipped_reason="already_processed",
    )
    status = str(existing["apply_status"] or "unknown")
    try:
        payload = json.loads(existing["payload"] or "{}")
    except (TypeError, ValueError):
        payload = {}
    result.counts = {head: len(payload.get(head) or []) for head in _HEADS}
    result.applied = status in {"applied", "unknown"}
    if not getattr(cfg.memory_delta, "apply_enabled", False) or status not in {
        "pending",
        "failed",
        "not_requested",
    }:
        return result

    try:
        from . import delta_apply

        with fts.cursor() as conn:
            applied = delta_apply.apply_delta(conn, cfg, payload)
            if applied.errors:
                raise RuntimeError(f"delta apply reported {len(applied.errors)} item error(s)")
            deltas_store.set_apply_status(conn, result.delta_id, "applied")
        result.applied = True
        result.skipped_reason = "resumed_apply"
    except Exception:  # noqa: BLE001 - leave retryable state for the next finalizer run
        with fts.cursor() as conn:
            deltas_store.set_apply_status(conn, result.delta_id, "failed")
        result.skipped_reason = "apply_failed"
        logger.warning("memory_delta %s: retry apply failed", session_id, exc_info=True)
    return result

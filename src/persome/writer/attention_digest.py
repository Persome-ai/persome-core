"Deterministic daily attention-dwell digest → durable user- fact."

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..evomem.engine import EvoMemory
from ..evomem.models import MemoryLayer, MemoryNode
from ..logger import get
from ..store import fts
from ..timeline import store as tl_store
from ..timeline.attention_trajectory import build_attention_trajectory

logger = get("persome.writer")

# ``user-`` is a VALID_PREFIXES entry AND a schema-miner fact prefix, so digest
# facts are eligible Face evidence without touching the miner's input gate.
ATTENTION_FILE = "user-attention.md"
_TAG = "attention dwell-digest"

# A surface must accumulate this much dwell in the day to enter the durable
# digest — well above the reducer's per-session 60s floor, because a durable
# fact should reflect sustained attention, not a long glance.
_MIN_DWELL_SECONDS = 5 * 60
_MAX_SURFACES = 8


@dataclass
class AttentionDigestResult:
    committed: bool = False
    summary: str = ""
    node_id: str = ""
    surfaces: list[str] = field(default_factory=list)
    skipped_reason: str = ""


def _format_dwell(seconds: int) -> str:
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"~{minutes}m"
    return f"~{seconds / 3600:.1f}h"


def _digest_prefix(day: str) -> str:
    return f"Attention digest {day}:"


def _render_digest(day: str, rows: list[tuple[str, int, str]]) -> str:
    parts = [f"{_format_dwell(sec)} {surface} [{rung}]" for surface, sec, rung in rows]
    return f"{_digest_prefix(day)} focus dwell by surface — " + "; ".join(parts)


def _aggregate(blocks: list[tl_store.TimelineBlock]) -> list[tuple[str, int, str]]:
    """Total dwell per surface across non-contiguous runs, longest first."""
    totals: dict[str, int] = {}
    rung_of: dict[str, str] = {}
    for span in build_attention_trajectory(blocks):
        if not span.surface:
            continue
        totals[span.surface] = totals.get(span.surface, 0) + span.dwell_seconds
        rung_of.setdefault(span.surface, span.rung)
    ranked = sorted(
        ((s, sec, rung_of[s]) for s, sec in totals.items() if sec >= _MIN_DWELL_SECONDS),
        key=lambda x: x[1],
        reverse=True,
    )
    return ranked[:_MAX_SURFACES]


def _find_today_digest(mem: EvoMemory, day: str) -> MemoryNode | None:
    for node in mem.store.all_latest():
        if node.file_name == ATTENTION_FILE and node.content.startswith(_digest_prefix(day)):
            return node
    return None


def run_attention_digest(
    cfg: Any,
    *,
    now: datetime | None = None,
    memory: EvoMemory | None = None,
) -> AttentionDigestResult:
    """Digest today's attention dwell into one durable ``user-attention.md`` fact.

    Deterministic — no LLM. Aggregates the day's per-block attention loci
    (Step-1 columns) into total dwell per surface and writes one ranked digest
    fact per calendar day. Re-runs within the same day supersede that day's
    digest instead of appending duplicates, so the tick can fire freely.

    A no-op (``skipped_reason='disabled'``) unless ``cfg.attention_digest_enabled``.
    ``now`` / ``memory`` are injectable seams for testing.
    """
    if not getattr(cfg, "attention_digest_enabled", False):
        return AttentionDigestResult(
            committed=False, summary="Attention digest is disabled", skipped_reason="disabled"
        )

    moment = now if now is not None else datetime.now().astimezone()
    day_start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    day = moment.date().isoformat()

    with fts.cursor() as conn:
        blocks = [b for b in tl_store.query_since(conn, day_start) if b.start_time <= moment]

    rows = _aggregate(blocks)
    if not rows:
        return AttentionDigestResult(
            committed=False,
            summary="No surface accumulated meaningful dwell today",
            skipped_reason="no dwell",
        )

    content = _render_digest(day, rows)
    surfaces = [surface for surface, _sec, _rung in rows]
    mem = memory if memory is not None else EvoMemory()

    existing = _find_today_digest(mem, day)
    if existing is None:
        node_id = mem.add_direct(
            content,
            layer=MemoryLayer.L2_FACT,
            file_name=ATTENTION_FILE,
            tags=_TAG,
        )
    elif existing.content == content:
        # Nothing changed since the last run — keep the chain quiet.
        return AttentionDigestResult(
            committed=False,
            summary="Digest unchanged since last run",
            node_id=existing.node_id,
            surfaces=surfaces,
            skipped_reason="unchanged",
        )
    else:
        from ..evomem.engine import _new_id

        stamp = datetime.now().astimezone()
        node = MemoryNode(
            node_id=_new_id(stamp),
            content=content,
            layer=MemoryLayer.L2_FACT,
            supersedes=[existing.node_id],
            is_latest=True,
            memory_at=stamp,
            gmt_created=stamp,
            user_id=mem.user_id,
            agent_id=mem.agent_id,
            file_name=ATTENTION_FILE,
            tags=_TAG,
            schema_summary=json.dumps({"day": day, "surfaces": surfaces}, ensure_ascii=False),
        )
        node_id = mem.commit_supersede(node, old_id=existing.node_id)

    logger.info("attention digest: wrote %s for %s (%d surfaces)", node_id, day, len(rows))
    return AttentionDigestResult(
        committed=True,
        summary=f"Attention digest for {day} covers {len(rows)} surface(s)",
        node_id=node_id,
        surfaces=surfaces,
    )

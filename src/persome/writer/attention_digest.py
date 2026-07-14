"Deterministic daily attention-dwell digest → durable user- fact."

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..evomem.engine import EvoMemory, _new_id
from ..evomem.models import MemoryLayer, MemoryNode
from ..logger import get
from ..store import fts
from ..timeline import store as tl_store

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
_MAX_SURFACE_CHARS = 240


@dataclass
class AttentionDigestResult:
    committed: bool = False
    summary: str = ""
    node_id: str = ""
    surfaces: list[str] = field(default_factory=list)
    skipped_reason: str = ""


@dataclass(frozen=True)
class _SurfaceDwell:
    surface: str
    dwell_seconds: int
    rung: str
    block_ids: tuple[str, ...]


def _format_dwell(seconds: int) -> str:
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"~{minutes}m"
    return f"~{seconds / 3600:.1f}h"


def _digest_prefix(day: str) -> str:
    return f"Attention digest {day}:"


def _render_digest(day: str, rows: list[_SurfaceDwell]) -> str:
    # A surface is untrusted screen-derived text. Keep it on one bounded line
    # and quote it as data before it reaches memory/schema prompts.
    parts = [
        f"{_format_dwell(row.dwell_seconds)} "
        f"{json.dumps(row.surface, ensure_ascii=False)} [{row.rung}]"
        for row in rows
    ]
    return f"{_digest_prefix(day)} focus dwell by surface — " + "; ".join(parts)


def _aware(value: datetime) -> datetime:
    """Interpret legacy naive timeline values with the runtime's local-time rule."""
    return value.astimezone() if value.tzinfo is None else value


def _clean_surface(value: str) -> str:
    return " ".join(str(value or "").split())[:_MAX_SURFACE_CHARS]


def _aggregate(
    blocks: list[tl_store.TimelineBlock],
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[_SurfaceDwell]:
    """Sum observed block duration per surface, clipped to the requested day.

    The trajectory view intentionally tolerates short gaps when drawing a
    continuous path. A durable dwell fact must not count those missing minutes
    as observed attention, so this aggregation sums block durations directly.
    """
    totals: dict[str, int] = {}
    rung_seconds: dict[str, dict[str, int]] = {}
    block_ids: dict[str, list[str]] = {}
    lower = _aware(since).astimezone(UTC) if since is not None else None
    upper = _aware(until).astimezone(UTC) if until is not None else None
    for block in blocks:
        surface = _clean_surface(block.attention_surface)
        if not surface:
            continue
        start = _aware(block.start_time).astimezone(UTC)
        end = _aware(block.end_time).astimezone(UTC)
        if lower is not None:
            start = max(start, lower)
        if upper is not None:
            end = min(end, upper)
        seconds = max(0, int((end - start).total_seconds()))
        if seconds == 0:
            continue
        totals[surface] = totals.get(surface, 0) + seconds
        rung = str(block.attention_rung or "unknown")
        by_rung = rung_seconds.setdefault(surface, {})
        by_rung[rung] = by_rung.get(rung, 0) + seconds
        ids = block_ids.setdefault(surface, [])
        if block.id and block.id not in ids:
            ids.append(block.id)

    ranked: list[_SurfaceDwell] = []
    for surface, seconds in totals.items():
        if seconds < _MIN_DWELL_SECONDS:
            continue
        rung = max(rung_seconds[surface].items(), key=lambda item: (item[1], item[0]))[0]
        ranked.append(
            _SurfaceDwell(
                surface=surface,
                dwell_seconds=seconds,
                rung=rung,
                block_ids=tuple(block_ids[surface]),
            )
        )
    ranked = sorted(
        ranked,
        key=lambda row: (-row.dwell_seconds, row.surface.casefold()),
    )
    return ranked[:_MAX_SURFACES]


def _find_today_digest(mem: EvoMemory, day: str) -> MemoryNode | None:
    for node in mem.store.all_latest():
        if node.file_name == ATTENTION_FILE and node.content.startswith(_digest_prefix(day)):
            return node
    return None


def _surface_metadata(rows: list[_SurfaceDwell]) -> list[dict[str, Any]]:
    return [
        {
            "surface": row.surface,
            "dwell_seconds": row.dwell_seconds,
            "rung": row.rung,
            "source_block_ids": list(row.block_ids),
        }
        for row in rows
    ]


def _digest_metadata(*, day: str, moment: datetime, rows: list[_SurfaceDwell]) -> str:
    return json.dumps(
        {
            "kind": "attention_digest",
            "day": day,
            "through": moment.isoformat(),
            "surfaces": _surface_metadata(rows),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _same_evidence(existing: MemoryNode, rows: list[_SurfaceDwell]) -> bool:
    """Whether the latest node already receipts the exact observed blocks.

    Rendered dwell is deliberately rounded for readability. Comparing only the
    content would lose a newly observed short block when both totals render as,
    for example, ``~5m``. The exact evidence payload, excluding the changing
    ``through`` timestamp, is the idempotence key.
    """
    try:
        metadata = json.loads(existing.schema_summary or "{}")
    except (TypeError, ValueError):
        return False
    return isinstance(metadata, dict) and metadata.get("surfaces") == _surface_metadata(rows)


def _new_digest_node(
    mem: EvoMemory,
    *,
    content: str,
    day: str,
    day_start: datetime,
    moment: datetime,
    rows: list[_SurfaceDwell],
    supersedes: list[str] | None = None,
) -> MemoryNode:
    stamp = moment.astimezone(UTC)
    return MemoryNode(
        node_id=_new_id(stamp),
        content=content,
        layer=MemoryLayer.L2_FACT,
        supersedes=list(supersedes or []),
        is_latest=True,
        memory_at=stamp,
        gmt_created=stamp,
        user_id=mem.user_id,
        agent_id=mem.agent_id,
        file_name=ATTENTION_FILE,
        tags=_TAG,
        confidence="high",
        occurred_at=moment.isoformat(),
        schema_summary=_digest_metadata(day=day, moment=moment, rows=rows),
        valid_from=day_start.isoformat(),
    )


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

    moment = _aware(now) if now is not None else datetime.now().astimezone()
    day_start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    day = moment.date().isoformat()

    with fts.cursor() as conn:
        blocks = tl_store.query_since(conn, day_start)

    rows = _aggregate(blocks, since=day_start, until=moment)
    if not rows:
        return AttentionDigestResult(
            committed=False,
            summary="No surface accumulated meaningful dwell today",
            skipped_reason="no dwell",
        )

    content = _render_digest(day, rows)
    surfaces = [row.surface for row in rows]
    mem = memory if memory is not None else EvoMemory()

    existing = _find_today_digest(mem, day)
    if existing is None:
        node = _new_digest_node(
            mem,
            content=content,
            day=day,
            day_start=day_start,
            moment=moment,
            rows=rows,
        )
        node_id = mem.commit_node(node)
    elif existing.content == content and _same_evidence(existing, rows):
        # Neither the readable digest nor its exact receipts changed — keep the
        # chain quiet even though the observation timestamp advanced.
        return AttentionDigestResult(
            committed=False,
            summary="Digest unchanged since last run",
            node_id=existing.node_id,
            surfaces=surfaces,
            skipped_reason="unchanged",
        )
    else:
        node = _new_digest_node(
            mem,
            content=content,
            day=day,
            day_start=day_start,
            moment=moment,
            rows=rows,
            supersedes=[existing.node_id],
        )
        node_id = mem.commit_supersede(
            node,
            old_id=existing.node_id,
            old_valid_until=moment.isoformat(),
        )

    logger.info("attention digest: wrote %s for %s (%d surfaces)", node_id, day, len(rows))
    return AttentionDigestResult(
        committed=True,
        summary=f"Attention digest for {day} covers {len(rows)} surface(s)",
        node_id=node_id,
        surfaces=surfaces,
    )

"""Attention trajectory — the timeline as a path through attention-space.

Step 2 of the attention-locus design (first principle #2: the locus *follows*
attention). Step 1 stamped every ``TimelineBlock`` with a dominant
``attention_surface`` / ``attention_rung`` / ``attention_confidence``. This layer
coalesces those per-block loci into an **attention trajectory**: contiguous runs
of the same surface with **dwell** (time spent), so "what did I attend to, and
for how long" is a queryable signal rather than "app X was frontmost".

Pure + derived: no new table — the trajectory is built on demand from the
Step-1 columns.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from . import store

# A same-surface run is broken when the next block starts more than this many
# seconds after the previous block ended, so an idle gap never inflates dwell.
# Slightly above the 1-min window length to tolerate normal block adjacency.
_DEFAULT_GAP_TOLERANCE = 120


@dataclass(frozen=True)
class AttentionSpan:
    """A contiguous run of attention on one surface."""

    surface: str
    rung: str  # representative rung of the run (its highest-confidence block)
    start: datetime
    end: datetime
    dwell_seconds: int
    block_count: int


def build_attention_trajectory(
    blocks: list[store.TimelineBlock],
    *,
    gap_tolerance_seconds: int = _DEFAULT_GAP_TOLERANCE,
) -> list[AttentionSpan]:
    """Coalesce per-block dominant loci into dwell spans. Pure.

    Blocks are sorted chronologically. Adjacent blocks sharing the same
    ``attention_surface`` merge into one span; a time gap larger than
    ``gap_tolerance_seconds`` breaks the run even on the same surface (idle gaps
    don't count as dwell). Non-contiguous same-surface runs stay separate spans.
    """
    if not blocks:
        return []
    ordered = sorted(blocks, key=lambda b: b.start_time)
    spans: list[AttentionSpan] = []
    run: list[store.TimelineBlock] = []

    def flush() -> None:
        if not run:
            return
        first, last = run[0], run[-1]
        rep = max(run, key=lambda b: b.attention_confidence)
        dwell = int((last.end_time - first.start_time).total_seconds())
        spans.append(
            AttentionSpan(
                surface=first.attention_surface,
                rung=rep.attention_rung,
                start=first.start_time,
                end=last.end_time,
                dwell_seconds=max(0, dwell),
                block_count=len(run),
            )
        )

    for b in ordered:
        if run:
            same = b.attention_surface == run[0].attention_surface
            gap = (b.start_time - run[-1].end_time).total_seconds()
            if same and gap <= gap_tolerance_seconds:
                run.append(b)
                continue
            flush()
            run = []
        run.append(b)
    flush()
    return spans


def trajectory_summary(spans: list[AttentionSpan], *, min_dwell_seconds: int = 60) -> dict:
    """Render spans into a JSON-able summary for MCP/API consumers.

    Returns ``by_dwell`` — surfaces ranked by TOTAL dwell (aggregated across
    non-contiguous runs), longest first, filtering surfaces under
    ``min_dwell_seconds`` — and ``trajectory`` — the chronological path of spans.
    Pure.
    """
    totals: dict[str, dict] = {}
    for s in spans:
        if not s.surface:
            continue
        entry = totals.setdefault(
            s.surface, {"surface": s.surface, "rung": s.rung, "dwell_seconds": 0}
        )
        entry["dwell_seconds"] += s.dwell_seconds
    by_dwell = sorted(
        (
            {
                "surface": e["surface"],
                "rung": e["rung"],
                "dwell_minutes": round(e["dwell_seconds"] / 60, 1),
            }
            for e in totals.values()
            if e["dwell_seconds"] >= min_dwell_seconds
        ),
        key=lambda x: x["dwell_minutes"],
        reverse=True,
    )
    trajectory = [
        {
            "surface": s.surface,
            "rung": s.rung,
            "start": s.start.isoformat(),
            "end": s.end.isoformat(),
            "dwell_minutes": round(s.dwell_seconds / 60, 1),
        }
        for s in spans
        if s.surface
    ]
    return {"by_dwell": by_dwell, "trajectory": trajectory}


def attention_trajectory(
    conn: sqlite3.Connection,
    since: datetime,
    until: datetime | None = None,
    *,
    gap_tolerance_seconds: int = _DEFAULT_GAP_TOLERANCE,
) -> list[AttentionSpan]:
    """Build the attention trajectory from ``timeline_blocks`` in ``[since, until]``.

    Thin DB wrapper over :func:`build_attention_trajectory` — reads the blocks
    (chronological) via the store and coalesces them. No new persistence.
    """
    blocks = store.query_since(conn, since)
    if until is not None:
        blocks = [b for b in blocks if b.start_time <= until]
    return build_attention_trajectory(blocks, gap_tolerance_seconds=gap_tolerance_seconds)

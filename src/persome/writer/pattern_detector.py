"""Pattern detector stage: detects repeated, evidence-backed user behavior.

Two-stage design:
1. Structured filtering: SQL queries extract high-frequency candidate patterns
   from timeline_blocks, captures, and event-daily entries.
2. LLM validation: LLM judges whether candidates are real habits vs coincidence,
   then writes confirmed behavioral memory to skills/skill-*.md.
"""

from __future__ import annotations

import functools
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..config import Config
from ..logger import get
from ..prompts import load as load_prompt
from ..session import store as session_store
from ..store import fts
from ..timeline import aggregator as timeline_aggregator
from ..timeline import store as timeline_store
from . import llm as llm_mod
from . import tools as tools_mod

logger = get("persome.writer")


@dataclass
class DetectResult:
    session_id: str
    committed: bool = False
    summary: str = ""
    written_ids: list[str] = field(default_factory=list)
    created_paths: list[str] = field(default_factory=list)
    iterations: int = 0
    skipped_reason: str = ""
    retryable: bool = False


# ─── public entry point ────────────────────────────────────────────────────


def detect_after_classify(
    cfg: Config,
    *,
    session_id: str,
    event_daily_path: str,
    session_start: datetime | None = None,
    session_end: datetime | None = None,
    stage_clock: datetime | None = None,
) -> DetectResult:
    """Pattern detection entry point for terminal session finalization."""
    if not cfg.pattern_detector.enabled:
        return DetectResult(session_id=session_id, skipped_reason="pattern detector disabled")

    # Determine window: from last pattern_detected_end to session_end
    window_start = session_start
    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, session_id)
        if row and row.pattern_detected_end:
            window_start = row.pattern_detected_end

    clock = _resolve_stage_clock(stage_clock or session_end)
    window_end = session_end or clock
    if window_start and window_start >= window_end:
        return DetectResult(session_id=session_id, skipped_reason="pattern window empty")

    lookback_start = window_end - timedelta(days=cfg.pattern_detector.lookback_days)
    # The lookback edge is an analysis horizon, not a causal cutoff. Aligning it
    # to a wall boundary avoids requiring expired raw captures for a harmless
    # sub-minute sliver days in the past; the current session end remains exact.
    timeline_start = timeline_store.floor_to_window(
        lookback_start,
        max(1, int(getattr(cfg.timeline, "window_minutes", 1))),
    )

    with fts.cursor() as conn:
        timeline_slice = timeline_aggregator.exact_timeline_slice(
            cfg,
            conn,
            start=timeline_start,
            end=window_end,
            require_closing_block=session_end is not None,
            # Structured frequency counts retain the full configured lookback.
            # Only the token-heavy raw-context mode keeps its historical 200
            # block prompt cap.
            limit=None if cfg.pattern_detector.structured_filter else 200,
        )
        if timeline_slice.skipped_reason:
            return DetectResult(
                session_id=session_id,
                skipped_reason=timeline_slice.skipped_reason,
                retryable=True,
            )
        if cfg.pattern_detector.structured_filter:
            candidates = _collect_candidates(
                conn,
                lookback_start=lookback_start,
                window_end=window_end,
                min_occurrences=cfg.pattern_detector.min_occurrences,
                timeline_blocks=timeline_slice.blocks,
            )
            if not candidates:
                return DetectResult(
                    session_id=session_id, skipped_reason="no pattern candidates found"
                )
            context = _assemble_context(
                candidates=candidates,
                event_daily_path=event_daily_path,
                session_id=session_id,
            )
        else:
            context = _assemble_raw_context(
                conn,
                lookback_start=lookback_start,
                window_end=window_end,
                event_daily_path=event_daily_path,
                session_id=session_id,
                timeline_blocks=timeline_slice.blocks,
            )

        result = _run_validation_loop(
            cfg,
            conn,
            session_id=session_id,
            context=context,
            stage_clock=clock,
        )

        if result.committed and session_end is not None:
            session_store.set_pattern_detected_end(conn, session_id, session_end)

        return result


# ─── stage 1: structured candidate filtering ───────────────────────────────


def _collect_candidates(
    conn: sqlite3.Connection,
    *,
    lookback_start: datetime,
    window_end: datetime,
    min_occurrences: int,
    timeline_blocks: list[timeline_store.TimelineBlock],
) -> dict[str, Any]:
    """Query the database for high-frequency candidate patterns.

    Returns a dict with five candidate categories:
      - app_sequences: repeated app combos from timeline_blocks
      - repeated_titles: repeated window titles from captures_fts
      - repeated_urls: repeated URLs from captures_fts
      - time_clusters: sessions clustered by hour-of-day + dominant app
      - event_memory: durable past activity entries with receipts
    """
    candidates: dict[str, Any] = {}

    # 1. App sequences from timeline_blocks
    app_seqs = _find_repeated_app_sequences(timeline_blocks, min_occurrences)
    if app_seqs:
        candidates["app_sequences"] = app_seqs

    # 2. Repeated window titles from captures_fts
    titles = _find_repeated_captures_field(
        conn, lookback_start, window_end, "window_title", min_occurrences
    )
    if titles:
        candidates["repeated_titles"] = titles

    # 3. Repeated URLs from captures_fts
    urls = _find_repeated_captures_field(conn, lookback_start, window_end, "url", min_occurrences)
    if urls:
        candidates["repeated_urls"] = urls

    # 4. Time-of-day clusters from sessions
    time_clusters = _find_time_clusters(conn, lookback_start, window_end, min_occurrences)
    if time_clusters:
        candidates["time_clusters"] = time_clusters

    # 5. Durable past activity memory supplies the semantic repetition signal.
    event_memory = _collect_event_memory(conn, lookback_start, window_end)
    if event_memory:
        candidates["event_memory"] = event_memory

    return candidates


def _collect_event_memory(
    conn: sqlite3.Connection, lookback_start: datetime, window_end: datetime
) -> list[dict[str, str]]:
    rows = conn.execute(
        """
        SELECT e.id,
               e.path,
               e.timestamp,
               e.content,
               COALESCE(NULLIF(m.occurred_at, ''), e.timestamp) AS occurrence_time
          FROM entries AS e
          LEFT JOIN entry_metadata AS m ON m.entry_id = e.id
         WHERE e.prefix = 'event'
           AND e.superseded = 0
           AND persome_epoch(COALESCE(NULLIF(m.occurred_at, ''), e.timestamp))
               >= persome_epoch(?)
           AND persome_epoch(COALESCE(NULLIF(m.occurred_at, ''), e.timestamp))
               < persome_epoch(?)
         ORDER BY persome_epoch(
                    COALESCE(NULLIF(m.occurred_at, ''), e.timestamp)
                  ) DESC
         LIMIT 20
        """,
        (lookback_start.isoformat(), window_end.isoformat()),
    ).fetchall()
    return [
        {
            "id": str(row["id"]),
            "path": str(row["path"]),
            "timestamp": str(row["occurrence_time"]),
            "summary": str(row["content"] or "")[:300],
            "receipt": f"⟨{row['id']}:{row['path']}⟩",
        }
        for row in rows
        if str(row["content"] or "").strip()
    ]


def _render_event_memory_lines(events: list[dict[str, str]]) -> list[str]:
    """Render durable activity memory as a candidate section shared by both modes."""
    if not events:
        return []
    lines = ["### Durable event memory (past activity with receipts)"]
    for event in events[:20]:
        lines.append(f"- [{event['timestamp']}] {event['summary']} receipt={event['receipt']}")
    lines.append("")
    return lines


def _find_repeated_app_sequences(
    blocks: list[timeline_store.TimelineBlock],
    min_occurrences: int,
) -> list[dict[str, Any]]:
    """Find app combinations that appear in multiple timeline blocks.

    Reads ``timeline_blocks.apps_used`` and treats each block as an unordered
    set of apps (sorted-tuple key). A 1-minute block where the user touched
    Mail+Slack+Cursor counts as one occurrence of the combo {Cursor, Mail,
    Slack}, regardless of switching order.

    This groups durable timeline co-occurrence rather than ordered raw-capture
    transitions.
    """
    if not blocks:
        return []

    # Count app-set occurrences (sorted tuple for dedup)
    seq_counts: Counter[tuple[str, ...]] = Counter()
    seq_examples: dict[tuple[str, ...], list[str]] = {}
    for block in blocks:
        apps = tuple(sorted(block.apps_used))
        if len(apps) < 2:
            continue
        seq_counts[apps] += 1
        seq_examples.setdefault(apps, []).append(block.start_time.isoformat())

    results: list[dict[str, Any]] = []
    for apps, count in seq_counts.most_common(20):
        if count < min_occurrences:
            break
        results.append(
            {
                "apps": list(apps),
                "count": count,
                "examples": seq_examples[apps][:5],
            }
        )
    return results


def _find_repeated_captures_field(
    conn: sqlite3.Connection,
    start: datetime,
    end: datetime,
    field: str,
    min_occurrences: int,
) -> list[dict[str, Any]]:
    """Find repeated non-empty values in a ``captures`` column.

    Window-bounded: caller passes both ``start`` and ``end`` because Pattern
    Detector only looks at the session-aligned slice between
    ``last_pattern_detected_end`` and ``session_end``.

    The bounded window keeps pattern detection aligned with the session slice.
    """
    rows = conn.execute(
        f"""
        SELECT {field}, timestamp, app_name
          FROM captures
         WHERE persome_epoch(timestamp) >= persome_epoch(?)
           AND persome_epoch(timestamp) < persome_epoch(?)
           AND {field} != ''
         ORDER BY persome_epoch(timestamp) ASC
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()

    if not rows:
        return []

    value_counts: Counter[str] = Counter()
    value_examples: dict[str, list[dict[str, str]]] = {}
    for r in rows:
        val = r[field]
        if not val or len(val) < 3:
            continue
        value_counts[val] += 1
        value_examples.setdefault(val, []).append(
            {"timestamp": r["timestamp"], "app": r["app_name"]}
        )

    results: list[dict[str, Any]] = []
    for val, count in value_counts.most_common(20):
        if count < min_occurrences:
            break
        results.append(
            {
                "value": val,
                "count": count,
                "examples": value_examples[val][:5],
            }
        )
    return results


def _find_time_clusters(
    conn: sqlite3.Connection,
    start: datetime,
    end: datetime,
    min_occurrences: int,
) -> list[dict[str, Any]]:
    """Find sessions that start at similar times with similar dominant apps."""
    rows = conn.execute(
        """
         SELECT start_time, end_time
          FROM sessions
         WHERE persome_epoch(start_time) >= persome_epoch(?)
           AND persome_epoch(start_time) < persome_epoch(?)
           AND status IN ('reduced', 'ended')
         ORDER BY persome_epoch(start_time) ASC
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()

    if not rows:
        return []

    # Group by hour-of-day + day-of-week
    hour_counts: Counter[tuple[int, int]] = Counter()  # (hour, weekday)
    hour_examples: dict[tuple[int, int], list[str]] = {}
    for r in rows:
        try:
            dt = datetime.fromisoformat(r["start_time"])
            key = (dt.hour, dt.weekday())
            hour_counts[key] += 1
            hour_examples.setdefault(key, []).append(r["start_time"])
        except (TypeError, ValueError):
            continue

    results: list[dict[str, Any]] = []
    for (hour, weekday), count in hour_counts.most_common(20):
        if count < min_occurrences:
            break
        weekday_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        results.append(
            {
                "hour": hour,
                "weekday": weekday_names[weekday],
                "count": count,
                "examples": hour_examples[(hour, weekday)][:5],
            }
        )
    return results


# ─── raw context assembly (burn-tokens mode) ───────────────────────────────


def _assemble_raw_context(
    conn: sqlite3.Connection,
    *,
    lookback_start: datetime,
    window_end: datetime,
    event_daily_path: str,
    session_id: str,
    timeline_blocks: list[timeline_store.TimelineBlock],
) -> str:
    """Feed raw timeline blocks and captures directly to the LLM.

    Used when cfg.pattern_detector.structured_filter is False.
    """
    parts: list[str] = [
        f"Source file: {event_daily_path}",
        f"Session being analyzed: {session_id}",
        "",
        f"## Raw activity data (last {(window_end - lookback_start).days + 1} days)",
        "",
        "Your job is to scan this raw data and detect any repetitive behavior "
        "evidence-backed behavior patterns. Look for:",
        "- App sequences that repeat across days",
        "- Window titles or URLs visited repeatedly",
        "- Sessions that consistently start at the same time",
        "- Any other routine or habit repeated across independent sessions",
        "",
    ]

    # Timeline blocks
    if timeline_blocks:
        parts.append("### Timeline blocks")
        for block in timeline_blocks:
            parts.append(
                f"- {block.start_time.isoformat()}–{block.end_time.isoformat()}"
                f" | apps: {', '.join(block.apps_used)}"
            )
            for e in block.entries[:2]:
                parts.append(f"  - {e}")
        parts.append("")

    # Captures
    caps = conn.execute(
        """
        SELECT timestamp, app_name, window_title, url
          FROM captures
         WHERE persome_epoch(timestamp) >= persome_epoch(?)
           AND persome_epoch(timestamp) < persome_epoch(?)
           AND (window_title != '' OR url != '')
         ORDER BY persome_epoch(timestamp) ASC
         LIMIT 200
        """,
        (lookback_start.isoformat(), window_end.isoformat()),
    ).fetchall()
    if caps:
        parts.append("### Captures")
        for c in caps:
            line = f"- {c['timestamp']} | {c['app_name']}"
            if c["window_title"]:
                line += f" | title: {c['window_title']}"
            if c["url"]:
                line += f" | url: {c['url']}"
            parts.append(line)
        parts.append("")

    events = _collect_event_memory(conn, lookback_start, window_end)
    parts.extend(_render_event_memory_lines(events))

    parts.append(
        "If you need to check existing workflow files for dedup, "
        "use `search_memory` or `read_memory`."
    )
    return "\n".join(parts)


# ─── context assembly ──────────────────────────────────────────────────────


def _assemble_context(
    *,
    candidates: dict[str, Any],
    event_daily_path: str,
    session_id: str,
) -> str:
    parts: list[str] = [
        f"Source file: {event_daily_path}",
        f"Session being analyzed: {session_id}",
        "",
        "## Candidate patterns extracted from recent data",
        "",
        "These are high-frequency signals detected by structured queries. "
        "Your job is to judge which ones represent real user habits worth "
        "recording as behavioral memory, vs coincidence or noise.",
        "",
    ]

    if "app_sequences" in candidates:
        parts.append("### Repeated app combinations (from timeline blocks)")
        for seq in candidates["app_sequences"]:
            parts.append(f"- Apps: {', '.join(seq['apps'])} — appeared {seq['count']} times")
            for ex in seq["examples"]:
                parts.append(f"  - at {ex}")
        parts.append("")

    if "repeated_titles" in candidates:
        parts.append("### Repeated window titles (from captures)")
        for item in candidates["repeated_titles"]:
            parts.append(f'- "{item["value"]}" — appeared {item["count"]} times')
        parts.append("")

    if "repeated_urls" in candidates:
        parts.append("### Repeated URLs (from captures)")
        for item in candidates["repeated_urls"]:
            parts.append(f"- {item['value']} — appeared {item['count']} times")
        parts.append("")

    if "time_clusters" in candidates:
        parts.append("### Sessions clustered by time of day")
        for cluster in candidates["time_clusters"]:
            parts.append(
                f"- {cluster['weekday']} around {cluster['hour']:02d}:00 — "
                f"{cluster['count']} sessions started in this slot"
            )
        parts.append("")

    if candidates.get("event_memory"):
        parts.extend(_render_event_memory_lines(candidates["event_memory"]))

    parts.append(
        "If you need to check existing workflow files for dedup, "
        "use `search_memory` or `read_memory`."
    )
    return "\n".join(parts)


# ─── stage 2: LLM validation via tool-call loop ────────────────────────────


def _run_validation_loop(
    cfg: Config,
    conn: sqlite3.Connection,
    *,
    session_id: str,
    context: str,
    stage_clock: datetime,
) -> DetectResult:
    system = load_prompt("pattern_detector.md")
    schema = load_prompt("schema.md")
    index = _render_index(conn)

    user_msg = (
        f"# Frozen stage clock\n\n{stage_clock.isoformat()}\n\n"
        f"# Schema\n\n{schema}\n\n"
        f"# Memory index\n\n{index}\n\n"
        f"# Pattern candidates\n\n{context}\n\n"
        f"Session being analyzed: {session_id}"
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]

    state = tools_mod.CommitState()
    iters = llm_mod.run_tool_loop(
        cfg,
        "pattern_detector",
        messages,
        tools=tools_mod.TOOL_SCHEMAS,
        dispatch_fn=functools.partial(
            tools_mod.dispatch,
            conn=conn,
            soft_limit_tokens=cfg.writer.soft_limit_tokens,
            state=state,
        ),
        valid_tool_names=tools_mod.TOOL_NAMES,
        state=state,
        max_iter=cfg.writer.max_tool_iterations,
        log_tag=f"pattern_detector {session_id}",
    )
    return DetectResult(
        session_id=session_id,
        committed=state.committed,
        summary=state.summary,
        written_ids=list(state.written_ids),
        created_paths=list(state.created_paths),
        iterations=iters,
    )


def _resolve_stage_clock(value: datetime | None) -> datetime:
    clock = value or datetime.now().astimezone()
    if clock.tzinfo is None:
        raise ValueError("pattern detector stage_clock must be timezone-aware")
    return clock


def _render_index(conn: sqlite3.Connection) -> str:
    active = fts.list_files(conn, include_dormant=False, include_archived=False)
    if not active:
        return "(no memory files yet — create them as needed)"
    lines = ["Active memory files:"]
    for f in active[:30]:
        lines.append(
            f"- {f.path}  # {f.description}  "
            f"(tags: {f.tags}; entries: {f.entry_count}; updated: {f.updated})"
        )
    return "\n".join(lines)

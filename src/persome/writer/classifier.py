"""Legacy classifier stage: event-daily → durable Markdown files.

Runs after the S2 reducer successfully appends a session summary to
``event-YYYY-MM-DD.md``. Reads that entry plus a small window of the
preceding entries of the same day, calls the ``classifier`` LLM stage,
and lets it drive the same tool-call loop the old routing stage used
(read_memory / search_memory / append / create / supersede / commit).

The prompt forbids writing back to ``event-*.md`` — event-daily is owned
by the reducer. With default ``memory_delta.apply_enabled=true`` this stage is
retired and returns a deliberate no-op; memory delta owns terminal Point/Line
formation. Disabling delta apply reactivates this compatibility writer.
"""

from __future__ import annotations

import functools
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..config import Config
from ..logger import get
from ..prompts import load as load_prompt
from ..session import store as session_store
from ..store import entries as entries_mod
from ..store import files as files_mod
from ..store import fts
from ..timeline import aggregator as timeline_aggregator
from ..timeline import store as timeline_store
from . import llm as llm_mod
from . import tools as tools_mod

logger = get("persome.writer")
_compaction_logger = get("persome.compaction")

_COMPLETED_SESSION_COUNT_KEY = "completed_session_count"

# How many trailing entries from yesterday's event-daily file to carry in as
# context. One day is deliberate: the classifier has retrieval tools
# (`search_memory` / `read_memory`) and should pull more on its own if a
# specific fact seems to need older grounding.
_PRIOR_DAY_ENTRIES = 8


@dataclass
class ClassifyResult:
    session_id: str
    committed: bool = False
    summary: str = ""
    written_ids: list[str] = field(default_factory=list)
    created_paths: list[str] = field(default_factory=list)
    iterations: int = 0
    skipped_reason: str = ""
    retryable: bool = False


def classify_window(
    cfg: Config,
    *,
    session_id: str,
    event_daily_path: str,
    start: datetime,
    end: datetime,
    evidence_end: datetime | None = None,
    require_closing_block: bool = False,
    include_prior_day: bool = False,
    stage_clock: datetime | None = None,
    on_event: llm_mod.OnEventFn | None = None,
) -> ClassifyResult:
    """Classify event-daily entries for ``session_id`` within ``[start, end)``.

    Used by two callers:
      * the 30-min classifier tick during an active session — classifies the
        window ``[classified_end or session_start, now)`` and advances
        ``classified_end`` on success.
      * the terminal classifier after session-end reduce — classifies the
        trailing window ``[classified_end or session_start, session_end)``.

    Only entries in event-daily tagged ``sid:<session_id>`` with a
    timestamp in the window count as focus entries; if none match the
    window, the tick is a silent no-op.
    """
    if not cfg.reducer.enabled:
        return ClassifyResult(session_id=session_id, skipped_reason="reducer disabled")
    if getattr(getattr(cfg, "memory_delta", None), "apply_enabled", False):
        return ClassifyResult(
            session_id=session_id, skipped_reason="classifier retired (delta apply)"
        )

    with fts.cursor() as conn:
        entries_mod.write_preset_files(conn)

        focus_entries = _focus_entries_in_range(
            event_daily_path=event_daily_path,
            session_id=session_id,
            start=start,
            end=end,
        )
        if not focus_entries:
            return ClassifyResult(
                session_id=session_id,
                skipped_reason="no session entries in window",
            )

        timeline_slice = timeline_aggregator.exact_timeline_slice(
            cfg,
            conn,
            start=start,
            end=evidence_end or end,
            require_closing_block=require_closing_block,
        )
        if timeline_slice.skipped_reason:
            return ClassifyResult(
                session_id=session_id,
                skipped_reason=timeline_slice.skipped_reason,
                retryable=True,
            )
        timeline_text = _render_timeline_blocks(timeline_slice.blocks)
        prior_day_text = _render_prior_day(start) if include_prior_day else ""

        context = _assemble_context(
            event_daily_path=event_daily_path,
            focus_entries=focus_entries,
            timeline_text=timeline_text,
            prior_day_text=prior_day_text,
        )

        return _run_tool_loop(
            cfg,
            conn,
            session_id=session_id,
            event_daily_path=event_daily_path,
            context=context,
            stage_clock=stage_clock,
            capture_evidence_bounds=tools_mod.CaptureEvidenceBounds(
                start=start,
                end=evidence_end or end,
            ),
            on_event=on_event,
        )


def classify_after_reduce(
    cfg: Config,
    *,
    session_id: str,
    event_daily_path: str,
    just_written_entry_id: str = "",
    session_start: datetime | None = None,
    session_end: datetime | None = None,
    window_start: datetime | None = None,
    stage_clock: datetime | None = None,
    processing_clock: datetime | None = None,
    on_event: llm_mod.OnEventFn | None = None,
) -> ClassifyResult:
    """Terminal-reduce classifier entry point.

    If ``window_start`` is provided (e.g. ``classified_end`` from the
    sessions table), classify only the trailing window
    ``[window_start, session_end)`` — the 30-min tick has already handled
    everything earlier in the session. Otherwise fall back to the whole
    session (behaves like the legacy callsite).
    """
    if not cfg.reducer.enabled:
        return ClassifyResult(session_id=session_id, skipped_reason="reducer disabled")
    if getattr(getattr(cfg, "memory_delta", None), "apply_enabled", False):
        return ClassifyResult(
            session_id=session_id, skipped_reason="classifier retired (delta apply)"
        )

    if session_start is None or session_end is None:
        # Legacy path: no time bounds available — best we can do is classify
        # every entry tagged with this session and hope for the best.
        result = _classify_untimed(
            cfg,
            session_id=session_id,
            event_daily_path=event_daily_path,
            just_written_entry_id=just_written_entry_id,
            stage_clock=stage_clock,
            on_event=on_event,
        )
        if result.committed:
            _check_and_trigger_compaction(cfg)
        return result

    effective_start = window_start or session_start
    if effective_start >= session_end:
        return ClassifyResult(
            session_id=session_id,
            skipped_reason="terminal window empty (already classified)",
        )
    # The session end is the logical "now" for relative language in its
    # evidence. Keep the independent processing clock only for locating legacy
    # event headings that were appended long after the session happened.
    logical_clock = _resolve_stage_clock(stage_clock or session_end)
    transaction_clock = _resolve_stage_clock(processing_clock)
    window_end = max(session_end, transaction_clock)
    result = classify_window(
        cfg,
        session_id=session_id,
        event_daily_path=event_daily_path,
        start=effective_start,
        end=window_end,
        evidence_end=session_end,
        require_closing_block=True,
        include_prior_day=True,
        stage_clock=logical_clock,
        on_event=on_event,
    )
    if result.committed:
        _check_and_trigger_compaction(cfg)
    return result


def _classify_untimed(
    cfg: Config,
    *,
    session_id: str,
    event_daily_path: str,
    just_written_entry_id: str,
    stage_clock: datetime | None = None,
    on_event: llm_mod.OnEventFn | None = None,
) -> ClassifyResult:
    with fts.cursor() as conn:
        entries_mod.write_preset_files(conn)
        focus_entries = _focus_entries(
            event_daily_path=event_daily_path,
            session_id=session_id,
            fallback_entry_id=just_written_entry_id,
        )
        if not focus_entries:
            return ClassifyResult(
                session_id=session_id,
                skipped_reason=f"no entries found in {event_daily_path}",
            )
        context = _assemble_context(
            event_daily_path=event_daily_path,
            focus_entries=focus_entries,
            timeline_text="",
            prior_day_text="",
        )
        return _run_tool_loop(
            cfg,
            conn,
            session_id=session_id,
            event_daily_path=event_daily_path,
            context=context,
            stage_clock=stage_clock,
            capture_evidence_bounds=None,
            on_event=on_event,
        )


def _focus_entries_in_range(
    *,
    event_daily_path: str,
    session_id: str,
    start: datetime,
    end: datetime,
) -> list[files_mod.ParsedEntry]:
    path = files_mod.memory_path(event_daily_path)
    if not path.exists():
        return []
    try:
        parsed = files_mod.read_file(path)
    except Exception:  # noqa: BLE001
        return []
    sid_tag = f"sid:{session_id}"
    matches: list[files_mod.ParsedEntry] = []
    for e in parsed.entries:
        if sid_tag not in e.tags:
            continue
        ts = _parse_entry_ts(e.occurred_at or e.timestamp)
        if ts is None:
            # Timestamp unparseable — keep it so the classifier sees it
            # rather than silently dropping a tagged entry.
            matches.append(e)
            continue
        ts_cmp = _align_tz(ts, start)
        start_cmp = start
        end_cmp = end
        if start_cmp <= ts_cmp < end_cmp:
            matches.append(e)
    return matches


def _align_tz(ts: datetime, ref: datetime) -> datetime:
    """Make an entry timestamp comparable while preserving its local-time contract.

    Markdown entry headings intentionally use offset-less local wall time. A
    capture-backed session may use UTC, so attaching the session's timezone to
    that naive heading would shift the entry by the local UTC offset. Resolve a
    naive value through the machine's historical local timezone instead.
    """
    if (ts.tzinfo is None) == (ref.tzinfo is None):
        return ts
    if ts.tzinfo is None and ref.tzinfo is not None:
        return ts.astimezone()
    return ts.astimezone().replace(tzinfo=None)


def _parse_entry_ts(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def _resolve_stage_clock(value: datetime | None) -> datetime:
    clock = value or datetime.now().astimezone()
    if clock.tzinfo is None:
        raise ValueError("classifier stage_clock must be timezone-aware")
    return clock


def _focus_entries(
    *,
    event_daily_path: str,
    session_id: str,
    fallback_entry_id: str,
) -> list[files_mod.ParsedEntry]:
    """Return every entry in today's event-daily tagged with this session.

    Falls back to ``[fallback_entry_id]`` (the single last-written entry) if
    the session tag is missing — keeps behaviour sane even if the tag
    convention shifts.
    """
    path = files_mod.memory_path(event_daily_path)
    if not path.exists():
        return []
    try:
        parsed = files_mod.read_file(path)
    except Exception:  # noqa: BLE001
        return []
    sid_tag = f"sid:{session_id}"
    matches = [e for e in parsed.entries if sid_tag in e.tags]
    if matches:
        return matches
    for e in parsed.entries:
        if e.id == fallback_entry_id:
            return [e]
    return [parsed.entries[-1]] if parsed.entries else []


def _render_timeline_blocks(blocks: list[timeline_store.TimelineBlock]) -> str:
    if not blocks:
        return "(no timeline blocks recorded for this session)"
    out: list[str] = []
    for block in blocks:
        s = block.start_time.strftime("%H:%M")
        e = block.end_time.strftime("%H:%M")
        entries = block.entries
        header = f"[{s}-{e}]"
        if not entries:
            out.append(f"{header} (no notable activity)")
            continue
        out.append(header)
        out.extend(f"  - {entry}" for entry in entries)
    return "\n".join(out)


def _render_prior_day(session_start: datetime) -> str:
    prior_date = (session_start - timedelta(days=1)).strftime("%Y-%m-%d")
    name = f"event-{prior_date}.md"
    path = files_mod.memory_path(name)
    if not path.exists():
        return ""
    try:
        parsed = files_mod.read_file(path)
    except Exception:  # noqa: BLE001
        return ""
    tail = parsed.entries[-_PRIOR_DAY_ENTRIES:]
    if not tail:
        return ""
    out: list[str] = [f"From {name} (last {len(tail)} entries):", ""]
    for e in tail:
        out.append(f"### [{e.timestamp}] {{id: {e.id}}}")
        body = e.body.strip()
        if body:
            out.append(body)
        out.append("")
    return "\n".join(out).strip()


def _assemble_context(
    *,
    event_daily_path: str,
    focus_entries: list[files_mod.ParsedEntry],
    timeline_text: str,
    prior_day_text: str,
) -> str:
    parts: list[str] = [f"Source file: {event_daily_path}", ""]
    parts.append("## Session entries (focus — classify these)")
    for e in focus_entries:
        parts.append(f"### [{e.timestamp}] {{id: {e.id}}}")
        body = e.body.strip()
        if body:
            parts.append(body)
        parts.append("")
    if timeline_text:
        parts.append("## Timeline blocks covering this session")
        parts.append(
            "These are the verbatim-preserving activity slices the reducer compressed. "
            "Use them to ground any durable fact you're considering writing — "
            "or to skip a fact that the compressed entry overstates."
        )
        parts.append("")
        parts.append(timeline_text)
        parts.append("")
    if prior_day_text:
        parts.append("## Preceding day (context, dedup anchor)")
        parts.append(prior_day_text)
        parts.append("")
    parts.append(
        "If you need earlier history or adjacent entity files, call "
        "`search_memory` or `read_memory` — don't guess."
    )
    return "\n".join(parts).strip()


def _render_index(conn: sqlite3.Connection) -> str:
    active = fts.list_files(conn, include_dormant=False, include_archived=False)
    if not active:
        return "(no non-event memory files yet — create them as needed)"
    # Classifier never touches event-*; show only the files it can
    # actually write to so it doesn't get tempted.
    filtered = [f for f in active if not f.path.startswith("event-")]
    if not filtered:
        return "(no non-event memory files yet — create them as needed)"
    lines = ["Active non-event memory files:"]
    for f in filtered[:30]:
        lines.append(
            f"- {f.path}  # {f.description}  "
            f"(tags: {f.tags}; entries: {f.entry_count}; updated: {f.updated})"
        )
    return "\n".join(lines)


def _render_entity_index(conn: sqlite3.Connection) -> str:
    """Build a rich preview of person-* and project-* entity files for the classifier prompt."""
    active = fts.list_files(conn, include_dormant=False, include_archived=False)
    entities = [f for f in active if f.path.startswith(("person-", "project-"))]
    if not entities:
        return ""
    lines: list[str] = []
    for f in entities[:20]:
        lines.append(f"\n### {f.path}  ({f.entry_count} entries, updated {f.updated})")
        lines.append(f"Description: {f.description}")
        p = files_mod.memory_path(f.path)
        if p.exists():
            parsed = files_mod.read_file(p)
            tail = [e for e in parsed.entries if not e.superseded_by][-2:]
            for e in tail:
                snippet = e.body[:150].replace("\n", " ")
                lines.append(f"  - [{e.timestamp}] {snippet}")
    return "\n".join(lines).strip()


def _run_tool_loop(
    cfg: Config,
    conn: sqlite3.Connection,
    *,
    session_id: str,
    event_daily_path: str,
    context: str,
    stage_clock: datetime | None,
    capture_evidence_bounds: tools_mod.CaptureEvidenceBounds | None,
    on_event: llm_mod.OnEventFn | None = None,
) -> ClassifyResult:
    system = load_prompt("classifier.md")
    schema = load_prompt("schema.md")
    index = _render_index(conn)
    entity_index = _render_entity_index(conn)
    entity_section = (
        f"# Known entities (person / project)\n\n{entity_index}\n\n" if entity_index else ""
    )

    strategy = (cfg.writer.contradiction_strategy or "abstract").strip().lower()
    if strategy == "supersede":
        contradiction_note = (
            "Contradiction strategy: **supersede** — when search_memory surfaces a "
            "contradicting old entry, prefer Path A (supersede the old entry with "
            "the new value). Only use abstraction when the temporal advantage is "
            "genuinely unclear."
        )
    else:
        contradiction_note = (
            "Contradiction strategy: **abstract** (default) — when search_memory "
            "surfaces a contradicting entry without a clear temporal advantage, "
            "prefer Path B: supersede both conflicting entries and append a "
            "higher-level rule tagged `abstracted-from:<id1>,<id2>`."
        )

    # Current date/time anchor (#532): without it the model can only guess the

    # FILENAME, which breaks across midnight or when an entry references an
    # earlier event — a wrong ``occurred_at`` then poisons the cross-domain
    # sweeper's ±25min behavior signature. Giving the model "today" lets it
    # resolve relative expressions to a correct ISO ``occurred_at``.
    now_anchor = _resolve_stage_clock(stage_clock).strftime("%Y-%m-%d %H:%M %A (%z)")
    user_msg = (
        f"# Current date/time\n\n"
        f'Now: {now_anchor}. Resolve any relative time phrase such as "last Friday "\n'
        f'or "last month" against this when writing an ISO `occurred_at`.\n\n'
        f"# Schema\n\n{schema}\n\n"
        f"# Memory index\n\n{index}\n\n"
        f"{entity_section}"
        f"# Event-daily context\n\n{context}\n\n"
        f"Source file (do NOT write to it): {event_daily_path}\n"
        f"Session being classified: {session_id}\n\n"
        f"{contradiction_note}"
    )

    # System prompt is the largest stable byte block per call — wrap in
    # list-of-blocks with an ephemeral cache_control marker so the
    # multi-round tool_loop reuses it across iterations and the next
    # classifier-tick reuses it across calls (5-minute TTL).
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        },
        {"role": "user", "content": user_msg},
    ]

    # Mark the last tool definition with cache_control so the tools-block
    # prefix (rendered before system per Anthropic spec) also caches.
    # Copy the module-level schema list to avoid cross-stage mutation.
    tools_with_cache: list[dict[str, Any]] = [dict(t) for t in tools_mod.CLASSIFIER_SCHEMAS]
    if tools_with_cache:
        tools_with_cache[-1] = {**tools_with_cache[-1], "cache_control": {"type": "ephemeral"}}

    state = tools_mod.CommitState()
    iters = llm_mod.run_tool_loop(
        cfg,
        "classifier",
        messages,
        tools=tools_with_cache,
        dispatch_fn=functools.partial(
            tools_mod.dispatch,
            conn=conn,
            soft_limit_tokens=cfg.writer.soft_limit_tokens,
            state=state,
            capture_evidence_bounds=capture_evidence_bounds,
        ),
        valid_tool_names=tools_mod.CLASSIFIER_TOOL_NAMES,
        state=state,
        max_iter=cfg.writer.max_tool_iterations,
        log_tag=f"classifier {session_id}",
        on_event=on_event,
    )
    return ClassifyResult(
        session_id=session_id,
        committed=state.committed,
        summary=state.summary,
        written_ids=list(state.written_ids),
        created_paths=list(state.created_paths),
        iterations=iters,
    )


def _check_and_trigger_compaction(cfg: Config) -> None:
    """Increment the completed-session counter; trigger compaction at cadence.

    Called after every successful classifier commit. The counter lives in
    ``session_store.system_state`` so it survives daemon restarts. When the
    counter hits a multiple of ``cfg.writer.consolidation_cadence``, a
    per-file ``compact.run_pending`` processes files flagged ``needs_compact``.
    """
    cadence = max(1, int(cfg.writer.consolidation_cadence))
    try:
        with fts.cursor() as conn:
            count = int(session_store.get_system_state(conn, _COMPLETED_SESSION_COUNT_KEY, "0")) + 1
            session_store.set_system_state(conn, _COMPLETED_SESSION_COUNT_KEY, str(count))
    except Exception as exc:  # noqa: BLE001
        _compaction_logger.warning("compaction counter update failed: %s", exc)
        return

    if count % cadence != 0:
        return

    _compaction_logger.info("compaction cadence reached (%d sessions) — triggering", count)
    try:
        _trigger_pending_compaction(cfg)
    except Exception as exc:  # noqa: BLE001
        _compaction_logger.warning("compaction trigger failed: %s", exc, exc_info=True)


def _trigger_pending_compaction(cfg: Config) -> None:
    """Run per-file compaction on files marked ``needs_compact``."""
    from . import compact as compact_mod

    with fts.cursor() as conn:
        compact_mod.run_pending(cfg, conn)

"""Dream stage: daily slow-thinking review + skill generation.

Two-stage design:
1. Structured analysis: SQL + Python extracts daily app stats, repeated app
   sequences, time-slot routines, and chat query-action pairs.
2. LLM Dream loop: validates macro-patterns, writes skills/skill-*.md and
   user-*.md via tool-call loop.

Fired once per day at 23:30 (before the 23:55 daily safety net).
"""

from __future__ import annotations

import functools
import json
import sqlite3
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from .. import events as events_mod
from .. import paths
from ..config import Config
from ..intent import store as intent_store
from ..intent.ontology import Intent
from ..logger import get
from ..prompts import load as load_prompt
from ..store import dream_runs as dream_runs_store
from ..store import fts
from . import book_chapters, book_page
from . import llm as llm_mod
from . import tools as tools_mod

logger = get("persome.writer")


@dataclass
class DreamResult:
    committed: bool = False
    summary: str = ""
    written_ids: list[str] = field(default_factory=list)
    created_paths: list[str] = field(default_factory=list)
    iterations: int = 0
    skipped_reason: str = ""


# ─── consolidation helpers ─────────────────────────────────────────────────

_LAST_RUN_FILE = "dream-last-run.json"


def _get_last_dream_run() -> datetime:
    """Read last-run timestamp. Returns (now − 24h) if file is missing or corrupt."""
    path = paths.root() / _LAST_RUN_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return datetime.fromisoformat(data["ts"])
    except (OSError, KeyError, ValueError):
        return datetime.now().astimezone() - timedelta(hours=24)


def _set_last_dream_run() -> None:
    path = paths.root() / _LAST_RUN_FILE
    path.write_text(json.dumps({"ts": datetime.now().astimezone().isoformat()}), encoding="utf-8")


def _new_classifier_entries(
    conn: sqlite3.Connection, since: datetime
) -> dict[str, list[dict[str, Any]]]:
    """Return entries added to non-event files since ``since``, grouped by path.

    Excludes event-*.md (owned by the reducer) and superseded entries.
    """
    rows = fts.recent(
        conn,
        since=since.isoformat(),
        limit=200,
        prefix_filter=[
            "user",
            "project",
            "tool",
            "topic",
            "person",
            "org",
            "skill",
        ],
        include_superseded=False,
    )
    by_path: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_path.setdefault(r.path, []).append(
            {
                "id": r.id,
                "timestamp": r.timestamp,
                "body_preview": r.content[:200],
            }
        )
    return by_path


# ─── public entry point ────────────────────────────────────────────────────


def run_dream(cfg: Config, *, on_event: llm_mod.OnEventFn | None = None) -> DreamResult:
    """Run the daily Dream stage. Returns DreamResult."""
    if not cfg.dream.enabled:
        return DreamResult(skipped_reason="dream disabled")

    lookback_start = datetime.now().astimezone() - timedelta(days=cfg.dream.lookback_days)
    last_run = _get_last_dream_run() if cfg.dream.consolidation_enabled else None

    with fts.cursor() as conn:
        # Stage 1: structured analysis
        app_stats = _daily_app_stats(conn, lookback_start)
        app_sequences = _mine_app_sequences(
            conn, lookback_start, cfg.dream.min_sequence_occurrences
        )
        routines = _detect_routines(conn, lookback_start)
        chat_pairs: list[dict[str, Any]] = []
        if cfg.dream.enable_chat_mining:
            chat_pairs = _mine_chat_pairs(cfg.dream.lookback_days, cfg.dream.max_chat_pairs)

        repeated_titles = _find_repeated_captures_field(
            conn, lookback_start, "window_title", cfg.dream.min_sequence_occurrences
        )
        repeated_urls = _find_repeated_captures_field(
            conn, lookback_start, "url", cfg.dream.min_sequence_occurrences
        )

        # Stage 1c: new classifier entries since last dream run
        new_entries: dict[str, list[dict[str, Any]]] = {}
        if last_run is not None:
            new_entries = _new_classifier_entries(conn, last_run)

        # Stage 1d: recognized intents from the unified stream over the lookback.
        # Dream mines macro behaviour; what the user repeatedly *intends* (recurring
        # meetings, reminders, chat-stated goals) is signal alongside passive activity.
        intents = intent_store.recent_intents(
            conn,
            start=lookback_start.isoformat(),
            end=datetime.now().astimezone().isoformat(),
        )

        context = _assemble_context(
            conn=conn,
            app_stats=app_stats,
            app_sequences=app_sequences,
            routines=routines,
            repeated_titles=repeated_titles,
            repeated_urls=repeated_urls,
            chat_pairs=chat_pairs,
            lookback_days=cfg.dream.lookback_days,
            new_entries=new_entries,
            intents=intents,
        )

        result = _run_dream_loop(cfg, conn, context=context, on_event=on_event)
        if result.committed:
            _set_last_dream_run()
        return result


# ─── stage 1: structured analysis ──────────────────────────────────────────


def _daily_app_stats(
    conn: sqlite3.Connection, lookback_start: datetime
) -> dict[str, dict[str, float]]:
    """Return {date_str: {app_name: minutes}} for the lookback window.

    Each timeline block is 1 minute wide, so block-count ≈ minutes.
    """
    rows = conn.execute(
        """
        SELECT start_time, apps_used, capture_count
          FROM timeline_blocks
         WHERE start_time >= ?
         ORDER BY start_time ASC
        """,
        (lookback_start.isoformat(),),
    ).fetchall()

    stats: dict[str, dict[str, float]] = {}
    for r in rows:
        day = r["start_time"][:10]  # YYYY-MM-DD
        apps = json.loads(r["apps_used"] or "[]")
        # Weight by capture_count so blocks with more captures count more
        weight = max(1, r["capture_count"] or 1)
        day_stats = stats.setdefault(day, {})
        for app in apps:
            day_stats[app] = day_stats.get(app, 0.0) + weight

    return stats


def _mine_app_sequences(
    conn: sqlite3.Connection,
    lookback_start: datetime,
    min_occurrences: int,
    max_length: int = 5,
) -> list[dict[str, Any]]:
    """Find repeated *ordered contiguous* app sequences from ``captures``.

    Reads ``captures.app_name`` ordered by timestamp, collapses consecutive
    duplicates, then enumerates contiguous subsequences of length 2..N.
    Useful for detecting transitions like Cursor → Slack → Mail.

    Related but DIFFERENT: ``pattern_detector._find_repeated_app_sequences``
    reads ``timeline_blocks.apps_used`` and treats each block as an
    *unordered set* of apps. The two answer different questions —
    transition vs co-occurrence — so they live side-by-side intentionally.
    If you change one, decide whether the other needs the same change.
    """
    rows = conn.execute(
        """
        SELECT timestamp, app_name
          FROM captures
         WHERE timestamp >= ?
           AND app_name != ''
         ORDER BY timestamp ASC
        """,
        (lookback_start.isoformat(),),
    ).fetchall()

    if not rows:
        return []

    # Build app sequence, collapsing consecutive duplicates
    seq: list[str] = []
    for r in rows:
        app = r["app_name"]
        if not seq or seq[-1] != app:
            seq.append(app)

    if len(seq) < 2:
        return []

    # Count all contiguous subsequences of length 2..max_length
    subseq_counts: Counter[tuple[str, ...]] = Counter()
    subseq_examples: dict[tuple[str, ...], list[str]] = {}
    for length in range(2, min(max_length + 1, len(seq) + 1)):
        for i in range(len(seq) - length + 1):
            sub = tuple(seq[i : i + length])
            subseq_counts[sub] += 1
            subseq_examples.setdefault(sub, []).append(rows[i]["timestamp"])

    results: list[dict[str, Any]] = []
    for sub, count in subseq_counts.most_common(20):
        if count < min_occurrences:
            break
        results.append(
            {
                "sequence": list(sub),
                "count": count,
                "examples": subseq_examples[sub][:5],
            }
        )
    return results


def _detect_routines(
    conn: sqlite3.Connection, lookback_start: datetime
) -> dict[str, list[dict[str, Any]]]:
    """Cluster common app combinations by time-of-day slot."""
    rows = conn.execute(
        """
        SELECT start_time, apps_used
          FROM timeline_blocks
         WHERE start_time >= ?
         ORDER BY start_time ASC
        """,
        (lookback_start.isoformat(),),
    ).fetchall()

    if not rows:
        return {}

    slots: dict[str, list[Any]] = {
        "morning (06-10)": [],
        "work (10-18)": [],
        "evening (18-22)": [],
        "night (22-06)": [],
    }
    for r in rows:
        try:
            dt = datetime.fromisoformat(r["start_time"])
            hour = dt.hour
            if 6 <= hour < 10:
                slot = "morning (06-10)"
            elif 10 <= hour < 18:
                slot = "work (10-18)"
            elif 18 <= hour < 22:
                slot = "evening (18-22)"
            else:
                slot = "night (22-06)"
            slots[slot].append(r)
        except (TypeError, ValueError):
            continue

    results: dict[str, list[dict[str, Any]]] = {}
    for slot_name, slot_rows in slots.items():
        if not slot_rows:
            continue
        combo_counts: Counter[tuple[str, ...]] = Counter()
        combo_examples: dict[tuple[str, ...], list[str]] = {}
        for r in slot_rows:
            apps = tuple(sorted(json.loads(r["apps_used"] or "[]")))
            if len(apps) < 1:
                continue
            combo_counts[apps] += 1
            combo_examples.setdefault(apps, []).append(r["start_time"])

        slot_results: list[dict[str, Any]] = []
        for apps, count in combo_counts.most_common(10):
            slot_results.append(
                {
                    "apps": list(apps),
                    "count": count,
                    "examples": combo_examples[apps][:5],
                }
            )
        if slot_results:
            results[slot_name] = slot_results

    return results


def _mine_chat_pairs(lookback_days: int, max_pairs: int) -> list[dict[str, Any]]:
    """Extract (user query → assistant action) pairs from recent chat history.

    The chat-history JSON schema is owned by another component and can drift
    silently. Wrap each per-pair extraction in a try/except and log a single
    warning summarizing how many pairs were skipped — better than returning
    an empty list and pretending there were no patterns.
    """
    history_dir = paths.root() / "chat-history"
    if not history_dir.exists():
        return []

    cutoff = datetime.now().astimezone() - timedelta(days=lookback_days)
    pairs: list[dict[str, Any]] = []
    skipped = 0

    for f in sorted(history_dir.glob("*.json"), reverse=True):
        # Parse date from filename: YYYYMMDD-HHMMSS.json
        try:
            file_date = datetime.strptime(f.stem, "%Y%m%d-%H%M%S").replace(
                tzinfo=datetime.now().astimezone().tzinfo
            )
        except ValueError:
            # Fallback: use mtime
            file_date = datetime.fromtimestamp(f.stat().st_mtime).astimezone()

        if file_date < cutoff:
            continue

        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            skipped += 1
            continue

        if not isinstance(data, list):
            skipped += 1
            continue

        for i in range(len(data) - 1):
            try:
                user_msg = data[i]
                assistant_msg = data[i + 1]
                if not isinstance(user_msg, dict) or not isinstance(assistant_msg, dict):
                    skipped += 1
                    continue
                if user_msg.get("role") != "user":
                    continue
                if assistant_msg.get("role") != "assistant":
                    continue
                tool_calls = assistant_msg.get("tool_calls")
                if not tool_calls:
                    continue
                actions = []
                for tc in tool_calls:
                    func = tc.get("function", {}) if isinstance(tc, dict) else {}
                    actions.append(
                        {
                            "tool": func.get("name", "unknown"),
                            "args_summary": _summarize_args(func.get("arguments", "")),
                        }
                    )
                pairs.append(
                    {
                        "query": str(user_msg.get("content", ""))[:200],
                        "actions": actions,
                        "date": file_date.strftime("%Y-%m-%d"),
                        "source": f.name,
                    }
                )
            except (AttributeError, TypeError, KeyError):
                # Schema drift — any unexpected shape inside a single pair.
                # Skip the pair, keep going; we report the total at the end.
                skipped += 1
                continue

    if skipped:
        logger.warning(
            "dream._mine_chat_pairs: skipped %d entries due to schema mismatch "
            "or unreadable files (kept %d pairs)",
            skipped,
            len(pairs),
        )

    return pairs[:max_pairs]


def _find_repeated_captures_field(
    conn: sqlite3.Connection,
    start: datetime,
    field: str,
    min_occurrences: int,
) -> list[dict[str, Any]]:
    """Find repeated non-empty values in a ``captures`` column.

    Open-ended forward over the full lookback (no ``end`` bound) because
    Dream runs once-per-day on the lookback_days window and wants every
    capture since ``start``.

    Related: ``pattern_detector._find_repeated_captures_field`` does the
    same counting but takes both ``start`` and ``end`` to scope to the
    session-aligned slice. Keep their output shape identical so prompt
    rendering helpers can be shared.
    """
    if field not in {"window_title", "url", "app_name"}:
        raise ValueError(f"invalid field for repeated capture search: {field}")
    rows = conn.execute(
        f"""
        SELECT {field}, timestamp, app_name
          FROM captures
         WHERE timestamp >= ?
           AND {field} != ''
         ORDER BY timestamp ASC
        """,
        (start.isoformat(),),
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


def _summarize_args(args_raw: str) -> str:
    """Summarize tool arguments for brevity."""
    if not args_raw:
        return ""
    try:
        args = json.loads(args_raw)
    except json.JSONDecodeError:
        return str(args_raw)[:80]
    # Keep only path/query if present
    parts: list[str] = []
    if "path" in args:
        parts.append(f"path={args['path']}")
    if "query" in args:
        parts.append(f"query={args['query'][:40]}")
    if not parts:
        # Truncate full args
        s = json.dumps(args, ensure_ascii=False)
        return s[:80] + "..." if len(s) > 80 else s
    return ", ".join(parts)


# ─── context assembly ──────────────────────────────────────────────────────


def _list_skill_files(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return all skills/skill-*.md files with entry count and latest entry preview."""
    rows = conn.execute(
        """
        SELECT f.path, f.description, f.entry_count,
               f.updated AS last_ts,
               (SELECT e.content FROM entries e
                WHERE e.path = f.path AND e.superseded = 0
                ORDER BY e.timestamp DESC LIMIT 1) AS latest_content
        FROM files f
        WHERE f.path LIKE 'skills/skill-%.md'
          AND f.status != 'archived'
        ORDER BY f.updated DESC
        """
    ).fetchall()
    return [
        {
            "path": r["path"],
            "description": r["description"] or "",
            "entry_count": r["entry_count"] or 0,
            "last_ts": r["last_ts"] or "",
            "preview": (r["latest_content"] or "")[:300],
        }
        for r in rows
    ]


def _assemble_context(
    *,
    conn: sqlite3.Connection,
    app_stats: dict[str, dict[str, float]],
    app_sequences: list[dict[str, Any]],
    routines: dict[str, list[dict[str, Any]]],
    repeated_titles: list[dict[str, Any]],
    repeated_urls: list[dict[str, Any]],
    chat_pairs: list[dict[str, Any]],
    lookback_days: int,
    new_entries: dict[str, list[dict[str, Any]]] | None = None,
    intents: list[Intent] | None = None,
) -> str:
    """Render Stage-1 output as a flat candidate list with stable IDs.

    The LLM picks IDs from this list and calls ``drill_*`` tools to fetch
    raw rows before deciding what to write. We deliberately do NOT include
    pre-digested aggregates beyond what's needed to rank a candidate — the
    drill tools are the source of truth for content.
    """
    skills = _list_skill_files(conn)

    parts: list[str] = []

    # Phase 0 section: existing skill files for review and improvement
    if skills:
        parts += [
            "## Existing skill files — Phase 0 review",
            "",
            "Review each skill below before processing Stage-1 candidates.",
            "For skills with stage: draft — evaluate executability and promote if ready.",
            "For skills with stage: skill-candidate — check if steps can be improved.",
            "",
        ]
        for s in skills:
            parts.append(f"  {s['path']}  ({s['entry_count']} entries, last: {s['last_ts'][:10]})")
            if s["description"]:
                parts.append(f"  {s['description']}")
            if s["preview"]:
                short = s["preview"][:200].replace("\n", " ")
                parts.append(f"  Preview: {short}")
            parts.append("")
        parts += ["---", ""]

    parts += [
        f"## Stage-1 candidates (last {lookback_days} days)",
        "",
        "Pick the 3–5 most promising IDs and drill them before writing.",
        "",
        "Candidates (drill before deciding):",
        "",
    ]

    has_any = False

    # T01..  daily-task: repeated window titles (one form/sheet/page seen on N days)
    for i, item in enumerate(repeated_titles[:15], start=1):
        title = str(item["value"])[:80]
        count = item["count"]
        ex = item.get("examples") or []
        app = ""
        if ex and isinstance(ex[0], dict):
            app = str(ex[0].get("app") or "")
        suffix = f", app={app}" if app else ""
        parts.append(f'T{i:02d}  daily-task   "{title}"  {count}× seen{suffix}')
        parts.append(f'     → drill_window(title="{title}", since_days={lookback_days})')
        has_any = True
    if repeated_titles[:15]:
        parts.append("")

    # U01..  routine-url
    for i, item in enumerate(repeated_urls[:10], start=1):
        url = str(item["value"])[:120]
        count = item["count"]
        parts.append(f"U{i:02d}  routine-url  {url}  {count}× seen")
        parts.append(f'     → drill_window(url="{url}", since_days={lookback_days})')
        has_any = True
    if repeated_urls[:10]:
        parts.append("")

    # S01..  app-seq
    for i, seq in enumerate(app_sequences[:10], start=1):
        seq_str = " → ".join(seq["sequence"])
        parts.append(f"S{i:02d}  app-seq      {seq_str}  {seq['count']}×")
        has_any = True
    if app_sequences[:10]:
        parts.append("")

    # R01..  routine (time-slot)
    r_idx = 1
    for slot_name, combos in routines.items():
        for combo in combos[:2]:
            apps_str = ", ".join(combo["apps"])
            ex = combo.get("examples") or []
            sample_date = ""
            if ex:
                first = ex[0]
                if isinstance(first, str):
                    sample_date = first[:10]
            parts.append(
                f"R{r_idx:02d}  routine      {slot_name}  {apps_str}  {combo['count']}× blocks"
            )
            if sample_date:
                parts.append(f'     → drill_timeline(date="{sample_date}")')
            r_idx += 1
            has_any = True
            if r_idx > 6:
                break
        if r_idx > 6:
            break
    if r_idx > 1:
        parts.append("")

    # C01..  chat-pair
    seen_queries: set[str] = set()
    c_idx = 1
    for pair in chat_pairs[:30]:
        q = str(pair.get("query", ""))
        if not q or q in seen_queries:
            continue
        seen_queries.add(q)
        actions = ", ".join(a.get("tool", "") for a in pair.get("actions", []))
        source = str(pair.get("source", ""))
        parts.append(
            f'C{c_idx:02d}  chat-pair    "{q[:80]}" → {actions}  on {pair.get("date", "?")}'
        )
        if source:
            parts.append(f'     → drill_chat(file="{source}")')
        c_idx += 1
        has_any = True
        if c_idx > 11:
            break
    if c_idx > 1:
        parts.append("")

    # I01..  intent (from the unified intent stream)
    for i, it in enumerate((intents or [])[:15], start=1):
        text = str(it.payload.get("text") or it.rationale or "")[:80]
        parts.append(f'I{i:02d}  intent       [{it.kind}] "{text}"  (scope={it.scope})')
        has_any = True
    if intents:
        parts.append("")

    if not has_any:
        parts.append("(no candidates above threshold this run)")
        parts.append("")

    # Compact footer: total days covered + latest-day top-apps for context
    if app_stats:
        parts.append("---")
        parts.append(f"Days covered: {len(app_stats)}")
        latest_day = max(app_stats.keys())
        latest = app_stats[latest_day]
        top3 = sorted(latest.items(), key=lambda x: x[1], reverse=True)[:3]
        parts.append(f"Latest day ({latest_day}): " + ", ".join(f"{a}={m:.0f}m" for a, m in top3))

    # Stage 1c: memory consolidation section (omitted when empty)
    if new_entries:
        total_entries = sum(len(v) for v in new_entries.values())
        parts.append("")
        parts.append(
            f"## Memory updates since last dream ({len(new_entries)} files, {total_entries} new entries)"
        )
        parts.append("")
        for path_str, entries in sorted(new_entries.items()):
            parts.append(f"{path_str}  ({len(entries)} new)")
            for e in entries[:10]:
                ts_short = (e["timestamp"] or "")[:16]
                preview = e["body_preview"].replace("\n", " ")[:120]
                parts.append(f"  [{ts_short}]  {preview}")
            if len(entries) > 10:
                parts.append(f"  … and {len(entries) - 10} more")
            parts.append("")

    return "\n".join(parts)


def _find_consecutive_patterns(
    app_stats: dict[str, dict[str, float]],
    min_consecutive_days: int,
    min_daily_hours: float,
) -> list[dict[str, Any]]:
    """Find apps that meet the consecutive-day + daily-hours threshold."""
    # Invert stats: {app: [(date_str, minutes)]}
    app_days: dict[str, list[tuple[str, float]]] = {}
    for day, day_stats in app_stats.items():
        for app, mins in day_stats.items():
            app_days.setdefault(app, []).append((day, mins))

    results: list[dict[str, Any]] = []
    for app, days in app_days.items():
        # Sort by date
        days_sorted = sorted(days, key=lambda x: x[0])
        # Find consecutive runs where each day meets min_daily_hours
        run_start = 0
        for i in range(len(days_sorted)):
            if i > 0:
                prev_date = date.fromisoformat(days_sorted[i - 1][0])
                curr_date = date.fromisoformat(days_sorted[i][0])
                gap = (curr_date - prev_date).days
                hours = days_sorted[i][1] / 60.0
                if gap != 1 or hours < min_daily_hours:
                    # Run broken
                    run_len = i - run_start
                    if run_len >= min_consecutive_days:
                        run_days = days_sorted[run_start:i]
                        avg_hours = sum(m for _, m in run_days) / len(run_days) / 60.0
                        results.append(
                            {
                                "app": app,
                                "days": run_len,
                                "avg_hours": avg_hours,
                                "latest_day": run_days[-1][0],
                            }
                        )
                    run_start = i
            # Also check if current day itself meets threshold
            if days_sorted[i][1] / 60.0 < min_daily_hours:
                run_start = i + 1

        # Check trailing run
        if run_start < len(days_sorted):
            run_len = len(days_sorted) - run_start
            if run_len >= min_consecutive_days:
                run_days = days_sorted[run_start:]
                avg_hours = sum(m for _, m in run_days) / len(run_days) / 60.0
                results.append(
                    {
                        "app": app,
                        "days": run_len,
                        "avg_hours": avg_hours,
                        "latest_day": run_days[-1][0],
                    }
                )

    # Sort by days descending
    results.sort(key=lambda x: x["days"], reverse=True)
    return results


# ─── stage 2: LLM dream loop ───────────────────────────────────────────────


def _run_dream_loop(
    cfg: Config,
    conn: sqlite3.Connection,
    *,
    context: str,
    on_event: llm_mod.OnEventFn | None = None,
) -> DreamResult:
    system = load_prompt("dream.md")
    schema = load_prompt("schema.md")
    index = _render_index(conn)

    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    user_msg = (
        f"# Schema\n\n{schema}\n\n"
        f"# Memory index\n\n{index}\n\n"
        f"# Daily analysis for {today}\n\n{context}\n\n"
        f"Today: {today}"
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]

    state = tools_mod.CommitState()
    iters = llm_mod.run_tool_loop(
        cfg,
        "dream",
        messages,
        tools=tools_mod.DREAM_SCHEMAS,
        dispatch_fn=functools.partial(
            tools_mod.dispatch_dream,
            conn=conn,
            soft_limit_tokens=cfg.writer.soft_limit_tokens,
            state=state,
        ),
        valid_tool_names=tools_mod.DREAM_TOOL_NAMES,
        state=state,
        max_iter=cfg.dream.max_tool_iterations,
        on_event=on_event,
    )
    return DreamResult(
        committed=state.committed,
        summary=state.summary,
        written_ids=list(state.written_ids),
        created_paths=list(state.created_paths),
        iterations=iters,
        skipped_reason="" if state.committed else "loop_exhausted",
    )


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


# ─── recording wrapper ─────────────────────────────────────────────────────
#
# Wraps ``run_dream`` so every LLM event is both republished on the global
# events bus (so existing SSE subscribers keep working) AND persisted to the
# ``dream_runs`` / ``dream_events`` tables. UI uses the SSE stream for live
# updates and the tables for history.

_dream_lock = threading.Lock()


class DreamAlreadyRunningError(RuntimeError):
    """Raised when a second dream is triggered while one is already in flight."""


def try_reserve_dream_run() -> bool:
    """Atomically reserve the single-run slot. Returns True if the caller
    now holds the reservation.

    Callers that proceed to run dream MUST use :func:`run_reserved_dream`,
    which releases the reservation when finished (success or failure).
    Callers that abandon a successful reservation (e.g. background task
    spawn failed) MUST call :func:`release_dream_reservation` to unblock
    the next attempt.
    """
    return _dream_lock.acquire(blocking=False)


def release_dream_reservation() -> None:
    """Release a reservation taken via :func:`try_reserve_dream_run` but
    never used. Calling this without holding the reservation is a bug
    (Python's :class:`threading.Lock` raises ``RuntimeError``).
    """
    _dream_lock.release()


def run_reserved_dream(cfg: Config, *, trigger: str) -> tuple[int, DreamResult]:
    """Run dream while persisting an audit trail. The caller MUST already
    hold the reservation (got ``True`` back from
    :func:`try_reserve_dream_run`). Releases the reservation in
    ``finally``, including on exceptions.

    *trigger* is recorded verbatim (``manual`` / ``daily-tick``) so the
    UI can distinguish user-initiated runs from the daily scheduled one.
    """
    try:
        with fts.cursor() as conn:
            run_id = dream_runs_store.start_run(conn, trigger=trigger)

        # The LLM tool-loop calls on_event synchronously from a worker thread.
        # Keep the body short — SQLite inserts are ~sub-ms locally, but if this
        # ever shows up in dream wall-time we can switch to a queue + background
        # writer thread.
        def _on_event(event_type: str, payload: dict[str, Any]) -> None:
            enriched = {"run_id": run_id, **payload}
            events_mod.publish("dream", event_type, enriched)
            try:
                with fts.cursor() as conn:
                    dream_runs_store.append_event(conn, run_id, event_type, payload)
            except Exception:  # noqa: BLE001
                # A failed event-row write must not abort the dream loop.
                logger.exception("dream_runs: append_event failed (run=%s)", run_id)

        events_mod.publish("dream", "stage_start", {"run_id": run_id, "trigger": trigger})
        try:
            result = run_dream(cfg, on_event=_on_event)
        except Exception as exc:
            with fts.cursor() as conn:
                dream_runs_store.fail_run(conn, run_id, error=str(exc))
            events_mod.publish(
                "dream",
                "stage_end",
                {"run_id": run_id, "status": "failed", "error": str(exc)},
            )
            raise

        with fts.cursor() as conn:
            dream_runs_store.end_run(
                conn,
                run_id,
                committed=result.committed,
                summary=result.summary,
                written_ids=list(result.written_ids),
                created_paths=list(result.created_paths),
                iterations=result.iterations,
                skipped_reason=result.skipped_reason,
            )
        events_mod.publish(
            "dream",
            "stage_end",
            {
                "run_id": run_id,
                "status": "committed" if result.committed else "skipped",
                "summary": result.summary,
                "written": len(result.written_ids),
                "iterations": result.iterations,
                "skipped_reason": result.skipped_reason,
            },
        )

        # Book-page sub-step: after the dream completes, write the day's
        # literary pages. It reuses the dream run's event stream (_on_event →
        # SSE + dream_events audit), runs for the dream's target day (today,
        # local), and is fully fault-tolerant — any failure here is swallowed so
        # it can never flip the dream's status or break the run.
        try:
            target_date = datetime.now().astimezone().strftime("%Y-%m-%d")
            book_page.run_book_pages(target_date, on_event=_on_event)
        except Exception:  # noqa: BLE001 — book pages must never break dream
            logger.exception("dream: book-page sub-step failed (run=%s)", run_id)

        # Book-chapter sub-step: after the pages are written, re-cluster the
        # recent chat sessions into themed chapters (Book → Sessions list). Like
        # the book-page step it reuses the dream run's event stream and is fully
        # fault-tolerant — any failure here is swallowed so it can never flip the
        # dream's status or break the run.
        try:
            book_chapters.run_book_chapters(on_event=_on_event)
        except Exception:  # noqa: BLE001 — book chapters must never break dream
            logger.exception("dream: book-chapters sub-step failed (run=%s)", run_id)

        return run_id, result
    finally:
        _dream_lock.release()


def run_dream_with_recording(cfg: Config, *, trigger: str) -> tuple[int, DreamResult]:
    """Reserve the slot and run dream in one shot. Used by ``daily-tick``
    where reservation and execution happen together. Manual triggers via
    the HTTP endpoint go through the two-step ``try_reserve_dream_run`` +
    ``run_reserved_dream`` flow so the reservation check and the
    background-task spawn happen atomically (no release/re-acquire race).

    Raises :class:`DreamAlreadyRunningError` if another dream is in flight.
    """
    if not try_reserve_dream_run():
        raise DreamAlreadyRunningError("another dream is already running")
    return run_reserved_dream(cfg, trigger=trigger)

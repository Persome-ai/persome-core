"""S2 tracker stage — the hourly aggregation-window thread folder (spec §四).

触发（F2/F4 修复）：挂载点只有**终态 reduce 回调**（``session/tick.py`` 的
``_after_reduce``，classifier 同款——flush 路径没有 hook 也不挂）。回调不直接
触发 tracker，只把 session 摘要入队；tracker 按聚合窗口批跑：
``max(window_minutes, 攒满 window_sessions 个摘要)`` 触发一次，一次调用消化整窗。
真实分布是 35 个微 session/天 → 约 8-12 次 tracker 调用/天。

H2 双模型分歧探针（spec §十 10.3）：聚合窗口判断便宜，所以两个不同 prompt 各跑
一遍；分歧率是**无标签的实时不确定性信号**（不确定性，非正确性）。分歧窗口自动
降被触线 confidence + 进待标注队列（``workthread_labels`` action=needs_label，
H1 标注屏优先消耗）。
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .. import events as events_mod
from ..config import Config
from ..logger import get
from ..prompts import load as load_prompt
from ..store import fts
from ..writer import llm as llm_mod
from . import executor
from . import store as wt_store
from .model import ThreadOp

logger = get("persome.workthread.tracker")

# How far back assignment intents feed input ④ (spec §四 输入装配).
_ASSIGNMENT_LOOKBACK_HOURS = 72
# Input ③ dormant index window.
_DORMANT_INDEX_DAYS = 90
# H2 disagreement: signature Jaccard below this counts as a disagreement.
_DISAGREE_JACCARD = 0.5
# Deterministic confidence down-weight applied to threads touched by a
# disagreed window (uncertainty signal, not a correctness verdict).
_DISAGREE_CONFIDENCE_FACTOR = 0.9

# A real screen-activity sub_task carries a ``[HH:MM-HH:MM, <app>]`` span header
# (emitted by the reducer from actual capture blocks; see session_reducer). Recall
# / memory-injection content (``central:``/``summary:`` schema digests, user-profile
# / methodology摘要) has NO such span — it is context, never "what you were doing".
# This is the structural distinguisher behind the recall-vs-activity gate (#248):
# spans are minutes-of-screen-time, so only spanned sub_tasks are activity evidence.
_SPAN_HEADER = re.compile(r"\[\s*\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}")


def has_spanned_activity(sub_tasks: list[str]) -> bool:
    """True iff any sub_task carries a real ``[HH:MM-HH:MM]`` activity span.

    The reducer prefixes every captured-activity sub_task with a span header; a
    recall-only / memory-injection summary contributes none. A window whose
    sessions are ALL spanless is not real activity (#248) — it must not seed a
    work thread, only ever aid naming/classification.
    """
    return any(_SPAN_HEADER.search(s or "") for s in sub_tasks)


def _row_has_spanned_activity(row: sqlite3.Row) -> bool:
    """Per-queue-row variant of :func:`has_spanned_activity` (sub_tasks是 JSON)."""
    try:
        sub_tasks = json.loads(row["sub_tasks"] or "[]")
    except (TypeError, ValueError):
        return False
    return has_spanned_activity([str(s) for s in sub_tasks])


@dataclass
class TrackResult:
    ran: bool
    window_id: str = ""
    sessions: int = 0
    ops: list[ThreadOp] = field(default_factory=list)
    apply: executor.ApplyResult | None = None
    disagreement: bool = False
    skipped_reason: str = ""


def enqueue_session_summary(
    cfg: Config,
    *,
    session_id: str,
    summary: str,
    sub_tasks: list[str],
    start_time: str,
    end_time: str,
) -> None:
    """Called from the terminal-reduce callback: enqueue, then maybe batch-run.

    Both halves are best-effort — a tracker failure must never affect the
    reducer/classifier chain it rides on (executor rule 4 upstream analogue).
    """
    if not cfg.thread_tracker.enabled:
        return
    try:
        with fts.cursor() as conn:
            wt_store.enqueue_session(
                conn,
                session_id=session_id,
                summary=summary,
                sub_tasks=sub_tasks,
                start_time=start_time,
                end_time=end_time,
            )
        maybe_run_window(cfg)
    except Exception as exc:  # noqa: BLE001 — never break the reduce chain
        logger.warning("workthread enqueue/track failed (ignored): %s", exc)


def window_due(rows: list[sqlite3.Row], *, now: datetime, cfg: Config) -> bool:
    """聚合窗口判据：攒满 N 个 session 摘要，或最老一条已等了 window_minutes."""
    if not rows:
        return False
    if len(rows) >= max(1, cfg.thread_tracker.window_sessions):
        return True
    try:
        oldest = datetime.fromisoformat(rows[0]["enqueued_at"])
    except (ValueError, TypeError):
        return True  # malformed timestamp — don't wedge the queue
    return (now - oldest) >= timedelta(minutes=max(1, cfg.thread_tracker.window_minutes))


def maybe_run_window(
    cfg: Config, *, force: bool = False, now: datetime | None = None
) -> TrackResult:
    """Run the tracker over the pending queue when the aggregation window is due."""
    now_dt = now or datetime.now()
    with fts.cursor() as conn:
        rows = wt_store.pending_queue(conn)
        if not rows:
            return TrackResult(ran=False, skipped_reason="queue empty")
        if not force and not window_due(rows, now=now_dt, cfg=cfg):
            return TrackResult(ran=False, skipped_reason=f"window not due ({len(rows)} pending)")
        return _run_window(cfg, conn, rows, now=now_dt)


def _run_window(
    cfg: Config, conn: sqlite3.Connection, rows: list[sqlite3.Row], *, now: datetime
) -> TrackResult:
    # window_id must be unique PER BATCH, not per wall-clock minute: the
    # executor's attach idempotence keys on it (same id = replay = replace the
    # binding), and two batches in one minute (day-end force flush right after
    # a count-triggered run) would otherwise roll each other's minutes back.
    # The trailing queue row id is monotonic, so the suffix never repeats.
    window_id = f"{now.isoformat(timespec='minutes')}#{rows[-1]['id']}"

    # Recall-vs-activity flag (#248): a window whose sessions are ALL spanless
    # carries no real screen-time evidence — it is recall / memory-injection摘要
    # (用户画像 / 工程哲学 / 方法论) only. Such a window may still legitimately drive
    # lifecycle ops on EXISTING threads (a spanless 收尾 with a complete证据), but it
    # must NOT seed a NEW thread — opening off recalled context is the hallucinated
    # zero-intersection line the issue reports. The executor enforces the narrow
    # rule (drop ``open`` when no activity); naming still uses recall via ①ʹ.
    window_has_activity = any(_row_has_spanned_activity(r) for r in rows)

    user_text = _render_input(conn, rows)
    system = load_prompt("thread_tracker.system.md")

    ops = _call_tracker(cfg, system, user_text, stage_label="primary")
    if ops is None:
        # LLM failed — leave the queue unconsumed; the next session end retries
        # with a bigger window (rule 4: never break upstream, never lose input).
        wt_store.record_tick(
            conn,
            ts=window_id,
            window_id=window_id,
            sessions=len(rows),
            opens=0,
            attaches=0,
            revives=0,
            completes=0,
            merges=0,
            outcome="llm_error",
        )
        return TrackResult(ran=False, window_id=window_id, skipped_reason="llm error")

    # H2 disagreement probe: a second, differently-prompted pass over the SAME
    # window. Runs BEFORE the executor so both passes judge identical state.
    disagreement = False
    if cfg.thread_tracker.disagreement_probe:
        probe_system = load_prompt("thread_tracker.probe.system.md")
        probe_ops = _call_tracker(cfg, probe_system, user_text, stage_label="probe")
        if probe_ops is not None:
            disagreement = ops_disagree(ops, probe_ops)

    session_ids = [str(r["session_id"]) for r in rows]
    result = executor.apply_ops(
        conn,
        ops,
        window_id=window_id,
        session_ids=session_ids,
        now=now.isoformat(timespec="minutes"),
        window_has_activity=window_has_activity,
    )

    if disagreement:
        _apply_disagreement(
            conn, result.touched_ids, window_id=window_id, day=now.strftime("%Y-%m-%d")
        )

    wt_store.mark_consumed(conn, [int(r["id"]) for r in rows])
    wt_store.record_tick(
        conn,
        ts=window_id,
        window_id=window_id,
        sessions=len(rows),
        opens=result.opens,
        attaches=result.attaches,
        revives=result.revives,
        completes=result.completes,
        merges=result.merges,
        disagreement=disagreement,
    )
    wt_store.maybe_freeze_on_churn(
        conn,
        freeze_on_churn=cfg.thread_tracker.freeze_on_churn,
        threshold=cfg.thread_tracker.churn_freeze_threshold,
    )

    logger.info(
        "workthread window %s: %d sessions → %d ops (open=%d attach=%d revive=%d "
        "complete=%d merge=%d, disagreement=%s, active=%s)",
        window_id,
        len(rows),
        len(ops),
        result.opens,
        result.attaches,
        result.revives,
        result.completes,
        result.merges,
        disagreement,
        result.active_id or "-",
    )
    events_mod.publish(
        "workthread",
        "window_tracked",
        {
            "window_id": window_id,
            "sessions": len(rows),
            "opens": result.opens,
            "attaches": result.attaches,
            "revives": result.revives,
            "completes": result.completes,
            "merges": result.merges,
            "disagreement": disagreement,
            "active_thread": result.active_id,
        },
    )
    return TrackResult(
        ran=True,
        window_id=window_id,
        sessions=len(rows),
        ops=ops,
        apply=result,
        disagreement=disagreement,
    )


def _call_tracker(
    cfg: Config, system: str, user_text: str, *, stage_label: str
) -> list[ThreadOp] | None:
    """One tracker LLM pass → validated ops, or None on call failure.

    The H2 probe differs by *prompt* (``thread_tracker.probe.system.md``), the
    cheap half of "两个不同 prompt/模型" — a per-call model override would need a
    second stage section; the prompt diversity alone already decorrelates the
    two passes' folding errors.
    """
    try:
        resp = llm_mod.call_llm(
            cfg,
            "thread_tracker",
            messages=[
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
                {"role": "user", "content": user_text},
            ],
            json_mode=True,
        )
        text = llm_mod.extract_text(resp).strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]
            if text.endswith("```"):
                text = text[:-3]
        data = json.loads(text) if text else []
        return executor.parse_ops(data)
    except Exception as exc:  # noqa: BLE001 — tracker must never crash callers
        logger.warning("thread tracker LLM (%s) failed: %s", stage_label, exc)
        return None


# ─── H2 disagreement ─────────────────────────────────────────────────────────


def ops_signature(ops: list[ThreadOp]) -> frozenset[str]:
    """Window-judgment signature: which lines did this pass route work to?

    attach/progress/complete → the target thread id; open → the normalized
    title token set (so two passes opening "the same" new line agree); merge →
    the absorbing pair. ``none`` contributes nothing.
    """
    sig: set[str] = set()
    for op in ops:
        if op.op in ("attach", "progress", "complete") and op.thread_id:
            sig.add(f"t:{op.thread_id}")
        elif op.op == "open":
            tokens = sorted(wt_store.normalize_title_tokens(op.title))[:6]
            sig.add("open:" + "·".join(tokens))
        elif op.op == "merge":
            sig.add(f"merge:{op.from_id}>{op.into_id}")
    return frozenset(sig)


def ops_disagree(a: list[ThreadOp], b: list[ThreadOp]) -> bool:
    """Jaccard of the two passes' signatures below threshold = disagreement."""
    sa, sb = ops_signature(a), ops_signature(b)
    if not sa and not sb:
        return False  # both said none/idle — agreement
    union = sa | sb
    if not union:
        return False
    return (len(sa & sb) / len(union)) < _DISAGREE_JACCARD


def _apply_disagreement(
    conn: sqlite3.Connection, touched_ids: list[str], *, window_id: str, day: str
) -> None:
    """Disagreed window → down-weight touched threads + queue for H1 labeling."""
    for thread_id in dict.fromkeys(touched_ids):
        thread = wt_store.get_thread(conn, thread_id)
        if thread is None:
            continue
        thread.confidence = round(thread.confidence * _DISAGREE_CONFIDENCE_FACTOR, 4)
        wt_store.save_thread(conn, thread)
        wt_store.add_label(
            conn,
            day=day,
            thread_id=thread_id,
            action="needs_label",
            payload={"window_id": window_id, "reason": "tracker disagreement (H2)"},
            source="disagreement",
        )


# ─── input assembly (spec §四 输入装配 ①②③④) ─────────────────────────────────


def _render_input(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> str:
    parts: list[str] = ["# ① 本聚合窗口内的 session 摘要（含 [HH:MM-HH:MM] 时间段）"]
    recall_only_parts: list[str] = []
    for r in rows:
        try:
            sub_tasks = [str(s) for s in json.loads(r["sub_tasks"] or "[]")]
        except (TypeError, ValueError):
            sub_tasks = []
        start = str(r["start_time"] or "")[11:16]
        end = str(r["end_time"] or "")[11:16]
        summary = str(r["summary"] or "").strip()
        # A spanless session摘要 carries no real screen-time evidence — it is recall
        # / context only (#248). Route it to a clearly-labeled非活动证据 block so the
        # LLM may use it for naming/分类 but never opens a thread off it alone.
        bucket = parts if has_spanned_activity(sub_tasks) else recall_only_parts
        bucket.append(f"[session {r['session_id']}] ({start}–{end})")
        if summary:
            bucket.append(summary)
        bucket.extend(f"- {s}" for s in sub_tasks)
        bucket.append("")
    if recall_only_parts:
        parts.append("")
        parts.append(
            "# ①ʹ 仅召回/上下文摘要（无 [HH:MM-HH:MM] 活动段 — 仅供命名/归类参考，"
            "**不可**单独据此 open 新线或 attach）"
        )
        parts.extend(recall_only_parts)

    parts.append("# ② Open 线清单（active + background）")
    open_lines = wt_store.open_threads(conn)
    if open_lines:
        for t in open_lines:
            recent = t.progress_notes[-1] if t.progress_notes else ""
            parts.append(
                f"- id={t.id} [{t.status}] {t.title} — {t.goal or '-'}"
                f"; last_active={t.last_active}" + (f"; 最近进展: {recent[:80]}" if recent else "")
            )
    else:
        parts.append("(无 open 线)")
    parts.append("")

    parts.append(
        f"# ③ 休眠线索引（近 {_DORMANT_INDEX_DAYS} 天 done/stale/superseded —"
        " 复活/recurring 的接球区：恢复其中一条 = 对它的 id 发 attach）"
    )
    dormant = wt_store.non_open_index(conn, days=_DORMANT_INDEX_DAYS)
    if dormant:
        for t in dormant:
            actor = f" by {t.origin_actor}" if t.origin_actor else ""
            parts.append(f"- id={t.id} [{t.status}] {t.title}{actor}; last_active={t.last_active}")
    else:
        parts.append("(无)")
    parts.append("")

    background = _assignment_background(conn)
    if background:
        parts.append("# ④ 背景：近 72h 指派类意图（assignment）")
        parts.append(background)
        parts.append("")

    parts.append(
        '请基于整窗输出操作 JSON（{"ops": [...]}）。ATTACH-FIRST；spans 逐字取自'
        " sub_task 的 [HH:MM-HH:MM] 头；一段 span 至多指派给一条线。"
    )
    return "\n".join(parts)


def _assignment_background(conn: sqlite3.Connection) -> str:
    """近 72h assignment intents — 工作线起点的最强证据（S0 → S2 接力）."""
    try:
        from ..intent import store as intent_store

        since = (datetime.now() - timedelta(hours=_ASSIGNMENT_LOOKBACK_HOURS)).isoformat(
            timespec="minutes"
        )
        # ``end`` is exclusive (`ts < end`) — an intent recognized THIS minute
        # must still feed the window, so push the bound past any minute value.
        rows = intent_store.recent_intents(conn, start=since, end="￿")
    except Exception:  # noqa: BLE001 — background is optional
        return ""
    lines: list[str] = []
    for it in rows:
        if it.kind != "assignment":
            continue
        task = str(it.payload.get("task_text") or "").strip()
        actor = str(it.payload.get("assigned_by") or "").strip()
        quote = it.evidence[0].quote if it.evidence else ""
        norm_actor = unicodedata.normalize("NFKC", actor)
        line = f"- [assignment] {task or it.rationale}"
        if norm_actor:
            line += f"（{norm_actor} 交办）"
        if quote:
            line += f" “{quote[:80]}”"
        lines.append(line)
    return "\n".join(lines[-8:])

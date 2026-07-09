"""Deterministic executor for the ThreadOp closed set (spec §四).

The LLM tracker emits ops; THIS module executes them. The contract:

时间账（F3 修复，全部可单测）:
- ``spans`` must be copied from the window's sub_task ``[HH:MM-HH:MM]`` headers —
  the LLM only *assigns* spans to threads; durations are computed here.
- Overlapping spans across threads within one window are split evenly per
  minute and the affected threads are marked ``approximate``.
- An attach without spans is legal but adds **zero** minutes (only bumps
  ``last_active``) — 宁可漏记不虚报。
- Minutes are NEVER taken from the model.

执行器规则:
1. attach 幂等（同窗口重放替换该窗口的 binding，不双计）；active 竞争走滞回。
2. open 查重闸（F8）：全历史语义查重（store.find_duplicate）；命中 → 转 attach，
   非 open 线自动复活（status 翻回 open 集，记 revived）。
3. merge 单向吸收，from 置 superseded；pinned 线不可被吸收。
4. 任何 op 失败只 log，绝不反向破坏上游落库。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..logger import get
from . import projection
from . import store as wt_store
from .model import (
    ACTIVE_TAKEOVER_SHARE,
    OPEN_STATUSES,
    ORIGIN_TYPES,
    STALE_AFTER_DAYS,
    Binding,
    ThreadOp,
    WorkThread,
)

logger = get("persome.workthread.executor")


@dataclass
class ApplyResult:
    """What one window's op batch did — feeds the telemetry tick."""

    opens: int = 0
    attaches: int = 0
    revives: int = 0
    completes: int = 0
    merges: int = 0
    progresses: int = 0
    skipped: int = 0
    touched_ids: list[str] = field(default_factory=list)
    active_id: str = ""


def _parse_hhmm(text: str) -> int | None:
    """``"HH:MM"`` → minute-of-day, or None when malformed."""
    parts = text.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h * 60 + m


def _span_minutes(spans: list[list[str]]) -> set[int]:
    """Spans → the set of minute-of-day indices they cover.

    A span whose end <= start is dropped (midnight-crossing sub_tasks are an
    edge the reducer's day-bucketed files never produce; 宁可漏记不虚报).
    """
    minutes: set[int] = set()
    for pair in spans:
        if len(pair) != 2:
            continue
        start, end = _parse_hhmm(pair[0]), _parse_hhmm(pair[1])
        if start is None or end is None or end <= start:
            continue
        minutes.update(range(start, end))
    return minutes


def apply_ops(
    conn: sqlite3.Connection,
    ops: list[ThreadOp],
    *,
    window_id: str,
    session_ids: list[str] | None = None,
    now: str | None = None,
    window_has_activity: bool = True,
) -> ApplyResult:
    """Execute one aggregation window's op batch deterministically.

    Order of execution: open-dedup resolution first (an open may become an
    attach), then per-thread span accounting with overlap均摊, then the
    lifecycle verbs, finally ONE hysteresis active-competition pass over the
    window's span totals.

    ``window_has_activity`` (#248): when False, EVERY queued session in this
    window was spanless — recall / memory-injection摘要 with no real screen-time
    evidence. A genuinely-new ``open`` in such a window is a hallucinated
    zero-intersection line (the LLM naming a thread off recalled context that
    never names a real activity), so new opens are DROPPED. A recall window may
    still legitimately drive lifecycle ops on EXISTING threads
    (attach/progress/complete/merge — e.g. a spanless 收尾 carrying完成证据, or a
    dedup-hit open that converts to attach): this is a new-thread-creation guard,
    NOT a window kill-switch.
    """
    now = now or datetime.now().isoformat(timespec="minutes")
    session_ids = session_ids or []
    result = ApplyResult()
    frozen_open = wt_store.is_open_frozen(conn)

    # Per-batch thread cache (#574): two ops in one window can target the same
    # thread (two attaches to one undertaking, or open-dedup hit + attach on the
    # same line). Loading each independently means each sees the batch-START state
    # and the second save_thread整行覆盖 the first → lost binding / minutes. Route
    # every load + new-insert through this cache so same-id ops share ONE mutable
    # instance that accumulates across the batch before its final save.
    thread_cache: dict[str, WorkThread] = {}

    def _load_thread(tid: str) -> WorkThread | None:
        if tid in thread_cache:
            return thread_cache[tid]
        t = wt_store.get_thread(conn, tid)
        if t is not None and t.id:
            thread_cache[t.id] = t
        return t

    def _cache(thread: WorkThread) -> WorkThread:
        """Register/return the canonical cached instance for this thread id."""
        if thread.id and thread.id in thread_cache:
            return thread_cache[thread.id]
        if thread.id:
            thread_cache[thread.id] = thread
        return thread

    # Phase 1 — resolve opens through the dedup gate; collect attach intents.
    # Each item: (thread_or_None_for_new_open, op, revived?)
    resolved: list[tuple[WorkThread | None, ThreadOp, bool]] = []
    for op in ops:
        try:
            if op.op == "none":
                continue
            if op.op == "open":
                dup = wt_store.find_duplicate(conn, title=op.title, origin_actor=op.origin_actor)
                if dup is not None:
                    dup = _cache(dup)
                    revived = dup.status not in OPEN_STATUSES
                    logger.info(
                        "open dedup hit: %r matches thread %s (%s)%s — converting to attach",
                        op.title,
                        dup.id,
                        dup.title,
                        " [revived]" if revived else "",
                    )
                    resolved.append((dup, op, revived))
                    continue
                if not window_has_activity:
                    # #248: no real screen-time evidence anywhere in this window —
                    # a NEW open (dedup already missed above) would be a thread
                    # named off recalled context whose title shares zero entities
                    # with any real activity. Drop it; recall is naming context,
                    # not "what you were doing". Existing-thread ops are untouched.
                    logger.info(
                        "recall-only window — dropping hallucinated open %r (no spanned "
                        "activity to ground a new thread; #248)",
                        op.title,
                    )
                    result.skipped += 1
                    continue
                if frozen_open:
                    # churn 超阈冻结：只 attach，不开新线（spec §七 遥测动作）。
                    # 无既有线可挂 → 本窗口这段时间不入账（漏 = 有限损失）。
                    logger.warning("open frozen (churn) — dropping open %r", op.title)
                    result.skipped += 1
                    continue
                if not op.title:
                    result.skipped += 1
                    continue
                resolved.append((None, op, False))
            elif op.op == "attach":
                thread = _load_thread(op.thread_id)
                if thread is None:
                    logger.warning("attach to unknown thread %r — skipped", op.thread_id)
                    result.skipped += 1
                    continue
                revived = thread.status not in OPEN_STATUSES
                resolved.append((thread, op, revived))
            else:
                resolved.append((None, op, False))
        except Exception as exc:  # noqa: BLE001 — rule 4: one bad op never kills the batch
            logger.warning("op resolution failed (%s): %s — skipped", op.op, exc)
            result.skipped += 1

    # Phase 2 — span accounting with per-minute overlap均摊 across the batch.
    # Index span-carrying items: position → minute set.
    span_sets: dict[int, set[int]] = {}
    for i, (_thread, op, _revived) in enumerate(resolved):
        if op.op in ("open", "attach"):
            span_sets[i] = _span_minutes(op.spans)
    minute_claims: dict[int, int] = {}
    for minutes in span_sets.values():
        for m in minutes:
            minute_claims[m] = minute_claims.get(m, 0) + 1
    # fair-share minutes (float) + did this item hit any contested minute?
    fair_minutes: dict[int, float] = {}
    contested: dict[int, bool] = {}
    for i, minutes in span_sets.items():
        share = 0.0
        cont = False
        for m in minutes:
            n = minute_claims[m]
            share += 1.0 / n
            if n > 1:
                cont = True
        fair_minutes[i] = share
        contested[i] = cont

    # Phase 3 — execute lifecycle verbs.
    window_minutes_by_thread: dict[str, float] = {}
    for i, (thread, op, revived) in enumerate(resolved):
        try:
            if op.op in ("open", "attach"):
                share = fair_minutes.get(i, 0.0)
                approx = contested.get(i, False)
                if thread is None:
                    # Re-run the dedup gate at execution time: an earlier open
                    # in THIS batch may have just created the thread this open
                    # is a twin of — Phase 1's lookup predates that insert, so
                    # without this re-check a window emitting two opens for one
                    # undertaking would mint a twin (the exact F8 failure).
                    dup = wt_store.find_duplicate(
                        conn, title=op.title, origin_actor=op.origin_actor
                    )
                    if dup is not None:
                        thread = _cache(dup)
                        revived = thread.status not in OPEN_STATUSES
                if thread is None:
                    # Genuine new open.
                    origin_type = (
                        op.origin_type if op.origin_type in ORIGIN_TYPES else ("self_initiated")
                    )
                    thread = WorkThread(
                        id="",
                        title=op.title,
                        goal=op.goal,
                        origin_type=origin_type,
                        origin_actor=op.origin_actor,
                        origin_evidence=(
                            [{"source": "session_summary", "quote": op.origin_quote}]
                            if op.origin_quote
                            else []
                        ),
                        origin_at=now,
                        status="background",
                        first_seen=now,
                        last_active=now,
                        confidence=op.confidence,
                    )
                    wt_store.insert_thread(conn, thread)
                    _cache(thread)  # register so a later same-batch op shares it
                    result.opens += 1
                    _bind_window(
                        thread, window_id=window_id, session_ids=session_ids, spans=op.spans
                    )
                    _credit_minutes(thread, share, approx, now)
                    wt_store.save_thread(conn, thread)
                    projection.project_event(conn, thread, f"opened — {op.goal or op.title}")
                else:
                    if revived:
                        thread.status = "background"
                        result.revives += 1
                        logger.info("thread %s revived by window %s", thread.id, window_id)
                    result.attaches += 1
                    _bind_window(
                        thread, window_id=window_id, session_ids=session_ids, spans=op.spans
                    )
                    _credit_minutes(thread, share, approx, now)
                    if op.note:
                        thread.progress_notes.append(f"[{now}] {op.note}")
                    wt_store.save_thread(conn, thread)
                    if revived:
                        projection.project_event(conn, thread, "revived (attach hit dormant line)")
                window_minutes_by_thread[thread.id] = (
                    window_minutes_by_thread.get(thread.id, 0.0) + share
                )
                result.touched_ids.append(thread.id)
            elif op.op == "progress":
                thread = _load_thread(op.thread_id)
                if thread is None or not op.note:
                    result.skipped += 1
                    continue
                thread.progress_notes.append(f"[{now}] {op.note}")
                thread.last_active = now
                wt_store.save_thread(conn, thread)
                result.progresses += 1
                result.touched_ids.append(thread.id)
            elif op.op == "complete":
                thread = _load_thread(op.thread_id)
                if thread is None:
                    result.skipped += 1
                    continue
                if not op.evidence_quote:
                    # Prompt rule 5: COMPLETE needs explicit evidence. Without a
                    # quote we refuse — staleness is code's job, not the model's.
                    logger.warning("complete without evidence_quote for %s — ignored", op.thread_id)
                    result.skipped += 1
                    continue
                thread.status = "done"
                thread.last_active = now
                thread.progress_notes.append(f"[{now}] completed — “{op.evidence_quote[:120]}”")
                wt_store.save_thread(conn, thread)
                result.completes += 1
                result.touched_ids.append(thread.id)
                projection.project_event(conn, thread, f"completed — “{op.evidence_quote[:80]}”")
            elif op.op == "merge":
                merged = merge_threads(conn, from_id=op.from_id, into_id=op.into_id, now=now)
                if merged:
                    # merge_threads mutated the DB directly; drop stale cached
                    # copies so any later same-batch op re-loads fresh state (#574).
                    thread_cache.pop(op.from_id, None)
                    thread_cache.pop(op.into_id, None)
                    result.merges += 1
                    result.touched_ids.append(op.into_id)
                else:
                    result.skipped += 1
        except Exception as exc:  # noqa: BLE001 — rule 4
            logger.warning("op execution failed (%s %s): %s", op.op, op.thread_id, exc)
            result.skipped += 1

    # Phase 4 — hysteresis active competition over THIS window's span totals.
    result.active_id = _compete_active(conn, window_minutes_by_thread, now=now)
    return result


def _bind_window(
    thread: WorkThread, *, window_id: str, session_ids: list[str], spans: list[list[str]]
) -> None:
    """Attach idempotence: re-applying the same window REPLACES its binding.

    The minutes credited for a replaced binding are subtracted before the new
    credit lands (callers credit after binding), so a tracker retry of the same
    window never double-counts.
    """
    kept: list[Binding] = []
    for b in thread.bindings:
        if b.window_id == window_id:
            # Roll back the previous credit for this window (idempotent replay).
            old_minutes = len(_span_minutes(b.spans))
            # Previous credit may have been fair-shared; we only know the upper
            # bound. Conservative rollback: subtract the full span length and
            # re-mark approximate (an idempotent replay after a split window is
            # rare; the figure stays a fair estimate, never an over-count).
            if old_minutes:
                thread.total_active_minutes = max(0, thread.total_active_minutes - old_minutes)
                thread.approximate = True
            continue
        kept.append(b)
    kept.append(Binding(window_id=window_id, session_ids=list(session_ids), spans=spans))
    thread.bindings = kept


def _credit_minutes(thread: WorkThread, minutes: float, approx: bool, now: str) -> None:
    thread.total_active_minutes += int(round(minutes))
    if approx:
        thread.approximate = True
    thread.last_active = now


def _compete_active(conn: sqlite3.Connection, window_minutes: dict[str, float], *, now: str) -> str:
    """带滞回的 active 竞争（spec §三）.

    The window's top thread takes ``active`` only when it won ≥60% of the
    window's assigned span minutes OR it already IS the incumbent; otherwise
    the incumbent stays. A window with no span minutes changes nothing.
    Returns the id of the active thread after the pass ("" when none).
    """
    incumbent = wt_store.active_thread(conn)
    total = sum(window_minutes.values())
    if total <= 0:
        return incumbent.id if incumbent else ""
    # Only OPEN threads may win the active slot. A thread that was completed /
    # merged earlier in THIS batch still carries this window's span minutes, but
    # picking it would demote the incumbent (line below) while the OPEN_STATUSES
    # guard blocks its own promotion → a zero-active gap + active_id pointing at a
    # non-open line (#565). top_share stays relative to the whole window total
    # (denominator unchanged) so the takeover bar isn't lowered.
    open_candidates = {
        tid: m
        for tid, m in window_minutes.items()
        if (t := wt_store.get_thread(conn, tid)) is not None and t.status in OPEN_STATUSES
    }
    if not open_candidates:
        return incumbent.id if incumbent else ""
    top_id = max(open_candidates, key=lambda k: open_candidates[k])
    top_share = window_minutes[top_id] / total
    winner_id: str
    if incumbent is None:
        winner_id = top_id
    elif top_id == incumbent.id:
        winner_id = incumbent.id
    elif top_share >= ACTIVE_TAKEOVER_SHARE:
        winner_id = top_id
    else:
        winner_id = incumbent.id

    if incumbent is not None and incumbent.id != winner_id:
        incumbent.status = "background"
        wt_store.save_thread(conn, incumbent)
    winner = wt_store.get_thread(conn, winner_id)
    if winner is not None and winner.status in OPEN_STATUSES and winner.status != "active":
        winner.status = "active"
        wt_store.save_thread(conn, winner)
    return winner_id if winner is not None else ""


def merge_threads(
    conn: sqlite3.Connection, *, from_id: str, into_id: str, now: str | None = None
) -> bool:
    """merge 单向吸收（rule 3）：from → superseded；pinned from 不可被吸收."""
    now = now or datetime.now().isoformat(timespec="minutes")
    src = wt_store.get_thread(conn, from_id)
    dst = wt_store.get_thread(conn, into_id)
    if src is None or dst is None or from_id == into_id:
        logger.warning("merge skipped: from=%r into=%r (missing/self)", from_id, into_id)
        return False
    if src.pinned:
        logger.warning("merge refused: thread %s is pinned (人工确认线不可被吸收)", from_id)
        return False
    dst.total_active_minutes += src.total_active_minutes
    if src.approximate:
        dst.approximate = True
    dst.bindings.extend(src.bindings)
    dst.progress_notes.append(f"[{now}] merged in thread {src.id} ({src.title})")
    dst.last_active = max(dst.last_active, src.last_active, now)
    src.status = "superseded"
    src.progress_notes.append(f"[{now}] superseded by merge into {dst.id}")
    wt_store.save_thread(conn, dst)
    wt_store.save_thread(conn, src)
    projection.project_event(conn, dst, f"absorbed thread {src.id} ({src.title})")
    return True


def harvest_stale(conn: sqlite3.Connection, *, now: datetime | None = None) -> int:
    """日终 tick：30 天无 attach 的 open 线收割为 stale（pinned 豁免）.

    Inactivity is NEVER completion — stale is reversible (the dedup gate
    revives a stale line the moment a window attaches to it again).
    """
    now_dt = now or datetime.now()
    cutoff = (now_dt - timedelta(days=STALE_AFTER_DAYS)).isoformat(timespec="minutes")
    harvested = 0
    for thread in wt_store.open_threads(conn):
        if thread.pinned:
            continue
        if thread.last_active and thread.last_active < cutoff:
            thread.status = "stale"
            wt_store.save_thread(conn, thread)
            projection.project_event(conn, thread, f"stale (no attach for {STALE_AFTER_DAYS}d)")
            harvested += 1
    return harvested


def parse_ops(data: object) -> list[ThreadOp]:
    """LLM payload → validated ops. Accepts ``[...]`` or ``{"ops": [...]}``."""
    raw = data.get("ops") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return []
    out: list[ThreadOp] = []
    for item in raw:
        op = ThreadOp.from_dict(item) if isinstance(item, dict) else None
        if op is not None:
            out.append(op)
    return out

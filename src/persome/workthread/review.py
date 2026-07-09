"""纠错口 + H1 日终标注屏（spec §六-3 / §十）— the label factory.

设计宪法的落点：「这一小时属于哪条线」的 ground truth 在用户脑里（偶然熵）。
本模块不是反馈功能，是 **harness 的主干**：每一次纠错/确认都是一条真值标签，
三合一回流（spec §10.4）——

1. 修正写回 ``work_threads``（status/title/归属/pin）；
2. 铸成金标准样本（``workthread_labels``，可由 ``export_day_fixture`` 导出为
   S2 eval 夹具，生产持续铸造 eval 集）；
3. confidence 校准（确定性 delta，schema_feedback 同款手法——deterministic,
   bounded, recoverable）。

CLI 形态先行（S3）：``persome thread review-day`` / ``thread correct``；
S4 的 HUD chip 只是同一闭集的图形化。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from ..logger import get
from . import executor
from . import store as wt_store

logger = get("persome.workthread.review")

# Deterministic confidence deltas (schema_feedback 同款: dismiss −0.05 /
# consume +0.03; here the signals are stronger — a human explicitly judged THIS
# thread — so the steps are bigger but still bounded and recoverable).
CONFIRM_DELTA = 0.05
CORRECT_DELTA = -0.15
CONFIDENCE_FLOOR = 0.1
CONFIDENCE_CEIL = 0.98

# Correction closed set (the HUD chip in S4 graphicalizes exactly this).
CORRECTION_ACTIONS: tuple[str, ...] = ("confirm", "not_this", "rename", "merge", "pin")


@dataclass
class DayReview:
    day: str
    lines: list[dict] = field(default_factory=list)  # one per thread touched that day
    pending_labels: int = 0  # H2 disagreement queue size (priority annotations)


def build_day_review(conn: sqlite3.Connection, *, day: str | None = None) -> DayReview:
    """日终一屏（H1）：当日被触及的线 + 累计时长，等用户点头或一划修正.

    "今天 3 条线：Kevin·意图识别 4.2h / spec 1.1h / 杂事 0.5h——对吗?"
    """
    day = day or datetime.now().strftime("%Y-%m-%d")
    review = DayReview(day=day)
    for thread in wt_store.list_threads(conn, limit=200):
        day_minutes = 0
        for binding in thread.bindings:
            if binding.window_id.startswith(day):
                day_minutes += len(executor._span_minutes(binding.spans))
        if day_minutes == 0 and not thread.last_active.startswith(day):
            continue
        review.lines.append(
            {
                "thread_id": thread.id,
                "title": thread.title,
                "status": thread.status,
                "origin_actor": thread.origin_actor,
                "day_minutes": day_minutes,
                "total_minutes": thread.total_active_minutes,
                "approximate": thread.approximate,
                "confidence": thread.confidence,
                "pinned": thread.pinned,
            }
        )
    review.lines.sort(key=lambda d: -d["day_minutes"])
    review.pending_labels = len(wt_store.pending_label_queue(conn))
    return review


def apply_correction(
    conn: sqlite3.Connection,
    *,
    thread_id: str,
    action: str,
    new_title: str = "",
    into_id: str = "",
    day: str | None = None,
    source: str = "correct",
) -> dict:
    """Execute one correction from the closed set; every call mints a label.

    - ``confirm``  — 点头：confidence +0.05（真值：划分是对的）。
    - ``not_this`` — 这不是一条真实的线：status → superseded（保留证据链，不
      删除——错误划分本身也是标签），confidence −0.15，user_corrected +1。
    - ``rename``   — 改名（同一性认对了、名字没说好）：title 替换，轻微正信号。
    - ``merge``    — 两条其实是一件事：走 executor 的 merge（pinned 保护内建）。
    - ``pin``      — 人工确认线：pinned=1，免疫 merge 吸收 / stale 收割。
    """
    if action not in CORRECTION_ACTIONS:
        return {
            "ok": False,
            "error": f"unknown action {action!r} (closed set: {CORRECTION_ACTIONS})",
        }
    thread = wt_store.get_thread(conn, thread_id)
    if thread is None:
        return {"ok": False, "error": f"thread {thread_id} not found"}
    day = day or datetime.now().strftime("%Y-%m-%d")
    now = datetime.now().isoformat(timespec="minutes")
    payload: dict = {"action": action}

    if action == "confirm":
        thread.confidence = min(CONFIDENCE_CEIL, round(thread.confidence + CONFIRM_DELTA, 4))
    elif action == "not_this":
        thread.status = "superseded"
        thread.user_corrected += 1
        thread.confidence = max(CONFIDENCE_FLOOR, round(thread.confidence + CORRECT_DELTA, 4))
        thread.progress_notes.append(f"[{now}] user correction: not a real thread")
    elif action == "rename":
        if not new_title:
            return {"ok": False, "error": "rename needs --rename <new title>"}
        payload["old_title"] = thread.title
        payload["new_title"] = new_title
        thread.title = new_title
        thread.user_corrected += 1
        thread.confidence = min(CONFIDENCE_CEIL, round(thread.confidence + 0.02, 4))
    elif action == "merge":
        if not into_id:
            return {"ok": False, "error": "merge needs --into <thread id>"}
        if not executor.merge_threads(conn, from_id=thread_id, into_id=into_id, now=now):
            return {"ok": False, "error": "merge refused (missing thread or pinned source)"}
        payload["into_id"] = into_id
        # The absorbing thread gains a human-confirmed identity signal.
        dst = wt_store.get_thread(conn, into_id)
        if dst is not None:
            dst.confidence = min(CONFIDENCE_CEIL, round(dst.confidence + CONFIRM_DELTA, 4))
            dst.user_corrected += 1
            wt_store.save_thread(conn, dst)
        thread = wt_store.get_thread(conn, thread_id) or thread
    elif action == "pin":
        thread.pinned = True
        thread.confidence = max(thread.confidence, 0.9)

    if action != "merge":
        wt_store.save_thread(conn, thread)

    # The label IS the product (10.4): it feeds confidence calibration and the
    # exported golden fixtures. H2's needs_label rows for this thread are
    # consumed by this very act (the human just judged it).
    wt_store.add_label(
        conn, day=day, thread_id=thread_id, action=action, payload=payload, source=source
    )
    _consume_needs_label(conn, thread_id)
    logger.info("thread correction: %s %s (%s)", action, thread_id, payload)
    return {"ok": True, "thread_id": thread_id, "action": action, **payload}


def _consume_needs_label(conn: sqlite3.Connection, thread_id: str) -> None:
    conn.execute(
        "UPDATE workthread_labels SET action = 'labeled' "
        "WHERE thread_id = ? AND action = 'needs_label'",
        (thread_id,),
    )
    conn.commit()


def current_work_context(conn: sqlite3.Connection) -> dict:
    """MCP/REST `current_work_context`（spec §六-2）— 可编程的"现在进行时"接口.

    ``total_minutes`` carries the ``approximate`` marker through verbatim（时间
    账契约：均摊过的分钟数是公平估计，不冒充精确值）。
    """

    def _frame(t) -> dict:  # type: ignore[no-untyped-def]
        return {
            "thread_id": t.id,
            "title": t.title,
            "goal": t.goal,
            "status": t.status,
            "origin": {
                "type": t.origin_type,
                "actor": t.origin_actor,
                "at": t.origin_at,
                "intent_id": t.origin_intent_id,
            },
            "since": t.first_seen,
            "last_active": t.last_active,
            "total_minutes": t.total_active_minutes,
            "approximate": t.approximate,
            "confidence": t.confidence,
            "pinned": t.pinned,
            "recent_progress": t.progress_notes[-3:],
            "evidence_refs": t.origin_evidence,
        }

    active = wt_store.active_thread(conn)
    background = wt_store.list_threads(conn, statuses=("background",), limit=5)
    return {
        "active_thread": _frame(active) if active else None,
        "background_threads": [_frame(t) for t in background],
        "stats": wt_store.stats(conn),
    }


def export_day_fixture(conn: sqlite3.Connection, *, day: str) -> dict:
    """把一个已标注日导出为 S2 金标准夹具骨架（H1 → eval 集回流，spec 10.4）.

    Returns a YAML-ready dict: the day's consumed queue rows (the tracker's
    inputs) + the human-verdict thread layout (the expected outputs). Appending
    it to ``tests/eval/golden/workthread_golden.yaml`` is a human act — the
    export keeps provenance explicit instead of silently mutating the repo.
    """
    conn.row_factory = sqlite3.Row
    sessions = conn.execute(
        "SELECT session_id, summary, sub_tasks, start_time, end_time FROM workthread_queue "
        "WHERE start_time LIKE ? ORDER BY start_time",
        (f"{day}%",),
    ).fetchall()
    review = build_day_review(conn, day=day)
    labels = [
        {"thread_id": r["thread_id"], "action": r["action"], "payload": r["payload"]}
        for r in wt_store.labels_for_day(conn, day)
    ]
    return {
        "day": day,
        "sessions": [dict(s) for s in sessions],
        "expected_threads": review.lines,
        "labels": labels,
    }

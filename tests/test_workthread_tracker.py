"""S2 tracker tests: 聚合窗口判据、整窗消化、H2 分歧探针、遥测与 churn 冻结."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from persome import config as config_mod
from persome.store import fts
from persome.workthread import store as wt_store
from persome.workthread import tracker
from persome.workthread.model import ThreadOp


@pytest.fixture
def cfg(ac_root):
    return config_mod.load()


def _enqueue(conn, n: int, *, start_hour: int = 10, enqueued_at: str | None = None) -> None:
    for i in range(n):
        wt_store.enqueue_session(
            conn,
            session_id=f"s{start_hour}{i}",
            summary=f"干活 {i}",
            sub_tasks=[
                f"[{start_hour}:0{i}-{start_hour}:3{i}, Cursor] 改意图识别代码, involving —"
            ],
            start_time=f"2026-06-12T{start_hour}:0{i}",
            end_time=f"2026-06-12T{start_hour}:3{i}",
            enqueued_at=enqueued_at,
        )


def _ops_resp(ops: list[dict]) -> str:
    return json.dumps({"ops": ops}, ensure_ascii=False)


# ─── 聚合窗口判据（F2/F4） ────────────────────────────────────────────────────


def test_window_not_due_below_thresholds(cfg, fake_llm):
    with fts.cursor() as conn:
        _enqueue(conn, 2)
    res = tracker.maybe_run_window(cfg)
    assert not res.ran
    assert "not due" in res.skipped_reason
    assert fake_llm.calls == []  # 没到窗口绝不烧 LLM


def test_window_due_on_session_count(cfg, fake_llm):
    fake_llm.set_default(
        "thread_tracker",
        _ops_resp(
            [
                {
                    "op": "open",
                    "title": "意图识别链路优化",
                    "origin_type": "assignment",
                    "origin_actor": "Kevin",
                    "origin_quote": "这个你来跟进",
                    "spans": [["10:00", "10:30"]],
                    "confidence": 0.8,
                }
            ]
        ),
    )
    with fts.cursor() as conn:
        _enqueue(conn, 5)
    res = tracker.maybe_run_window(cfg)
    assert res.ran
    assert res.apply is not None and res.apply.opens == 1
    with fts.cursor() as conn:
        assert wt_store.pending_queue(conn) == []  # 整窗消化、队列清空
        threads = wt_store.list_threads(conn)
        assert len(threads) == 1 and threads[0].total_active_minutes == 30


def test_window_due_on_age(cfg, fake_llm):
    fake_llm.set_default("thread_tracker", _ops_resp([{"op": "none"}]))
    old = (datetime.now() - timedelta(minutes=90)).isoformat(timespec="seconds")
    with fts.cursor() as conn:
        _enqueue(conn, 1, enqueued_at=old)
    res = tracker.maybe_run_window(cfg)
    assert res.ran


def test_llm_failure_leaves_queue_for_retry(cfg, fake_llm, monkeypatch):
    monkeypatch.setattr(
        "persome.writer.llm.call_llm",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gateway down")),
    )
    with fts.cursor() as conn:
        _enqueue(conn, 5)
    res = tracker.maybe_run_window(cfg)
    assert not res.ran and res.skipped_reason == "llm error"
    with fts.cursor() as conn:
        assert len(wt_store.pending_queue(conn)) == 5  # 输入不丢，下次重试
        row = conn.execute(
            "SELECT outcome FROM workthread_ticks ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row[0] == "llm_error"


def test_disabled_tracker_never_enqueues(cfg, fake_llm):
    cfg.thread_tracker.enabled = False
    tracker.enqueue_session_summary(
        cfg,
        session_id="s1",
        summary="x",
        sub_tasks=[],
        start_time="2026-06-12T10:00",
        end_time="2026-06-12T10:05",
    )
    with fts.cursor() as conn:
        assert wt_store.pending_queue(conn) == []


# ─── 输入装配 ①②③④（F1） ─────────────────────────────────────────────────────


def test_input_contains_dormant_index_and_assignment_background(cfg, fake_llm):
    from persome.intent import sink
    from persome.intent.ontology import Intent, IntentEvidence

    with fts.cursor() as conn:
        # ② open 线 + ③ 休眠线
        from persome.workthread.model import WorkThread

        wt_store.insert_thread(
            conn,
            WorkThread(
                id="",
                title="进行中的线",
                status="active",
                first_seen="2026-06-10T09:00",
                last_active="2026-06-12T09:00",
            ),
        )
        wt_store.insert_thread(
            conn,
            WorkThread(
                id="",
                title="上周做完的线",
                status="done",
                origin_actor="Kevin",
                first_seen="2026-06-05T09:00",
                last_active=datetime.now().isoformat(timespec="minutes"),
            ),
        )
        # ④ 近 72h assignment intent
        sink.persist_intent(
            conn,
            Intent(
                kind="assignment",
                scope="session-x",
                confidence=0.8,
                ts=datetime.now().isoformat(timespec="minutes"),
                payload={"task_text": "把意图识别链路打通", "assigned_by": "Kevin"},
                evidence=[IntentEvidence(source="timeline_block", ref_id="b1", quote="你来跟进")],
            ),
        )
        _enqueue(conn, 5)
    fake_llm.set_default("thread_tracker", _ops_resp([{"op": "none"}]))
    res = tracker.maybe_run_window(cfg)
    assert res.ran
    user_text = fake_llm.calls[0]["messages"][1]["content"]
    assert "① 本聚合窗口" in user_text
    assert "进行中的线" in user_text  # ②
    assert "上周做完的线" in user_text  # ③ 休眠接球区
    assert "把意图识别链路打通" in user_text  # ④ assignment 背景


# ─── #248 recall-vs-activity gate ────────────────────────────────────────────


def _enqueue_recall_only(conn, n: int, *, enqueued_at: str | None = None) -> None:
    """N spanless session摘要 — recall / memory-injection 内容（无 [HH:MM-HH:MM]）.

    模拟 #248：reducer 把召回的 central:/summary: 结构化记忆摘要（用户画像 / 工程
    哲学 / 方法论）回显进 summary，sub_tasks 不带任何活动时间段。
    """
    for i in range(n):
        wt_store.enqueue_session(
            conn,
            session_id=f"recall{i}",
            summary=(
                "central: 用户在工具选型上系统性偏好极简方案\n"
                "summary: 倾向第一性原理拆解、代价不对称优先"
            ),
            sub_tasks=["[Unknown] no notable activity, involving —"],
            start_time=f"2026-06-12T10:0{i}",
            end_time=f"2026-06-12T10:0{i}",
            enqueued_at=enqueued_at,
        )


def test_has_spanned_activity_distinguishes_recall_from_activity():
    # 真实活动：带 [HH:MM-HH:MM] 段
    assert tracker.has_spanned_activity(["[10:00-10:30, Cursor] 改代码"])
    # 召回/记忆注入：无时间段
    assert not tracker.has_spanned_activity(["[Unknown] no notable activity, involving —"])
    assert not tracker.has_spanned_activity(
        ["central: 用户偏好极简方案", "summary: 第一性原理拆解"]
    )
    assert not tracker.has_spanned_activity([])


def test_recall_only_window_opens_no_thread(cfg, fake_llm):
    """#248 核心：整窗都是召回摘要（零活动段）→ LLM 即便幻觉开线也不落库.

    把 LLM 默认应答设成"幻觉开线"（据召回摘要补全的零交集具体标题）——正是 #248
    的失败态。执行器据 window_has_activity=False 丢弃这条新 open，零交集工作线不落库。
    """
    fake_llm.set_default(
        "thread_tracker",
        _ops_resp(
            [
                {
                    "op": "open",
                    "title": "极简工具选型方案落地",  # 召回里没有的零交集具体标题
                    "origin_type": "self_initiated",
                    "spans": [["10:00", "10:30"]],
                    "confidence": 0.8,
                }
            ]
        ),
    )
    with fts.cursor() as conn:
        _enqueue_recall_only(conn, 5)
    res = tracker.maybe_run_window(cfg)
    assert res.ran  # 窗口正常跑（不锁死生命周期 op），但…
    assert res.apply is not None and res.apply.opens == 0  # …幻觉 open 被丢
    assert res.apply.skipped >= 1
    with fts.cursor() as conn:
        assert wt_store.list_threads(conn) == []  # 没有幻觉零交集工作线
        assert wt_store.pending_queue(conn) == []  # 队列已消化，不会反复重试


def test_recall_only_window_still_completes_existing_thread(cfg, fake_llm):
    """召回窗口不锁死生命周期 op：对既有线的 complete/attach/progress 仍生效.

    新建线才幻觉，收尾/完成既有线是合法的——无活动段的"收尾" session 带完成证据
    时，必须能 complete 既有线（否则 golden self-check 的收尾步骤会被误挡）。
    """
    from persome.workthread.model import WorkThread

    with fts.cursor() as conn:
        wt_store.insert_thread(
            conn,
            WorkThread(
                id="",
                title="意图识别链路优化",
                status="active",
                first_seen="2026-06-12T09:00",
                last_active="2026-06-12T10:00",
            ),
        )
        tid = wt_store.list_threads(conn)[0].id
        _enqueue_recall_only(conn, 5)
    fake_llm.set_default(
        "thread_tracker",
        _ops_resp([{"op": "complete", "thread_id": tid, "evidence_quote": "发了，搞定"}]),
    )
    res = tracker.maybe_run_window(cfg)
    assert res.ran and res.apply is not None and res.apply.completes == 1
    with fts.cursor() as conn:
        assert wt_store.get_thread(conn, tid).status == "done"  # 既有线照常完成


def test_mixed_window_still_runs_recall_aids_naming(cfg, fake_llm):
    """召回不一刀切：窗口里有真实活动段时仍正常折叠，召回摘要降级为命名背景."""
    fake_llm.set_default(
        "thread_tracker",
        _ops_resp(
            [
                {
                    "op": "open",
                    "title": "意图识别链路优化",
                    "origin_type": "self_initiated",
                    "spans": [["10:00", "10:30"]],
                    "confidence": 0.8,
                }
            ]
        ),
    )
    with fts.cursor() as conn:
        _enqueue(conn, 3)  # 真实活动段
        _enqueue_recall_only(conn, 2)  # 召回摘要
    res = tracker.maybe_run_window(cfg)
    assert res.ran  # 有真实活动 → 不被门挡住
    assert len(fake_llm.calls) >= 1
    user_text = fake_llm.calls[0]["messages"][1]["content"]
    # 召回摘要被标进"非活动证据"块，仅供命名/归类
    assert "①ʹ" in user_text and "仅供命名/归类" in user_text
    assert "central: 用户在工具选型" in user_text  # 召回内容仍在场（辅助命名）
    with fts.cursor() as conn:
        assert len(wt_store.list_threads(conn)) == 1  # 正常折叠出一条真实线


# ─── H2 分歧探针 ─────────────────────────────────────────────────────────────


def test_ops_signature_and_disagree():
    attach_a = ThreadOp.from_dict({"op": "attach", "thread_id": "t1"})
    attach_b = ThreadOp.from_dict({"op": "attach", "thread_id": "t2"})
    none_op = ThreadOp.from_dict({"op": "none"})
    assert not tracker.ops_disagree([attach_a], [attach_a])
    assert tracker.ops_disagree([attach_a], [attach_b])
    assert not tracker.ops_disagree([none_op], [none_op])  # 双方都说没活 = 一致
    # 同名 open 的两次判断 = 一致
    open_a = ThreadOp.from_dict({"op": "open", "title": "意图识别优化"})
    open_b = ThreadOp.from_dict({"op": "open", "title": "意图识别优化"})
    assert not tracker.ops_disagree([open_a], [open_b])


def test_disagreement_downweights_and_queues_label(cfg, fake_llm):
    primary = _ops_resp(
        [
            {
                "op": "open",
                "title": "意图识别链路优化",
                "origin_actor": "Kevin",
                "spans": [["10:00", "10:30"]],
                "confidence": 0.8,
            }
        ]
    )
    probe = _ops_resp([{"op": "none"}])
    fake_llm.add_script(
        "thread_tracker",
        [_resp(primary), _resp(probe)],
    )
    with fts.cursor() as conn:
        _enqueue(conn, 5)
    res = tracker.maybe_run_window(cfg)
    assert res.ran and res.disagreement
    with fts.cursor() as conn:
        t = wt_store.list_threads(conn)[0]
        assert t.confidence == pytest.approx(0.8 * 0.9)
        queue = wt_store.pending_label_queue(conn)
        assert len(queue) == 1 and queue[0]["source"] == "disagreement"
        row = conn.execute(
            "SELECT disagreement FROM workthread_ticks ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row[0] == 1


def test_probe_off_single_call(cfg, fake_llm):
    cfg.thread_tracker.disagreement_probe = False
    fake_llm.set_default("thread_tracker", _ops_resp([{"op": "none"}]))
    with fts.cursor() as conn:
        _enqueue(conn, 5)
    res = tracker.maybe_run_window(cfg)
    assert res.ran and not res.disagreement
    assert len(fake_llm.calls) == 1


def _resp(text: str):
    from persome.writer.llm import _build_response

    return _build_response(text)


# ─── churn 冻结（spec §七 遥测动作） ──────────────────────────────────────────


def test_churn_freeze_above_threshold(ac_root):
    with fts.cursor() as conn:
        # 7 天内 12 attach、5 open → churn ≈ 0.42 > 0.3，达到最小分母
        now = datetime.now().isoformat(timespec="minutes")
        wt_store.record_tick(
            conn,
            ts=now,
            window_id="w",
            sessions=5,
            opens=5,
            attaches=12,
            revives=0,
            completes=0,
            merges=0,
        )
        assert wt_store.maybe_freeze_on_churn(conn) is True
        assert wt_store.is_open_frozen(conn)
        wt_store.unfreeze_open(conn)
        assert not wt_store.is_open_frozen(conn)


def test_churn_small_denominator_never_freezes(ac_root):
    with fts.cursor() as conn:
        now = datetime.now().isoformat(timespec="minutes")
        wt_store.record_tick(
            conn,
            ts=now,
            window_id="w",
            sessions=1,
            opens=1,
            attaches=2,
            revives=0,
            completes=0,
            merges=0,
        )
        assert wt_store.maybe_freeze_on_churn(conn) is False


def test_churn_freeze_kill_switch_clears_stale_freeze(ac_root):
    # freeze_on_churn=False is the kill-switch: it must NEVER freeze, and it must
    # also clear a stale freeze an earlier over-aggressive threshold left behind,
    # so the tracker recovers (mints new threads again) without a manual unfreeze.
    with fts.cursor() as conn:
        now = datetime.now().isoformat(timespec="minutes")
        wt_store.record_tick(
            conn,
            ts=now,
            window_id="w",
            sessions=5,
            opens=5,
            attaches=12,  # churn ≈ 0.42 > 0.3 — would freeze under the default guard
            revives=0,
            completes=0,
            merges=0,
        )
        wt_store.set_state(conn, "frozen_open", "1")  # simulate a stale freeze
        assert wt_store.is_open_frozen(conn)

        assert wt_store.maybe_freeze_on_churn(conn, freeze_on_churn=False) is False
        assert not wt_store.is_open_frozen(conn)  # stale freeze cleared


def test_churn_freeze_threshold_override(ac_root):
    # A raised threshold lets an active-dev workflow (high opens/attaches) keep
    # minting threads instead of false-freezing.
    with fts.cursor() as conn:
        now = datetime.now().isoformat(timespec="minutes")
        wt_store.record_tick(
            conn,
            ts=now,
            window_id="w",
            sessions=5,
            opens=5,
            attaches=12,  # churn ≈ 0.42
            revives=0,
            completes=0,
            merges=0,
        )
        # default threshold 0.3 freezes; 0.5 lets it through
        assert wt_store.maybe_freeze_on_churn(conn, threshold=0.5) is False
        assert not wt_store.is_open_frozen(conn)


def test_stats_shape(ac_root):
    with fts.cursor() as conn:
        s = wt_store.stats(conn)
        assert {"thread_churn", "revive_rate", "disagreement_rate", "frozen_open"} <= set(s)

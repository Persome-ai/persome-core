"""S3 纠错口 + H1 标签工厂验收：闭集纠错、confidence 回灌、标签铸造、日终一屏."""

from __future__ import annotations

from datetime import datetime

import pytest

from persome.store import fts
from persome.workthread import review as wt_review
from persome.workthread import store as wt_store
from persome.workthread.model import Binding, WorkThread


@pytest.fixture
def conn(ac_root):
    with fts.cursor() as c:
        yield c


def _seed(conn, **kw) -> WorkThread:
    defaults = dict(
        id="",
        title="Kevin 交办：意图识别优化",
        status="active",
        origin_actor="Kevin",
        first_seen="2026-06-10T09:00",
        last_active=datetime.now().isoformat(timespec="minutes"),
        confidence=0.7,
    )
    defaults.update(kw)
    t = WorkThread(**defaults)
    wt_store.insert_thread(conn, t)
    return t


def test_confirm_raises_confidence_and_mints_label(conn):
    t = _seed(conn)
    res = wt_review.apply_correction(conn, thread_id=t.id, action="confirm")
    assert res["ok"]
    assert wt_store.get_thread(conn, t.id).confidence == pytest.approx(0.75)
    day = datetime.now().strftime("%Y-%m-%d")
    labels = wt_store.labels_for_day(conn, day)
    assert len(labels) == 1 and labels[0]["action"] == "confirm"


def test_not_this_supersedes_and_lowers_confidence(conn):
    t = _seed(conn)
    wt_review.apply_correction(conn, thread_id=t.id, action="not_this")
    got = wt_store.get_thread(conn, t.id)
    assert got.status == "superseded"
    assert got.confidence == pytest.approx(0.55)
    assert got.user_corrected == 1


def test_rename(conn):
    t = _seed(conn)
    res = wt_review.apply_correction(
        conn, thread_id=t.id, action="rename", new_title="意图识别链路打通"
    )
    assert res["ok"]
    assert wt_store.get_thread(conn, t.id).title == "意图识别链路打通"
    assert not wt_review.apply_correction(conn, thread_id=t.id, action="rename")["ok"]


def test_merge_correction_pinned_protection(conn):
    a = _seed(conn, title="线 A", pinned=True)
    b = _seed(conn, title="线 B", origin_actor="self")
    res = wt_review.apply_correction(conn, thread_id=a.id, action="merge", into_id=b.id)
    assert not res["ok"]  # pinned source 不可被吸收
    res = wt_review.apply_correction(conn, thread_id=b.id, action="merge", into_id=a.id)
    assert res["ok"]
    assert wt_store.get_thread(conn, b.id).status == "superseded"


def test_pin_immunizes(conn):
    t = _seed(conn, confidence=0.5)
    wt_review.apply_correction(conn, thread_id=t.id, action="pin")
    got = wt_store.get_thread(conn, t.id)
    assert got.pinned and got.confidence >= 0.9


def test_unknown_action_rejected_closed_set(conn):
    t = _seed(conn)
    assert not wt_review.apply_correction(conn, thread_id=t.id, action="delete")["ok"]


def test_correction_consumes_needs_label_queue(conn):
    """H2 分歧队列的样本被 H1 标注动作消耗（10.4 闭环）。"""
    t = _seed(conn)
    day = datetime.now().strftime("%Y-%m-%d")
    wt_store.add_label(
        conn, day=day, thread_id=t.id, action="needs_label", payload={}, source="disagreement"
    )
    assert len(wt_store.pending_label_queue(conn)) == 1
    wt_review.apply_correction(conn, thread_id=t.id, action="confirm")
    assert wt_store.pending_label_queue(conn) == []


def test_day_review_minutes_and_ordering(conn):
    day = datetime.now().strftime("%Y-%m-%d")
    big = _seed(conn, title="大头线")
    small = _seed(conn, title="小活线", origin_actor="self", status="background")
    big.bindings = [Binding(window_id=f"{day}T11:00", spans=[["09:00", "13:12"]])]
    small.bindings = [Binding(window_id=f"{day}T11:00", spans=[["14:00", "15:06"]])]
    wt_store.save_thread(conn, big)
    wt_store.save_thread(conn, small)
    rv = wt_review.build_day_review(conn)
    assert [line["title"] for line in rv.lines] == ["大头线", "小活线"]
    assert rv.lines[0]["day_minutes"] == 252  # 4.2h
    assert rv.lines[1]["day_minutes"] == 66  # 1.1h


def test_current_work_context_shape(conn):
    t = _seed(conn, total_active_minutes=192, approximate=True)
    _seed(conn, title="后台线", status="background", origin_actor="self")
    ctx = wt_review.current_work_context(conn)
    assert ctx["active_thread"]["thread_id"] == t.id
    assert ctx["active_thread"]["total_minutes"] == 192
    assert ctx["active_thread"]["approximate"] is True  # approximate 标记透传
    assert ctx["active_thread"]["origin"]["actor"] == "Kevin"
    assert len(ctx["background_threads"]) == 1
    assert "thread_churn" in ctx["stats"]


def test_export_day_fixture_shape(conn):
    day = datetime.now().strftime("%Y-%m-%d")
    t = _seed(conn)
    wt_store.enqueue_session(
        conn,
        session_id="s1",
        summary="干活",
        sub_tasks=["[10:00-10:30, Cursor] 改代码"],
        start_time=f"{day}T10:00",
        end_time=f"{day}T10:30",
    )
    wt_review.apply_correction(conn, thread_id=t.id, action="confirm")
    fixture = wt_review.export_day_fixture(conn, day=day)
    assert fixture["day"] == day
    assert len(fixture["sessions"]) == 1
    assert fixture["labels"][0]["action"] == "confirm"

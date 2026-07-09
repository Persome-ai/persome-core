"""S1 acceptance (spec §七): 六 op 语义、查重/复活、滞回竞争、spans 计时与分摊
全部单测复现 — zero LLM."""

from __future__ import annotations

from datetime import datetime

import pytest

from persome.store import fts
from persome.workthread import executor, projection
from persome.workthread import store as wt_store
from persome.workthread.model import ThreadOp, WorkThread


@pytest.fixture
def conn(ac_root):
    with fts.cursor() as c:
        yield c


def _op(op: str, **kw) -> ThreadOp:
    parsed = ThreadOp.from_dict({"op": op, **kw})
    assert parsed is not None
    return parsed


def _seed(conn, title="Kevin 交办：意图识别链路优化", status="background", **kw) -> WorkThread:
    thread = WorkThread(
        id="",
        title=title,
        status=status,
        first_seen="2026-06-10T09:00",
        last_active=kw.pop("last_active", "2026-06-10T09:00"),
        origin_actor=kw.pop("origin_actor", "Kevin"),
        **kw,
    )
    wt_store.insert_thread(conn, thread)
    return thread


# ─── spans 时间账契约（F3） ───────────────────────────────────────────────────


def test_span_minutes_basic_and_malformed():
    assert len(executor._span_minutes([["10:00", "10:30"]])) == 30
    # end <= start / malformed → dropped, never negative or fabricated
    assert executor._span_minutes([["10:30", "10:00"]]) == set()
    assert executor._span_minutes([["junk", "10:00"]]) == set()
    assert executor._span_minutes([["25:00", "26:00"]]) == set()


def test_attach_credits_span_minutes(conn):
    t = _seed(conn)
    executor.apply_ops(
        conn,
        [_op("attach", thread_id=t.id, spans=[["20:11", "20:25"], ["21:02", "21:40"]])],
        window_id="2026-06-12T21:00",
    )
    got = wt_store.get_thread(conn, t.id)
    assert got.total_active_minutes == 14 + 38
    assert got.approximate is False


def test_attach_without_spans_counts_zero_minutes(conn):
    """无 spans 的 attach 合法但不计时长（宁可漏记不虚报）。"""
    t = _seed(conn)
    executor.apply_ops(conn, [_op("attach", thread_id=t.id)], window_id="w1")
    got = wt_store.get_thread(conn, t.id)
    assert got.total_active_minutes == 0
    assert got.last_active  # bumped


def test_overlapping_spans_split_evenly_and_marked_approximate(conn):
    """同窗口多线 spans 重叠 → 重叠部分按线数均摊 + approximate 标记。"""
    a = _seed(conn, title="线 A：意图识别优化")
    b = _seed(conn, title="线 B：周报草稿", origin_actor="self")
    executor.apply_ops(
        conn,
        [
            _op("attach", thread_id=a.id, spans=[["10:00", "10:30"]]),
            _op("attach", thread_id=b.id, spans=[["10:00", "10:30"]]),
        ],
        window_id="w1",
    )
    ga, gb = wt_store.get_thread(conn, a.id), wt_store.get_thread(conn, b.id)
    assert ga.total_active_minutes == 15
    assert gb.total_active_minutes == 15
    assert ga.approximate and gb.approximate


def test_attach_idempotent_replay_same_window(conn):
    """attach 幂等：同窗口重放替换 binding，分钟数不双计。"""
    t = _seed(conn)
    for _ in range(2):
        executor.apply_ops(
            conn,
            [_op("attach", thread_id=t.id, spans=[["10:00", "10:30"]])],
            window_id="2026-06-12T11:00",
        )
    got = wt_store.get_thread(conn, t.id)
    assert got.total_active_minutes == 30
    assert len([b for b in got.bindings if b.window_id == "2026-06-12T11:00"]) == 1


# ─── open 查重闸 + 复活（F1/F8） ──────────────────────────────────────────────


def test_open_creates_new_thread_with_origin(conn):
    res = executor.apply_ops(
        conn,
        [
            _op(
                "open",
                title="Kevin 交办：意图识别链路优化",
                goal="把快慢路打通",
                origin_type="assignment",
                origin_actor="Kevin",
                origin_quote="这个意图识别的活你来跟进一下",
                spans=[["09:00", "09:40"]],
                confidence=0.8,
            )
        ],
        window_id="w1",
    )
    assert res.opens == 1
    threads = wt_store.open_threads(conn)
    assert len(threads) == 1
    t = threads[0]
    assert t.origin_type == "assignment"
    assert t.origin_actor == "Kevin"
    assert t.origin_evidence[0]["quote"].startswith("这个意图识别")
    assert t.total_active_minutes == 40


def test_open_dedup_converts_to_attach_on_open_thread(conn):
    """对既有 open 线发 open → 转 attach，不开孪生。"""
    t = _seed(conn, title="意图识别链路优化")
    res = executor.apply_ops(
        conn,
        [
            _op(
                "open",
                title="Kevin 交办：意图识别优化",
                origin_actor="Kevin",
                spans=[["10:00", "10:10"]],
            )
        ],
        window_id="w1",
    )
    assert res.opens == 0 and res.attaches == 1
    assert len(wt_store.list_threads(conn)) == 1
    assert wt_store.get_thread(conn, t.id).total_active_minutes == 10


def test_open_dedup_revives_dormant_thread(conn):
    """全历史查重命中非 open 线 → 自动转 attach 并复活（spec 执行器规则 2）。"""
    t = _seed(conn, title="意图识别链路优化", status="stale")
    res = executor.apply_ops(
        conn,
        [_op("open", title="继续意图识别链路优化", origin_actor="Kevin")],
        window_id="w2",
    )
    assert res.revives == 1
    got = wt_store.get_thread(conn, t.id)
    assert got.status in ("active", "background")


def test_attach_to_dormant_thread_revives(conn):
    """输入③接球区：LLM 直接对休眠线 id 发 attach → executor 复活它。"""
    t = _seed(conn, title="周报模板重构", status="done")
    res = executor.apply_ops(
        conn, [_op("attach", thread_id=t.id, spans=[["14:00", "14:20"]])], window_id="w3"
    )
    assert res.revives == 1
    assert wt_store.get_thread(conn, t.id).status in ("active", "background")


def test_open_different_actor_does_not_fold(conn):
    _seed(conn, title="意图识别链路优化", origin_actor="Kevin")
    res = executor.apply_ops(
        conn,
        [_op("open", title="意图识别链路优化二期", origin_actor="Alice")],
        window_id="w1",
    )
    assert res.opens == 1
    assert len(wt_store.list_threads(conn)) == 2


# ─── 滞回 active 竞争（F2） ───────────────────────────────────────────────────


def test_first_window_top_thread_becomes_active(conn):
    a = _seed(conn, title="线 A 大头任务")
    b = _seed(conn, title="线 B 小活", origin_actor="self")
    executor.apply_ops(
        conn,
        [
            _op("attach", thread_id=a.id, spans=[["10:00", "10:50"]]),
            _op("attach", thread_id=b.id, spans=[["10:50", "11:00"]]),
        ],
        window_id="w1",
    )
    assert wt_store.get_thread(conn, a.id).status == "active"
    assert wt_store.get_thread(conn, b.id).status == "background"


def test_hysteresis_incumbent_survives_under_60_percent(conn):
    """候选未达窗口 span 时长 60% → 维持现任（7.6 分钟噪声不翻转 active）。"""
    incumbent = _seed(conn, title="现任主线", status="active")
    challenger = _seed(conn, title="挑战者支线", origin_actor="self")
    executor.apply_ops(
        conn,
        [
            _op("attach", thread_id=incumbent.id, spans=[["10:00", "10:25"]]),  # 25min
            _op("attach", thread_id=challenger.id, spans=[["10:25", "10:55"]]),  # 30min ≈55%
        ],
        window_id="w1",
    )
    assert wt_store.get_thread(conn, incumbent.id).status == "active"
    assert wt_store.get_thread(conn, challenger.id).status == "background"


def test_hysteresis_takeover_at_60_percent(conn):
    incumbent = _seed(conn, title="现任主线", status="active")
    challenger = _seed(conn, title="挑战者支线", origin_actor="self")
    executor.apply_ops(
        conn,
        [
            _op("attach", thread_id=incumbent.id, spans=[["10:00", "10:20"]]),  # 20min
            _op("attach", thread_id=challenger.id, spans=[["10:20", "11:00"]]),  # 40min ≥60%
        ],
        window_id="w1",
    )
    assert wt_store.get_thread(conn, challenger.id).status == "active"
    assert wt_store.get_thread(conn, incumbent.id).status == "background"


def test_window_without_spans_keeps_incumbent(conn):
    incumbent = _seed(conn, title="现任主线", status="active")
    other = _seed(conn, title="别的线", origin_actor="self")
    executor.apply_ops(conn, [_op("progress", thread_id=other.id, note="改了点")], window_id="w1")
    assert wt_store.get_thread(conn, incumbent.id).status == "active"


# ─── complete / merge / progress ─────────────────────────────────────────────


def test_complete_requires_evidence_quote(conn):
    """Prompt 规则 5：不活跃永远不是完成；无证据的 complete 被拒。"""
    t = _seed(conn)
    res = executor.apply_ops(conn, [_op("complete", thread_id=t.id)], window_id="w1")
    assert res.completes == 0 and res.skipped == 1
    assert wt_store.get_thread(conn, t.id).status == "background"

    res = executor.apply_ops(
        conn,
        [_op("complete", thread_id=t.id, evidence_quote="PR merged，发了")],
        window_id="w2",
    )
    assert res.completes == 1
    assert wt_store.get_thread(conn, t.id).status == "done"


def test_merge_absorbs_minutes_and_supersedes(conn):
    a = _seed(conn, title="线 A")
    b = _seed(conn, title="线 B", origin_actor="self")
    executor.apply_ops(
        conn, [_op("attach", thread_id=a.id, spans=[["10:00", "10:30"]])], window_id="w1"
    )
    res = executor.apply_ops(conn, [_op("merge", from_id=a.id, into_id=b.id)], window_id="w2")
    assert res.merges == 1
    ga, gb = wt_store.get_thread(conn, a.id), wt_store.get_thread(conn, b.id)
    assert ga.status == "superseded"  # 闭集第五态（F9）
    assert gb.total_active_minutes == 30


def test_merge_refuses_pinned_source(conn):
    a = _seed(conn, title="线 A 已 pin", pinned=True)
    b = _seed(conn, title="线 B", origin_actor="self")
    res = executor.apply_ops(conn, [_op("merge", from_id=a.id, into_id=b.id)], window_id="w1")
    assert res.merges == 0
    assert wt_store.get_thread(conn, a.id).status == "background"


def test_progress_appends_note(conn):
    t = _seed(conn)
    executor.apply_ops(conn, [_op("progress", thread_id=t.id, note="跑通了 demo")], window_id="w1")
    got = wt_store.get_thread(conn, t.id)
    assert any("跑通了 demo" in n for n in got.progress_notes)


def test_unknown_op_dropped_by_parser():
    assert ThreadOp.from_dict({"op": "delete", "thread_id": "x"}) is None
    assert executor.parse_ops({"ops": [{"op": "explode"}, {"op": "none"}]})[0].op == "none"


def test_bad_op_never_breaks_batch(conn):
    """规则 4：单 op 失败只 log，其余照常执行。"""
    t = _seed(conn)
    res = executor.apply_ops(
        conn,
        [
            _op("attach", thread_id="nonexistent"),
            _op("attach", thread_id=t.id, spans=[["10:00", "10:05"]]),
        ],
        window_id="w1",
    )
    assert res.skipped == 1 and res.attaches == 1


# ─── stale 收割 + freeze ─────────────────────────────────────────────────────


def test_harvest_stale_after_30_days_pinned_exempt(conn):
    old = _seed(conn, title="一个月没碰的线", last_active="2026-05-01T10:00")
    pinned = _seed(conn, title="pin 住的老线", last_active="2026-05-01T10:00", pinned=True)
    fresh = _seed(conn, title="昨天还在干的线", last_active="2026-06-11T10:00")
    harvested = executor.harvest_stale(conn, now=datetime(2026, 6, 12, 23, 55))
    assert harvested == 1
    assert wt_store.get_thread(conn, old.id).status == "stale"
    assert wt_store.get_thread(conn, pinned.id).status == "background"
    assert wt_store.get_thread(conn, fresh.id).status == "background"


def test_frozen_open_drops_open_but_allows_attach(conn):
    t = _seed(conn)
    wt_store.set_state(conn, "frozen_open", "1")
    res = executor.apply_ops(
        conn,
        [
            _op("open", title="一条全新的线", origin_actor="Bob"),
            _op("attach", thread_id=t.id, spans=[["10:00", "10:10"]]),
        ],
        window_id="w1",
    )
    assert res.opens == 0 and res.attaches == 1
    assert len(wt_store.list_threads(conn)) == 1
    # ...但查重命中既有线的 open 仍可走 attach+复活（不是新开线）
    res2 = executor.apply_ops(
        conn,
        [_op("open", title="Kevin 交办：意图识别链路优化", origin_actor="Kevin")],
        window_id="w2",
    )
    assert res2.attaches == 1 and res2.opens == 0


# ─── 投影 ─────────────────────────────────────────────────────────────────────


def test_projection_lands_in_thread_md(conn):
    executor.apply_ops(
        conn,
        [_op("open", title="投影测试线", origin_actor="Kevin", spans=[["09:00", "09:10"]])],
        window_id="w1",
    )
    t = wt_store.list_threads(conn)[0]
    row = conn.execute(
        "SELECT path, content FROM entries WHERE path = ?", (f"thread-{t.id}.md",)
    ).fetchone()
    assert row is not None
    assert "投影测试线" in row[1]


def test_projection_failure_never_breaks_executor(conn, monkeypatch):
    monkeypatch.setattr(projection.entries_mod, "append_entry", lambda *a, **k: 1 / 0)
    res = executor.apply_ops(
        conn, [_op("open", title="投影炸了也要落库", origin_actor="x")], window_id="w1"
    )
    assert res.opens == 1
    assert len(wt_store.list_threads(conn)) == 1


# ─── 查重归一 ─────────────────────────────────────────────────────────────────


def test_title_similarity_cjk_bigrams():
    assert wt_store.title_similarity("意图识别链路优化", "意图识别优化") >= 0.5
    assert wt_store.title_similarity("意图识别链路优化", "周报模板重构") < 0.5


# ─── 新一轮 auto-review 回归（#573 / #569 / #588） ────────────────────────────


def test_find_duplicate_excludes_superseded(conn):
    """#573: superseded（merge 单向吸收后的终态）不进复活候选——否则一个 open op
    命中它即悄悄撤销那次 merge、重生孪生线、分钟双计。"""
    _seed(conn, title="意图识别链路优化", status="superseded")
    got = wt_store.find_duplicate(conn, title="意图识别链路优化", origin_actor="Kevin")
    assert got is None  # 终态被排除，不复活


def test_find_duplicate_prefers_open_over_higher_sim_nonopen(conn):
    """#569: Open 线优先于非 open（复活是兜底）——一条相似度更高的 done 线不应盖过
    一条相似度略低但仍过阈值的 open 线。"""
    query = "Kevin 交办 意图识别 链路 优化 重构"
    _seed(conn, title=query, status="done")  # 完全匹配 sim=1.0
    open_t = _seed(conn, title="Kevin 交办 意图识别 链路 优化", status="background")  # 少一词
    got = wt_store.find_duplicate(conn, title=query, origin_actor="Kevin")
    assert got is not None
    assert got.id == open_t.id  # open 优先，尽管 done 线相似度更高
    assert got.status == "background"


def test_binding_from_dict_skips_malformed_spans():
    """#588: 非长度-2 的 span（截断/多余/迁移脏行）应被跳过，而不是硬解包抛
    ValueError 把整张表读取打崩。"""
    from persome.workthread.model import Binding

    b = Binding.from_dict(
        {
            "window_id": "2026-06-12T21:00",
            "session_ids": ["s1"],
            "spans": [
                ["10:00", "10:30"],  # good
                ["10:00"],  # 太短
                ["10:00", "10:30", "x"],  # 太长
                "junk",  # 非 list
            ],
        }
    )
    assert b.spans == [["10:00", "10:30"]]  # 只保留合法对，不抛错


def test_duplicate_attach_same_thread_accumulates_not_overwrites(conn):
    """#574: 同窗口两个指向同一 thread 的 attach 必须累积到同一可变实例,第二次
    save 不能整行覆盖、丢掉第一个 attach 的改动（progress_notes）。"""
    t = _seed(conn)
    executor.apply_ops(
        conn,
        [
            _op("attach", thread_id=t.id, spans=[["20:00", "20:10"]], note="子任务A"),
            _op("attach", thread_id=t.id, spans=[["20:00", "20:10"]], note="子任务B"),
        ],
        window_id="2026-06-12T20:30",
    )
    got = wt_store.get_thread(conn, t.id)
    notes = " ".join(got.progress_notes)
    assert "子任务A" in notes and "子任务B" in notes  # 两条 attach 的 note 都在


def test_completed_thread_not_selected_as_active_winner(conn):
    """#565: 同窗口内 attach 后 complete 的线（已 done）不能被选为 active winner——
    否则把 incumbent 降级却又无法提升 done 线,留下零-active 空档。"""
    incumbent = _seed(conn, title="既有 active 线", status="active")
    t = _seed(conn, title="本窗口做完的线", status="background")
    res = executor.apply_ops(
        conn,
        [
            _op("attach", thread_id=t.id, spans=[["20:00", "21:00"]]),  # 占绝大多数分钟
            _op("complete", thread_id=t.id, evidence_quote="搞定了"),
        ],
        window_id="2026-06-12T21:00",
    )
    assert wt_store.get_thread(conn, t.id).status == "done"
    assert res.active_id != t.id  # done 线不当 active
    assert wt_store.get_thread(conn, incumbent.id).status == "active"  # incumbent 未被降级

"""S0/S3 recall 验收：工作线层位次/独立预算/置信闸 + 近期活动层 + 挤出遥测."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from persome.intent import recall
from persome.store import entries as entries_mod
from persome.store import fts, recall_budget_ticks
from persome.workthread import store as wt_store
from persome.workthread.model import WorkThread


@pytest.fixture
def conn(ac_root):
    with fts.cursor() as c:
        yield c


def _thread(
    conn, *, status="active", confidence=0.9, title="Kevin 交办：意图识别优化"
) -> WorkThread:
    t = WorkThread(
        id="",
        title=title,
        status=status,
        origin_actor="Kevin",
        first_seen="2026-06-10T09:00",
        last_active="2026-06-12T10:00",
        total_active_minutes=192,
        confidence=confidence,
        progress_notes=["[2026-06-12T10:00] 快路打通了"],
    )
    wt_store.insert_thread(conn, t)
    return t


def test_workthread_layer_injected_after_schema_prior(conn):
    _thread(conn)
    out = recall.assemble_background(
        conn,
        scope="session-x",
        hints=[],
        schema_prior=["用户习惯早上写代码"],
        workthread_chars=200,
    )
    assert "# 当前工作线" in out
    assert "[进行中] Kevin 交办：意图识别优化" in out
    assert "192min" in out and "Kevin 交办" in out
    # 位次：schema_prior 之后（spec §六-1，最高优先有消融背书，不抢）
    assert out.index("# 用户惯性先验") < out.index("# 当前工作线")


def test_workthread_layer_confidence_gate(conn):
    _thread(conn, confidence=0.4)
    out = recall.assemble_background(conn, scope="session-x", hints=[], workthread_chars=200)
    assert "# 当前工作线" not in out  # confidence < 0.6 不注入


def test_workthread_layer_off_by_default_param(conn):
    _thread(conn)
    out = recall.assemble_background(conn, scope="session-x", hints=[])
    assert "# 当前工作线" not in out  # workthread_chars=0 → byte-identical 路径


def test_workthread_independent_budget_never_squeezes_main(conn):
    """独立预算：工作线层的注入不挤占主预算（挤出率不回归的结构性保证）。"""
    _thread(conn)
    # 一个刚好够 schema_prior 的主预算——若工作线层挤占主预算，prior 必被拒。
    prior = ["x" * 50]
    out = recall.assemble_background(
        conn,
        scope="session-x",
        hints=[],
        schema_prior=prior,
        max_chars=70,
        workthread_chars=200,
    )
    assert "# 用户惯性先验" in out
    assert "# 当前工作线" in out
    # 遥测：workthread 桶有 admitted 记录
    stats = recall_budget_ticks.stats(conn)
    assert stats["by_layer"]["workthread"]["admitted"] >= 1
    assert stats["by_layer"]["workthread"]["rejected"] == 0


def test_workthread_layer_at_most_one_background(conn):
    _thread(conn, status="active", title="主线")
    _thread(conn, status="background", title="后台线一")
    _thread(conn, status="background", title="后台线二")
    out = recall.assemble_background(conn, scope="s", hints=[], workthread_chars=400)
    assert out.count("[后台]") == 1  # active 一条 + background 至多一条


# ---------------------------------------------------------------------------
# #623 口径修复：recall_budget_ticks 须把工作线独立预算纳入 max_chars/used 口径
# ---------------------------------------------------------------------------


def test_tick_max_chars_includes_workthread_independent_budget(conn):
    """真实装配上限 = 主预算 + 工作线独立预算（#623 根因 recall.py:312-320）.

    工作线走独立预算（默认 200），它叠加在主预算之上，真实装配上限是
    ``max_chars + workthread_chars``。tick 若只记 ``max_chars`` 会系统性低报
    容量上限——"是否把 max_chars 抬到 2400" 的决策依据被压低。
    """
    _thread(conn)
    recall.assemble_background(
        conn,
        scope="session-x",
        hints=[],
        max_chars=1200,
        workthread_chars=200,
    )
    conn.row_factory = __import__("sqlite3").Row
    row = conn.execute("SELECT * FROM recall_budget_ticks ORDER BY id DESC LIMIT 1").fetchone()
    assert row["max_chars"] == 1400  # 1200 主 + 200 工作线独立预算


def test_tick_used_includes_workthread_admitted_chars(conn):
    """``used`` 必须含工作线已注入的字符（#623 根因 recall.py:360-367）.

    工作线镜像层 ``local.add(line, layer="")`` 不回写 ``budget.used``，导致
    ``used`` 漏掉工作线注入的字符。实证不变量：``used == Σ(all admitted_chars)``。
    """
    import json

    _thread(conn)
    recall.assemble_background(
        conn,
        scope="session-x",
        hints=[],
        max_chars=1200,
        workthread_chars=200,
    )
    conn.row_factory = __import__("sqlite3").Row
    row = conn.execute("SELECT * FROM recall_budget_ticks ORDER BY id DESC LIMIT 1").fetchone()
    layers = json.loads(row["layers"])
    assert layers["workthread"]["admitted_chars"] > 0  # 工作线确实注入了字符
    # used 含工作线注入的字符——跨所有层口径自洽
    assert row["used"] == sum(b["admitted_chars"] for b in layers.values())


def test_tick_mirrors_workthread_rejected(conn):
    """工作线独立预算的 rejected/rejected_chars 也要镜像（#623 finding M）.

    当前镜像层只回写 admitted，工作线层被独立预算挤出时 rejected 永远显示 0，
    挤出率口径失真。给一个极小的独立预算把工作线挤出，rejected 必须 > 0。
    """
    import json

    _thread(conn)
    recall.assemble_background(
        conn,
        scope="session-x",
        hints=[],
        max_chars=1200,
        workthread_chars=5,  # 远小于一条工作线，必被挤出
    )
    conn.row_factory = __import__("sqlite3").Row
    row = conn.execute("SELECT * FROM recall_budget_ticks ORDER BY id DESC LIMIT 1").fetchone()
    layers = json.loads(row["layers"])
    assert layers["workthread"]["rejected"] >= 1
    assert layers["workthread"]["rejected_chars"] > 0
    assert row["squeezed"] == 1  # 工作线被挤出 → 这次 recall 是 squeezed


def test_workthread_does_not_squeeze_downstream_layers(conn):
    """口径修复不得改变 admission：工作线层注入在主层之前，绝不能因为它把
    ``budget.used`` 抬高而让下游层（scene/behavior/fact/keyword/events）少装内容。

    review finding：旧修法在 ``_workthread_layer`` 里 ``budget.used += local.used``，
    而工作线层比 ``_recent_events_layer`` 等主层早跑，于是把 used 抬高、把下游
    挤出。这里用一个紧到刚好容纳 events 行的主预算，对比 带/不带 workthread_chars
    两种调用的输出——下游 events 层的装配必须一致（独立预算不挤占主层）。
    """
    day = datetime.now().strftime("%Y-%m-%d")
    entries_mod.create_file(conn, name=f"event-{day}.md", description="d", tags=["event"])
    entries_mod.append_entry(conn, name=f"event-{day}.md", content="近期活动一条", tags=["s"])
    _thread(conn)
    # 主预算只够 events 一行（events 行 = "[ts] 近期活动一条"，约 30+ 字符）。
    common = dict(scope="session-x", hints=[], recent_events_hours=48, max_chars=60)
    without_wt = recall.assemble_background(conn, **common)
    with_wt = recall.assemble_background(conn, **common, workthread_chars=200)
    # 不带工作线时 events 层应当装上（基线断言：预算确实够 events）。
    assert "# 近期活动（event-daily 摘要）" in without_wt
    # 带工作线时 events 层仍必须装上——工作线走独立预算，不挤占主预算。
    assert "# 近期活动（event-daily 摘要）" in with_wt
    assert "近期活动一条" in with_wt


def test_recent_events_layer(conn):
    day = datetime.now().strftime("%Y-%m-%d")
    entries_mod.create_file(conn, name=f"event-{day}.md", description="d", tags=["event"])
    entries_mod.append_entry(
        conn,
        name=f"event-{day}.md",
        content="**Session abc** (10:00–10:30)\n\ncontinued 意图识别优化\n- [10:00-10:30, Cursor] 改代码",
        tags=["session"],
    )
    out = recall.assemble_background(conn, scope="session-x", hints=[], recent_events_hours=48)
    assert "# 近期活动（event-daily 摘要）" in out
    assert "continued 意图识别优化" in out


def test_recent_events_layer_respects_window(conn):
    old_day = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
    entries_mod.create_file(conn, name=f"event-{old_day}.md", description="d", tags=["event"])
    # append_entry stamps NOW as the entry timestamp, so simulate an old entry by
    # rewriting the timestamp column directly (the layer filters on it).
    eid = entries_mod.append_entry(
        conn, name=f"event-{old_day}.md", content="十天前的活", tags=["session"]
    )
    old_ts = (datetime.now() - timedelta(days=10)).isoformat(timespec="minutes")
    conn.execute("UPDATE entries SET timestamp = ? WHERE id = ?", (old_ts, eid))
    conn.commit()
    out = recall.assemble_background(conn, scope="session-x", hints=[], recent_events_hours=48)
    assert "十天前的活" not in out


def test_recent_events_layer_lowest_priority_squeeze_is_visible(conn):
    """近期活动层排最后：预算耗尽时被拒并记进 events 桶（S0 验收量规）。"""
    day = datetime.now().strftime("%Y-%m-%d")
    entries_mod.create_file(conn, name=f"event-{day}.md", description="d", tags=["event"])
    entries_mod.append_entry(conn, name=f"event-{day}.md", content="活动 A" * 100, tags=["s"])
    out = recall.assemble_background(
        conn,
        scope="session-x",
        hints=[],
        schema_prior=["p" * 90],
        max_chars=100,
        recent_events_hours=48,
    )
    assert "# 近期活动" not in out
    stats = recall_budget_ticks.stats(conn)
    assert stats["by_layer"]["events"]["rejected"] >= 1

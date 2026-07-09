"""§1.4 — recall 的 evo_nodes 折叠（PR-7 后的唯一链折叠路径）。

契约（设计稿 §1.4 + PR-7 退役语义）：

- ``fold_superseded=False``（默认）→ legacy 未折叠输出**字节等价**：查询不引用
  evo_nodes，evo_nodes 表整个不存在也不影响输出。
- ``fold_superseded=True`` → 折叠子查询读 ``evo_nodes WHERE is_latest=1 AND
  status='active'``（scope=default，Q4）；trail 从双向指针渲染，``← [曾]``/
  ``← [精炼自]`` 语义、``_TRAIL_MAX_ANCESTORS``、60 字符截断全部保留。
- event- 条目（Q2 豁免，不在 evo_nodes）在折叠下仍按 ``superseded=0`` 列判定
  ——keyword 层命中 event- 条目不丢失（本文件钉死）。
- 冷启动（evo_nodes 缺表/为空 = backfill 未跑）→ 折叠退化到 ``superseded=0``
  派生列（P1 不变量证明的等价折叠），输出与 evo 折叠 byte-identical；不告警、
  不抛、不变空。
"""

from __future__ import annotations

import pytest

from persome.evomem import backfill
from persome.intent import recall
from persome.store import entries as entries_mod
from persome.store import fts


@pytest.fixture(autouse=True)
def _quiet_alerts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("persome.events.publish", lambda *a, **k: None)


def _seed(conn) -> dict[str, str]:
    """咖啡→茶→抹茶（最后一跳 refined-from）双跳链 + 第二条链 + 孤立条目 + event 条目。"""
    entries_mod.create_file(conn, name="user-preferences.md", description="p", tags=["t"])
    coffee = entries_mod.append_entry(
        conn, name="user-preferences.md", content="用户喝咖啡 beverage", tags=["t"]
    )
    tea = entries_mod.supersede_entry(
        conn,
        name="user-preferences.md",
        old_entry_id=coffee,
        new_content="用户喝茶 beverage",
        reason="口味变了",
        tags=["t"],
    )
    matcha = entries_mod.supersede_entry(
        conn,
        name="user-preferences.md",
        old_entry_id=tea,
        new_content="用户喝抹茶 beverage",
        reason="精炼",
        tags=["t"],
        refined_from=tea,
    )
    entries_mod.create_file(conn, name="project-x.md", description="x", tags=["t"])
    v1 = entries_mod.append_entry(
        conn, name="project-x.md", content="DeepSeek v1 beverage", tags=["t"]
    )
    v2 = entries_mod.supersede_entry(
        conn,
        name="project-x.md",
        old_entry_id=v1,
        new_content="DeepSeek v2 beverage",
        reason="updated",
        tags=["t"],
    )
    isolated = entries_mod.append_entry(
        conn, name="user-preferences.md", content="用户早起 sunrise", tags=["t"]
    )
    entries_mod.create_file(conn, name="event-2026-06-10.md", description="e", tags=[])
    event = entries_mod.append_entry(
        conn, name="event-2026-06-10.md", content="beverage event row", tags=[]
    )
    return {
        "coffee": coffee,
        "tea": tea,
        "matcha": matcha,
        "v1": v1,
        "v2": v2,
        "isolated": isolated,
        "event": event,
    }


def _seed_and_backfill() -> dict[str, str]:
    with fts.cursor() as conn:
        ids = _seed(conn)
    report = backfill.run_backfill()
    assert report.ok, (report.violations, report.heads_only_evo, report.heads_only_fts)
    return ids


# ── P0：fold off 字节等价 ─────────────────────────────────────────────────────


def test_fold_off_is_byte_identical_and_evo_free(ac_root) -> None:
    """默认（fold_superseded=False）== 显式 False；查询不引用 evo_nodes：
    把 evo_nodes 表整个删掉，off 路径输出一字不变。"""
    _seed_and_backfill()
    kwargs = dict(scope="", hints=["beverage", "sunrise"], per_hint=10)
    with fts.cursor() as conn:
        default = recall.assemble_background(conn, **kwargs)
        explicit_off = recall.assemble_background(conn, fold_superseded=False, **kwargs)
        conn.execute("DROP TABLE evo_nodes")
        without_table = recall.assemble_background(conn, **kwargs)
    assert default == explicit_off == without_table
    assert "喝咖啡" in default  # legacy un-folded：退役版本照常出现


# ── ON：evo 折叠行为 + 与冷启动退化折叠 byte-identical ───────────────────────


def test_evo_fold_returns_chain_heads_only(ac_root) -> None:
    """纯折叠（无 trail）：退役旧版折出、链头与孤立条目浮出。"""
    _seed_and_backfill()
    with fts.cursor() as conn:
        out = recall.assemble_background(
            conn, scope="", hints=["beverage", "sunrise"], per_hint=10, fold_superseded=True
        )
    assert "喝咖啡" not in out  # 退役版本折出
    assert "喝抹茶" in out  # 链头浮出
    assert "早起" in out  # 孤立活跃条目浮出


def test_evo_fold_byte_identical_to_column_fold(ac_root) -> None:
    """等价断言（原双读对账 face 1 的回归形态）：evo 折叠与冷启动退化的
    superseded 列折叠输出逐字节相等——P1 不变量在测试里持续钉死。"""
    _seed_and_backfill()
    kwargs = dict(scope="", hints=["beverage", "sunrise"], per_hint=10, fold_superseded=True)
    with fts.cursor() as conn:
        via_evo = recall.assemble_background(conn, **kwargs)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(recall, "_evo_fold_ready", lambda conn: False)
        with fts.cursor() as conn:
            via_column = recall.assemble_background(conn, **kwargs)
    assert via_evo == via_column


def test_evo_trail_renders_supersede_and_refined_markers(ac_root) -> None:
    """trail 从 evo_nodes 双向指针渲染：``← [精炼自]``（refined_from 列）与
    ``← [曾]``（supersedes 边）语义保留。"""
    _seed_and_backfill()
    with fts.cursor() as conn:
        out = recall.assemble_background(
            conn,
            scope="",
            hints=["beverage"],
            per_hint=10,
            fold_superseded=True,
            chain_trail=True,
        )
    head_line = next(line for line in out.splitlines() if "喝抹茶" in line)
    # 双跳链 latest→oldest：抹茶 ←[精炼自] 茶 ←[曾] 咖啡，全部内联在链头行上。
    assert "← [精炼自] 用户喝茶" in head_line
    assert "← [曾] 用户喝咖啡" in head_line
    assert head_line.index("[精炼自]") < head_line.index("[曾]")


def test_evo_path_multi_chain_hits(ac_root) -> None:
    """同一 hint 命中多条链：每条链各自折到自己的链头，互不串扰。"""
    _seed_and_backfill()
    with fts.cursor() as conn:
        out = recall.assemble_background(
            conn,
            scope="",
            hints=["beverage"],
            per_hint=10,
            fold_superseded=True,
            chain_trail=True,
        )
    assert "DeepSeek v2" in out
    assert "v1 beverage" not in out.replace("← [曾] DeepSeek v1 beverage", "")
    deepseek_line = next(line for line in out.splitlines() if "DeepSeek v2" in line)
    assert "← [曾] DeepSeek v1" in deepseek_line


# ── Q2：event- 豁免条目不因折叠丢失 ──────────────────────────────────────────


def test_event_hits_survive_evo_fold(ac_root) -> None:
    """event- 条目不在 evo_nodes（Q2），但 keyword 层（include_events=True）命中
    必须照常浮出——折叠用 ``superseded=0`` 列兜住它们。"""
    _seed_and_backfill()
    with fts.cursor() as conn:
        out = recall.assemble_background(
            conn,
            scope="",
            hints=["beverage"],
            per_hint=10,
            include_events=True,
            fold_superseded=True,
        )
    assert "beverage event row" in out


def test_retired_event_entry_stays_folded_on_evo_path(ac_root) -> None:
    """event- 条目被退役（superseded=1）后，折叠同样把它折出（列判定照旧适用）。"""
    ids = _seed_and_backfill()
    with fts.cursor() as conn:
        entries_mod.mark_entry_deleted(conn, name="event-2026-06-10.md", entry_id=ids["event"])
        out = recall.assemble_background(
            conn,
            scope="",
            hints=["beverage"],
            per_hint=10,
            include_events=True,
            fold_superseded=True,
        )
    assert "beverage event row" not in out


# ── 行为证明 + 冷启动退化 ────────────────────────────────────────────────────


def test_fold_actually_reads_evo_nodes(ac_root) -> None:
    """行为证明：只篡改 evo_nodes（superseded 列不动），折叠跟着 evo_nodes 走
    （丢掉被篡改的孤立条目）。"""
    ids = _seed_and_backfill()
    with fts.cursor() as conn:
        conn.execute("UPDATE evo_nodes SET is_latest = 0 WHERE node_id = ?", (ids["isolated"],))
        out = recall.assemble_background(
            conn, scope="", hints=["sunrise"], per_hint=10, fold_superseded=True
        )
    assert "早起" not in out


def test_cold_start_degrades_to_column_fold_without_warning(ac_root, caplog) -> None:
    """evo_nodes 缺表（backfill 从未跑）：折叠退化到 superseded 列——输出非空、
    等价折叠、零 warning（这是文档化的冷启动路径，不是降级故障）。"""
    with fts.cursor() as conn:
        _seed(conn)
        conn.execute("DROP TABLE IF EXISTS evo_nodes")
        with caplog.at_level("WARNING", logger="persome.intent.recall"):
            out = recall.assemble_background(
                conn, scope="", hints=["beverage"], per_hint=10, fold_superseded=True
            )
    assert "喝抹茶" in out  # 折叠照常生效（superseded 列）
    assert "喝咖啡" not in out
    assert not any("fold query failed" in r.message for r in caplog.records)


def test_tick_records_hints_telemetry(ac_root) -> None:
    """每次 assemble 落一行 budget telemetry，且携带 hints（调试遥测）。"""
    from persome.store import recall_budget_ticks

    _seed_and_backfill()
    with fts.cursor() as conn:
        recall_budget_ticks.ensure_schema(conn)
        before = conn.execute("SELECT COUNT(*) FROM recall_budget_ticks").fetchone()[0]
        recall.assemble_background(conn, scope="s", hints=["beverage"], per_hint=10)
        after = conn.execute("SELECT COUNT(*) FROM recall_budget_ticks").fetchone()[0]
        hints = conn.execute(
            "SELECT hints FROM recall_budget_ticks ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert after == before + 1
    assert hints == '["beverage"]'

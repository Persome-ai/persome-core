"""增量影子写测试（SSOT 切换设计稿 §4.2 双写影子期，PR-3）。

覆盖：三写路影子（add / supersede / refined-from UPDATE / delete / ABSTRACT 形态）、
核心不变式「增量影子写后的 evo_nodes 态 == 重跑全量 backfill 的态」（逐字段全等）、
失败不回滚主写、计数器阈值报警、关闭 = 主写等价零影子、event- 豁免、冷启动/前驱
缺失跳过、compact 绕路站点的可见 miss。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from persome.evomem import backfill, integrity, shadow
from persome.evomem import store as evo_store
from persome.store import entries, fts


def _dump_evo(conn) -> list[tuple]:
    """evo_nodes 全表逐字段 dump（稳定排序），供不变式全等断言。"""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(evo_nodes)")]
    rows = conn.execute(
        f"SELECT {', '.join(cols)} FROM evo_nodes ORDER BY node_id, user_id, agent_id"
    ).fetchall()
    return [tuple(r) for r in rows]


def _assert_incremental_equals_full_backfill() -> None:
    """核心不变式：当前（增量影子写出的）evo_nodes 态 == 重跑全量 backfill 的态。

    backfill 幂等 upsert 全部 markdown 条目：若增量影子漏写了行、或任一字段与
    全量映射有出入，重跑后 dump 必然不同，断言即红。
    """
    with fts.cursor() as conn:
        before = _dump_evo(conn)
    report = backfill.run_backfill()
    assert report.ok, (report.violations, report.heads_only_evo, report.heads_only_fts)
    with fts.cursor() as conn:
        after = _dump_evo(conn)
    assert before == after


def _node(conn, node_id: str):
    row = conn.execute(
        "SELECT * FROM evo_nodes WHERE node_id=? AND user_id='default' AND agent_id='default'",
        (node_id,),
    ).fetchone()
    return dict(row) if row else None


def _seed_baseline(name: str = "project-base.md") -> str:
    """建一个文件 + 一条初始条目，跑 backfill 建立影子期基线。返回初始 entry id。"""
    with fts.cursor() as conn:
        entries.create_file(conn, name=name, description="d", tags=["seed"])
        eid = entries.append_entry(conn, name=name, content="baseline fact", tags=["base"])
    report = backfill.run_backfill()
    assert report.ok
    shadow.reset_misses()
    return eid


# ── 三写路影子覆盖 ────────────────────────────────────────────────────────────


def test_append_shadow_writes_node(ac_root: Path) -> None:
    _seed_baseline()
    with fts.cursor() as conn:
        eid = entries.append_entry(
            conn,
            name="project-base.md",
            content="new fact",
            tags=["alpha"],
            confidence="high",
            conflicted=True,
            occurred_at="2026-06-01 10:00",
        )
    with fts.cursor() as conn:
        node = _node(conn, eid)
    assert node is not None
    assert node["content"] == "new fact"
    assert node["status"] == "active"
    assert node["is_latest"] == 1
    assert json.loads(node["supersedes"]) == []
    assert json.loads(node["superseded_by"]) == []
    assert node["file_name"] == "project-base.md"
    assert node["tags"] == "alpha"  # 元认知 colon-tag 不进 tags 列
    assert node["confidence"] == "high"
    assert node["conflicted"] == 1
    assert node["occurred_at"] == "2026-06-01T10:00"
    assert node["valid_from"] is not None
    assert shadow.miss_count() == 0
    _assert_incremental_equals_full_backfill()


def test_supersede_shadow_links_chain(ac_root: Path) -> None:
    old = _seed_baseline()
    with fts.cursor() as conn:
        new = entries.supersede_entry(
            conn,
            name="project-base.md",
            old_entry_id=old,
            new_content="v2 fact",
            reason="updated",
            tags=["base"],
        )
    with fts.cursor() as conn:
        old_node, new_node = _node(conn, old), _node(conn, new)
    assert old_node["status"] == "shadow"
    assert old_node["is_latest"] == 0
    assert json.loads(old_node["superseded_by"]) == [new]
    assert new_node["status"] == "active"
    assert new_node["is_latest"] == 1
    assert json.loads(new_node["supersedes"]) == [old]
    assert shadow.miss_count() == 0
    _assert_incremental_equals_full_backfill()


def test_refined_from_update_shape(ac_root: Path) -> None:
    """UPDATE 形态（EVO-02 双标签法）：supersede + refined_from 出处。"""
    old = _seed_baseline(name="user-pref.md")
    with fts.cursor() as conn:
        new = entries.supersede_entry(
            conn,
            name="user-pref.md",
            old_entry_id=old,
            new_content="sharpened",
            reason="refined",
            refined_from=old,
        )
    with fts.cursor() as conn:
        new_node = _node(conn, new)
        old_node = _node(conn, old)
    assert new_node["refined_from"] == old
    assert json.loads(new_node["supersedes"]) == [old]
    assert old_node["status"] == "shadow"
    _assert_incremental_equals_full_backfill()


def test_delete_shadow_retires_node(ac_root: Path) -> None:
    eid = _seed_baseline(name="person-bob.md")
    with fts.cursor() as conn:
        entries.mark_entry_deleted(conn, name="person-bob.md", entry_id=eid)
    with fts.cursor() as conn:
        node = _node(conn, eid)
    assert node["status"] == "shadow"
    assert node["is_latest"] == 0
    assert json.loads(node["superseded_by"]) == []  # 孤儿退役不进链
    assert node["content"] == "baseline fact"  # strike 已剥（与 backfill 同口径）
    assert node["valid_until"] is not None
    _assert_incremental_equals_full_backfill()


def test_abstract_shape(ac_root: Path) -> None:
    """ABSTRACT 形态：合成条目带多值出处 tag + 源逐个 strike（与 legacy ABSTRACT 落地同序）。"""
    _seed_baseline(name="topic-merge.md")
    with fts.cursor() as conn:
        a = entries.append_entry(conn, name="topic-merge.md", content="part a", tags=[])
        b = entries.append_entry(conn, name="topic-merge.md", content="part b", tags=[])
        synth = entries.append_entry(
            conn,
            name="topic-merge.md",
            content="synthesis",
            tags=[f"abstracted-from:{a},{b}"],
        )
        entries.mark_entry_deleted(conn, name="topic-merge.md", entry_id=a)
        entries.mark_entry_deleted(conn, name="topic-merge.md", entry_id=b)
    with fts.cursor() as conn:
        synth_node = _node(conn, synth)
        a_node, b_node = _node(conn, a), _node(conn, b)
    assert json.loads(synth_node["abstracted_from"]) == [a, b]
    assert synth_node["status"] == "active" and synth_node["is_latest"] == 1
    assert "abstracted-from" not in synth_node["tags"]  # 出处进列，不进 tags
    for src in (a_node, b_node):
        assert src["status"] == "shadow" and src["is_latest"] == 0
        assert json.loads(src["superseded_by"]) == []  # 多源出处是 provenance 边，不进链
    assert shadow.miss_count() == 0
    _assert_incremental_equals_full_backfill()


def test_mixed_sequence_invariant(ac_root: Path) -> None:
    """混合操作序列（链式 supersede ×2 + refined + delete + append）后不变式仍成立。"""
    v1 = _seed_baseline()
    with fts.cursor() as conn:
        v2 = entries.supersede_entry(
            conn, name="project-base.md", old_entry_id=v1, new_content="v2", reason="r"
        )
        v3 = entries.supersede_entry(
            conn,
            name="project-base.md",
            old_entry_id=v2,
            new_content="v3",
            reason="r",
            refined_from=v2,
        )
        extra = entries.append_entry(conn, name="project-base.md", content="extra", tags=[])
        entries.mark_entry_deleted(conn, name="project-base.md", entry_id=extra)
    with fts.cursor() as conn:
        assert _node(conn, v3)["is_latest"] == 1
        assert _node(conn, v2)["status"] == "shadow"
    assert shadow.miss_count() == 0
    _assert_incremental_equals_full_backfill()


# ── 纪律：失败/跳过 ──────────────────────────────────────────────────────────


def test_shadow_failure_never_rolls_back_main_write(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_baseline()

    def boom(*a, **k):  # noqa: ANN002, ANN003
        raise RuntimeError("shadow exploded")

    monkeypatch.setattr(evo_store, "upsert_node", boom)
    with fts.cursor() as conn:
        eid = entries.append_entry(conn, name="project-base.md", content="survives", tags=[])
        # 主写完好：markdown + FTS 都有
        row = conn.execute("SELECT superseded FROM entries WHERE id=?", (eid,)).fetchone()
    assert row is not None and row["superseded"] == 0
    with fts.cursor() as conn:
        assert _node(conn, eid) is None  # 影子没写进去（且不半写）
    assert shadow.miss_count() == 1


def test_miss_counter_alerts_at_threshold(ac_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_baseline()
    alerts: list[tuple] = []
    monkeypatch.setattr(integrity, "emit_alert", lambda *a, **k: alerts.append((a, k)))
    monkeypatch.setattr(evo_store, "upsert_node", lambda *a, **k: 1 / 0)
    with fts.cursor() as conn:
        for i in range(shadow._ALERT_EVERY):
            entries.append_entry(conn, name="project-base.md", content=f"m{i}", tags=[])
    assert shadow.miss_count() == shadow._ALERT_EVERY
    assert len(alerts) == 1
    (check, _detail), kwargs = alerts[0]
    assert check == "shadow_write_lag"
    assert kwargs["source"] == "shadow_write"
    assert kwargs["structural"] is False


def test_disabled_flag_means_no_shadow_and_no_misses(ac_root: Path) -> None:
    """P0：关闭开关 = 主写路径行为与现状等价——零影子写、零 miss、零报警。"""
    (ac_root / "config.toml").write_text("[evomem]\nshadow_write_enabled = false\n")
    with fts.cursor() as conn:
        entries.create_file(conn, name="project-off.md", description="d", tags=[])
        eid = entries.append_entry(conn, name="project-off.md", content="fact", tags=[])
        new = entries.supersede_entry(
            conn, name="project-off.md", old_entry_id=eid, new_content="v2", reason="r"
        )
        entries.mark_entry_deleted(conn, name="project-off.md", entry_id=new)
        # 主写完好（FTS / 链投影照常）
        assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 2
        evo_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='evo_nodes'"
        ).fetchone()
        if evo_exists:
            assert conn.execute("SELECT COUNT(*) FROM evo_nodes").fetchone()[0] == 0
    assert shadow.miss_count() == 0


def test_default_flag_is_on() -> None:
    from persome.config import EvomemConfig

    assert EvomemConfig().shadow_write_enabled is True


def test_event_prefix_exempt(ac_root: Path) -> None:
    """Q2：event- 条目静默跳过——不进 evo_nodes 也不算 miss。"""
    _seed_baseline()
    with fts.cursor() as conn:
        entries.create_file(conn, name="event-2026-06-10.md", description="d", tags=[])
        eid = entries.append_entry(conn, name="event-2026-06-10.md", content="log", tags=[])
    with fts.cursor() as conn:
        assert _node(conn, eid) is None
    assert shadow.miss_count() == 0
    _assert_incremental_equals_full_backfill()


def test_cold_start_skip_when_backfill_never_ran(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """冷启动衔接：evo_nodes 缺表/为空 → warning 跳过（计 miss、不报警、不建表）。"""
    alerts: list = []
    monkeypatch.setattr(integrity, "emit_alert", lambda *a, **k: alerts.append(a))
    monkeypatch.setattr(shadow, "_ALERT_EVERY", 1)  # 即便阈值=1，冷启动跳过也不报警
    with fts.cursor() as conn:
        entries.create_file(conn, name="project-cold.md", description="d", tags=[])
        eid = entries.append_entry(conn, name="project-cold.md", content="fact", tags=[])
        # 主写完好
        assert conn.execute("SELECT 1 FROM entries WHERE id=?", (eid,)).fetchone()
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='evo_nodes'"
        ).fetchone()
    assert shadow.miss_count() == 1
    assert alerts == []


def test_missing_predecessor_skips_whole_batch(ac_root: Path) -> None:
    """前驱缺失（影子明显落后）：跳过整批不半建链，不留悬空指针。

    滞后用真实形态制造：v1 的 append 与 v1→v2 的 supersede 期间影子写失败
    （upsert 临时炸掉），evo_nodes 完全没有这条链；恢复后 supersede v2→v3。
    """
    _seed_baseline()  # evo 里有 baseline 节点，冷启动守卫不触发
    real_upsert = evo_store.upsert_node
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(evo_store, "upsert_node", lambda *a, **k: 1 / 0)
        with fts.cursor() as conn:
            v1 = entries.append_entry(conn, name="project-base.md", content="v1", tags=[])
            v2 = entries.supersede_entry(
                conn, name="project-base.md", old_entry_id=v1, new_content="v2", reason="r"
            )
    assert evo_store.upsert_node is real_upsert
    shadow.reset_misses()
    with fts.cursor() as conn:
        v3 = entries.supersede_entry(
            conn, name="project-base.md", old_entry_id=v2, new_content="v3", reason="r"
        )
    with fts.cursor() as conn:
        assert _node(conn, v3) is None  # 整批跳过：v3 没写
        assert _node(conn, v2) is None  # v2 也没补（不半建链）
        # 主写完好
        assert conn.execute("SELECT 1 FROM entries WHERE id=?", (v3,)).fetchone()
        violations = [v for v in integrity.run_checks(conn) if v.structural]
    assert violations == []  # 影子滞后但结构自洽（没留悬空/单向指针）
    assert shadow.miss_count() == 1
    # 修复动作 = 重跑幂等 backfill，补齐后不变式恢复
    report = backfill.run_backfill()
    assert report.ok
    with fts.cursor() as conn:
        assert _node(conn, v3)["is_latest"] == 1
    _assert_incremental_equals_full_backfill()


def test_stale_mirror_pointer_skips_whole_batch(ac_root: Path) -> None:
    """批外前驱存在但镜像指针未闭合（那条边的影子被漏写过）：同样整批跳过。

    只查「端点存在」不够——对着一个 stale 前驱写单向 supersedes 指针会制造
    pointer_symmetry violation。守卫必须验证镜像指针已指回批内节点。
    滞后形态：v1 在 evo（baseline 影子写过），v1→v2 那次 supersede 的影子失败。
    """
    v1 = _seed_baseline()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(evo_store, "upsert_node", lambda *a, **k: 1 / 0)
        with fts.cursor() as conn:
            v2 = entries.supersede_entry(
                conn, name="project-base.md", old_entry_id=v1, new_content="v2", reason="r"
            )
    shadow.reset_misses()
    with fts.cursor() as conn:
        v3 = entries.supersede_entry(
            conn, name="project-base.md", old_entry_id=v2, new_content="v3", reason="r"
        )
    with fts.cursor() as conn:
        assert _node(conn, v3) is None  # 跳过：没对 stale 前驱写单向指针
        assert json.loads(_node(conn, v1)["superseded_by"]) == []  # v1 没被动过
        violations = [v for v in integrity.run_checks(conn) if v.structural]
    assert violations == []
    assert shadow.miss_count() == 1
    # 重跑 backfill 补齐后自愈
    assert backfill.run_backfill().ok
    _assert_incremental_equals_full_backfill()


def test_compact_out_of_band_rewrite_records_visible_miss(ac_root: Path) -> None:
    """唯一绕路站点（writer/compact.py 整文件重写）：记成可见 miss；event- 豁免。"""
    _seed_baseline()
    shadow.note_out_of_band_rewrite(["project-base.md", "event-2026-06-10.md"])
    assert shadow.miss_count() == 1


def test_self_heal_when_old_node_absent_but_in_batch(ac_root: Path) -> None:
    """链根缺失但在本批内（supersede 同时重映射旧节点）：从 markdown 整批重建，自愈。

    滞后形态：v1 的 append 影子失败（v1 不在 evo），随后的 supersede 批 =
    [v1, v2]，链端点全在批内 → 两个节点都从 markdown 终态重映射，落库即闭合。
    """
    _seed_baseline()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(evo_store, "upsert_node", lambda *a, **k: 1 / 0)
        with fts.cursor() as conn:
            v1 = entries.append_entry(conn, name="project-base.md", content="v1", tags=[])
    shadow.reset_misses()
    with fts.cursor() as conn:
        v2 = entries.supersede_entry(
            conn, name="project-base.md", old_entry_id=v1, new_content="v2", reason="r"
        )
    with fts.cursor() as conn:
        assert _node(conn, v1)["status"] == "shadow"
        assert json.loads(_node(conn, v2)["supersedes"]) == [v1]
    assert shadow.miss_count() == 0
    _assert_incremental_equals_full_backfill()

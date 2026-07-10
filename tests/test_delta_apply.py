"""§4.2 确定性 apply 通道单测（writer/delta_apply.py）。

apply_delta 吃 gate_delta 的输出（post-gate clean，identity 已规范化），确定性铸点/边。
覆盖：kind-aware 铸点 / ended→valid_until / 关系铸边+极性 / 非法端点丢弃 / ended→close_edge /
event 活动点 / 幂等 / fail-open / classifier 退役守卫。
"""

from __future__ import annotations

from types import SimpleNamespace

from persome.evomem.engine import EvoMemory
from persome.store import fts
from persome.writer import delta_apply


def _apply(clean: dict) -> delta_apply.ApplyResult:
    with fts.cursor() as conn:
        return delta_apply.apply_delta(conn, None, clean, memory=EvoMemory())


def _apply_cfg(clean: dict, **flags) -> delta_apply.ApplyResult:
    """apply with a real memory_delta cfg (e.g. apply_assertions=True)."""
    cfg = SimpleNamespace(memory_delta=SimpleNamespace(**flags))
    with fts.cursor() as conn:
        return delta_apply.apply_delta(conn, cfg, clean, memory=EvoMemory())


def _point_files(prefix: str) -> list[str]:
    with fts.cursor() as conn:
        conn.row_factory = None
        return [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT file_name FROM evo_nodes WHERE file_name LIKE ?", (f"{prefix}-%",)
            )
        ]


def test_entities_mint_kind_aware_points(ac_root):
    clean = {
        "entities": [
            {"ref": "张伟", "kind": "person", "ended": False, "quote": "和张伟对齐"},
            {"new_entity": "Acme", "kind": "project", "ended": False, "quote": "Acme 主项目"},
            {"ref": "研发群", "kind": "org", "ended": False, "quote": "研发群里"},
            {"ref": "excalidraw", "kind": "artifact", "ended": False, "quote": "用 excalidraw"},
        ],
        "relations": [],
        "events": [],
        "assertions": [],
    }
    r = _apply(clean)
    assert r.entities_minted == 4
    # kind → 前缀映射（artifact→tool-）
    assert _point_files("person") == ["person-张伟.md"]
    assert _point_files("project") == ["project-acme.md"]
    assert _point_files("org") == ["org-研发群.md"]
    assert _point_files("tool") == ["tool-excalidraw.md"]


def test_self_and_bad_kind_skipped(ac_root):
    clean = {
        "entities": [
            {"ref": "self", "kind": "person", "ended": False, "quote": "x"},  # self 不铸
            {"ref": "x", "kind": "bogus", "ended": False, "quote": "x"},  # 非法 kind 跳过
            {"kind": "person", "ended": False, "quote": "x"},  # 无 canonical 跳过
        ],
        "relations": [],
        "events": [],
        "assertions": [],
    }
    r = _apply(clean)
    assert r.entities_minted == 0


def test_ended_entity_stamps_valid_until(ac_root):
    clean = {
        "entities": [{"ref": "研发群", "kind": "org", "ended": True, "quote": "退出研发群"}],
        "relations": [],
        "events": [],
        "assertions": [],
    }
    _apply(clean)
    with fts.cursor() as conn:
        conn.row_factory = None
        rows = conn.execute(
            "SELECT valid_until FROM evo_nodes WHERE file_name = 'org-研发群.md'"
        ).fetchall()
    assert any(row[0] is not None for row in rows)


def test_idempotent_rerun_sees_not_remints(ac_root):
    clean = {
        "entities": [{"ref": "张伟", "kind": "person", "ended": False, "quote": "x"}],
        "relations": [],
        "events": [],
        "assertions": [],
    }
    r1 = _apply(clean)
    r2 = _apply(clean)
    assert r1.entities_minted == 1 and r1.entities_seen == 0
    assert r2.entities_minted == 0 and r2.entities_seen == 1


def test_relations_mint_edges_with_polarity(ac_root):
    clean = {
        "entities": [{"ref": "张伟", "kind": "person", "ended": False, "quote": "x"}],
        "relations": [
            {
                "src": {"ref": "self"},
                "dst": {"ref": "张伟"},
                "predicate": "knows",
                "polarity": "+",
                "ended": False,
                "quote": "和张伟愉快合作",
                "confidence": 0.9,
            }
        ],
        "events": [],
        "assertions": [],
    }
    r = _apply(clean)
    assert r.edges_new == 1
    with fts.cursor() as conn:
        conn.row_factory = None
        row = conn.execute(
            "SELECT src_identity, dst_identity, predicate, polarity FROM relation_edges"
            " WHERE predicate='knows'"
        ).fetchone()
    assert row == ("self", "张伟", "knows", "+")
    # ① 地板边随行：self engaged_with 张伟（连通保底）
    with fts.cursor() as conn:
        conn.row_factory = None
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM relation_edges WHERE predicate='engaged_with'"
                " AND dst_identity='张伟'"
            ).fetchone()[0]
            == 1
        )


def test_illegal_endpoint_relation_dropped(ac_root):
    # participates_in 的合法端点是 {SELF,PERSON,ORG}→{PROJECT,EVENT}；person→person 非法
    clean = {
        "entities": [
            {"ref": "张伟", "kind": "person", "ended": False, "quote": "x"},
            {"ref": "李四", "kind": "person", "ended": False, "quote": "x"},
        ],
        "relations": [
            {
                "src": {"ref": "张伟"},
                "dst": {"ref": "李四"},
                "predicate": "participates_in",
                "polarity": "0",
                "ended": False,
                "quote": "x",
                "confidence": 0.9,
            }
        ],
        "events": [],
        "assertions": [],
    }
    r = _apply(clean)
    assert r.edges_new == 0  # 非法 participates_in 端点被 add_edge 矩阵闸拒
    with fts.cursor() as conn:
        conn.row_factory = None
        # 非法②层边零；但①地板边合法（engaged_with 端点全 kind 合法）→ 两实体各一条
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM relation_edges WHERE predicate='participates_in'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM relation_edges WHERE predicate='engaged_with'"
            ).fetchone()[0]
            == 2
        )


def test_ended_relation_closes_edge(ac_root):
    clean = {
        "entities": [{"ref": "张伟", "kind": "person", "ended": False, "quote": "x"}],
        "relations": [
            {
                "src": {"ref": "self"},
                "dst": {"ref": "张伟"},
                "predicate": "reports_to",
                "polarity": "0",
                "ended": True,
                "quote": "不再向张伟汇报",
                "confidence": 0.9,
            }
        ],
        "events": [],
        "assertions": [],
    }
    r = _apply(clean)
    assert r.edges_closed == 1
    with fts.cursor() as conn:
        conn.row_factory = None
        vt = conn.execute(
            "SELECT valid_to FROM relation_edges WHERE predicate='reports_to'"
        ).fetchone()[0]
    assert vt is not None  # §4.6 leg-a：delta ended → close_edge（②层结构边被收口）


def test_events_mint_activity_point_and_edge(ac_root):
    clean = {
        "entities": [],
        "relations": [],
        "events": [
            {
                "title": "完成季度对账",
                "participants": [{"ref": "self"}],
                "quote": "完成了季度对账",
                "confidence": 0.9,
            }
        ],
        "assertions": [],
    }
    r = _apply(clean)
    assert r.events_minted == 1
    with fts.cursor() as conn:
        conn.row_factory = None
        row = conn.execute(
            "SELECT src_identity, dst_identity, predicate FROM relation_edges"
        ).fetchone()
    assert row is not None and row[0] == "self" and row[1].startswith("event:")
    assert row[2] == "participates_in"


def test_empty_and_malformed_fail_open(ac_root):
    assert _apply({}).skipped_reason == "empty"
    # 垃圾条目不崩：缺字段/类型错 → 跳过或记 errors，绝不抛
    r = _apply(
        {"entities": [None, {"kind": "person"}, "garbage"], "relations": [42], "events": [None]}
    )
    assert isinstance(r, delta_apply.ApplyResult)


def test_floor_edge_connects_every_entity_no_orphan(ac_root):
    """① 关联地板：即使无任何显式关系，每个实体也 engaged_with self → 无假孤儿。
    连通性 kind 无关（org/artifact 一样连）——这修的正是「腾讯校招无边」。"""
    clean = {
        "entities": [
            {"new_entity": "腾讯校招", "kind": "org", "ended": False, "quote": "看校招页"},
            {"new_entity": "某工具", "kind": "artifact", "ended": False, "quote": "用了某工具"},
        ],
        "relations": [],  # 零显式关系
        "events": [],
        "assertions": [],
    }
    r = _apply(clean)
    assert r.floor_edges == 2
    with fts.cursor() as conn:
        conn.row_factory = None
        connected = {
            row[0]
            for row in conn.execute(
                "SELECT dst_identity FROM relation_edges WHERE src_identity='self'"
                " AND predicate='engaged_with'"
            )
        }
    assert connected == {"腾讯校招", "某工具"}  # 都连 USER，无孤儿


def test_floor_obs_accumulates_across_sessions(ac_root):
    """① 地板 obs = 跨会话累加（=会话数=attention 权重），不是 MAX-of-1（点层稀 bug 的死信号）。
    additive reinforce：同一实体在 N 个会话各出现一次 → engaged_with obs = N（修前冻结在 1）。"""
    clean = {
        "entities": [{"ref": "张三", "kind": "person", "ended": False, "quote": "和张三"}],
        "relations": [],
        "events": [],
        "assertions": [],
    }
    for _ in range(3):  # 模拟同一实体在 3 个会话各出现一次
        _apply(clean)
    with fts.cursor() as conn:
        conn.row_factory = None
        row = conn.execute(
            "SELECT observations FROM relation_edges WHERE predicate='engaged_with'"
            " AND dst_identity='张三' AND valid_to IS NULL"
        ).fetchone()
    assert row is not None and row[0] == 3  # 修前会永远是 1


def test_org_nesting_part_of(ac_root):
    """part_of 扩到 O→O：部门 part_of 公司。"""
    clean = {
        "entities": [
            {"new_entity": "腾讯", "kind": "org", "ended": False, "quote": "x"},
            {"new_entity": "混元部", "kind": "org", "ended": False, "quote": "x"},
        ],
        "relations": [
            {
                "src": {"ref": "混元部"},
                "dst": {"ref": "腾讯"},
                "predicate": "part_of",
                "polarity": "0",
                "ended": False,
                "quote": "混元部属于腾讯",
                "confidence": 0.9,
            }
        ],
        "events": [],
        "assertions": [],
    }
    r = _apply(clean)
    assert r.edges_new == 1  # O→O part_of 现在合法
    with fts.cursor() as conn:
        conn.row_factory = None
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM relation_edges WHERE predicate='part_of'"
                " AND src_identity='混元部' AND dst_identity='腾讯'"
            ).fetchone()[0]
            == 1
        )


def test_classifier_retired_when_apply_enabled(ac_root):
    """apply_enabled → classify_after_reduce/classify_window 短路 no-op（点归 delta）。"""
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from persome.writer import classifier as classifier_mod

    cfg = SimpleNamespace(
        reducer=SimpleNamespace(enabled=True),
        memory_delta=SimpleNamespace(apply_enabled=True),
    )
    res = classifier_mod.classify_after_reduce(
        cfg,
        session_id="s1",
        event_daily_path="event-2026-07-04.md",
        session_start=datetime(2026, 7, 4, tzinfo=UTC),
        session_end=datetime(2026, 7, 4, 1, tzinfo=UTC),
    )
    assert res.skipped_reason == "classifier retired (delta apply)"
    assert not res.committed


def test_assertions_land_as_fact_entries(ac_root):
    """② assertions 头：subject→实体文件，text 作事实条目 append（tags='fact …'）。喂 schema 的料。"""
    clean = {
        "entities": [{"ref": "温子墨", "kind": "person", "ended": False, "quote": "x"}],
        "relations": [],
        "events": [],
        "assertions": [
            {
                "subject": {"ref": "温子墨"},
                "text": "温子墨拿了腾讯 offer",
                "quote": "q",
                "confidence": 0.95,
            },
            {
                "subject": {"ref": "温子墨"},
                "text": "温子墨改了 inspector.py",
                "quote": "q",
                "confidence": 0.9,
            },
        ],
    }
    r = _apply_cfg(clean, apply_assertions=True)
    assert r.assertions_minted == 2
    with fts.cursor() as conn:
        conn.row_factory = None
        facts = {
            row[0]
            for row in conn.execute(
                "SELECT content FROM evo_nodes WHERE file_name='person-温子墨.md'"
                " AND is_latest=1 AND status='active' AND tags LIKE 'fact%'"
            )
        }
    assert "温子墨拿了腾讯 offer" in facts and "温子墨改了 inspector.py" in facts


def test_assertions_gated_off_by_default(ac_root):
    """apply_assertions 默认 OFF：cfg=None → 不落事实条目（shadow 攒量期字节等价）。"""
    clean = {
        "entities": [{"ref": "王五", "kind": "person", "ended": False, "quote": "x"}],
        "relations": [],
        "events": [],
        "assertions": [
            {"subject": {"ref": "王五"}, "text": "王五负责X", "quote": "q", "confidence": 0.9}
        ],
    }
    r = _apply(clean)  # cfg=None → apply_assertions 关
    assert r.assertions_minted == 0


def test_assertions_unroutable_subject_skipped(ac_root):
    """主体既非本 delta 实体、也无现存文件 → 不可路由 → 保守跳过（不臆断 kind）。"""
    clean = {
        "entities": [],
        "relations": [],
        "events": [],
        "assertions": [
            {
                "subject": {"new_entity": "陌生人"},
                "text": "陌生人做了X",
                "quote": "q",
                "confidence": 0.9,
            }
        ],
    }
    r = _apply_cfg(clean, apply_assertions=True)
    assert r.assertions_minted == 0


def test_assertions_idempotent_across_sessions(ac_root):
    """幂等：同一事实第二次 apply 不重复落（同文件同 text 已存在 → seen，不 mint）。"""
    clean = {
        "entities": [{"ref": "赵六", "kind": "person", "ended": False, "quote": "x"}],
        "relations": [],
        "events": [],
        "assertions": [
            {"subject": {"ref": "赵六"}, "text": "赵六是架构师", "quote": "q", "confidence": 0.9}
        ],
    }
    _apply_cfg(clean, apply_assertions=True)
    r2 = _apply_cfg(clean, apply_assertions=True)  # 独立 conn（第二场会话）
    assert r2.assertions_minted == 0 and r2.assertions_seen == 1


def test_reproject_entries_from_evomem_feeds_retrieval(ac_root):
    """reader↔重建保鲜（#39）：delta 铸点只写 evo_nodes（检索读的 entries 看不到），每日
    重投影把 evo_nodes 投进 entries → 检索能看到重建（spec 2026-07-04 §reader-cutover）。"""
    from persome.session.tick import _reproject_entries_from_evomem

    clean = {
        "entities": [{"ref": "张三", "kind": "person", "ended": False, "quote": "x"}],
        "relations": [],
        "events": [],
        "assertions": [],
    }
    _apply(clean)  # 铸 evo_nodes（add_direct 不投 entries）
    with fts.cursor() as conn:
        conn.row_factory = None
        before = conn.execute(
            "SELECT count(*) FROM entries WHERE path='person-张三.md' AND superseded=0"
        ).fetchone()[0]
    _reproject_entries_from_evomem()
    with fts.cursor() as conn:
        conn.row_factory = None
        after = conn.execute(
            "SELECT count(*) FROM entries WHERE path='person-张三.md' AND superseded=0"
        ).fetchone()[0]
    assert before == 0 and after >= 1  # 投影前检索盲，投影后检索可见

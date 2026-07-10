"""§1.5-2 图侧孤儿收敛单测（writer/orphan_reaper.py）。

孤儿（无实质边）到期遗忘；连通（有边）留；event/self 不收；软遗忘（收据留）。
用 delta_apply 铸点/边构造世界，`now=` 拨到未来让点「到龄」。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from persome.evomem.engine import EvoMemory
from persome.store import fts
from persome.writer import delta_apply, orphan_reaper

FUTURE = datetime(2030, 1, 1, tzinfo=UTC)  # 让所有点都超过 30 天龄


def _cfg(enabled=True, ttl=30):
    return SimpleNamespace(
        orphan_reaper=SimpleNamespace(enabled=enabled, ttl_days=ttl, max_per_night=200)
    )


def _mint(clean):
    with fts.cursor() as conn:
        delta_apply.apply_delta(conn, None, clean, memory=EvoMemory())


def _active_points(prefix):
    with fts.cursor() as conn:
        conn.row_factory = None
        return {
            r[0]
            for r in conn.execute(
                "SELECT file_name FROM evo_nodes WHERE file_name LIKE ? AND is_latest=1 AND status='active'",
                (f"{prefix}-%",),
            )
        }


def test_orphan_forgotten_connected_kept(ac_root):
    # 张伟：有 knows 边（连通）；李四：无边（孤儿）；both person
    clean = {
        "entities": [
            {"ref": "张伟", "kind": "person", "ended": False, "quote": "x"},
            {"ref": "李四", "kind": "person", "ended": False, "quote": "x"},
        ],
        "relations": [
            {
                "src": {"ref": "self"},
                "dst": {"ref": "张伟"},
                "predicate": "knows",
                "polarity": "0",
                "ended": False,
                "quote": "认识张伟",
                "confidence": 0.9,
            }
        ],
        "events": [],
        "assertions": [],
    }
    _mint(clean)
    assert _active_points("person") == {"person-张伟.md", "person-李四.md"}
    with fts.cursor() as conn:
        r = orphan_reaper.run_orphan_reap(_cfg(), conn, now=FUTURE)
    assert r.reaped == 1
    assert r.reaped_files == ["person-李四.md"]
    # 连通的张伟留，孤儿李四被忘
    assert _active_points("person") == {"person-张伟.md"}


def test_young_orphan_kept(ac_root):
    _mint(
        {
            "entities": [{"ref": "新人", "kind": "person", "ended": False, "quote": "x"}],
            "relations": [],
            "events": [],
            "assertions": [],
        }
    )
    # now=现在 → 刚铸的点没到 30 天龄 → 不收
    with fts.cursor() as conn:
        r = orphan_reaper.run_orphan_reap(_cfg(), conn, now=datetime.now(UTC))
    assert r.reaped == 0
    assert _active_points("person") == {"person-新人.md"}


def test_events_and_self_not_reaped(ac_root):
    # event: 活动点 + self 不在收割前缀内
    _mint(
        {
            "entities": [],
            "relations": [],
            "events": [
                {
                    "title": "开了个会",
                    "participants": [{"ref": "self"}],
                    "quote": "x",
                    "confidence": 0.9,
                }
            ],
            "assertions": [],
        }
    )
    with fts.cursor() as conn:
        r = orphan_reaper.run_orphan_reap(_cfg(), conn, now=FUTURE)
        conn.row_factory = None
        # event: 边仍在（活动点不被当孤儿点收）
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM relation_edges WHERE dst_identity LIKE 'event:%'"
            ).fetchone()[0]
            == 1
        )
    assert r.reaped == 0


def test_disabled_noop(ac_root):
    _mint(
        {
            "entities": [{"ref": "孤儿", "kind": "org", "ended": False, "quote": "x"}],
            "relations": [],
            "events": [],
            "assertions": [],
        }
    )
    with fts.cursor() as conn:
        r = orphan_reaper.run_orphan_reap(_cfg(enabled=False), conn, now=FUTURE)
    assert r.skipped_reason == "disabled" and r.reaped == 0
    assert _active_points("org") == {"org-孤儿.md"}


def test_reap_is_soft_receipt_stays(ac_root):
    # 遗忘=markdown strike，收据字节留在盘上（可回放），非物理删
    _mint(
        {
            "entities": [{"ref": "过客", "kind": "artifact", "ended": False, "quote": "x"}],
            "relations": [],
            "events": [],
            "assertions": [],
        }
    )
    with fts.cursor() as conn:
        orphan_reaper.run_orphan_reap(_cfg(), conn, now=FUTURE)
        conn.row_factory = None
        # 节点行仍在（superseded / 非 active），不是被 DELETE
        total = conn.execute(
            "SELECT COUNT(*) FROM evo_nodes WHERE file_name='tool-过客.md'"
        ).fetchone()[0]
    assert total >= 1  # 收据留
    assert "tool-过客.md" not in _active_points("tool")  # 但退出活跃图


def test_fact_rows_not_treated_as_entities(ac_root):
    """回归：实体文件里的**事实条目**（assertions，content=事实句、tags='fact …'）不能被
    当成 content=规范名的实体点收割——修前 find_orphans 把每个 is_latest 行当实体点，
    对事实行边匹配全落空 → 误把事实当孤儿收。tags='entity' 闸只锁实体点。"""
    cfg = SimpleNamespace(
        memory_delta=SimpleNamespace(apply_assertions=True),
        orphan_reaper=SimpleNamespace(enabled=True, ttl_days=30, max_per_night=200),
    )
    clean = {
        "entities": [{"ref": "李四", "kind": "person", "ended": False, "quote": "x"}],
        "relations": [],  # 李四无 ② 边 → 是孤儿实体
        "events": [],
        "assertions": [  # 关于李四的多条事实（content=事实句）
            {
                "subject": {"ref": "李四"},
                "text": "李四负责后端服务",
                "quote": "q",
                "confidence": 0.9,
            },
            {
                "subject": {"ref": "李四"},
                "text": "李四昨天改了 inspector.py",
                "quote": "q",
                "confidence": 0.9,
            },
        ],
    }
    with fts.cursor() as conn:
        delta_apply.apply_delta(conn, cfg, clean, memory=EvoMemory())
        # 先确认事实行真落了（否则测试空转）
        conn.row_factory = None
        fact_n = conn.execute(
            "SELECT COUNT(*) FROM evo_nodes WHERE file_name='person-李四.md'"
            " AND is_latest=1 AND status='active' AND tags LIKE 'fact%'"
        ).fetchone()[0]
        assert fact_n == 2
        cands = orphan_reaper.find_orphans(conn, ttl_days=30, now=FUTURE, engaged_keep=2)
    # 候选只含「李四」这个实体点（content=规范名），绝不含两条事实句 content
    assert {c[2] for c in cands} == {"李四"}

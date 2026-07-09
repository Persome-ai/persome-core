"""evo_nodes SSOT 扩列（切换设计稿 §1.2 + Q8，PR-2）：建表 / 迁移 / 序列化测试。

两态收敛：新建库（_CREATE_SQL 直接带全列）与旧形态库（PRAGMA 探测 + ALTER TABLE
ADD COLUMN 迁移）必须落到同一 schema；旧行经迁移后读出为缺省值。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from persome import paths
from persome.evomem import backup
from persome.evomem.models import MemoryLayer, MemoryNode, MemoryStatus
from persome.evomem.store import _SSOT_COLUMNS, MigrationSnapshotError, NodeStore
from persome.store import fts

_OLD_SHAPE_SQL = """
CREATE TABLE IF NOT EXISTS evo_nodes (
    node_id       TEXT NOT NULL,
    user_id       TEXT NOT NULL DEFAULT 'default',
    agent_id      TEXT NOT NULL DEFAULT 'default',
    content       TEXT NOT NULL,
    layer         TEXT NOT NULL,
    supersedes    TEXT NOT NULL DEFAULT '[]',
    superseded_by TEXT NOT NULL DEFAULT '[]',
    is_latest     INTEGER NOT NULL DEFAULT 1,
    status        TEXT NOT NULL DEFAULT 'active',
    memory_at     TEXT,
    gmt_created   TEXT,
    PRIMARY KEY (node_id, user_id, agent_id)
);
"""


def _columns() -> set[str]:
    with fts.cursor() as conn:
        return {row[1] for row in conn.execute("PRAGMA table_info(evo_nodes)")}


def _snapshots() -> list[str]:
    backup_dir = paths.backup_dir()
    if not backup_dir.is_dir():
        return []
    return sorted(p.name for p in backup_dir.iterdir() if p.suffix == ".db")


def test_fresh_table_has_all_ssot_columns(ac_root: Path) -> None:
    NodeStore()
    cols = _columns()
    for name, _decl in _SSOT_COLUMNS:
        assert name in cols, f"fresh table missing {name}"


def test_old_shape_table_is_migrated(ac_root: Path) -> None:
    """旧形态库（PR-1 时代的 11 列）经 NodeStore 初始化补齐全列，旧行读出缺省值。"""
    with fts.cursor() as conn:
        conn.executescript(_OLD_SHAPE_SQL)
        conn.execute(
            "INSERT INTO evo_nodes (node_id, user_id, agent_id, content, layer)"
            " VALUES ('legacy', 'default', 'default', 'old row', 'l2_fact')"
        )
    assert not _columns() >= {name for name, _ in _SSOT_COLUMNS}

    store = NodeStore()  # triggers _migrate
    cols = _columns()
    for name, _decl in _SSOT_COLUMNS:
        assert name in cols, f"migration missing {name}"

    node = store.get("legacy")
    assert node is not None
    assert node.content == "old row"
    assert node.file_name == ""
    assert node.tags == ""
    assert node.refined_from is None
    assert node.abstracted_from == []
    assert node.confidence is None
    assert node.conflicted is False
    assert node.occurred_at is None
    assert node.schema_summary is None
    assert node.schema_inferences is None
    assert node.schema_confidence is None
    assert node.valid_from is None
    assert node.valid_until is None


def test_migration_is_idempotent(ac_root: Path) -> None:
    NodeStore()
    before = _columns()
    NodeStore()  # second init must not fail / duplicate columns
    assert _columns() == before


def test_new_fields_round_trip(ac_root: Path) -> None:
    store = NodeStore()
    node = MemoryNode(
        node_id="n-full",
        content="central: 偏好极简\nsummary: 证据\ninferences:\n- 拒绝重框架",
        layer=MemoryLayer.L6_SCHEMA,
        status=MemoryStatus.ACTIVE,
        file_name="schema-project-x.md",
        tags="schema stable",
        refined_from="20260601-1200-aaaaaa",
        abstracted_from=["20260601-1201-bbbbbb", "20260601-1202-cccccc"],
        confidence="high",
        conflicted=True,
        occurred_at="2026-06-01T10:00",
        schema_summary="证据",
        schema_inferences=["拒绝重框架"],
        schema_confidence=0.72,
        valid_from="2026-06-01T12:00",
        valid_until="2026-06-02T12:00",
    )
    store.save(node)
    got = store.get("n-full")
    assert got == node


def test_defaulted_fields_round_trip(ac_root: Path) -> None:
    """全缺省节点（engine 现行写形态）round-trip 不变——既有调用方零改动。"""
    store = NodeStore()
    node = MemoryNode(node_id="n-min", content="c", layer=MemoryLayer.L2_FACT)
    store.save(node)
    assert store.get("n-min") == node


# ── §3.2 framework-layer 变更前快照（issue #489）─────────────────────────────


def test_fresh_table_takes_no_pre_migration_snapshot(ac_root: Path) -> None:
    """新建库：``_CREATE_SQL`` 直接带全列，没有破坏性 DDL → 不该付快照代价。

    成本克制铁律——``NodeStore()`` 在 daemon 里被频繁实例化，盲目每次打快照会把每日
    快照纪律退化成「每次开连接都 VACUUM」。
    """
    NodeStore()
    assert _snapshots() == []


def test_old_shape_migration_takes_pre_change_snapshot(ac_root: Path) -> None:
    """旧形态库经迁移补列前，framework 层强制落一次验证式变更前快照，迁移随后完成。"""
    with fts.cursor() as conn:
        conn.executescript(_OLD_SHAPE_SQL)
        conn.execute(
            "INSERT INTO evo_nodes (node_id, user_id, agent_id, content, layer)"
            " VALUES ('legacy', 'default', 'default', 'old row', 'l2_fact')"
        )
    assert _snapshots() == []  # nothing yet

    NodeStore()  # triggers _migrate → forced pre-change snapshot

    snaps = _snapshots()
    assert len(snaps) == 1, "migration must drop exactly one pre-change snapshot"
    assert snaps[0].startswith("evo-") and snaps[0].endswith(".db")
    # snapshot precedes the DDL; migration still completed after it.
    for name, _decl in _SSOT_COLUMNS:
        assert name in _columns(), f"migration missing {name}"


def test_already_migrated_reinit_takes_no_snapshot(ac_root: Path) -> None:
    """已是最新 schema 的库再次 ``NodeStore()``：``_migrate`` no-op，不打快照。"""
    NodeStore()  # fresh — full schema, no snapshot
    assert _snapshots() == []
    NodeStore()  # re-init, columns already present → no destructive DDL
    assert _snapshots() == []


def test_snapshot_failure_aborts_migration_fail_safe(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """快照失败 → ``MigrationSnapshotError``，``ALTER TABLE`` 不执行（fail-safe）。

    绝不在无救生艇状态下改 SSOT 库的 schema：宁可拒绝初始化，也不留下「快照没拿到
    但 schema 已破坏性变更」的中间态。
    """
    with fts.cursor() as conn:
        conn.executescript(_OLD_SHAPE_SQL)
    before = _columns()
    assert not before >= {name for name, _ in _SSOT_COLUMNS}

    monkeypatch.setattr(backup, "create_snapshot", lambda **_kw: None)
    with pytest.raises(MigrationSnapshotError):
        NodeStore()

    # schema untouched — no column was added behind a failed snapshot
    assert _columns() == before
    assert _snapshots() == []


def test_repeated_migration_does_not_overwrite_existing_snapshot(ac_root: Path) -> None:
    """变更前快照对路径正确且不互相覆盖：同库重复迁移不产生第二次（迁移完成后再
    init 是 no-op），且快照确实落在 ``paths.backup_dir()`` 下、名字符合约定。"""
    with fts.cursor() as conn:
        conn.executescript(_OLD_SHAPE_SQL)

    NodeStore()  # migrate once → one snapshot
    snaps_after_first = _snapshots()
    assert len(snaps_after_first) == 1
    snap_path = paths.backup_dir() / snaps_after_first[0]
    assert snap_path.is_file(), "snapshot must land under paths.backup_dir()"
    first_size = snap_path.stat().st_size

    NodeStore()  # schema already current → no second snapshot, no clobber
    snaps_after_second = _snapshots()
    assert snaps_after_second == snaps_after_first, "re-init must not add/replace snapshot"
    assert snap_path.stat().st_size == first_size, "existing snapshot left intact"

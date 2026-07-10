"Tests for test store migration."

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
        content="central: \u504f\u597d\u6781\u7b80\nsummary: \u8bc1\u636e\ninferences:\n- \u62d2\u7edd\u91cd\u6846\u67b6",
        layer=MemoryLayer.L6_SCHEMA,
        status=MemoryStatus.ACTIVE,
        file_name="schema-project-x.md",
        tags="schema stable",
        refined_from="20260601-1200-aaaaaa",
        abstracted_from=["20260601-1201-bbbbbb", "20260601-1202-cccccc"],
        confidence="high",
        conflicted=True,
        occurred_at="2026-06-01T10:00",
        schema_summary="\u8bc1\u636e",
        schema_inferences=["\u62d2\u7edd\u91cd\u6846\u67b6"],
        schema_confidence=0.72,
        valid_from="2026-06-01T12:00",
        valid_until="2026-06-02T12:00",
    )
    store.save(node)
    got = store.get("n-full")
    assert got == node


def test_defaulted_fields_round_trip(ac_root: Path) -> None:
    store = NodeStore()
    node = MemoryNode(node_id="n-min", content="c", layer=MemoryLayer.L2_FACT)
    store.save(node)
    assert store.get("n-min") == node


def test_fresh_table_takes_no_pre_migration_snapshot(ac_root: Path) -> None:
    NodeStore()
    assert _snapshots() == []


def test_old_shape_migration_takes_pre_change_snapshot(ac_root: Path) -> None:
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
    NodeStore()  # fresh — full schema, no snapshot
    assert _snapshots() == []
    NodeStore()  # re-init, columns already present → no destructive DDL
    assert _snapshots() == []


def test_snapshot_failure_aborts_migration_fail_safe(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

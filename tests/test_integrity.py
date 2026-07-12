"""Startup integrity check + auto-quarantine (#202)."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from persome import config as config_mod
from persome import integrity, paths
from persome.evomem import backup
from persome.evomem.models import MemoryLayer, MemoryNode
from persome.evomem.store import NodeStore
from persome.model import create_build_manifest, load_live_manifest
from persome.store import entries, fts, relation_edges, schema_faces
from persome.store import files as files_mod


def _make_healthy_db() -> Path:
    """Create a real, schema-initialised SQLite DB at the canonical path."""
    with fts.cursor() as conn:
        conn.execute("SELECT 1")
    return paths.index_db()


def _pending_payload(db: Path, quarantine: Path, *, reason: str) -> dict[str, object]:
    return {
        "version": 1,
        "phase": "prepared",
        "started_at": "2026-07-12T10:00:00+08:00",
        "original_path": str(db),
        "quarantine_path": str(quarantine),
        "reason": reason,
        "authority_unknown": False,
        "manifest_was_present": True,
    }


def test_healthy_db_and_config_are_not_quarantined(ac_root: Path) -> None:
    _make_healthy_db()
    config_mod.write_default_if_missing()

    recovered = integrity.check_and_recover()

    assert recovered == []
    assert paths.index_db().exists()
    assert not paths.integrity_recovery_marker().exists()
    # No quarantine copies were left behind.
    assert list(ac_root.glob("*.corrupt.*")) == []


def test_missing_files_are_not_corruption(ac_root: Path) -> None:
    # Fresh root: no DB, no config. A clean first run must not trip the check.
    assert not paths.index_db().exists()
    assert not paths.config_file().exists()

    recovered = integrity.check_and_recover()

    assert recovered == []
    assert not paths.integrity_recovery_marker().exists()
    assert not paths.integrity_recovery_pending().exists()
    assert not paths.integrity_config_recovery_pending().exists()
    assert not paths.index_db().exists()
    assert not paths.config_file().exists()


def test_empty_db_file_is_treated_as_healthy(ac_root: Path) -> None:
    # A brand-new empty DB created by fts.connect must read as "ok".
    _make_healthy_db()
    recovered = integrity.check_and_recover()
    assert recovered == []


def test_corrupt_db_is_quarantined_and_rebuilt(ac_root: Path) -> None:
    # Write garbage where a SQLite file should be — a non-SQLite header makes
    # PRAGMA integrity_check / open fail.
    db = paths.index_db()
    db.write_bytes(b"this is definitely not a sqlite database" * 50)

    recovered = integrity.check_and_recover()

    assert len(recovered) == 1
    q = recovered[0]
    assert q.kind == "database"
    assert q.original_path == str(db)
    # Original moved aside, preserved for analysis.
    quarantine = Path(q.quarantine_path)
    assert quarantine.exists()
    assert ".corrupt." in quarantine.name
    # A fresh, healthy DB can now be opened at the canonical path.
    with fts.cursor() as conn:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    assert [r[0] for r in rows] == ["ok"]


def test_corrupt_db_replays_memory_and_invalidates_stale_model_build(ac_root: Path) -> None:
    with fts.cursor() as conn:
        entries.create_file(
            conn,
            name="project-recovery.md",
            description="Database recovery proof",
            tags=["recovery"],
        )
        entry_id = entries.append_entry(
            conn,
            name="project-recovery.md",
            content="Durable memory survives full database quarantine.",
            tags=["recovery"],
        )
    paths.atomic_write_private_text(
        paths.model_build_manifest(),
        json.dumps({"status": "complete", "build_id": "stale-build"}),
    )
    paths.index_db().write_bytes(b"not-a-sqlite-database" * 100)

    recovered = integrity.check_and_recover()

    assert [item.kind for item in recovered] == ["database"]
    assert not paths.model_build_manifest().exists()
    with fts.cursor() as conn:
        row = conn.execute(
            "SELECT content FROM entries WHERE id=?",
            (entry_id,),
        ).fetchone()
        node = conn.execute(
            "SELECT content FROM evo_nodes WHERE node_id=?",
            (entry_id,),
        ).fetchone()
    assert row is not None and row[0] == "Durable memory survives full database quarantine."
    assert node is not None and node[0] == row[0]

    marker = json.loads(paths.integrity_recovery_marker().read_text())
    database = marker["database_recovery"]
    assert database["status"] == "restored"
    assert database["source"] == "markdown"
    assert database["nodes"] == 1
    assert database["projection_entries"] == 1
    assert database["model_rebuild_required"] is True
    assert database["stale_manifest_was_present"] is True
    assert database["manifest_invalidated"] is True
    assert database["lossy"] is True
    assert "structural_geometry" in database["not_recovered_without_snapshot"]


def test_corrupt_db_replays_projection_under_evomem_authority(ac_root: Path) -> None:
    config_mod.write_default_if_missing()
    with fts.cursor() as conn:
        entries.create_file(
            conn,
            name="project-evomem-recovery.md",
            description="Evomem recovery proof",
            tags=[],
        )
        entry_id = entries.append_entry(
            conn,
            name="project-evomem-recovery.md",
            content="The projected graph can be restored as canonical state.",
            tags=[],
        )
    config_text = paths.config_file().read_text(encoding="utf-8")
    assert 'write_authority = "markdown"' in config_text
    paths.atomic_write_private_text(
        paths.config_file(),
        config_text.replace('write_authority = "markdown"', 'write_authority = "evomem"'),
    )
    paths.index_db().write_bytes(b"evomem-database-corruption" * 100)

    integrity.check_and_recover()

    with fts.cursor() as conn:
        entry = conn.execute("SELECT content FROM entries WHERE id=?", (entry_id,)).fetchone()
        node = conn.execute("SELECT content FROM evo_nodes WHERE node_id=?", (entry_id,)).fetchone()
    assert (
        entry is not None and entry[0] == "The projected graph can be restored as canonical state."
    )
    assert node is not None and node[0] == entry[0]
    marker = json.loads(paths.integrity_recovery_marker().read_text())
    assert marker["database_recovery"]["status"] == "restored"


@pytest.mark.parametrize(
    "config_state",
    ["valid", "corrupt", "missing"],
    ids=["evomem-config", "corrupt-config", "missing-config"],
)
def test_verified_snapshot_wins_over_stale_markdown_when_markdown_is_not_known_authority(
    ac_root: Path,
    config_state: str,
) -> None:
    config_mod.write_default_if_missing()
    with fts.cursor() as conn:
        entries.create_file(
            conn,
            name="project-canonical-snapshot.md",
            description="Canonical snapshot proof",
            tags=[],
        )
        entry_id = entries.append_entry(
            conn,
            name="project-canonical-snapshot.md",
            content="STALE MARKDOWN PROJECTION",
            tags=[],
        )
    NodeStore().save(
        MemoryNode(
            node_id=entry_id,
            content="CANONICAL SNAPSHOT VALUE",
            layer=MemoryLayer.L2_FACT,
            file_name="project-canonical-snapshot.md",
        )
    )
    config_text = paths.config_file().read_text(encoding="utf-8")
    paths.atomic_write_private_text(
        paths.config_file(),
        config_text.replace('write_authority = "markdown"', 'write_authority = "  EVOMEM  "'),
    )
    snapshot = backup.create_snapshot(
        now=datetime(2026, 7, 12, 9, 0, tzinfo=UTC),
        structural_only=True,
    )
    assert snapshot is not None
    if config_state == "corrupt":
        paths.config_file().write_text("[[[ corrupt config", encoding="utf-8")
    elif config_state == "missing":
        paths.config_file().unlink()
    paths.index_db().write_bytes(b"evomem-database-corruption" * 100)

    integrity.check_and_recover()

    with fts.cursor() as conn:
        node = conn.execute("SELECT content FROM evo_nodes WHERE node_id=?", (entry_id,)).fetchone()
        entry = conn.execute("SELECT content FROM entries WHERE id=?", (entry_id,)).fetchone()
    assert node is not None and node[0] == "CANONICAL SNAPSHOT VALUE"
    assert entry is not None
    assert "STALE MARKDOWN PROJECTION" in (
        paths.memory_dir() / "project-canonical-snapshot.md"
    ).read_text(encoding="utf-8")
    database = json.loads(paths.integrity_recovery_marker().read_text())["database_recovery"]
    if config_state == "valid":
        assert entry[0] == node[0]
        assert config_mod.load().evomem.write_authority.strip().lower() == "evomem"
        with fts.cursor() as conn:
            entries.rebuild_index(conn)
            rebuilt = conn.execute("SELECT content FROM entries WHERE id=?", (entry_id,)).fetchone()
        assert rebuilt is not None and rebuilt[0] == "CANONICAL SNAPSHOT VALUE"
        assert database["status"] == "restored"
        assert database["source"] == "verified_snapshot"
        assert database["write_authority"] == "evomem"
        assert database["canonical_source"] == "verified_snapshot"
    else:
        assert entry[0] == "STALE MARKDOWN PROJECTION"
        assert config_mod.load().evomem.write_authority == "unknown"
        assert paths.integrity_recovery_pending().exists()
        assert paths.integrity_config_recovery_pending().exists()
        assert database["status"] == "partial"
        assert database["authority_resolution_required"] is True


def test_unknown_authority_snapshot_preserves_lagging_shadow_until_owner_chooses(
    ac_root: Path,
) -> None:
    config_mod.write_default_if_missing()
    with fts.cursor() as conn:
        entries.create_file(
            conn,
            name="project-snapshot-authority.md",
            description="Snapshot authority ambiguity",
            tags=[],
        )
        entry_id = entries.append_entry(
            conn,
            name="project-snapshot-authority.md",
            content="MARKDOWN CANONICAL CANDIDATE",
            tags=[],
        )
    NodeStore().save(
        MemoryNode(
            node_id=entry_id,
            content="LAGGING EVOMEM SHADOW CANDIDATE",
            layer=MemoryLayer.L2_FACT,
            file_name="project-snapshot-authority.md",
        )
    )
    snapshot = backup.create_snapshot(
        now=datetime(2026, 7, 12, 9, 0, tzinfo=UTC),
        structural_only=True,
    )
    assert snapshot is not None
    paths.config_file().write_text("[[[ corrupt config", encoding="utf-8")
    paths.index_db().write_bytes(b"snapshot-authority-corruption" * 100)

    integrity.check_and_recover()

    assert config_mod.load().evomem.write_authority == "unknown"
    assert paths.integrity_config_recovery_pending().exists()
    assert paths.integrity_recovery_pending().exists()
    with fts.cursor() as conn:
        entry = conn.execute("SELECT content FROM entries WHERE id=?", (entry_id,)).fetchone()
        node = conn.execute("SELECT content FROM evo_nodes WHERE node_id=?", (entry_id,)).fetchone()
    assert entry is not None and entry[0] == "MARKDOWN CANONICAL CANDIDATE"
    assert node is not None and node[0] == "LAGGING EVOMEM SHADOW CANDIDATE"
    assert "MARKDOWN CANONICAL CANDIDATE" in (
        paths.memory_dir() / "project-snapshot-authority.md"
    ).read_text(encoding="utf-8")
    database = json.loads(paths.integrity_recovery_marker().read_text())["database_recovery"]
    assert database["status"] == "partial"
    assert database["authority_resolution_required"] is True
    assert database["preserved_authority_sources"] == [
        "snapshot_entries",
        "snapshot_evo_nodes",
        "current_markdown",
    ]


def test_config_recovery_intent_closes_crash_gap_before_database_journal(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_mod.write_default_if_missing()
    with fts.cursor() as conn:
        entries.create_file(
            conn,
            name="project-config-crash-gap.md",
            description="Config crash-gap proof",
            tags=[],
        )
        entry_id = entries.append_entry(
            conn,
            name="project-config-crash-gap.md",
            content="STALE MARKDOWN AFTER CONFIG CRASH",
            tags=[],
        )
    NodeStore().save(
        MemoryNode(
            node_id=entry_id,
            content="CANONICAL SNAPSHOT AFTER CONFIG CRASH",
            layer=MemoryLayer.L2_FACT,
            file_name="project-config-crash-gap.md",
        )
    )
    config_text = paths.config_file().read_text(encoding="utf-8")
    paths.atomic_write_private_text(
        paths.config_file(),
        config_text.replace('write_authority = "markdown"', 'write_authority = "evomem"'),
    )
    snapshot = backup.create_snapshot(
        now=datetime(2026, 7, 12, 9, 0, tzinfo=UTC),
        structural_only=True,
    )
    assert snapshot is not None
    paths.index_db().write_bytes(b"database-corrupt-during-config-recovery" * 100)
    paths.config_file().write_text("[[[ corrupt config", encoding="utf-8")

    def _crash_before_database_journal(_payload: dict[str, object]) -> None:
        raise KeyboardInterrupt("synthetic process crash")

    with monkeypatch.context() as crash:
        crash.setattr(integrity, "_write_pending_recovery", _crash_before_database_journal)
        with pytest.raises(KeyboardInterrupt, match="synthetic process crash"):
            integrity.check_and_recover()

    assert paths.integrity_config_recovery_pending().exists()
    assert not paths.integrity_recovery_pending().exists()
    assert config_mod.load().models["default"].model

    recovered = integrity.check_and_recover()

    assert sorted(item.kind for item in recovered) == ["config", "database"]
    assert paths.integrity_config_recovery_pending().exists()
    assert paths.integrity_recovery_pending().exists()
    assert config_mod.load().evomem.write_authority == "unknown"
    with fts.cursor() as conn:
        node = conn.execute("SELECT content FROM evo_nodes WHERE node_id=?", (entry_id,)).fetchone()
        entry = conn.execute("SELECT content FROM entries WHERE id=?", (entry_id,)).fetchone()
    assert node is not None and node[0] == "CANONICAL SNAPSHOT AFTER CONFIG CRASH"
    assert entry is not None and entry[0] == "STALE MARKDOWN AFTER CONFIG CRASH"

    config_text = paths.config_file().read_text(encoding="utf-8")
    paths.atomic_write_private_text(
        paths.config_file(),
        config_text.replace('write_authority = "unknown"', 'write_authority = "evomem"'),
    )
    integrity.check_and_recover()

    assert not paths.integrity_config_recovery_pending().exists()
    assert not paths.integrity_recovery_pending().exists()
    assert config_mod.load().evomem.write_authority == "evomem"
    with fts.cursor() as conn:
        entries.rebuild_index(conn)
        rebuilt = conn.execute("SELECT content FROM entries WHERE id=?", (entry_id,)).fetchone()
    assert rebuilt is not None and rebuilt[0] == "CANONICAL SNAPSHOT AFTER CONFIG CRASH"
    database = json.loads(paths.integrity_recovery_marker().read_text())["database_recovery"]
    assert database["write_authority"] == "evomem"
    assert database["canonical_source"] == "verified_snapshot"


def test_owner_repaired_config_intent_is_cleared_after_healthy_database_check(
    ac_root: Path,
) -> None:
    _make_healthy_db()
    config_mod.write_default_if_missing()
    NodeStore().save(
        MemoryNode(
            node_id="shadow-node",
            content="A shadow node must not override an explicit owner repair.",
            layer=MemoryLayer.L2_FACT,
            file_name="project-shadow.md",
        )
    )
    valid_config = paths.config_file().read_text(encoding="utf-8")
    paths.config_file().write_text("[[[ corrupt before owner repair", encoding="utf-8")
    reason = integrity._config_corruption_reason(paths.config_file())
    assert reason is not None
    quarantine = integrity._quarantine_destination(paths.config_file())
    integrity._write_pending_config_recovery(
        {
            "version": 1,
            "phase": "prepared",
            "started_at": "2026-07-12T10:00:00+08:00",
            "original_path": str(paths.config_file()),
            "quarantine_path": str(quarantine),
            "reason": reason,
        }
    )
    paths.atomic_write_private_text(paths.config_file(), valid_config)

    recovered = integrity.check_and_recover()

    assert recovered == []
    assert not quarantine.exists()
    assert not paths.integrity_config_recovery_pending().exists()
    assert paths.integrity_recovery_marker().exists()
    assert config_mod.load().evomem.write_authority == "markdown"
    with fts.cursor() as conn:
        assert (
            conn.execute("SELECT count(*) FROM evo_nodes WHERE node_id='shadow-node'").fetchone()[0]
            == 0
        )


def test_owner_repaired_config_reconciles_opposite_live_projection(ac_root: Path) -> None:
    config_mod.write_default_if_missing()
    with fts.cursor() as conn:
        entries.create_file(
            conn,
            name="project-owner-repair-choice.md",
            description="Owner repair chooses evomem",
            tags=[],
        )
        entry_id = entries.append_entry(
            conn,
            name="project-owner-repair-choice.md",
            content="OLD MARKDOWN LIVE VALUE",
            tags=[],
        )
    NodeStore().save(
        MemoryNode(
            node_id=entry_id,
            content="OWNER CHOSEN EVOMEM VALUE",
            layer=MemoryLayer.L2_FACT,
            file_name="project-owner-repair-choice.md",
        )
    )
    evomem_config = (
        paths.config_file()
        .read_text(encoding="utf-8")
        .replace(
            'write_authority = "markdown"',
            'write_authority = "evomem"',
        )
    )
    paths.config_file().write_text("[[[ corrupt before owner repair", encoding="utf-8")
    reason = integrity._config_corruption_reason(paths.config_file())
    assert reason is not None
    quarantine = integrity._quarantine_destination(paths.config_file())
    integrity._write_pending_config_recovery(
        {
            "version": 1,
            "phase": "prepared",
            "started_at": "2026-07-12T10:00:00+08:00",
            "original_path": str(paths.config_file()),
            "quarantine_path": str(quarantine),
            "reason": reason,
        }
    )
    paths.atomic_write_private_text(paths.config_file(), evomem_config)

    integrity.check_and_recover()

    assert config_mod.load().evomem.write_authority == "evomem"
    assert not paths.integrity_config_recovery_pending().exists()
    with fts.cursor() as conn:
        entry = conn.execute("SELECT content FROM entries WHERE id=?", (entry_id,)).fetchone()
    assert entry is not None and entry[0] == "OWNER CHOSEN EVOMEM VALUE"
    markdown = (paths.memory_dir() / "project-owner-repair-choice.md").read_text(encoding="utf-8")
    assert "OWNER CHOSEN EVOMEM VALUE" in markdown
    assert "OLD MARKDOWN LIVE VALUE" not in markdown


def test_authority_resolved_phase_preserves_durable_owner_choice(ac_root: Path) -> None:
    config_mod.write_default_if_missing()
    with fts.cursor() as conn:
        entries.create_file(
            conn,
            name="project-resolved-crash.md",
            description="Resolved authority crash window",
            tags=[],
        )
        entry_id = entries.append_entry(
            conn,
            name="project-resolved-crash.md",
            content="SAME VALUE",
            tags=[],
        )
    NodeStore().save(
        MemoryNode(
            node_id=entry_id,
            content="SAME VALUE",
            layer=MemoryLayer.L2_FACT,
            file_name="project-resolved-crash.md",
        )
    )
    quarantine = integrity._quarantine_destination(paths.config_file())
    paths.atomic_write_private_text(quarantine, "previous corrupt config")
    integrity._write_pending_config_recovery(
        {
            "version": 1,
            "phase": "authority_resolved",
            "started_at": "2026-07-12T10:00:00+08:00",
            "original_path": str(paths.config_file()),
            "quarantine_path": str(quarantine),
            "reason": "crash after owner selected markdown",
            "resolved_write_authority": "markdown",
        }
    )

    integrity.check_and_recover()

    assert config_mod.load().evomem.write_authority == "markdown"
    assert not paths.integrity_config_recovery_pending().exists()
    with fts.cursor() as conn:
        assert conn.execute("SELECT content FROM entries WHERE id=?", (entry_id,)).fetchone()[
            0
        ] == ("SAME VALUE")


@pytest.mark.parametrize("last_live_projection", ["markdown", "evomem"])
def test_corrupt_config_with_divergent_projections_fails_closed_until_owner_chooses(
    ac_root: Path,
    last_live_projection: str,
) -> None:
    config_mod.write_default_if_missing()
    with fts.cursor() as conn:
        entries.create_file(
            conn,
            name="project-authority-inference.md",
            description="Authority inference",
            tags=[],
        )
        entry_id = entries.append_entry(
            conn,
            name="project-authority-inference.md",
            content="MARKDOWN PROJECTION VALUE",
            tags=[],
        )
    NodeStore().save(
        MemoryNode(
            node_id=entry_id,
            content="EVOMEM CANONICAL VALUE",
            layer=MemoryLayer.L2_FACT,
            file_name="project-authority-inference.md",
        )
    )
    if last_live_projection == "evomem":
        with fts.cursor() as conn:
            entries.rebuild_index(conn, source_authority="evomem")
    paths.config_file().write_text("[[[ corrupt config", encoding="utf-8")

    integrity.check_and_recover()

    assert config_mod.load().evomem.write_authority == "unknown"
    assert paths.integrity_config_recovery_pending().exists()
    with fts.cursor() as conn:
        content = conn.execute("SELECT content FROM entries WHERE id=?", (entry_id,)).fetchone()[0]
    assert content == (
        "EVOMEM CANONICAL VALUE"
        if last_live_projection == "evomem"
        else "MARKDOWN PROJECTION VALUE"
    )

    # Explicit owner resolution unfreezes the pending intent without guessing.
    config_text = paths.config_file().read_text(encoding="utf-8")
    paths.atomic_write_private_text(
        paths.config_file(),
        config_text.replace(
            'write_authority = "unknown"',
            f'write_authority = "{last_live_projection}"',
        ),
    )
    integrity.check_and_recover()

    assert not paths.integrity_config_recovery_pending().exists()
    assert config_mod.load().evomem.write_authority == last_live_projection
    with fts.cursor() as conn:
        entries.rebuild_index(conn)
        resolved = conn.execute("SELECT content FROM entries WHERE id=?", (entry_id,)).fetchone()[0]
    assert resolved == content


def test_unknown_authority_invalidates_completed_model_manifest(ac_root: Path) -> None:
    config_mod.write_default_if_missing()
    with fts.cursor() as conn:
        entries.create_file(
            conn,
            name="project-authority-manifest.md",
            description="Authority manifest guard",
            tags=[],
        )
        entry_id = entries.append_entry(
            conn,
            name="project-authority-manifest.md",
            content="MARKDOWN AUTHORITY CANDIDATE",
            tags=[],
        )
    NodeStore().save(
        MemoryNode(
            node_id=entry_id,
            content="EVOMEM AUTHORITY CANDIDATE",
            layer=MemoryLayer.L2_FACT,
            file_name="project-authority-manifest.md",
        )
    )
    manifest = create_build_manifest(
        core_commit="0123456789abcdef",
        started_at="2026-07-12T09:00:00+08:00",
        completed_at="2026-07-12T09:01:00+08:00",
    )
    paths.atomic_write_private_text(paths.model_build_manifest(), json.dumps(manifest))
    paths.config_file().write_text("[[[ corrupt config", encoding="utf-8")

    integrity.check_and_recover()

    assert not paths.model_build_manifest().exists()
    assert load_live_manifest()["status"] == "not_built"
    database = json.loads(paths.integrity_recovery_marker().read_text())["database_recovery"]
    assert database["status"] == "partial"
    assert database["model_rebuild_required"] is True
    assert database["stale_manifest_was_present"] is True
    assert database["manifest_invalidated"] is True


@pytest.mark.parametrize(
    ("last_live_projection", "owner_choice", "expected"),
    [
        ("evomem", "markdown", "MARKDOWN OWNER VALUE"),
        ("markdown", "evomem", "EVOMEM OWNER VALUE"),
    ],
)
def test_owner_choice_reconciles_opposite_live_projection_before_unfreezing(
    ac_root: Path,
    last_live_projection: str,
    owner_choice: str,
    expected: str,
) -> None:
    config_mod.write_default_if_missing()
    with fts.cursor() as conn:
        entries.create_file(
            conn,
            name="project-opposite-choice.md",
            description="Opposite authority choice",
            tags=[],
        )
        entry_id = entries.append_entry(
            conn,
            name="project-opposite-choice.md",
            content="MARKDOWN OWNER VALUE",
            tags=[],
        )
    NodeStore().save(
        MemoryNode(
            node_id=entry_id,
            content="EVOMEM OWNER VALUE",
            layer=MemoryLayer.L2_FACT,
            file_name="project-opposite-choice.md",
        )
    )
    if last_live_projection == "evomem":
        with fts.cursor() as conn:
            entries.rebuild_index(conn, source_authority="evomem")
    paths.config_file().write_text("[[[ corrupt config", encoding="utf-8")

    integrity.check_and_recover()
    assert config_mod.load().evomem.write_authority == "unknown"
    config_text = paths.config_file().read_text(encoding="utf-8")
    paths.atomic_write_private_text(
        paths.config_file(),
        config_text.replace('write_authority = "unknown"', f'write_authority = "{owner_choice}"'),
    )

    integrity.check_and_recover()

    assert config_mod.load().evomem.write_authority == owner_choice
    assert not paths.integrity_config_recovery_pending().exists()
    with fts.cursor() as conn:
        entry = conn.execute("SELECT content FROM entries WHERE id=?", (entry_id,)).fetchone()
        node = conn.execute("SELECT content FROM evo_nodes WHERE node_id=?", (entry_id,)).fetchone()
    assert entry is not None and entry[0] == expected
    if owner_choice == "markdown":
        assert node is not None and node[0] == expected
    else:
        markdown = (paths.memory_dir() / "project-opposite-choice.md").read_text(encoding="utf-8")
        assert expected in markdown
        assert "MARKDOWN OWNER VALUE" not in markdown


@pytest.mark.parametrize("config_state", ["corrupt", "missing"])
def test_pending_database_resume_treats_later_config_damage_as_unknown_authority(
    ac_root: Path,
    config_state: str,
) -> None:
    config_mod.write_default_if_missing()
    with fts.cursor() as conn:
        entries.create_file(
            conn,
            name="project-pending-config-damage.md",
            description="Pending recovery config proof",
            tags=[],
        )
        entry_id = entries.append_entry(
            conn,
            name="project-pending-config-damage.md",
            content="STALE MARKDOWN DURING PENDING RECOVERY",
            tags=[],
        )
    NodeStore().save(
        MemoryNode(
            node_id=entry_id,
            content="CANONICAL SNAPSHOT DURING PENDING RECOVERY",
            layer=MemoryLayer.L2_FACT,
            file_name="project-pending-config-damage.md",
        )
    )
    config_text = paths.config_file().read_text(encoding="utf-8")
    paths.atomic_write_private_text(
        paths.config_file(),
        config_text.replace('write_authority = "markdown"', 'write_authority = "evomem"'),
    )
    snapshot = backup.create_snapshot(
        now=datetime(2026, 7, 12, 9, 0, tzinfo=UTC),
        structural_only=True,
    )
    assert snapshot is not None
    db = paths.index_db()
    db.write_bytes(b"pending-database-config-damage" * 100)
    quarantine = integrity._quarantine_destination(db)
    integrity._write_pending_recovery(
        _pending_payload(db, quarantine, reason="synthetic pending database recovery")
    )
    integrity._quarantine(db, destination=quarantine)
    if config_state == "corrupt":
        paths.config_file().write_text("[[[ corrupt while pending", encoding="utf-8")
    else:
        paths.config_file().unlink()

    integrity.check_and_recover()

    assert paths.integrity_recovery_pending().exists()
    assert paths.integrity_config_recovery_pending().exists()
    assert config_mod.load().evomem.write_authority == "unknown"
    with fts.cursor() as conn:
        node = conn.execute("SELECT content FROM evo_nodes WHERE node_id=?", (entry_id,)).fetchone()
        entry = conn.execute("SELECT content FROM entries WHERE id=?", (entry_id,)).fetchone()
    assert node is not None and node[0] == "CANONICAL SNAPSHOT DURING PENDING RECOVERY"
    assert entry is not None and entry[0] == "STALE MARKDOWN DURING PENDING RECOVERY"
    database = json.loads(paths.integrity_recovery_marker().read_text())["database_recovery"]
    assert database["authority_resolution_required"] is True

    config_text = paths.config_file().read_text(encoding="utf-8")
    paths.atomic_write_private_text(
        paths.config_file(),
        config_text.replace('write_authority = "unknown"', 'write_authority = "evomem"'),
    )
    integrity.check_and_recover()

    assert not paths.integrity_recovery_pending().exists()
    assert not paths.integrity_config_recovery_pending().exists()
    with fts.cursor() as conn:
        resolved = conn.execute("SELECT content FROM entries WHERE id=?", (entry_id,)).fetchone()
    assert resolved is not None and resolved[0] == "CANONICAL SNAPSHOT DURING PENDING RECOVERY"


def test_markdown_authority_does_not_resurrect_forgotten_snapshot_node(ac_root: Path) -> None:
    config_mod.write_default_if_missing()
    with fts.cursor() as conn:
        entries.create_file(
            conn,
            name="project-forgotten.md",
            description="Forgetting recovery proof",
            tags=[],
        )
        entry_id = entries.append_entry(
            conn,
            name="project-forgotten.md",
            content="MUST STAY FORGOTTEN",
            tags=[],
        )
    NodeStore().save(
        MemoryNode(
            node_id=entry_id,
            content="MUST STAY FORGOTTEN",
            layer=MemoryLayer.L2_FACT,
            file_name="project-forgotten.md",
        )
    )
    NodeStore(user_id="other-user", agent_id="other-agent").save(
        MemoryNode(
            node_id=entry_id,
            content="OTHER SCOPE REMAINS CANONICAL",
            layer=MemoryLayer.L2_FACT,
            file_name="project-foreign-scope.md",
        )
    )
    with fts.cursor() as conn:
        schema_faces.upsert_root(
            conn,
            signature="Root derived from the soon-forgotten memory",
            members=[entry_id],
        )
        relation_edges.add_edge(
            conn,
            src_identity="self",
            dst_identity="forgotten-project",
            predicate="engaged_with",
            src_kind="self",
            dst_kind="project",
            provenance="inferred",
            confidence=0.9,
            status="active",
        )
        conn.execute(
            "INSERT INTO cross_domain_probe_state"
            " (pair_key, last_probed_at, probe_count, detected) VALUES (?, ?, 1, 1)",
            ('["forgotten-a","forgotten-b"]', "2026-07-12T00:00:00+00:00"),
        )
    snapshot = backup.create_snapshot(
        now=datetime(2026, 7, 12, 9, 0, tzinfo=UTC),
        structural_only=True,
    )
    assert snapshot is not None
    (paths.memory_dir() / "project-forgotten.md").unlink()
    paths.index_db().write_bytes(b"markdown-database-corruption" * 100)

    integrity.check_and_recover()

    with fts.cursor() as conn:
        assert (
            conn.execute("SELECT count(*) FROM entries WHERE id=?", (entry_id,)).fetchone()[0] == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM evo_nodes "
                "WHERE node_id=? AND user_id='default' AND agent_id='default'",
                (entry_id,),
            ).fetchone()[0]
            == 0
        )
        other_scope = conn.execute(
            "SELECT content FROM evo_nodes "
            "WHERE node_id=? AND user_id='other-user' AND agent_id='other-agent'",
            (entry_id,),
        ).fetchone()
        assert conn.execute("SELECT count(*) FROM relation_edges").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM schema_faces").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM cross_domain_probe_state").fetchone()[0] == 0
    assert other_scope is not None and other_scope[0] == "OTHER SCOPE REMAINS CANONICAL"
    database = json.loads(paths.integrity_recovery_marker().read_text())["database_recovery"]
    assert database["stale_projected_nodes_removed"] == 1
    assert database["derived_geometry_rows_invalidated"] >= 3


def test_incomplete_strict_markdown_discovery_never_deletes_snapshot_node(
    ac_root: Path, tmp_path: Path
) -> None:
    config_mod.write_default_if_missing()
    with fts.cursor() as conn:
        entries.create_file(
            conn,
            name="project-discovery-guard.md",
            description="Strict discovery guard",
            tags=[],
        )
        entry_id = entries.append_entry(
            conn,
            name="project-discovery-guard.md",
            content="SNAPSHOT NODE MUST SURVIVE",
            tags=[],
        )
    NodeStore().save(
        MemoryNode(
            node_id=entry_id,
            content="SNAPSHOT NODE MUST SURVIVE",
            layer=MemoryLayer.L2_FACT,
            file_name="project-discovery-guard.md",
        )
    )
    snapshot = backup.create_snapshot(
        now=datetime(2026, 7, 12, 9, 0, tzinfo=UTC),
        structural_only=True,
    )
    assert snapshot is not None
    memory = paths.memory_dir() / "project-discovery-guard.md"
    memory.unlink()
    outside = tmp_path / "outside.md"
    outside.write_text("UNTRUSTED EXTERNAL SOURCE", encoding="utf-8")
    memory.symlink_to(outside)
    paths.index_db().write_bytes(b"strict-discovery-database-corruption" * 100)

    integrity.check_and_recover()

    recovery = json.loads(paths.integrity_recovery_marker().read_text())["database_recovery"]
    assert recovery["status"] == "partial"
    assert "memory Markdown must be one regular file" in recovery["error"]
    with fts.cursor() as conn:
        node = conn.execute("SELECT content FROM evo_nodes WHERE node_id=?", (entry_id,)).fetchone()
    assert node is not None and node[0] == "SNAPSHOT NODE MUST SURVIVE"


def test_markdown_authority_refreshes_file_metadata_after_snapshot(ac_root: Path) -> None:
    config_mod.write_default_if_missing()
    with fts.cursor() as conn:
        entries.create_file(
            conn,
            name="project-frontmatter.md",
            description="OLD DESCRIPTION",
            tags=["old"],
        )
        entries.append_entry(
            conn,
            name="project-frontmatter.md",
            content="Current entry content.",
            tags=[],
        )
    snapshot = backup.create_snapshot(
        now=datetime(2026, 7, 12, 9, 0, tzinfo=UTC),
        structural_only=True,
    )
    assert snapshot is not None
    files_mod.update_frontmatter(
        paths.memory_dir() / "project-frontmatter.md",
        {"description": "NEW DESCRIPTION", "tags": ["new"], "status": "archived"},
    )
    paths.index_db().write_bytes(b"frontmatter-database-corruption" * 100)

    integrity.check_and_recover()

    with fts.cursor() as conn:
        row = conn.execute(
            "SELECT description, tags, status FROM files WHERE path='project-frontmatter.md'"
        ).fetchone()
    assert tuple(row) == ("NEW DESCRIPTION", "new", "archived")


def test_nested_skill_projection_is_replayed_after_full_quarantine(ac_root: Path) -> None:
    with fts.cursor() as conn:
        entries.create_file(
            conn,
            name="skills/skill-recovery.md",
            description="Nested skill recovery",
            tags=[],
        )
        entry_id = entries.append_entry(
            conn,
            name="skills/skill-recovery.md",
            content="Nested behavioral memory survives quarantine.",
            tags=[],
        )
    paths.index_db().write_bytes(b"nested-skill-database-corruption" * 100)

    integrity.check_and_recover()

    with fts.cursor() as conn:
        entry = conn.execute("SELECT path, content FROM entries WHERE id=?", (entry_id,)).fetchone()
        node_count = conn.execute(
            "SELECT count(*) FROM evo_nodes WHERE node_id=?", (entry_id,)
        ).fetchone()[0]
    assert entry is not None and tuple(entry) == (
        "skills/skill-recovery.md",
        "Nested behavioral memory survives quarantine.",
    )
    assert node_count == 0


def test_evomem_snapshot_recovery_keeps_nested_skill_as_direct_markdown(ac_root: Path) -> None:
    config_mod.write_default_if_missing()
    NodeStore().save(
        MemoryNode(
            node_id="baseline-node",
            content="baseline",
            layer=MemoryLayer.L2_FACT,
            file_name="project-baseline.md",
        )
    )
    with fts.cursor() as conn:
        entries.create_file(
            conn,
            name="skills/skill-snapshot-recovery.md",
            description="Direct nested skill",
            tags=[],
        )
        entry_id = entries.append_entry(
            conn,
            name="skills/skill-snapshot-recovery.md",
            content="Direct skill survives evomem snapshot recovery.",
            tags=[],
        )
    NodeStore().save(
        MemoryNode(
            node_id=entry_id,
            content="OLD SNAPSHOT SKILL",
            layer=MemoryLayer.L2_FACT,
            file_name="skills/skill-snapshot-recovery.md",
        )
    )
    config_text = paths.config_file().read_text(encoding="utf-8")
    paths.atomic_write_private_text(
        paths.config_file(),
        config_text.replace('write_authority = "markdown"', 'write_authority = "evomem"'),
    )
    snapshot = backup.create_snapshot(
        now=datetime(2026, 7, 12, 9, 0, tzinfo=UTC),
        structural_only=True,
    )
    assert snapshot is not None
    paths.index_db().write_bytes(b"nested-evomem-snapshot-corruption" * 100)

    integrity.check_and_recover()

    with fts.cursor() as conn:
        entry = conn.execute("SELECT path, content FROM entries WHERE id=?", (entry_id,)).fetchone()
        node_count = conn.execute(
            "SELECT count(*) FROM evo_nodes WHERE node_id=?", (entry_id,)
        ).fetchone()[0]
    assert entry is not None and tuple(entry) == (
        "skills/skill-snapshot-recovery.md",
        "Direct skill survives evomem snapshot recovery.",
    )
    assert node_count == 0


def test_nested_skill_legacy_cleanup_preserves_same_named_top_level_snapshot_node(
    ac_root: Path,
) -> None:
    config_mod.write_default_if_missing()
    with fts.cursor() as conn:
        entries.create_file(
            conn,
            name="skill-same.md",
            description="Canonical top-level skill",
            tags=[],
        )
        top_id = entries.append_entry(
            conn,
            name="skill-same.md",
            content="TOP LEVEL CANONICAL SKILL",
            tags=[],
        )
        entries.create_file(
            conn,
            name="skills/skill-same.md",
            description="Direct nested skill",
            tags=[],
        )
        nested_id = entries.append_entry(
            conn,
            name="skills/skill-same.md",
            content="NESTED DIRECT SKILL",
            tags=[],
        )
    NodeStore().save(
        MemoryNode(
            node_id=top_id,
            content="TOP LEVEL CANONICAL SKILL",
            layer=MemoryLayer.L2_FACT,
            file_name="skill-same.md",
        )
    )
    # A modern accidental shadow row retains the direct nested filename and is
    # therefore safe to identify and remove during recovery.
    NodeStore().save(
        MemoryNode(
            node_id=nested_id,
            content="LEGACY NESTED SNAPSHOT VALUE",
            layer=MemoryLayer.L2_FACT,
            file_name="skills/skill-same.md",
        )
    )
    NodeStore(user_id="other-user", agent_id="other-agent").save(
        MemoryNode(
            node_id=nested_id,
            content="OTHER SCOPE MUST SURVIVE",
            layer=MemoryLayer.L2_FACT,
            file_name="skill-same.md",
        )
    )
    config_text = paths.config_file().read_text(encoding="utf-8")
    paths.atomic_write_private_text(
        paths.config_file(),
        config_text.replace('write_authority = "markdown"', 'write_authority = "evomem"'),
    )
    snapshot = backup.create_snapshot(
        now=datetime(2026, 7, 12, 9, 0, tzinfo=UTC),
        structural_only=True,
    )
    assert snapshot is not None
    paths.index_db().write_bytes(b"same-name-snapshot-corruption" * 100)

    integrity.check_and_recover()

    with fts.cursor() as conn:
        top_node = conn.execute(
            "SELECT file_name, content FROM evo_nodes WHERE node_id=?", (top_id,)
        ).fetchone()
        nested_node = conn.execute(
            "SELECT file_name, content FROM evo_nodes "
            "WHERE node_id=? AND user_id='default' AND agent_id='default'",
            (nested_id,),
        ).fetchone()
        other_scope_node = conn.execute(
            "SELECT file_name, content FROM evo_nodes "
            "WHERE node_id=? AND user_id='other-user' AND agent_id='other-agent'",
            (nested_id,),
        ).fetchone()
        top_entry = conn.execute(
            "SELECT path, content FROM entries WHERE id=?", (top_id,)
        ).fetchone()
        nested_entry = conn.execute(
            "SELECT path, content FROM entries WHERE id=?", (nested_id,)
        ).fetchone()

    assert top_node is not None and tuple(top_node) == (
        "skill-same.md",
        "TOP LEVEL CANONICAL SKILL",
    )
    assert nested_node is None
    assert other_scope_node is not None and tuple(other_scope_node) == (
        "skill-same.md",
        "OTHER SCOPE MUST SURVIVE",
    )
    assert top_entry is not None and tuple(top_entry) == (
        "skill-same.md",
        "TOP LEVEL CANONICAL SKILL",
    )
    assert nested_entry is not None and tuple(nested_entry) == (
        "skills/skill-same.md",
        "NESTED DIRECT SKILL",
    )


def test_ambiguous_basename_only_nested_node_fails_closed(ac_root: Path) -> None:
    config_mod.write_default_if_missing()
    with fts.cursor() as conn:
        entries.create_file(
            conn,
            name="skills/skill-ambiguous.md",
            description="Direct nested skill",
            tags=[],
        )
        entry_id = entries.append_entry(
            conn,
            name="skills/skill-ambiguous.md",
            content="DIRECT NESTED VALUE",
            tags=[],
        )
    NodeStore().save(
        MemoryNode(
            node_id=entry_id,
            content="AMBIGUOUS CANONICAL VALUE",
            layer=MemoryLayer.L2_FACT,
            file_name="skill-ambiguous.md",
        )
    )
    config_text = paths.config_file().read_text(encoding="utf-8")
    paths.atomic_write_private_text(
        paths.config_file(),
        config_text.replace('write_authority = "markdown"', 'write_authority = "evomem"'),
    )
    snapshot = backup.create_snapshot(
        now=datetime(2026, 7, 12, 9, 0, tzinfo=UTC),
        structural_only=True,
    )
    assert snapshot is not None
    paths.index_db().write_bytes(b"ambiguous-direct-node-corruption" * 100)

    integrity.check_and_recover()

    recovery = json.loads(paths.integrity_recovery_marker().read_text())["database_recovery"]
    assert recovery["status"] == "partial"
    assert "collides with canonical evomem file" in recovery["error"]
    with fts.cursor() as conn:
        node = conn.execute(
            "SELECT content FROM evo_nodes "
            "WHERE node_id=? AND user_id='default' AND agent_id='default'",
            (entry_id,),
        ).fetchone()
    assert node is not None and node[0] == "AMBIGUOUS CANONICAL VALUE"


def test_corrupt_db_restores_verified_snapshot_before_projection_replay(ac_root: Path) -> None:
    with fts.cursor() as conn:
        fts.insert_capture(
            conn,
            id="capture-from-snapshot",
            timestamp="2026-07-12T00:00:00+00:00",
            app_name="Test App",
            bundle_id="com.persome.test",
            window_title="Verified snapshot recovery",
            focused_role="AXTextArea",
            focused_value="snapshot",
            visible_text="capture survives through the verified backup",
            url="https://example.test/snapshot",
        )
    snapshot = backup.create_snapshot(
        now=datetime(2026, 7, 12, 9, 0, tzinfo=UTC),
        structural_only=True,
    )
    assert snapshot is not None
    paths.index_db().write_bytes(b"full-database-corruption" * 100)

    integrity.check_and_recover()

    with fts.cursor() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM captures WHERE id='capture-from-snapshot'"
            ).fetchone()[0]
            == 1
        )
    marker = json.loads(paths.integrity_recovery_marker().read_text())
    database = marker["database_recovery"]
    assert database["status"] == "restored"
    assert database["source"] == "verified_snapshot"
    assert database["snapshot_path"] == str(snapshot)
    assert database["lossy"] is True
    assert database["recovery_completeness"] == "best_effort"
    assert database["potentially_lost_since_snapshot"] is True
    assert database["snapshot_modified_at"]
    assert database["not_recovered_without_snapshot"] == []


def test_foreign_newer_snapshot_is_skipped_for_older_persome_snapshot(ac_root: Path) -> None:
    with fts.cursor() as conn:
        fts.insert_capture(
            conn,
            id="valuable-capture",
            timestamp="2026-07-11T00:00:00+00:00",
            app_name="Test App",
            bundle_id="com.persome.test",
            window_title="Persome snapshot",
            focused_role="AXTextArea",
            focused_value="valuable",
            visible_text="valuable capture in a genuine Persome snapshot",
            url="https://example.test/genuine",
        )
    genuine = backup.create_snapshot(
        now=datetime(2026, 7, 11, 9, 0, tzinfo=UTC),
        structural_only=True,
    )
    assert genuine is not None
    foreign = paths.backup_dir() / "evo-20260712.db"
    conn = sqlite3.connect(foreign)
    try:
        conn.execute("CREATE TABLE files(path TEXT)")
        conn.execute("CREATE TABLE entries(id TEXT)")
        conn.execute("CREATE TABLE captures(id TEXT)")
        conn.commit()
    finally:
        conn.close()
    paths.index_db().write_bytes(b"full-database-corruption" * 100)

    integrity.check_and_recover()

    with fts.cursor() as conn:
        assert (
            conn.execute("SELECT count(*) FROM captures WHERE id='valuable-capture'").fetchone()[0]
            == 1
        )
    database = json.loads(paths.integrity_recovery_marker().read_text())["database_recovery"]
    assert database["snapshot_path"] == str(genuine)


def test_projection_replay_failure_is_reported_without_losing_startup(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths.index_db().write_bytes(b"full-database-corruption" * 100)
    paths.atomic_write_private_text(
        paths.model_build_manifest(),
        json.dumps({"status": "complete", "build_id": "stale-build"}),
    )
    monkeypatch.setattr(
        integrity,
        "_repopulate_after_quarantine",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("synthetic projection failure")),
    )

    recovered = integrity.check_and_recover()

    assert [item.kind for item in recovered] == ["database"]
    assert not paths.model_build_manifest().exists()
    with fts.cursor() as conn:
        assert [row[0] for row in conn.execute("PRAGMA integrity_check")] == ["ok"]
    marker = json.loads(paths.integrity_recovery_marker().read_text())
    database = marker["database_recovery"]
    assert database["status"] == "failed"
    assert database["source"] == "none"
    assert "synthetic projection failure" in database["error"]
    assert database["model_rebuild_required"] is True


def test_duplicate_markdown_ids_fail_recovery_instead_of_collapsing(ac_root: Path) -> None:
    with fts.cursor() as conn:
        entries.create_file(
            conn,
            name="project-duplicate-recovery.md",
            description="Duplicate recovery guard",
            tags=[],
        )
        entry_id = entries.append_entry(
            conn,
            name="project-duplicate-recovery.md",
            content="FIRST VALUE",
            tags=[],
        )
    memory = paths.memory_dir() / "project-duplicate-recovery.md"
    files_mod.atomic_write_text(
        memory,
        memory.read_text(encoding="utf-8")
        + f"\n\n## [2026-07-12T10:00:00+00:00] {{id: {entry_id}}}\nSECOND VALUE\n",
    )
    paths.index_db().write_bytes(b"duplicate-id-database-corruption" * 100)

    integrity.check_and_recover()

    database = json.loads(paths.integrity_recovery_marker().read_text())["database_recovery"]
    assert database["status"] == "failed"
    assert "duplicate Markdown memory node id" in database["error"]
    with fts.cursor() as conn:
        assert conn.execute("SELECT count(*) FROM entries").fetchone()[0] == 0


def test_recovery_resumes_after_crash_immediately_after_quarantine(ac_root: Path) -> None:
    with fts.cursor() as conn:
        entries.create_file(
            conn,
            name="project-resumable-recovery.md",
            description="Resumable recovery",
            tags=[],
        )
        entry_id = entries.append_entry(
            conn,
            name="project-resumable-recovery.md",
            content="Recovery resumes from its journal.",
            tags=[],
        )
    manifest = create_build_manifest(
        core_commit="0123456789abcdef",
        started_at="2026-07-12T09:00:00+08:00",
        completed_at="2026-07-12T09:01:00+08:00",
    )
    paths.atomic_write_private_text(paths.model_build_manifest(), json.dumps(manifest))
    db = paths.index_db()
    db.write_bytes(b"crash-resume-corruption" * 100)
    quarantine = integrity._quarantine_destination(db)
    integrity._write_pending_recovery(
        _pending_payload(db, quarantine, reason="synthetic crash after quarantine")
    )
    integrity._quarantine(db, destination=quarantine)

    assert load_live_manifest()["status"] == "not_built"
    recovered = integrity.check_and_recover()

    assert [item.kind for item in recovered] == ["database"]
    assert not paths.integrity_recovery_pending().exists()
    assert not paths.model_build_manifest().exists()
    with fts.cursor() as conn:
        assert (
            conn.execute("SELECT count(*) FROM entries WHERE id=?", (entry_id,)).fetchone()[0] == 1
        )


def test_recovery_resumes_after_snapshot_copy_before_projection_replay(ac_root: Path) -> None:
    with fts.cursor() as conn:
        fts.insert_capture(
            conn,
            id="capture-before-crash",
            timestamp="2026-07-12T00:00:00+00:00",
            app_name="Test App",
            bundle_id="com.persome.test",
            window_title="Crash phase",
            focused_role="AXTextArea",
            focused_value="resume",
            visible_text="snapshot copy survives resumed replay",
            url="https://example.test/crash-phase",
        )
    snapshot = backup.create_snapshot(
        now=datetime(2026, 7, 12, 9, 0, tzinfo=UTC),
        structural_only=True,
    )
    assert snapshot is not None
    db = paths.index_db()
    db.write_bytes(b"snapshot-phase-corruption" * 100)
    quarantine = integrity._quarantine_destination(db)
    pending = _pending_payload(db, quarantine, reason="synthetic crash after snapshot copy")
    integrity._write_pending_recovery(pending)
    integrity._quarantine(db, destination=quarantine)
    integrity._copy_verified_snapshot(snapshot, db)
    with fts.cursor() as conn:
        fts.insert_capture(
            conn,
            id="post-recovery-capture",
            timestamp="2026-07-12T00:01:00+00:00",
            app_name="Test App",
            bundle_id="com.persome.test",
            window_title="After snapshot copy",
            focused_role="AXTextArea",
            focused_value="preserve",
            visible_text="healthy live database must not be overwritten on resume",
            url="https://example.test/post-recovery",
        )
    integrity._set_pending_phase(pending, "snapshot_restored")

    integrity.check_and_recover()

    assert not paths.integrity_recovery_pending().exists()
    with fts.cursor() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM captures WHERE id='capture-before-crash'"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM captures WHERE id='post-recovery-capture'"
            ).fetchone()[0]
            == 1
        )


def test_pending_recovery_preserves_owner_repaired_healthy_database(ac_root: Path) -> None:
    config_mod.write_default_if_missing()
    with fts.cursor() as conn:
        fts.insert_capture(
            conn,
            id="owner-repaired-capture",
            timestamp="2026-07-12T00:00:00+00:00",
            app_name="Test App",
            bundle_id="com.persome.test",
            window_title="Owner repaired database",
            focused_role="AXTextArea",
            focused_value="preserve",
            visible_text="healthy owner repair must stay live",
            url="https://example.test/owner-repair",
        )
    manifest = create_build_manifest(
        core_commit="0123456789abcdef",
        started_at="2026-07-12T09:00:00+08:00",
        completed_at="2026-07-12T09:01:00+08:00",
    )
    paths.atomic_write_private_text(paths.model_build_manifest(), json.dumps(manifest))
    db = paths.index_db()
    quarantine = integrity._quarantine_destination(db)
    integrity._write_pending_recovery(
        _pending_payload(db, quarantine, reason="database was repaired by owner")
    )

    recovered = integrity.check_and_recover()

    assert recovered == []
    assert db.exists()
    assert not quarantine.exists()
    assert not paths.integrity_recovery_pending().exists()
    assert not paths.model_build_manifest().exists()
    with fts.cursor() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM captures WHERE id='owner-repaired-capture'"
            ).fetchone()[0]
            == 1
        )
    database = json.loads(paths.integrity_recovery_marker().read_text())["database_recovery"]
    assert database["source"] == "owner_repaired_database"
    assert database["preserved_existing_database"] is True


def test_pending_recovery_resumes_when_sidecar_moved_before_main_database(
    ac_root: Path,
) -> None:
    config_mod.write_default_if_missing()
    with fts.cursor() as conn:
        entries.create_file(
            conn,
            name="project-sidecar-crash.md",
            description="Sidecar-first quarantine crash",
            tags=[],
        )
        entry_id = entries.append_entry(
            conn,
            name="project-sidecar-crash.md",
            content="Recovery must resume after a sidecar-only quarantine artifact.",
            tags=[],
        )
    db = paths.index_db()
    quarantine = integrity._quarantine_destination(db)
    pending = _pending_payload(db, quarantine, reason="synthetic crash after WAL move")
    integrity._write_pending_recovery(pending)
    # ``_quarantine`` moves sidecars before the main file. Emulate a process
    # death after that first rename: the live main is still structurally healthy
    # without its WAL, but this is not an owner repair.
    quarantined_wal = quarantine.with_name(f"{quarantine.name}.wal")
    paths.atomic_write_private_text(quarantined_wal, "already moved WAL bytes")

    recovered = integrity.check_and_recover()

    assert any(item.kind == "database" for item in recovered)
    assert quarantine.exists()
    assert quarantined_wal.exists()
    assert not paths.integrity_recovery_pending().exists()
    with fts.cursor() as conn:
        assert (
            conn.execute("SELECT count(*) FROM entries WHERE id=?", (entry_id,)).fetchone()[0] == 1
        )
    database = json.loads(paths.integrity_recovery_marker().read_text())["database_recovery"]
    assert database["source"] != "owner_repaired_database"


def test_failed_recovery_retries_and_preserves_post_failure_writes(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_mod.write_default_if_missing()
    with fts.cursor() as conn:
        entries.create_file(
            conn,
            name="project-retry-recovery.md",
            description="Retry recovery",
            tags=[],
        )
        entry_id = entries.append_entry(
            conn,
            name="project-retry-recovery.md",
            content="Markdown recovery succeeds on retry.",
            tags=[],
        )
    paths.index_db().write_bytes(b"retryable-database-corruption" * 100)

    with monkeypatch.context() as first_attempt:
        first_attempt.setattr(
            integrity,
            "_repopulate_after_quarantine",
            lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("transient replay failure")),
        )
        integrity.check_and_recover()

    pending = json.loads(paths.integrity_recovery_pending().read_text())
    assert pending["phase"] == "failed"
    assert pending["database_recovery"]["status"] == "failed"
    with fts.cursor() as conn:
        fts.insert_capture(
            conn,
            id="capture-after-failed-recovery",
            timestamp="2026-07-12T00:02:00+00:00",
            app_name="Test App",
            bundle_id="com.persome.test",
            window_title="Post failure write",
            focused_role="AXTextArea",
            focused_value="preserve",
            visible_text="must survive the successful retry",
            url="https://example.test/retry-write",
        )

    integrity.check_and_recover()

    assert not paths.integrity_recovery_pending().exists()
    with fts.cursor() as conn:
        assert (
            conn.execute("SELECT count(*) FROM entries WHERE id=?", (entry_id,)).fetchone()[0] == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM captures WHERE id='capture-after-failed-recovery'"
            ).fetchone()[0]
            == 1
        )


def test_invalid_database_recovery_journal_is_retained_then_recovered(ac_root: Path) -> None:
    config_mod.write_default_if_missing()
    with fts.cursor() as conn:
        entries.create_file(
            conn,
            name="project-invalid-journal.md",
            description="Invalid journal recovery",
            tags=[],
        )
        entry_id = entries.append_entry(
            conn,
            name="project-invalid-journal.md",
            content="Recovery must not remain blocked.",
            tags=[],
        )
    paths.index_db().write_bytes(b"invalid-journal-database-corruption" * 100)
    paths.atomic_write_private_text(paths.integrity_recovery_pending(), '{"version":')

    recovered = integrity.check_and_recover()

    assert {item.kind for item in recovered} == {"database_recovery_journal", "database"}
    assert not paths.integrity_recovery_pending().exists()
    assert list(ac_root.glob(".integrity-recovery.pending.json.corrupt.*"))
    with fts.cursor() as conn:
        assert (
            conn.execute("SELECT count(*) FROM entries WHERE id=?", (entry_id,)).fetchone()[0] == 1
        )


@pytest.mark.parametrize("defect", ["missing_fields", "unknown_phase", "unsafe_path"])
def test_semantically_invalid_database_journal_is_quarantined_and_recovery_continues(
    ac_root: Path,
    defect: str,
) -> None:
    config_mod.write_default_if_missing()
    with fts.cursor() as conn:
        entries.create_file(
            conn,
            name="project-semantic-journal.md",
            description="Semantic journal recovery",
            tags=[],
        )
        entry_id = entries.append_entry(
            conn,
            name="project-semantic-journal.md",
            content="A malformed version-one journal must not wedge recovery.",
            tags=[],
        )
    db = paths.index_db()
    db.write_bytes(b"semantic-journal-database-corruption" * 100)
    quarantine = integrity._quarantine_destination(db)
    payload = _pending_payload(db, quarantine, reason="semantic journal test")
    if defect == "missing_fields":
        payload = {"version": 1}
    elif defect == "unknown_phase":
        payload["phase"] = "teleported"
    else:
        payload["quarantine_path"] = str(ac_root.parent / "unsafe" / quarantine.name)
    paths.atomic_write_private_text(paths.integrity_recovery_pending(), json.dumps(payload))

    recovered = integrity.check_and_recover()

    assert {item.kind for item in recovered} == {"database_recovery_journal", "database"}
    assert not paths.integrity_recovery_pending().exists()
    assert list(ac_root.glob(".integrity-recovery.pending.json.corrupt.*"))
    with fts.cursor() as conn:
        assert (
            conn.execute("SELECT count(*) FROM entries WHERE id=?", (entry_id,)).fetchone()[0] == 1
        )


def test_invalid_config_recovery_journal_does_not_block_config_repair(ac_root: Path) -> None:
    _make_healthy_db()
    paths.config_file().write_text("[[[ invalid config", encoding="utf-8")
    paths.atomic_write_private_text(
        paths.integrity_config_recovery_pending(),
        '{"version":',
    )

    recovered = integrity.check_and_recover()

    assert {item.kind for item in recovered} == {"config_recovery_journal", "config"}
    assert not paths.integrity_config_recovery_pending().exists()
    assert list(ac_root.glob(".integrity-config-recovery.pending.json.corrupt.*"))
    assert config_mod.load().evomem.write_authority == "markdown"


@pytest.mark.parametrize("defect", ["missing_fields", "unknown_phase", "unsafe_path"])
def test_semantically_invalid_config_journal_is_quarantined_and_config_repair_continues(
    ac_root: Path,
    defect: str,
) -> None:
    _make_healthy_db()
    paths.config_file().write_text("[[[ invalid config", encoding="utf-8")
    quarantine = integrity._quarantine_destination(paths.config_file())
    payload: dict[str, object] = {
        "version": 1,
        "phase": "prepared",
        "started_at": "2026-07-12T10:00:00+08:00",
        "original_path": str(paths.config_file()),
        "quarantine_path": str(quarantine),
        "reason": "semantic config journal test",
    }
    if defect == "missing_fields":
        payload = {"version": 1}
    elif defect == "unknown_phase":
        payload["phase"] = "teleported"
    else:
        payload["quarantine_path"] = str(ac_root.parent / "unsafe" / quarantine.name)
    paths.atomic_write_private_text(paths.integrity_config_recovery_pending(), json.dumps(payload))

    recovered = integrity.check_and_recover()

    assert {item.kind for item in recovered} == {"config_recovery_journal", "config"}
    assert not paths.integrity_config_recovery_pending().exists()
    assert list(ac_root.glob(".integrity-config-recovery.pending.json.corrupt.*"))
    assert config_mod.load().evomem.write_authority == "markdown"


def test_recovery_marker_blocks_stale_manifest_when_invalidation_fails(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = create_build_manifest(
        core_commit="0123456789abcdef",
        started_at="2026-07-12T09:00:00+08:00",
        completed_at="2026-07-12T09:01:00+08:00",
    )
    paths.atomic_write_private_text(paths.model_build_manifest(), json.dumps(manifest))
    paths.index_db().write_bytes(b"manifest-invalidation-corruption" * 100)
    monkeypatch.setattr(integrity, "_invalidate_model_manifest", lambda: False)

    integrity.check_and_recover()

    assert paths.model_build_manifest().exists()
    assert load_live_manifest()["status"] == "not_built"
    database = json.loads(paths.integrity_recovery_marker().read_text())["database_recovery"]
    assert database["manifest_invalidated"] is False

    # A later config-only recovery must not overwrite the sole monotonic guard
    # that keeps the stale manifest untrusted.
    paths.config_file().write_text("[[[ later config corruption", encoding="utf-8")
    integrity.check_and_recover()

    assert load_live_manifest()["status"] == "not_built"
    preserved = json.loads(paths.integrity_recovery_marker().read_text())["database_recovery"]
    assert preserved["model_rebuild_required"] is True
    assert preserved["manifest_invalidated"] is False


def test_malformed_derived_captures_fts_is_rebuilt_without_quarantine(ac_root: Path) -> None:
    _make_healthy_db()
    with fts.cursor() as conn:
        fts.insert_capture(
            conn,
            id="capture-derived-fts-test",
            timestamp="2026-07-12T00:00:00+00:00",
            app_name="Test App",
            bundle_id="com.persome.test",
            window_title="Derived index recovery",
            focused_role="AXTextArea",
            focused_value="needle",
            visible_text="needle survives a derived-index rebuild",
            url="https://example.test/recovery",
        )
        # Deleting a live FTS segment corrupts only the derived inverted index;
        # the canonical captures row remains intact.
        conn.execute("DELETE FROM captures_fts_data WHERE id > 1")

    assert "captures_fts" in (integrity._db_corruption_reason(paths.index_db()) or "")

    recovered = integrity.check_and_recover()

    assert recovered == []
    assert paths.index_db().exists()
    assert not paths.integrity_recovery_marker().exists()
    assert list(ac_root.glob("index.db.corrupt.*")) == []
    with fts.cursor() as conn:
        assert [row[0] for row in conn.execute("PRAGMA integrity_check")] == ["ok"]
        assert conn.execute("SELECT count(*) FROM captures").fetchone()[0] == 1
        hits = fts.search_captures(conn, query="needle")
    assert [hit.id for hit in hits] == ["capture-derived-fts-test"]


def test_schema_reset_rebuilds_unloadable_derived_captures_fts(ac_root: Path) -> None:
    _make_healthy_db()
    with fts.cursor() as conn:
        fts.insert_capture(
            conn,
            id="capture-schema-reset-test",
            timestamp="2026-07-12T00:00:00+00:00",
            app_name="Test App",
            bundle_id="com.persome.test",
            window_title="Schema reset recovery",
            focused_role="AXTextArea",
            focused_value="needle",
            visible_text="needle survives a schema reset",
            url="https://example.test/schema-reset",
        )
        conn.execute("DELETE FROM captures_fts_data WHERE id > 1")

    integrity._rebuild_captures_fts_via_schema_reset(paths.index_db())

    with fts.cursor() as conn:
        assert [row[0] for row in conn.execute("PRAGMA integrity_check")] == ["ok"]
        assert conn.execute("SELECT count(*) FROM captures").fetchone()[0] == 1
        hits = fts.search_captures(conn, query="needle")
    assert [hit.id for hit in hits] == ["capture-schema-reset-test"]


def test_connect_rebuilds_missing_derived_captures_fts(ac_root: Path) -> None:
    _make_healthy_db()
    with fts.cursor() as conn:
        fts.insert_capture(
            conn,
            id="capture-missing-fts-test",
            timestamp="2026-07-12T00:00:00+00:00",
            app_name="Test App",
            bundle_id="com.persome.test",
            window_title="Missing index recovery",
            focused_role="AXTextArea",
            focused_value="needle",
            visible_text="needle survives an interrupted recovery",
            url="https://example.test/missing-fts",
        )

    conn = sqlite3.connect(paths.index_db())
    try:
        conn.execute("BEGIN IMMEDIATE")
        fts.reset_corrupt_captures_fts_schema(conn)
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()

    with fts.cursor() as conn:
        assert [row[0] for row in conn.execute("PRAGMA integrity_check")] == ["ok"]
        hits = fts.search_captures(conn, query="needle")
    assert [hit.id for hit in hits] == ["capture-missing-fts-test"]


def test_schema_reset_rebuilds_all_derived_fts_from_canonical_sources(ac_root: Path) -> None:
    _make_healthy_db()
    with fts.cursor() as conn:
        entries.create_file(
            conn,
            name="project-derived-fts.md",
            description="Derived FTS recovery",
            tags=[],
        )
        memory_id = entries.append_entry(
            conn,
            name="project-derived-fts.md",
            content="durable entry index recovery",
            tags=[],
        )
        fts.insert_capture(
            conn,
            id="capture-all-derived-fts-test",
            timestamp="2026-07-12T00:00:00+00:00",
            app_name="Test App",
            bundle_id="com.persome.test",
            window_title="All derived index recovery",
            focused_role="AXTextArea",
            focused_value="needle",
            visible_text="capture index recovery survives",
            url="https://example.test/all-derived",
        )
        conn.execute("DELETE FROM entries_data WHERE id > 1")
        conn.execute("DELETE FROM captures_fts_data WHERE id > 1")

    integrity._rebuild_derived_fts_via_schema_reset(paths.index_db())

    with fts.cursor() as conn:
        assert [row[0] for row in conn.execute("PRAGMA integrity_check")] == ["ok"]
        entry_hits = fts.search(conn, query="durable")
        capture_hits = fts.search_captures(conn, query="capture")
    assert [hit.id for hit in entry_hits] == [memory_id]
    assert [hit.id for hit in capture_hits] == ["capture-all-derived-fts-test"]


def test_derived_captures_fts_repair_defers_instead_of_quarantining(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_healthy_db()
    monkeypatch.setattr(
        integrity,
        "_db_corruption_reason",
        lambda _db_path: (
            "integrity_check: malformed inverted index for FTS5 table main.captures_fts"
        ),
    )
    monkeypatch.setattr(
        integrity,
        "_try_rebuild_derived_fts",
        lambda _db_path: None,
    )

    recovered = integrity.check_and_recover()

    assert recovered == []
    assert paths.index_db().exists()
    assert list(ac_root.glob("index.db.corrupt.*")) == []


def test_derived_fts_vtable_constructor_error_is_rebuildable() -> None:
    assert integrity._derived_fts_vtable_failure(
        sqlite3.DatabaseError("vtable constructor failed: captures_fts")
    ) == {"captures_fts"}
    assert integrity._derived_fts_vtable_failure(
        sqlite3.DatabaseError("vtable constructor failed: entries")
    ) == {"entries"}
    assert (
        integrity._derived_fts_vtable_failure(
            sqlite3.DatabaseError("vtable constructor failed: unrelated")
        )
        == set()
    )


def test_derived_fts_damage_recognizes_modern_sqlite_messages() -> None:
    # SQLite >= 3.50 reports per-finding fts5 lines instead of one summary line.
    assert integrity._derived_fts_damage(
        ['fts5: corruption found reading blob 10 from table "captures_fts"']
    ) == {"captures_fts"}
    assert integrity._derived_fts_damage(
        [
            'fts5: missing row 7 from table "entries"',
            "malformed inverted index for FTS5 table main.captures_fts",
        ]
    ) == {"entries", "captures_fts"}
    # Damage naming any other table is still core damage, never a derived repair.
    assert (
        integrity._derived_fts_damage(['fts5: corruption found reading blob 3 from table "other"'])
        is None
    )
    assert integrity._derived_fts_damage(["row 1 missing from index sqlite_autoindex_files_1"]) is (
        None
    )


def test_generic_malformed_error_requires_readable_canonical_tables(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_healthy_db()
    conn = sqlite3.connect(paths.index_db())
    error = sqlite3.DatabaseError("database disk image is malformed")
    try:
        assert integrity._is_generic_derived_fts_failure(conn, error)
        monkeypatch.setattr(integrity, "_canonical_tables_are_readable", lambda _conn: False)
        assert not integrity._is_generic_derived_fts_failure(conn, error)
    finally:
        conn.close()


def test_corrupt_config_is_quarantined_and_default_rebuilt(ac_root: Path) -> None:
    config = paths.config_file()
    # Invalid TOML: dangling key with no value, unclosed table header.
    config.write_text("[models\nthis is = = not toml ===\n")

    recovered = integrity.check_and_recover()

    assert len(recovered) == 1
    q = recovered[0]
    assert q.kind == "config"
    quarantine = Path(q.quarantine_path)
    assert quarantine.exists()
    assert ".corrupt." in quarantine.name
    # A fresh default config was written and parses cleanly.
    assert config.exists()
    loaded = config_mod.load()
    assert loaded.models["default"].model  # default template populated something sane.


def test_recovery_marker_written_with_paths(ac_root: Path) -> None:
    paths.config_file().write_text("=== broken ===\n")

    integrity.check_and_recover()

    marker = paths.integrity_recovery_marker()
    assert marker.exists()
    assert marker.stat().st_mode & 0o777 == 0o600
    payload = json.loads(marker.read_text())
    assert "recovered_at" in payload
    assert len(payload["files"]) == 1
    entry = payload["files"][0]
    assert entry["kind"] == "config"
    # The corrupt path is included so the user can find / report it.
    assert ".corrupt." in entry["quarantine_path"]
    assert entry["reason"]


def test_both_db_and_config_corrupt_recovers_both(ac_root: Path) -> None:
    with fts.cursor() as conn:
        entries.create_file(
            conn,
            name="project-combined-recovery.md",
            description="Combined recovery",
            tags=[],
        )
        entry_id = entries.append_entry(
            conn,
            name="project-combined-recovery.md",
            content="Configuration is repaired before memory is replayed.",
            tags=[],
        )
    paths.index_db().write_bytes(b"garbage-not-sqlite" * 100)
    paths.config_file().write_text("[[[ not toml")

    recovered = integrity.check_and_recover()

    kinds = sorted(q.kind for q in recovered)
    assert kinds == ["config", "database"]
    payload = json.loads(paths.integrity_recovery_marker().read_text())
    assert len(payload["files"]) == 2
    assert payload["database_recovery"]["status"] == "restored"
    with fts.cursor() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM entries WHERE id=?",
                (entry_id,),
            ).fetchone()[0]
            == 1
        )


def test_no_stale_wal_sidecars_survive_next_to_rebuilt_db(ac_root: Path) -> None:
    # The safety property: after recovering a corrupt DB, no stale WAL/SHM
    # sidecar may sit next to the freshly rebuilt index.db (a half-written WAL
    # could otherwise resurrect corruption). SQLite itself may drop the
    # sidecars while we probe the garbage file; whatever the mechanism, they
    # must not remain at the live paths.
    db = paths.index_db()
    db.write_bytes(b"garbage-not-sqlite" * 100)
    db.with_name(f"{db.name}-wal").write_bytes(b"stale-wal")
    db.with_name(f"{db.name}-shm").write_bytes(b"stale-shm")

    integrity.check_and_recover()

    assert not db.with_name(f"{db.name}-wal").exists()
    assert not db.with_name(f"{db.name}-shm").exists()
    # And the rebuilt DB is healthy.
    with fts.cursor() as conn:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    assert [r[0] for r in rows] == ["ok"]


def test_quarantine_sidecar_rename_failure_aborts_before_moving_main(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = paths.index_db()
    db.write_bytes(b"corrupt-main")
    wal = db.with_name(f"{db.name}-wal")
    wal.write_bytes(b"PRIVATE_WAL_PAGE")
    real_rename = Path.rename

    def fail_wal(path: Path, target: Path):
        if path == wal:
            raise OSError("synthetic rename failure")
        return real_rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_wal)

    with pytest.raises(RuntimeError, match="cannot safely quarantine SQLite sidecar"):
        integrity._quarantine(db)

    assert db.exists()
    assert wal.exists()
    assert list(ac_root.glob("index.db.corrupt.*")) == []


def test_check_is_fast_under_500ms(ac_root: Path) -> None:
    _make_healthy_db()
    config_mod.write_default_if_missing()

    started = time.perf_counter()
    integrity.check_and_recover()
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    assert elapsed_ms < 500.0

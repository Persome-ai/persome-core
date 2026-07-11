"""Startup integrity check + auto-quarantine (#202)."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from persome import config as config_mod
from persome import integrity, paths
from persome.store import entries, fts


def _make_healthy_db() -> Path:
    """Create a real, schema-initialised SQLite DB at the canonical path."""
    with fts.cursor() as conn:
        conn.execute("SELECT 1")
    return paths.index_db()


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
    paths.index_db().write_bytes(b"garbage-not-sqlite" * 100)
    paths.config_file().write_text("[[[ not toml")

    recovered = integrity.check_and_recover()

    kinds = sorted(q.kind for q in recovered)
    assert kinds == ["config", "database"]
    payload = json.loads(paths.integrity_recovery_marker().read_text())
    assert len(payload["files"]) == 2


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

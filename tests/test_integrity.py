"""Startup integrity check + auto-quarantine (#202)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from persome import config as config_mod
from persome import integrity, paths
from persome.store import fts


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
    assert loaded.chat.model  # default template populated something sane.


def test_recovery_marker_written_with_paths(ac_root: Path) -> None:
    paths.config_file().write_text("=== broken ===\n")

    integrity.check_and_recover()

    marker = paths.integrity_recovery_marker()
    assert marker.exists()
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


def test_check_is_fast_under_500ms(ac_root: Path) -> None:
    _make_healthy_db()
    config_mod.write_default_if_missing()

    started = time.perf_counter()
    integrity.check_and_recover()
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    assert elapsed_ms < 500.0

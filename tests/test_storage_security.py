"""Security invariants for local personal-data storage and deletion."""

from __future__ import annotations

import os
import sqlite3
import stat
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from persome import cli, paths
from persome.capture import scheduler
from persome.evomem import backup
from persome.store import entries as entries_store
from persome.store import fts


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_ensure_dirs_repairs_private_directory_modes(ac_root: Path) -> None:
    for directory in (ac_root, paths.memory_dir(), paths.capture_buffer_dir(), paths.logs_dir()):
        directory.chmod(0o755)

    paths.ensure_dirs()

    for directory in (ac_root, paths.memory_dir(), paths.capture_buffer_dir(), paths.logs_dir()):
        assert _mode(directory) == 0o700


def test_ensure_dirs_hardens_existing_legacy_chat_era_trees(ac_root: Path) -> None:
    """Legacy chat-history/ and skills/ trees from Chat-era installs are never
    created, but when present they must come out of ensure_dirs owner-only."""
    legacy_trees = (ac_root / "chat-history", ac_root / "skills")
    for tree in legacy_trees:
        tree.mkdir()
        (tree / "leftover.md").write_text("legacy")
        tree.chmod(0o755)

    paths.ensure_dirs()

    for tree in legacy_trees:
        assert _mode(tree) == 0o700


def test_ensure_dirs_does_not_create_legacy_chat_era_trees(ac_root: Path) -> None:
    paths.ensure_dirs()

    assert not (ac_root / "chat-history").exists()
    assert not (ac_root / "skills").exists()


def test_permission_marker_symlink_cannot_overwrite_target(ac_root: Path) -> None:
    marker = ac_root / ".permissions-v1"
    marker.unlink()
    target = ac_root.parent / "unrelated.txt"
    target.write_text("do not overwrite", encoding="utf-8")
    target.chmod(0o644)
    marker.symlink_to(target)

    with pytest.raises(RuntimeError, match="must not be a symlink"):
        paths.ensure_dirs()

    assert target.read_text(encoding="utf-8") == "do not overwrite"
    assert _mode(target) == 0o644


def test_permission_marker_rejects_non_regular_file(ac_root: Path) -> None:
    marker = ac_root / ".permissions-v1"
    marker.unlink()
    os.mkfifo(marker)

    with pytest.raises(RuntimeError, match="not a regular file"):
        paths.ensure_dirs()


def test_private_directory_rejects_symlink(ac_root: Path) -> None:
    capture_dir = paths.capture_buffer_dir()
    capture_dir.rmdir()
    target = ac_root.parent / "external-captures"
    target.mkdir(mode=0o755)
    capture_dir.symlink_to(target, target_is_directory=True)

    with pytest.raises(RuntimeError, match="must not be a symlink"):
        paths.ensure_dirs()

    assert _mode(target) == 0o755


def test_private_file_rejects_hard_link_without_chmodding_victim(ac_root: Path) -> None:
    victim = ac_root.parent / "external-victim.txt"
    victim.write_text("unchanged", encoding="utf-8")
    victim.chmod(0o644)
    linked = ac_root / "hard-linked-state"
    os.link(victim, linked)

    with pytest.raises(RuntimeError, match="must not be hard-linked"):
        paths.ensure_private_file(linked)

    assert victim.read_text(encoding="utf-8") == "unchanged"
    assert _mode(victim) == 0o644


def test_private_file_tolerates_sqlite_sidecar_disappearing_during_validation(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar = ac_root / "index.db-wal"
    sidecar.write_bytes(b"ephemeral SQLite pages")
    real_open = os.open

    def open_then_unlink(path, flags, mode=0o777):  # noqa: ANN001
        fd = real_open(path, flags, mode)
        Path(path).unlink()
        return fd

    monkeypatch.setattr(paths.os, "open", open_then_unlink)

    assert paths.ensure_private_file(sidecar) == sidecar
    assert not sidecar.exists()


def test_permission_migration_refuses_hard_link_without_chmodding_victim(ac_root: Path) -> None:
    (ac_root / ".permissions-v1").unlink()
    victim = ac_root.parent / "migration-victim.txt"
    victim.write_text("unchanged", encoding="utf-8")
    victim.chmod(0o644)
    os.link(victim, paths.capture_buffer_dir() / "linked.json")

    with pytest.raises(RuntimeError, match="permission migration refuses hard-linked"):
        paths.ensure_dirs()

    assert victim.read_text(encoding="utf-8") == "unchanged"
    assert _mode(victim) == 0o644


def test_sqlite_and_capture_files_are_owner_only(ac_root: Path) -> None:
    with fts.cursor() as conn:
        assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
        for table in ("entries", "captures_fts"):
            assert (
                conn.execute(f"SELECT v FROM {table}_config WHERE k='secure-delete'").fetchone()[0]
                == 1
            )
        fts.insert_capture(
            conn,
            id="private",
            timestamp="2026-07-11T12:00:00+08:00",
            app_name="Private",
            bundle_id="private.app",
            window_title="Private",
            focused_role="AXTextArea",
            focused_value="secret",
            visible_text="secret",
            url="",
        )
    assert _mode(paths.index_db()) == 0o600
    for sidecar in (Path(f"{paths.index_db()}-wal"), Path(f"{paths.index_db()}-shm")):
        if sidecar.exists():
            assert _mode(sidecar) == 0o600

    capture = scheduler._write_capture(
        {
            "timestamp": "2026-07-11T12:00:01+08:00",
            "schema_version": 2,
            "trigger": {"event_type": "manual"},
            "window_meta": {"app_name": "Private", "title": "Private", "bundle_id": "x"},
            "focused_element": {"role": "AXTextArea", "value": "secret"},
            "visible_text": "secret",
            "url": "",
        }
    )
    assert _mode(capture) == 0o600


def test_capture_atomic_write_replaces_symlink_without_touching_victim(ac_root: Path) -> None:
    timestamp = "2026-07-11T12:00:09+08:00"
    target = paths.capture_buffer_dir() / f"{scheduler._safe_filename(timestamp)}.json"
    victim = ac_root.parent / "victim.txt"
    victim.write_text("do not overwrite", encoding="utf-8")
    victim.chmod(0o644)
    target.symlink_to(victim)

    written = scheduler._write_capture(
        {
            "timestamp": timestamp,
            "schema_version": 2,
            "trigger": {"event_type": "manual"},
            "window_meta": {"app_name": "Private", "title": "Private", "bundle_id": "x"},
            "focused_element": {"role": "AXTextArea", "value": "safe"},
            "visible_text": "safe",
            "url": "",
        }
    )

    assert written == target
    assert target.is_file() and not target.is_symlink()
    assert victim.read_text(encoding="utf-8") == "do not overwrite"
    assert _mode(victim) == 0o644


def test_private_atomic_write_replaces_fifo_without_blocking(ac_root: Path) -> None:
    target = paths.logs_dir() / "state.json"
    os.mkfifo(target)

    paths.atomic_write_private_text(target, '{"safe": true}\n')

    assert target.is_file()
    assert target.read_text(encoding="utf-8") == '{"safe": true}\n'
    assert _mode(target) == 0o600


def test_private_lock_open_rejects_symlink_and_fifo(ac_root: Path) -> None:
    victim = ac_root.parent / "lock-victim.txt"
    victim.write_text("ORIGINAL", encoding="utf-8")
    victim.chmod(0o644)
    symlink = paths.root() / "attacker.lock"
    symlink.symlink_to(victim)

    with pytest.raises(OSError):
        paths.open_private_lock_file(symlink)
    assert victim.read_text(encoding="utf-8") == "ORIGINAL"
    assert _mode(victim) == 0o644

    fifo = paths.root() / "fifo.lock"
    os.mkfifo(fifo)
    with pytest.raises(RuntimeError, match="regular inode"):
        paths.open_private_lock_file(fifo)


def test_explicit_external_sqlite_keeps_caller_permissions(tmp_path: Path, ac_root: Path) -> None:
    external = tmp_path / "shared.db"
    sqlite3.connect(external).close()
    external.chmod(0o640)

    with fts.cursor(external):
        pass

    assert _mode(external) == 0o640


def test_internal_sqlite_rejects_symlink_escape(tmp_path: Path, ac_root: Path) -> None:
    external = tmp_path / "outside.db"
    sqlite3.connect(external).close()
    paths.index_db().symlink_to(external)

    with pytest.raises(RuntimeError, match="escapes PERSOME_ROOT"):
        fts.connect()


def test_internal_sqlite_rejects_symlink_within_root(ac_root: Path) -> None:
    target = ac_root / "other.db"
    sqlite3.connect(target).close()
    paths.index_db().symlink_to(target)

    with pytest.raises(RuntimeError, match="must not be a symlink"):
        fts.connect()


def test_internal_sqlite_rejects_preexisting_sidecar_symlink(ac_root: Path) -> None:
    victim = ac_root.parent / "wal-victim.txt"
    victim.write_text("ORIGINAL", encoding="utf-8")
    victim.chmod(0o644)
    wal = paths.index_db().with_name(paths.index_db().name + "-wal")
    wal.symlink_to(victim)

    with pytest.raises(RuntimeError, match="must not be a symlink"):
        fts.connect()

    assert victim.read_text(encoding="utf-8") == "ORIGINAL"
    assert _mode(victim) == 0o644


def test_internal_sqlite_checks_symlink_parent_before_creating_directories(
    tmp_path: Path, ac_root: Path
) -> None:
    external = tmp_path / "outside"
    external.mkdir()
    link = ac_root / "linked-parent"
    link.symlink_to(external, target_is_directory=True)
    db_path = link / "must-not-be-created" / "index.db"

    with pytest.raises(RuntimeError, match="escapes PERSOME_ROOT"):
        fts.connect(db_path)

    assert not (external / "must-not-be-created").exists()


def test_connect_purges_terms_deleted_by_legacy_fts_settings(ac_root: Path) -> None:
    entry_secret = "zzzzhistoricdeletedentrysecretzzzz"
    capture_secret = "zzzzhistoricdeletedcapturesecretzzzz"
    db = paths.index_db()
    with sqlite3.connect(db) as conn:
        conn.executescript(fts.SCHEMA)
        conn.execute("INSERT INTO entries(entries, rank) VALUES('secure-delete', 0)")
        conn.execute("INSERT INTO captures_fts(captures_fts, rank) VALUES('secure-delete', 0)")
        conn.execute(
            "INSERT INTO entries(id,path,prefix,timestamp,tags,content,superseded) "
            "VALUES('legacy-entry','project-x.md','project-','2020','',?,0)",
            (entry_secret,),
        )
        conn.execute("DELETE FROM entries WHERE id='legacy-entry'")
        conn.execute(
            "INSERT INTO captures(id,timestamp,visible_text) VALUES('legacy-capture','2020',?)",
            (capture_secret,),
        )
        conn.execute("DELETE FROM captures WHERE id='legacy-capture'")
        conn.commit()
        conn.execute("VACUUM")
    legacy_bytes = db.read_bytes()
    assert entry_secret.encode() in legacy_bytes
    assert capture_secret.encode() in legacy_bytes

    with fts.cursor() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] >= 1

    artifacts = [db, Path(f"{db}-wal"), Path(f"{db}-shm"), Path(f"{db}-journal")]
    forensic = b"".join(path.read_bytes() for path in artifacts if path.exists())
    assert entry_secret.encode() not in forensic
    assert capture_secret.encode() not in forensic


def test_ingest_future_timestamp_is_clamped_to_server_clock(ac_root: Path) -> None:
    future = (datetime.now(UTC) + timedelta(days=3650)).isoformat()
    sanitized = scheduler._sanitize_ingest_timestamp(future)
    parsed = datetime.fromisoformat(sanitized).astimezone(UTC)
    assert parsed < datetime.now(UTC) + timedelta(minutes=6)
    assert sanitized != future


def test_ingest_timestamp_is_canonicalized_for_capture_ids(ac_root: Path) -> None:
    source = datetime.now(UTC).replace(microsecond=987654)
    alternate_offset = source.astimezone(timezone(timedelta(hours=-7))).isoformat()

    sanitized = scheduler._sanitize_ingest_timestamp(alternate_offset)

    parsed = datetime.fromisoformat(sanitized)
    assert parsed.microsecond == source.microsecond
    assert parsed.utcoffset() == timedelta(0)
    assert abs((parsed.astimezone(UTC) - source.replace(microsecond=0)).total_seconds()) < 1


def test_ingest_timestamp_keys_remain_monotonic_across_dst_fallback(ac_root: Path) -> None:
    before = scheduler._sanitize_ingest_timestamp("2025-11-02T01:59:59-04:00")
    after = scheduler._sanitize_ingest_timestamp("2025-11-02T01:00:00-05:00")

    assert datetime.fromisoformat(after) - datetime.fromisoformat(before) == timedelta(seconds=1)
    assert scheduler._safe_filename(before) < scheduler._safe_filename(after)


def test_distinct_subsecond_ingests_do_not_overwrite(ac_root: Path) -> None:
    first = scheduler._sanitize_ingest_timestamp("2026-07-11T09:00:00.123456+00:00")
    second = scheduler._sanitize_ingest_timestamp("2026-07-11T09:00:00.987654+00:00")
    assert first != second

    for timestamp, content in ((first, "secret-1"), (second, "secret-2")):
        scheduler._write_capture(
            {
                "timestamp": timestamp,
                "schema_version": 2,
                "trigger": {"event_type": "manual"},
                "window_meta": {"app_name": "Private", "title": content, "bundle_id": "x"},
                "focused_element": {"role": "AXTextArea", "value": content},
                "visible_text": content,
                "url": "",
            }
        )

    captures = list(paths.capture_buffer_dir().glob("*.json"))
    assert len(captures) == 2
    payloads = [path.read_text(encoding="utf-8") for path in captures]
    assert any("secret-1" in payload for payload in payloads)
    assert any("secret-2" in payload for payload in payloads)


def test_size_cap_evicts_unabsorbed_captures(ac_root: Path) -> None:
    written: list[Path] = []
    for i in range(3):
        written.append(
            scheduler._write_capture(
                {
                    "timestamp": f"2026-07-11T12:00:0{i}+08:00",
                    "schema_version": 2,
                    "trigger": {"event_type": "manual"},
                    "window_meta": {"app_name": "Private", "title": str(i), "bundle_id": "x"},
                    "focused_element": {"role": "AXTextArea", "value": ""},
                    "visible_text": "x" * 500_000,
                    "url": "",
                }
            )
        )
        os.utime(written[-1], (1_700_000_000 + i, 1_700_000_000 + i))

    stats = scheduler.cleanup_buffer(
        retention_hours=24 * 365,
        # Every file is deliberately unabsorbed. The disk cap must still win.
        processed_before_ts="2020-01-01T00:00:00+00:00",
        max_mb=1,
    )

    assert stats["evicted"] >= 1
    assert sum(p.stat().st_size for p in paths.capture_buffer_dir().glob("*.json")) <= 1024**2


def test_clean_captures_scrubs_daily_snapshots(ac_root: Path) -> None:
    with fts.cursor() as conn:
        fts.insert_capture(
            conn,
            id="snapshot-secret",
            timestamp="2026-07-11T12:00:00+08:00",
            app_name="Private",
            bundle_id="private.app",
            window_title="Private",
            focused_role="AXTextArea",
            focused_value="secret",
            visible_text="secret",
            url="",
        )
    snapshot = backup.create_snapshot(now=datetime(2026, 7, 11, 12, 0))
    assert snapshot is not None
    with sqlite3.connect(snapshot) as conn:
        conn.execute("INSERT INTO captures_fts(captures_fts, rank) VALUES('secure-delete', 0)")

    cli._clean_captures()

    with sqlite3.connect(snapshot) as conn:
        assert conn.execute("SELECT COUNT(*) FROM captures").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM captures_fts WHERE captures_fts MATCH 'secret'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT v FROM captures_fts_config WHERE k='secure-delete'").fetchone()[0]
            == 1
        )


def test_snapshot_scrub_purges_pre_security_deleted_fts_terms(ac_root: Path) -> None:
    secret = "zzzzhistoricsnapshotcapturesecretzzzz"
    paths.backup_dir().mkdir(parents=True, exist_ok=True)
    snapshot = paths.backup_dir() / "evo-20200101.db"
    with sqlite3.connect(snapshot) as conn:
        conn.executescript(fts.SCHEMA)
        conn.execute("INSERT INTO captures_fts(captures_fts, rank) VALUES('secure-delete', 0)")
        conn.execute(
            "INSERT INTO captures(id,timestamp,visible_text) VALUES('legacy','2020',?)",
            (secret,),
        )
        conn.execute("DELETE FROM captures WHERE id='legacy'")
        conn.commit()
        conn.execute("VACUUM")
    assert secret.encode() in snapshot.read_bytes()

    assert backup.scrub_database_copies(("captures",), (snapshot,)) == 1

    assert secret.encode() not in snapshot.read_bytes()


def test_snapshot_scrub_rejects_hard_link_before_mutating_external_db(ac_root: Path) -> None:
    victim = ac_root.parent / "external-recovery.db"
    with sqlite3.connect(victim) as conn:
        conn.executescript(fts.SCHEMA)
        conn.execute(
            "INSERT INTO captures(id,timestamp,visible_text) VALUES('external','2026-07-11',?)",
            ("must remain",),
        )
    victim.chmod(0o644)
    before = victim.read_bytes()

    paths.backup_dir().mkdir(parents=True, exist_ok=True)
    linked = paths.backup_dir() / "evo-20260711.db"
    os.link(victim, linked)

    assert backup.scrub_database_copies(("captures",), (linked,)) == 1

    assert not linked.exists()
    assert victim.read_bytes() == before
    assert _mode(victim) == 0o644
    with sqlite3.connect(victim) as conn:
        assert conn.execute("SELECT visible_text FROM captures").fetchone()[0] == "must remain"


def test_clean_captures_removes_unpromoted_snapshot_and_sidecars(ac_root: Path) -> None:
    with fts.cursor() as conn:
        fts.insert_capture(
            conn,
            id="temporary-snapshot-secret",
            timestamp="2026-07-11T12:00:00+08:00",
            app_name="Private",
            bundle_id="private.app",
            window_title="Private",
            focused_role="AXTextArea",
            focused_value="secret",
            visible_text="secret",
            url="",
        )
    snapshot = backup.create_snapshot(now=datetime(2026, 7, 11, 12, 0))
    assert snapshot is not None
    temporary = snapshot.with_name(f"{snapshot.name}.tmp")
    snapshot.rename(temporary)
    orphan_wal = paths.backup_dir() / "evo-orphan.db-wal"
    orphan_wal.write_bytes(b"private pages")

    cli._clean_captures()

    assert not temporary.exists()
    assert not orphan_wal.exists()


def test_clean_captures_scrubs_integrity_quarantine_and_removes_journal(ac_root: Path) -> None:
    with fts.cursor() as conn:
        fts.insert_capture(
            conn,
            id="quarantine-secret",
            timestamp="2026-07-11T12:00:00+08:00",
            app_name="Private",
            bundle_id="private.app",
            window_title="Private",
            focused_role="AXTextArea",
            focused_value="secret",
            visible_text="secret",
            url="",
        )
    quarantined = paths.root() / "index.db.corrupt.20260711-120000"
    with sqlite3.connect(paths.index_db()) as source, sqlite3.connect(quarantined) as target:
        source.backup(target)
    renamed_wal = quarantined.with_name(f"{quarantined.name}.wal")
    renamed_wal.write_bytes(b"private pages")

    cli._clean_captures()

    with sqlite3.connect(quarantined) as conn:
        assert conn.execute("SELECT COUNT(*) FROM captures").fetchone()[0] == 0
    assert not renamed_wal.exists()


def test_clean_all_removes_quarantined_personal_data(ac_root: Path) -> None:
    quarantined = paths.root() / "index.db.corrupt.20260711T120000"
    quarantined.write_text("private remnants", encoding="utf-8")
    journal = paths.root() / "index.db-journal"
    journal.write_text("private journal pages", encoding="utf-8")

    cli.clean_all(yes=True)

    assert not quarantined.exists()
    assert not journal.exists()


def test_clean_commands_remove_atomic_crash_artifacts(ac_root: Path) -> None:
    memory_temp = paths.memory_dir() / ".project-private.md.crash"
    memory_temp.write_text("PRIVATE_MEMORY_SECRET", encoding="utf-8")
    model_temp = paths.root() / ".model-build.json.crash"
    marker_temp = paths.root() / ".integrity-recovery.json.crash"
    model_temp.write_text("PRIVATE_MODEL_RANGE", encoding="utf-8")
    marker_temp.write_text("PRIVATE_LOCAL_PATH", encoding="utf-8")

    cli._clean_memory()

    assert not memory_temp.exists()
    assert not model_temp.exists()
    assert not marker_temp.exists()


def test_memory_rebuild_prunes_orphan_derived_rows(ac_root: Path) -> None:
    with fts.cursor() as conn:
        fts.insert_entry(
            conn,
            id="deleted-entry",
            path="project-deleted.md",
            prefix="project-",
            timestamp="2026-07-11T12:00",
            tags="private",
            content="deleted private fact",
            superseded=0,
        )
        conn.execute(
            "INSERT INTO entry_temporal(entry_id, valid_from) VALUES (?, ?)",
            ("deleted-entry", "2026-07-11T12:00"),
        )
        conn.execute(
            "INSERT INTO entry_retrieval_stats(entry_id, retrieval_count) VALUES (?, ?)",
            ("deleted-entry", 1),
        )
        conn.execute(
            "INSERT INTO entry_vectors(entry_id, dim, model, vector, embedded_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("deleted-entry", 1, "test", b"private-vector", "2026-07-11T12:01"),
        )

        assert entries_store.rebuild_index(conn) == (0, 0)
        for table in ("entries", "entry_temporal", "entry_retrieval_stats", "entry_vectors"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_memory_rebuild_prunes_vectors_for_superseded_entries(ac_root: Path) -> None:
    with fts.cursor() as conn:
        entries_store.create_file(
            conn,
            name="project-private.md",
            description="Private",
            tags=["private"],
        )
        entry_id = entries_store.append_entry(
            conn,
            name="project-private.md",
            content="obsolete private fact",
            tags=["private"],
        )
        entries_store.mark_entry_deleted(
            conn,
            name="project-private.md",
            entry_id=entry_id,
        )
        conn.execute(
            "INSERT INTO entry_vectors(entry_id, dim, model, vector, embedded_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (entry_id, 1, "test", b"obsolete-vector", "2026-07-11T12:01"),
        )
        conn.execute(
            "INSERT INTO vector_queue(entry_id, enqueued_at) VALUES (?, ?)",
            (entry_id, "2026-07-11T12:01"),
        )

        entries_store.rebuild_index(conn)

        assert conn.execute("SELECT superseded FROM entries WHERE id=?", (entry_id,)).fetchone()[0]
        assert conn.execute("SELECT COUNT(*) FROM entry_vectors").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM vector_queue").fetchone()[0] == 0


def test_stuck_copy_does_not_shield_later_snapshots_from_erasure(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "zzzzlatersnapshotsecretzzzz"
    paths.backup_dir().mkdir(parents=True, exist_ok=True)
    stuck = paths.backup_dir() / "evo-20200101.db"
    stuck.write_bytes(b"this is not a sqlite database")
    later = paths.backup_dir() / "evo-20260711.db"
    with sqlite3.connect(later) as conn:
        conn.executescript(fts.SCHEMA)
        conn.execute(
            "INSERT INTO captures(id,timestamp,visible_text) VALUES('later','2026',?)",
            (secret,),
        )
        conn.commit()
    assert secret.encode() in later.read_bytes()

    real_remove = backup._remove_sqlite_copy

    def flaky_remove(main: Path) -> None:
        if main.name == stuck.name:
            raise RuntimeError(f"cannot remove unsanitized SQLite artifact {main}: stuck inode")
        real_remove(main)

    monkeypatch.setattr(backup, "_remove_sqlite_copy", flaky_remove)

    with pytest.raises(RuntimeError, match="erasure incomplete.*evo-20200101"):
        backup.scrub_database_copies(("captures",), (stuck, later))

    # The sweep completed past the stuck copy: the later snapshot was scrubbed.
    assert secret.encode() not in later.read_bytes()
    with sqlite3.connect(later) as conn:
        assert conn.execute("SELECT COUNT(*) FROM captures").fetchone()[0] == 0

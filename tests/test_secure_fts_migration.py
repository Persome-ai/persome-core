"""Regression tests for the one-time secure-FTS purge in ``fts.connect``.

2026-07-13 index.db corruption postmortem: the purge raised on a busy
``wal_checkpoint(TRUNCATE)`` *after* the FTS rebuild had already committed, and
only recorded ``user_version`` at the very end. On a live daemon (which always
has concurrent readers) the migration therefore never completed and re-ran a
full double-FTS rebuild + VACUUM on every single connection for days: capture
indexing failed with "database is locked", the daily ``VACUUM INTO`` snapshot
died inside ``connect()`` (last good backup went stale), and a full-database
rewrite raced every other writer and the hard-exit restart path until page 1
of index.db was overwritten by a relocated FTS leaf page.

These tests pin the fixed contract:

  * the rebuild is atomic with its ``user_version`` milestone and runs once,
  * a connection is NEVER refused because the compaction cannot win a quiet
    moment (concurrent readers must not fail ``connect()``),
  * the daily snapshot works while the purge is still pending,
  * the legacy-byte erasure guarantee still holds once compaction completes.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from persome import paths
from persome.store import fts


def _seed_legacy_db(db: Path, *, entry_secret: str, capture_secret: str) -> None:
    """Create a pre-migration DB whose deleted rows left reconstructable bytes."""
    with sqlite3.connect(db) as conn:
        conn.executescript(fts.SCHEMA)
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
        # A live row so VACUUM INTO snapshots and FTS rebuilds have real work.
        conn.execute(
            "INSERT INTO captures(id,timestamp,app_name,visible_text) "
            "VALUES('live-capture','2026','TestApp','still here')"
        )
        conn.commit()


def _user_version(db: Path) -> int:
    with sqlite3.connect(db) as conn:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _forensic_bytes(db: Path) -> bytes:
    artifacts = [db, Path(f"{db}-wal"), Path(f"{db}-shm"), Path(f"{db}-journal")]
    return b"".join(path.read_bytes() for path in artifacts if path.exists())


def test_purge_records_both_milestones_on_a_quiet_database(ac_root: Path) -> None:
    db = paths.index_db()
    _seed_legacy_db(db, entry_secret="quietentrysecret", capture_secret="quietcapturesecret")

    with fts.cursor():
        pass

    assert _user_version(db) >= fts._SECURE_FTS_COMPACT_VERSION
    forensic = _forensic_bytes(db)
    assert b"quietentrysecret" not in forensic
    assert b"quietcapturesecret" not in forensic


def test_purge_rebuild_runs_exactly_once_across_connections(ac_root: Path, monkeypatch) -> None:
    """The full-FTS rewrite must never repeat once its milestone is recorded."""
    db = paths.index_db()
    _seed_legacy_db(db, entry_secret="onceentrysecret", capture_secret="oncecapturesecret")

    statements: list[str] = []
    real_connect = sqlite3.connect

    def spy_connect(*args, **kwargs):  # type: ignore[no-untyped-def]
        conn = real_connect(*args, **kwargs)
        if kwargs.get("check_same_thread") is False:  # only fts.connect's own opens
            conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(fts.sqlite3, "connect", spy_connect)

    with fts.cursor():
        pass
    first_rebuilds = [s for s in statements if "VALUES('rebuild')" in s]
    assert len(first_rebuilds) == len(fts._FTS_TABLES)

    statements.clear()
    with fts.cursor():
        pass
    assert [s for s in statements if "VALUES('rebuild')" in s] == []
    assert [s for s in statements if s.strip().upper() == "VACUUM"] == []


def test_connect_succeeds_with_concurrent_reader_holding_the_wal(ac_root: Path) -> None:
    """THE 2026-07-13 regression: a concurrent reader must not fail connect().

    The old code raised ``RuntimeError: SQLite secure FTS5 setup or
    legacy-content purge failed`` here (busy TRUNCATE checkpoint), which made
    every daemon subsystem fail and the migration restart forever.
    """
    db = paths.index_db()
    _seed_legacy_db(db, entry_secret="busyentrysecret", capture_secret="busycapturesecret")

    blocker = sqlite3.connect(db, timeout=10.0)
    try:
        blocker.execute("PRAGMA journal_mode=WAL")
        blocker.execute("BEGIN")
        blocker.execute("SELECT count(*) FROM captures").fetchone()  # pin a WAL snapshot

        with fts.cursor() as conn:
            # The connection is fully usable despite the pinned reader.
            conn.execute(
                "INSERT INTO captures(id,timestamp,visible_text) "
                "VALUES('during-block','2026','written under contention')"
            )
        # The atomic rebuild milestone committed (WAL readers don't block
        # writers); only the compaction may still be pending.
        assert _user_version(db) >= fts._SECURE_FTS_REBUILD_VERSION
    finally:
        blocker.close()

    # Once the reader is gone, the next connection finishes compaction and the
    # legacy bytes are gone from every artifact.
    with fts.cursor():
        pass
    assert _user_version(db) >= fts._SECURE_FTS_COMPACT_VERSION
    forensic = _forensic_bytes(db)
    assert b"busyentrysecret" not in forensic
    assert b"busycapturesecret" not in forensic


def test_concurrent_connect_storm_leaves_database_intact(ac_root: Path) -> None:
    """Boot-shaped race: many subsystems connect at once on a pre-purge DB."""
    db = paths.index_db()
    _seed_legacy_db(db, entry_secret="stormentrysecret", capture_secret="stormcapturesecret")

    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        try:
            barrier.wait(timeout=30)
            with fts.cursor() as conn:
                conn.execute(
                    "INSERT INTO captures(id,timestamp,visible_text) VALUES(?,?,?)",
                    (f"storm-{i}", "2026", f"storm capture {i}"),
                )
        except BaseException as exc:  # noqa: BLE001 — assert on it below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert errors == []
    assert _user_version(db) >= fts._SECURE_FTS_REBUILD_VERSION
    with sqlite3.connect(db) as conn:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
        assert [str(r[0]) for r in rows] == ["ok"]
        assert (
            conn.execute("SELECT count(*) FROM captures WHERE id LIKE 'storm-%'").fetchone()[0] == 8
        )
        # Page 1 must still be the database header, not a relocated page image.
    assert db.read_bytes()[:16] == b"SQLite format 3\x00"


def test_daily_snapshot_survives_pending_purge_and_concurrent_reader(ac_root: Path) -> None:
    """The dead-backup regression: ``VACUUM INTO`` snapshots died inside
    ``connect()`` while the purge was pending, so the last good backup went
    stale for days before the corruption hit."""
    from persome.evomem import backup

    db = paths.index_db()
    _seed_legacy_db(db, entry_secret="snapentrysecret", capture_secret="snapcapturesecret")

    blocker = sqlite3.connect(db, timeout=10.0)
    try:
        blocker.execute("PRAGMA journal_mode=WAL")
        blocker.execute("BEGIN")
        blocker.execute("SELECT count(*) FROM captures").fetchone()

        dest = backup.create_snapshot()
    finally:
        blocker.close()

    assert dest is not None and dest.exists()
    with sqlite3.connect(f"file:{dest}?mode=ro", uri=True) as snap:
        assert (
            snap.execute("SELECT count(*) FROM captures WHERE id='live-capture'").fetchone()[0] == 1
        )

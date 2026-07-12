"Validated SQLite snapshots for canonical evomem state."

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path

from .. import paths
from ..config import Config
from ..logger import get
from . import integrity

_log = get("persome.evomem")

_SNAPSHOT_RE = re.compile(r"^evo-(\d{8})\.db$")
_SCRUBBABLE_TABLES = frozenset({"captures", "timeline_blocks"})


def _local_today(now: datetime | None = None) -> date:
    return (now or datetime.now().astimezone()).date()


def snapshot_path(day: date) -> Path:
    return paths.backup_dir() / f"evo-{day.strftime('%Y%m%d')}.db"


def create_snapshot(
    *,
    db_path: Path | None = None,
    now: datetime | None = None,
    structural_only: bool = False,
) -> Path | None:
    from ..store import fts  # local import keeps module import light

    dest = snapshot_path(_local_today(now))
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        paths.ensure_private_dir(paths.backup_dir())
        # A prior crash may leave a valid-looking main plus a hot WAL. Remove
        # the complete temporary copy before SQLite can replay any stale pages.
        _remove_sqlite_copy(tmp)
        with fts.cursor(db_path) as conn:
            conn.execute("VACUUM INTO ?", (str(tmp),))
    except (sqlite3.Error, OSError, RuntimeError) as e:
        integrity.emit_alert(
            "snapshot_failed",
            f"VACUUM INTO {tmp.name} failed: {e}",
            source="snapshot",
        )
        _remove_sqlite_copy(tmp)
        return None

    violations = integrity.verify_snapshot(tmp)
    for v in violations:
        integrity.emit_alert(
            "snapshot_verification",
            f"{tmp.name}: {v.check}: {v.detail}",
            source="snapshot",
            structural=v.structural,
        )
    blocking = [v for v in violations if v.structural] if structural_only else violations
    if blocking:
        _remove_sqlite_copy(tmp)
        _log.error(
            "snapshot %s FAILED verification (%d violation(s), %d blocking) — discarded, "
            "existing snapshot (if any) preserved",
            dest.name,
            len(violations),
            len(blocking),
        )
        return None

    try:
        # Destination-named journals belong to the old same-day snapshot. They
        # must be gone before promotion or SQLite can replay them onto the new
        # main and resurrect deleted rows. Verification may also have created
        # temporary-named sidecars, which have no recovery contract.
        _remove_sqlite_sidecars(tmp)
        _remove_sqlite_sidecars(dest)
        tmp.replace(dest)  # atomic same-day refresh
        _remove_sqlite_sidecars(dest)
        paths.ensure_private_file(dest)
    except (OSError, RuntimeError) as e:
        integrity.emit_alert(
            "snapshot_failed",
            f"safe promotion of {tmp.name} failed: {e}",
            source="snapshot",
        )
        _remove_sqlite_copy(tmp)
        return None
    # The snapshot is already promoted and successful at this point. The size is
    # for an info log only, so a concurrent cleanup of ``dest`` (e.g. a later
    # ``apply_retention`` in the same daily tick) racing the ``stat()`` must NOT
    # turn a successful snapshot into a raised exception — that would both
    # violate the documented never-raises contract and make the caller
    # (``run_daily_backup``) mis-report a good snapshot as a failure (#488).
    try:
        size = dest.stat().st_size
    except OSError:
        size = -1
    _log.info("snapshot written: %s (%d bytes)", dest.name, size)
    return dest


def _validated_scrub_tables(tables: Iterable[str]) -> tuple[str, ...]:
    requested = tuple(dict.fromkeys(tables))
    invalid = set(requested) - _SCRUBBABLE_TABLES
    if invalid:
        raise ValueError(f"unsupported snapshot scrub table(s): {sorted(invalid)}")
    return requested


def _sqlite_copy_artifacts(main: Path) -> tuple[Path, ...]:
    return (
        main,
        main.with_name(f"{main.name}-wal"),
        main.with_name(f"{main.name}-shm"),
        main.with_name(f"{main.name}-journal"),
        main.with_name(f"{main.name}.wal"),
        main.with_name(f"{main.name}.shm"),
        main.with_name(f"{main.name}.journal"),
    )


def _remove_sqlite_copy(main: Path) -> None:
    artifacts = _sqlite_copy_artifacts(main)
    # Main-last keeps the snapshot discoverable for a later retention retry if
    # a sidecar cannot be removed during this pass.
    for artifact in (*artifacts[1:], artifacts[0]):
        try:
            artifact.unlink(missing_ok=True)
        except OSError as exc:
            raise RuntimeError(
                f"cannot remove unsanitized SQLite artifact {artifact}: {exc}"
            ) from exc


def _remove_sqlite_sidecars(main: Path) -> None:
    """Remove journals after an offline copy has been checkpointed and scrubbed."""
    for artifact in _sqlite_copy_artifacts(main)[1:]:
        try:
            artifact.unlink(missing_ok=True)
        except OSError as exc:
            raise RuntimeError(f"cannot remove scrubbed SQLite sidecar {artifact}: {exc}") from exc


def scrub_database_copies(
    tables: Iterable[str],
    artifacts: Iterable[Path],
    *,
    remove_after: bool = False,
) -> int:
    """Securely scrub selected tables from offline SQLite copies.

    Corrupt/unreadable copies are deleted rather than silently retained. This
    helper also handles integrity-quarantine databases outside ``backup/``.

    Returns the number of copies whose erasure verifiably completed (scrubbed
    in place, or removed entirely). A copy that can be neither scrubbed nor
    removed does not count and never blocks the rest of the sweep; the sweep
    finishes first and then raises ``RuntimeError`` naming every leftover.
    """
    requested = _validated_scrub_tables(tables)
    candidates = tuple(dict.fromkeys(Path(item) for item in artifacts))
    if not requested or not candidates:
        return 0

    scrubbed = 0
    leftovers: list[str] = []
    for snapshot in sorted(candidates):
        conn: sqlite3.Connection | None = None
        try:
            # Validate the inode before SQLite can write through this path.
            # A symlink or hard link may point at an unrelated owner file;
            # checking only after DELETE/VACUUM would mutate that external
            # database before the safety check had a chance to reject it.
            paths.ensure_private_file(snapshot)
            conn = sqlite3.connect(snapshot, timeout=10.0)
            conn.execute("PRAGMA secure_delete=ON")
            # Snapshots are offline recovery artifacts; DELETE journaling keeps
            # the scrub self-contained instead of leaving WAL sidecars behind.
            conn.execute("PRAGMA journal_mode=DELETE")
            for table in requested:
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
                if exists is not None:
                    if table == "captures":
                        # Old snapshots may predate the runtime-wide FTS5
                        # secure-delete setting. Enable it before the trigger
                        # removes capture terms; core secure_delete alone does
                        # not erase reconstructable FTS shadow segments.
                        fts_exists = conn.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='captures_fts'"
                        ).fetchone()
                        if fts_exists is not None:
                            conn.execute(
                                "INSERT INTO captures_fts(captures_fts, rank) "
                                "VALUES('secure-delete', 1)"
                            )
                    conn.execute(f"DELETE FROM {table}")
            conn.commit()
            # Rebuild every FTS table from its current live rows. This removes
            # segment terms left by deletes performed by pre-security releases,
            # before the persistent FTS5 secure-delete option was enabled.
            for fts_table in ("entries", "captures_fts"):
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (fts_table,)
                ).fetchone()
                if exists is not None:
                    conn.execute(
                        f"INSERT INTO {fts_table}({fts_table}, rank) VALUES('secure-delete', 1)"
                    )
                    conn.execute(f"INSERT INTO {fts_table}({fts_table}) VALUES('rebuild')")
            conn.commit()
            conn.execute("VACUUM")
            quick = conn.execute("PRAGMA quick_check").fetchone()
            if quick is None or quick[0] != "ok":
                raise sqlite3.DatabaseError(f"quick_check failed: {quick!r}")
            conn.close()
            conn = None
            if remove_after:
                _remove_sqlite_copy(snapshot)
            else:
                # A quarantined SQLite WAL is renamed to ``<main>.wal`` and is
                # no longer replayed automatically.  It can still contain raw
                # pages, so every journal spelling is deleted after the main
                # database has been scrubbed and compacted.
                _remove_sqlite_sidecars(snapshot)
                paths.ensure_private_file(snapshot)
        except (OSError, sqlite3.Error, RuntimeError) as exc:
            if conn is not None:
                conn.close()
            _log.warning("snapshot scrub failed for %s; removing snapshot: %s", snapshot.name, exc)
            try:
                _remove_sqlite_copy(snapshot)
            except RuntimeError as removal_exc:
                # A stuck copy must not shield the remaining copies from
                # erasure: record it, finish the sweep, and fail loud at the end.
                leftovers.append(f"{snapshot.name}: {removal_exc}")
                continue
        scrubbed += 1
    if leftovers:
        raise RuntimeError("snapshot erasure incomplete: " + "; ".join(sorted(leftovers)))
    return scrubbed


def scrub_snapshots(tables: Iterable[str]) -> int:
    """Remove selected personal-data tables from every retained/recovery snapshot."""
    requested = _validated_scrub_tables(tables)
    backup_dir = paths.backup_dir()
    if not requested or not backup_dir.is_dir():
        return 0

    stable = sorted(backup_dir.glob("evo-*.db"))
    temporary = sorted(backup_dir.glob("evo-*.db.tmp"))
    scrubbed = scrub_database_copies(requested, stable)
    # A .tmp snapshot was never promoted/verified and has no recovery contract.
    # Scrub then remove it so a SIGKILL remnant cannot outlive explicit erasure.
    scrubbed += scrub_database_copies(requested, temporary, remove_after=True)

    # Clean orphaned journals/sidecars whose main file no longer exists. They
    # may contain pages from an interrupted snapshot even though they are not a
    # valid standalone database.
    for artifact in backup_dir.iterdir():
        if artifact.name.endswith(("-wal", "-shm", "-journal", ".wal", ".shm", ".journal")):
            artifact.unlink(missing_ok=True)
    return scrubbed


def apply_retention(
    *,
    keep_daily: int = 7,
    keep_weekly: int = 4,
    now: datetime | None = None,
) -> list[Path]:
    """Delete snapshots outside the retention policy; return what was removed.

    Keep a snapshot iff its date is within the last ``keep_daily`` days, OR it
    is a Monday within the last ``keep_weekly`` weeks. Unparseable names and
    future-dated files (clock skew) are kept defensively."""
    backup = paths.backup_dir()
    if not backup.is_dir():
        return []
    today = _local_today(now)
    removed: list[Path] = []
    for p in sorted(backup.iterdir()):
        m = _SNAPSHOT_RE.match(p.name)
        if not m:
            continue
        try:
            d = datetime.strptime(m.group(1), "%Y%m%d").date()
        except ValueError:
            continue
        age = (today - d).days
        if age < 0:  # future-dated — keep, defensive
            continue
        keep = age < keep_daily or (d.weekday() == 0 and age < keep_weekly * 7)
        if not keep:
            try:
                _remove_sqlite_copy(p)
                removed.append(p)
            except (OSError, RuntimeError) as e:
                _log.warning("retention: failed to remove %s: %s", p.name, e)
    if removed:
        _log.info("retention: removed %d expired snapshot(s)", len(removed))
    return removed


def run_daily_backup(
    cfg: Config, *, db_path: Path | None = None, now: datetime | None = None
) -> Path | None:
    """The daily-tick entry point: snapshot (verified) + retention. Never raises."""
    dest = create_snapshot(db_path=db_path, now=now)
    try:
        apply_retention(
            keep_daily=cfg.evomem.snapshot_keep_daily,
            keep_weekly=cfg.evomem.snapshot_keep_weekly,
            now=now,
        )
    except Exception as e:  # noqa: BLE001 — retention must not kill the tick
        _log.warning("retention pass failed: %s", e)
    return dest

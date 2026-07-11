"""Startup integrity check + auto-quarantine of corrupt local data (#202).

If the SQLite index or the TOML config is corrupt, the daemon would otherwise
fail to start (or crash later) with no actionable message. This module runs a
cheap check at startup, and when it finds damage it:

  1. renames the bad file to ``<name>.corrupt.<timestamp>`` (kept for analysis),
  2. lets the normal code path rebuild a fresh file from defaults
     (config: rewritten here; DB: recreated lazily by ``fts.connect``),
  3. records an inspectable recovery marker (``.integrity-recovery.json``)
     that an embedding client or operator can surface and acknowledge,
  4. logs the check result + every quarantine as a JSON line.

It is intentionally conservative: a *missing* file is NOT corruption (it is a
clean first run), and an *empty* DB / freshly-written config must never be
flagged. Only a positive signal of damage — a failed ``PRAGMA integrity_check``
or a ``tomllib`` parse error — triggers quarantine.
"""

from __future__ import annotations

import json
import sqlite3
import time
import tomllib
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from . import config as config_mod
from . import paths
from .logger import get

_log = get("persome.daemon")

# Cap the integrity_check work so a huge DB can't blow the <500ms startup
# budget. The first N problems are enough to decide "corrupt".
_INTEGRITY_CHECK_LIMIT = 100
_DERIVED_FTS_INTEGRITY_ERRORS = {
    "malformed inverted index for FTS5 table main.entries": "entries",
    "malformed inverted index for FTS5 table main.captures_fts": "captures_fts",
}
_DERIVED_FTS_TABLES = frozenset(_DERIVED_FTS_INTEGRITY_ERRORS.values())
_GENERIC_MALFORMED_DB_ERROR = "database disk image is malformed"


@dataclass(frozen=True)
class QuarantinedFile:
    """One file the check moved aside, recorded in the recovery marker."""

    kind: str  # "database" | "config"
    original_path: str
    quarantine_path: str
    reason: str


def _timestamp_suffix() -> str:
    # Filesystem-safe, sorts chronologically, unique enough for startup.
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _quarantine(path: Path) -> Path:
    """Rename ``path`` (and any SQLite sidecars) to ``<name>.corrupt.<ts>``.

    Returns the destination of the main file. Sidecars (``-wal`` / ``-shm`` / ``-journal``)
    are moved to matching names so a half-written WAL can't resurrect the
    corrupt DB on the next open. Sidecars move first and every rename is
    mandatory; otherwise the live main remains discoverable and recovery fails
    closed instead of starting beside an unsafely retained journal.
    """
    suffix = _timestamp_suffix()
    dest = path.with_name(f"{path.name}.corrupt.{suffix}")
    # Avoid clobbering a marker from a previous recovery in the same second.
    n = 1
    while dest.exists():
        dest = path.with_name(f"{path.name}.corrupt.{suffix}.{n}")
        n += 1
    moved: list[tuple[Path, Path]] = []
    for sidecar in (f"{path.name}-wal", f"{path.name}-shm", f"{path.name}-journal"):
        src = path.with_name(sidecar)
        try:
            src.lstat()
        except FileNotFoundError:
            continue
        else:
            sidecar_dest = dest.with_name(f"{dest.name}.{sidecar.rsplit('-', 1)[1]}")
            try:
                src.rename(sidecar_dest)
            except OSError as exc:
                for original, quarantined in reversed(moved):
                    quarantined.rename(original)
                raise RuntimeError(f"cannot safely quarantine SQLite sidecar {src}") from exc
            moved.append((src, sidecar_dest))
    try:
        path.rename(dest)
    except OSError as exc:
        for original, quarantined in reversed(moved):
            quarantined.rename(original)
        raise RuntimeError(f"cannot safely quarantine corrupt file {path}") from exc
    return dest


def _db_corruption_reason(db_path: Path) -> str | None:
    """Return a reason string if the DB is corrupt, else ``None``.

    A missing file is not corruption. ``PRAGMA integrity_check`` returns a
    single ``"ok"`` row for a healthy DB (including a brand-new empty one);
    anything else — or a ``DatabaseError`` on open — means damage.
    """
    if not db_path.exists():
        return None
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        rows = conn.execute(f"PRAGMA integrity_check({_INTEGRITY_CHECK_LIMIT})").fetchall()
        results = [str(r[0]) for r in rows]
        if results == ["ok"]:
            return None
        # Trim so a flood of problems doesn't bloat the log line / marker.
        joined = "; ".join(results[:5])
        return f"integrity_check: {joined}"
    except sqlite3.DatabaseError as e:
        return f"open failed: {e}"
    finally:
        if conn is not None:
            conn.close()


def _derived_fts_damage(results: list[str]) -> set[str] | None:
    """Return damaged derived FTS tables, or ``None`` for core DB damage."""
    if not results:
        return None
    damaged: set[str] = set()
    for result in results:
        table = _DERIVED_FTS_INTEGRITY_ERRORS.get(result.strip())
        if table is None:
            return None
        damaged.add(table)
    return damaged


def _derived_fts_vtable_failure(error: sqlite3.DatabaseError) -> set[str]:
    """Return derived FTS tables SQLite could not construct."""
    message = str(error)
    return {
        table for table in _DERIVED_FTS_TABLES if f"vtable constructor failed: {table}" in message
    }


def _canonical_tables_are_readable(conn: sqlite3.Connection) -> bool:
    """Check every non-FTS table before repairing a generic SQLite error."""
    from .store import fts

    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    for (name,) in rows:
        if name == "sqlite_sequence" or name in fts.DERIVED_FTS_SCHEMA_OBJECTS:
            continue
        quoted = '"' + str(name).replace('"', '""') + '"'
        conn.execute(f"SELECT count(*) FROM {quoted}").fetchone()  # noqa: S608
    return True


def _is_generic_derived_fts_failure(conn: sqlite3.Connection, error: sqlite3.DatabaseError) -> bool:
    """Recognize the old SQLite error emitted when both FTS projections fail.

    SQLite sometimes collapses multiple damaged FTS virtual tables into the
    generic ``database disk image is malformed`` error. We accept that as a
    derived-index repair candidate only after every regular table remains
    readable; final ``integrity_check`` validation still decides success.
    """
    if _GENERIC_MALFORMED_DB_ERROR not in str(error).lower():
        return False
    try:
        return _canonical_tables_are_readable(conn)
    except sqlite3.DatabaseError:
        return False


def _rebuild_captures_fts_via_schema_reset(db_path: Path) -> None:
    """Recover a derived capture FTS that an older SQLite cannot drop normally."""
    from .store import fts

    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        fts.reset_corrupt_captures_fts_schema(conn)
        conn.commit()
        # Schema reset makes the old FTS shadow pages unreachable. VACUUM is
        # required before integrity_check will accept the rebuilt database.
        conn.execute("VACUUM")
    finally:
        conn.close()

    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        fts.rebuild_captures_fts(conn)
        conn.commit()
    finally:
        conn.close()


def _rebuild_derived_fts_via_schema_reset(db_path: Path) -> None:
    """Recreate both derived FTS projections from their canonical sources."""
    from .store import fts

    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        fts.reset_corrupt_derived_fts_schema(conn)
        conn.commit()
        # The old shadow pages are unreachable after the narrow schema reset.
        # Vacuum before recreating either virtual table so integrity_check reads
        # only canonical tables plus freshly built FTS segments.
        conn.execute("VACUUM")
    finally:
        conn.close()

    # fts.connect recreates the capture index and rebuilds entries from
    # Markdown/evo_nodes when their FTS table is absent.
    with fts.cursor(db_path):
        pass


def _try_rebuild_derived_fts(db_path: Path) -> bool | None:
    """Repair malformed derived FTS indexes without touching user data.

    ``captures_fts`` is derived from ``captures`` and ``entries`` from the
    Markdown/evo_nodes projection. ``None`` means the data is intact but the
    rebuild must be retried (for example, another local reader owns the
    database); ``False`` means the failure was not limited to these indexes.
    """
    conn: sqlite3.Connection | None = None
    damaged_fts: set[str] | None = None
    requires_full_schema_reset = False
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        try:
            results = [
                str(row[0])
                for row in conn.execute(
                    f"PRAGMA integrity_check({_INTEGRITY_CHECK_LIMIT})"
                ).fetchall()
            ]
        except sqlite3.DatabaseError as exc:
            damaged_fts = _derived_fts_vtable_failure(exc)
            if damaged_fts:
                requires_full_schema_reset = "entries" in damaged_fts
            elif _is_generic_derived_fts_failure(conn, exc):
                # Both FTS tables can collapse to this generic error on older
                # SQLite. The canonical-table preflight above bounds the reset
                # to derived data; a fresh integrity_check validates it later.
                damaged_fts = set(_DERIVED_FTS_TABLES)
                requires_full_schema_reset = True
            else:
                raise
        else:
            damaged_fts = _derived_fts_damage(results)
        if not damaged_fts:
            return False

        if requires_full_schema_reset or "entries" in damaged_fts:
            conn.close()
            conn = None
            _rebuild_derived_fts_via_schema_reset(db_path)
        elif "captures_fts" in damaged_fts:
            # A normal DROP/CREATE can still leave older macOS SQLite builds
            # unable to construct the replacement virtual table on the next
            # connection. The narrow schema reset plus VACUUM removes those
            # unreachable shadow pages before recreating the derived index.
            conn.close()
            conn = None
            _rebuild_captures_fts_via_schema_reset(db_path)
        else:
            return False
        # Older macOS SQLite builds can retain the malformed virtual-table
        # constructor in this connection after DROP/CREATE. Validate from a
        # fresh connection so that cache cannot turn a successful rebuild into
        # a rollback.
        if conn is not None:
            conn.close()
        conn = sqlite3.connect(db_path, timeout=5.0)
        repaired = [
            str(row[0])
            for row in conn.execute(f"PRAGMA integrity_check({_INTEGRITY_CHECK_LIMIT})").fetchall()
        ]
        if repaired != ["ok"]:
            return None if _derived_fts_damage(repaired) else False
        return True
    except (RuntimeError, sqlite3.DatabaseError) as exc:
        if conn is not None and conn.in_transaction:
            conn.rollback()
        _log.warning(
            "integrity: derived FTS repair failed",
            extra={"path": str(db_path), "error": str(exc)},
        )
        return None if damaged_fts else False
    finally:
        if conn is not None:
            conn.close()


def _config_corruption_reason(config_path: Path) -> str | None:
    """Return a reason string if the config is unparseable, else ``None``.

    A missing config is not corruption (``write_default_if_missing`` creates
    it). Only a TOML parse error counts.
    """
    if not config_path.exists():
        return None
    try:
        with open(config_path, "rb") as f:
            tomllib.load(f)
        return None
    except tomllib.TOMLDecodeError as e:
        return f"TOML parse error: {e}"
    except OSError as e:
        # Unreadable file (permissions, etc.) — treat as damaged so we don't
        # wedge startup, but say so explicitly in the reason.
        return f"unreadable: {e}"


def _write_recovery_marker(quarantined: list[QuarantinedFile]) -> None:
    payload = {
        "recovered_at": datetime.now().astimezone().isoformat(),
        "files": [asdict(q) for q in quarantined],
    }
    marker = paths.integrity_recovery_marker()
    try:
        paths.atomic_write_private_text(
            marker,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )
    except (OSError, RuntimeError) as e:
        _log.warning("integrity: failed to write recovery marker", extra={"error": str(e)})


def check_and_recover() -> list[QuarantinedFile]:
    """Run the startup integrity check, quarantining any corrupt file.

    Must be called after ``paths.ensure_dirs()`` and *before* the config is
    loaded or the DB is opened, so a corrupt file is moved aside before any
    code path trips over it. Returns the list of quarantined files (empty when
    everything was healthy). Never raises — a check that itself fails degrades
    to "assume healthy" and logs the failure.
    """
    started = time.perf_counter()
    quarantined: list[QuarantinedFile] = []

    db_path = paths.index_db()
    config_path = paths.config_file()
    rebuilt_derived_fts = False
    deferred_derived_fts_repair = False

    try:
        db_reason = _db_corruption_reason(db_path)
    except Exception as e:  # defensive: never let the check abort startup
        db_reason = None
        _log.warning("integrity: DB check errored, assuming healthy", extra={"error": str(e)})
    if db_reason is not None:
        derived_fts_repair = _try_rebuild_derived_fts(db_path)
        if derived_fts_repair is True:
            rebuilt_derived_fts = True
            _log.warning(
                "integrity: rebuilt derived FTS indexes",
                extra={"path": str(db_path), "reason": db_reason},
            )
        elif derived_fts_repair is None:
            deferred_derived_fts_repair = True
            _log.warning(
                "integrity: deferred derived FTS repair",
                extra={"path": str(db_path), "reason": db_reason},
            )
        else:
            dest = _quarantine(db_path)
            quarantined.append(
                QuarantinedFile(
                    kind="database",
                    original_path=str(db_path),
                    quarantine_path=str(dest),
                    reason=db_reason,
                )
            )
            _log.warning(
                "integrity: quarantined corrupt database",
                extra={"path": str(db_path), "moved_to": str(dest), "reason": db_reason},
            )
            # The DB is recreated lazily on the next fts.connect(); nothing to do.

    try:
        config_reason = _config_corruption_reason(config_path)
    except Exception as e:  # defensive
        config_reason = None
        _log.warning("integrity: config check errored, assuming healthy", extra={"error": str(e)})
    if config_reason is not None:
        dest = _quarantine(config_path)
        quarantined.append(
            QuarantinedFile(
                kind="config",
                original_path=str(config_path),
                quarantine_path=str(dest),
                reason=config_reason,
            )
        )
        # Rebuild a fresh default config so the daemon starts with sane values.
        config_mod.write_default_if_missing(config_path)
        _log.warning(
            "integrity: quarantined corrupt config, rebuilt default",
            extra={"path": str(config_path), "moved_to": str(dest), "reason": config_reason},
        )

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if quarantined:
        _write_recovery_marker(quarantined)
    _log.info(
        "integrity check complete",
        extra={
            "elapsed_ms": round(elapsed_ms, 1),
            "recovered": len(quarantined),
            "derived_repaired": int(rebuilt_derived_fts),
            "derived_repair_deferred": int(deferred_derived_fts_repair),
            "status": (
                "recovered"
                if quarantined
                else "repaired_derived"
                if rebuilt_derived_fts
                else "degraded_derived"
                if deferred_derived_fts_repair
                else "ok"
            ),
        },
    )
    return quarantined

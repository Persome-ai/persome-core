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
_CAPTURES_FTS_INTEGRITY_ERROR = "malformed inverted index for FTS5 table main.captures_fts"


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


def _is_captures_fts_only_damage(results: list[str]) -> bool:
    """Return whether every integrity failure belongs to the derived capture FTS."""
    return bool(results) and all(
        result.strip() == _CAPTURES_FTS_INTEGRITY_ERROR for result in results
    )


def _try_rebuild_captures_fts(db_path: Path) -> bool | None:
    """Repair a malformed derived capture FTS index without touching user data.

    The raw ``captures`` table is authoritative; ``captures_fts`` is an
    external-content FTS5 index that can be recreated from it. ``None`` means
    the data is intact but the rebuild must be retried (for example, another
    local reader owns the database); ``False`` means the failure was not
    limited to this derived index.
    """
    conn: sqlite3.Connection | None = None
    captures_fts_only = False
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        results = [
            str(row[0])
            for row in conn.execute(f"PRAGMA integrity_check({_INTEGRITY_CHECK_LIMIT})").fetchall()
        ]
        captures_fts_only = _is_captures_fts_only_damage(results)
        if not captures_fts_only:
            return False

        from .store import fts

        conn.execute("BEGIN IMMEDIATE")
        fts.rebuild_captures_fts(conn)
        repaired = [
            str(row[0])
            for row in conn.execute(f"PRAGMA integrity_check({_INTEGRITY_CHECK_LIMIT})").fetchall()
        ]
        if repaired != ["ok"]:
            conn.rollback()
            return False
        conn.commit()
        return True
    except (RuntimeError, sqlite3.DatabaseError) as exc:
        if conn is not None and conn.in_transaction:
            conn.rollback()
        _log.warning(
            "integrity: derived captures FTS repair failed",
            extra={"path": str(db_path), "error": str(exc)},
        )
        return None if captures_fts_only else False
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
    rebuilt_captures_fts = False
    deferred_captures_fts_repair = False

    try:
        db_reason = _db_corruption_reason(db_path)
    except Exception as e:  # defensive: never let the check abort startup
        db_reason = None
        _log.warning("integrity: DB check errored, assuming healthy", extra={"error": str(e)})
    if db_reason is not None:
        capture_fts_repair = _try_rebuild_captures_fts(db_path)
        if capture_fts_repair is True:
            rebuilt_captures_fts = True
            _log.warning(
                "integrity: rebuilt derived captures FTS index",
                extra={"path": str(db_path), "reason": db_reason},
            )
        elif capture_fts_repair is None:
            deferred_captures_fts_repair = True
            _log.warning(
                "integrity: deferred derived captures FTS repair",
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
            "derived_repaired": int(rebuilt_captures_fts),
            "derived_repair_deferred": int(deferred_captures_fts_repair),
            "status": (
                "recovered"
                if quarantined
                else "repaired_derived"
                if rebuilt_captures_fts
                else "degraded_derived"
                if deferred_captures_fts_repair
                else "ok"
            ),
        },
    )
    return quarantined

"""Startup integrity check + auto-quarantine of corrupt local data (#202).

If the SQLite index or the TOML config is corrupt, the daemon would otherwise
fail to start (or crash later) with no actionable message. This module runs a
cheap check at startup, and when it finds damage it:

  1. renames the bad file to ``<name>.corrupt.<timestamp>`` (kept for analysis),
  2. lets the normal code path rebuild a fresh file from defaults
     (config: rewritten here; DB: recreated lazily by ``fts.connect``),
  3. records a one-time recovery marker (``.integrity-recovery.json``) that
     Mens.app surfaces to the user, then deletes,
  4. logs the check result + every quarantine as a JSON line.

It is intentionally conservative: a *missing* file is NOT corruption (it is a
clean first run), and an *empty* DB / freshly-written config must never be
flagged. Only a positive signal of damage — a failed ``PRAGMA integrity_check``
or a ``tomllib`` parse error — triggers quarantine.
"""

from __future__ import annotations

import contextlib
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

    Returns the destination of the main file. Sidecars (``-wal`` / ``-shm``)
    are moved to matching names so a half-written WAL can't resurrect the
    corrupt DB on the next open.
    """
    suffix = _timestamp_suffix()
    dest = path.with_name(f"{path.name}.corrupt.{suffix}")
    # Avoid clobbering a marker from a previous recovery in the same second.
    n = 1
    while dest.exists():
        dest = path.with_name(f"{path.name}.corrupt.{suffix}.{n}")
        n += 1
    path.rename(dest)
    for sidecar in (f"{path.name}-wal", f"{path.name}-shm"):
        src = path.with_name(sidecar)
        if src.exists():
            # Best-effort: a stuck sidecar shouldn't abort recovery; the fresh
            # DB opens with its own new WAL anyway.
            with contextlib.suppress(OSError):
                src.rename(dest.with_name(f"{dest.name}.{sidecar.rsplit('-', 1)[1]}"))
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
        marker.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    except OSError as e:
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

    try:
        db_reason = _db_corruption_reason(db_path)
    except Exception as e:  # defensive: never let the check abort startup
        db_reason = None
        _log.warning("integrity: DB check errored, assuming healthy", extra={"error": str(e)})
    if db_reason is not None:
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
            "status": "recovered" if quarantined else "ok",
        },
    )
    return quarantined

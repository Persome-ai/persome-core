"""Startup integrity check + auto-quarantine of corrupt local data (#202).

If the SQLite index or the TOML config is corrupt, the daemon would otherwise
fail to start (or crash later) with no actionable message. This module runs a
cheap check at startup, and when it finds damage it:

  1. renames the bad file to ``<name>.corrupt.<timestamp>`` (kept for analysis),
  2. rebuilds config from defaults and restores the DB from the latest verified
     snapshot when possible, then reconciles the Markdown memory projection,
  3. records an inspectable recovery marker (``.integrity-recovery.json``)
     that an embedding client or operator can surface and acknowledge,
  4. logs the check result + every quarantine as a JSON line.

It is intentionally conservative: a *missing* file is NOT corruption (it is a
clean first run), and an *empty* DB / freshly-written config must never be
flagged. Only a positive signal of damage — a failed ``PRAGMA integrity_check``
or a ``tomllib`` parse error — triggers quarantine.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import tempfile
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
# SQLite >= 3.50 reports FTS5 damage per finding instead of one summary line,
# e.g. 'fts5: corruption found reading blob 10 from table "captures_fts"'.
_DERIVED_FTS_INTEGRITY_RE = re.compile(
    r'^fts5: .* table "(' + "|".join(sorted(_DERIVED_FTS_TABLES)) + r')"$'
)
_GENERIC_MALFORMED_DB_ERROR = "database disk image is malformed"
_SNAPSHOT_NAME_RE = re.compile(r"^evo-\d{8}\.db$")
_PERSOME_SNAPSHOT_COLUMNS = {
    "files": frozenset(
        {
            "path",
            "prefix",
            "description",
            "tags",
            "status",
            "entry_count",
            "created",
            "updated",
            "needs_compact",
        }
    ),
    "entries": frozenset({"id", "path", "prefix", "timestamp", "tags", "content", "superseded"}),
    "captures": frozenset(
        {
            "id",
            "timestamp",
            "app_name",
            "bundle_id",
            "window_title",
            "focused_role",
            "focused_value",
            "visible_text",
            "url",
        }
    ),
}
_EVOMEM_SNAPSHOT_COLUMNS = frozenset(
    {
        "node_id",
        "user_id",
        "agent_id",
        "content",
        "layer",
        "supersedes",
        "superseded_by",
        "is_latest",
        "status",
        "file_name",
    }
)
_DATABASE_RECOVERY_PHASES = frozenset(
    {
        "prepared",
        "manifest_invalidated",
        "quarantined",
        "restoring_snapshot",
        "snapshot_restored",
        "replaying_without_snapshot",
        "replaying_preserved_database",
        "replaying_live_database_from_markdown",
        "replayed",
        "failed",
        "owner_repaired",
    }
)
_CONFIG_RECOVERY_PHASES = frozenset(
    {
        "prepared",
        "quarantined",
        "replacement_ready",
        "authority_unresolved",
        "authority_resolved",
    }
)


@dataclass(frozen=True)
class QuarantinedFile:
    """One file the check moved aside, recorded in the recovery marker."""

    kind: str  # "database" | "config"
    original_path: str
    quarantine_path: str
    reason: str


class _WriteAuthorityUnresolved(RuntimeError):
    """Recovery found multiple intact sources but no unique canonical one."""


def _timestamp_suffix() -> str:
    # Filesystem-safe, sorts chronologically, unique enough for startup.
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _quarantine_destination(path: Path) -> Path:
    """Choose a collision-free quarantine path without mutating the source."""
    suffix = _timestamp_suffix()
    dest = path.with_name(f"{path.name}.corrupt.{suffix}")
    n = 1
    while dest.exists():
        dest = path.with_name(f"{path.name}.corrupt.{suffix}.{n}")
        n += 1
    return dest


def _quarantine_destination_has_artifacts(destination: Path) -> bool:
    """Return whether a prepared quarantine has moved any SQLite artifact.

    ``_quarantine`` moves WAL/SHM/journal sidecars before the main database. A
    crash in that window therefore leaves ``destination`` absent while (for
    example) ``destination.wal`` already exists. Such a journal has begun its
    quarantine and must be resumed; treating the now-WAL-less live main as an
    owner repair could silently accept stale data and discard the journal.
    """
    candidates = (
        destination,
        destination.with_name(f"{destination.name}.wal"),
        destination.with_name(f"{destination.name}.shm"),
        destination.with_name(f"{destination.name}.journal"),
    )
    for candidate in candidates:
        try:
            candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            # An unreadable destination is not proof that quarantine never
            # started. Fail toward the resumable recovery path.
            return True
        return True
    return False


def _quarantine(path: Path, *, destination: Path | None = None) -> Path:
    """Rename ``path`` (and any SQLite sidecars) to ``<name>.corrupt.<ts>``.

    Returns the destination of the main file. Sidecars (``-wal`` / ``-shm`` / ``-journal``)
    are moved to matching names so a half-written WAL can't resurrect the
    corrupt DB on the next open. Sidecars move first and every rename is
    mandatory; otherwise the live main remains discoverable and recovery fails
    closed instead of starting beside an unsafely retained journal.
    """
    dest = destination or _quarantine_destination(path)
    if dest.exists():
        raise RuntimeError(f"quarantine destination already exists: {dest}")
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
        stripped = result.strip()
        table = _DERIVED_FTS_INTEGRITY_ERRORS.get(stripped)
        if table is None:
            match = _DERIVED_FTS_INTEGRITY_RE.match(stripped)
            if match is None:
                return None
            table = match.group(1)
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


def _write_recovery_marker(
    quarantined: list[QuarantinedFile],
    *,
    database_recovery: dict[str, object] | None = None,
) -> bool:
    marker = paths.integrity_recovery_marker()
    if database_recovery is None:
        try:
            previous = json.loads(marker.read_text(encoding="utf-8"))
            previous_database = (
                previous.get("database_recovery") if isinstance(previous, dict) else None
            )
            manifest = paths.model_build_manifest()
            manifest_superseded = (
                manifest.exists() and manifest.stat().st_mtime_ns > marker.stat().st_mtime_ns
            )
            if (
                isinstance(previous_database, dict)
                and previous_database.get("model_rebuild_required")
                and not manifest_superseded
            ):
                database_recovery = previous_database
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
            pass
    payload = {
        "recovered_at": datetime.now().astimezone().isoformat(),
        "files": [asdict(q) for q in quarantined],
    }
    if database_recovery is not None:
        payload["database_recovery"] = database_recovery
    try:
        paths.atomic_write_private_text(
            marker,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )
        return True
    except (OSError, RuntimeError) as e:
        _log.warning("integrity: failed to write recovery marker", extra={"error": str(e)})
        return False


def _write_pending_recovery(payload: dict[str, object]) -> None:
    paths.atomic_write_private_text(
        paths.integrity_recovery_pending(),
        json.dumps(payload, ensure_ascii=False, indent=2),
    )


def _retain_invalid_pending_journal(path: Path, *, label: str, error: str) -> Path | None:
    """Keep an invalid journal for forensics without blocking normal recovery."""
    try:
        destination = _quarantine(path)
    except (OSError, RuntimeError) as exc:
        _log.warning(
            f"integrity: could not retain invalid {label} journal",
            extra={"path": str(path), "error": str(exc), "journal_error": error},
        )
        return None
    _log.warning(
        f"integrity: retained invalid {label} journal and resumed normal checks",
        extra={"path": str(path), "moved_to": str(destination), "error": error},
    )
    return destination


def _pending_journal_semantic_error(
    payload: object,
    *,
    original: Path,
    phases: frozenset[str],
) -> str | None:
    """Validate the fields that make a version-1 resume journal safe to act on."""
    if not isinstance(payload, dict):
        return "journal payload is not an object"
    version = payload.get("version")
    if isinstance(version, bool) or version != 1:
        return "unsupported journal version"
    phase = payload.get("phase")
    if not isinstance(phase, str) or phase not in phases:
        return f"unknown or missing recovery phase: {phase!r}"
    started_at = payload.get("started_at")
    if not isinstance(started_at, str) or not started_at.strip():
        return "missing recovery start timestamp"
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return "missing recovery reason"
    original_value = payload.get("original_path")
    quarantine_value = payload.get("quarantine_path")
    if not isinstance(original_value, str) or Path(original_value) != original:
        return "journal original path is not the canonical owner path"
    if not isinstance(quarantine_value, str):
        return "missing quarantine path"
    quarantine = Path(quarantine_value)
    if (
        not quarantine.is_absolute()
        or quarantine.parent != original.parent
        or not quarantine.name.startswith(f"{original.name}.corrupt.")
    ):
        return "journal quarantine path is outside the canonical recovery namespace"
    return None


def _load_pending_recovery() -> tuple[dict[str, object] | None, Path | None]:
    try:
        payload = json.loads(paths.integrity_recovery_pending().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, None
    except (json.JSONDecodeError, OSError) as exc:
        _log.warning("integrity: unreadable pending recovery journal", extra={"error": str(exc)})
        retained = _retain_invalid_pending_journal(
            paths.integrity_recovery_pending(),
            label="database recovery",
            error=str(exc),
        )
        if retained is not None:
            return None, retained
        return {"version": 0, "phase": "invalid", "error": str(exc)}, None
    semantic_error = _pending_journal_semantic_error(
        payload,
        original=paths.index_db(),
        phases=_DATABASE_RECOVERY_PHASES,
    )
    if semantic_error is not None:
        _log.warning(
            "integrity: invalid pending recovery journal",
            extra={"error": semantic_error},
        )
        retained = _retain_invalid_pending_journal(
            paths.integrity_recovery_pending(),
            label="database recovery",
            error=semantic_error,
        )
        if retained is not None:
            return None, retained
        return {"version": 0, "phase": "invalid", "error": semantic_error}, None
    return payload, None


def _set_pending_phase(payload: dict[str, object], phase: str) -> None:
    payload["phase"] = phase
    payload["updated_at"] = datetime.now().astimezone().isoformat()
    _write_pending_recovery(payload)


def _remove_pending_recovery() -> None:
    try:
        paths.integrity_recovery_pending().unlink(missing_ok=True)
    except OSError as exc:
        _log.warning(
            "integrity: failed to remove completed recovery journal", extra={"error": str(exc)}
        )


def _write_pending_config_recovery(payload: dict[str, object]) -> None:
    """Persist config-recovery intent before replacing the corrupt source."""
    paths.atomic_write_private_text(
        paths.integrity_config_recovery_pending(),
        json.dumps(payload, ensure_ascii=False, indent=2),
    )


def _load_pending_config_recovery() -> tuple[dict[str, object] | None, Path | None]:
    try:
        payload = json.loads(paths.integrity_config_recovery_pending().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, None
    except (json.JSONDecodeError, OSError) as exc:
        _log.warning(
            "integrity: unreadable pending config recovery intent",
            extra={"error": str(exc)},
        )
        retained = _retain_invalid_pending_journal(
            paths.integrity_config_recovery_pending(),
            label="config recovery",
            error=str(exc),
        )
        if retained is not None:
            return None, retained
        return {"version": 0, "phase": "invalid", "error": str(exc)}, None
    semantic_error = _pending_journal_semantic_error(
        payload,
        original=paths.config_file(),
        phases=_CONFIG_RECOVERY_PHASES,
    )
    if semantic_error is not None:
        _log.warning(
            "integrity: invalid pending config recovery intent",
            extra={"error": semantic_error},
        )
        retained = _retain_invalid_pending_journal(
            paths.integrity_config_recovery_pending(),
            label="config recovery",
            error=semantic_error,
        )
        if retained is not None:
            return None, retained
        return {"version": 0, "phase": "invalid", "error": semantic_error}, None
    return payload, None


def _set_pending_config_phase(payload: dict[str, object], phase: str) -> None:
    payload["phase"] = phase
    payload["updated_at"] = datetime.now().astimezone().isoformat()
    _write_pending_config_recovery(payload)


def _remove_pending_config_recovery() -> None:
    try:
        paths.integrity_config_recovery_pending().unlink(missing_ok=True)
    except OSError as exc:
        _log.warning(
            "integrity: failed to remove completed config recovery intent",
            extra={"error": str(exc)},
        )


def _resume_config_recovery(
    config_path: Path,
    payload: dict[str, object],
) -> QuarantinedFile | None:
    """Finish one prepared config quarantine without trusting its replacement."""
    original = Path(str(payload["original_path"]))
    dest = Path(str(payload["quarantine_path"]))
    reason = str(payload["reason"])
    prior_phase = str(payload.get("phase") or "prepared")
    authority_was_unresolved = prior_phase == "authority_unresolved"
    authority_was_resolved = prior_phase == "authority_resolved"
    if (
        original != config_path
        or dest.parent != config_path.parent
        or not dest.name.startswith(f"{config_path.name}.corrupt.")
    ):
        raise ValueError("pending config recovery contains unsafe paths")

    current_reason = _config_corruption_reason(config_path)
    if not dest.exists() and current_reason is not None:
        _quarantine(config_path, destination=dest)
        _set_pending_config_phase(payload, "quarantined")
    elif not dest.exists() and config_path.exists():
        # The owner repaired the config after intent was prepared but before
        # quarantine. Keep the authority-unknown signal for this startup, but
        # do not move a now-valid file aside.
        payload["owner_repaired_before_quarantine"] = True
        _set_pending_config_phase(payload, "replacement_ready")
    elif dest.exists() and str(payload.get("phase")) == "prepared":
        _set_pending_config_phase(payload, "quarantined")

    if not config_path.exists():
        config_mod.write_default_if_missing(config_path)
    elif _config_corruption_reason(config_path) is not None:
        # A second corrupt replacement while the first intent is pending is a
        # new incident. Preserve it at a fresh destination rather than
        # overwriting the original forensic copy.
        replacement_dest = _quarantine(config_path)
        additional = payload.get("additional_quarantine_paths")
        if not isinstance(additional, list):
            additional = []
        additional.append(str(replacement_dest))
        payload["additional_quarantine_paths"] = additional
        config_mod.write_default_if_missing(config_path)
    _set_pending_config_phase(
        payload,
        (
            "authority_resolved"
            if authority_was_resolved
            else "authority_unresolved"
            if authority_was_unresolved
            else "replacement_ready"
        ),
    )

    if not dest.exists():
        return None
    return QuarantinedFile(
        kind="config",
        original_path=str(original),
        quarantine_path=str(dest),
        reason=reason,
    )


def _database_has_default_evomem_nodes(db_path: Path) -> bool:
    if not db_path.exists():
        return False
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(
            f"{db_path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=5.0,
        )
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='evo_nodes'"
        ).fetchone()
        if table is None:
            return False
        return (
            conn.execute(
                "SELECT 1 FROM evo_nodes WHERE user_id='default' AND agent_id='default' LIMIT 1"
            ).fetchone()
            is not None
        )
    except sqlite3.Error:
        return False
    finally:
        if conn is not None:
            conn.close()


def _persist_recovered_write_authority(config_path: Path, authority: str) -> None:
    """Make an unknown-authority recovery choice durable before unfreezing."""
    if authority not in {"markdown", "evomem", "unknown"}:
        raise ValueError(f"unsupported recovered write authority: {authority}")
    import tomlkit
    from tomlkit.items import Table

    config_mod.write_default_if_missing(config_path)
    document = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    evomem = document.get("evomem")
    if not isinstance(evomem, Table):
        evomem = tomlkit.table()
        document["evomem"] = evomem
    evomem["write_authority"] = authority
    paths.atomic_write_private_text(config_path, tomlkit.dumps(document))


def _choose_recovered_write_authority(
    db_path: Path,
    database_recovery: dict[str, object] | None,
) -> str:
    if database_recovery is not None:
        canonical = str(database_recovery.get("canonical_source") or "")
        source = str(database_recovery.get("source") or "")
        if canonical == "markdown_projection" or source in {"markdown", "empty"}:
            return "markdown"
        if canonical == "verified_snapshot" or source.startswith("verified_snapshot"):
            return "evomem"
    return _infer_live_write_authority(db_path)


def _infer_live_write_authority(db_path: Path) -> str:
    """Infer an unknown healthy-DB authority only when one projection matches.

    A Markdown-authoritative runtime can contain lagging shadow evo_nodes, while
    an evomem-authoritative runtime can contain lagging projected Markdown.
    Presence alone cannot distinguish them. The live retrieval projection is
    the last successfully reconciled view, so compare it with both candidates
    and fail closed if neither (or an ambiguous divergent state) matches.
    """
    from .store import entries as entries_mod
    from .store import files as files_mod

    if not db_path.exists():
        return "markdown"
    conn = sqlite3.connect(
        f"{db_path.resolve().as_uri()}?mode=ro",
        uri=True,
        timeout=5.0,
    )
    try:
        entries_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entries'"
        ).fetchone()
        live = (
            {
                str(row[0]): (str(row[1]), str(row[2]), int(row[3]))
                for row in conn.execute(
                    "SELECT id, path, content, superseded FROM entries "
                    "WHERE prefix != 'event' AND instr(path, '/') = 0"
                ).fetchall()
            }
            if entries_table is not None
            else {}
        )
        evo: dict[str, tuple[str, str, int]] = {}
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='evo_nodes'"
        ).fetchone()
        if table is not None:
            for row in conn.execute(
                "SELECT node_id, file_name, content, is_latest, status, supersedes "
                "FROM evo_nodes WHERE user_id='default' AND agent_id='default'"
            ).fetchall():
                try:
                    supersedes = set(json.loads(str(row[5] or "[]")))
                except (TypeError, ValueError, json.JSONDecodeError):
                    supersedes = set()
                evo[str(row[0])] = (
                    str(row[1] or ""),
                    entries_mod._fts_content_without_provenance(  # noqa: SLF001
                        str(row[2] or ""),
                        supersedes=supersedes,
                    ),
                    0 if bool(row[3]) and str(row[4]) == "active" else 1,
                )
    finally:
        conn.close()

    parsed: list[tuple[str, object]] = []
    source_state: dict[str, tuple[str, str | None, int, str | None, str | None]] = {}
    for path in files_mod.list_memory_files(strict=True):
        name = files_mod.memory_name(path)
        if "/" in name or files_mod.validate_prefix(name) == "event":
            continue
        for entry in files_mod.read_file(path).entries:
            if entry.id in source_state:
                raise ValueError(
                    f"duplicate Markdown entry id while inferring authority: {entry.id}"
                )
            source_state[entry.id] = entries_mod._temporal_source_state(entry)  # noqa: SLF001
            parsed.append((name, entry))
    supersedes_by_id = entries_mod._supersedes_by_id(source_state)  # noqa: SLF001
    markdown = {
        entry.id: (
            name,
            entries_mod._fts_content_from_markdown_entry(  # noqa: SLF001
                entry,
                superseded=entries_mod._superseded_from_tags(entry),  # noqa: SLF001
                supersedes=supersedes_by_id.get(entry.id, set()),
            ),
            entries_mod._superseded_from_tags(entry),  # noqa: SLF001
        )
        for name, entry in parsed
    }

    markdown_matches = live == markdown
    evomem_matches = live == evo
    if not live and not markdown and not evo:
        return "markdown"
    if not evo and markdown_matches:
        return "markdown"
    if not markdown and evomem_matches:
        return "evomem"
    if markdown_matches and evomem_matches:
        raise _WriteAuthorityUnresolved(
            "could not infer write authority safely: Markdown and evomem both match "
            "the last live projection"
        )
    raise RuntimeError(
        "could not infer write authority safely: Markdown and evomem projections diverge"
    )


def _explicit_pending_write_authority(
    config_path: Path,
    pending_config: dict[str, object] | None,
) -> str | None:
    """Return an authority only when the pending journal proves owner intent.

    A replacement default is not a choice. Once recovery has asked the owner to
    choose (or has durably recorded that choice), however, the current valid
    config is the input needed to resume a previously non-destructive replay.
    """
    if pending_config is None:
        return None
    phase = str(pending_config.get("phase") or "")
    if not (
        pending_config.get("owner_repaired_before_quarantine")
        or phase in {"authority_unresolved", "authority_resolved"}
    ):
        return None
    authority = config_mod.load(config_path).evomem.write_authority.strip().lower()
    return authority if authority in {"markdown", "evomem"} else None


def _has_reconcilable_memory_state(db_path: Path) -> bool:
    """Return whether an authority choice can change durable memory state."""
    from .store import files as files_mod

    if _database_has_default_evomem_nodes(db_path):
        return True
    for path in files_mod.list_memory_files(strict=True):
        name = files_mod.memory_name(path)
        if "/" in name or files_mod.validate_prefix(name) == "event":
            continue
        if files_mod.read_file(path).entries:
            return True
    return False


def _copy_verified_snapshot(snapshot: Path, db_path: Path) -> None:
    """Atomically copy one verified SQLite snapshot into the live DB path."""
    paths.ensure_private_file(snapshot)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{db_path.name}.restore-",
        suffix=".tmp",
        dir=db_path.parent,
    )
    os.close(fd)
    temp = Path(temp_name)
    source: sqlite3.Connection | None = None
    target: sqlite3.Connection | None = None
    try:
        source = sqlite3.connect(f"{snapshot.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
        target = sqlite3.connect(temp, timeout=5.0)
        source.backup(target)
        rows = target.execute(f"PRAGMA integrity_check({_INTEGRITY_CHECK_LIMIT})").fetchall()
        if [str(row[0]) for row in rows] != ["ok"]:
            raise sqlite3.DatabaseError("restored snapshot failed integrity_check")
        target.close()
        target = None
        source.close()
        source = None
        paths.ensure_private_file(temp)
        temp.replace(db_path)
        paths.ensure_private_file(db_path)
    finally:
        if target is not None:
            target.close()
        if source is not None:
            source.close()
        temp.unlink(missing_ok=True)


def _restore_latest_verified_snapshot(db_path: Path) -> tuple[Path, bool] | None:
    """Restore the newest structurally valid daily snapshot, if one exists."""
    from .evomem import integrity as evomem_integrity

    backup_dir = paths.backup_dir()
    if not backup_dir.is_dir():
        return None
    candidates = sorted(
        (path for path in backup_dir.glob("evo-*.db") if _SNAPSHOT_NAME_RE.fullmatch(path.name)),
        reverse=True,
    )
    for snapshot in candidates:
        try:
            paths.ensure_private_file(snapshot)
            conn = sqlite3.connect(
                f"{snapshot.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=5.0,
            )
            try:
                valid_schema = all(
                    {
                        str(row[1])
                        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
                    }.issuperset(required)
                    for table, required in _PERSOME_SNAPSHOT_COLUMNS.items()
                )
                entries_sql_row = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE name='entries' AND type='table'"
                ).fetchone()
                entries_sql = str(entries_sql_row[0] or "") if entries_sql_row else ""
                valid_schema = valid_schema and "VIRTUAL TABLE" in entries_sql.upper()
                valid_schema = valid_schema and "USING FTS5" in entries_sql.upper()
                evo_columns = {
                    str(row[1]) for row in conn.execute("PRAGMA table_info(evo_nodes)").fetchall()
                }
                has_valid_evomem = evo_columns.issuperset(_EVOMEM_SNAPSHOT_COLUMNS)
            finally:
                conn.close()
            if not valid_schema:
                _log.warning(
                    "integrity: skipped foreign or incomplete recovery snapshot",
                    extra={"path": str(snapshot)},
                )
                continue
            violations = evomem_integrity.verify_snapshot(snapshot)
            if any(violation.structural for violation in violations):
                _log.warning(
                    "integrity: skipped invalid recovery snapshot",
                    extra={"path": str(snapshot)},
                )
                continue
            _copy_verified_snapshot(snapshot, db_path)
            if not has_valid_evomem:
                # Pre-evomem snapshots remain valuable for captures/timeline,
                # but their absent/incomplete node table cannot be treated as
                # canonical. Remove it from the live copy so NodeStore can
                # safely recreate it from current Markdown.
                live = sqlite3.connect(db_path)
                try:
                    live.execute("DROP TABLE IF EXISTS evo_nodes")
                    live.commit()
                finally:
                    live.close()
            _log.warning(
                "integrity: restored verified database snapshot",
                extra={"path": str(snapshot), "evomem_canonical": has_valid_evomem},
            )
            return snapshot, has_valid_evomem
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            _log.warning(
                "integrity: recovery snapshot restore failed",
                extra={"path": str(snapshot), "error": str(exc)},
            )
    return None


def _repopulate_after_quarantine(
    *,
    snapshot_restored: bool,
    authority_unknown: bool = False,
    write_authority_override: str | None = None,
    strict_authority_resolution: bool = False,
) -> dict[str, object]:
    """Rebuild current memory projections without LLM calls or a new snapshot.

    The corrupt DB itself has already been retained as the rollback/forensic
    artifact. Taking a normal pre-restore snapshot here could overwrite the
    same day's last known-good snapshot with a newly-created empty database.
    """
    from .evomem import inversion as evo_inversion
    from .evomem.store import NodeStore, upsert_node
    from .store import entries as entries_mod
    from .store import files as files_mod
    from .store import fts, index_md, projector

    if write_authority_override not in {None, "markdown", "evomem"}:
        raise ValueError(f"unsupported recovery write authority: {write_authority_override}")
    write_authority = write_authority_override or (
        "unknown" if authority_unknown else evo_inversion.authority()
    )
    # When both config and DB were damaged, a verified snapshot is the only
    # trustworthy canonical signal; the rewritten default config must not make
    # a potentially stale Markdown projection win by accident. Without a
    # snapshot, Markdown remains the only best-effort recovery source.
    restore_nodes_from_markdown = not snapshot_restored or write_authority == "markdown"
    parsed_files: list[tuple[str, list[files_mod.ParsedEntry]]] = []
    skipped_event_files = 0
    direct_markdown_files = 0
    skipped_invalid_files = 0
    direct_entry_sources: dict[str, str] = {}
    memory_files = files_mod.list_memory_files(strict=True)
    for path in memory_files:
        name = files_mod.memory_name(path)
        try:
            prefix = files_mod.validate_prefix(name)
        except ValueError as exc:
            skipped_invalid_files += 1
            _log.warning("integrity: skipping memory projection %s: %s", name, exc)
            continue
        if prefix == "event" or "/" in name:
            direct = files_mod.read_file(path)
            for entry in direct.entries:
                previous = direct_entry_sources.get(entry.id)
                if previous is not None and previous != name:
                    raise ValueError(
                        f"duplicate direct Markdown entry id {entry.id!r} "
                        f"in {previous!r} and {name!r}"
                    )
                direct_entry_sources[entry.id] = name
            if prefix == "event":
                skipped_event_files += 1
            else:
                direct_markdown_files += 1
            continue
        if restore_nodes_from_markdown:
            parsed_files.append((name, files_mod.read_file(path).entries))

    nodes = projector.rebuild_nodes_from_projection(parsed_files)
    if strict_authority_resolution and skipped_invalid_files:
        raise ValueError(
            "cannot resolve write authority while unsupported Markdown memory files are present"
        )
    current_node_keys = {(node.node_id, node.user_id, node.agent_id) for node in nodes}
    if len(current_node_keys) != len(nodes):
        seen: set[tuple[str, str, str]] = set()
        duplicates: set[tuple[str, str, str]] = set()
        for node in nodes:
            key = (node.node_id, node.user_id, node.agent_id)
            if key in seen:
                duplicates.add(key)
            seen.add(key)
        sample = ", ".join(key[0] for key in sorted(duplicates)[:5])
        raise ValueError(f"duplicate Markdown memory node id(s): {sample}")
    stale_node_keys: list[tuple[str, str, str]] = []
    direct_nodes_removed = 0
    derived_geometry_rows_invalidated = 0
    NodeStore()  # create/migrate the canonical node table before one recovery transaction
    with fts.cursor() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # event-* and nested skills/* are direct-Markdown namespaces under
            # every authority. Delete only nodes whose stored filename proves
            # they belong to that direct namespace. A basename-only row is
            # ambiguous with a legitimate evomem top-level file, so fail closed
            # instead of silently choosing one source and deleting the other.
            for entry_id, source_name in sorted(direct_entry_sources.items()):
                row = conn.execute(
                    "SELECT file_name FROM evo_nodes "
                    "WHERE node_id=? AND user_id='default' AND agent_id='default'",
                    (entry_id,),
                ).fetchone()
                if row is None:
                    continue
                node_file_name = str(row[0] or "")
                if node_file_name != source_name:
                    raise ValueError(
                        f"direct Markdown entry id {entry_id!r} from {source_name!r} "
                        f"collides with canonical evomem file {node_file_name!r}"
                    )
                direct_nodes_removed += conn.execute(
                    "DELETE FROM evo_nodes "
                    "WHERE node_id=? AND user_id='default' AND agent_id='default'",
                    (entry_id,),
                ).rowcount
            direct_nodes_removed += conn.execute(
                "DELETE FROM evo_nodes WHERE user_id='default' AND agent_id='default' "
                "AND (file_name LIKE 'event-%' OR instr(file_name, '/') > 0)"
            ).rowcount
            # A verified snapshot can be older than the Markdown projection.
            # Under Markdown authority, remove only snapshot nodes that were
            # previously exposed through the non-event retrieval projection
            # and are now absent from the complete, successfully parsed source.
            # DB-native/unprojected and event nodes remain untouched.
            if snapshot_restored and write_authority == "markdown" and skipped_invalid_files == 0:
                projected = (
                    conn.execute(
                        "SELECT node_id, user_id, agent_id FROM evo_nodes "
                        "WHERE user_id='default' AND agent_id='default'"
                    ).fetchall()
                    if strict_authority_resolution
                    else conn.execute(
                        "SELECT n.node_id, n.user_id, n.agent_id "
                        "FROM evo_nodes n JOIN entries e ON e.id = n.node_id "
                        "WHERE n.user_id='default' AND n.agent_id='default' "
                        "AND e.prefix != 'event'"
                    ).fetchall()
                )
                stale_node_keys = [
                    (str(row[0]), str(row[1]), str(row[2]))
                    for row in projected
                    if (str(row[0]), str(row[1]), str(row[2])) not in current_node_keys
                ]
                conn.executemany(
                    "DELETE FROM evo_nodes WHERE node_id=? AND user_id=? AND agent_id=?",
                    stale_node_keys,
                )
            # Faces, Volumes, Root, and their probe queue are rebuildable output,
            # not recovery authority. Always discard snapshot copies so malformed
            # or stale geometry cannot break the viewer before the required build.
            # Relation edges are retained for a known evomem snapshot, but must
            # be cleared when current Markdown/direct sources removed or changed
            # Points that those relations may expose.
            tables_to_invalidate = ["schema_faces", "cross_domain_probe_state"]
            if (
                (snapshot_restored and restore_nodes_from_markdown)
                or stale_node_keys
                or direct_nodes_removed
            ):
                tables_to_invalidate.append("relation_edges")
            for table in tables_to_invalidate:
                if conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone():
                    derived_geometry_rows_invalidated += conn.execute(
                        f"DELETE FROM {table}"
                    ).rowcount
            for node in nodes:
                upsert_node(conn, node, user_id=node.user_id, agent_id=node.agent_id)
            # Recovery has either restored canonical snapshot nodes or just
            # reconstructed them from the complete Markdown projection. Use
            # that unified graph to rebuild retrieval, including nested skill
            # paths; direct event Markdown remains the intentional fallback.
            projection_files, projection_entries = entries_mod.rebuild_index(
                conn,
                source_authority="evomem",
                allow_incomplete_recovery=True,
            )
            if restore_nodes_from_markdown:
                for name, parsed_entries in parsed_files:
                    parsed = files_mod.read_file(files_mod.memory_path(name))
                    fts.upsert_file(
                        conn,
                        fts.FileRow(
                            path=name,
                            prefix=files_mod.validate_prefix(name),
                            description=parsed.description,
                            tags=" ".join(parsed.tags),
                            status=parsed.status,
                            entry_count=len(parsed_entries),
                            created=parsed.created,
                            updated=parsed.updated,
                            needs_compact=1 if parsed.needs_compact else 0,
                        ),
                    )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        try:
            index_md.rebuild(conn)
            index_rebuilt = True
            index_error = None
        except (OSError, RuntimeError, ValueError) as exc:
            index_rebuilt = False
            index_error = str(exc)
            _log.warning(
                "integrity: memory index.md rebuild failed after database recovery",
                extra={"error": str(exc)},
            )

    return {
        "write_authority": write_authority,
        "canonical_source": (
            "markdown_projection" if restore_nodes_from_markdown else "verified_snapshot"
        ),
        "markdown_files": len(parsed_files),
        "skipped_event_files": skipped_event_files,
        "direct_markdown_files": direct_markdown_files,
        "skipped_invalid_files": skipped_invalid_files,
        "nodes": len(nodes),
        "projection_files": projection_files,
        "projection_entries": projection_entries,
        "stale_projected_nodes_removed": len(stale_node_keys),
        "legacy_direct_nodes_removed": direct_nodes_removed,
        "derived_geometry_rows_invalidated": derived_geometry_rows_invalidated,
        "index_md_rebuilt": index_rebuilt,
        "index_md_error": index_error,
    }


def _reconcile_resolved_write_authority(authority: str) -> dict[str, object]:
    """Apply one explicit authority choice before clearing its guard journal.

    The shared recovery transaction rebuilds ``entries`` from the chosen source,
    reconciles Point state when Markdown wins, and invalidates derived geometry.
    When evomem wins, also overwrite its non-exempt Markdown projections and
    verify that the surviving sources now agree. Projection writes are
    idempotent; any partial filesystem failure keeps the journal for retry.
    """
    from .evomem import inversion as evo_inversion
    from .store import fts

    if authority not in {"markdown", "evomem"}:
        raise ValueError(f"unsupported resolved write authority: {authority}")
    result = _repopulate_after_quarantine(
        snapshot_restored=True,
        authority_unknown=False,
        write_authority_override=authority,
        strict_authority_resolution=True,
    )
    if authority != "evomem":
        return result

    with fts.cursor() as conn:
        evo_inversion.project_live_all(conn)
        canonical_nodes = int(
            conn.execute(
                "SELECT count(*) FROM evo_nodes WHERE user_id='default' AND agent_id='default'"
            ).fetchone()[0]
        )
    if canonical_nodes:
        try:
            inferred = _infer_live_write_authority(paths.index_db())
        except _WriteAuthorityUnresolved as exc:
            if "both match the last live projection" not in str(exc):
                raise
        else:
            raise RuntimeError(
                "evomem authority reconciliation did not align Markdown with canonical nodes "
                f"(remaining source: {inferred})"
            )
    return result


def _invalidate_model_manifest() -> bool:
    """Remove derived model views that belong to the quarantined database."""
    manifest = paths.model_build_manifest()
    manifest_invalidated = True
    try:
        manifest.unlink(missing_ok=True)
    except OSError as exc:
        manifest_invalidated = False
        _log.warning(
            "integrity: failed to invalidate stale model build manifest",
            extra={"path": str(manifest), "error": str(exc)},
        )
    try:
        from .model.human import HumanMarkdownConflict, remove_managed_human_markdown

        remove_managed_human_markdown()
    except HumanMarkdownConflict as exc:
        _log.warning("integrity: preserving user-owned HUMAN.md", extra={"error": str(exc)})
    except (OSError, RuntimeError) as exc:
        _log.warning(
            "integrity: failed to remove stale HUMAN.md projection",
            extra={"path": str(paths.human_file()), "error": str(exc)},
        )
    return manifest_invalidated


def _capture_buffer_replay_available() -> bool:
    try:
        return any(paths.capture_buffer_dir().glob("*.json"))
    except OSError:
        return False


def _snapshot_modified_at(snapshot: Path | None) -> str | None:
    if snapshot is None:
        return None
    try:
        return datetime.fromtimestamp(snapshot.stat().st_mtime).astimezone().isoformat()
    except OSError:
        return None


def _recover_database_projection(
    *,
    authority_unknown: bool,
    manifest_was_present: bool,
    manifest_invalidated: bool,
    preserve_existing: bool = False,
    existing_canonical_baseline: bool | None = None,
    write_authority_override: str | None = None,
    pending: dict[str, object] | None = None,
) -> dict[str, object]:
    """Restore/replay a database after its damaged predecessor is quarantined."""
    db_path = paths.index_db()
    snapshot: Path | None = None
    snapshot_has_evomem = False
    try:
        if preserve_existing:
            if pending is not None:
                _set_pending_phase(
                    pending,
                    (
                        "replaying_preserved_database"
                        if existing_canonical_baseline is not False
                        else "replaying_live_database_from_markdown"
                    ),
                )
        else:
            if pending is not None:
                _set_pending_phase(pending, "restoring_snapshot")
            restored = _restore_latest_verified_snapshot(db_path)
            if restored is not None:
                snapshot, snapshot_has_evomem = restored
            if pending is not None:
                if snapshot is not None:
                    pending["snapshot_path"] = str(snapshot)
                    pending["snapshot_has_evomem"] = snapshot_has_evomem
                    _set_pending_phase(pending, "snapshot_restored")
                else:
                    _set_pending_phase(pending, "replaying_without_snapshot")
        has_database_baseline = preserve_existing or snapshot is not None
        canonical_baseline = (snapshot is not None and snapshot_has_evomem) or (
            preserve_existing
            and (
                preserve_existing
                if existing_canonical_baseline is None
                else existing_canonical_baseline
            )
        )
        recovery_authority = write_authority_override
        if authority_unknown and canonical_baseline and recovery_authority is None:
            # A complete evo_nodes table proves that an evomem source exists,
            # not that it was authoritative. It may be a lagging shadow from a
            # Markdown-authoritative runtime. Compare the restored snapshot's
            # last retrieval projection with *both* surviving sources before
            # any deletes/upserts. Only a single-source match is conclusive.
            try:
                recovery_authority = _infer_live_write_authority(db_path)
            except (RuntimeError, ValueError) as exc:
                raise _WriteAuthorityUnresolved(
                    "verified snapshot and current Markdown preserve multiple possible "
                    f"write authorities; owner choice required ({exc})"
                ) from exc
        projection = _repopulate_after_quarantine(
            snapshot_restored=canonical_baseline,
            authority_unknown=authority_unknown,
            write_authority_override=recovery_authority,
        )
        source = (
            (
                "existing_recovered_database"
                if canonical_baseline
                else "existing_live_database+markdown"
            )
            if preserve_existing
            else "verified_snapshot+markdown"
            if snapshot is not None
            and projection["canonical_source"] == "markdown_projection"
            and int(projection["markdown_files"]) > 0
            else "verified_snapshot"
            if snapshot is not None
            else "markdown"
            if int(projection["projection_files"]) > 0
            else "empty"
        )
        capture_replay_available = _capture_buffer_replay_available()
        recovery: dict[str, object] = {
            "status": "restored",
            "source": source,
            "snapshot_path": str(snapshot) if snapshot is not None else None,
            "snapshot_modified_at": _snapshot_modified_at(snapshot),
            "snapshot_has_evomem": snapshot_has_evomem,
            "preserved_existing_database": preserve_existing,
            **projection,
            # A daily snapshot can predate the corruption and Markdown is an
            # approximate projection under evomem authority. Never claim an
            # exact/lossless restore from either source.
            "lossy": True,
            "recovery_completeness": "best_effort",
            "potentially_lost_since_snapshot": has_database_baseline,
            "not_recovered_without_snapshot": (
                []
                if has_database_baseline
                else [
                    (
                        "captures_pending_replay"
                        if capture_replay_available
                        else "captures_unavailable"
                    ),
                    "timeline_blocks",
                    "sessions",
                    "relation_edges",
                    "structural_geometry",
                ]
            ),
            "capture_buffer_replay_available": capture_replay_available,
            "capture_recovery": (
                "pending_replay"
                if capture_replay_available
                else "snapshot_only"
                if has_database_baseline
                else "unavailable"
            ),
            "model_rebuild_required": True,
            "stale_manifest_was_present": manifest_was_present,
            "manifest_invalidated": manifest_invalidated,
            "reconciled_write_authority": (
                "markdown" if projection.get("canonical_source") == "markdown_projection" else None
            ),
        }
        _log.warning(
            "integrity: rebuilt database projections after quarantine",
            extra={
                "source": source,
                "nodes": projection["nodes"],
                "entries": projection["projection_entries"],
            },
        )
        return recovery
    except Exception as exc:  # noqa: BLE001 - retain startup availability
        # A verified snapshot remains useful even if a newer Markdown projection
        # cannot be parsed. Without one, create a healthy empty DB so startup
        # remains available and report the loss.
        if not db_path.exists():
            try:
                from .store import fts

                with fts.cursor(db_path):
                    pass
            except Exception:  # noqa: BLE001 - original error is primary
                pass
        _log.warning(
            "integrity: database projection recovery failed",
            extra={"error": str(exc), "snapshot_restored": snapshot is not None},
        )
        has_database_baseline = preserve_existing or snapshot is not None
        authority_resolution_required = isinstance(exc, _WriteAuthorityUnresolved)
        return {
            "status": "partial" if has_database_baseline else "failed",
            "source": (
                (
                    "existing_recovered_database"
                    if existing_canonical_baseline is not False
                    else "existing_live_database+markdown"
                )
                if preserve_existing
                else "verified_snapshot"
                if snapshot is not None
                else "none"
            ),
            "snapshot_path": str(snapshot) if snapshot is not None else None,
            "snapshot_modified_at": _snapshot_modified_at(snapshot),
            "snapshot_has_evomem": snapshot_has_evomem,
            "error": str(exc),
            "authority_resolution_required": authority_resolution_required,
            "preserved_authority_sources": (
                ["snapshot_entries", "snapshot_evo_nodes", "current_markdown"]
                if authority_resolution_required
                else []
            ),
            "lossy": True,
            "recovery_completeness": "best_effort",
            "potentially_lost_since_snapshot": has_database_baseline,
            "preserved_existing_database": preserve_existing,
            "capture_buffer_replay_available": _capture_buffer_replay_available(),
            "model_rebuild_required": True,
            "stale_manifest_was_present": manifest_was_present,
            "manifest_invalidated": manifest_invalidated,
        }


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
    config_was_missing = not config_path.exists()
    missing_config_needs_guard = config_was_missing and (
        db_path.exists() or paths.integrity_recovery_pending().exists()
    )
    pending_config, invalid_config_journal = _load_pending_config_recovery()
    config_authority_unknown = (
        missing_config_needs_guard
        or pending_config is not None
        or invalid_config_journal is not None
    )
    if invalid_config_journal is not None:
        quarantined.append(
            QuarantinedFile(
                kind="config_recovery_journal",
                original_path=str(paths.integrity_config_recovery_pending()),
                quarantine_path=str(invalid_config_journal),
                reason="invalid recovery journal",
            )
        )
    config_recovery_resolved = False
    authority_persisted = not config_authority_unknown
    database_safe_to_finalize = False
    rebuilt_derived_fts = False
    deferred_derived_fts_repair = False
    database_recovery: dict[str, object] | None = None

    # Config must be usable before a database replay chooses Markdown or
    # evomem authority. Persist intent before replacing a corrupt config: a
    # crash after the rename/default write but before the DB journal must not
    # make that fresh default look like known Markdown authority next time.
    try:
        config_reason = _config_corruption_reason(config_path)
    except Exception as e:  # defensive
        config_reason = None
        _log.warning("integrity: config check errored, assuming healthy", extra={"error": str(e)})
    if pending_config is not None:
        try:
            resumed_config = _resume_config_recovery(config_path, pending_config)
            config_recovery_resolved = True
            if resumed_config is not None:
                quarantined.append(resumed_config)
            config_reason = str(pending_config.get("reason") or config_reason or "unknown")
            _log.warning(
                "integrity: resumed incomplete config recovery",
                extra={"quarantine_path": pending_config.get("quarantine_path")},
            )
        except Exception as exc:  # noqa: BLE001 - retain intent and fail authority closed
            _log.warning(
                "integrity: pending config recovery failed",
                extra={"error": str(exc)},
            )
    elif config_reason is not None or missing_config_needs_guard:
        config_authority_unknown = True
        dest = _quarantine_destination(config_path)
        recovery_reason = config_reason or "config missing while database authority was unknown"
        pending_config = {
            "version": 1,
            "phase": "prepared",
            "started_at": datetime.now().astimezone().isoformat(),
            "original_path": str(config_path),
            "quarantine_path": str(dest),
            "reason": recovery_reason,
        }
        try:
            _write_pending_config_recovery(pending_config)
            recovered_config = _resume_config_recovery(config_path, pending_config)
            config_recovery_resolved = True
            if recovered_config is not None:
                quarantined.append(recovered_config)
            _log.warning(
                "integrity: recovered missing or corrupt config to a guarded default",
                extra={
                    "path": str(config_path),
                    "moved_to": str(dest),
                    "reason": recovery_reason,
                },
            )
        except Exception as exc:  # noqa: BLE001 - never mutate without durable intent
            _log.warning(
                "integrity: config quarantine/recovery interrupted",
                extra={"error": str(exc)},
            )

    explicit_pending_authority = _explicit_pending_write_authority(config_path, pending_config)
    pending, invalid_database_journal = _load_pending_recovery()
    invalid_journal_manifest_was_present = False
    invalid_journal_manifest_invalidated = False
    if invalid_database_journal is not None:
        quarantined.append(
            QuarantinedFile(
                kind="database_recovery_journal",
                original_path=str(paths.integrity_recovery_pending()),
                quarantine_path=str(invalid_database_journal),
                reason="invalid recovery journal",
            )
        )
        invalid_journal_manifest_was_present = paths.model_build_manifest().exists()
        invalid_journal_manifest_invalidated = _invalidate_model_manifest()
    if pending is not None:
        try:
            original = Path(str(pending["original_path"]))
            dest = Path(str(pending["quarantine_path"]))
            reason = str(pending["reason"])
            if (
                original != db_path
                or dest.parent != db_path.parent
                or not dest.name.startswith(f"{db_path.name}.corrupt.")
            ):
                raise ValueError("pending recovery journal contains unsafe database paths")
            resume_phase = str(pending.get("phase") or "prepared")
            manifest_was_present = bool(pending.get("manifest_was_present"))
            manifest_invalidated = _invalidate_model_manifest()
            live_reason_before_quarantine = (
                _db_corruption_reason(original) if original.exists() else None
            )
            owner_repaired = (
                not _quarantine_destination_has_artifacts(dest)
                and original.exists()
                and live_reason_before_quarantine is None
            )
            if owner_repaired:
                pending["owner_repaired_before_quarantine"] = True
                database_recovery = {
                    "status": "restored",
                    "source": "owner_repaired_database",
                    "preserved_existing_database": True,
                    "lossy": True,
                    "recovery_completeness": "owner_managed",
                    "model_rebuild_required": True,
                    "stale_manifest_was_present": manifest_was_present,
                    "manifest_invalidated": manifest_invalidated,
                }
                pending["database_recovery"] = database_recovery
                _set_pending_phase(pending, "owner_repaired")
                _log.warning(
                    "integrity: preserved owner-repaired healthy database",
                    extra={"path": str(original)},
                )
            else:
                if resume_phase == "prepared":
                    _set_pending_phase(pending, "manifest_invalidated")
                if not dest.exists() and original.exists():
                    _quarantine(original, destination=dest)
                if resume_phase in {"prepared", "manifest_invalidated"}:
                    _set_pending_phase(pending, "quarantined")
                if dest.exists():
                    quarantined.append(
                        QuarantinedFile(
                            kind="database",
                            original_path=str(original),
                            quarantine_path=str(dest),
                            reason=reason,
                        )
                    )

                saved_recovery = pending.get("database_recovery")
                live_database_healthy = db_path.exists() and _db_corruption_reason(db_path) is None
                saved_restored = (
                    isinstance(saved_recovery, dict) and saved_recovery.get("status") == "restored"
                )
                if saved_restored and live_database_healthy:
                    database_recovery = saved_recovery
                else:
                    saved_source = (
                        str(saved_recovery.get("source"))
                        if isinstance(saved_recovery, dict)
                        else ""
                    )
                    phase_has_trusted_snapshot = (
                        resume_phase == "snapshot_restored"
                        and pending.get("snapshot_has_evomem") is not False
                    )
                    saved_has_trusted_snapshot = saved_source == "verified_snapshot" and (
                        not isinstance(saved_recovery, dict)
                        or saved_recovery.get("snapshot_has_evomem") is not False
                    )
                    trusted_existing_baseline = live_database_healthy and (
                        phase_has_trusted_snapshot
                        or resume_phase == "replaying_preserved_database"
                        or saved_source == "existing_recovered_database"
                        or saved_has_trusted_snapshot
                    )
                    if db_path.exists() and not live_database_healthy:
                        retry_dest = _quarantine_destination(db_path)
                        _quarantine(db_path, destination=retry_dest)
                        additional = pending.get("additional_quarantine_paths")
                        if not isinstance(additional, list):
                            additional = []
                        additional.append(str(retry_dest))
                        pending["additional_quarantine_paths"] = additional
                        _write_pending_recovery(pending)
                        quarantined.append(
                            QuarantinedFile(
                                kind="database_retry",
                                original_path=str(db_path),
                                quarantine_path=str(retry_dest),
                                reason="untrusted live database left by interrupted recovery",
                            )
                        )
                    database_recovery = _recover_database_projection(
                        authority_unknown=(
                            bool(pending.get("authority_unknown")) or config_authority_unknown
                        ),
                        manifest_was_present=manifest_was_present,
                        manifest_invalidated=manifest_invalidated,
                        preserve_existing=live_database_healthy,
                        existing_canonical_baseline=trusted_existing_baseline,
                        write_authority_override=explicit_pending_authority,
                        pending=pending,
                    )
                    pending["database_recovery"] = database_recovery
                    _set_pending_phase(
                        pending,
                        "replayed" if database_recovery.get("status") == "restored" else "failed",
                    )
            _log.warning(
                "integrity: resumed incomplete database recovery",
                extra={"quarantine_path": str(dest)},
            )
            database_safe_to_finalize = database_recovery.get("status") == "restored"
        except Exception as exc:  # noqa: BLE001 - journal must keep startup fail-open
            _log.warning("integrity: pending database recovery failed", extra={"error": str(exc)})
            database_recovery = {
                "status": "failed",
                "source": "pending_recovery",
                "error": str(exc),
                "lossy": True,
                "model_rebuild_required": True,
                "manifest_invalidated": _invalidate_model_manifest(),
            }
    else:
        database_check_succeeded = False
        try:
            db_reason = _db_corruption_reason(db_path)
            database_check_succeeded = True
        except Exception as e:  # defensive: never let the check abort startup
            db_reason = None
            _log.warning("integrity: DB check errored, assuming healthy", extra={"error": str(e)})
        if db_reason is not None:
            derived_fts_repair = _try_rebuild_derived_fts(db_path)
            if derived_fts_repair is True:
                rebuilt_derived_fts = True
                database_safe_to_finalize = True
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
                dest = _quarantine_destination(db_path)
                manifest_was_present = paths.model_build_manifest().exists()
                pending = {
                    "version": 1,
                    "phase": "prepared",
                    "started_at": datetime.now().astimezone().isoformat(),
                    "original_path": str(db_path),
                    "quarantine_path": str(dest),
                    "reason": db_reason,
                    "authority_unknown": config_authority_unknown,
                    "manifest_was_present": manifest_was_present,
                }
                try:
                    _write_pending_recovery(pending)
                    manifest_invalidated = _invalidate_model_manifest()
                    _set_pending_phase(pending, "manifest_invalidated")
                    _quarantine(db_path, destination=dest)
                    _set_pending_phase(pending, "quarantined")
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
                        extra={
                            "path": str(db_path),
                            "moved_to": str(dest),
                            "reason": db_reason,
                        },
                    )
                    database_recovery = _recover_database_projection(
                        authority_unknown=config_authority_unknown,
                        manifest_was_present=manifest_was_present,
                        manifest_invalidated=manifest_invalidated,
                        write_authority_override=explicit_pending_authority,
                        pending=pending,
                    )
                    pending["database_recovery"] = database_recovery
                    _set_pending_phase(
                        pending,
                        "replayed" if database_recovery.get("status") == "restored" else "failed",
                    )
                except Exception as exc:  # noqa: BLE001 - retain journal for next startup
                    _log.warning(
                        "integrity: database quarantine/recovery interrupted",
                        extra={"error": str(exc)},
                    )
                    database_recovery = {
                        "status": "failed",
                        "source": "pending_recovery",
                        "error": str(exc),
                        "lossy": True,
                        "model_rebuild_required": True,
                        "manifest_invalidated": _invalidate_model_manifest(),
                    }
                database_safe_to_finalize = database_recovery.get("status") == "restored"
        elif database_check_succeeded:
            database_safe_to_finalize = True

    if invalid_database_journal is not None:
        if database_recovery is None and database_safe_to_finalize:
            database_recovery = {
                "status": "restored",
                "source": "verified_live_database_after_invalid_journal",
                "preserved_existing_database": True,
                "lossy": True,
                "recovery_completeness": "best_effort",
                "model_rebuild_required": True,
                "stale_manifest_was_present": invalid_journal_manifest_was_present,
                "manifest_invalidated": invalid_journal_manifest_invalidated,
            }
        elif database_recovery is not None:
            database_recovery["invalid_pending_journal_quarantined"] = str(invalid_database_journal)
            database_recovery["stale_manifest_was_present"] = bool(
                database_recovery.get("stale_manifest_was_present")
                or invalid_journal_manifest_was_present
            )
            database_recovery["manifest_invalidated"] = bool(
                database_recovery.get("manifest_invalidated")
                or invalid_journal_manifest_invalidated
            )

    if database_recovery is not None and database_recovery.get("authority_resolution_required"):
        # The snapshot/live DB and Markdown are both intact, but neither can be
        # selected without inventing authority. Preserve both, freeze writes,
        # and turn the generated default into an explicit owner prompt.
        database_safe_to_finalize = False
        config_authority_unknown = True
        try:
            _persist_recovered_write_authority(config_path, "unknown")
            if pending_config is None:
                pending_config = {
                    "version": 1,
                    "phase": "authority_unresolved",
                    "started_at": datetime.now().astimezone().isoformat(),
                    "original_path": str(config_path),
                    "quarantine_path": str(_quarantine_destination(config_path)),
                    "reason": "multiple intact write-authority sources need an owner choice",
                }
                _write_pending_config_recovery(pending_config)
                config_recovery_resolved = True
            else:
                pending_config["authority_error"] = str(database_recovery.get("error") or "")
                _set_pending_config_phase(pending_config, "authority_unresolved")
        except Exception as exc:  # noqa: BLE001 - both journals remain fail-closed
            _log.warning(
                "integrity: failed to persist snapshot authority prompt",
                extra={"error": str(exc)},
            )

    if config_authority_unknown and database_safe_to_finalize:
        try:
            current_authority = config_mod.load(config_path).evomem.write_authority.strip().lower()
            pending_config_phase = (
                str(pending_config.get("phase") or "") if pending_config is not None else ""
            )
            explicit_recovery_choice = pending_config is not None and (
                pending_config.get("owner_repaired_before_quarantine")
                or pending_config_phase in {"authority_unresolved", "authority_resolved"}
            )
            if explicit_recovery_choice:
                resolved_authority = current_authority
                if resolved_authority not in {"markdown", "evomem"}:
                    raise ValueError(
                        "recovered config still needs an explicit write authority choice"
                    )
            else:
                resolved_authority = _choose_recovered_write_authority(db_path, database_recovery)
            reconciliation_required = explicit_recovery_choice or not (
                database_recovery is not None
                and database_recovery.get("reconciled_write_authority") == resolved_authority
            )
            if reconciliation_required:
                reconciliation_required = _has_reconcilable_memory_state(db_path)
            reconciliation: dict[str, object] | None = None
            if reconciliation_required:
                authority_manifest_was_present = paths.model_build_manifest().exists()
                authority_manifest_invalidated = _invalidate_model_manifest()
                if database_recovery is None:
                    database_recovery = {
                        "status": "partial",
                        "source": "write_authority_reconciliation",
                        "lossy": False,
                        "recovery_completeness": "authority_pending",
                        "model_rebuild_required": True,
                        "stale_manifest_was_present": authority_manifest_was_present,
                        "manifest_invalidated": authority_manifest_invalidated,
                    }
                else:
                    database_recovery["model_rebuild_required"] = True
                    database_recovery["stale_manifest_was_present"] = bool(
                        database_recovery.get("stale_manifest_was_present")
                        or authority_manifest_was_present
                    )
                    database_recovery["manifest_invalidated"] = bool(
                        database_recovery.get("manifest_invalidated")
                        or authority_manifest_invalidated
                    )
                reconciliation = _reconcile_resolved_write_authority(resolved_authority)
                database_recovery["status"] = "restored"
                database_recovery["recovery_completeness"] = "authority_reconciled"
            _persist_recovered_write_authority(config_path, resolved_authority)
            authority_persisted = True
            if pending_config is not None:
                pending_config["resolved_write_authority"] = resolved_authority
                _set_pending_config_phase(pending_config, "authority_resolved")
            if database_recovery is not None:
                database_recovery["resolved_write_authority"] = resolved_authority
                if reconciliation is not None:
                    database_recovery["authority_reconciliation"] = reconciliation
        except Exception as exc:  # noqa: BLE001 - keep guard intent until durable
            database_safe_to_finalize = False
            if database_recovery is None:
                authority_manifest_was_present = paths.model_build_manifest().exists()
                authority_manifest_invalidated = _invalidate_model_manifest()
                database_recovery = {
                    "status": "partial",
                    "source": "write_authority_resolution",
                    "lossy": False,
                    "recovery_completeness": "authority_pending",
                    "model_rebuild_required": True,
                    "stale_manifest_was_present": authority_manifest_was_present,
                    "manifest_invalidated": authority_manifest_invalidated,
                    "authority_resolution_required": True,
                    "authority_error": str(exc),
                }
            else:
                database_recovery["status"] = "partial"
                database_recovery["authority_resolution_required"] = True
                database_recovery["authority_error"] = str(exc)
            with contextlib.suppress(Exception):
                _persist_recovered_write_authority(config_path, "unknown")
                if pending_config is None:
                    pending_config = {
                        "version": 1,
                        "phase": "authority_unresolved",
                        "started_at": datetime.now().astimezone().isoformat(),
                        "original_path": str(config_path),
                        "quarantine_path": str(_quarantine_destination(config_path)),
                        "reason": "write authority could not be inferred safely",
                    }
                    _write_pending_config_recovery(pending_config)
                    config_recovery_resolved = True
                else:
                    pending_config["authority_error"] = str(exc)
                    _set_pending_config_phase(pending_config, "authority_unresolved")
            _log.warning(
                "integrity: failed to persist recovered write authority",
                extra={"error": str(exc)},
            )

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    marker_written = False
    if quarantined or database_recovery is not None:
        marker_written = _write_recovery_marker(
            quarantined,
            database_recovery=database_recovery,
        )
        if (
            marker_written
            and pending is not None
            and database_recovery is not None
            and database_recovery.get("status") == "restored"
        ):
            _remove_pending_recovery()
    if (
        pending_config is not None
        and config_recovery_resolved
        and database_safe_to_finalize
        and authority_persisted
    ):
        config_is_valid = config_path.exists() and _config_corruption_reason(config_path) is None
        config_was_quarantined = any(item.kind == "config" for item in quarantined)
        if config_is_valid and (not config_was_quarantined or marker_written):
            _remove_pending_config_recovery()
    _log.info(
        "integrity check complete",
        extra={
            "elapsed_ms": round(elapsed_ms, 1),
            "recovered": len(quarantined),
            "derived_repaired": int(rebuilt_derived_fts),
            "derived_repair_deferred": int(deferred_derived_fts_repair),
            "database_recovery": (
                database_recovery.get("status") if database_recovery is not None else None
            ),
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

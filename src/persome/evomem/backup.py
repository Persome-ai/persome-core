"Validated SQLite snapshots for canonical evomem state."

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

from .. import paths
from ..config import Config
from ..logger import get
from . import integrity

_log = get("persome.evomem")

_SNAPSHOT_RE = re.compile(r"^evo-(\d{8})\.db$")


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
        paths.backup_dir().mkdir(parents=True, exist_ok=True)
        tmp.unlink(missing_ok=True)
        with fts.cursor(db_path) as conn:
            conn.execute("VACUUM INTO ?", (str(tmp),))
    except (sqlite3.Error, OSError) as e:
        integrity.emit_alert(
            "snapshot_failed",
            f"VACUUM INTO {tmp.name} failed: {e}",
            source="snapshot",
        )
        tmp.unlink(missing_ok=True)
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
        tmp.unlink(missing_ok=True)
        _log.error(
            "snapshot %s FAILED verification (%d violation(s), %d blocking) — discarded, "
            "existing snapshot (if any) preserved",
            dest.name,
            len(violations),
            len(blocking),
        )
        return None

    tmp.replace(dest)  # atomic same-day refresh
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
                p.unlink()
                removed.append(p)
            except OSError as e:
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

"""Single source of truth for on-disk locations under ~/.persome/."""

from __future__ import annotations

import os
from pathlib import Path


def root() -> Path:
    override = (
        os.environ.get("PERSOME_ROOT")
        or os.environ.get("MENS_CONTEXT_ROOT")  # Mens is the legacy name
        or os.environ.get("OPENCHRONICLE_ROOT")  # OpenChronicle is the legacy name
    )
    if override:
        return Path(override).expanduser().resolve()
    new = Path.home() / ".persome"
    legacy = Path.home() / ".mens"  # Mens is the legacy name
    # Migration-friendly default: a machine upgraded from the pre-rename daemon
    # keeps its existing data dir until ~/.persome is created explicitly.
    if legacy.exists() and not new.exists():
        return legacy
    return new


def app_data_root() -> Path:
    """The Swift app's data dir (``~/.persome``), where it writes tasks.json / settings.json /
    meetings.json — the read surface for the Agent-Native Persome app-data MCP tools (Phase 2,
    docs/superpowers/specs/2026-06-25-agent-native-persome-design.md).

    Distinct from :func:`root` (the *chronicle* root): the packaged app points
    ``PERSOME_ROOT`` at ``<app data dir>/chronicle``, so the app's JSON sits in the parent;
    a bare CLI/dev run with no override has ``root()`` already == ``~/.persome``. An explicit
    ``PERSOME_APP_DATA_DIR`` override wins when set."""
    override = (os.environ.get("PERSOME_APP_DATA_DIR") or os.environ.get("MENS_APP_DATA_DIR"))  # Mens is the legacy name
    if override:
        return Path(override).expanduser().resolve()
    r = root()
    return r.parent if r.name == "chronicle" else r


def memory_dir() -> Path:
    return root() / "memory"


def capture_buffer_dir() -> Path:
    return root() / "capture-buffer"


def logs_dir() -> Path:
    return root() / "logs"


def ocr_samples_dir() -> Path:
    """Local-only OCR training samples (geometry + structured result, NEVER screenshots).

    Written when `[capture] ocr_collect_training_data` is on; bounded by a keep-newest
    prune. No upload path exists — these stay on the user's machine.
    """
    return root() / "ocr-samples"


def config_file() -> Path:
    return root() / "config.toml"


def env_file() -> Path:
    """Dotenv-format secret store. Written by Mens.app (single SoT), sourced
    by `persome start` before forking the daemon so business code can
    just `os.environ.get(...)`. CLI users may edit it directly."""
    return root() / "env"


def index_db() -> Path:
    return root() / "index.db"


def pid_file() -> Path:
    return root() / ".pid"


def paused_flag() -> Path:
    return root() / ".paused"


def writer_state() -> Path:
    """Tracks last-commit timestamp and processed capture files."""
    return root() / ".writer-state.json"


def integrity_recovery_marker() -> Path:
    """One-time marker written by the startup integrity check when it had to
    quarantine a corrupt DB / config (#202). Mens.app reads it to show a
    single recovery notice, then deletes it. JSON payload, see
    ``integrity.py``."""
    return root() / ".integrity-recovery.json"


def backup_dir() -> Path:
    """Daily ``VACUUM INTO`` snapshots of ``index.db`` (``evo-YYYYMMDD.db``).

    Part of the evomem SSOT switch survivability base (design §3.2) — see
    ``evomem/backup.py``. Created on demand by the backup module, NOT by
    ``ensure_dirs``, so a config-disabled install leaves no empty dir behind."""
    return root() / "backup"


def skills_dir() -> Path:
    return root() / "skills"


def launchd_stdout_log() -> Path:
    """stdout sink for the launchd-managed daemon. Lives under logs/ so the
    diagnostic bundle (which globs logs/) collects it automatically."""
    return logs_dir() / "launchd.out.log"


def launchd_stderr_log() -> Path:
    """stderr sink for the launchd-managed daemon (see launchd_stdout_log)."""
    return logs_dir() / "launchd.err.log"


def ensure_dirs() -> None:
    for d in (root(), memory_dir(), capture_buffer_dir(), logs_dir()):
        d.mkdir(parents=True, exist_ok=True)

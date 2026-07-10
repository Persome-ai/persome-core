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


def memory_dir() -> Path:
    return root() / "memory"


def capture_buffer_dir() -> Path:
    return root() / "capture-buffer"


def logs_dir() -> Path:
    return root() / "logs"


def exports_dir() -> Path:
    """Generated, user-shareable artifacts such as model snapshots."""
    return root() / "exports"


def model_build_lock() -> Path:
    return root() / "model-build.lock"


def session_model_lock() -> Path:
    """Cross-process lock for terminal session modeling/finalization."""
    return root() / "session-model.lock"


def model_build_manifest() -> Path:
    return root() / "model-build.json"


def config_file() -> Path:
    return root() / "config.toml"


def env_file() -> Path:
    """Owner-only dotenv secret store sourced before the daemon forks."""
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
    quarantine a corrupt DB / config (#202). Operators and embedding clients
    may surface and acknowledge its JSON payload; see ``integrity.py``."""
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

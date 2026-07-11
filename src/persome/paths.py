"""Single source of truth for on-disk locations under ~/.persome/."""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from pathlib import Path
from typing import BinaryIO, TextIO

_PRIVATE_DIR_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_PERMISSIONS_MIGRATION_MARKER = ".permissions-v1"


def root() -> Path:
    override = os.environ.get("PERSOME_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".persome").resolve()


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


def launchd_stdout_log() -> Path:
    """stdout sink for the launchd-managed daemon. Lives under logs/ so the
    diagnostic bundle (which globs logs/) collects it automatically."""
    return logs_dir() / "launchd.out.log"


def launchd_stderr_log() -> Path:
    """stderr sink for the launchd-managed daemon (see launchd_stdout_log)."""
    return logs_dir() / "launchd.err.log"


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` and enforce owner-only traversal.

    The runtime stores screen text, URLs, screenshots, memories, and logs.  A
    permissive process umask must therefore never turn a newly-created data
    directory into a group/world-readable boundary.
    """
    path.mkdir(parents=True, exist_ok=True, mode=_PRIVATE_DIR_MODE)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError(f"private data directory must not be a symlink: {path}") from exc
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise RuntimeError(f"private data path is not a directory: {path}")
        os.fchmod(fd, _PRIVATE_DIR_MODE)
    finally:
        os.close(fd)
    return path


def ensure_private_file(path: Path) -> Path:
    """Enforce owner-only access on an existing sensitive file."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    # SQLite may unlink an idle WAL/SHM between lstat(), open(), and fstat()
    # while another connection is starting. Retry path replacement, and treat
    # a file that genuinely disappeared as already safe. Static symlinks,
    # special files, and hard links still fail before chmod or any data access.
    for _attempt in range(3):
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return path
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeError(f"private data file must not be a symlink: {path}")
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(f"private data path is not a regular file: {path}")
        if metadata.st_nlink != 1:
            raise RuntimeError(f"private data file must not be hard-linked: {path}")
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RuntimeError(f"cannot safely open private data file: {path}") from exc
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise RuntimeError(f"private data path is not a regular file: {path}")
            if opened.st_nlink == 0 or (opened.st_dev, opened.st_ino) != (
                metadata.st_dev,
                metadata.st_ino,
            ):
                continue
            if opened.st_nlink != 1:
                raise RuntimeError(f"private data file must not be hard-linked: {path}")
            os.fchmod(fd, _PRIVATE_FILE_MODE)
            return path
        finally:
            os.close(fd)
    try:
        path.lstat()
    except FileNotFoundError:
        return path
    raise RuntimeError(f"private data file changed during safety validation: {path}")


def _is_within_data_root(path: Path) -> bool:
    try:
        path.absolute().relative_to(root().absolute())
    except ValueError:
        return False
    try:
        path.parent.resolve().relative_to(root().resolve())
    except ValueError as exc:
        raise RuntimeError(f"private path escapes PERSOME_ROOT through a symlink: {path}") from exc
    return True


def atomic_write_private_text(path: Path, content: str, *, encoding: str = "utf-8") -> Path:
    """Atomically replace a sensitive text file with an owner-only regular file.

    Writing a private temp inode and renaming it over the destination avoids the
    check/write gap of ``Path.write_text(); chmod()``. A pre-positioned symlink,
    FIFO, or hard link at the predictable destination is replaced rather than
    followed or opened.
    """
    if _is_within_data_root(path):
        ensure_private_dir(path.parent)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, _PRIVATE_FILE_MODE)
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        ensure_private_file(path)
        with contextlib.suppress(OSError):
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            directory_fd = os.open(path.parent, flags)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def open_private_append_text(
    path: Path,
    *,
    encoding: str | None = "utf-8",
    errors: str | None = None,
) -> TextIO:
    """Open an owner-only regular file for append without following links/FIFOs."""
    ensure_private_dir(path.parent)
    flags = (
        os.O_WRONLY
        | os.O_APPEND
        | os.O_CREAT
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    fd = os.open(path, flags, _PRIVATE_FILE_MODE)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise RuntimeError(f"private append target must be one regular inode: {path}")
        os.fchmod(fd, _PRIVATE_FILE_MODE)
        return os.fdopen(fd, "a", encoding=encoding, errors=errors)
    except BaseException:
        os.close(fd)
        raise


def append_private_text(path: Path, content: str, *, encoding: str = "utf-8") -> Path:
    """Append text through :func:`open_private_append_text`."""
    with open_private_append_text(path, encoding=encoding) as handle:
        handle.write(content)
    return path


def open_private_lock_file(path: Path) -> BinaryIO:
    """Open an owner-only regular lock inode without following links or FIFOs."""
    ensure_private_dir(path.parent)
    flags = (
        os.O_RDWR
        | os.O_APPEND
        | os.O_CREAT
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    fd = os.open(path, flags, _PRIVATE_FILE_MODE)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise RuntimeError(f"private lock target must be one regular inode: {path}")
        os.fchmod(fd, _PRIVATE_FILE_MODE)
        return os.fdopen(fd, "a+b")
    except BaseException:
        os.close(fd)
        raise


def _migrate_existing_permissions() -> None:
    """One-time repair for data written by releases that inherited umask 022.

    Traversing a large capture buffer on every short-lived CLI command would be
    unnecessarily expensive, so the recursive repair is recorded once.  The
    root and known data directories are still re-checked on every call; because
    the root itself is 0700, descendants remain inaccessible to other users
    even if a later manual edit loosens an individual file mode.
    """
    marker = root() / _PERMISSIONS_MIGRATION_MARKER
    if marker.is_symlink():
        raise RuntimeError(f"permission migration marker must not be a symlink: {marker}")
    if marker.is_file() and not marker.is_symlink():
        ensure_private_file(marker)
        return

    private_trees = (
        memory_dir(),
        capture_buffer_dir(),
        logs_dir(),
        exports_dir(),
        backup_dir(),
        root() / "projection-md",
        # Legacy trees written by versions that shipped the removed Chat
        # feature; existing installs keep owner-only permissions on them.
        root() / "skills",
        root() / "chat-history",
    )
    for tree in private_trees:
        if not tree.exists():
            continue
        for item in (tree, *tree.rglob("*")):
            with contextlib.suppress(FileNotFoundError, PermissionError):
                if item.is_symlink():
                    continue
                if item.is_dir():
                    item.chmod(_PRIVATE_DIR_MODE)
                elif item.is_file():
                    metadata = item.lstat()
                    if metadata.st_nlink != 1:
                        raise RuntimeError(
                            f"permission migration refuses hard-linked data file: {item}"
                        )
                    # Restore owner read/write and preserve only the owner's
                    # execute bit (custom skill tools may be executable).
                    item.chmod(_PRIVATE_FILE_MODE | (metadata.st_mode & 0o100))

    # Root-level databases, state, locks, config and quarantine copies are also
    # personal data.  Installed environments live in subdirectories and are not
    # traversed or modified here.
    for item in root().iterdir():
        with contextlib.suppress(FileNotFoundError, PermissionError):
            if item.is_symlink():
                continue
            if item.is_file():
                metadata = item.lstat()
                if metadata.st_nlink != 1:
                    raise RuntimeError(
                        f"permission migration refuses hard-linked data file: {item}"
                    )
                item.chmod(_PRIVATE_FILE_MODE | (metadata.st_mode & 0o100))

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(marker, flags, _PRIVATE_FILE_MODE)
    except FileExistsError:
        ensure_private_file(marker)
    else:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("owner-only storage permissions applied\n")


def ensure_dirs() -> None:
    for d in (root(), memory_dir(), capture_buffer_dir(), logs_dir()):
        ensure_private_dir(d)
    # Optional directories stay lazy, but existing ones are always protected.
    for d in (
        exports_dir(),
        backup_dir(),
        root() / "projection-md",
        # Legacy Chat-era trees — protected when present, never created.
        root() / "skills",
        root() / "chat-history",
    ):
        if d.exists():
            ensure_private_dir(d)
    _migrate_existing_permissions()

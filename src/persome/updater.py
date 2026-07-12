"""Self-update orchestration for the installed Persome Runtime.

The public updater intentionally downloads a fresh, shallow checkout instead
of mutating a user's development repository.  ``install.sh --update`` remains
the single installation authority, so dependency locking, wheel construction,
secret preservation, native-helper compilation, onboarding, and runtime proof
stay identical to a manual update.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import tempfile
import time
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from . import paths

OFFICIAL_REPOSITORY = "https://github.com/Intuition-Lab/personal-model.git"
DEFAULT_BRANCH = "main"


class UpdateError(RuntimeError):
    """The Runtime could not be updated safely."""


@dataclass(frozen=True)
class UpdateSource:
    path: Path
    revision: str
    official: bool


def _validate_source(path: Path) -> Path:
    requested = path.expanduser()
    if requested.is_symlink():
        raise UpdateError(f"update source must not be a symlink: {requested}")
    root = requested.resolve()
    installer = root / "install.sh"
    required_files = (
        root / "pyproject.toml",
        root / "uv.lock",
        root / "build-constraints.txt",
        installer,
    )
    package_dir = root / "src" / "persome"
    if (
        not root.is_dir()
        or root.is_symlink()
        or package_dir.is_symlink()
        or not package_dir.is_dir()
        or any(item.is_symlink() or not item.is_file() for item in required_files)
    ):
        raise UpdateError(f"not a complete Persome source checkout: {root}")
    try:
        with (root / "pyproject.toml").open("rb") as handle:
            project_name = tomllib.load(handle).get("project", {}).get("name")
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise UpdateError(f"could not validate {root / 'pyproject.toml'}: {exc}") from exc
    if project_name != "persome-core":
        raise UpdateError(f"unexpected project name in update source: {project_name!r}")
    return root


def _revision(path: Path) -> str:
    git = shutil.which("git")
    if git is None:
        return "local source"
    result = subprocess.run(
        [git, "-C", str(path), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    revision = result.stdout.strip()
    return revision if result.returncode == 0 and revision else "local source"


@contextlib.contextmanager
def acquire_source(source: Path | None = None) -> Iterator[UpdateSource]:
    """Yield a validated local source or a fresh official ``main`` checkout."""
    if source is not None:
        root = _validate_source(source)
        yield UpdateSource(root, _revision(root), False)
        return

    git = shutil.which("git")
    if git is None:
        raise UpdateError("git is required to download the latest Persome Runtime")
    with tempfile.TemporaryDirectory(prefix="persome-update-") as temporary:
        root = Path(temporary) / "personal-model"
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        result = subprocess.run(
            [
                git,
                "clone",
                "--depth",
                "1",
                "--single-branch",
                "--branch",
                DEFAULT_BRANCH,
                OFFICIAL_REPOSITORY,
                str(root),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "git clone failed"
            raise UpdateError(f"could not download official {DEFAULT_BRANCH}: {detail}")
        validated = _validate_source(root)
        revision = _revision(validated)
        if revision == "local source":
            raise UpdateError("downloaded update has no Git revision")
        yield UpdateSource(validated, revision, True)


def _running_daemon_pid() -> int | None:
    try:
        pid = int(paths.pid_file().read_text(encoding="utf-8").strip())
    except (FileNotFoundError, OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid
    return pid


def launchagent_is_loaded() -> bool:
    """Whether launchd currently owns the Runtime lifecycle."""
    from . import launchagent

    return launchagent.is_loaded()


def stop_runtime(*, launchagent_was_loaded: bool, timeout: float = 20.0) -> None:
    """Stop launchd ownership and any current daemon before replacing code."""
    from . import launchagent

    if launchagent_was_loaded:
        result = launchagent.bootout()
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "launchctl bootout failed"
            raise UpdateError(f"could not stop the Persome LaunchAgent: {detail}")

    pid = _running_daemon_pid()
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError as exc:
        raise UpdateError(f"could not stop Persome daemon pid {pid}: {exc}") from exc

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _running_daemon_pid() is None:
            return
        time.sleep(0.2)
    raise UpdateError(f"Persome daemon pid {pid} did not stop within {timeout:.0f}s")


def run_installer(source: UpdateSource) -> None:
    """Install the validated source through the locked update-mode installer."""
    env = os.environ.copy()
    # ``PERSOME_ROOT`` is the Runtime's canonical path override, while the
    # installer historically calls the same location ``PERSOME_INSTALL_HOME``.
    # Pin both so a custom-root install can never be updated into ~/.persome by
    # accident.
    env["PERSOME_ROOT"] = str(paths.root())
    env["PERSOME_INSTALL_HOME"] = str(paths.root())
    result = subprocess.run(
        ["/bin/bash", str(source.path / "install.sh"), "--update"],
        cwd=source.path,
        check=False,
        env=env,
    )
    if result.returncode != 0:
        raise UpdateError(
            "the installer did not complete; the existing Persome data and secrets were preserved"
        )


def restore_launchagent(was_loaded: bool) -> None:
    """Restore launchd ownership with the newly installed Runtime binary."""
    if not was_loaded:
        return
    binary = paths.root() / "venv" / "bin" / "persome"
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise UpdateError(f"updated Persome executable is missing: {binary}")
    result = subprocess.run(
        [str(binary), "launchagent", "install", "--binary", str(binary)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "launchagent install failed"
        raise UpdateError(f"update succeeded but LaunchAgent restoration failed: {detail}")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if launchagent_is_loaded() and _running_daemon_pid() is not None:
            return
        time.sleep(0.25)
    raise UpdateError("updated LaunchAgent did not become loaded and running within 15s")


def recover_runtime(launchagent_was_loaded: bool) -> None:
    """Best-effort recovery when an update fails after the old daemon stopped."""
    if _running_daemon_pid() is not None:
        return
    if launchagent_was_loaded:
        restore_launchagent(True)
        return
    binary = paths.root() / "venv" / "bin" / "persome"
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise UpdateError(f"no runnable Persome executable remains at {binary}")
    result = subprocess.run(
        [str(binary), "start"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0 and _running_daemon_pid() is None:
        detail = result.stderr.strip() or result.stdout.strip() or "runtime restart failed"
        raise UpdateError(f"could not restart the previous Runtime: {detail}")

"""Self-update orchestration for the installed Persome Runtime.

The public updater intentionally downloads a fresh, shallow checkout instead
of mutating a user's development repository.  ``install.sh --update`` remains
the single installation authority, so dependency locking, wheel construction,
secret preservation, native-helper compilation, onboarding, and runtime proof
stay identical to a manual update.
"""

from __future__ import annotations

import contextlib
import fcntl
import importlib.metadata
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from . import config as config_mod
from . import launchagent, paths, runtime_pid

OFFICIAL_REPOSITORY = "https://github.com/Intuition-Lab/personal-model.git"
DEFAULT_BRANCH = "main"


class UpdateError(RuntimeError):
    """The Runtime could not be updated safely."""


class UpdateCancelled(UpdateError):
    """The user cancelled an update after its rollback completed."""

    def __init__(self, message: str, *, signum: int = signal.SIGINT) -> None:
        super().__init__(message)
        self.signum = int(signum)
        self.exit_code = 128 + self.signum


class UpdateSignal(BaseException):
    """A termination signal received while an update transaction is active."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = int(signum)
        self.exit_code = 128 + self.signum


@dataclass(frozen=True)
class UpdateSource:
    path: Path
    revision: str
    official: bool


_CANCELLATION_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
_LEGACY_HANDOFF_ENV = "PERSOME_UPDATE_INFER_LAUNCHAGENT_FROM_PLIST"
_LEGACY_LOCK_WAIT_SECONDS = 300.0
_LEGACY_KEEPER_MAX_SECONDS = 300.0
_TRANSACTION_MARKER = ".persome-update-transaction"
_ACTIVE_UPDATE_LOCK_FD: int | None = None


@dataclass(frozen=True)
class UpdateTransaction:
    launchagent_was_loaded: bool
    phase: str
    transaction_id: str


@dataclass(frozen=True)
class LegacyUpdateTransaction:
    """Recovery metadata written by the pre-candidate updater."""

    launchagent_was_loaded: bool
    phase: str


@contextlib.contextmanager
def catch_update_signals() -> Iterator[None]:
    """Translate terminal/process cancellation into a recoverable exception."""

    previous: dict[signal.Signals, signal.Handlers] = {}

    def _raise(signum: int, _frame: object) -> None:
        raise UpdateSignal(signum)

    for signum in _CANCELLATION_SIGNALS:
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, _raise)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


@contextlib.contextmanager
def ignore_update_signals() -> Iterator[None]:
    """Keep atomic rollback/recovery alive through repeated cancellation."""

    previous: dict[signal.Signals, signal.Handlers] = {}
    for signum in _CANCELLATION_SIGNALS:
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, signal.SIG_IGN)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def claim_legacy_foreground() -> bool:
    """Retained API for older CLI callers; signal ownership stays unchanged.

    A delegated updater may itself be a session leader, so trying to move it
    into the terminal foreground is neither reliable nor necessary.  The new
    installer never changes the active venv, and the inherited update-lock
    keeper prevents the previous updater from racing the completed handoff.
    """

    return False


def _update_lock_path() -> Path:
    return paths.root() / ".update.lock"


def _update_backup_dir() -> Path:
    """Legacy pre-candidate backup path, checked only to fail closed."""

    return paths.root() / "venv.previous.update"


def _update_replacement_dir() -> Path:
    return paths.root() / "venv.replacement.update"


def _update_state_file() -> Path:
    return paths.update_state_file()


def _venv_dir() -> Path:
    return paths.root() / "venv"


def _runtime_binary() -> Path:
    return _venv_dir() / "bin" / "persome"


def _runtime_python() -> Path:
    return _venv_dir() / "bin" / "python"


def is_external_package_install() -> bool:
    """Return whether the running CLI is the public package-manager install.

    Source installs own ``<PERSOME_ROOT>/venv`` and can use the transactional
    directory exchange. A PyPI tool/venv is owned by uv, pipx, or pip instead;
    that package manager must replace it before onboarding re-proves Runtime
    ownership and capture readiness.
    """

    if _venv_dir().is_dir():
        return False
    try:
        importlib.metadata.distribution("personal-model")
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


_LOCK_KEEPER_CODE = r"""
import os
import sys
import time

parent = int(sys.argv[1])
deadline = time.monotonic() + float(sys.argv[2])
while time.monotonic() < deadline:
    try:
        os.kill(parent, 0)
    except ProcessLookupError:
        break
    except PermissionError:
        pass
    time.sleep(0.1)
"""


def _spawn_legacy_lock_keeper(lock_fd: int, parent_pid: int) -> subprocess.Popen[bytes]:
    """Keep the inherited flock until the delegating updater actually exits."""

    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            _LOCK_KEEPER_CODE,
            str(parent_pid),
            str(_LEGACY_KEEPER_MAX_SECONDS),
        ],
        pass_fds=(lock_fd,),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


@contextlib.contextmanager
def update_lock() -> Iterator[None]:
    """Hold the one root-scoped update lock.

    Normal callers fail fast. A delegated previous-release updater has already
    touched lifecycle state before reaching this code, so it waits for the
    active transaction and leaves an inherited-fd lock keeper alive until its
    delegating parent exits (with a bounded failsafe).
    """

    global _ACTIVE_UPDATE_LOCK_FD
    previous_lock_fd = _ACTIVE_UPDATE_LOCK_FD
    lock_acquired = False
    legacy_parent_pid = os.getppid()
    try:
        handle = paths.open_private_lock_file(_update_lock_path())
    except (OSError, RuntimeError) as exc:
        raise UpdateError(f"could not open the Runtime update lock: {exc}") from exc
    try:
        legacy_handoff = os.environ.get(_LEGACY_HANDOFF_ENV) == "1"
        deadline = time.monotonic() + _LEGACY_LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                lock_acquired = True
                break
            except BlockingIOError as exc:
                if not legacy_handoff:
                    raise UpdateError("another Persome update is already in progress") from exc
                if time.monotonic() >= deadline:
                    raise UpdateError(
                        "timed out waiting for the in-progress Persome update to finish"
                    ) from exc
                time.sleep(0.2)
        _ACTIVE_UPDATE_LOCK_FD = handle.fileno()
        yield
    finally:
        keeper: subprocess.Popen[bytes] | None = None
        if lock_acquired and os.environ.get(_LEGACY_HANDOFF_ENV) == "1":
            with contextlib.suppress(OSError):
                keeper = _spawn_legacy_lock_keeper(handle.fileno(), legacy_parent_pid)
        _ACTIVE_UPDATE_LOCK_FD = previous_lock_fd
        if lock_acquired and keeper is None:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


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
    try:
        result = subprocess.run(
            [git, "-C", str(path), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "local source"
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
        try:
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
        except subprocess.TimeoutExpired as exc:
            raise UpdateError(
                f"downloading official {DEFAULT_BRANCH} timed out after 180 seconds"
            ) from exc
        except OSError as exc:
            raise UpdateError(f"could not run git to download the Runtime: {exc}") from exc
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "git clone failed"
            raise UpdateError(f"could not download official {DEFAULT_BRANCH}: {detail}")
        validated = _validate_source(root)
        revision = _revision(validated)
        if revision == "local source":
            raise UpdateError("downloaded update has no Git revision")
        yield UpdateSource(validated, revision, True)


def _running_daemon_process() -> runtime_pid.ProcessIdentity | None:
    """Return only a PID-file process whose complete Runtime identity matches."""

    return runtime_pid.resolve_recorded_process()


def _running_daemon_pid() -> int | None:
    process = _running_daemon_process()
    return process.pid if process is not None else None


def launchagent_is_loaded() -> bool:
    """Whether launchd currently owns the Runtime lifecycle."""

    try:
        result = subprocess.run(
            ["launchctl", "print", launchagent.gui_domain_target()],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired as exc:
        raise UpdateError("checking the Persome LaunchAgent timed out after 10 seconds") from exc
    except OSError as exc:
        raise UpdateError(f"could not inspect the Persome LaunchAgent: {exc}") from exc
    return result.returncode == 0


def launchagent_should_be_restored() -> bool:
    """Return the intended owner, including a previous-updater handoff."""
    if launchagent_is_loaded():
        return True
    if launchagent.owner_intended():
        return True
    if os.environ.get("PERSOME_UPDATE_INFER_LAUNCHAGENT_FROM_PLIST") != "1":
        return False
    # A released updater booted launchd out before invoking the new installer,
    # so the loaded state is no longer observable. Its retained, owner-only
    # plist is the compatibility receipt; accept the normal shim as well as a
    # direct venv binary. Future installs use the explicit owner marker above.
    return launchagent.configured_runtime_binary() is not None


def _bootout_launchagent() -> None:
    try:
        result = subprocess.run(
            ["launchctl", "bootout", launchagent.gui_domain_target()],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except subprocess.TimeoutExpired as exc:
        raise UpdateError("stopping the Persome LaunchAgent timed out after 20 seconds") from exc
    except OSError as exc:
        raise UpdateError(f"could not stop the Persome LaunchAgent: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "launchctl bootout failed"
        raise UpdateError(f"could not stop the Persome LaunchAgent: {detail}")


def stop_runtime(
    *, launchagent_was_loaded: bool, timeout: float = 20.0, force: bool = False
) -> None:
    """Stop launchd ownership and any current daemon before replacing code."""
    process = _running_daemon_process()
    if launchagent_was_loaded and launchagent_is_loaded():
        _bootout_launchagent()
    if process is None:
        process = _running_daemon_process()
    if process is None:
        problem = runtime_pid.unresolved_runtime_reason()
        if problem is not None:
            raise UpdateError(f"could not safely stop Persome: {problem}")
        return
    if not runtime_pid.same_process_is_running(process):
        return
    if not runtime_pid.signal_process(process, signal.SIGTERM):
        if not runtime_pid.same_process_is_running(process):
            return
        raise UpdateError(f"could not safely stop Persome daemon pid {process.pid}")
    if runtime_pid.wait_for_exit(process, timeout):
        return
    if (
        force
        and runtime_pid.signal_process(process, signal.SIGKILL)
        and runtime_pid.wait_for_exit(process, 5)
    ):
        return
    raise UpdateError(f"Persome daemon pid {process.pid} did not stop within {timeout:.0f}s")


def _child_environment(*, include_source: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["PERSOME_ROOT"] = str(paths.root())
    env["PERSOME_INSTALL_HOME"] = str(paths.root())
    env["PYTHONNOUSERSITE"] = "1"
    for variable in (
        "SSL_CERT_FILE",
        "PYTHONHOME",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "__PYVENV_LAUNCHER__",
    ):
        env.pop(variable, None)
    old_bin = str(_venv_dir() / "bin")
    env["PATH"] = os.pathsep.join(
        component for component in env.get("PATH", "").split(os.pathsep) if component != old_bin
    )
    if include_source is not None:
        env["PYTHONPATH"] = str(include_source / "src")
    return env


def run_installer(source: UpdateSource) -> None:
    """Build a marked candidate venv without changing the active installation."""

    state = _read_update_state()
    if not isinstance(state, UpdateTransaction) or state.phase != "preparing":
        raise UpdateError("the update installer requires an active preparing transaction")
    lock_fd = _ACTIVE_UPDATE_LOCK_FD
    if lock_fd is None:
        raise UpdateError("the update installer requires the exclusive Runtime update lock")
    env = _child_environment()
    env["PERSOME_UPDATE_DEFER_COMMIT"] = "1"
    env["PERSOME_UPDATE_REPLACEMENT"] = str(_update_replacement_dir())
    env["PERSOME_UPDATE_TRANSACTION_ID"] = state.transaction_id
    env["PERSOME_UPDATE_LOCK_FD"] = str(lock_fd)
    try:
        process = subprocess.Popen(
            ["/bin/bash", str(source.path / "install.sh"), "--update"],
            cwd=source.path,
            env=env,
            pass_fds=(lock_fd,),
            start_new_session=True,
        )
    except OSError as exc:
        raise UpdateError(f"could not start the transactional installer: {exc}") from exc
    try:
        try:
            returncode = process.wait()
        except (KeyboardInterrupt, UpdateSignal) as exc:
            signum = signal.SIGINT if isinstance(exc, KeyboardInterrupt) else exc.signum
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(process.pid, signum)
            with ignore_update_signals():
                rollback_deadline = time.monotonic() + 30
                while process.poll() is None:
                    remaining = rollback_deadline - time.monotonic()
                    if remaining <= 0:
                        with contextlib.suppress(ProcessLookupError, PermissionError):
                            os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                        break
                    try:
                        process.wait(timeout=remaining)
                    except (KeyboardInterrupt, UpdateSignal):
                        continue
                    except subprocess.TimeoutExpired:
                        continue
            # Cancellation wins even if the child happened to exit 0 between
            # signal delivery and poll(). The active venv was never touched.
            raise UpdateCancelled(
                "update cancelled; the previous installation remains active",
                signum=signum,
            ) from exc
    except OSError as exc:
        raise UpdateError(f"waiting for the transactional installer failed: {exc}") from exc
    if returncode != 0:
        raise UpdateError(
            "the installer did not complete; the existing Persome data and secrets were preserved"
        )
    if not transaction_prepared():
        raise UpdateError("the installer exited without a complete, marked candidate venv")


def _write_update_state(*, launchagent_was_loaded: bool, phase: str, transaction_id: str) -> None:
    if phase not in {"preparing", "prepared", "activated", "committing"}:
        raise ValueError(f"invalid update phase: {phase}")
    if len(transaction_id) != 32 or any(
        character not in "0123456789abcdef" for character in transaction_id
    ):
        raise ValueError("invalid update transaction ID")
    paths.atomic_write_private_text(
        _update_state_file(),
        json.dumps(
            {
                "schema_version": 2,
                "launchagent_was_loaded": launchagent_was_loaded,
                "phase": phase,
                "transaction_id": transaction_id,
            },
            sort_keys=True,
        ),
    )
    _fsync_root()


def _read_update_state() -> UpdateTransaction | LegacyUpdateTransaction | None:
    state_file = _update_state_file()
    try:
        paths.ensure_private_file(state_file)
        payload = json.loads(state_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        raise UpdateError(f"could not safely read unfinished update state: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("launchagent_was_loaded"), bool):
        raise UpdateError(f"unfinished update state is invalid: {state_file}")
    if payload.get("schema_version") == 1 and payload.get("phase") in {
        "preparing",
        "prepared",
        "committing",
    }:
        return LegacyUpdateTransaction(
            bool(payload["launchagent_was_loaded"]), str(payload["phase"])
        )
    if (
        payload.get("schema_version") != 2
        or payload.get("phase") not in {"preparing", "prepared", "activated", "committing"}
        or not isinstance(payload.get("transaction_id"), str)
        or len(payload["transaction_id"]) != 32
        or any(character not in "0123456789abcdef" for character in payload["transaction_id"])
    ):
        raise UpdateError(f"unfinished update state is invalid: {state_file}")
    return UpdateTransaction(
        bool(payload["launchagent_was_loaded"]),
        str(payload["phase"]),
        str(payload["transaction_id"]),
    )


def begin_update_transaction(launchagent_was_loaded: bool) -> str:
    if (
        _read_update_state() is not None
        or os.path.lexists(_update_replacement_dir())
        or os.path.lexists(_update_backup_dir())
    ):
        raise UpdateError("an unfinished update must be recovered before starting another")
    transaction_id = uuid.uuid4().hex
    _write_update_state(
        launchagent_was_loaded=launchagent_was_loaded,
        phase="preparing",
        transaction_id=transaction_id,
    )
    return transaction_id


def mark_update_phase(launchagent_was_loaded: bool, phase: str) -> None:
    state = _read_update_state()
    if (
        not isinstance(state, UpdateTransaction)
        or state.launchagent_was_loaded != launchagent_was_loaded
    ):
        raise UpdateError("the update transaction owner metadata changed unexpectedly")
    _write_update_state(
        launchagent_was_loaded=launchagent_was_loaded,
        phase=phase,
        transaction_id=state.transaction_id,
    )


def clear_update_state() -> None:
    state = _read_update_state()
    if isinstance(state, UpdateTransaction):
        active_marker = _marker_transaction(_venv_dir())
        if active_marker not in {None, state.transaction_id}:
            raise UpdateError("the active Runtime marker does not match update state")
    if (
        isinstance(state, UpdateTransaction)
        and _marker_transaction(_venv_dir()) == state.transaction_id
    ):
        marker = _venv_dir() / _TRANSACTION_MARKER
        marker.unlink()
        _fsync_directory(_venv_dir())
    with contextlib.suppress(FileNotFoundError):
        _update_state_file().unlink()
    _fsync_root()


def _fsync_root() -> None:
    """Durably order transaction metadata and virtualenv directory renames."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(paths.root(), flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise UpdateError(f"could not persist the update transaction directory: {exc}") from exc


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(directory, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise UpdateError(f"could not persist update metadata in {directory}: {exc}") from exc


def _marker_transaction(directory: Path) -> str | None:
    marker = directory / _TRANSACTION_MARKER
    try:
        stat_result = marker.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise UpdateError(f"could not inspect candidate transaction marker: {exc}") from exc
    if (
        marker.is_symlink()
        or not marker.is_file()
        or stat_result.st_uid != os.getuid()
        or stat_result.st_mode & 0o077
        or stat_result.st_nlink != 1
    ):
        raise UpdateError(f"candidate transaction marker is unsafe: {marker}")
    try:
        value = marker.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise UpdateError(f"could not read candidate transaction marker: {exc}") from exc
    if len(value) != 32 or any(character not in "0123456789abcdef" for character in value):
        raise UpdateError(f"candidate transaction marker is invalid: {marker}")
    return value


def cleanup_transaction_artifacts() -> None:
    for candidate in paths.root().glob("venv.previous.committed.*"):
        if candidate.is_dir() and not candidate.is_symlink():
            with contextlib.suppress(OSError):
                shutil.rmtree(candidate)


def ensure_no_pending_update() -> None:
    if (
        _read_update_state() is not None
        or os.path.lexists(_update_replacement_dir())
        or os.path.lexists(_update_backup_dir())
    ):
        raise UpdateError("an unfinished update remains after automatic recovery")
    cleanup_transaction_artifacts()


def transaction_prepared() -> bool:
    state = _read_update_state()
    candidate = _update_replacement_dir()
    return bool(
        isinstance(state, UpdateTransaction)
        and state.phase == "preparing"
        and candidate.is_dir()
        and not candidate.is_symlink()
        and _marker_transaction(candidate) == state.transaction_id
    )


def _atomic_exchange(first: Path, second: Path) -> None:
    """Exchange two same-filesystem directories in one kernel operation."""

    try:
        paths.atomic_exchange(first, second)
    except (OSError, ValueError) as exc:
        raise UpdateError(
            f"could not atomically exchange the active and candidate Runtime: {exc}"
        ) from exc
    _fsync_root()


def _validate_safe_venv(directory: Path, *, label: str) -> None:
    if directory.is_symlink() or not directory.is_dir():
        raise UpdateError(f"{label} is not a safe virtualenv directory: {directory}")


def _validate_relocatable_candidate(directory: Path) -> None:
    """Reject a console script that would execute the old directory after exchange."""
    executable = directory / "bin" / "persome"
    if executable.is_symlink() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise UpdateError(f"candidate Runtime executable is missing or unsafe: {executable}")
    try:
        script = executable.read_bytes()
    except OSError as exc:
        raise UpdateError(f"could not inspect candidate Runtime executable: {exc}") from exc
    if os.fsencode(directory) in script:
        raise UpdateError(
            "candidate Runtime is not relocatable; its console script contains the inactive "
            "virtualenv path"
        )


def activate_prepared_install() -> None:
    """Atomically activate the complete candidate before starting its daemon."""

    state = _read_update_state()
    if not isinstance(state, UpdateTransaction) or state.phase != "prepared":
        raise UpdateError("the candidate Runtime is not in prepared state")
    active = _venv_dir()
    candidate = _update_replacement_dir()
    _validate_safe_venv(active, label="active Runtime")
    _validate_safe_venv(candidate, label="candidate Runtime")
    _validate_relocatable_candidate(candidate)
    if _marker_transaction(candidate) != state.transaction_id:
        raise UpdateError("the candidate Runtime does not belong to this update transaction")
    if _marker_transaction(active) is not None:
        raise UpdateError("the active Runtime unexpectedly contains an update marker")
    _atomic_exchange(active, candidate)
    _write_update_state(
        launchagent_was_loaded=state.launchagent_was_loaded,
        phase="activated",
        transaction_id=state.transaction_id,
    )


def _wait_for_runtime_process(timeout: float = 20.0) -> runtime_pid.ProcessIdentity:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        process = _running_daemon_process()
        if process is not None:
            return process
        time.sleep(0.2)
    raise UpdateError(
        f"the Runtime process did not become identity-verifiable within {timeout:.0f}s"
    )


def restore_launchagent(was_loaded: bool) -> None:
    """Restore launchd ownership; the caller performs the final Runtime proof."""
    if not was_loaded:
        return
    binary = _runtime_binary()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise UpdateError(f"updated Persome executable is missing: {binary}")
    try:
        result = subprocess.run(
            [str(binary), "launchagent", "install", "--binary", str(binary)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=_child_environment(),
        )
    except subprocess.TimeoutExpired as exc:
        raise UpdateError("restoring the Persome LaunchAgent timed out after 30 seconds") from exc
    except OSError as exc:
        raise UpdateError(f"could not run LaunchAgent restoration: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "launchagent install failed"
        raise UpdateError(f"update succeeded but LaunchAgent restoration failed: {detail}")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if launchagent_is_loaded() and launchagent.owns_recorded_runtime(str(binary)):
            return
        time.sleep(0.25)
    raise UpdateError("updated LaunchAgent did not become loaded and running within 15s")


def _start_background_runtime() -> None:
    binary = _runtime_binary()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise UpdateError(f"no runnable Persome executable remains at {binary}")
    try:
        result = subprocess.run(
            [str(binary), "start"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=_child_environment(),
        )
    except subprocess.TimeoutExpired as exc:
        raise UpdateError("starting the Persome Runtime timed out after 30 seconds") from exc
    except OSError as exc:
        raise UpdateError(f"could not start the Persome Runtime: {exc}") from exc
    if result.returncode != 0 and _running_daemon_pid() is None:
        detail = result.stderr.strip() or result.stdout.strip() or "runtime start failed"
        raise UpdateError(f"could not start the Persome Runtime: {detail}")
    _wait_for_runtime_process()


def activate_runtime(launchagent_was_loaded: bool) -> None:
    """Start the prepared replacement under the user's prior ownership mode."""

    activate_prepared_install()
    if launchagent_was_loaded:
        restore_launchagent(True)
    else:
        _start_background_runtime()


def prove_runtime(launchagent_was_loaded: bool) -> None:
    """Run onboarding proof against the final owner without changing capture policy."""

    binary = _runtime_binary()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise UpdateError(f"Runtime proof executable is missing: {binary}")
    command = [
        str(binary),
        "onboard",
        "--preserve-policy",
        "--expect-owner",
        "launchagent" if launchagent_was_loaded else "background",
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            timeout=240,
            env=_child_environment(),
        )
    except subprocess.TimeoutExpired as exc:
        raise UpdateError("final Runtime onboarding proof timed out after 240 seconds") from exc
    except OSError as exc:
        raise UpdateError(f"could not run the final Runtime onboarding proof: {exc}") from exc
    if result.returncode != 0:
        raise UpdateError("the final Runtime owner failed onboarding health/capture proof")


def commit_prepared_install() -> Path:
    """Commit the activated Runtime and return its old-venv cleanup path."""

    state = _read_update_state()
    previous = _update_replacement_dir()
    if not isinstance(state, UpdateTransaction) or state.phase != "committing":
        raise UpdateError("the activated Runtime is not ready to commit")
    _validate_safe_venv(_venv_dir(), label="activated Runtime")
    _validate_safe_venv(previous, label="retained previous Runtime")
    if _marker_transaction(_venv_dir()) != state.transaction_id:
        raise UpdateError("the activated Runtime does not belong to this update transaction")
    if _marker_transaction(previous) is not None:
        raise UpdateError("the retained previous Runtime has an unexpected update marker")
    cleanup = paths.root() / f"venv.previous.committed.{uuid.uuid4().hex}"
    try:
        os.replace(previous, cleanup)
        _fsync_root()
    except OSError as exc:
        raise UpdateError(f"could not commit the prepared Runtime update: {exc}") from exc
    return cleanup


def cleanup_committed_install(cleanup: Path) -> None:
    """Best-effort cleanup after the atomic commit point."""

    if cleanup.parent != paths.root() or not cleanup.name.startswith("venv.previous.committed."):
        return
    with contextlib.suppress(OSError):
        shutil.rmtree(cleanup)


def rollback_prepared_install() -> bool:
    """Restore old code if exchanged, or discard an unactivated candidate."""

    state = _read_update_state()
    candidate = _update_replacement_dir()
    if state is None:
        if os.path.lexists(candidate):
            raise UpdateError("a candidate Runtime exists without update transaction metadata")
        return False
    if not isinstance(state, UpdateTransaction):
        raise UpdateError("legacy update artifacts require legacy recovery")
    active = _venv_dir()
    _validate_safe_venv(active, label="active Runtime")
    active_marker = _marker_transaction(active)
    if active_marker is not None and active_marker != state.transaction_id:
        raise UpdateError("the active Runtime belongs to a different update transaction")
    if not os.path.lexists(candidate):
        if active_marker == state.transaction_id and state.phase != "committing":
            raise UpdateError("the previous Runtime is missing; rollback cannot continue safely")
        return False
    _validate_safe_venv(candidate, label="candidate Runtime")
    candidate_marker = _marker_transaction(candidate)
    if active_marker == state.transaction_id:
        if candidate_marker is not None:
            raise UpdateError("both Runtime directories contain update transaction markers")
        _atomic_exchange(active, candidate)
    elif candidate_marker not in {None, state.transaction_id}:
        raise UpdateError("the candidate Runtime belongs to a different update transaction")

    # Active is now definitively the old venv. Rename the failed/partial new
    # candidate before slow cleanup so the reserved transaction path is clear.
    failed = paths.root() / f"venv.failed.update.{uuid.uuid4().hex}"
    try:
        os.replace(candidate, failed)
        _fsync_root()
    except OSError as exc:
        raise UpdateError(f"could not quarantine the failed Runtime candidate: {exc}") from exc
    with contextlib.suppress(OSError):
        shutil.rmtree(failed)
    return True


def _wait_for_legacy_runtime_proof(timeout: float = 30.0) -> None:
    """Backward-compatible proof for a restored pre-preserve-policy Runtime."""

    process = _wait_for_runtime_process()
    cfg = config_mod.load()
    http_enabled = bool(cfg.mcp.auto_start and cfg.mcp.transport in {"sse", "streamable-http"})
    if not http_enabled:
        # HTTP is intentionally disabled. Pinning the full PID/start/command
        # identity across a short stability window proves the old owner stayed up.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if not runtime_pid.same_process_is_running(process):
                raise UpdateError("the restored Runtime exited during its stability proof")
            time.sleep(0.1)
        return
    deadline = time.monotonic() + timeout
    from .security.auth import LocalAPIConfigurationError, loopback_http_url

    try:
        endpoint = loopback_http_url(cfg.mcp.host, cfg.mcp.port, "/health")
    except LocalAPIConfigurationError as exc:
        raise UpdateError(f"could not build the restored Runtime health URL: {exc}") from exc
    while time.monotonic() < deadline:
        if not runtime_pid.same_process_is_running(process):
            raise UpdateError("the restored Runtime exited before its health proof")
        try:
            with urllib.request.urlopen(endpoint, timeout=2) as response:  # noqa: S310
                payload = json.loads(response.read())
        except (OSError, ValueError, urllib.error.URLError):
            time.sleep(0.25)
            continue
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict) and data.get("status") in {"ok", "degraded"}:
            return
        time.sleep(0.25)
    raise UpdateError("the restored Runtime did not pass its legacy health proof within 30 seconds")


def _clear_replacement_runtime_state() -> None:
    """Remove generation state only after the replacement process is stopped."""

    for target in (paths.pid_file(), paths.runtime_state_file()):
        with contextlib.suppress(FileNotFoundError):
            target.unlink()


def recover_runtime(launchagent_was_loaded: bool) -> None:
    """Restore prior lifecycle ownership and prove a legacy Runtime safely."""

    if launchagent_was_loaded:
        restore_launchagent(True)
    elif _running_daemon_pid() is None:
        _start_background_runtime()
    _wait_for_legacy_runtime_proof()


def rollback_and_recover(launchagent_was_loaded: bool) -> None:
    """Stop any replacement, restore old code/ownership, and run preserve proof."""

    currently_loaded = launchagent_is_loaded()
    stop_runtime(launchagent_was_loaded=currently_loaded, force=True)
    rollback_prepared_install()
    _clear_replacement_runtime_state()
    recover_runtime(launchagent_was_loaded)
    clear_update_state()


def _discard_directory(directory: Path, *, prefix: str) -> None:
    """Clear a reserved transaction path before its best-effort slow removal."""

    quarantined = paths.root() / f"{prefix}.{uuid.uuid4().hex}"
    try:
        os.replace(directory, quarantined)
        _fsync_root()
    except OSError as exc:
        raise UpdateError(f"could not quarantine update artifact {directory}: {exc}") from exc
    with contextlib.suppress(OSError):
        shutil.rmtree(quarantined)


def _recover_legacy_pending_update(state: LegacyUpdateTransaction) -> None:
    """Recover schema-v1 two-rename transactions from an installed release."""

    backup = _update_backup_dir()
    backup_exists = os.path.lexists(backup)
    active = _venv_dir()
    if state.phase == "committing" and not backup_exists:
        # The previous updater had already passed proof and renamed its backup
        # away. Preserve that committed installation and re-establish ownership.
        currently_loaded = launchagent_is_loaded()
        if currently_loaded != state.launchagent_was_loaded or _running_daemon_pid() is None:
            stop_runtime(launchagent_was_loaded=currently_loaded, force=True)
            if state.launchagent_was_loaded:
                restore_launchagent(True)
            else:
                _start_background_runtime()
        _wait_for_legacy_runtime_proof()
        clear_update_state()
        cleanup_transaction_artifacts()
        return

    currently_loaded = launchagent_is_loaded()
    stop_runtime(launchagent_was_loaded=currently_loaded, force=True)
    if backup_exists:
        _validate_safe_venv(backup, label="legacy previous Runtime")
        if os.path.lexists(active):
            _validate_safe_venv(active, label="legacy replacement Runtime")
            _atomic_exchange(active, backup)
            _discard_directory(backup, prefix="venv.failed.legacy-update")
        else:
            try:
                os.replace(backup, active)
                _fsync_root()
            except OSError as exc:
                raise UpdateError(f"could not restore the legacy previous Runtime: {exc}") from exc
    elif not active.is_dir() or active.is_symlink():
        raise UpdateError("the interrupted legacy update left no recoverable Runtime venv")
    _clear_replacement_runtime_state()
    recover_runtime(state.launchagent_was_loaded)
    clear_update_state()


def recover_pending_update() -> None:
    """Repair an interrupted deterministic transaction under the update lock."""

    state = _read_update_state()
    candidate_exists = os.path.lexists(_update_replacement_dir())
    legacy_backup_exists = os.path.lexists(_update_backup_dir())
    if state is None and not candidate_exists and not legacy_backup_exists:
        cleanup_transaction_artifacts()
        return
    if state is None:
        raise UpdateError("an update artifact exists without lifecycle recovery metadata")
    if isinstance(state, LegacyUpdateTransaction):
        _recover_legacy_pending_update(state)
        return
    if legacy_backup_exists:
        raise UpdateError(
            "a legacy update backup remains; move it aside only after verifying the active Runtime"
        )
    active_marker = _marker_transaction(_venv_dir())
    if state.phase == "committing" and active_marker == state.transaction_id:
        # Proof completed before the committing phase was written. Finish the
        # old-venv rename if needed, then re-prove the intended final owner.
        if candidate_exists:
            commit_prepared_install()
        currently_loaded = launchagent_is_loaded()
        if currently_loaded != state.launchagent_was_loaded or _running_daemon_pid() is None:
            stop_runtime(launchagent_was_loaded=currently_loaded, force=True)
            if state.launchagent_was_loaded:
                restore_launchagent(True)
            else:
                _start_background_runtime()
        else:
            _wait_for_runtime_process()
        prove_runtime(state.launchagent_was_loaded)
        clear_update_state()
        cleanup_transaction_artifacts()
        return
    rollback_and_recover(state.launchagent_was_loaded)
    cleanup_transaction_artifacts()

"""Safe Runtime self-update orchestration."""

from __future__ import annotations

import contextlib
import fcntl
import importlib.metadata
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from persome import cli, paths, updater


def _source_tree(root: Path) -> Path:
    (root / "src" / "persome").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "persome-core"\nversion = "0.0.0"\n',
        encoding="utf-8",
    )
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (root / "build-constraints.txt").write_text("", encoding="utf-8")
    (root / "install.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    return root


def test_external_package_install_requires_public_distribution_and_no_managed_venv(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        importlib.metadata,
        "distribution",
        lambda name: object() if name == "personal-model" else None,
    )
    assert updater.is_external_package_install() is True

    (ac_root / "venv").mkdir()
    assert updater.is_external_package_install() is False


def test_root_distribution_without_managed_venv_is_not_external(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(_name: str) -> object:
        raise importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(importlib.metadata, "distribution", missing)
    assert updater.is_external_package_install() is False


def _begin_transaction(*, launchagent: bool = False) -> str:
    return updater.begin_update_transaction(launchagent)


def _write_candidate_marker(directory: Path, transaction_id: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    executable = directory / "bin" / "persome"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("#!/bin/sh\nprintf 'candidate runtime\\n'\n", encoding="utf-8")
    executable.chmod(0o700)
    marker = directory / ".persome-update-transaction"
    marker.write_text(transaction_id + "\n", encoding="utf-8")
    marker.chmod(0o600)


def test_local_source_is_validated_without_mutating_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _source_tree(tmp_path / "source")
    monkeypatch.setattr(updater, "_revision", lambda path: "a" * 40)

    with updater.acquire_source(root) as source:
        assert source.path == root
        assert source.revision == "a" * 40
        assert source.official is False

    assert root.exists()


def test_source_rejects_symlinked_installer(tmp_path: Path) -> None:
    root = _source_tree(tmp_path / "source")
    installer = root / "install.sh"
    installer.unlink()
    installer.symlink_to(tmp_path / "attacker.sh")

    with pytest.raises(updater.UpdateError, match="complete Persome"):
        updater._validate_source(root)


def test_official_source_is_a_fresh_shallow_main_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []

    class TemporaryDirectory:
        def __init__(self, **_: object) -> None:
            pass

        def __enter__(self) -> str:
            return str(tmp_path)

        def __exit__(self, *args: object) -> None:
            pass

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if "clone" in command:
            _source_tree(Path(command[-1]))
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, "b" * 40 + "\n", "")

    monkeypatch.setattr(updater.tempfile, "TemporaryDirectory", TemporaryDirectory)
    monkeypatch.setattr(updater.shutil, "which", lambda name: "/usr/bin/git")
    monkeypatch.setattr(updater.subprocess, "run", fake_run)

    with updater.acquire_source() as source:
        assert source.official is True
        assert source.revision == "b" * 40

    clone = commands[0]
    assert clone[:2] == ["/usr/bin/git", "clone"]
    assert "--depth" in clone and "1" in clone
    assert "--single-branch" in clone
    assert updater.DEFAULT_BRANCH in clone
    assert updater.OFFICIAL_REPOSITORY in clone


def test_stop_runtime_terminates_daemon_after_disabling_launchagent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = updater.runtime_pid.ProcessIdentity(4242, os.getuid(), 1.0, "persome start")
    calls: list[object] = []
    monkeypatch.setattr(updater, "_running_daemon_process", lambda: process)
    monkeypatch.setattr(updater, "launchagent_is_loaded", lambda: True)
    monkeypatch.setattr(updater, "_bootout_launchagent", lambda: calls.append("bootout"))
    monkeypatch.setattr(
        updater.runtime_pid,
        "same_process_is_running",
        lambda value: value is process,
    )
    monkeypatch.setattr(
        updater.runtime_pid,
        "signal_process",
        lambda value, sig: calls.append((value.pid, sig)) or True,
    )
    monkeypatch.setattr(updater.runtime_pid, "wait_for_exit", lambda value, timeout: True)

    updater.stop_runtime(launchagent_was_loaded=True)
    assert calls == ["bootout", (4242, updater.signal.SIGTERM)]


def test_stop_runtime_rejects_live_daemon_with_unverifiable_generation(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths.pid_file().write_text("4242")
    paths.runtime_state_file().write_text("{}")
    process = updater.runtime_pid.ProcessIdentity(
        4242,
        os.getuid(),
        1.0,
        "/tmp/persome start --foreground",
    )
    monkeypatch.setattr(updater.runtime_pid, "inspect_process", lambda _pid: process)
    monkeypatch.setattr(updater, "launchagent_is_loaded", lambda: False)

    with pytest.raises(updater.UpdateError, match="invalid or stale generation"):
        updater.stop_runtime(launchagent_was_loaded=False)


def test_installer_uses_update_mode_without_shell_interpolation(
    ac_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = updater.UpdateSource(_source_tree(tmp_path / "source"), "c" * 40, False)
    seen: dict[str, object] = {}
    monkeypatch.setenv("SSL_CERT_FILE", str(paths.root() / "venv" / "cert.pem"))

    class Process:
        def __init__(self, command: list[str], **kwargs: object) -> None:
            seen.update(command=command, kwargs=kwargs)

        def wait(self, timeout: float | None = None) -> int:
            return 0

    monkeypatch.setattr(updater.subprocess, "Popen", Process)
    monkeypatch.setattr(updater, "transaction_prepared", lambda: True)
    transaction_id = _begin_transaction()
    monkeypatch.setattr(updater, "_ACTIVE_UPDATE_LOCK_FD", 91)

    updater.run_installer(source)

    assert seen["command"] == ["/bin/bash", str(source.path / "install.sh"), "--update"]
    assert seen["kwargs"]["cwd"] == source.path  # type: ignore[index]
    assert seen["kwargs"]["start_new_session"] is True  # type: ignore[index]
    assert seen["kwargs"]["pass_fds"] == (91,)  # type: ignore[index]
    env = seen["kwargs"]["env"]  # type: ignore[index]
    assert env["PERSOME_ROOT"] == str(paths.root())
    assert env["PERSOME_INSTALL_HOME"] == str(paths.root())
    assert "SSL_CERT_FILE" not in env
    assert "PYTHONPATH" not in env
    assert env["PERSOME_UPDATE_DEFER_COMMIT"] == "1"
    assert env["PERSOME_UPDATE_REPLACEMENT"] == str(paths.root() / "venv.replacement.update")
    assert env["PERSOME_UPDATE_TRANSACTION_ID"] == transaction_id
    assert env["PERSOME_UPDATE_LOCK_FD"] == "91"


def test_installer_candidate_preparation_never_touches_active_venv(
    ac_root: Path, tmp_path: Path
) -> None:
    source_root = _source_tree(tmp_path / "source")
    (source_root / "install.sh").write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        'mkdir -p "$PERSOME_UPDATE_REPLACEMENT/bin"\n'
        'printf "%s\\n" "$PERSOME_UPDATE_TRANSACTION_ID" '
        '> "$PERSOME_UPDATE_REPLACEMENT/.persome-update-transaction"\n'
        'chmod 0600 "$PERSOME_UPDATE_REPLACEMENT/.persome-update-transaction"\n',
        encoding="utf-8",
    )
    source = updater.UpdateSource(source_root, "c" * 40, False)
    active = paths.root() / "venv"
    active.mkdir()
    (active / "old").write_text("old", encoding="utf-8")

    with updater.update_lock():
        transaction_id = _begin_transaction()
        updater.run_installer(source)

    candidate = paths.root() / "venv.replacement.update"
    assert (active / "old").read_text(encoding="utf-8") == "old"
    assert updater._marker_transaction(candidate) == transaction_id


def test_interrupted_installer_waits_for_transaction_rollback(
    ac_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = updater.UpdateSource(_source_tree(tmp_path / "source"), "c" * 40, False)
    signals: list[tuple[int, int]] = []
    binary = paths.root() / "venv" / "bin" / "persome"
    binary.parent.mkdir(parents=True)
    binary.write_text("", encoding="utf-8")
    binary.chmod(0o755)

    class Process:
        pid = 4242

        def __init__(self, command: list[str], **kwargs: object) -> None:
            self.waits = 0
            self.done = False

        def wait(self, timeout: float | None = None) -> int:
            self.waits += 1
            if self.waits == 1:
                raise KeyboardInterrupt
            assert timeout is not None and 0 < timeout <= 30
            self.done = True
            return 130

        def poll(self) -> int | None:
            return 130 if self.done else None

    monkeypatch.setattr(updater.subprocess, "Popen", Process)
    monkeypatch.setattr(updater.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    _begin_transaction()
    monkeypatch.setattr(updater, "_ACTIVE_UPDATE_LOCK_FD", 91)

    with pytest.raises(updater.UpdateCancelled, match="previous installation remains active"):
        updater.run_installer(source)

    assert signals == [(4242, updater.signal.SIGINT)]


def test_second_interrupt_does_not_kill_transaction_rollback(
    ac_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = updater.UpdateSource(_source_tree(tmp_path / "source"), "c" * 40, False)
    binary = paths.root() / "venv" / "bin" / "persome"
    binary.parent.mkdir(parents=True)
    binary.write_text("", encoding="utf-8")
    binary.chmod(0o755)
    signals: list[tuple[int, int]] = []

    class Process:
        pid = 4242

        def __init__(self, command: list[str], **kwargs: object) -> None:
            self.waits = 0
            self.done = False

        def wait(self, timeout: float | None = None) -> int:
            self.waits += 1
            if self.waits <= 2:
                raise KeyboardInterrupt
            self.done = True
            return 130

        def poll(self) -> int | None:
            return 130 if self.done else None

    monkeypatch.setattr(updater.subprocess, "Popen", Process)
    monkeypatch.setattr(updater.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    _begin_transaction()
    monkeypatch.setattr(updater, "_ACTIVE_UPDATE_LOCK_FD", 91)

    with pytest.raises(updater.UpdateCancelled, match="previous installation remains active"):
        updater.run_installer(source)

    assert signals == [(4242, updater.signal.SIGINT)]


def test_interrupt_after_installer_exits_zero_still_cancels(
    ac_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = updater.UpdateSource(_source_tree(tmp_path / "source"), "c" * 40, False)

    class Process:
        pid = 4242

        def __init__(self, command: list[str], **kwargs: object) -> None:
            pass

        def wait(self, timeout: float | None = None) -> int:
            raise KeyboardInterrupt

        def poll(self) -> int:
            return 0

    monkeypatch.setattr(updater.subprocess, "Popen", Process)
    _begin_transaction()
    monkeypatch.setattr(updater, "_ACTIVE_UPDATE_LOCK_FD", 91)

    with pytest.raises(updater.UpdateCancelled) as raised:
        updater.run_installer(source)

    assert raised.value.exit_code == 130
    assert not (paths.root() / "venv").exists()


def test_failed_update_recovers_background_runtime(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = paths.root() / "venv" / "bin" / "persome"
    binary.parent.mkdir(parents=True)
    binary.write_text("", encoding="utf-8")
    binary.chmod(0o755)
    calls: list[object] = []
    monkeypatch.setattr(updater, "_running_daemon_pid", lambda: None)
    monkeypatch.setattr(updater, "_start_background_runtime", lambda: calls.append("start"))
    monkeypatch.setattr(updater, "_wait_for_legacy_runtime_proof", lambda: calls.append("proof"))

    updater.recover_runtime(False)

    assert calls == ["start", "proof"]


def test_launchagent_restore_uses_new_binary_and_waits_for_running_state(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = paths.root() / "venv" / "bin" / "persome"
    binary.parent.mkdir(parents=True)
    binary.write_text("", encoding="utf-8")
    binary.chmod(0o755)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        updater.subprocess,
        "run",
        lambda command, **kwargs: (
            calls.append(command) or subprocess.CompletedProcess(command, 0, "", "")
        ),
    )
    monkeypatch.setattr(updater, "launchagent_is_loaded", lambda: True)
    monkeypatch.setattr(updater.launchagent, "owns_recorded_runtime", lambda binary: True)

    updater.restore_launchagent(True)

    assert calls == [
        [str(binary), "launchagent", "install", "--binary", str(binary)],
    ]


def test_cli_update_runs_download_stop_install_and_restore(
    ac_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = updater.UpdateSource(_source_tree(tmp_path / "source"), "d" * 40, True)
    calls: list[object] = []

    @contextlib.contextmanager
    def fake_acquire(path: Path | None = None):
        calls.append(("acquire", path))
        yield source

    monkeypatch.setattr(updater, "acquire_source", fake_acquire)
    monkeypatch.setattr(updater, "recover_pending_update", lambda: calls.append("recover-pending"))
    monkeypatch.setattr(updater, "ensure_no_pending_update", lambda: calls.append("ensure-clean"))
    monkeypatch.setattr(updater, "launchagent_should_be_restored", lambda: True)
    monkeypatch.setattr(
        updater,
        "begin_update_transaction",
        lambda loaded: calls.append(("begin", loaded)),
    )
    monkeypatch.setattr(
        updater,
        "stop_runtime",
        lambda *, launchagent_was_loaded, force=False: calls.append(
            ("stop", launchagent_was_loaded, force)
        ),
    )
    monkeypatch.setattr(updater, "run_installer", lambda value: calls.append(("install", value)))
    monkeypatch.setattr(
        updater,
        "mark_update_phase",
        lambda loaded, phase: calls.append(("phase", loaded, phase)),
    )
    monkeypatch.setattr(
        updater, "activate_runtime", lambda loaded: calls.append(("activate", loaded))
    )
    monkeypatch.setattr(updater, "prove_runtime", lambda loaded: calls.append(("prove", loaded)))
    cleanup = ac_root / "committed"
    monkeypatch.setattr(
        updater,
        "commit_prepared_install",
        lambda: calls.append("commit") or cleanup,
    )
    monkeypatch.setattr(updater, "clear_update_state", lambda: calls.append("clear"))
    monkeypatch.setattr(
        updater,
        "cleanup_committed_install",
        lambda path: calls.append(("cleanup", path)),
    )

    result = CliRunner().invoke(cli.app, ["update"])

    assert result.exit_code == 0, result.output
    assert calls == [
        "recover-pending",
        "ensure-clean",
        ("acquire", None),
        ("begin", True),
        ("stop", True, False),
        ("install", source),
        ("phase", True, "prepared"),
        ("activate", True),
        ("prove", True),
        ("phase", True, "committing"),
        "commit",
        "clear",
        ("cleanup", cleanup),
    ]
    assert "Persome update complete" in result.output
    assert "personal data were" in result.output
    assert "preserved" in result.output
    assert "Checking for an interrupted update" in result.output
    assert "Downloading the latest official main revision" in result.output


def test_cli_download_failure_does_not_change_runtime(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    @contextlib.contextmanager
    def failed_acquire(path: Path | None = None):
        raise updater.UpdateError("offline")
        yield  # pragma: no cover

    monkeypatch.setattr(updater, "acquire_source", failed_acquire)
    monkeypatch.setattr(updater, "recover_pending_update", lambda: None)
    monkeypatch.setattr(updater, "ensure_no_pending_update", lambda: None)
    monkeypatch.setattr(updater, "stop_runtime", lambda **kwargs: calls.append("stop"))

    result = CliRunner().invoke(cli.app, ["update"])

    assert result.exit_code == 1
    assert "offline" in result.output
    assert calls == []


def test_cli_cancel_before_transaction_reports_runtime_unchanged(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    @contextlib.contextmanager
    def cancelled_acquire(path: Path | None = None):
        raise updater.UpdateSignal(updater.signal.SIGINT)
        yield  # pragma: no cover

    monkeypatch.setattr(updater, "acquire_source", cancelled_acquire)
    monkeypatch.setattr(updater, "recover_pending_update", lambda: None)
    monkeypatch.setattr(updater, "ensure_no_pending_update", lambda: None)

    result = CliRunner().invoke(cli.app, ["update"])

    assert result.exit_code == 130
    assert "before the Runtime was changed" in result.output
    assert "was restored" not in result.output


def test_pending_recovery_ignores_repeated_terminal_interrupt(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def recover() -> None:
        calls.append("recover-start")
        os.kill(os.getpid(), updater.signal.SIGINT)
        calls.append("recover-finished")

    monkeypatch.setattr(updater, "recover_pending_update", recover)
    monkeypatch.setattr(
        updater,
        "ensure_no_pending_update",
        lambda: (_ for _ in ()).throw(updater.UpdateError("stop after recovery")),
    )

    result = CliRunner().invoke(cli.app, ["update"])

    assert result.exit_code == 1
    assert calls == ["recover-start", "recover-finished"]
    assert "stop after recovery" in result.output


def test_cli_install_failure_attempts_runtime_recovery(
    ac_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = updater.UpdateSource(_source_tree(tmp_path / "source"), "e" * 40, True)
    calls: list[object] = []

    @contextlib.contextmanager
    def fake_acquire(path: Path | None = None):
        yield source

    monkeypatch.setattr(updater, "acquire_source", fake_acquire)
    monkeypatch.setattr(updater, "recover_pending_update", lambda: None)
    monkeypatch.setattr(updater, "ensure_no_pending_update", lambda: None)
    monkeypatch.setattr(updater, "launchagent_should_be_restored", lambda: True)
    monkeypatch.setattr(updater, "begin_update_transaction", lambda loaded: None)
    monkeypatch.setattr(updater, "stop_runtime", lambda **kwargs: calls.append("stop"))
    monkeypatch.setattr(
        updater,
        "run_installer",
        lambda value: (_ for _ in ()).throw(updater.UpdateError("install failed")),
    )
    monkeypatch.setattr(
        updater,
        "rollback_and_recover",
        lambda loaded: calls.append(("rollback", loaded)),
    )

    result = CliRunner().invoke(cli.app, ["update"])

    assert result.exit_code == 1
    assert "install failed" in result.output
    assert calls == ["stop", ("rollback", True)]


def test_cli_interrupt_during_stop_recovers_runtime(
    ac_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = updater.UpdateSource(_source_tree(tmp_path / "source"), "f" * 40, True)
    calls: list[object] = []

    @contextlib.contextmanager
    def fake_acquire(path: Path | None = None):
        yield source

    monkeypatch.setattr(updater, "acquire_source", fake_acquire)
    monkeypatch.setattr(updater, "recover_pending_update", lambda: None)
    monkeypatch.setattr(updater, "ensure_no_pending_update", lambda: None)
    monkeypatch.setattr(updater, "launchagent_should_be_restored", lambda: False)
    monkeypatch.setattr(updater, "begin_update_transaction", lambda loaded: None)
    monkeypatch.setattr(
        updater,
        "stop_runtime",
        lambda **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(
        updater,
        "rollback_and_recover",
        lambda loaded: calls.append(("rollback", loaded)),
    )

    result = CliRunner().invoke(cli.app, ["update"])

    assert result.exit_code == 130
    assert calls == [("rollback", False)]
    assert "Update cancelled" in result.output


@pytest.mark.parametrize(
    ("installer_arg", "launchagent_marker"),
    [("--no-client-config", "0"), ("--update", "0")],
)
def test_existing_install_delegates_to_transactional_update(
    tmp_path: Path, installer_arg: str, launchagent_marker: str
) -> None:
    root = tmp_path / "existing root"
    python = root / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    record = tmp_path / "delegated.txt"
    python.write_text(
        "#!/bin/bash\n"
        'printf "%s\\n" "$@" > "$PERSOME_DELEGATION_RECORD"\n'
        'printf "%s\\n" "$PERSOME_ROOT" "$PYTHONPATH" >> "$PERSOME_DELEGATION_RECORD"\n'
        'printf "marker=%s\\n" "${PERSOME_UPDATE_INFER_LAUNCHAGENT_FROM_PLIST:-0}" '
        '>> "$PERSOME_DELEGATION_RECORD"\n',
        encoding="utf-8",
    )
    python.chmod(0o755)
    repo = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "PERSOME_INSTALL_HOME": str(root),
        "PERSOME_DELEGATION_RECORD": str(record),
    }

    result = subprocess.run(
        ["/bin/bash", str(repo / "install.sh"), installer_arg],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    assert "bootstrapping the current source-tree updater" in result.stdout
    assert record.read_text(encoding="utf-8").splitlines() == [
        "-m",
        "persome",
        "update",
        "--source",
        str(repo),
        str(root),
        str(repo / "src"),
        f"marker={launchagent_marker}",
    ]


def test_previous_updater_parent_enables_legacy_handoff_only_for_compatibility(
    tmp_path: Path,
) -> None:
    root = tmp_path / "existing"
    python = root / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    record = tmp_path / "delegated.txt"
    python.write_text(
        "#!/bin/bash\n"
        'printf "%s" "${PERSOME_UPDATE_INFER_LAUNCHAGENT_FROM_PLIST:-0}" '
        '> "$PERSOME_DELEGATION_RECORD"\n',
        encoding="utf-8",
    )
    python.chmod(0o755)
    wrapper = tmp_path / "persome"
    wrapper.write_text('#!/bin/bash\n/bin/bash "$2" --update\n', encoding="utf-8")
    wrapper.chmod(0o755)
    repo = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [str(wrapper), "update", str(repo / "install.sh")],
        cwd=repo,
        env={
            **os.environ,
            "PERSOME_INSTALL_HOME": str(root),
            "PERSOME_DELEGATION_RECORD": str(record),
        },
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    assert record.read_text(encoding="utf-8") == "1"


@pytest.mark.parametrize(("signal_name", "returncode"), [("INT", 130), ("TERM", 143), ("HUP", 129)])
def test_update_signal_restores_old_venv_with_real_bash(
    tmp_path: Path, signal_name: str, returncode: int
) -> None:
    install_script = (Path(__file__).resolve().parents[1] / "install.sh").read_text(
        encoding="utf-8"
    )
    transaction_code = install_script.split("# The fallback bootstrap downloads", maxsplit=1)[0]
    root = tmp_path / signal_name.lower()
    active = root / "venv"
    backup = root / "venv.previous.test"
    active.mkdir(parents=True)
    backup.mkdir(parents=True)
    (active / "new-marker").write_text("new", encoding="utf-8")
    (backup / "old-marker").write_text("old", encoding="utf-8")
    env_file = root / "env"
    env_file.write_text("PERSOME_LLM_API_KEY=preserved\n", encoding="utf-8")
    harness = (
        transaction_code
        + "\n"
        + 'INSTALL_HOME="$1"\n'
        + 'VENV_DIR="${INSTALL_HOME}/venv"\n'
        + 'OLD_VENV_BACKUP="${INSTALL_HOME}/venv.previous.test"\n'
        + "INSTALL_TRANSACTION_ACTIVE=1\n"
        + "UPDATE_MODE=0\n"
        + f"kill -{signal_name} $$\n"
    )

    result = subprocess.run(
        ["/bin/bash", "-c", harness, "rollback-test", str(root)],
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == returncode
    assert (active / "old-marker").read_text(encoding="utf-8") == "old"
    assert not (active / "new-marker").exists()
    assert not backup.exists()
    assert env_file.read_text(encoding="utf-8") == "PERSOME_LLM_API_KEY=preserved\n"


def test_update_mode_skips_setup_prompts_but_keeps_runtime_proof() -> None:
    script = (Path(__file__).resolve().parents[1] / "install.sh").read_text(encoding="utf-8")

    assert "--update" in script
    assert "UPDATE_MODE=1" in script
    assert "update mode: preserving the existing LLM profile and credentials" in script
    assert "PERSOME_UPDATE_DEFER_COMMIT" in script
    assert "PERSOME_UPDATE_TRANSACTION_ID" in script
    assert "PERSOME_UPDATE_LOCK_FD" in script
    assert "venv.replacement.update" in script
    assert "final Runtime proof and commit are owned by persome update" in script
    assert script.index("defer_install_commit\n") < script.index("run_onboarding\n")
    assert "restoring the previous virtualenv" in script
    assert "discarding the inactive update candidate" in script
    assert script.index("defer_install_commit\n") < script.index("install_shim\n")
    assert "trap - EXIT INT TERM HUP" in script


@pytest.mark.parametrize(
    "missing_variable",
    ["PERSOME_UPDATE_TRANSACTION_ID", "PERSOME_UPDATE_LOCK_FD"],
)
def test_internal_deferred_install_flags_cannot_bypass_transaction_validation(
    tmp_path: Path, missing_variable: str
) -> None:
    root = tmp_path / "root"
    python = root / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    state = root / ".update-state.json"
    transaction_id = "a" * 32
    state.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "launchagent_was_loaded": False,
                "phase": "preparing",
                "transaction_id": transaction_id,
            }
        ),
        encoding="utf-8",
    )
    state.chmod(0o600)
    lock = root / ".update.lock"
    lock.touch(mode=0o600)
    descriptor = os.open(lock, os.O_RDWR)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    env = {
        **os.environ,
        "PERSOME_INSTALL_HOME": str(root),
        "PERSOME_UPDATE_DEFER_COMMIT": "1",
        "PERSOME_UPDATE_REPLACEMENT": str(root / "venv.replacement.update"),
        "PERSOME_UPDATE_TRANSACTION_ID": transaction_id,
        "PERSOME_UPDATE_LOCK_FD": str(descriptor),
    }
    env.pop(missing_variable)
    repo = Path(__file__).resolve().parents[1]
    try:
        result = subprocess.run(
            ["/bin/bash", str(repo / "install.sh"), "--update"],
            cwd=repo,
            env=env,
            pass_fds=(descriptor,),
            capture_output=True,
            text=True,
            timeout=15,
        )
    finally:
        os.close(descriptor)

    assert result.returncode == 1
    assert "deferred update" in result.stderr


def test_internal_deferred_install_rejects_mismatched_state_nonce(tmp_path: Path) -> None:
    root = tmp_path / "root"
    python = root / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    state = root / ".update-state.json"
    state.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "launchagent_was_loaded": False,
                "phase": "preparing",
                "transaction_id": "a" * 32,
            }
        ),
        encoding="utf-8",
    )
    state.chmod(0o600)
    lock = root / ".update.lock"
    lock.touch(mode=0o600)
    descriptor = os.open(lock, os.O_RDWR)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    repo = Path(__file__).resolve().parents[1]
    try:
        result = subprocess.run(
            ["/bin/bash", str(repo / "install.sh"), "--update"],
            cwd=repo,
            env={
                **os.environ,
                "PERSOME_INSTALL_HOME": str(root),
                "PERSOME_UPDATE_DEFER_COMMIT": "1",
                "PERSOME_UPDATE_REPLACEMENT": str(root / "venv.replacement.update"),
                "PERSOME_UPDATE_TRANSACTION_ID": "b" * 32,
                "PERSOME_UPDATE_LOCK_FD": str(descriptor),
            },
            pass_fds=(descriptor,),
            capture_output=True,
            text=True,
            timeout=15,
        )
    finally:
        os.close(descriptor)

    assert result.returncode == 1
    assert "transaction validation failed" in result.stderr


def test_internal_deferred_install_rejects_an_unlocked_descriptor(tmp_path: Path) -> None:
    root = tmp_path / "root"
    python = root / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    transaction_id = "a" * 32
    state = root / ".update-state.json"
    state.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "launchagent_was_loaded": False,
                "phase": "preparing",
                "transaction_id": transaction_id,
            }
        ),
        encoding="utf-8",
    )
    state.chmod(0o600)
    lock = root / ".update.lock"
    lock.touch(mode=0o600)
    descriptor = os.open(lock, os.O_RDWR)
    repo = Path(__file__).resolve().parents[1]
    try:
        result = subprocess.run(
            ["/bin/bash", str(repo / "install.sh"), "--update"],
            cwd=repo,
            env={
                **os.environ,
                "PERSOME_INSTALL_HOME": str(root),
                "PERSOME_UPDATE_DEFER_COMMIT": "1",
                "PERSOME_UPDATE_REPLACEMENT": str(root / "venv.replacement.update"),
                "PERSOME_UPDATE_TRANSACTION_ID": transaction_id,
                "PERSOME_UPDATE_LOCK_FD": str(descriptor),
            },
            pass_fds=(descriptor,),
            capture_output=True,
            text=True,
            timeout=15,
        )
    finally:
        os.close(descriptor)

    assert result.returncode == 1
    assert "transaction validation failed" in result.stderr


def test_internal_deferred_install_accepts_matching_nonce_and_inherited_lock(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    python = root / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    transaction_id = "a" * 32
    state = root / ".update-state.json"
    state.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "launchagent_was_loaded": False,
                "phase": "preparing",
                "transaction_id": transaction_id,
            }
        ),
        encoding="utf-8",
    )
    state.chmod(0o600)
    lock = root / ".update.lock"
    lock.touch(mode=0o600)
    descriptor = os.open(lock, os.O_RDWR)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    uname = fake_bin / "uname"
    uname.write_text("#!/bin/bash\nprintf 'Linux\\n'\n", encoding="utf-8")
    uname.chmod(0o755)
    repo = Path(__file__).resolve().parents[1]
    try:
        result = subprocess.run(
            ["/bin/bash", str(repo / "install.sh"), "--update"],
            cwd=repo,
            env={
                **os.environ,
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                "PERSOME_INSTALL_HOME": str(root),
                "PERSOME_UPDATE_DEFER_COMMIT": "1",
                "PERSOME_UPDATE_REPLACEMENT": str(root / "venv.replacement.update"),
                "PERSOME_UPDATE_TRANSACTION_ID": transaction_id,
                "PERSOME_UPDATE_LOCK_FD": str(descriptor),
            },
            pass_fds=(descriptor,),
            capture_output=True,
            text=True,
            timeout=15,
        )
    finally:
        os.close(descriptor)

    assert result.returncode == 1
    assert "supports macOS only" in result.stderr
    assert "transaction validation failed" not in result.stderr


def test_update_lock_rejects_a_concurrent_updater(ac_root: Path) -> None:
    with (
        updater.update_lock(),
        pytest.raises(updater.UpdateError, match="already in progress"),
        updater.update_lock(),
    ):
        pass


def test_final_proof_executes_installed_binary_with_clean_environment(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = paths.root() / "venv" / "bin" / "persome"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/bash\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("PYTHONPATH", "/tmp/source-tree")
    monkeypatch.setenv("SSL_CERT_FILE", "/tmp/old-venv/cert.pem")
    seen: dict[str, object] = {}

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.update(command=command, kwargs=kwargs)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(updater.subprocess, "run", run)

    updater.prove_runtime(True)

    assert seen["command"] == [
        str(binary),
        "onboard",
        "--preserve-policy",
        "--expect-owner",
        "launchagent",
    ]
    env = seen["kwargs"]["env"]  # type: ignore[index]
    assert "PYTHONPATH" not in env
    assert "SSL_CERT_FILE" not in env


def test_legacy_updater_handoff_recovers_launchagent_ownership(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(updater, "launchagent_is_loaded", lambda: False)
    monkeypatch.setattr(updater.launchagent, "owner_intended", lambda: False)
    monkeypatch.setattr(
        updater.launchagent,
        "configured_runtime_binary",
        lambda: "/tmp/example/persome",
    )
    monkeypatch.setenv("PERSOME_UPDATE_INFER_LAUNCHAGENT_FROM_PLIST", "1")

    assert updater.launchagent_should_be_restored() is True

    monkeypatch.delenv("PERSOME_UPDATE_INFER_LAUNCHAGENT_FROM_PLIST")
    assert updater.launchagent_should_be_restored() is False


def test_legacy_handoff_accepts_the_normal_executable_shim_plist(
    ac_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "bin" / "persome"
    binary.parent.mkdir()
    binary.write_text("#!/bin/bash\n", encoding="utf-8")
    binary.chmod(0o755)
    plist = tmp_path / "com.persome.runtime.plist"
    monkeypatch.setattr(updater.launchagent, "plist_path", lambda: plist)
    updater.launchagent.write_plist(str(binary))
    monkeypatch.setattr(updater, "launchagent_is_loaded", lambda: False)
    monkeypatch.setattr(updater.launchagent, "owner_intended", lambda: False)
    monkeypatch.setenv("PERSOME_UPDATE_INFER_LAUNCHAGENT_FROM_PLIST", "1")

    assert updater.launchagent_should_be_restored() is True


def test_legacy_foreground_handoff_does_not_attempt_impossible_process_group_move(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PERSOME_UPDATE_INFER_LAUNCHAGENT_FROM_PLIST", "1")
    monkeypatch.setattr(
        updater.os,
        "setpgid",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not move process group")),
    )

    assert updater.claim_legacy_foreground() is False


def test_legacy_lock_keeper_tracks_parent_lifetime_instead_of_fixed_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    class Process:
        def __init__(self, command: list[str], **kwargs: object) -> None:
            seen.update(command=command, kwargs=kwargs)

    monkeypatch.setattr(updater.subprocess, "Popen", Process)

    updater._spawn_legacy_lock_keeper(17, 4242)

    command = seen["command"]
    assert command[0] == sys.executable
    assert command[1] == "-c"
    assert "os.kill(parent, 0)" in command[2]
    assert command[3] == "4242"
    assert seen["kwargs"]["pass_fds"] == (17,)  # type: ignore[index]


def test_rollback_prepared_install_atomically_restores_old_venv(ac_root: Path) -> None:
    active = paths.root() / "venv"
    candidate = paths.root() / "venv.replacement.update"
    active.mkdir()
    _begin_transaction()
    candidate.mkdir()
    (active / "old").write_text("old", encoding="utf-8")
    (candidate / "partial-new").write_text("new", encoding="utf-8")
    assert updater.rollback_prepared_install() is True

    assert (active / "old").read_text(encoding="utf-8") == "old"
    assert not (active / "partial-new").exists()
    assert not candidate.exists()
    assert not list(paths.root().glob("venv.failed.update.*"))


def test_activation_uses_atomic_exchange_and_rollback_infers_it_from_marker(
    ac_root: Path,
) -> None:
    active = paths.root() / "venv"
    candidate = paths.root() / "venv.replacement.update"
    active.mkdir()
    (active / "old").write_text("old", encoding="utf-8")
    transaction_id = _begin_transaction()
    _write_candidate_marker(candidate, transaction_id)
    (candidate / "new").write_text("new", encoding="utf-8")
    updater.mark_update_phase(False, "prepared")

    updater.activate_prepared_install()

    assert (active / "new").read_text(encoding="utf-8") == "new"
    assert (candidate / "old").read_text(encoding="utf-8") == "old"
    assert updater._read_update_state().phase == "activated"  # type: ignore[union-attr]

    assert updater.rollback_prepared_install() is True
    assert (active / "old").read_text(encoding="utf-8") == "old"
    assert not (active / "new").exists()
    assert not candidate.exists()


def test_committed_candidate_console_script_survives_directory_exchange(
    ac_root: Path,
) -> None:
    active = paths.root() / "venv"
    candidate = paths.root() / "venv.replacement.update"
    active.mkdir()
    (active / "old").write_text("old", encoding="utf-8")
    transaction_id = _begin_transaction()
    _write_candidate_marker(candidate, transaction_id)
    updater.mark_update_phase(False, "prepared")

    updater.activate_prepared_install()
    updater.mark_update_phase(False, "committing")
    cleanup = updater.commit_prepared_install()
    updater.clear_update_state()
    updater.cleanup_committed_install(cleanup)

    result = subprocess.run(
        [str(active / "bin" / "persome")],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout == "candidate runtime\n"
    assert not candidate.exists()


def test_activation_rejects_candidate_with_absolute_inactive_shebang(
    ac_root: Path,
) -> None:
    active = paths.root() / "venv"
    candidate = paths.root() / "venv.replacement.update"
    active.mkdir()
    (active / "old").write_text("old", encoding="utf-8")
    transaction_id = _begin_transaction()
    _write_candidate_marker(candidate, transaction_id)
    executable = candidate / "bin" / "persome"
    executable.write_text(
        f"#!{candidate}/bin/python\nprint('wrong interpreter')\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    updater.mark_update_phase(False, "prepared")

    with pytest.raises(updater.UpdateError, match="not relocatable"):
        updater.activate_prepared_install()

    assert (active / "old").is_file()
    assert candidate.is_dir()


def test_crash_after_exchange_before_phase_write_is_rollbackable_from_marker(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = paths.root() / "venv"
    candidate = paths.root() / "venv.replacement.update"
    active.mkdir()
    (active / "old").write_text("old", encoding="utf-8")
    transaction_id = _begin_transaction()
    _write_candidate_marker(candidate, transaction_id)
    (candidate / "new").write_text("new", encoding="utf-8")
    updater.mark_update_phase(False, "prepared")
    original_write = updater._write_update_state
    monkeypatch.setattr(
        updater,
        "_write_update_state",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("simulated crash")),
    )

    with pytest.raises(RuntimeError, match="simulated crash"):
        updater.activate_prepared_install()

    monkeypatch.setattr(updater, "_write_update_state", original_write)
    assert updater._read_update_state().phase == "prepared"  # type: ignore[union-attr]
    assert updater._marker_transaction(active) == transaction_id
    assert updater.rollback_prepared_install() is True
    assert (active / "old").exists()


def test_commit_never_creates_a_missing_active_venv_window(ac_root: Path) -> None:
    active = paths.root() / "venv"
    candidate = paths.root() / "venv.replacement.update"
    active.mkdir()
    (active / "old").write_text("old", encoding="utf-8")
    transaction_id = _begin_transaction()
    _write_candidate_marker(candidate, transaction_id)
    (candidate / "new").write_text("new", encoding="utf-8")
    updater.mark_update_phase(False, "prepared")
    updater.activate_prepared_install()
    updater.mark_update_phase(False, "committing")

    cleanup = updater.commit_prepared_install()

    assert (active / "new").exists()
    assert cleanup.is_dir()
    assert (cleanup / "old").exists()
    assert not candidate.exists()
    updater.clear_update_state()
    assert not (active / ".persome-update-transaction").exists()


def test_committed_interruption_reproves_final_runtime_before_clearing_state(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = paths.root() / "venv"
    transaction_id = "a" * 32
    _write_candidate_marker(active, transaction_id)
    updater._write_update_state(
        launchagent_was_loaded=False,
        phase="committing",
        transaction_id=transaction_id,
    )
    calls: list[str] = []
    monkeypatch.setattr(updater, "launchagent_is_loaded", lambda: False)
    monkeypatch.setattr(updater, "_running_daemon_pid", lambda: 4242)
    monkeypatch.setattr(updater, "_wait_for_runtime_process", lambda: calls.append("process"))
    monkeypatch.setattr(updater, "prove_runtime", lambda loaded: calls.append(f"proof:{loaded}"))
    monkeypatch.setattr(updater, "cleanup_transaction_artifacts", lambda: calls.append("cleanup"))

    updater.recover_pending_update()

    assert calls == ["process", "proof:False", "cleanup"]
    assert not updater._update_state_file().exists()


def test_legacy_two_rename_transaction_is_recovered_with_atomic_exchange(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = paths.root() / "venv"
    backup = paths.root() / "venv.previous.update"
    active.mkdir()
    backup.mkdir()
    (active / "new").write_text("new", encoding="utf-8")
    (backup / "old").write_text("old", encoding="utf-8")
    paths.atomic_write_private_text(
        updater._update_state_file(),
        json.dumps(
            {
                "schema_version": 1,
                "launchagent_was_loaded": False,
                "phase": "prepared",
            }
        ),
    )
    calls: list[str] = []
    monkeypatch.setattr(updater, "launchagent_is_loaded", lambda: False)
    monkeypatch.setattr(updater, "stop_runtime", lambda **_kwargs: calls.append("stop"))
    monkeypatch.setattr(updater, "_clear_replacement_runtime_state", lambda: None)
    monkeypatch.setattr(
        updater, "recover_runtime", lambda loaded: calls.append(f"recover:{loaded}")
    )

    updater.recover_pending_update()

    assert (active / "old").exists()
    assert not (active / "new").exists()
    assert not backup.exists()
    assert calls == ["stop", "recover:False"]
    assert not updater._update_state_file().exists()


def test_legacy_crash_during_missing_active_window_restores_backup(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = paths.root() / "venv.previous.update"
    backup.mkdir()
    (backup / "old").write_text("old", encoding="utf-8")
    paths.atomic_write_private_text(
        updater._update_state_file(),
        json.dumps(
            {
                "schema_version": 1,
                "launchagent_was_loaded": True,
                "phase": "preparing",
            }
        ),
    )
    monkeypatch.setattr(updater, "launchagent_is_loaded", lambda: False)
    monkeypatch.setattr(updater, "stop_runtime", lambda **_kwargs: None)
    monkeypatch.setattr(updater, "_clear_replacement_runtime_state", lambda: None)
    monkeypatch.setattr(updater, "recover_runtime", lambda _loaded: None)

    updater.recover_pending_update()

    assert (paths.root() / "venv" / "old").exists()
    assert not backup.exists()
    assert not updater._update_state_file().exists()


@pytest.mark.parametrize(
    "signum", [updater.signal.SIGINT, updater.signal.SIGTERM, updater.signal.SIGHUP]
)
def test_outer_update_signals_become_recoverable_cancellation(
    signum: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed: dict[int, object] = {}

    def install_handler(signal_number: int, handler: object) -> None:
        installed[signal_number] = handler

    monkeypatch.setattr(updater.signal, "getsignal", lambda _signum: updater.signal.SIG_DFL)
    monkeypatch.setattr(updater.signal, "signal", install_handler)
    with pytest.raises(updater.UpdateSignal) as raised, updater.catch_update_signals():
        handler = installed[signum]
        assert callable(handler)
        handler(signum, None)

    assert raised.value.signum == signum
    assert raised.value.exit_code == 128 + signum

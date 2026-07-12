"""Safe Runtime self-update orchestration."""

from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from persome import cli, launchagent, paths, updater


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
    pids = iter([4242, 4242, None])
    signals: list[tuple[int, int]] = []
    bootout = subprocess.CompletedProcess(["launchctl"], 0, "", "")
    monkeypatch.setattr(launchagent, "is_loaded", lambda: True)
    monkeypatch.setattr(launchagent, "bootout", lambda: bootout)
    monkeypatch.setattr(updater, "_running_daemon_pid", lambda: next(pids))
    monkeypatch.setattr(updater.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(updater.time, "sleep", lambda seconds: None)

    updater.stop_runtime(launchagent_was_loaded=True)
    assert signals == [(4242, updater.signal.SIGTERM)]


def test_installer_uses_update_mode_without_shell_interpolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = updater.UpdateSource(_source_tree(tmp_path / "source"), "c" * 40, False)
    seen: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.update(command=command, kwargs=kwargs)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(updater.subprocess, "run", fake_run)

    updater.run_installer(source)

    assert seen["command"] == ["/bin/bash", str(source.path / "install.sh"), "--update"]
    assert seen["kwargs"]["cwd"] == source.path  # type: ignore[index]
    assert seen["kwargs"]["check"] is False  # type: ignore[index]
    env = seen["kwargs"]["env"]  # type: ignore[index]
    assert env["PERSOME_ROOT"] == str(paths.root())
    assert env["PERSOME_INSTALL_HOME"] == str(paths.root())


def test_failed_update_recovers_background_runtime(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = paths.root() / "venv" / "bin" / "persome"
    binary.parent.mkdir(parents=True)
    binary.write_text("", encoding="utf-8")
    binary.chmod(0o755)
    calls: list[list[str]] = []
    monkeypatch.setattr(updater, "_running_daemon_pid", lambda: None)
    monkeypatch.setattr(
        updater.subprocess,
        "run",
        lambda command, **kwargs: (
            calls.append(command) or subprocess.CompletedProcess(command, 0, "", "")
        ),
    )

    updater.recover_runtime(False)

    assert calls == [[str(binary), "start"]]


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
    monkeypatch.setattr(updater, "_running_daemon_pid", lambda: 4242)

    updater.restore_launchagent(True)

    assert calls == [
        [str(binary), "launchagent", "install", "--binary", str(binary)],
    ]


def test_cli_update_runs_download_stop_install_and_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = updater.UpdateSource(_source_tree(tmp_path / "source"), "d" * 40, True)
    calls: list[object] = []

    @contextlib.contextmanager
    def fake_acquire(path: Path | None = None):
        calls.append(("acquire", path))
        yield source

    monkeypatch.setattr(updater, "acquire_source", fake_acquire)
    monkeypatch.setattr(updater, "launchagent_is_loaded", lambda: True)
    monkeypatch.setattr(
        updater,
        "stop_runtime",
        lambda launchagent_was_loaded: calls.append(("stop", launchagent_was_loaded)),
    )
    monkeypatch.setattr(updater, "run_installer", lambda value: calls.append(("install", value)))
    monkeypatch.setattr(
        updater, "restore_launchagent", lambda value: calls.append(("restore", value))
    )

    result = CliRunner().invoke(cli.app, ["update"])

    assert result.exit_code == 0, result.output
    assert calls == [
        ("acquire", None),
        ("stop", True),
        ("install", source),
        ("restore", True),
    ]
    assert "Persome update complete" in result.output
    assert "personal data were" in result.output
    assert "preserved" in result.output


def test_cli_download_failure_does_not_change_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    @contextlib.contextmanager
    def failed_acquire(path: Path | None = None):
        raise updater.UpdateError("offline")
        yield  # pragma: no cover

    monkeypatch.setattr(updater, "acquire_source", failed_acquire)
    monkeypatch.setattr(updater, "stop_runtime", lambda **kwargs: calls.append("stop"))
    monkeypatch.setattr(updater, "recover_runtime", lambda was_loaded: calls.append("recover"))

    result = CliRunner().invoke(cli.app, ["update"])

    assert result.exit_code == 1
    assert "offline" in result.output
    assert calls == []


def test_cli_install_failure_attempts_runtime_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = updater.UpdateSource(_source_tree(tmp_path / "source"), "e" * 40, True)
    calls: list[object] = []

    @contextlib.contextmanager
    def fake_acquire(path: Path | None = None):
        yield source

    monkeypatch.setattr(updater, "acquire_source", fake_acquire)
    monkeypatch.setattr(updater, "launchagent_is_loaded", lambda: True)
    monkeypatch.setattr(updater, "stop_runtime", lambda **kwargs: calls.append("stop"))
    monkeypatch.setattr(
        updater,
        "run_installer",
        lambda value: (_ for _ in ()).throw(updater.UpdateError("install failed")),
    )
    monkeypatch.setattr(
        updater, "recover_runtime", lambda was_loaded: calls.append(("recover", was_loaded))
    )

    result = CliRunner().invoke(cli.app, ["update"])

    assert result.exit_code == 1
    assert "install failed" in result.output
    assert calls == ["stop", ("recover", True)]


def test_update_mode_skips_setup_prompts_but_keeps_runtime_proof() -> None:
    script = (Path(__file__).resolve().parents[1] / "install.sh").read_text(encoding="utf-8")

    assert "--update" in script
    assert "UPDATE_MODE=1" in script
    assert "update mode: preserving the existing LLM profile and credentials" in script
    assert "non-interactive update: verifying existing permissions and Runtime health" in script
    assert "onboard --tier tiny --no-gui" in script
    assert script.index("run_onboarding\n") < script.rindex("commit_install\n")
    assert "restoring the previous virtualenv" in script

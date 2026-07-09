"""Tests for the macOS LaunchAgent integration (issue #194).

These exercise plist content and the launchctl wrappers without touching the
real ``~/Library/LaunchAgents`` directory or invoking ``launchctl`` — the plist
path and ``subprocess.run`` are redirected/monkeypatched.
"""

from __future__ import annotations

import plistlib
import signal
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from persome import cli, launchagent, paths


def test_label_matches_dart_contract() -> None:
    # The Dart side hardcodes this same string in embedded_daemon_service.dart.
    assert launchagent.LABEL == "com.persome.runtime"


def test_plist_path_is_under_launchagents() -> None:
    path = launchagent.plist_path()
    assert path.parent == Path.home() / "Library" / "LaunchAgents"
    assert path.name == "com.persome.runtime.plist"


def test_gui_domain_target_shape() -> None:
    target = launchagent.gui_domain_target()
    assert target.startswith("gui/")
    assert target.endswith(f"/{launchagent.LABEL}")


def test_build_plist_core_fields(ac_root: Path) -> None:
    binary = "/Applications/acme.app/Contents/Resources/oc/persome"
    pl = launchagent.build_plist(binary)

    assert pl["Label"] == launchagent.LABEL
    assert pl["ProgramArguments"] == [binary, "start", "--foreground"]
    assert pl["KeepAlive"] is True
    assert pl["RunAtLoad"] is True
    # Logs route under the data root so the diagnostic bundle collects them.
    assert pl["StandardOutPath"] == str(paths.launchd_stdout_log())
    assert pl["StandardErrorPath"] == str(paths.launchd_stderr_log())
    assert str(ac_root) in pl["StandardOutPath"]


def test_build_plist_propagates_root_override(ac_root: Path) -> None:
    pl = launchagent.build_plist("/bin/persome")
    env = pl["EnvironmentVariables"]
    assert isinstance(env, dict)
    assert env["PERSOME_ROOT"] == str(ac_root)


def test_log_paths_live_under_logs_dir(ac_root: Path) -> None:
    assert paths.launchd_stdout_log() == paths.logs_dir() / "launchd.out.log"
    assert paths.launchd_stderr_log() == paths.logs_dir() / "launchd.err.log"


def test_write_plist_roundtrips(
    ac_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "LaunchAgents" / "com.persome.runtime.plist"
    monkeypatch.setattr(launchagent, "plist_path", lambda: target)

    binary = "/usr/local/bin/persome"
    written = launchagent.write_plist(binary)

    assert written == target
    assert target.exists()
    with target.open("rb") as fh:
        loaded = plistlib.load(fh)
    assert loaded["ProgramArguments"][0] == binary
    assert loaded["KeepAlive"] is True


def test_install_writes_and_bootstraps(
    ac_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "LaunchAgents" / "com.persome.runtime.plist"
    monkeypatch.setattr(launchagent, "plist_path", lambda: target)
    # Pretend nothing is loaded yet, and capture launchctl invocations.
    monkeypatch.setattr(launchagent, "is_loaded", lambda: False)
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    written = launchagent.install("/usr/local/bin/persome")
    assert written == target
    assert target.exists()
    # Legacy-label sweep first, then exactly one bootstrap (no prior bootout
    # since not loaded).
    assert [c[1] for c in calls] == ["bootout"] * len(launchagent.LEGACY_LABELS) + ["bootstrap"]


def test_install_reloads_when_already_loaded(
    ac_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "LaunchAgents" / "com.persome.runtime.plist"
    monkeypatch.setattr(launchagent, "plist_path", lambda: target)
    monkeypatch.setattr(launchagent, "is_loaded", lambda: True)
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    launchagent.install("/usr/local/bin/persome")
    # Legacy sweep, bootout (stale job), then bootstrap (fresh binary path).
    verbs = [c[1] for c in calls]
    assert verbs == ["bootout"] * len(launchagent.LEGACY_LABELS) + ["bootout", "bootstrap"]


def test_uninstall_boots_out_and_removes_plist(
    ac_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "LaunchAgents" / "com.persome.runtime.plist"
    target.parent.mkdir(parents=True)
    target.write_text("stub")
    monkeypatch.setattr(launchagent, "plist_path", lambda: target)
    monkeypatch.setattr(launchagent, "is_loaded", lambda: True)
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    launchagent.uninstall()
    assert not target.exists()
    assert calls[0][1] == "bootout"


def test_kickstart_restart_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[list[str]] = []

    def fake_run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        captured.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    launchagent.kickstart(restart=True)
    assert "-k" in captured[0]
    launchagent.kickstart(restart=False)
    assert "-k" not in captured[1]


# ── CLI surface ───────────────────────────────────────────────────────────


def test_cli_install_invokes_module(ac_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    def fake_install(binary: str) -> Path:
        seen["binary"] = binary
        return Path("/tmp/x.plist")

    monkeypatch.setattr(launchagent, "install", fake_install)
    result = CliRunner().invoke(
        cli.app, ["launchagent", "install", "--binary", "/opt/oc/persome"]
    )
    assert result.exit_code == 0, result.output
    assert seen["binary"] == "/opt/oc/persome"
    assert "LaunchAgent installed" in result.output


def test_cli_status_exit_codes(ac_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launchagent, "is_loaded", lambda: True)
    monkeypatch.setattr(launchagent, "plist_path", lambda: Path("/tmp/x.plist"))
    loaded = CliRunner().invoke(cli.app, ["launchagent", "status"])
    assert loaded.exit_code == 0
    assert "yes" in loaded.output

    monkeypatch.setattr(launchagent, "is_loaded", lambda: False)
    unloaded = CliRunner().invoke(cli.app, ["launchagent", "status"])
    # status exits non-zero when not loaded (scriptable health check).
    assert unloaded.exit_code == 1


def test_cli_uninstall_invokes_module(ac_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"n": 0}
    monkeypatch.setattr(launchagent, "uninstall", lambda: called.__setitem__("n", 1))
    result = CliRunner().invoke(cli.app, ["launchagent", "uninstall"])
    assert result.exit_code == 0, result.output
    assert called["n"] == 1


# ── _terminate_stray_daemon: kill a pre-launchd orphan on takeover ──────────


def test_terminate_stray_ignores_missing_pidfile(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(launchagent.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    launchagent._terminate_stray_daemon()
    assert sent == []  # no pid file → nothing to signal


def test_terminate_stray_ignores_dead_pid(ac_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths.pid_file().parent.mkdir(parents=True, exist_ok=True)
    paths.pid_file().write_text("999999")
    probed: list[int] = []

    def fake_kill(pid: int, sig: int) -> None:
        probed.append(sig)
        raise ProcessLookupError  # already gone

    monkeypatch.setattr(launchagent.os, "kill", fake_kill)
    launchagent._terminate_stray_daemon()
    assert probed == [0]  # only the liveness probe; never reached SIGTERM


def test_terminate_stray_sigterms_live_pid(ac_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths.pid_file().parent.mkdir(parents=True, exist_ok=True)
    paths.pid_file().write_text("4242")
    sent: list[int] = []
    state = {"alive": True}

    def fake_kill(pid: int, sig: int) -> None:
        if sig == 0:  # liveness probe
            if not state["alive"]:
                raise ProcessLookupError
            return
        sent.append(sig)
        state["alive"] = False  # dies after SIGTERM

    monkeypatch.setattr(launchagent.os, "kill", fake_kill)
    monkeypatch.setattr(launchagent.time, "sleep", lambda _: None)
    launchagent._terminate_stray_daemon()
    assert sent == [signal.SIGTERM]


def test_install_terminates_stray_before_bootstrap(
    ac_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "agent.plist"
    monkeypatch.setattr(launchagent, "plist_path", lambda: target)
    monkeypatch.setattr(launchagent, "is_loaded", lambda: False)
    order: list[str] = []
    monkeypatch.setattr(launchagent, "_terminate_stray_daemon", lambda: order.append("terminate"))

    def fake_run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        order.append(args[1])  # launchctl verb
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    launchagent.install("/usr/local/bin/persome")
    # Stray daemon is killed BEFORE the fresh job is bootstrapped (the legacy
    # label sweep records its bootout verbs first).
    assert order == ["bootout"] * len(launchagent.LEGACY_LABELS) + ["terminate", "bootstrap"]

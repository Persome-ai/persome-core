"""CLI integrations must not leak the daemon bearer credential."""

from __future__ import annotations

import json
import stat
import subprocess
import sys
import webbrowser
from pathlib import Path
from types import SimpleNamespace

import httpx
from typer.testing import CliRunner

from persome import cli
from persome.env_file import LOCAL_API_TOKEN_ENV


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _http_runtime_config() -> SimpleNamespace:
    return SimpleNamespace(
        mcp=SimpleNamespace(
            auto_start=True,
            transport="streamable-http",
            host="127.0.0.1",
            port=8742,
        )
    )


def _runtime_process() -> SimpleNamespace:
    return SimpleNamespace(
        pid=4242,
        generation="generation-4242",
        runtime_started_at=1_752_300_000.0,
    )


def test_model_open_exchanges_bearer_for_one_time_browser_url(ac_root: Path, monkeypatch) -> None:
    token = "t" * 48
    monkeypatch.setenv(LOCAL_API_TOKEN_ENV, token)
    request: dict[str, object] = {}

    def fake_post(url: str, **kwargs):  # type: ignore[no-untyped-def]
        request.update(url=url, **kwargs)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "bootstrap_url": "/auth/browser-bootstrap?nonce=" + "n" * 43,
                },
            },
            request=httpx.Request("POST", url),
        )

    opened: list[str] = []
    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(webbrowser, "open", lambda url, new=0: opened.append(url) or True)

    result = CliRunner().invoke(cli.app, ["model", "open"])

    assert result.exit_code == 0, result.output
    assert request["url"] == "http://127.0.0.1:8742/auth/browser-bootstrap"
    assert request["headers"] == {"Authorization": f"Bearer {token}"}
    assert request["trust_env"] is False
    assert opened == [
        "http://127.0.0.1:8742/auth/browser-bootstrap?nonce=" + "n" * 43,
    ]
    assert token not in result.output
    assert token not in opened[0]


def test_model_open_can_target_unified_onboarding(ac_root: Path, monkeypatch) -> None:
    token = "t" * 48
    monkeypatch.setenv(LOCAL_API_TOKEN_ENV, token)
    requested: list[str] = []

    def fake_post(url: str, **_kwargs):  # type: ignore[no-untyped-def]
        requested.append(url)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "bootstrap_url": "/auth/browser-bootstrap?nonce="
                    + "n" * 43
                    + "&view=onboarding"
                },
            },
            request=httpx.Request("POST", url),
        )

    opened: list[str] = []
    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(webbrowser, "open", lambda url, new=0: opened.append(url) or True)

    result = CliRunner().invoke(cli.app, ["model", "open", "--onboarding"])

    assert result.exit_code == 0, result.output
    assert requested == ["http://127.0.0.1:8742/auth/browser-bootstrap?view=onboarding"]
    assert opened[0].endswith("&view=onboarding")
    assert "onboarding" in result.output


def test_model_open_can_schedule_detached_one_shot_reminder(ac_root: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    launched: dict[str, object] = {}

    class _ScheduledProcess:
        pid = 7331

    def fake_popen(command, **kwargs):  # type: ignore[no-untyped-def]
        launched["command"] = command
        launched.update(kwargs)
        return _ScheduledProcess()

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)

    result = CliRunner().invoke(cli.app, ["model", "open", "--after", "30"])

    assert result.exit_code == 0, result.output
    assert launched["command"] == [
        sys.executable,
        "-m",
        "persome",
        "model",
        "open",
        "--scheduled-after-seconds",
        "1800",
    ]
    assert launched["stdin"] is subprocess.DEVNULL
    assert launched["stderr"] is subprocess.STDOUT
    assert launched["close_fds"] is True
    assert launched["start_new_session"] is True
    assert launched["env"]["PERSOME_ROOT"] == str(ac_root)
    log_path = ac_root / "logs" / "model-open-reminder.log"
    assert launched["stdout"] is not subprocess.DEVNULL
    assert _mode(log_path) == 0o600
    assert "open automatically in 30 minutes" in result.output
    assert "persome model open" in result.output


def test_scheduled_model_open_waits_before_requesting_browser_capability(
    ac_root: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv(LOCAL_API_TOKEN_ENV, "t" * 48)
    delays: list[float] = []
    monkeypatch.setattr(cli.time, "sleep", delays.append)
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kwargs: httpx.Response(
            200,
            json={
                "success": True,
                "data": {"bootstrap_url": "/auth/browser-bootstrap?nonce=" + "n" * 43},
            },
            request=httpx.Request("POST", url),
        ),
    )
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url, new=0: opened.append(url) or True)

    result = CliRunner().invoke(
        cli.app,
        ["model", "open", "--scheduled-after-seconds", "2.5"],
    )

    assert result.exit_code == 0, result.output
    assert delays == [2.5]
    assert len(opened) == 1


def test_bare_model_command_opens_viewer(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    opened: list[bool] = []
    monkeypatch.setattr(cli, "model_open", lambda: opened.append(True))

    result = CliRunner().invoke(cli.app, ["model"])

    assert result.exit_code == 0, result.output
    assert opened == [True]


def test_model_open_rejects_absolute_bootstrap_url(ac_root: Path, monkeypatch) -> None:
    monkeypatch.setenv(LOCAL_API_TOKEN_ENV, "t" * 48)
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kwargs: httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "bootstrap_url": "https://attacker.invalid/auth/browser-bootstrap?nonce=x",
                },
            },
            request=httpx.Request("POST", url),
        ),
    )
    monkeypatch.setattr(webbrowser, "open", lambda *_args, **_kwargs: None)

    result = CliRunner().invoke(cli.app, ["model", "open"])

    assert result.exit_code == 1
    assert "invalid browser capability" in result.output


def test_model_open_retries_daemon_startup_connection_race(ac_root: Path, monkeypatch) -> None:
    token = "t" * 48
    monkeypatch.setenv(LOCAL_API_TOKEN_ENV, token)
    calls: list[str] = []
    delays: list[float] = []

    def fake_post(url: str, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(url)
        if len(calls) < 3:
            raise httpx.ConnectError(
                "daemon socket not bound yet",
                request=httpx.Request("POST", url),
            )
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "bootstrap_url": "/auth/browser-bootstrap?nonce=" + "n" * 43,
                },
            },
            request=httpx.Request("POST", url),
        )

    opened: list[str] = []
    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(cli, "_daemon_lock_is_held", lambda: True)
    monkeypatch.setattr(cli.time, "sleep", delays.append)
    monkeypatch.setattr(webbrowser, "open", lambda url, new=0: opened.append(url) or True)

    result = CliRunner().invoke(cli.app, ["model", "open"])

    assert result.exit_code == 0, result.output
    assert calls == [
        "http://127.0.0.1:8742/auth/browser-bootstrap",
        "http://127.0.0.1:8742/auth/browser-bootstrap",
        "http://127.0.0.1:8742/auth/browser-bootstrap",
    ]
    assert delays == [
        cli._MODEL_VIEWER_STARTUP_RETRY_SECONDS,
        cli._MODEL_VIEWER_STARTUP_RETRY_SECONDS,
    ]
    assert len(opened) == 1


def test_model_open_does_not_retry_authentication_failure(ac_root: Path, monkeypatch) -> None:
    monkeypatch.setenv(LOCAL_API_TOKEN_ENV, "t" * 48)
    calls: list[str] = []
    delays: list[float] = []

    def fake_post(url: str, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(url)
        return httpx.Response(
            401,
            json={"success": False, "error": "unauthorized"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(cli.time, "sleep", delays.append)
    monkeypatch.setattr(
        webbrowser,
        "open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("browser opened")),
    )

    result = CliRunner().invoke(cli.app, ["model", "open"])

    assert result.exit_code == 1
    assert len(calls) == 1
    assert delays == []
    assert "authentication failed" in result.output
    assert "HTTP 401" in result.output


def test_model_open_startup_retry_is_bounded(ac_root: Path, monkeypatch) -> None:
    monkeypatch.setenv(LOCAL_API_TOKEN_ENV, "t" * 48)
    calls: list[str] = []
    delays: list[float] = []

    def connection_refused(url: str, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(url)
        raise httpx.ConnectError(
            "daemon socket not bound",
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", connection_refused)
    monkeypatch.setattr(cli, "_daemon_lock_is_held", lambda: True)
    monkeypatch.setattr(cli.time, "sleep", delays.append)

    result = CliRunner().invoke(cli.app, ["model", "open"])

    assert result.exit_code == 1
    assert len(calls) == cli._MODEL_VIEWER_STARTUP_ATTEMPTS
    assert len(delays) == cli._MODEL_VIEWER_STARTUP_ATTEMPTS - 1
    assert "Could not authorize" in result.output


def test_model_open_fails_immediately_when_runtime_is_stopped(ac_root: Path, monkeypatch) -> None:
    monkeypatch.setenv(LOCAL_API_TOKEN_ENV, "t" * 48)
    calls: list[str] = []
    delays: list[float] = []

    def connection_refused(url: str, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(url)
        raise httpx.ConnectError(
            "daemon socket not bound",
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", connection_refused)
    monkeypatch.setattr(cli, "_daemon_lock_is_held", lambda: False)
    monkeypatch.setattr(cli, "_read_pid", lambda: None)
    monkeypatch.setattr(cli.time, "sleep", delays.append)

    result = CliRunner().invoke(cli.app, ["model", "open"])

    assert result.exit_code == 1
    assert len(calls) == 1
    assert delays == []
    assert "Could not authorize" in result.output


def test_background_start_probe_requires_authenticated_persome_identity(
    ac_root: Path, monkeypatch
) -> None:
    token = "s" * 48
    process = _runtime_process()
    request: dict[str, object] = {}
    monkeypatch.setenv(LOCAL_API_TOKEN_ENV, token)
    monkeypatch.setattr(cli.runtime_pid, "resolve_recorded_process", lambda: process)

    def fake_get(url: str, **kwargs):  # type: ignore[no-untyped-def]
        request.update(url=url, **kwargs)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "version": cli.__version__,
                    "root": str(cli.paths.root()),
                    "daemon": f"running pid {process.pid}",
                },
            },
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx, "get", fake_get)

    state, detail, observed = cli._probe_background_runtime(_http_runtime_config())

    assert state == "ready"
    assert "authenticated local HTTP Runtime" in detail
    assert observed is process
    assert request["url"] == "http://127.0.0.1:8742/status"
    assert request["headers"] == {"Authorization": f"Bearer {token}"}
    assert request["trust_env"] is False


def test_background_start_probe_rejects_non_persome_service_on_port(
    ac_root: Path, monkeypatch
) -> None:
    process = _runtime_process()
    monkeypatch.setenv(LOCAL_API_TOKEN_ENV, "s" * 48)
    monkeypatch.setattr(cli.runtime_pid, "resolve_recorded_process", lambda: process)
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **_kwargs: httpx.Response(
            200,
            json={"status": "ok"},
            request=httpx.Request("GET", url),
        ),
    )

    state, detail, observed = cli._probe_background_runtime(_http_runtime_config())

    assert state == "fatal"
    assert "different or incompatible service" in detail
    assert observed is process


def test_background_start_wait_is_bounded_when_socket_never_binds(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        cli,
        "_probe_background_runtime",
        lambda _cfg, _expected=None: ("retry", "socket not bound", None),
    )

    ready, detail, process = cli._wait_for_background_start(
        _http_runtime_config(), timeout_seconds=0
    )

    assert ready is False
    assert "startup timed out" in detail
    assert process is None


def test_cli_client_installers_use_stdio_without_bearer(ac_root: Path, monkeypatch) -> None:
    monkeypatch.setenv(LOCAL_API_TOKEN_ENV, "secret-bearer-value" * 3)
    binaries = {
        "persome": "/opt/persome/bin/persome",
        "claude": "/opt/claude/bin/claude",
        "codex": "/opt/codex/bin/codex",
    }
    monkeypatch.setattr(cli.shutil, "which", lambda name: binaries.get(name))
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(command)
        return subprocess.CompletedProcess(command, 1 if "remove" in command else 0, "", "")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    claude = CliRunner().invoke(cli.app, ["install", "claude-code"])
    codex = CliRunner().invoke(cli.app, ["install", "codex"])

    assert claude.exit_code == 0, claude.output
    assert codex.exit_code == 0, codex.output
    assert [
        "/opt/claude/bin/claude",
        "mcp",
        "add",
        "-s",
        "user",
        "persome",
        "--",
        "/opt/persome/bin/persome",
        "mcp",
    ] in calls
    assert [
        "/opt/codex/bin/codex",
        "mcp",
        "add",
        "persome",
        "--",
        "/opt/persome/bin/persome",
        "mcp",
    ] in calls
    assert all("Bearer" not in " ".join(command) for command in calls)


def test_opencode_installer_writes_private_local_stdio_config(
    ac_root: Path, tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "opencode.json"
    monkeypatch.setattr(cli, "_opencode_config_path", lambda: config_path)
    monkeypatch.setattr(cli, "_stdio_mcp_command", lambda: ["/opt/persome", "mcp"])

    result = CliRunner().invoke(cli.app, ["install", "opencode"])

    assert result.exit_code == 0, result.output
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["mcp"]["persome"] == {
        "type": "local",
        "command": ["/opt/persome", "mcp"],
        "enabled": True,
    }
    assert _mode(config_path) == 0o600


def test_cursor_installer_defaults_to_project_and_preserves_config(
    ac_root: Path, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / ".cursor" / "mcp.json"
    config_path.parent.mkdir()
    config_path.write_text(
        json.dumps({"theme": "dark", "mcpServers": {"keep": {"command": "keep"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "_stdio_mcp_command", lambda: ["/opt/persome", "mcp"])

    first = CliRunner().invoke(cli.app, ["install", "cursor"])
    second = CliRunner().invoke(cli.app, ["install", "cursor"])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["theme"] == "dark"
    assert data["mcpServers"]["keep"] == {"command": "keep"}
    assert data["mcpServers"]["persome"] == {"command": "/opt/persome", "args": ["mcp"]}
    assert "Updated" in second.output
    assert _mode(config_path) == 0o600


def test_cursor_user_scope_and_uninstall_are_isolated(
    ac_root: Path, tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(project)
    monkeypatch.setattr(cli, "_stdio_mcp_command", lambda: ["/opt/persome", "mcp"])

    installed = CliRunner().invoke(cli.app, ["install", "cursor", "--scope", "user"])
    removed = CliRunner().invoke(cli.app, ["uninstall", "cursor", "--scope", "user"])
    removed_again = CliRunner().invoke(cli.app, ["uninstall", "cursor", "--scope", "user"])

    assert installed.exit_code == 0, installed.output
    assert removed.exit_code == 0, removed.output
    assert removed_again.exit_code == 0, removed_again.output
    user_config = home / ".cursor" / "mcp.json"
    assert "persome" not in json.loads(user_config.read_text(encoding="utf-8"))["mcpServers"]
    assert not (project / ".cursor" / "mcp.json").exists()


def test_cursor_installer_does_not_replace_malformed_config(
    ac_root: Path, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / ".cursor" / "mcp.json"
    config_path.parent.mkdir()
    config_path.write_text("{not-json", encoding="utf-8")

    result = CliRunner().invoke(cli.app, ["install", "cursor"])

    assert result.exit_code == 1
    assert config_path.read_text(encoding="utf-8") == "{not-json"


def test_http_mcp_json_is_authenticated_private_and_warned(
    ac_root: Path, tmp_path: Path, monkeypatch
) -> None:
    token = "h" * 48
    monkeypatch.setenv(LOCAL_API_TOKEN_ENV, token)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        cli.app,
        ["install", "mcp-json", "--http", "--filename", "persome.json"],
    )

    assert result.exit_code == 0, result.output
    output = tmp_path / "persome.json"
    entry = json.loads(output.read_text(encoding="utf-8"))["mcpServers"]["persome"]
    assert entry["headers"] == {"Authorization": f"Bearer {token}"}
    assert _mode(output) == 0o600
    assert "do not commit or share" in result.output
    assert token not in result.output


def test_private_json_writer_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("unchanged", encoding="utf-8")
    link = tmp_path / "config.json"
    link.symlink_to(target)

    try:
        cli._write_private_json(link, {"changed": True})
    except RuntimeError as exc:
        assert "symlinked config" in str(exc)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("symlinked client config was accepted")
    assert target.read_text(encoding="utf-8") == "unchanged"


def test_opencode_uninstaller_preserves_private_mode(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "opencode.json"
    config_path.write_text(
        json.dumps({"mcp": {"persome": {"type": "local"}, "keep": {"enabled": True}}}),
        encoding="utf-8",
    )
    config_path.chmod(0o644)
    monkeypatch.setattr(cli, "_opencode_config_path", lambda: config_path)

    result = CliRunner().invoke(cli.app, ["uninstall", "opencode"])

    assert result.exit_code == 0, result.output
    assert "persome" not in json.loads(config_path.read_text(encoding="utf-8"))["mcp"]
    assert _mode(config_path) == 0o600


def test_claude_desktop_uninstaller_preserves_private_mode(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "claude_desktop_config.json"
    config_path.write_text(
        json.dumps({"mcpServers": {"persome": {"command": "x"}, "keep": {"command": "y"}}}),
        encoding="utf-8",
    )
    config_path.chmod(0o644)
    monkeypatch.setattr(cli, "_claude_desktop_config_path", lambda: config_path)
    monkeypatch.setattr(cli, "_restart_reminder", lambda _action: None)

    result = CliRunner().invoke(cli.app, ["uninstall", "claude-desktop"])

    assert result.exit_code == 0, result.output
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert "persome" not in data["mcpServers"]
    assert _mode(config_path) == 0o600


def test_personal_json_report_is_private_and_does_not_follow_symlink(
    ac_root: Path, tmp_path: Path
) -> None:
    victim = tmp_path / "victim.json"
    victim.write_text("ORIGINAL", encoding="utf-8")
    victim.chmod(0o644)
    output = tmp_path / "root-report.json"
    output.symlink_to(victim)

    result = CliRunner().invoke(
        cli.app,
        ["root-report", "--json-out", str(output)],
    )

    assert result.exit_code == 0, result.output
    assert output.is_file() and not output.is_symlink()
    assert json.loads(output.read_text(encoding="utf-8"))["root"] is None
    assert _mode(output) == 0o600
    assert victim.read_text(encoding="utf-8") == "ORIGINAL"
    assert _mode(victim) == 0o644

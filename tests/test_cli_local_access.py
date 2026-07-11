"""CLI integrations must not leak the daemon bearer credential."""

from __future__ import annotations

import json
import stat
import subprocess
import webbrowser
from pathlib import Path

import httpx
from typer.testing import CliRunner

from persome import cli
from persome.env_file import LOCAL_API_TOKEN_ENV


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


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

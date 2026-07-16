"""Authenticated coding-agent CLIs can fund bounded background modeling."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from persome import config, paths
from persome import doctor as doctor_mod
from persome.cli import app
from persome.config import AgentFundingConfig, Config
from persome.writer import agent_cli
from persome.writer import llm as llm_mod


def _executable(tmp_path: Path, name: str) -> str:
    path = tmp_path / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)
    return str(path)


def _funding(tmp_path: Path, **overrides: Any) -> AgentFundingConfig:
    values = {
        "enabled": True,
        "client": "codex",
        "executable": _executable(tmp_path, "codex"),
        "daily_call_limit": 50,
        "timeout_seconds": 30.0,
        "max_parallel_calls": 1,
    }
    values.update(overrides)
    return AgentFundingConfig(**values)


def _completed(command: list[str], stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 0, stdout, "")


def test_executable_resolution_preserves_updater_managed_shim(tmp_path: Path) -> None:
    target = Path(_executable(tmp_path, "codex-versioned"))
    shim = tmp_path / "codex"
    shim.symlink_to(target)
    assert agent_cli.find_executable("codex", str(shim)) == str(shim.absolute())


def test_codex_bridge_uses_stdin_structured_output_and_strips_auth_env(
    ac_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("PERSOME_LLM_API_KEY", "must-not-leak-either")
    seen: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.update({"command": command, **kwargs})
        payload = {"content": "done", "tool_calls": [], "finish_reason": "stop"}
        return _completed(command, json.dumps(payload))

    monkeypatch.setattr(agent_cli.subprocess, "run", fake_run)
    response = agent_cli.complete(
        _funding(tmp_path),
        messages=[{"role": "user", "content": "private model input"}],
        tools=None,
        max_tokens=128,
    )

    assert llm_mod.extract_text(response) == "done"
    assert seen["input"].endswith("REQUEST_JSON\n" + seen["input"].split("REQUEST_JSON\n", 1)[1])
    assert "private model input" not in " ".join(seen["command"])
    assert "--ignore-user-config" in seen["command"]
    assert "--ephemeral" in seen["command"]
    assert seen["env"].get("OPENAI_API_KEY") is None
    assert seen["env"].get("PERSOME_LLM_API_KEY") is None
    assert stat.S_IMODE(paths.agent_funding_usage_file().stat().st_mode) == 0o600


def test_claude_bridge_accepts_structured_output_and_disables_tools(
    ac_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _executable(tmp_path, "claude")

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        envelope = {
            "content": "",
            "tool_calls": [{"name": "commit", "arguments_json": '{"content":"durable"}'}],
            "finish_reason": "tool_calls",
        }
        return _completed(command, json.dumps({"structured_output": envelope}))

    monkeypatch.setattr(agent_cli.subprocess, "run", fake_run)
    response = agent_cli.complete(
        _funding(tmp_path, client="claude-code", executable=executable),
        messages=[{"role": "user", "content": "remember this"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "commit",
                    "description": "persist",
                    "parameters": {"type": "object"},
                },
            }
        ],
        max_tokens=256,
    )

    assert llm_mod.extract_tool_calls(response) == [
        {"id": "agent-cli-1", "name": "commit", "arguments": {"content": "durable"}}
    ]


def test_cursor_bridge_parses_result_envelope(
    ac_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _executable(tmp_path, "cursor-agent")

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        envelope = {"content": "cursor result", "tool_calls": [], "finish_reason": "stop"}
        return _completed(command, json.dumps({"type": "result", "result": json.dumps(envelope)}))

    monkeypatch.setattr(agent_cli.subprocess, "run", fake_run)
    response = agent_cli.complete(
        _funding(tmp_path, client="cursor-agent", executable=executable),
        messages=[{"role": "user", "content": "model"}],
        tools=None,
        max_tokens=64,
    )
    assert llm_mod.extract_text(response) == "cursor result"


def test_daily_budget_is_durable_and_fail_closed(
    ac_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        del kwargs
        calls += 1
        return _completed(
            command,
            json.dumps({"content": "ok", "tool_calls": [], "finish_reason": "stop"}),
        )

    monkeypatch.setattr(agent_cli.subprocess, "run", fake_run)
    funding = _funding(tmp_path, daily_call_limit=1)
    agent_cli.complete(
        funding,
        messages=[{"role": "user", "content": "one"}],
        tools=None,
        max_tokens=32,
    )
    with pytest.raises(agent_cli.AgentFundingBudgetExceeded, match="daily call limit"):
        agent_cli.complete(
            funding,
            messages=[{"role": "user", "content": "two"}],
            tools=None,
            max_tokens=32,
        )
    assert calls == 1
    assert agent_cli.usage_status(funding).used == 1


@pytest.mark.parametrize(
    ("client", "stdout", "ready", "method"),
    [
        ("codex", "Logged in using ChatGPT\n", True, "chatgpt"),
        ("codex", "Logged in using an API key\n", False, "api-key"),
        (
            "claude-code",
            json.dumps({"loggedIn": True, "authMethod": "oauth", "apiProvider": "firstParty"}),
            True,
            "oauth",
        ),
    ],
)
def test_client_status_distinguishes_subscription_from_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    client: str,
    stdout: str,
    ready: bool,
    method: str,
) -> None:
    executable = _executable(tmp_path, client)
    monkeypatch.setattr(
        agent_cli.subprocess,
        "run",
        lambda command, **kwargs: _completed(command, stdout),
    )
    status = agent_cli.client_status(client, executable)
    assert status.entitlement_ready is ready
    assert status.auth_method == method


@pytest.mark.parametrize(
    ("client", "help_text"),
    [
        ("codex", "--output-schema --ignore-user-config --ephemeral"),
        (
            "claude-code",
            "--json-schema --tools --no-session-persistence --strict-mcp-config",
        ),
        ("cursor-agent", "--output-format --print"),
    ],
)
def test_client_capability_detection_requires_safe_noninteractive_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    client: str,
    help_text: str,
) -> None:
    executable = _executable(tmp_path, client)
    monkeypatch.setattr(
        agent_cli.subprocess,
        "run",
        lambda command, **kwargs: _completed(command, help_text),
    )
    assert agent_cli.client_capability_status(client, executable).supported is True


def test_call_llm_prefers_enabled_agent_funding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PERSOME_LLM_MOCK", raising=False)
    seen: dict[str, Any] = {}

    def fake_complete(funding: AgentFundingConfig, **kwargs: Any) -> Any:
        seen["funding"] = funding
        seen.update(kwargs)
        return SimpleNamespace(source="agent-cli")

    monkeypatch.setattr(agent_cli, "complete", fake_complete)
    cfg = Config(agent_funding=AgentFundingConfig(enabled=True, client="codex"))
    result = llm_mod.call_llm(
        cfg,
        "timeline",
        messages=[{"role": "user", "content": "hello"}],
    )
    assert result.source == "agent-cli"
    assert seen["messages"][0]["content"] == "hello"


def test_agent_funding_config_round_trip_keeps_tokens_out(ac_root: Path, tmp_path: Path) -> None:
    paths.config_file().write_text('[models.default]\nmodel = "fallback"\n', encoding="utf-8")
    funding = _funding(tmp_path, daily_call_limit=17, model="subscription-model")
    agent_cli.save_config(funding)

    text = paths.config_file().read_text(encoding="utf-8")
    loaded = config.load().agent_funding
    assert loaded.enabled is True
    assert loaded.daily_call_limit == 17
    assert loaded.model == "subscription-model"
    assert "oauth" not in text.lower()
    assert "token" not in text.lower()


def test_llm_agent_setup_saves_verified_client_without_live_spend(
    ac_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _executable(tmp_path, "codex")
    status = agent_cli.AgentClientStatus(
        client="codex",
        executable=executable,
        installed=True,
        authenticated=True,
        entitlement_ready=True,
        auth_method="chatgpt",
        detail="ready via chatgpt",
    )
    monkeypatch.setattr(agent_cli, "find_executable", lambda client, configured="": executable)
    monkeypatch.setattr(agent_cli, "client_status", lambda client, executable="": status)
    monkeypatch.setattr(
        agent_cli,
        "client_capability_status",
        lambda client, executable="": agent_cli.AgentCapabilityStatus(
            client, True, "structured bridge supported"
        ),
    )
    monkeypatch.setattr(
        agent_cli,
        "probe",
        lambda config: pytest.fail("setup without --check must not spend allowance"),
    )

    result = CliRunner().invoke(
        app,
        ["llm", "agent", "setup", "--client", "codex", "--daily-call-limit", "23"],
    )

    assert result.exit_code == 0, result.output
    loaded = config.load().agent_funding
    assert loaded.enabled is True
    assert loaded.client == "codex"
    assert loaded.daily_call_limit == 23
    assert os.path.isabs(loaded.executable)


def test_installer_fund_model_flag_is_explicit_consent(
    ac_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _executable(tmp_path, "codex")
    monkeypatch.setattr(
        "persome.cli.shutil.which",
        lambda name: executable if name in {"codex", "persome"} else None,
    )
    monkeypatch.setattr(
        "persome.cli.subprocess.run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )
    status = agent_cli.AgentClientStatus(
        "codex", executable, True, True, True, "chatgpt", "ready via chatgpt"
    )
    monkeypatch.setattr(agent_cli, "find_executable", lambda client, configured="": executable)
    monkeypatch.setattr(agent_cli, "client_status", lambda client, executable="": status)
    monkeypatch.setattr(
        agent_cli,
        "client_capability_status",
        lambda client, executable="": agent_cli.AgentCapabilityStatus(
            client, True, "structured bridge supported"
        ),
    )

    plain = CliRunner().invoke(app, ["install", "codex"])
    assert plain.exit_code == 0, plain.output
    assert config.load().agent_funding.enabled is False

    funded = CliRunner().invoke(
        app,
        ["install", "codex", "--fund-model", "--daily-call-limit", "31"],
    )
    assert funded.exit_code == 0, funded.output
    assert "not its login token" in funded.output
    assert config.load().agent_funding.enabled is True
    assert config.load().agent_funding.daily_call_limit == 31


def test_doctor_accepts_client_owned_login_without_provider_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    funding = AgentFundingConfig(enabled=True, client="codex", daily_call_limit=10)
    status = agent_cli.AgentClientStatus(
        "codex", "/opt/codex", True, True, True, "chatgpt", "ready via chatgpt"
    )
    usage = agent_cli.AgentUsageStatus("2026-07-16", 2, 10, 8)
    monkeypatch.setattr(agent_cli, "client_status", lambda client, executable="": status)
    monkeypatch.setattr(
        agent_cli,
        "client_capability_status",
        lambda client, executable="": agent_cli.AgentCapabilityStatus(
            client, True, "structured bridge supported"
        ),
    )
    monkeypatch.setattr(agent_cli, "usage_status", lambda config: usage)

    check = doctor_mod.check_agent_funding(funding)
    assert check.status == "ok"
    assert "OAuth token remains client-owned" in check.detail

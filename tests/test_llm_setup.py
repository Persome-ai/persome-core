from __future__ import annotations

import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from typer.testing import CliRunner

from persome import config, paths
from persome.cli import app
from persome.llm_setup import ProbeResult, probe_profile, save_profile
from persome.providers import make_profile


def _profile(api_key: str = "synthetic-secret"):  # type: ignore[no-untyped-def]
    return make_profile(
        "openai",
        model="gpt-4.1-mini",
        base_url="https://gateway.example/v1",
        api_key_env="OPENAI_API_KEY",
        api_key=api_key,
        protocol="openai",
    )


def test_save_profile_keeps_secret_out_of_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    env_path = tmp_path / "env"
    config_path.write_text("[capture]\ninterval_minutes = 3\n")

    save_profile(_profile(), config_path=config_path, env_path=env_path)

    text = config_path.read_text()
    assert "synthetic-secret" not in text
    assert 'provider = "openai"' in text
    assert "interval_minutes = 3" in text
    assert env_path.read_text() == "OPENAI_API_KEY=synthetic-secret\n"
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    loaded = config.load(config_path).model_for("default")
    assert loaded.protocol == "openai"
    assert loaded.base_url == "https://gateway.example/v1"


def test_probe_profile_checks_completion_and_tools(monkeypatch) -> None:
    calls = 0

    class _FakeCompletions:
        def create(self, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
                )
            tool_call = SimpleNamespace(function=SimpleNamespace(name="persome_setup_check"))
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[tool_call]))]
            )

    class _FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            self.chat = SimpleNamespace(completions=_FakeCompletions())

    monkeypatch.setattr("openai.OpenAI", _FakeClient)

    result = probe_profile(_profile())

    assert result.completion_ok is True
    assert result.tool_call_ok is True
    assert calls == 2


def test_probe_retries_auto_when_forced_tool_choice_is_rejected(monkeypatch) -> None:
    calls = 0

    class _FakeCompletions:
        def create(self, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
                )
            if calls == 2:
                raise RuntimeError("forced tool_choice is unsupported")
            assert kwargs["tool_choice"] == "auto"
            tool_call = SimpleNamespace(function=SimpleNamespace(name="persome_setup_check"))
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[tool_call]))]
            )

    class _FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            self.chat = SimpleNamespace(completions=_FakeCompletions())

    monkeypatch.setattr("openai.OpenAI", _FakeClient)

    result = probe_profile(_profile())

    assert result.tool_call_ok is True
    assert result.error is None
    assert calls == 3


def test_setup_cli_detects_exported_key_and_persists_profile(ac_root: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-secret")
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "llm",
            "setup",
            "--provider",
            "openai",
            "--model",
            "gpt-test",
            "--base-url",
            "https://gateway.example/v1",
            "--yes",
            "--skip-check",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "synthetic-secret" not in result.output
    selected = config.load().model_for("default")
    assert selected.provider == "openai"
    assert selected.model == "gpt-test"
    assert paths.env_file().read_text().count("OPENAI_API_KEY=") == 1


def test_setup_cli_does_not_save_failed_probe(ac_root: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-secret")

    class _BrokenClient:
        def __init__(self, **kwargs: Any) -> None:
            raise RuntimeError("bad endpoint")

    monkeypatch.setattr("openai.OpenAI", _BrokenClient)
    result = CliRunner().invoke(
        app,
        [
            "llm",
            "setup",
            "--provider",
            "openai",
            "--model",
            "gpt-test",
            "--base-url",
            "https://bad.example/v1",
            "--yes",
        ],
    )

    assert result.exit_code == 1
    assert "Nothing was saved" in result.output
    assert "synthetic-secret" not in paths.config_file().read_text()


def test_status_describes_keyless_local_provider(ac_root: Path) -> None:
    setup = CliRunner().invoke(
        app,
        [
            "llm",
            "setup",
            "--provider",
            "ollama",
            "--model",
            "qwen3:8b",
            "--yes",
            "--skip-check",
        ],
    )
    assert setup.exit_code == 0, setup.output

    status = CliRunner().invoke(app, ["llm", "status"])

    assert status.exit_code == 0
    assert "not required" in status.output
    assert "set via OLLAMA_API_KEY" not in status.output


def test_status_check_fails_when_tool_calling_is_unconfirmed(ac_root: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    setup = CliRunner().invoke(
        app,
        ["llm", "setup", "--provider", "ollama", "--yes", "--skip-check"],
    )
    assert setup.exit_code == 0, setup.output
    monkeypatch.setattr(
        "persome.llm_setup.probe_profile",
        lambda profile: ProbeResult(True, False, 12, "tool choice unsupported"),
    )

    status = CliRunner().invoke(app, ["llm", "status", "--check"])

    assert status.exit_code == 1
    assert "not confirmed" in status.output

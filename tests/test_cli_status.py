"""Tests for `persome status` — particularly the per-stage LLM probes."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from persome import __version__, cli, paths
from persome.writer import llm as llm_mod


def test_status_renders_mocked_pings(ac_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PERSOME_LLM_MOCK=1 short-circuits each stage probe to '✓ mocked'."""
    monkeypatch.setenv("PERSOME_LLM_MOCK", "1")
    runner = CliRunner()
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Model (timeline)" in out
    assert "Model (reducer)" in out
    assert "Model (classifier)" in out
    assert "Model (compact)" in out
    # All four stages share the default model, so they all show the mocked tick.
    assert out.count("mocked") >= 4


def test_ping_stages_dedups_identical_configs(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stages with identical (model, base_url, api_key) only ping the network once.

    The default config gives every stage the same model, so a four-stage
    status call should invoke ping_stage exactly once and reuse the result
    via dataclasses.replace for the other three.
    """
    monkeypatch.delenv("PERSOME_LLM_MOCK", raising=False)
    call_count = {"n": 0}

    def counting_ping(cfg, stage, *, timeout=5.0):  # noqa: ARG001
        call_count["n"] += 1
        return llm_mod.PingResult(
            stage=stage,
            model=cfg.model_for(stage).model,
            ok=True,
            latency_ms=42,
            error=None,
        )

    monkeypatch.setattr(llm_mod, "ping_stage", counting_ping)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["status"])

    assert result.exit_code == 0, result.output
    # All four stages share the default model, so dedup collapses to one network call.
    assert call_count["n"] == 1, f"expected 1 ping_stage call, got {call_count['n']}"
    # …but every stage row still shows a tick — the result was replicated, not skipped.
    assert result.output.count("42 ms") == 4


def test_status_loads_env_file_before_probe(ac_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """status sources ~/.persome/env before the per-stage model probe.

    Regression: the probe used to run in the `status` process without loading the
    env file, so ping_stage built the Anthropic client with no ANTHROPIC_API_KEY
    and every stage falsely reported "Could not resolve authentication method" —
    even when the daemon (which loads env in start()) was healthy.
    """
    monkeypatch.delenv("PERSOME_LLM_MOCK", raising=False)
    # load_env_file does not overwrite an already-set var, so clear it first.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    env_path = paths.env_file()
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("ANTHROPIC_API_KEY=sk-test-from-env-file\n")

    seen: dict[str, str | None] = {}

    def capture_ping(cfg, stage, *, timeout=5.0):  # noqa: ARG001
        seen[stage] = os.environ.get("ANTHROPIC_API_KEY")
        return llm_mod.PingResult(
            stage=stage,
            model=cfg.model_for(stage).model,
            ok=True,
            latency_ms=1,
            error=None,
        )

    monkeypatch.setattr(llm_mod, "ping_stage", capture_ping)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["status"])

    assert result.exit_code == 0, result.output
    assert seen, "ping_stage was never called"
    assert all(v == "sk-test-from-env-file" for v in seen.values()), seen


def test_status_renders_probe_failure(ac_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When ping_stage raises, status shows ✗ <ErrorClass> and still exits 0."""
    # Make sure mock-mode is OFF so the real ping_stage path runs.
    monkeypatch.delenv("PERSOME_LLM_MOCK", raising=False)

    def boom(cfg, stage, *, timeout=5.0):  # noqa: ARG001
        return llm_mod.PingResult(
            stage=stage,
            model=cfg.model_for(stage).model,
            ok=False,
            latency_ms=None,
            error="AuthenticationError",
        )

    monkeypatch.setattr(llm_mod, "ping_stage", boom)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0, result.output
    assert "AuthenticationError" in result.output
    assert "✗" in result.output


# ═══════════════════════════════════════════════════════════════════
#  Status helper unit tests
# ═══════════════════════════════════════════════════════════════════


def test_daemon_uptime_stopped_when_no_pid(ac_root: Path) -> None:
    """Returns "stopped" when the daemon is not running."""
    assert cli._daemon_uptime() == "stopped"


def test_daemon_uptime_running(ac_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Returns a human-readable uptime when PID file exists."""
    paths.pid_file().write_text("99999")
    monkeypatch.setattr(cli, "_read_pid", lambda: 99999)

    uptime = cli._daemon_uptime()

    assert uptime != "stopped"
    assert "m" in uptime  # recently created PID file → minutes-level uptime


def test_health_stopped() -> None:
    """(None, None) → "stopped", "red"."""
    label, style = cli._health_status(None, None)
    assert label == "stopped"
    assert style == "red"


def test_health_running_no_captures() -> None:
    """PID exists but no last timestamp → "running (no captures yet)", "yellow"."""
    label, style = cli._health_status(9999, None)
    assert "no captures" in label
    assert style == "yellow"


def test_health_healthy() -> None:
    """Timestamp within 5 minutes → "healthy", "green"."""
    label, style = cli._health_status(9999, datetime.now().isoformat())
    assert label == "healthy"
    assert style == "green"


def test_health_stale() -> None:
    """Timestamp older than 5 minutes → "stale", "yellow"."""
    old = (datetime.now() - timedelta(minutes=10)).isoformat()
    label, style = cli._health_status(9999, old)
    assert "stale" in label
    assert style == "yellow"


def test_health_tz_aware_timestamp() -> None:
    """Offset-aware timestamps don't cause TypeError in subtraction."""
    label, style = cli._health_status(9999, "2026-04-22T14:00:00+08:00")
    assert label in ("healthy", "stale (no captures in >5m)")
    assert "red" not in style  # not stopped


def test_health_malformed_timestamp() -> None:
    """Unparseable timestamps are handled gracefully."""
    label, style = cli._health_status(9999, "not-a-timestamp")
    assert label == "running"
    assert style == "green"


def test_last_capture_none_when_dir_missing(ac_root: Path) -> None:
    """No capture-buffer dir → (None, None)."""
    ts, app = cli._last_capture_info()
    assert ts is None
    assert app is None


def test_last_capture_finds_newest(ac_root: Path) -> None:
    """Returns timestamp and app_name from the most recent buffer file."""
    buf = paths.capture_buffer_dir()
    buf.mkdir(parents=True, exist_ok=True)
    (buf / "c1.json").write_text(
        json.dumps(
            {
                "timestamp": "2026-04-22T14:00:00+08:00",
                "window_meta": {"app_name": "Cursor"},
            }
        )
    )
    (buf / "c2.json").write_text(
        json.dumps(
            {
                "timestamp": "2026-04-22T14:05:00+08:00",
                "window_meta": {"app_name": "Safari"},
            }
        )
    )

    ts, app = cli._last_capture_info()
    assert ts == "2026-04-22T14:05:00+08:00", f"got ts={ts!r}"
    assert app == "Safari", f"got app={app!r}"


def test_last_capture_handles_corrupted_json(ac_root: Path) -> None:
    """Corrupted JSON returns the filename stem as timestamp, None for app."""
    buf = paths.capture_buffer_dir()
    buf.mkdir(parents=True, exist_ok=True)
    (buf / "bad.json").write_text("{not valid json")

    ts, app = cli._last_capture_info()
    assert ts == "bad"
    assert app is None


# ═══════════════════════════════════════════════════════════════════
#  Status command integration tests
# ═══════════════════════════════════════════════════════════════════


def test_status_renders_new_fields(ac_root: Path) -> None:
    """Status output includes Version, Uptime, Health, and Last Capture."""
    runner = CliRunner()
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0, result.output
    assert "Version" in result.output
    assert "Uptime" in result.output
    assert "Health" in result.output
    assert "Last Capture" in result.output


def test_status_shows_version(ac_root: Path) -> None:
    """Status table includes the installed version string."""
    runner = CliRunner()
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0
    assert __version__ in result.output

"""OCR onboarding, persistence, and side-effect-free health state."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from persome import config, paths
from persome.capture import ocr_health, ocr_local, screen_recording
from persome.cli import app
from persome.config import CaptureConfig
from persome.ocr_setup import save_ocr_config


def _ready_probes(monkeypatch: pytest.MonkeyPatch, *, permission: bool = True) -> None:
    monkeypatch.setattr(ocr_health.sys, "platform", "darwin")
    monkeypatch.setattr(ocr_local, "runtime_available", lambda: True)
    monkeypatch.setattr(ocr_local, "models_available", lambda tier: True)
    monkeypatch.setattr(ocr_local, "disabled_by_environment", lambda: False)
    monkeypatch.setattr(screen_recording, "has_screen_recording", lambda: permission)


def test_health_ready_requires_config_runtime_models_and_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_probes(monkeypatch)

    health = ocr_health.inspect(CaptureConfig(enable_ocr_fallback=True))

    assert health.ready is True
    assert health.state == "ready"
    assert health.as_dict()["screen_recording"] == "granted"


def test_health_reports_permission_block(monkeypatch: pytest.MonkeyPatch) -> None:
    _ready_probes(monkeypatch, permission=False)

    health = ocr_health.inspect(CaptureConfig(enable_ocr_fallback=True))

    assert health.ready is False
    assert health.state == "permission_required"


def test_health_reports_disabled_without_hiding_runtime_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_probes(monkeypatch)

    health = ocr_health.inspect(CaptureConfig(enable_ocr_fallback=False))

    assert health.state == "disabled"
    assert health.runtime_available is True
    assert health.models_available is True


def test_save_ocr_config_preserves_other_capture_fields(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[capture]\ninterval_minutes = 7\n", encoding="utf-8")

    save_ocr_config(enabled=True, tier="small", config_path=path)

    text = path.read_text(encoding="utf-8")
    assert "interval_minutes = 7" in text
    loaded = config.load(path).capture
    assert loaded.enable_ocr_fallback is True
    assert loaded.ocr_tier == "small"
    assert loaded.ocr_structured is True


def test_setup_requests_permission_warms_worker_and_enables_ocr(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready_probes(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(
        screen_recording,
        "request_screen_recording",
        lambda: calls.append("permission") or True,
    )
    monkeypatch.setattr(ocr_local, "warm", lambda tier: calls.append(f"warm:{tier}") or True)

    result = CliRunner().invoke(app, ["ocr", "setup", "--tier", "tiny"])

    assert result.exit_code == 0, result.output
    assert calls == ["permission", "warm:tiny"]
    assert config.load().capture.enable_ocr_fallback is True
    assert "enabled and ready" in result.output


def test_setup_enables_ocr_but_fails_until_permission_is_granted(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready_probes(monkeypatch, permission=False)
    opened: list[bool] = []
    monkeypatch.setattr(screen_recording, "request_screen_recording", lambda: False)
    monkeypatch.setattr(ocr_local, "warm", lambda tier: True)
    monkeypatch.setattr(
        "persome.cli._open_screen_recording_settings",
        lambda: opened.append(True),
    )

    result = CliRunner().invoke(app, ["ocr", "setup"])

    assert result.exit_code == 1
    assert config.load().capture.enable_ocr_fallback is True
    assert opened == [True]
    assert "not granted yet" in result.output


def test_setup_does_not_enable_missing_runtime(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ocr_local, "disabled_by_environment", lambda: False)
    monkeypatch.setattr(ocr_local, "runtime_available", lambda: False)

    result = CliRunner().invoke(app, ["ocr", "setup"])

    assert result.exit_code == 1
    assert config.load().capture.enable_ocr_fallback is False
    assert "runtime is unavailable" in result.output


def test_disable_preserves_tier(ac_root: Path) -> None:
    save_ocr_config(enabled=True, tier="small", config_path=paths.config_file())

    result = CliRunner().invoke(app, ["ocr", "disable"])

    assert result.exit_code == 0
    capture = config.load().capture
    assert capture.enable_ocr_fallback is False
    assert capture.ocr_tier == "small"


def test_install_routes_ocr_through_the_complete_onboarding_gate() -> None:
    script = (Path(__file__).resolve().parents[1] / "install.sh").read_text(encoding="utf-8")

    assert "run_onboarding()" in script
    assert 'persome" onboard --tier tiny' in script
    assert script.index("run_onboarding\n") < script.index(
        "print_summary\n", script.index("main()")
    )

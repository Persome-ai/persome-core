"""Permission dialogs and the post-onboarding runtime proof."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from persome import config, onboarding, paths
from persome.capture import ocr_health, ocr_local, screen_recording
from persome.cli import app


class ScriptedUI:
    def __init__(
        self,
        *,
        confirm: bool = True,
        actions: list[onboarding.PermissionAction] | None = None,
    ) -> None:
        self.confirm_result = confirm
        self.actions = list(actions or [])
        self.confirmed: list[str] = []
        self.success_messages: list[str] = []

    def confirm(self, *, title: str, message: str, action: str) -> bool:
        self.confirmed.append(action)
        return self.confirm_result

    def wait_for_permission(self, *, title: str, message: str) -> onboarding.PermissionAction:
        return self.actions.pop(0)

    def success(self, message: str) -> None:  # pragma: no cover - protocol completeness
        self.success_messages.append(message)


def test_permission_flow_explains_requests_and_waits_for_live_grant() -> None:
    states = iter([False, False, False, True])
    opened: list[bool] = []
    requested: list[bool] = []
    ui = ScriptedUI(actions=["open_settings", "check_again"])

    onboarding._ensure_permission(
        label="Accessibility",
        check=lambda: next(states),
        request=lambda: requested.append(True) or False,
        open_settings=lambda: opened.append(True),
        ui=ui,
        explanation="Synthetic privacy explanation.",
    )

    assert ui.confirmed == ["Request Accessibility"]
    assert requested == [True]
    assert opened == [True]


def test_permission_flow_cancellation_is_a_hard_stop() -> None:
    ui = ScriptedUI(confirm=False)

    with pytest.raises(onboarding.OnboardingCancelled):
        onboarding._ensure_permission(
            label="Screen Recording",
            check=lambda: False,
            request=lambda: False,
            open_settings=lambda: None,
            ui=ui,
            explanation="Synthetic privacy explanation.",
        )


def test_local_ocr_is_saved_only_after_permission_and_worker_proof(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ocr_local, "disabled_by_environment", lambda: False)
    monkeypatch.setattr(ocr_local, "runtime_available", lambda: True)
    monkeypatch.setattr(ocr_local, "models_available", lambda tier: tier == "tiny")
    monkeypatch.setattr(ocr_local, "warm", lambda tier: tier == "tiny")
    monkeypatch.setattr(screen_recording, "has_screen_recording", lambda: True)
    monkeypatch.setattr(ocr_health.sys, "platform", "darwin")

    changed = onboarding.ensure_local_ocr(tier="tiny", ui=ScriptedUI())

    assert changed is True
    assert config.load().capture.enable_ocr_fallback is True
    assert config.load().capture.ocr_tier == "tiny"


def test_runtime_proof_requires_health_and_fresh_readable_capture(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture_path = paths.capture_buffer_dir() / "fresh.json"
    capture_path.parent.mkdir(parents=True, exist_ok=True)
    capture_path.write_text(
        '{"timestamp":"2026-07-12T00:00:00+00:00","window_meta":{"app_name":"Terminal"}}',
        encoding="utf-8",
    )
    calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(onboarding, "_running_daemon_pid", lambda: 4242)
    monkeypatch.setattr(
        onboarding,
        "_health_payload",
        lambda host, port: {"status": "ok", "ocr": "ready"},
    )
    monkeypatch.setattr(onboarding, "_latest_capture", lambda: capture_path)

    def fake_cli(*args: str, timeout: float = 180.0) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(onboarding, "_run_cli", fake_cli)

    proof = onboarding.ensure_runtime(restart=False)

    assert proof.pid == 4242
    assert proof.health == "ok"
    assert proof.ocr == "ready"
    assert proof.capture_path == capture_path
    assert calls == [("capture-once",)]


def test_runtime_proof_rejects_degraded_ocr(ac_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(onboarding, "_running_daemon_pid", lambda: 4242)
    monkeypatch.setattr(
        onboarding,
        "_health_payload",
        lambda host, port: {"status": "degraded", "ocr": "permission_required"},
    )

    with pytest.raises(onboarding.OnboardingError, match="degraded"):
        onboarding.ensure_runtime(restart=False, timeout=0.01)


def test_complete_onboarding_orders_permissions_before_runtime_proof(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ui = ScriptedUI()
    calls: list[str] = []
    capture_path = ac_root / "capture-buffer" / "fresh.json"
    proof = onboarding.RuntimeProof(4242, "ok", "ready", capture_path)
    monkeypatch.setattr(onboarding.sys, "platform", "darwin")
    monkeypatch.setattr(onboarding, "OnboardingUI", lambda gui: ui)
    monkeypatch.setattr(
        onboarding,
        "ensure_accessibility",
        lambda active_ui: calls.append("accessibility"),
    )
    monkeypatch.setattr(
        onboarding,
        "ensure_local_ocr",
        lambda tier, ui: calls.append("ocr") or True,
    )
    monkeypatch.setattr(
        onboarding,
        "ensure_runtime",
        lambda restart: calls.append(f"runtime:{restart}") or proof,
    )

    assert onboarding.onboard() == proof
    assert calls == ["accessibility", "ocr", "runtime:True"]
    assert ui.success_messages


def test_cli_onboard_reports_each_completed_gate(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture_path = ac_root / "capture-buffer" / "fresh.json"
    monkeypatch.setattr(
        onboarding,
        "onboard",
        lambda tier, gui: onboarding.RuntimeProof(
            pid=4242,
            health="ok",
            ocr="ready",
            capture_path=capture_path,
        ),
    )

    result = CliRunner().invoke(app, ["onboard"])

    assert result.exit_code == 0, result.output
    assert "Accessibility granted" in result.output
    assert "Local OCR and Screen Recording ready" in result.output
    assert "Persome running and healthy" in result.output
    assert "Fresh capture verified" in result.output


def test_installer_runs_strict_onboarding_gate() -> None:
    script = (Path(__file__).resolve().parents[1] / "install.sh").read_text(encoding="utf-8")

    assert "run_onboarding()" in script
    assert 'persome" onboard --tier tiny' in script
    assert script.index("run_onboarding\n") < script.index(
        "print_summary\n", script.index("main()")
    )
    assert 'die "onboarding is incomplete' in script

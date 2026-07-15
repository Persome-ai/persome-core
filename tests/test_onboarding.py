"""Permission dialogs and the post-onboarding runtime proof."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from typer.testing import CliRunner

from persome import config, launchagent, onboarding, paths
from persome.capture import ax_capture, ocr_health, ocr_local, scheduler, screen_recording, watcher
from persome.cli import app


@pytest.fixture(autouse=True)
def no_real_launchagent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit onboarding must never depend on or mutate the user's launchd job."""

    monkeypatch.setattr(onboarding, "_launchagent_is_loaded", lambda: False)
    monkeypatch.setattr(launchagent, "owner_intended", lambda: False)
    monkeypatch.setattr(launchagent, "configured_runtime_binary", lambda: "/fake/persome")
    monkeypatch.setattr(launchagent, "owns_recorded_runtime", lambda binary: True)
    monkeypatch.setattr(scheduler, "capture_gate_reason", lambda capture: None)


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
        self.status_messages: list[str] = []
        self.success_messages: list[str] = []

    def status(self, message: str) -> None:
        self.status_messages.append(message)

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
    assert ui.status_messages == [
        "Checking Accessibility permission...",
        "Requesting Accessibility from macOS...",
        "✓ Accessibility granted",
    ]


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


def test_accessibility_requests_each_actual_native_principal_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watcher_binary = tmp_path / "mac-ax-watcher"
    state = {"helper": False, "watcher": False}
    ui = ScriptedUI()
    monkeypatch.setattr(
        ax_capture,
        "ax_trusted",
        lambda **kwargs: state["helper"],
    )
    monkeypatch.setattr(
        ax_capture,
        "request_accessibility_permission",
        lambda **kwargs: state.__setitem__("helper", True) or True,
    )
    monkeypatch.setattr(watcher, "_resolve_watcher_path", lambda: watcher_binary)
    monkeypatch.setattr(
        ax_capture,
        "_binary_ax_trusted",
        lambda binary: state["watcher"] if binary == watcher_binary else False,
    )
    monkeypatch.setattr(
        watcher,
        "request_accessibility_permission",
        lambda: state.__setitem__("watcher", True) or True,
    )

    assert onboarding.ensure_accessibility(ui, event_driven=True) is True
    assert ui.confirmed == [
        "Request Capture Helper Accessibility",
        "Request Event Watcher Accessibility",
    ]


def test_accessibility_skips_watcher_when_event_driven_capture_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ax_capture, "ax_trusted", lambda **kwargs: True)
    monkeypatch.setattr(
        watcher,
        "_resolve_watcher_path",
        lambda: pytest.fail("disabled watcher must not become a permission principal"),
    )

    assert onboarding.ensure_accessibility(ScriptedUI(), event_driven=False) is False


def test_permission_dialog_timeout_falls_back_to_visible_terminal_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(onboarding.sys, "platform", "darwin")
    monkeypatch.setattr(onboarding.shutil, "which", lambda name: "/usr/bin/osascript")
    seen: dict[str, object] = {}

    def timeout(command: list[str], **kwargs: object) -> None:
        seen.update(command=command, kwargs=kwargs)
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(onboarding.subprocess, "run", timeout)
    monkeypatch.setattr(onboarding.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")

    accepted = onboarding.OnboardingUI().confirm(
        title="Permission", message="Explain it.", action="Request Accessibility"
    )

    assert accepted is False
    assert seen["kwargs"]["timeout"] == 300  # type: ignore[index]


def test_non_tty_missing_permission_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(onboarding.sys.stdin, "isatty", lambda: False)
    ui = onboarding.OnboardingUI(gui=False)

    with pytest.raises(onboarding.OnboardingCancelled, match="was cancelled"):
        onboarding._ensure_permission(
            label="Accessibility",
            check=lambda: False,
            request=lambda: False,
            open_settings=lambda: None,
            ui=ui,
            explanation="Synthetic explanation.",
        )


def test_local_ocr_prerequisites_are_saved_before_daemon_worker_proof(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ocr_local, "disabled_by_environment", lambda: False)
    monkeypatch.setattr(ocr_local, "runtime_available", lambda: True)
    monkeypatch.setattr(ocr_local, "models_available", lambda tier: tier == "tiny")
    monkeypatch.setattr(
        ocr_local,
        "warm",
        lambda tier: pytest.fail("onboarding must warm only the daemon-owned worker"),
    )
    monkeypatch.setattr(screen_recording, "has_screen_recording", lambda: True)
    monkeypatch.setattr(ocr_health.sys, "platform", "darwin")

    changed = onboarding.ensure_local_ocr(tier="tiny", ui=ScriptedUI())

    assert changed.config_changed is True
    assert changed.require_worker is True
    assert config.load().capture.enable_ocr_fallback is True
    assert config.load().capture.ocr_tier == "tiny"


def test_runtime_proof_requires_health_and_fresh_readable_capture(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture_path = paths.capture_buffer_dir() / "fresh.json"
    capture_path.parent.mkdir(parents=True, exist_ok=True)
    capture_path.write_text(
        '{"timestamp":"2026-07-12T00:00:00+00:00","visible_text":"ready"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(onboarding, "_running_daemon_pid", lambda: 4242)
    monkeypatch.setattr(
        onboarding,
        "_health_payload",
        lambda host, port: {"status": "ok", "ocr": "ready", "ocr_worker": "ready"},
    )
    monkeypatch.setattr(
        onboarding,
        "_runtime_permissions",
        lambda host, port: {"accessibility": "granted", "screen_recording": "granted"},
    )
    monkeypatch.setattr(
        onboarding,
        "_request_runtime_capture",
        lambda host, port: onboarding.CaptureRequestProof(capture_path, "daemon", "fresh-capture"),
    )
    monkeypatch.setattr(
        onboarding,
        "_run_cli",
        lambda *args, **kwargs: pytest.fail("fresh capture must use the daemon"),
    )

    proof = onboarding.ensure_runtime(restart=False)

    assert proof.pid == 4242
    assert proof.health == "ok"
    assert proof.ocr == "ready"
    assert proof.capture_path == capture_path


def test_runtime_proof_accepts_healthy_components_with_aggregate_degradation(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture_path = paths.capture_buffer_dir() / "fresh.json"
    capture_path.parent.mkdir(parents=True, exist_ok=True)
    capture_path.write_text(
        '{"timestamp":"2026-07-12T00:00:00+00:00","visible_text":"ready"}',
        encoding="utf-8",
    )
    health_calls = 0

    def health(host: str, port: int) -> dict[str, str]:
        nonlocal health_calls
        health_calls += 1
        return {
            "status": "degraded",
            "ocr": "ready",
            "ocr_worker": "ready",
            "index": "ok",
            "capture_pipeline": "ok",
        }

    monkeypatch.setattr(onboarding, "_running_daemon_pid", lambda: 4242)
    monkeypatch.setattr(onboarding, "_health_payload", health)
    monkeypatch.setattr(
        onboarding,
        "_runtime_permissions",
        lambda host, port: {"accessibility": "granted", "screen_recording": "granted"},
    )
    monkeypatch.setattr(
        onboarding,
        "_request_runtime_capture",
        lambda host, port: onboarding.CaptureRequestProof(capture_path, "daemon", "fresh-capture"),
    )

    proof = onboarding.ensure_runtime(restart=False)

    assert proof.health == "degraded"
    assert proof.ocr == "ready"
    assert proof.capture_path == capture_path
    assert health_calls >= 2


@pytest.mark.parametrize(
    ("index_state", "capture_state"),
    [("degraded", "ok"), ("ok", "degraded"), ("unknown", "ok")],
)
def test_runtime_proof_rejects_degraded_core_components(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    index_state: str,
    capture_state: str,
) -> None:
    monkeypatch.setattr(onboarding, "_running_daemon_pid", lambda: 4242)
    monkeypatch.setattr(
        onboarding,
        "_health_payload",
        lambda host, port: {
            "status": "degraded",
            "ocr": "ready",
            "ocr_worker": "ready",
            "index": index_state,
            "capture_pipeline": capture_state,
        },
    )
    monkeypatch.setattr(
        onboarding,
        "_runtime_permissions",
        lambda host, port: {"accessibility": "granted", "screen_recording": "granted"},
    )

    with pytest.raises(onboarding.OnboardingError, match="status=degraded"):
        onboarding.ensure_runtime(restart=False, timeout=0.01)


def test_runtime_proof_rejects_degraded_ocr(ac_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(onboarding, "_running_daemon_pid", lambda: 4242)
    monkeypatch.setattr(
        onboarding,
        "_health_payload",
        lambda host, port: {
            "status": "degraded",
            "ocr": "permission_required",
            "ocr_worker": "ready",
        },
    )
    monkeypatch.setattr(
        onboarding,
        "_runtime_permissions",
        lambda host, port: {"accessibility": "granted", "screen_recording": "granted"},
    )

    with pytest.raises(onboarding.OnboardingError, match="status=degraded"):
        onboarding.ensure_runtime(restart=False, timeout=0.01)


@pytest.mark.parametrize(
    ("permissions", "missing"),
    [
        ({"accessibility": "denied", "screen_recording": "granted"}, "Accessibility"),
        ({"accessibility": "granted", "screen_recording": "denied"}, "Screen Recording"),
        (
            {"accessibility": "denied", "screen_recording": "denied"},
            "Accessibility and Screen Recording",
        ),
    ],
)
def test_runtime_proof_rejects_daemon_permission_mismatch(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    permissions: dict[str, str],
    missing: str,
) -> None:
    monkeypatch.setattr(onboarding, "_running_daemon_pid", lambda: 4242)
    monkeypatch.setattr(
        onboarding,
        "_health_payload",
        lambda host, port: {"status": "ok", "ocr": "ready", "ocr_worker": "ready"},
    )
    monkeypatch.setattr(
        onboarding,
        "_runtime_permissions",
        lambda host, port: permissions,
    )

    with pytest.raises(onboarding.OnboardingError, match=f"does not have {missing}"):
        onboarding.ensure_runtime(restart=False)


def test_failed_stop_cannot_pass_with_old_daemon(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(onboarding, "_running_daemon_pid", lambda: 4242)
    monkeypatch.setattr(
        onboarding,
        "_run_cli",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, "", "stop failed"),
    )
    monkeypatch.setattr(onboarding.time, "sleep", lambda seconds: None)

    with pytest.raises(onboarding.OnboardingError, match="could not be stopped: stop failed"):
        onboarding.ensure_runtime(restart=True, timeout=0.01)


def test_unreachable_existing_daemon_is_restarted(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture_path = paths.capture_buffer_dir() / "fresh.json"
    capture_path.parent.mkdir(parents=True, exist_ok=True)
    capture_path.write_text(
        '{"timestamp":"2026-07-12T00:00:00+00:00","visible_text":"ready"}',
        encoding="utf-8",
    )
    state = {"pid": 4242, "health_calls": 0}
    calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(onboarding, "_running_daemon_pid", lambda: state["pid"])

    def health(host: str, port: int) -> dict[str, str] | None:
        state["health_calls"] += 1
        if state["health_calls"] == 1:
            return None
        return {"status": "ok", "ocr": "ready", "ocr_worker": "ready"}

    def cli(*args: str, timeout: float = 180.0) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[0] == "stop":
            state["pid"] = None
        elif args[0] == "start":
            state["pid"] = 7777
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(onboarding, "_health_payload", health)
    monkeypatch.setattr(onboarding, "_run_cli", cli)
    monkeypatch.setattr(
        onboarding,
        "_runtime_permissions",
        lambda host, port: {"accessibility": "granted", "screen_recording": "granted"},
    )
    monkeypatch.setattr(
        onboarding,
        "_request_runtime_capture",
        lambda host, port: onboarding.CaptureRequestProof(capture_path, "daemon", "fresh-capture"),
    )

    proof = onboarding.ensure_runtime(restart=False)

    assert proof.pid == 7777
    assert calls == [("stop", "--timeout", "20"), ("start",)]


def test_runtime_waits_for_daemon_worker_warming_to_finish(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture_path = paths.capture_buffer_dir() / "fresh.json"
    capture_path.parent.mkdir(parents=True, exist_ok=True)
    capture_path.write_text(
        '{"timestamp":"2026-07-12T00:00:00+00:00","visible_text":"ready"}',
        encoding="utf-8",
    )
    states = iter(["warming", "warming", "ready", "ready"])
    monkeypatch.setattr(onboarding, "_running_daemon_pid", lambda: 4242)
    monkeypatch.setattr(
        onboarding,
        "_health_payload",
        lambda host, port: {
            "status": "ok",
            "ocr": "ready",
            "ocr_worker": next(states),
        },
    )
    monkeypatch.setattr(
        onboarding,
        "_runtime_permissions",
        lambda host, port: {"accessibility": "granted", "screen_recording": "granted"},
    )
    monkeypatch.setattr(
        onboarding,
        "_request_runtime_capture",
        lambda host, port: onboarding.CaptureRequestProof(capture_path, "daemon", "fresh-capture"),
    )
    monkeypatch.setattr(onboarding.time, "sleep", lambda seconds: None)

    assert onboarding.ensure_runtime(restart=False).pid == 4242


def test_daemon_worker_failure_is_a_hard_stop(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(onboarding, "_running_daemon_pid", lambda: 4242)
    monkeypatch.setattr(
        onboarding,
        "_run_cli",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "", ""),
    )
    states = iter(["warming", "failed"])
    monkeypatch.setattr(
        onboarding,
        "_health_payload",
        lambda host, port: {
            "status": "degraded",
            "ocr": "ready",
            "ocr_worker": next(states),
        },
    )
    monkeypatch.setattr(
        onboarding,
        "_runtime_permissions",
        lambda host, port: {"accessibility": "granted", "screen_recording": "granted"},
    )

    with pytest.raises(onboarding.OnboardingError, match="worker failed"):
        onboarding.ensure_runtime(restart=False)


def test_cli_timeout_is_reported_as_onboarding_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        onboarding.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(args[0], kwargs["timeout"])
        ),
    )

    with pytest.raises(onboarding.OnboardingError, match="persome start.*30 seconds"):
        onboarding._run_cli("start", timeout=30)


def test_runtime_capture_receipt_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSOME_LOCAL_API_TOKEN", "x" * 32)

    class Response:
        status_code = 200

        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict:
            return {
                "data": {
                    "id": "../escape",
                    "mode": "daemon",
                    "receipt": "fresh-capture",
                }
            }

    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: Response())

    with pytest.raises(onboarding.OnboardingError, match="unsafe.*id"):
        onboarding._request_runtime_capture("127.0.0.1", 8742)


def test_runtime_capture_runner_not_ready_is_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSOME_LOCAL_API_TOKEN", "x" * 32)

    class Response:
        status_code = 503

    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: Response())

    assert onboarding._request_runtime_capture("127.0.0.1", 8742) is None


def test_daemon_onboarding_capture_forces_a_fresh_record_past_dedup(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = {
        "timestamp": "2026-07-12T00:00:00+00:00",
        "window_meta": {"app_name": "Terminal", "title": "Onboarding"},
        "visible_text": "  ready",
    }
    written: list[Path] = []
    monkeypatch.setattr(scheduler, "_build_capture", lambda cfg, provider, trigger: output)

    def write(_out: dict) -> Path:
        path = paths.capture_buffer_dir() / f"fresh-{len(written)}.json"
        written.append(path)
        return path

    monkeypatch.setattr(scheduler, "_write_capture", write)
    runner = scheduler._CaptureRunner(config.load().capture, provider=object())
    scheduler._set_active_runner(runner)
    try:
        first = scheduler.capture_now()
        second = scheduler.capture_now()
    finally:
        scheduler._set_active_runner(None)

    assert first != second
    assert len(written) == 2


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
        lambda active_ui, *, event_driven: calls.append("accessibility"),
    )
    monkeypatch.setattr(
        onboarding,
        "ensure_local_ocr",
        lambda tier, ui, preserve_policy, screenshot_permission_required, capture_source: (
            calls.append("ocr")
            or onboarding.OCRPolicyProof(True, True, False, tier or "tiny", "ready")
        ),
    )
    monkeypatch.setattr(
        onboarding,
        "ensure_runtime",
        lambda **kwargs: calls.append(f"runtime:{kwargs['restart']}") or proof,
    )

    assert onboarding.onboard() == proof
    assert calls == ["accessibility", "ocr", "runtime:True"]
    assert ui.success_messages


def test_invalid_ocr_tier_fails_before_any_permission_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(onboarding.sys, "platform", "darwin")
    monkeypatch.setattr(
        onboarding,
        "ensure_accessibility",
        lambda *args, **kwargs: pytest.fail("invalid input must not request TCC access"),
    )

    with pytest.raises(onboarding.OnboardingError, match="unsupported OCR tier"):
        onboarding.onboard(tier="huge")


def test_loaded_but_unowned_launchagent_is_repaired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owned = {"value": False}
    installs: list[str] = []
    monkeypatch.setattr(onboarding, "_launchagent_is_loaded", lambda: True)
    monkeypatch.setattr(launchagent, "owner_intended", lambda: True)
    monkeypatch.setattr(launchagent, "configured_runtime_binary", lambda: "/stable/persome")
    monkeypatch.setattr(
        launchagent,
        "owns_recorded_runtime",
        lambda binary: owned["value"],
    )

    def install(binary: str) -> Path:
        installs.append(binary)
        owned["value"] = True
        return Path("/tmp/persome.plist")

    monkeypatch.setattr(launchagent, "install", install)

    assert onboarding._select_runtime_owner("any", ui=ScriptedUI()) == "launchagent"
    assert installs == ["/stable/persome"]


def test_runtime_readiness_emits_progress_heartbeat(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config.Config()
    ui = ScriptedUI()
    clock = {"value": 0.0}

    def monotonic() -> float:
        clock["value"] += 5
        return clock["value"]

    monkeypatch.setattr(config, "load", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(scheduler, "capture_gate_reason", lambda capture: None)
    monkeypatch.setattr(onboarding, "_running_daemon_pid", lambda: 4242)
    monkeypatch.setattr(
        onboarding,
        "_health_payload",
        lambda host, port: {"status": "starting", "ocr": "ready", "ocr_worker": "warming"},
    )
    monkeypatch.setattr(
        onboarding,
        "_runtime_permissions",
        lambda host, port: None,
    )
    monkeypatch.setattr(onboarding.time, "monotonic", monotonic)
    monkeypatch.setattr(onboarding.time, "sleep", lambda seconds: None)

    with pytest.raises(onboarding.OnboardingError, match="did not become fully ready"):
        onboarding.ensure_runtime(restart=False, timeout=40, ui=ui)

    assert any("Still waiting for Runtime readiness" in line for line in ui.status_messages)
    assert any("Accessibility: checking" in line for line in ui.status_messages)
    assert any("Screen Recording: checking" in line for line in ui.status_messages)


def test_success_is_printed_and_uses_a_nonblocking_notification(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ui = onboarding.OnboardingUI(gui=True)
    calls: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        ui,
        "_osascript",
        lambda script, *args, **kwargs: calls.append((script, kwargs)) or "Notified",
    )

    ui.success("Everything passed.")

    assert "Persome is ready" in capsys.readouterr().out
    assert calls == [(onboarding._NOTIFICATION_SCRIPT, {"timeout": 5})]


def test_cli_onboard_reports_each_completed_gate_and_syncs_human(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from persome.model import human as human_mod

    capture_path = ac_root / "capture-buffer" / "fresh.json"
    sync_human = MagicMock(return_value=paths.human_file())
    monkeypatch.setattr(human_mod, "sync_live_human_markdown", sync_human)
    monkeypatch.setattr(
        onboarding,
        "onboard",
        lambda tier, gui, preserve_policy, expected_owner: onboarding.RuntimeProof(
            pid=4242,
            health="ok",
            ocr="ready",
            capture_path=capture_path,
            accessibility="granted",
            screen_recording="granted",
        ),
    )

    result = CliRunner().invoke(app, ["onboard"])

    assert result.exit_code == 0, result.output
    sync_human.assert_called_once_with()
    assert "Accessibility granted" in result.output
    assert "Screen Recording granted" in result.output
    assert "Isolated local OCR worker ready" in result.output
    assert "Persome running and healthy" in result.output
    assert "Fresh capture verified" in result.output


def test_onboard_help_describes_mode_aware_runtime_proof() -> None:
    result = CliRunner().invoke(app, ["onboard", "--help"])

    assert result.exit_code == 0, result.output
    assert "Runtime ownership" in result.output
    assert "live readiness" in result.output


def test_plain_cli_onboard_forwards_no_tier_override(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def run(**kwargs: object) -> onboarding.RuntimeProof:
        seen.update(kwargs)
        return onboarding.RuntimeProof(4242, "ok", "disabled", None, receipt="privacy-paused")

    monkeypatch.setattr(onboarding, "onboard", run)

    result = CliRunner().invoke(app, ["onboard"])

    assert result.exit_code == 0, result.output
    assert seen["tier"] is None
    assert seen["expected_owner"] == "any"


@pytest.mark.parametrize(
    ("error_type", "expected"),
    [
        (onboarding.OnboardingCancelled, "Onboarding stopped"),
        (onboarding.OnboardingError, "Onboarding failed"),
    ],
)
def test_cli_onboard_errors_are_visible_and_actionable(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
    expected: str,
) -> None:
    monkeypatch.setattr(
        onboarding,
        "onboard",
        lambda **kwargs: (_ for _ in ()).throw(error_type("synthetic reason")),
    )

    result = CliRunner().invoke(app, ["onboard"])

    assert result.exit_code == 1
    assert expected in result.output
    assert "synthetic reason" in result.output


def test_installer_runs_strict_onboarding_gate() -> None:
    script = (Path(__file__).resolve().parents[1] / "install.sh").read_text(encoding="utf-8")

    assert "run_onboarding()" in script
    assert 'persome" onboard --tier tiny' in script
    assert 'venv "${VENV_DIR}" --python "${python_target}" --relocatable' in script
    assert script.index("run_onboarding\n") < script.index(
        "print_summary\n", script.index("main()")
    )
    assert 'die "onboarding is incomplete' in script


def test_installer_schedules_and_emphasizes_model_open_cta() -> None:
    script = (Path(__file__).resolve().parents[1] / "install.sh").read_text(encoding="utf-8")

    assert 'persome" model open --onboarding' in script
    assert "Your Personal Model Opens in 30 Minutes" not in script
    assert "YOUR NEXT STEP — OPEN YOUR PERSONAL MODEL" not in script
    assert "MODEL CTA — KEEP PERSOME RUNNING" in script
    assert "MODEL CTA — OPEN YOUR PERSONAL MODEL" in script
    main = script.index("main()")
    assert script.index("run_onboarding\n", main) < script.index("schedule_model_open\n", main)
    assert script.index("schedule_model_open\n", main) < script.index("print_summary\n", main)


def test_preserve_policy_keeps_explicit_ocr_disable(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config.Config()
    cfg.capture.enable_ocr_fallback = False
    cfg.capture.ocr_tier = "medium"
    monkeypatch.setattr(config, "load", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(
        ocr_local,
        "runtime_available",
        lambda: pytest.fail("disabled policy must not start or inspect Paddle"),
    )
    monkeypatch.setattr(screen_recording, "has_screen_recording", lambda: True)

    proof = onboarding.ensure_local_ocr(
        tier="tiny",
        ui=ScriptedUI(),
        preserve_policy=True,
    )

    assert proof == onboarding.OCRPolicyProof(False, False, False, "medium", "disabled")
    assert cfg.capture.enable_ocr_fallback is False
    assert cfg.capture.ocr_tier == "medium"


def test_preserve_policy_keeps_existing_ocr_tier(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config.Config()
    cfg.capture.enable_ocr_fallback = True
    cfg.capture.ocr_tier = "small"
    monkeypatch.setattr(config, "load", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(ocr_local, "disabled_by_environment", lambda: False)
    monkeypatch.setattr(ocr_local, "runtime_available", lambda: True)
    monkeypatch.setattr(ocr_local, "models_available", lambda tier: tier == "small")
    monkeypatch.setattr(screen_recording, "has_screen_recording", lambda: True)
    monkeypatch.setattr(
        ocr_health,
        "inspect",
        lambda capture: SimpleNamespace(ready=True, state="ready", detail="ready"),
    )

    proof = onboarding.ensure_local_ocr(
        tier="tiny",
        ui=ScriptedUI(),
        preserve_policy=True,
    )

    assert proof.tier == "small"
    assert proof.config_changed is False
    assert cfg.capture.ocr_tier == "small"


def test_fresh_intel_onboarding_enables_vision_ocr(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config.Config()
    monkeypatch.setattr(config, "load", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(ocr_local, "runtime_available", lambda: True)
    monkeypatch.setattr(ocr_local, "runtime_backend", lambda: "vision")
    monkeypatch.setattr(ocr_local, "models_available", lambda tier: True)
    monkeypatch.setattr(screen_recording, "has_screen_recording", lambda: True)
    monkeypatch.setattr(
        ocr_health,
        "inspect",
        lambda capture: SimpleNamespace(ready=True, state="ready", detail="ready"),
    )

    proof = onboarding.ensure_local_ocr(tier="tiny", ui=ScriptedUI())

    assert proof.require_worker is True
    assert proof.state == "ready"


def test_plain_repeated_onboarding_preserves_explicit_ocr_opt_out(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config.Config()
    cfg.capture.enable_ocr_fallback = False
    cfg.capture.ocr_policy = "disabled"
    cfg.capture.ocr_tier = "small"
    cfg.capture.include_screenshot = False
    monkeypatch.setattr(config, "load", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(
        ocr_local,
        "runtime_available",
        lambda: pytest.fail("saved opt-out must not inspect Paddle"),
    )
    monkeypatch.setattr(
        onboarding,
        "_ensure_permission",
        lambda **kwargs: pytest.fail("pixel opt-out must not request Screen Recording"),
    )

    proof = onboarding.ensure_local_ocr(
        tier=None,
        ui=ScriptedUI(),
        preserve_policy=False,
        screenshot_permission_required=False,
    )

    assert proof.require_worker is False
    assert proof.config_changed is False
    assert proof.state == "disabled"
    assert proof.tier == "small"
    assert cfg.capture.enable_ocr_fallback is False


def test_ingest_onboarding_skips_daemon_tcc_requests(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config.Config()
    cfg.capture.source = "ingest"
    monkeypatch.setattr(config, "load", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(onboarding.sys, "platform", "darwin")
    ui = ScriptedUI()
    monkeypatch.setattr(onboarding, "OnboardingUI", lambda gui: ui)
    monkeypatch.setattr(
        onboarding,
        "ensure_accessibility",
        lambda active_ui: pytest.fail("ingest mode does not own Accessibility"),
    )
    seen: dict[str, object] = {}

    def ocr_policy(**kwargs: object) -> onboarding.OCRPolicyProof:
        seen.update(kwargs)
        return onboarding.OCRPolicyProof(False, False, False, "tiny", "disabled")

    proof = onboarding.RuntimeProof(
        4242,
        "ok",
        "disabled",
        None,
        mode="ingest",
        receipt="ingest-ready",
        generation="a" * 32,
    )
    monkeypatch.setattr(onboarding, "ensure_local_ocr", ocr_policy)
    monkeypatch.setattr(onboarding, "ensure_runtime", lambda **kwargs: proof)

    assert onboarding.onboard(preserve_policy=True) == proof
    assert seen["screenshot_permission_required"] is False
    assert seen["capture_source"] == "ingest"
    assert any("trusted ingest producer" in message for message in ui.status_messages)


@pytest.mark.parametrize("gate", ["paused", "locked"])
def test_strict_capture_proof_fails_immediately_for_privacy_gate(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch, gate: str
) -> None:
    cfg = config.Config()
    monkeypatch.setattr(config, "load", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(scheduler, "capture_gate_reason", lambda capture: gate)
    monkeypatch.setattr(
        onboarding,
        "_running_daemon_pid",
        lambda: pytest.fail("privacy failure must happen before lifecycle mutation"),
    )

    with pytest.raises(onboarding.OnboardingError, match=gate):
        onboarding.ensure_runtime(restart=False, preserve_policy=False)


def test_update_preserves_paused_capture_without_forcing_a_frame(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config.Config()
    monkeypatch.setattr(config, "load", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(scheduler, "capture_gate_reason", lambda capture: "paused")
    monkeypatch.setattr(onboarding, "_running_daemon_pid", lambda: 4242)
    monkeypatch.setattr(
        onboarding,
        "_health_payload",
        lambda host, port: {"status": "ok", "ocr": "disabled", "ocr_worker": "not_started"},
    )
    monkeypatch.setattr(
        onboarding,
        "_runtime_permissions",
        lambda host, port: {"accessibility": "granted", "screen_recording": "denied"},
    )
    monkeypatch.setattr(
        onboarding,
        "_runtime_state",
        lambda pid, **kwargs: {
            "generation": "a" * 32,
            "phase": "ready",
            "permissions": {"accessibility": "granted", "screen_recording": "denied"},
            "ocr": "disabled",
            "ocr_enabled": False,
            "ocr_tier": "tiny",
        },
    )
    requests: list[tuple[object, ...]] = []

    def paused_receipt(*args: object) -> onboarding.CaptureRequestProof:
        requests.append(args)
        return onboarding.CaptureRequestProof(None, "privacy", "privacy-paused")

    monkeypatch.setattr(onboarding, "_request_runtime_capture", paused_receipt)

    proof = onboarding.ensure_runtime(
        restart=False,
        require_ocr=False,
        preserve_policy=True,
        expected_ocr_state="disabled",
    )

    assert proof.capture_path is None
    assert proof.receipt == "privacy-paused"
    assert requests


def test_preserve_policy_accepts_screen_lock_that_happens_during_proof(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config.Config()
    monkeypatch.setattr(config, "load", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(onboarding, "_running_daemon_pid", lambda: 4242)
    monkeypatch.setattr(
        onboarding,
        "_health_payload",
        lambda host, port: {"status": "ok", "ocr": "disabled", "ocr_worker": "not_started"},
    )
    monkeypatch.setattr(
        onboarding,
        "_runtime_permissions",
        lambda host, port: {"accessibility": "granted", "screen_recording": "denied"},
    )
    monkeypatch.setattr(
        onboarding,
        "_request_runtime_capture",
        lambda host, port: onboarding.CaptureRequestProof(None, "privacy", "privacy-locked"),
    )

    proof = onboarding.ensure_runtime(
        restart=False,
        require_ocr=False,
        preserve_policy=True,
        expected_ocr_state="disabled",
    )

    assert proof.receipt == "privacy-locked"


def test_preserve_policy_accepts_live_ingest_readiness_after_unlock(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config.Config()
    cfg.capture.source = "ingest"
    monkeypatch.setattr(config, "load", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(scheduler, "capture_gate_reason", lambda capture: "locked")
    monkeypatch.setattr(onboarding, "_running_daemon_pid", lambda: 4242)
    monkeypatch.setattr(
        onboarding,
        "_health_payload",
        lambda host, port: {"status": "ok", "ocr": "disabled", "ocr_worker": "not_started"},
    )
    monkeypatch.setattr(
        onboarding,
        "_runtime_permissions",
        lambda host, port: {
            "accessibility": "not_applicable",
            "screen_recording": "not_applicable",
        },
    )
    monkeypatch.setattr(
        onboarding,
        "_request_runtime_capture",
        lambda host, port: onboarding.CaptureRequestProof(None, "ingest", "ingest-ready"),
    )

    proof = onboarding.ensure_runtime(
        restart=False,
        require_ocr=False,
        preserve_policy=True,
        expected_ocr_state="disabled",
    )

    assert proof.receipt == "ingest-ready"


def test_strict_proof_reports_privacy_gate_race_actionably(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config.Config()
    monkeypatch.setattr(config, "load", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(onboarding, "_running_daemon_pid", lambda: 4242)
    monkeypatch.setattr(
        onboarding,
        "_health_payload",
        lambda host, port: {"status": "ok", "ocr": "disabled", "ocr_worker": "not_started"},
    )
    monkeypatch.setattr(
        onboarding,
        "_runtime_permissions",
        lambda host, port: {"accessibility": "granted", "screen_recording": "denied"},
    )
    monkeypatch.setattr(
        onboarding,
        "_request_runtime_capture",
        lambda host, port: onboarding.CaptureRequestProof(None, "privacy", "privacy-locked"),
    )

    with pytest.raises(onboarding.OnboardingError, match="became locked.*unlock the screen"):
        onboarding.ensure_runtime(
            restart=False,
            require_ocr=False,
            preserve_policy=False,
            expected_ocr_state="disabled",
        )


def test_non_http_runtime_uses_generation_state_and_startup_capture(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config.Config()
    cfg.mcp.auto_start = False
    cfg.capture.enable_ocr_fallback = True
    monkeypatch.setattr(config, "load", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(scheduler, "capture_gate_reason", lambda capture: None)
    capture_path = paths.capture_buffer_dir() / "fresh.json"
    capture_path.write_text(
        '{"timestamp":"2026-07-12T00:00:00+00:00","visible_text":"ready"}',
        encoding="utf-8",
    )
    live = {"pid": None}
    monkeypatch.setattr(onboarding, "_running_daemon_pid", lambda: live["pid"])

    def run_cli(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args == ("start",)
        live["pid"] = 4242
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(onboarding, "_run_cli", run_cli)
    monkeypatch.setattr(
        onboarding,
        "_health_payload",
        lambda *args: pytest.fail("HTTP is intentionally disabled"),
    )
    monkeypatch.setattr(
        onboarding,
        "_runtime_state",
        lambda pid, **kwargs: {
            "generation": "b" * 32,
            "phase": "ready",
            "permissions": {"accessibility": "granted", "screen_recording": "granted"},
            "ocr_worker": "ready",
            "ocr": "ready",
            "ocr_enabled": True,
            "ocr_tier": "tiny",
            "last_capture_id": "fresh",
            "last_capture_reason": "heartbeat",
        },
    )

    proof = onboarding.ensure_runtime(restart=False, require_ocr=True)

    assert proof.pid == 4242
    assert proof.generation == "b" * 32
    assert proof.capture_path == capture_path


def test_ingest_runtime_returns_runner_readiness_without_fake_capture(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config.Config()
    cfg.capture.source = "ingest"
    monkeypatch.setattr(config, "load", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(onboarding, "_running_daemon_pid", lambda: 4242)
    monkeypatch.setattr(
        onboarding,
        "_health_payload",
        lambda host, port: {"status": "ok", "ocr": "disabled", "ocr_worker": "not_started"},
    )
    monkeypatch.setattr(
        onboarding,
        "_runtime_permissions",
        lambda host, port: {
            "accessibility": "not_applicable",
            "screen_recording": "not_applicable",
        },
    )
    monkeypatch.setattr(
        onboarding,
        "_request_runtime_capture",
        lambda host, port: onboarding.CaptureRequestProof(None, "ingest", "ingest-ready"),
    )
    monkeypatch.setattr(onboarding, "_runtime_state", lambda *args, **kwargs: None)

    proof = onboarding.ensure_runtime(
        restart=False,
        require_ocr=False,
        preserve_policy=True,
        expected_ocr_state="disabled",
    )

    assert proof.mode == "ingest"
    assert proof.receipt == "ingest-ready"
    assert proof.capture_path is None


def test_permission_auth_failure_is_actionable_without_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PERSOME_LOCAL_API_TOKEN", "x" * 32)
    response = httpx.Response(
        401,
        request=httpx.Request("GET", "http://127.0.0.1:8742/permissions"),
    )
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: response)

    with pytest.raises(onboarding.OnboardingError, match="stale shell override"):
        onboarding._runtime_permissions("127.0.0.1", 8742)


def test_onboarding_loopback_url_supports_ipv6() -> None:
    assert onboarding._local_api_url("::1", 8742, "/health") == "http://[::1]:8742/health"


def test_new_screen_recording_grant_restarts_existing_daemon(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config.Config()
    monkeypatch.setattr(config, "load", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(onboarding.sys, "platform", "darwin")
    monkeypatch.setattr(onboarding, "OnboardingUI", lambda gui: ScriptedUI())
    monkeypatch.setattr(onboarding, "ensure_accessibility", lambda ui, *, event_driven: False)
    monkeypatch.setattr(
        onboarding,
        "ensure_local_ocr",
        lambda **kwargs: onboarding.OCRPolicyProof(
            True,
            False,
            True,
            "tiny",
            "ready",
        ),
    )
    seen: dict[str, object] = {}
    proof = onboarding.RuntimeProof(4242, "ok", "ready", ac_root / "fresh.json")

    def runtime(**kwargs: object) -> onboarding.RuntimeProof:
        seen.update(kwargs)
        return proof

    monkeypatch.setattr(onboarding, "ensure_runtime", runtime)

    assert onboarding.onboard() == proof
    assert seen["restart"] is True


def test_launchagent_restart_accepts_direct_old_to_new_pid_handoff(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture_path = paths.capture_buffer_dir() / "fresh.json"
    capture_path.write_text(
        '{"timestamp":"2026-07-12T00:00:00+00:00","visible_text":"ready"}',
        encoding="utf-8",
    )
    first = {"value": True}

    def pid() -> int:
        if first["value"]:
            first["value"] = False
            return 100
        return 200

    monkeypatch.setattr(onboarding, "_running_daemon_pid", pid)
    monkeypatch.setattr(onboarding, "_launchagent_is_loaded", lambda: True)
    kicks: list[bool] = []
    monkeypatch.setattr(
        onboarding,
        "_kickstart_launchagent",
        lambda *, kill: kicks.append(kill),
    )
    monkeypatch.setattr(
        onboarding,
        "_run_cli",
        lambda *args, **kwargs: pytest.fail("launchd owns this lifecycle"),
    )
    monkeypatch.setattr(
        onboarding,
        "_health_payload",
        lambda host, port: {
            "status": "ok",
            "ocr": "ready",
            "ocr_worker": "ready",
            "ocr_enabled": "True",
            "ocr_tier": "tiny",
        },
    )
    monkeypatch.setattr(
        onboarding,
        "_runtime_permissions",
        lambda host, port: {"accessibility": "granted", "screen_recording": "granted"},
    )
    monkeypatch.setattr(
        onboarding,
        "_request_runtime_capture",
        lambda host, port: onboarding.CaptureRequestProof(capture_path, "daemon", "fresh-capture"),
    )
    monkeypatch.setattr(onboarding, "_runtime_state", lambda *args, **kwargs: None)

    proof = onboarding.ensure_runtime(
        restart=True,
        expected_ocr_state="ready",
        expected_ocr_tier="tiny",
        expected_ocr_enabled=True,
    )

    assert proof.pid == 200
    assert kicks == [True]


def test_launchagent_non_http_existing_runtime_restarts_for_fresh_generation(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config.Config()
    cfg.mcp.auto_start = False
    cfg.capture.enable_ocr_fallback = True
    monkeypatch.setattr(config, "load", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(scheduler, "capture_gate_reason", lambda capture: None)
    capture_path = paths.capture_buffer_dir() / "fresh.json"
    capture_path.write_text(
        '{"timestamp":"2026-07-12T00:00:00+00:00","visible_text":"ready"}',
        encoding="utf-8",
    )
    pids = iter([100, 200])
    current = {"pid": 200}

    def pid() -> int:
        return next(pids, current["pid"])

    monkeypatch.setattr(onboarding, "_running_daemon_pid", pid)
    monkeypatch.setattr(onboarding, "_launchagent_is_loaded", lambda: True)
    kicks: list[bool] = []
    monkeypatch.setattr(
        onboarding,
        "_kickstart_launchagent",
        lambda *, kill: kicks.append(kill),
    )
    state = {
        "generation": "c" * 32,
        "phase": "ready",
        "permissions": {"accessibility": "granted", "screen_recording": "granted"},
        "ocr_worker": "ready",
        "ocr": "ready",
        "ocr_enabled": True,
        "ocr_tier": "tiny",
        "last_capture_id": "fresh",
        "last_capture_reason": "startup",
    }
    monkeypatch.setattr(onboarding, "_runtime_state", lambda *args, **kwargs: state)

    proof = onboarding.ensure_runtime(
        restart=False,
        expected_ocr_state="ready",
        expected_ocr_tier="tiny",
        expected_ocr_enabled=True,
    )

    assert proof.pid == 200
    assert proof.generation == "c" * 32
    assert kicks == [True]


def test_ingest_without_http_transport_is_rejected_before_lifecycle_mutation(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config.Config()
    cfg.capture.source = "ingest"
    cfg.mcp.auto_start = False
    monkeypatch.setattr(config, "load", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(
        onboarding,
        "_running_daemon_pid",
        lambda: pytest.fail("invalid configuration must fail before starting a daemon"),
    )

    with pytest.raises(onboarding.OnboardingError, match="requires mcp.auto_start=true"):
        onboarding.ensure_runtime(restart=False, require_ocr=False)


def test_pixel_and_ocr_opt_out_does_not_probe_screen_recording(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config.Config()
    cfg.capture.enable_ocr_fallback = False
    cfg.capture.include_screenshot = False
    monkeypatch.setattr(config, "load", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(
        screen_recording,
        "has_screen_recording",
        lambda: pytest.fail("pixel opt-out must not probe Screen Recording"),
    )
    monkeypatch.setattr(
        ocr_local,
        "runtime_available",
        lambda: pytest.fail("OCR opt-out must not inspect Paddle"),
    )

    proof = onboarding.ensure_local_ocr(
        tier="tiny",
        ui=ScriptedUI(),
        preserve_policy=True,
        screenshot_permission_required=False,
        capture_source="daemon",
    )

    assert proof.state == "disabled"
    assert proof.permission_changed is False


def test_screenshot_policy_still_proves_screen_recording_when_ocr_is_disabled(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config.Config()
    cfg.capture.enable_ocr_fallback = False
    cfg.capture.include_screenshot = True
    monkeypatch.setattr(config, "load", lambda *args, **kwargs: cfg)
    checked: list[str] = []
    monkeypatch.setattr(
        screen_recording,
        "has_screen_recording",
        lambda: checked.append("screen") or True,
    )

    proof = onboarding.ensure_local_ocr(
        tier="tiny",
        ui=ScriptedUI(),
        preserve_policy=True,
        screenshot_permission_required=True,
        capture_source="daemon",
    )

    assert proof.state == "disabled"
    assert checked == ["screen"]


def test_preserve_policy_restarts_daemon_using_a_different_ocr_policy(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config.Config()
    cfg.capture.enable_ocr_fallback = False
    cfg.capture.include_screenshot = False
    monkeypatch.setattr(config, "load", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(scheduler, "capture_gate_reason", lambda capture: None)
    capture_path = paths.capture_buffer_dir() / "fresh.json"
    capture_path.write_text(
        '{"timestamp":"2026-07-12T00:00:00+00:00","visible_text":"ready"}',
        encoding="utf-8",
    )
    live = {"pid": 100}
    commands: list[str] = []

    def run_cli(command: str, *args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        live["pid"] = None if command == "stop" else 200
        return subprocess.CompletedProcess([command], 0, "", "")

    monkeypatch.setattr(onboarding, "_running_daemon_pid", lambda: live["pid"])
    monkeypatch.setattr(onboarding, "_run_cli", run_cli)

    def health(host: str, port: int) -> dict[str, str]:
        if live["pid"] == 100:
            return {
                "status": "ok",
                "ocr": "ready",
                "ocr_worker": "ready",
                "ocr_enabled": "True",
                "ocr_tier": "small",
            }
        return {
            "status": "ok",
            "ocr": "disabled",
            "ocr_worker": "not_started",
            "ocr_enabled": "False",
            "ocr_tier": "tiny",
        }

    monkeypatch.setattr(onboarding, "_health_payload", health)
    monkeypatch.setattr(
        onboarding,
        "_runtime_permissions",
        lambda host, port: {"accessibility": "granted", "screen_recording": "denied"},
    )
    monkeypatch.setattr(
        onboarding,
        "_request_runtime_capture",
        lambda host, port: onboarding.CaptureRequestProof(capture_path, "daemon", "fresh-capture"),
    )
    monkeypatch.setattr(onboarding, "_runtime_state", lambda *args, **kwargs: None)

    proof = onboarding.ensure_runtime(
        restart=False,
        require_ocr=False,
        require_screen_recording=False,
        preserve_policy=True,
        expected_ocr_state="disabled",
        expected_ocr_tier="tiny",
        expected_ocr_enabled=False,
    )

    assert proof.pid == 200
    assert commands == ["stop", "start"]


def test_non_http_proof_rechecks_worker_after_capture(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config.Config()
    cfg.mcp.auto_start = False
    cfg.capture.enable_ocr_fallback = True
    monkeypatch.setattr(config, "load", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(scheduler, "capture_gate_reason", lambda capture: None)
    capture_path = paths.capture_buffer_dir() / "fresh.json"
    capture_path.write_text(
        '{"timestamp":"2026-07-12T00:00:00+00:00","visible_text":"ready"}',
        encoding="utf-8",
    )
    live = {"pid": None}
    state = {
        "generation": "d" * 32,
        "phase": "ready",
        "permissions": {"accessibility": "granted", "screen_recording": "granted"},
        "ocr_worker": "ready",
        "ocr": "ready",
        "ocr_enabled": True,
        "ocr_tier": "tiny",
        "last_capture_id": "fresh",
        "last_capture_reason": "startup",
    }
    monkeypatch.setattr(onboarding, "_running_daemon_pid", lambda: live["pid"])
    monkeypatch.setattr(
        onboarding,
        "_run_cli",
        lambda *args, **kwargs: (
            live.__setitem__("pid", 4242) or subprocess.CompletedProcess(args, 0, "", "")
        ),
    )
    monkeypatch.setattr(onboarding, "_runtime_state", lambda *args, **kwargs: state)

    def verify_context(path: Path, **kwargs: object) -> bool:
        state["ocr_worker"] = "failed"
        return True

    monkeypatch.setattr(onboarding, "_capture_has_real_context", verify_context)

    with pytest.raises(onboarding.OnboardingError, match="lost readiness"):
        onboarding.ensure_runtime(
            restart=False,
            expected_ocr_state="ready",
            expected_ocr_tier="tiny",
            expected_ocr_enabled=True,
        )


def test_cli_reports_degraded_ax_only_runtime_truthfully(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture_path = ac_root / "capture-buffer" / "fresh.json"
    monkeypatch.setattr(
        onboarding,
        "onboard",
        lambda tier, gui, preserve_policy, expected_owner: onboarding.RuntimeProof(
            4242,
            "degraded",
            "runtime_unavailable",
            capture_path,
            accessibility="granted",
            screen_recording="granted",
        ),
    )

    result = CliRunner().invoke(app, ["onboard"])

    assert result.exit_code == 0, result.output
    assert "degraded optional features" in result.output
    assert "running and healthy" not in result.output

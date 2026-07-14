"""Interactive macOS permission onboarding and runtime proof.

The installer delegates here so it cannot claim success after merely printing
privacy instructions. Each sensitive request gets a separate, plain-language
native dialog. Standard daemon mode requires live Accessibility and Screen
Recording grants, a working isolated OCR worker, a healthy final owner, and a
fresh capture; supported alternate policies return explicit mode-aware receipts.
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from . import paths

PermissionAction = Literal["cancel", "open_settings", "check_again"]
RuntimeOwner = Literal["any", "launchagent", "background"]

_CONFIRM_SCRIPT = """
on run argv
    set dialogTitle to item 1 of argv
    set dialogMessage to item 2 of argv
    set actionLabel to item 3 of argv
    tell current application to activate
    try
        set chosen to button returned of (display dialog dialogMessage with title dialogTitle buttons {"Cancel", actionLabel} default button actionLabel cancel button "Cancel" with icon note)
        return chosen
    on error number -128
        return "Cancel"
    end try
end run
"""

_WAIT_SCRIPT = """
on run argv
    set dialogTitle to item 1 of argv
    set dialogMessage to item 2 of argv
    tell current application to activate
    try
        set chosen to button returned of (display dialog dialogMessage with title dialogTitle buttons {"Cancel", "Open Settings", "Check Again"} default button "Check Again" cancel button "Cancel" with icon caution)
        return chosen
    on error number -128
        return "Cancel"
    end try
end run
"""

_NOTIFICATION_SCRIPT = """
on run argv
    display notification (item 2 of argv) with title (item 1 of argv)
    return "Notified"
end run
"""


class OnboardingError(RuntimeError):
    """The required onboarding proof could not be completed."""


class OnboardingCancelled(OnboardingError):
    """The user explicitly cancelled a permission request."""


class PermissionUI(Protocol):
    def status(self, message: str) -> None: ...

    def confirm(self, *, title: str, message: str, action: str) -> bool: ...

    def wait_for_permission(self, *, title: str, message: str) -> PermissionAction: ...


class OnboardingUI:
    """Native macOS dialogs with a terminal fallback for remote shells."""

    def __init__(self, *, gui: bool = True) -> None:
        self.gui = gui and sys.platform == "darwin" and shutil.which("osascript") is not None

    def _osascript(self, script: str, *args: str, timeout: float | None = None) -> str | None:
        if not self.gui:
            return None
        try:
            result = subprocess.run(
                ["osascript", "-e", script, *args],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    @staticmethod
    def _terminal_confirm(title: str, message: str, action: str) -> bool:
        print(f"\n{title}\n{message}")
        if not sys.stdin.isatty():
            return False
        reply = input(f"{action}? [Y/n] ").strip().lower()
        return reply in {"", "y", "yes"}

    def confirm(self, *, title: str, message: str, action: str) -> bool:
        result = self._osascript(_CONFIRM_SCRIPT, title, message, action, timeout=300)
        if result is not None:
            return result == action
        return self._terminal_confirm(title, message, action)

    @staticmethod
    def status(message: str) -> None:
        print(message, flush=True)

    def wait_for_permission(self, *, title: str, message: str) -> PermissionAction:
        result = self._osascript(_WAIT_SCRIPT, title, message, timeout=300)
        if result == "Open Settings":
            return "open_settings"
        if result == "Check Again":
            return "check_again"
        if result is not None:
            return "cancel"

        print(f"\n{title}\n{message}")
        if not sys.stdin.isatty():
            return "cancel"
        reply = input("[O]pen Settings, [C]heck Again, or [Q]uit? ").strip().lower()
        if reply in {"o", "open"}:
            return "open_settings"
        if reply in {"", "c", "check"}:
            return "check_again"
        return "cancel"

    def success(self, message: str) -> None:
        # Completion must never hold the CLI open behind a modal dialog. Keep
        # the terminal authoritative and use a best-effort notification only.
        print(f"\nPersome is ready\n{message}", flush=True)
        self._osascript(_NOTIFICATION_SCRIPT, "Persome is ready", message, timeout=5)


def _open_privacy_settings(pane: str) -> None:
    if sys.platform != "darwin":
        return
    subprocess.run(
        ["open", f"x-apple.systempreferences:com.apple.preference.security?{pane}"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def open_accessibility_settings() -> None:
    _open_privacy_settings("Privacy_Accessibility")


def open_screen_recording_settings() -> None:
    _open_privacy_settings("Privacy_ScreenCapture")


def _ensure_permission(
    *,
    label: str,
    check: Callable[[], bool],
    request: Callable[[], bool],
    open_settings: Callable[[], None],
    ui: PermissionUI,
    explanation: str,
    settings_label: str | None = None,
    entry_name: str = "Persome",
) -> bool:
    """Request one permission and return whether it was newly granted."""
    ui.status(f"Checking {label} permission...")
    if check():
        ui.status(f"✓ {label} already granted")
        return False
    if not ui.confirm(
        title=f"Persome needs {label}",
        message=explanation,
        action=f"Request {label}",
    ):
        raise OnboardingCancelled(f"{label} request was cancelled")

    ui.status(f"Requesting {label} from macOS...")
    request()
    pane_label = settings_label or label
    while not check():
        action = ui.wait_for_permission(
            title=f"Finish {label} setup",
            message=(
                f"Enable {entry_name} under Privacy & Security -> {pane_label}. "
                "Return here and choose Check Again. Persome will not continue until macOS "
                "reports that the permission is granted."
            ),
        )
        if action == "cancel":
            raise OnboardingCancelled(f"{label} was not granted")
        if action == "open_settings":
            open_settings()
    ui.status(f"✓ {label} granted")
    return True


def ensure_accessibility(ui: PermissionUI, *, event_driven: bool = True) -> bool:
    from .capture import ax_capture, watcher

    helper_changed = _ensure_permission(
        label="Capture Helper Accessibility",
        check=lambda: ax_capture.ax_trusted(refresh=True, include_watcher=False),
        request=lambda: ax_capture.request_accessibility_permission(include_watcher=False),
        open_settings=open_accessibility_settings,
        ui=ui,
        explanation=(
            "Persome's bundled capture helper reads the focused app's visible text and "
            "structure. It cannot type, click, or control your Mac. macOS will show one "
            "permission request for this helper next."
        ),
        settings_label="Accessibility",
        entry_name="mac-ax-helper",
    )
    if not event_driven:
        return helper_changed

    def watcher_trusted() -> bool:
        binary = watcher._resolve_watcher_path()
        return bool(binary is not None and ax_capture._binary_ax_trusted(binary))

    watcher_changed = _ensure_permission(
        label="Event Watcher Accessibility",
        check=watcher_trusted,
        request=watcher.request_accessibility_permission,
        open_settings=open_accessibility_settings,
        ui=ui,
        explanation=(
            "Persome's bundled event watcher notices window and typing-context changes so "
            "capture can react without polling constantly. It cannot type, click, or control "
            "your Mac. macOS will show a separate request for this watcher next."
        ),
        settings_label="Accessibility",
        entry_name="mac-ax-watcher",
    )
    return helper_changed or watcher_changed


@dataclass(frozen=True)
class OCRPolicyProof:
    require_worker: bool
    config_changed: bool
    permission_changed: bool
    tier: str
    state: str


def ensure_local_ocr(
    *,
    tier: str | None,
    ui: PermissionUI,
    preserve_policy: bool = False,
    screenshot_permission_required: bool = True,
    capture_source: str = "daemon",
) -> OCRPolicyProof:
    """Verify the effective OCR policy without silently changing it on updates."""
    from .capture import ocr_health, ocr_local, screen_recording
    from .config import load
    from .ocr_setup import VALID_TIERS, save_ocr_config

    before = load().capture
    effective_tier = (
        before.ocr_tier
        if preserve_policy or (tier is None and before.enable_ocr_fallback)
        else tier or before.ocr_tier or "tiny"
    )
    ui.status(f"Checking bundled local OCR ({effective_tier})...")

    require_worker = True
    fallback_state: str | None = None
    fallback_message: str | None = None
    explicit_opt_out = before.ocr_policy == "disabled" and tier is None
    if (preserve_policy or explicit_opt_out) and not before.enable_ocr_fallback:
        require_worker = False
        fallback_state = "disabled"
        fallback_message = "disabled"
    elif preserve_policy and ocr_local.disabled_by_environment():
        require_worker = False
        fallback_state = "disabled_by_environment"
        fallback_message = "disabled_by_environment"
    runtime_available = ocr_local.runtime_available() if require_worker else False
    intel_without_runtime = platform.machine().lower() in {"x86_64", "amd64"}
    if require_worker and not runtime_available and intel_without_runtime:
        require_worker = False
        fallback_state = "runtime_unavailable" if before.enable_ocr_fallback else "disabled"
        fallback_message = "runtime_unavailable"

    if require_worker and effective_tier not in VALID_TIERS:
        raise OnboardingError(
            f"unsupported OCR tier {effective_tier!r}: choose {', '.join(VALID_TIERS)}"
        )
    if require_worker and ocr_local.disabled_by_environment():
        raise OnboardingError("OCR is disabled by PERSOME_DISABLE_OCR")
    if require_worker and not runtime_available:
        raise OnboardingError("the local Paddle OCR runtime is unavailable on this architecture")
    if require_worker and not ocr_local.models_available(effective_tier):
        raise OnboardingError(f"bundled PP-OCRv6 {effective_tier} model weights are missing")

    permission_changed = False
    require_screen_recording = bool(
        capture_source == "daemon" and (screenshot_permission_required or require_worker)
    )
    if require_screen_recording:
        permission_changed = _ensure_permission(
            label="Screen Recording",
            check=screen_recording.has_screen_recording,
            request=screen_recording.request_screen_recording,
            open_settings=open_screen_recording_settings,
            ui=ui,
            explanation=(
                "Persome uses Screen Recording for locally encrypted screenshots and, when "
                "enabled, bundled PP-OCRv6 when an app's Accessibility text is incomplete. "
                "Pixels are not sent to an LLM or uploaded. macOS will show its own permission "
                "request next."
            ),
            entry_name="the Persome Runtime",
        )
    elif capture_source == "ingest":
        ui.status("✓ Screen pixels are owned by the trusted ingest producer")
    else:
        ui.status("✓ Screen pixel capture is disabled by policy")

    if not require_worker:
        messages = {
            "disabled": "✓ Preserving the configured OCR opt-out",
            "disabled_by_environment": "✓ Preserving the PERSOME_DISABLE_OCR safety policy",
            "runtime_unavailable": "✓ Local OCR is unavailable on this architecture",
        }
        assert fallback_state is not None
        if preserve_policy and not before.enable_ocr_fallback and before.ocr_policy == "auto":
            # Older releases had no explicit-intent field. An update must not
            # later let ordinary repeated onboarding reinterpret the preserved
            # disabled policy as a fresh auto-enable request.
            save_ocr_config(
                enabled=False,
                tier=effective_tier,
                config_path=paths.config_file(),
                policy="disabled",
            )
        ui.status(
            f"{messages[fallback_message or fallback_state]}; "
            "Accessibility capture remains available"
        )
        return OCRPolicyProof(
            False,
            False,
            permission_changed,
            effective_tier,
            fallback_state,
        )

    changed = not before.enable_ocr_fallback or before.ocr_tier != effective_tier
    if not preserve_policy:
        save_ocr_config(
            enabled=True,
            tier=effective_tier,
            config_path=paths.config_file(),
            policy="enabled",
        )
    health = ocr_health.inspect(load().capture)
    if not health.ready:
        raise OnboardingError(f"local OCR verification failed: {health.state}: {health.detail}")
    ui.status("✓ Local OCR prerequisites ready")
    return OCRPolicyProof(
        True,
        changed and not preserve_policy,
        permission_changed,
        effective_tier,
        health.state,
    )


@dataclass(frozen=True)
class RuntimeProof:
    pid: int
    health: str
    ocr: str
    capture_path: Path | None
    mode: str = "daemon"
    receipt: str = "fresh-capture"
    generation: str = ""
    accessibility: str = "not_applicable"
    screen_recording: str = "not_applicable"
    owner: str = "background"


def _running_daemon_pid() -> int | None:
    from . import runtime_pid

    process = runtime_pid.resolve_recorded_process()
    return process.pid if process is not None else None


def _launchagent_is_loaded() -> bool:
    from . import launchagent

    return launchagent.is_loaded()


def _select_runtime_owner(expected_owner: RuntimeOwner, *, ui: PermissionUI | None) -> str:
    """Resolve lifecycle intent and restore launchd ownership when required."""
    from . import launchagent

    if expected_owner not in {"any", "launchagent", "background"}:
        raise OnboardingError(
            "invalid Runtime owner expectation; use any, launchagent, or background"
        )
    loaded = _launchagent_is_loaded()
    owner = (
        "launchagent"
        if expected_owner == "any" and (loaded or launchagent.owner_intended())
        else "background"
        if expected_owner == "any"
        else expected_owner
    )
    if owner == "background" and loaded:
        raise OnboardingError(
            "the Persome LaunchAgent is loaded, so a background-owned Runtime cannot be proved"
        )
    if owner == "launchagent":
        binary = launchagent.configured_runtime_binary()
        if binary is None:
            raise OnboardingError(
                "launchd ownership is expected, but its configured Persome binary is missing; "
                "rerun the installer"
            )
        if not loaded or not launchagent.owns_recorded_runtime(binary):
            if ui is not None:
                ui.status("Restoring launchd ownership of the Persome Runtime...")
            try:
                launchagent.install(binary)
            except RuntimeError as exc:
                raise OnboardingError(
                    f"could not restore launchd Runtime ownership: {exc}"
                ) from exc
        if not _launchagent_is_loaded():
            raise OnboardingError("launchd did not retain the Persome Runtime job")
    return owner


def _prove_runtime_owner(pid: int, expected_owner: str) -> str:
    """Bind the final generation to its required lifecycle owner."""
    from . import launchagent

    if _running_daemon_pid() != pid:
        raise OnboardingError("the final Runtime generation no longer matches its process")
    loaded = _launchagent_is_loaded()
    if expected_owner == "launchagent":
        binary = launchagent.configured_runtime_binary()
        if binary is None or not loaded or not launchagent.owns_recorded_runtime(binary):
            raise OnboardingError(
                "the healthy Runtime is not owned by the configured Persome LaunchAgent"
            )
        return "launchagent"
    if loaded:
        raise OnboardingError(
            "launchd owns the healthy Runtime, but background ownership was required"
        )
    return "background"


def _kickstart_launchagent(*, kill: bool) -> None:
    from . import launchagent

    try:
        result = launchagent.kickstart(kill=kill)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OnboardingError(f"could not restart the launchd-owned Runtime: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "launchctl kickstart failed"
        raise OnboardingError(f"could not restart the launchd-owned Runtime: {detail}")


def _cli_prefix() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "persome"]


def _run_cli(*args: str, timeout: float = 180.0) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [*_cli_prefix(), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        command = " ".join(("persome", *args))
        raise OnboardingError(f"`{command}` timed out after {timeout:.0f} seconds") from exc


def _local_api_url(host: str, port: int, path: str) -> str:
    from .security.auth import LocalAPIConfigurationError, loopback_http_url

    try:
        return loopback_http_url(host, port, path)
    except LocalAPIConfigurationError as exc:
        raise OnboardingError(str(exc)) from exc


def _local_api_headers() -> dict[str, str]:
    from .security.auth import LocalAPIConfigurationError, auth_headers

    try:
        return auth_headers()
    except LocalAPIConfigurationError as exc:
        raise OnboardingError(str(exc)) from exc


def _health_payload(host: str, port: int) -> dict[str, str] | None:
    import httpx

    url = _local_api_url(host, port, "/health")
    try:
        response = httpx.get(url, timeout=2.0)
        response.raise_for_status()
        data = response.json().get("data")
    except (httpx.RequestError, httpx.HTTPStatusError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return {str(key): str(value) for key, value in data.items()}


def _runtime_permissions(host: str, port: int) -> dict[str, str] | None:
    """Read live probes for the Runtime and its configured native helpers."""
    import httpx

    try:
        response = httpx.get(
            _local_api_url(host, port, "/permissions"),
            headers=_local_api_headers(),
            timeout=2.0,
        )
        response.raise_for_status()
        data = response.json().get("data")
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in {401, 403}:
            raise OnboardingError(
                "the running Runtime rejected PERSOME_LOCAL_API_TOKEN; clear any stale shell "
                "override and rerun onboarding"
            ) from exc
        if status == 404:
            raise OnboardingError(
                "the running Runtime does not expose the permission proof endpoint; restart "
                "it with the current Persome version"
            ) from exc
        if status == 503:
            return None
        raise OnboardingError(f"permission proof failed with HTTP {status}") from exc
    except (httpx.RequestError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return {str(key): str(value) for key, value in data.items()}


@dataclass(frozen=True)
class CaptureRequestProof:
    path: Path | None
    mode: str
    receipt: str


def _request_runtime_capture(host: str, port: int) -> CaptureRequestProof | None:
    """Ask the daemon-owned runner for a fresh capture without a second writer."""
    import httpx

    try:
        response = httpx.post(
            _local_api_url(host, port, "/_onboarding/capture"),
            headers=_local_api_headers(),
            timeout=45.0,
        )
        if response.status_code == 503:
            return None
        response.raise_for_status()
        data = response.json().get("data")
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in {401, 403}:
            raise OnboardingError(
                "the running Runtime rejected PERSOME_LOCAL_API_TOKEN; clear any stale shell "
                "override and rerun onboarding"
            ) from exc
        if status == 404:
            raise OnboardingError(
                "the running Runtime does not expose the fresh-capture proof endpoint; "
                "restart it with the current Persome version"
            ) from exc
        if status == 409:
            return CaptureRequestProof(None, "privacy", "privacy-paused")
        if status == 423:
            return CaptureRequestProof(None, "privacy", "privacy-locked")
        raise OnboardingError(
            f"the Runtime rejected the fresh-capture proof (HTTP {status})"
        ) from exc
    except (httpx.RequestError, ValueError) as exc:
        raise OnboardingError(f"the fresh-capture request failed: {exc}") from exc
    if not isinstance(data, dict):
        raise OnboardingError("the Runtime returned an invalid fresh-capture receipt")
    mode = data.get("mode")
    receipt = data.get("receipt")
    capture_id = data.get("id")
    if mode == "ingest" and receipt == "ingest-ready" and capture_id is None:
        return CaptureRequestProof(None, mode, receipt)
    if mode != "daemon" or receipt != "fresh-capture" or not isinstance(capture_id, str):
        raise OnboardingError("the Runtime returned an invalid fresh-capture receipt")
    if not capture_id or Path(capture_id).name != capture_id:
        raise OnboardingError("the Runtime returned an unsafe fresh-capture id")
    return CaptureRequestProof(
        paths.capture_buffer_dir() / f"{capture_id}.json",
        mode,
        receipt,
    )


def _runtime_state(
    pid: int,
    *,
    minimum_updated_at: float | None = None,
) -> dict[str, object] | None:
    """Read and validate the current daemon generation's owner-only state."""
    from . import runtime_pid

    generation = runtime_pid.read_runtime_generation()
    if generation is None or generation.pid != pid:
        return None
    if minimum_updated_at is not None and generation.updated_at < minimum_updated_at - 1:
        return None
    state_path = paths.runtime_state_file()
    if state_path.is_symlink():
        return None
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if (
        payload.get("schema_version") != 1
        or payload.get("pid") != pid
        or payload.get("generation") != generation.generation
        or payload.get("started_at") != generation.started_at
        or payload.get("capture_mode") not in {"daemon", "ingest"}
        or payload.get("ocr_worker") not in {"not_started", "warming", "ready", "failed"}
        or payload.get("phase") not in {"starting", "ready"}
        or payload.get("ocr")
        not in {
            "ready",
            "disabled",
            "disabled_by_environment",
            "runtime_unavailable",
            "models_missing",
            "permission_required",
        }
        or not isinstance(payload.get("ocr_enabled"), bool)
        or not isinstance(payload.get("ocr_tier"), str)
        or not isinstance(payload.get("permissions"), dict)
    ):
        return None
    return payload


def _capture_path_from_id(capture_id: object) -> Path | None:
    if not isinstance(capture_id, str) or not capture_id or Path(capture_id).name != capture_id:
        return None
    return paths.capture_buffer_dir() / f"{capture_id}.json"


def _capture_has_real_context(path: Path, *, wait_for_ocr: float = 10.0) -> bool:
    """Require actual AX/text/pixels or completed OCR, never a queued OCR flag."""
    deadline = time.monotonic() + max(0.0, wait_for_ocr)
    while True:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise OnboardingError(f"fresh capture record is unreadable: {exc}") from exc
        if not isinstance(record, dict):
            raise OnboardingError("fresh capture record is not a JSON object")
        if not record.get("timestamp"):
            raise OnboardingError("fresh capture record has no timestamp")

        focused = record.get("focused_element")
        screenshot = record.get("screenshot")
        ax_tree = record.get("ax_tree")
        if (
            bool(str(record.get("visible_text") or "").strip())
            or (isinstance(focused, dict) and any(focused.get(key) for key in ("role", "value")))
            or (isinstance(ax_tree, dict) and bool(ax_tree.get("apps")))
            or (isinstance(screenshot, dict) and bool(screenshot.get("image_base64")))
        ):
            return True

        if record.get("ocr_submitted"):
            try:
                from .store import fts

                with fts.cursor() as conn:
                    if fts.get_ocr_result_for_capture(conn, path.stem):
                        return True
            except Exception as exc:  # noqa: BLE001
                raise OnboardingError(f"fresh capture OCR receipt is unreadable: {exc}") from exc

        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)


def ensure_runtime(
    *,
    restart: bool,
    timeout: float = 150.0,
    ui: PermissionUI | None = None,
    require_ocr: bool = True,
    require_screen_recording: bool | None = None,
    preserve_policy: bool = False,
    expected_ocr_state: str = "ready",
    expected_ocr_tier: str | None = None,
    expected_ocr_enabled: bool | None = None,
    expected_owner: RuntimeOwner = "any",
) -> RuntimeProof:
    """Leave the daemon running and prove its effective mode without policy drift."""
    from .capture import scheduler
    from .config import load

    cfg = load()
    http_enabled = bool(cfg.mcp.auto_start and cfg.mcp.transport in {"sse", "streamable-http"})
    if cfg.capture.source == "ingest" and not http_enabled:
        raise OnboardingError(
            "capture.source='ingest' requires mcp.auto_start=true with the streamable-http "
            "transport so the authenticated /captures/ingest endpoint exists"
        )
    screen_recording_required = (
        require_ocr if require_screen_recording is None else require_screen_recording
    )
    privacy_gate = scheduler.capture_gate_reason(cfg.capture)
    if privacy_gate and not preserve_policy:
        action = "run `persome resume`" if privacy_gate == "paused" else "unlock the screen"
        raise OnboardingError(
            f"capture proof is blocked because capture is {privacy_gate}; {action}, then retry"
        )

    selected_owner = _select_runtime_owner(expected_owner, ui=ui)
    proof_started_at = time.time()
    pid = _running_daemon_pid()
    launchagent_loaded = _launchagent_is_loaded()
    if pid is not None and not restart:
        if not http_enabled:
            restart = True
        else:
            current_health = _health_payload(cfg.mcp.host, cfg.mcp.port)
            policy_mismatch = bool(
                current_health is not None
                and (
                    (
                        expected_ocr_state != "ready"
                        and current_health.get("ocr") != expected_ocr_state
                    )
                    or (
                        expected_ocr_tier is not None
                        and current_health.get("ocr_tier") != expected_ocr_tier
                    )
                    or (
                        expected_ocr_enabled is not None
                        and current_health.get("ocr_enabled") != str(expected_ocr_enabled)
                    )
                )
            )
            if (
                current_health is None
                or policy_mismatch
                or (
                    require_ocr
                    and (
                        "ocr_worker" not in current_health
                        or current_health.get("ocr_worker") == "failed"
                    )
                )
            ):
                restart = True
        if restart and ui is not None:
            ui.status("Restarting the Runtime to establish a fresh readiness generation...")
    if restart and pid is not None:
        if ui is not None:
            ui.status("Restarting Persome to apply and verify the OCR configuration...")
        previous_pid = pid
        if launchagent_loaded:
            _kickstart_launchagent(kill=True)
            deadline = time.monotonic() + 30
            next_restart_progress_at = time.monotonic() + 10
            replacement_pid: int | None = None
            while time.monotonic() < deadline:
                candidate = _running_daemon_pid()
                if candidate is not None and candidate != previous_pid:
                    replacement_pid = candidate
                    break
                now = time.monotonic()
                if ui is not None and now >= next_restart_progress_at:
                    ui.status("Still waiting for launchd to publish the replacement Runtime...")
                    next_restart_progress_at = now + 10
                time.sleep(0.2)
            if replacement_pid is None:
                raise OnboardingError(
                    "launchd did not publish a new, identity-verifiable Runtime generation"
                )
            pid = replacement_pid
        else:
            result = _run_cli("stop", "--timeout", "20", timeout=25)
            if result.returncode != 0 and _running_daemon_pid() is not None:
                detail = result.stderr.strip() or result.stdout.strip() or "unknown stop failure"
                raise OnboardingError(f"the previous Runtime could not be stopped: {detail}")
            deadline = time.monotonic() + 25
            while _running_daemon_pid() is not None and time.monotonic() < deadline:
                time.sleep(0.2)
            pid = _running_daemon_pid()
            if pid is not None:
                detail = result.stderr.strip() or result.stdout.strip()
                suffix = f": {detail}" if detail else ""
                raise OnboardingError(
                    f"the previous Runtime process (pid {pid}) did not stop{suffix}"
                )

    if pid is None:
        if ui is not None:
            ui.status("Starting the Persome Runtime...")
        if launchagent_loaded:
            _kickstart_launchagent(kill=False)
            result = subprocess.CompletedProcess([], 0, "", "")
        else:
            result = _run_cli("start", timeout=30)
        start_deadline = time.monotonic() + min(timeout, 30)
        next_start_progress_at = time.monotonic() + 10
        while pid is None and time.monotonic() < start_deadline:
            pid = _running_daemon_pid()
            if pid is None:
                now = time.monotonic()
                if ui is not None and now >= next_start_progress_at:
                    ui.status("Still waiting for the Runtime process to publish its identity...")
                    next_start_progress_at = now + 10
                time.sleep(0.2)
        if pid is None:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown start failure"
            raise OnboardingError(f"Persome could not start: {detail}")

    if ui is not None:
        worker_note = " and its isolated OCR worker" if require_ocr else ""
        ui.status(f"Waiting for the Runtime{worker_note} to become ready...")
    deadline = time.monotonic() + timeout
    payload: dict[str, str] | None = None
    permissions: dict[str, str] | None = None
    state: dict[str, object] | None = None
    worker: str | None = None
    ocr_state: str | None = None
    ready = False
    next_progress_at = time.monotonic() + 10
    while time.monotonic() < deadline:
        current_pid = _running_daemon_pid()
        if current_pid != pid:
            raise OnboardingError("the Runtime process changed while onboarding was verifying it")

        if http_enabled:
            payload = _health_payload(cfg.mcp.host, cfg.mcp.port)
            permissions = (
                _runtime_permissions(cfg.mcp.host, cfg.mcp.port) if payload is not None else None
            )
            worker = payload.get("ocr_worker") if payload is not None else None
            ocr_state = payload.get("ocr") if payload is not None else None
            status_ready = bool(
                payload is not None
                and payload.get("status") in ({"ok"} if require_ocr else {"ok", "degraded"})
            )
        else:
            state = _runtime_state(pid, minimum_updated_at=proof_started_at)
            permissions_payload = state.get("permissions") if state is not None else None
            permissions = (
                {str(key): str(value) for key, value in permissions_payload.items()}
                if isinstance(permissions_payload, dict)
                else None
            )
            worker = str(state.get("ocr_worker")) if state is not None else None
            ocr_state = str(state.get("ocr")) if state is not None else None
            status_ready = state is not None and state.get("phase") == "ready"

        if require_ocr and worker == "failed":
            raise OnboardingError("the daemon's isolated OCR worker failed to initialize")
        if cfg.capture.source == "daemon" and permissions is not None:
            required_permissions = [("Accessibility", "accessibility")]
            if screen_recording_required:
                required_permissions.append(("Screen Recording", "screen_recording"))
            missing_permissions = [
                label for label, key in required_permissions if permissions.get(key) != "granted"
            ]
            if missing_permissions:
                raise OnboardingError(
                    "the running Runtime does not have "
                    f"{' and '.join(missing_permissions)} permission; enable it in "
                    "System Settings -> Privacy & Security, then rerun `persome onboard`"
                )
        permission_ready = cfg.capture.source != "daemon" or bool(
            permissions is not None
            and permissions.get("accessibility") == "granted"
            and (not screen_recording_required or permissions.get("screen_recording") == "granted")
        )
        reported_tier = (
            payload.get("ocr_tier")
            if payload is not None
            else str(state.get("ocr_tier"))
            if state is not None
            else None
        )
        reported_enabled = (
            payload.get("ocr_enabled")
            if payload is not None
            else str(state.get("ocr_enabled"))
            if state is not None
            else None
        )
        policy_ready = bool(
            ocr_state == expected_ocr_state
            and (expected_ocr_tier is None or reported_tier == expected_ocr_tier)
            and (expected_ocr_enabled is None or reported_enabled == str(expected_ocr_enabled))
        )
        ready = bool(
            status_ready
            and policy_ready
            and (not require_ocr or (ocr_state == "ready" and worker == "ready"))
            and permission_ready
        )
        if ready:
            break
        now = time.monotonic()
        if ui is not None and now >= next_progress_at:
            runtime_status = (
                payload.get("status", "starting")
                if payload is not None
                else str(state.get("phase", "starting"))
                if state is not None
                else "starting"
            )
            worker_status = worker or ("not required" if not require_ocr else "starting")
            if cfg.capture.source == "daemon":
                accessibility_status = (
                    permissions.get("accessibility", "checking")
                    if permissions is not None
                    else "checking"
                )
                screen_status = (
                    permissions.get("screen_recording", "checking")
                    if permissions is not None and screen_recording_required
                    else "not required"
                    if not screen_recording_required
                    else "checking"
                )
                permission_note = (
                    f", Accessibility: {accessibility_status}, Screen Recording: {screen_status}"
                )
            else:
                permission_note = ", capture permissions: owned by trusted ingest"
            ui.status(
                "Still waiting for Runtime readiness "
                f"(runtime: {runtime_status}, OCR worker: {worker_status}"
                f"{permission_note})..."
            )
            next_progress_at = now + 10
        time.sleep(0.25)
    if not ready:
        runtime_status = (
            payload.get("status", "unreachable")
            if payload is not None
            else str(state.get("phase", "unreachable"))
            if state is not None
            else "unreachable"
        )
        raise OnboardingError(
            "the daemon did not become fully ready before the timeout "
            f"(status={runtime_status}, "
            f"ocr={ocr_state or 'unknown'}, "
            f"worker={worker or 'unknown'}, "
            "accessibility="
            f"{permissions.get('accessibility', 'unknown') if permissions else 'unknown'}, "
            "screen_recording="
            f"{permissions.get('screen_recording', 'unknown') if permissions else 'unknown'})"
        )

    generation = ""
    current_state = _runtime_state(pid)
    if current_state is not None:
        generation = str(current_state["generation"])

    capture_path: Path | None = None
    capture_mode = cfg.capture.source
    receipt = "fresh-capture"
    capture_started_at = time.time()
    if http_enabled:
        if ui is not None:
            ui.status("Requesting a mode-aware capture proof from the running Runtime...")
        requested: CaptureRequestProof | None = None
        capture_deadline = time.monotonic() + 10
        while requested is None and time.monotonic() < capture_deadline:
            requested = _request_runtime_capture(cfg.mcp.host, cfg.mcp.port)
            if requested is None:
                time.sleep(0.25)
        if requested is None:
            raise OnboardingError("the daemon's live capture runner was not ready")
        capture_path = requested.path
        capture_mode = requested.mode
        receipt = requested.receipt
    else:
        if ui is not None:
            ui.status("Waiting for this daemon generation's owner-only capture receipt...")
        capture_deadline = time.monotonic() + min(timeout, 30)
        next_capture_progress_at = time.monotonic() + 10
        while time.monotonic() < capture_deadline:
            state = _runtime_state(pid, minimum_updated_at=proof_started_at)
            if state is None:
                time.sleep(0.2)
                continue
            generation = str(state["generation"])
            reason = str(state.get("last_capture_reason") or "")
            if reason in {"paused", "locked"}:
                receipt = f"privacy-{reason}"
                break
            if cfg.capture.source == "ingest" and reason == "ingest-ready":
                receipt = "ingest-ready"
                break
            capture_path = _capture_path_from_id(state.get("last_capture_id"))
            if capture_path is not None:
                receipt = "fresh-capture"
                break
            now = time.monotonic()
            if ui is not None and now >= next_capture_progress_at:
                ui.status("Still waiting for this Runtime generation's capture receipt...")
                next_capture_progress_at = now + 10
            time.sleep(0.2)
        else:
            raise OnboardingError("this Runtime generation did not publish a capture receipt")

    if receipt in {"privacy-paused", "privacy-locked"}:
        live_gate = receipt.removeprefix("privacy-")
        if not preserve_policy:
            action = "run `persome resume`" if live_gate == "paused" else "unlock the screen"
            raise OnboardingError(
                f"capture became {live_gate} while proof was running; {action}, then retry"
            )
        if _running_daemon_pid() != pid:
            raise OnboardingError("the Runtime process changed after the privacy receipt")
        if ui is not None:
            ui.status(f"✓ Runtime ready; capture remains {live_gate} by owner policy")
        owner = _prove_runtime_owner(pid, selected_owner)
        return RuntimeProof(
            pid=pid,
            health=payload.get("status", "ok") if payload else "ok",
            ocr=payload.get("ocr", expected_ocr_state) if payload else expected_ocr_state,
            capture_path=None,
            mode=cfg.capture.source,
            receipt=receipt,
            generation=generation,
            accessibility=(
                permissions.get("accessibility", "not_applicable")
                if permissions
                else "not_applicable"
            ),
            screen_recording=(
                permissions.get("screen_recording", "not_applicable")
                if permissions
                else "not_applicable"
            ),
            owner=owner,
        )

    if capture_path is not None:
        try:
            capture_mtime = capture_path.stat().st_mtime
        except OSError as exc:
            raise OnboardingError(f"fresh capture receipt is missing: {capture_path}") from exc
        if capture_mtime < min(proof_started_at, capture_started_at) - 1:
            raise OnboardingError("fresh capture verification returned a stale capture record")
        if not _capture_has_real_context(capture_path):
            raise OnboardingError(
                "fresh capture contains no AX text, focused element, screenshot, or completed "
                "OCR; focus a normal unlocked window and retry"
            )
    elif receipt != "ingest-ready":
        raise OnboardingError("the Runtime did not return a valid mode-aware capture receipt")

    if _running_daemon_pid() != pid:
        raise OnboardingError("the Runtime process changed after the capture proof")
    if http_enabled:
        final_payload = _health_payload(cfg.mcp.host, cfg.mcp.port)
        if final_payload is None or (
            require_ocr
            and (final_payload.get("status") != "ok" or final_payload.get("ocr_worker") != "ready")
        ):
            raise OnboardingError("the daemon lost health after the capture smoke test")
        if (
            final_payload.get("ocr") != expected_ocr_state
            or (
                expected_ocr_tier is not None and final_payload.get("ocr_tier") != expected_ocr_tier
            )
            or (
                expected_ocr_enabled is not None
                and final_payload.get("ocr_enabled") != str(expected_ocr_enabled)
            )
        ):
            raise OnboardingError("the daemon's OCR policy changed after the capture smoke test")
        final_permissions = _runtime_permissions(cfg.mcp.host, cfg.mcp.port)
        if cfg.capture.source == "daemon" and (
            final_permissions is None
            or final_permissions.get("accessibility") != "granted"
            or (
                screen_recording_required and final_permissions.get("screen_recording") != "granted"
            )
        ):
            raise OnboardingError("the daemon lost a required macOS permission after capture")
        if final_permissions is not None:
            permissions = final_permissions
        payload = final_payload
    else:
        final_state = _runtime_state(pid)
        if final_state is None:
            raise OnboardingError("the Runtime generation receipt disappeared after capture proof")
        if require_ocr and final_state.get("ocr_worker") != "ready":
            raise OnboardingError("the daemon's OCR worker lost readiness after capture proof")
        if (
            final_state.get("ocr") != expected_ocr_state
            or (expected_ocr_tier is not None and final_state.get("ocr_tier") != expected_ocr_tier)
            or (
                expected_ocr_enabled is not None
                and final_state.get("ocr_enabled") is not expected_ocr_enabled
            )
        ):
            raise OnboardingError("the daemon's OCR policy changed after capture proof")
        final_permissions = final_state.get("permissions")
        if cfg.capture.source == "daemon" and (
            not isinstance(final_permissions, dict)
            or final_permissions.get("accessibility") != "granted"
            or (
                screen_recording_required and final_permissions.get("screen_recording") != "granted"
            )
        ):
            raise OnboardingError("the daemon lost a required macOS permission after capture")

    owner = _prove_runtime_owner(pid, selected_owner)
    if ui is not None:
        if receipt == "ingest-ready":
            ui.status("✓ Runtime healthy and trusted ingest runner ready")
        else:
            ui.status("✓ Runtime healthy and fresh capture verified")

    return RuntimeProof(
        pid=pid,
        health=payload.get("status", "ok") if payload else "ok",
        ocr=payload.get("ocr", expected_ocr_state) if payload else expected_ocr_state,
        capture_path=capture_path,
        mode=capture_mode,
        receipt=receipt,
        generation=generation,
        accessibility=(
            permissions.get("accessibility", "not_applicable") if permissions else "not_applicable"
        ),
        screen_recording=(
            permissions.get("screen_recording", "not_applicable")
            if permissions
            else "not_applicable"
        ),
        owner=owner,
    )


def onboard(
    *,
    tier: str | None = None,
    gui: bool = True,
    preserve_policy: bool = False,
    expected_owner: RuntimeOwner = "any",
) -> RuntimeProof:
    """Run the complete permission, OCR, daemon, and capture onboarding gate."""
    if sys.platform != "darwin":
        raise OnboardingError("live Persome onboarding requires macOS")
    from .config import load
    from .ocr_setup import VALID_TIERS

    if tier is not None and tier not in VALID_TIERS:
        raise OnboardingError(f"unsupported OCR tier {tier!r}: choose {', '.join(VALID_TIERS)}")

    ui = OnboardingUI(gui=gui)
    cfg = load()
    accessibility_changed = False
    if cfg.capture.source == "daemon":
        accessibility_changed = ensure_accessibility(ui, event_driven=cfg.capture.event_driven)
    else:
        ui.status("✓ Accessibility and screen pixels are owned by the trusted ingest producer")
    ocr = ensure_local_ocr(
        tier=tier,
        ui=ui,
        preserve_policy=preserve_policy,
        screenshot_permission_required=(
            cfg.capture.source == "daemon" and cfg.capture.include_screenshot
        ),
        capture_source=cfg.capture.source,
    )
    effective_cfg = load()
    require_screen_recording = bool(
        effective_cfg.capture.source == "daemon"
        and (effective_cfg.capture.include_screenshot or ocr.require_worker)
    )
    proof = ensure_runtime(
        restart=(accessibility_changed or ocr.config_changed or ocr.permission_changed),
        ui=ui,
        require_ocr=ocr.require_worker,
        require_screen_recording=require_screen_recording,
        preserve_policy=preserve_policy,
        expected_ocr_state=ocr.state,
        expected_ocr_tier=ocr.tier,
        expected_ocr_enabled=effective_cfg.capture.enable_ocr_fallback,
        expected_owner=expected_owner,
    )
    if not preserve_policy:
        # Optional and read-only: capture readiness remains the hard gate, while
        # a detected active Obsidian vault can seed Day-One model formation.
        from .source_import import offer_obsidian_import

        offer_obsidian_import(ui, effective_cfg)
    if proof.receipt == "fresh-capture":
        completion = "a fresh capture with real context was verified"
    elif proof.receipt == "ingest-ready":
        completion = "the authenticated ingest runner is ready"
    else:
        completion = f"capture remains {proof.receipt.removeprefix('privacy-')} by owner policy"
    ui.success(f"Persome generation {proof.generation or proof.pid} is ready; {completion}.")
    return proof

"""Interactive macOS permission onboarding and runtime proof.

The installer delegates here so it cannot claim success after merely printing
privacy instructions.  Each sensitive request gets a separate, plain-language
native dialog.  Completion requires live Accessibility and Screen Recording
grants, a working isolated OCR worker, a healthy daemon, and a fresh capture.
"""

from __future__ import annotations

import json
import os
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

_CONFIRM_SCRIPT = """
on run argv
    set dialogTitle to item 1 of argv
    set dialogMessage to item 2 of argv
    set actionLabel to item 3 of argv
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
    try
        set chosen to button returned of (display dialog dialogMessage with title dialogTitle buttons {"Cancel", "Open Settings", "Check Again"} default button "Check Again" cancel button "Cancel" with icon caution)
        return chosen
    on error number -128
        return "Cancel"
    end try
end run
"""

_INFO_SCRIPT = """
on run argv
    display dialog (item 2 of argv) with title (item 1 of argv) buttons {"Done"} default button "Done" with icon note
    return "Done"
end run
"""


class OnboardingError(RuntimeError):
    """The required onboarding proof could not be completed."""


class OnboardingCancelled(OnboardingError):
    """The user explicitly cancelled a permission request."""


class PermissionUI(Protocol):
    def confirm(self, *, title: str, message: str, action: str) -> bool: ...

    def wait_for_permission(self, *, title: str, message: str) -> PermissionAction: ...


class OnboardingUI:
    """Native macOS dialogs with a terminal fallback for remote shells."""

    def __init__(self, *, gui: bool = True) -> None:
        self.gui = gui and sys.platform == "darwin" and shutil.which("osascript") is not None

    def _osascript(self, script: str, *args: str) -> str | None:
        if not self.gui:
            return None
        try:
            result = subprocess.run(
                ["osascript", "-e", script, *args],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError:
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
        result = self._osascript(_CONFIRM_SCRIPT, title, message, action)
        if result is not None:
            return result == action
        return self._terminal_confirm(title, message, action)

    def wait_for_permission(self, *, title: str, message: str) -> PermissionAction:
        result = self._osascript(_WAIT_SCRIPT, title, message)
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
        if self._osascript(_INFO_SCRIPT, "Persome is ready", message) is None:
            print(f"\nPersome is ready\n{message}")


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
) -> bool:
    """Request one permission and do not return until its live probe passes."""
    if check():
        return True
    if not ui.confirm(
        title=f"Persome needs {label}",
        message=explanation,
        action=f"Request {label}",
    ):
        raise OnboardingCancelled(f"{label} request was cancelled")

    request()
    while not check():
        action = ui.wait_for_permission(
            title=f"Finish {label} setup",
            message=(
                f"Enable the terminal or Persome entry under Privacy & Security -> {label}. "
                "Return here and choose Check Again. Persome will not continue until macOS "
                "reports that the permission is granted."
            ),
        )
        if action == "cancel":
            raise OnboardingCancelled(f"{label} was not granted")
        if action == "open_settings":
            open_settings()
    return True


def ensure_accessibility(ui: PermissionUI) -> None:
    from .capture import ax_capture, watcher

    _ensure_permission(
        label="Accessibility",
        check=ax_capture.ax_trusted,
        request=watcher.request_accessibility_permission,
        open_settings=open_accessibility_settings,
        ui=ui,
        explanation=(
            "Persome uses macOS Accessibility to read the focused app's visible text and "
            "structure. It does not use this permission to type, click, or control your Mac. "
            "macOS will show its own permission request next."
        ),
    )


def ensure_local_ocr(*, tier: str, ui: PermissionUI) -> bool:
    """Grant Screen Recording, warm OCR, persist it, and return whether config changed."""
    from .capture import ocr_health, ocr_local, screen_recording
    from .config import load
    from .ocr_setup import VALID_TIERS, save_ocr_config

    if tier not in VALID_TIERS:
        raise OnboardingError(f"unsupported OCR tier {tier!r}: choose {', '.join(VALID_TIERS)}")
    if ocr_local.disabled_by_environment():
        raise OnboardingError("OCR is disabled by PERSOME_DISABLE_OCR")
    if not ocr_local.runtime_available():
        raise OnboardingError("the local Paddle OCR runtime is unavailable on this architecture")
    if not ocr_local.models_available(tier):
        raise OnboardingError(f"bundled PP-OCRv6 {tier} model weights are missing")

    _ensure_permission(
        label="Screen Recording",
        check=screen_recording.has_screen_recording,
        request=screen_recording.request_screen_recording,
        open_settings=open_screen_recording_settings,
        ui=ui,
        explanation=(
            "Persome uses Screen Recording only to run bundled PP-OCRv6 on this Mac when an "
            "app's Accessibility text is incomplete. Pixels are not sent to an LLM or uploaded. "
            "macOS will show its own permission request next."
        ),
    )

    if not ocr_local.warm(tier):
        raise OnboardingError("the isolated local OCR worker could not initialize")

    before = load().capture
    changed = not before.enable_ocr_fallback or before.ocr_tier != tier
    save_ocr_config(enabled=True, tier=tier, config_path=paths.config_file())
    health = ocr_health.inspect(load().capture)
    if not health.ready:
        raise OnboardingError(f"local OCR verification failed: {health.state}: {health.detail}")
    return changed


@dataclass(frozen=True)
class RuntimeProof:
    pid: int
    health: str
    ocr: str
    capture_path: Path


def _running_daemon_pid() -> int | None:
    try:
        pid = int(paths.pid_file().read_text(encoding="utf-8").strip())
    except (FileNotFoundError, OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid
    return pid


def _cli_prefix() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "persome"]


def _run_cli(*args: str, timeout: float = 180.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*_cli_prefix(), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _health_payload(host: str, port: int) -> dict[str, str] | None:
    try:
        import httpx

        response = httpx.get(f"http://{host}:{port}/health", timeout=2.0)
        response.raise_for_status()
        data = response.json().get("data")
    except Exception:  # noqa: BLE001 - the bounded poll reports a useful final error
        return None
    if not isinstance(data, dict):
        return None
    return {str(key): str(value) for key, value in data.items()}


def _latest_capture() -> Path | None:
    directory = paths.capture_buffer_dir()
    if not directory.exists():
        return None
    captures = [path for path in directory.iterdir() if path.suffix == ".json"]
    return max(captures, key=lambda path: path.stat().st_mtime, default=None)


def ensure_runtime(*, restart: bool, timeout: float = 45.0) -> RuntimeProof:
    """Leave the daemon running and prove HTTP health plus one fresh capture."""
    from .config import load

    cfg = load()
    pid = _running_daemon_pid()
    if restart and pid is not None:
        _run_cli("stop", "--timeout", "20", timeout=25)
        deadline = time.monotonic() + 25
        while _running_daemon_pid() is not None and time.monotonic() < deadline:
            time.sleep(0.2)
        pid = _running_daemon_pid()

    if pid is None:
        result = _run_cli("start", timeout=30)
        if result.returncode != 0 and _running_daemon_pid() is None:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown start failure"
            raise OnboardingError(f"Persome could not start: {detail}")

    deadline = time.monotonic() + timeout
    payload: dict[str, str] | None = None
    while time.monotonic() < deadline:
        pid = _running_daemon_pid()
        payload = _health_payload(cfg.mcp.host, cfg.mcp.port)
        if pid is not None and payload is not None:
            break
        time.sleep(0.25)
    if pid is None or payload is None:
        raise OnboardingError("the daemon did not become healthy before the timeout")
    if payload.get("status") != "ok" or payload.get("ocr") != "ready":
        raise OnboardingError(
            f"the daemon is degraded (status={payload.get('status')}, ocr={payload.get('ocr')})"
        )

    started_at = time.time()
    capture = _run_cli("capture-once", timeout=180)
    if capture.returncode != 0:
        detail = capture.stderr.strip() or capture.stdout.strip() or "capture failed"
        raise OnboardingError(f"fresh capture verification failed: {detail}")
    capture_path = _latest_capture()
    if capture_path is None or capture_path.stat().st_mtime < started_at - 1:
        raise OnboardingError("fresh capture verification did not create a capture record")

    payload = _health_payload(cfg.mcp.host, cfg.mcp.port)
    pid = _running_daemon_pid()
    if pid is None or payload is None or payload.get("status") != "ok":
        raise OnboardingError("the daemon lost health after the capture smoke test")

    # Verify the record is readable JSON before treating it as onboarding proof.
    try:
        record = json.loads(capture_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OnboardingError(f"fresh capture record is unreadable: {exc}") from exc
    if not isinstance(record, dict):
        raise OnboardingError("fresh capture record is not a JSON object")
    meta = record.get("window_meta")
    has_context = bool(
        (isinstance(meta, dict) and (meta.get("app_name") or meta.get("title")))
        or record.get("visible_text")
        or record.get("focused_element")
        or record.get("ax_tree")
        or record.get("ocr_submitted")
    )
    if not record.get("timestamp") or not has_context:
        raise OnboardingError("fresh capture record contains no usable screen context")

    return RuntimeProof(
        pid=pid,
        health=payload["status"],
        ocr=payload.get("ocr", "unknown"),
        capture_path=capture_path,
    )


def onboard(*, tier: str = "tiny", gui: bool = True) -> RuntimeProof:
    """Run the complete permission, OCR, daemon, and capture onboarding gate."""
    if sys.platform != "darwin":
        raise OnboardingError("live Persome onboarding requires macOS")
    ui = OnboardingUI(gui=gui)
    ensure_accessibility(ui)
    ocr_changed = ensure_local_ocr(tier=tier, ui=ui)
    proof = ensure_runtime(restart=ocr_changed)
    ui.success(
        "Accessibility and local OCR are ready. Persome is running, its local health endpoint "
        "is OK, and a fresh capture was written successfully."
    )
    return proof

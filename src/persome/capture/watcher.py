"""Long-running AX event watcher subprocess manager.

Wraps the vendored ``mac-ax-watcher`` Swift binary. Reads JSONL events from
stdout and dispatches them through a registered callback. Reconnects on
crash with exponential backoff.

Ported from Einsia-Partner's backend/core/memory/watcher.py — path resolution
adapted to Persome's bundled-resource layout (mirrors ax_capture.py).
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import select
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..logger import get
from . import ax_capture
from .ax_capture import _maybe_compile

logger = get("persome.capture")


def _resolve_watcher_path() -> Path | None:
    """Find or build the mac-ax-watcher binary.

    Search order mirrors ax_capture._resolve_helper_path:
      1. PERSOME_AX_WATCHER env var
      2. Packaged resource shipped with the wheel (_bundled/)
      3. Dev source tree (Persome/resources/)
    """
    if platform.system() != "Darwin":
        return None

    override = os.environ.get("PERSOME_AX_WATCHER")
    if override:
        p = Path(override).expanduser().resolve()
        if p.is_file() and os.access(p, os.X_OK):
            return p
        logger.warning("PERSOME_AX_WATCHER set but not executable: %s", p)

    candidates: list[Path] = []
    try:
        from importlib.resources import files as _pkg_files

        bundled_dir = Path(str(_pkg_files("persome").joinpath("_bundled")))
        candidates.append(bundled_dir / "mac-ax-watcher")
    except (ModuleNotFoundError, ValueError):
        pass

    dev_root = Path(__file__).resolve().parents[3]
    candidates.append(dev_root / "resources" / "mac-ax-watcher")

    for binary_path in candidates:
        swift_path = binary_path.with_suffix(".swift")
        if swift_path.is_file():
            _maybe_compile(swift_path, binary_path)
        if binary_path.is_file() and os.access(binary_path, os.X_OK):
            return binary_path

    return None


class AXWatcherProcess:
    """Owns the mac-ax-watcher subprocess and a reader thread.

    Thread safety: ``start`` / ``stop`` may be called from any thread.
    The callback runs on the reader thread — keep it fast and thread-safe.
    """

    # Kill and restart the subprocess if it produces no output for this long.
    # A genuinely idle user still generates no AX events, but 5 minutes of
    # complete silence while the process is alive almost certainly means the
    # Swift binary is frozen — restart is harmless and restores event flow.
    _DEFAULT_STALE_TIMEOUT = 300.0
    _DEFAULT_PERMISSION_POLL = 2.0

    def __init__(
        self,
        *,
        max_reconnect_delay: float = 60.0,
        stale_timeout_seconds: float = _DEFAULT_STALE_TIMEOUT,
        permission_poll_seconds: float = _DEFAULT_PERMISSION_POLL,
    ) -> None:
        self._watcher_path = _resolve_watcher_path()
        self._callback: Callable[[dict[str, Any]], None] | None = None
        self._process: subprocess.Popen | None = None
        self._reader_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._max_reconnect_delay = max_reconnect_delay
        self._stale_timeout = stale_timeout_seconds
        self._permission_poll = max(0.01, permission_poll_seconds)

    @property
    def available(self) -> bool:
        return self._watcher_path is not None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def on_event(self, callback: Callable[[dict[str, Any]], None]) -> None:
        self._callback = callback

    def start(self) -> None:
        if not self._watcher_path:
            logger.warning("AX watcher not available (not macOS or binary not found)")
            return
        self._stop_event.clear()
        self._reader_thread = threading.Thread(
            target=self._run_loop, daemon=True, name="ax-watcher-reader"
        )
        self._reader_thread.start()
        logger.info("AX watcher started: %s", self._watcher_path)

    def stop(self, *, join_timeout: float = 5.0) -> None:
        """Stop the subprocess and join the reader thread.

        Closing ``stdout`` is necessary because the reader loop is blocked
        on a line read; otherwise ``join`` would hang for the full
        ``join_timeout`` even after the process is dead.
        """
        self._stop_event.set()
        proc = self._process
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=join_timeout)
            except subprocess.TimeoutExpired:
                self._force_kill(proc, wait_timeout=1.0)
        if proc and proc.stdout is not None:
            with contextlib.suppress(OSError, ValueError):
                proc.stdout.close()
        self._process = None

        reader = self._reader_thread
        if reader is not None and reader.is_alive():
            reader.join(timeout=join_timeout)
            if reader.is_alive():
                logger.warning("AX watcher reader thread did not exit within %.1fs", join_timeout)
        self._reader_thread = None
        logger.info("AX watcher stopped")

    def _run_loop(self) -> None:
        delay = 1.0
        while not self._stop_event.is_set():
            return_code: int | None = None
            try:
                self._start_process()
                if self._process is None:
                    break
                return_code = self._read_events()
            except Exception as exc:  # noqa: BLE001
                logger.warning("AX watcher error: %s", exc)

            if self._stop_event.is_set():
                break

            if return_code == 2:
                delay = 1.0
                if self._wait_for_accessibility():
                    logger.info("Accessibility permission granted — restarting AX watcher")
                    continue
                break

            logger.info("AX watcher exited, reconnecting in %.0fs", delay)
            self._stop_event.wait(delay)
            delay = min(delay * 2, self._max_reconnect_delay)

    def _wait_for_accessibility(self) -> bool:
        """Poll the daemon's TCC trust without showing repeated permission dialogs."""
        logger.warning(
            "Accessibility permission not granted — waiting for approval (polling every %.0fs)",
            self._permission_poll,
        )
        while not self._stop_event.is_set():
            if ax_capture.ax_trusted():
                return True
            self._stop_event.wait(self._permission_poll)
        return False

    def _start_process(self) -> None:
        if not self._watcher_path:
            return
        try:
            self._process = subprocess.Popen(
                [str(self._watcher_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
            logger.info("AX watcher subprocess started (pid=%d)", self._process.pid)
        except OSError as exc:
            logger.error("Failed to start AX watcher: %s", exc)
            self._process = None

    @staticmethod
    def _force_kill(proc: subprocess.Popen, *, wait_timeout: float = 2.0) -> None:
        proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=wait_timeout)

    def _read_events(self) -> int | None:
        """Read until exit/stop/staleness and return the subprocess exit code."""
        if not self._process or not self._process.stdout:
            return None

        stdout = self._process.stdout
        last_activity = time.monotonic()
        poll = min(30.0, self._stale_timeout / 2)

        while not self._stop_event.is_set():
            ready, _, _ = select.select([stdout], [], [], poll)

            if not ready:
                silent_for = time.monotonic() - last_activity
                if silent_for >= self._stale_timeout:
                    logger.warning(
                        "AX watcher silent for %.0fs (process frozen?), killing and restarting",
                        silent_for,
                    )
                    proc = self._process
                    if proc and proc.poll() is None:
                        self._force_kill(proc)
                    return proc.wait() if proc is not None else None
                continue

            if self._stop_event.is_set():
                break

            line = stdout.readline()
            if not line:  # EOF — process exited
                break

            last_activity = time.monotonic()
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("Invalid JSON from watcher: %s", line[:100])
                continue
            event_type = event.get("event_type", "")
            if event_type.startswith("_"):
                if event_type == "_electron_ax_activated":
                    # Electron webcontents AX activation (issue #556) — keep
                    # at INFO so before/after miss-rate comparisons can line
                    # activations up against parser_miss lines in the logs.
                    details = event.get("details") or {}
                    logger.info(
                        "Electron AX activated: bundle=%s pid=%s reason=%s "
                        "set_manual_err=%s set_enhanced_err=%s",
                        event.get("bundle_id"),
                        event.get("pid"),
                        details.get("reason"),
                        details.get("set_manual_err"),
                        details.get("set_enhanced_err"),
                    )
                else:
                    logger.debug("Watcher internal event: %s", event_type)
                continue
            if self._callback:
                try:
                    self._callback(event)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Event callback error: %s", exc)

        if self._process:
            rc = self._process.wait()
            if rc != 0 and rc != 2:
                logger.warning("AX watcher exited with code %d", rc)
            return rc
        return None

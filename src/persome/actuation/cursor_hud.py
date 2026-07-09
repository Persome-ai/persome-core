"""Persistent floating Persome cursor — the daemon-side controller for `mac-ax-actuator cursor-hud`.

Instead of a per-act flash, the daemon keeps ONE long-lived overlay process alive during a
computer-use session and feeds it the action point + step note after each act, so the user sees a
single Persome cursor float across the steps (like Claude Code's computer-use cursor). It auto-hides
after `idle_seconds` with no action (the session went quiet) and re-spawns on the next update.

The same helper also renders the **takeover glow + badge** (spec
`docs/superpowers/specs/2026-07-02-takeover-glow-overlay-design.md`): `glow(payload)` sends a
`{"glow": …}` line over the same stdin pipe. While a glow is active the idle timeout stretches to
`glow_idle_seconds` — an agent thinks for 30–120s between acts, and the takeover halo must not go
dark mid-run; a terminal/cleared glow drops back to the short cursor idle.

Lifecycle is lazy + best-effort: spawning/feeding the HUD never affects the actuation result.
"""

from __future__ import annotations

import contextlib
import json
import threading
from typing import Any

from ..logger import get

logger = get("persome.actuation.hud")


class CursorHUD:
    """Manages the long-running cursor-hud subprocess. Thread-safe; lazy-spawn; idle auto-stop."""

    def __init__(self, idle_seconds: float = 8.0, glow_idle_seconds: float = 180.0) -> None:
        self._idle = idle_seconds
        self._glow_idle = glow_idle_seconds
        self._glow_active = False
        self._lock = threading.Lock()
        self._proc: Any = None
        self._timer: threading.Timer | None = None

    def _spawn_locked(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        import subprocess

        from .actuator import _resolve_actuator_path

        binary = _resolve_actuator_path()
        if binary is None:
            return False
        try:
            self._proc = subprocess.Popen(
                [str(binary), "cursor-hud"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            return True
        except OSError as exc:
            logger.warning("cursor-hud spawn failed: %s", exc)
            self._proc = None
            return False

    def update(
        self, point: list[float] | None, note: str, elements: list[dict] | None = None
    ) -> None:
        """Move the floating cursor to `point` (AX coords) with `note`; optional element boxes.

        No-op (best-effort) if the HUD can't be spawned/written."""
        if point is None and not note:
            return
        with self._lock:
            if not self._spawn_locked():
                return
            msg: dict[str, Any] = {"note": note}
            if point and len(point) == 2:
                msg["x"], msg["y"] = point[0], point[1]
            if elements:
                msg["elements"] = [
                    {"bbox": e["bbox"], "role": e.get("role", "")}
                    for e in elements
                    if e.get("bbox")
                ]
            try:
                self._proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
                self._proc.stdin.flush()
            except (OSError, ValueError):
                self._proc = None
                return
            self._arm_idle_locked()

    def glow(self, payload: dict) -> None:
        """Send a takeover-glow state update (`{"glow": <payload>}`) to the overlay.

        `payload` is the tracker's glow body ({app, pid, state, note, task_id, point?}) or
        `{"clear": true}`. A terminal (`done`/`failed`) or cleared glow lets the process idle out
        on the short cursor timeout again (the helper plays its own terminal fade). Best-effort:
        never raises, never affects the actuation result."""
        if not payload:
            return
        with self._lock:
            if not self._spawn_locked():
                return
            terminal = bool(payload.get("clear")) or payload.get("state") in ("done", "failed")
            self._glow_active = not terminal
            try:
                self._proc.stdin.write(json.dumps({"glow": payload}, ensure_ascii=False) + "\n")
                self._proc.stdin.flush()
            except (OSError, ValueError):
                self._proc = None
                self._glow_active = False
                return
            self._arm_idle_locked()

    def clear_glow(self) -> None:
        """Drop the takeover glow immediately (badge + halo disappear)."""
        self.glow({"clear": True})

    def _arm_idle_locked(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        # A live glow keeps the process around through the agent's between-acts thinking gaps.
        timeout = self._glow_idle if self._glow_active else self._idle
        self._timer = threading.Timer(timeout, self.stop)
        self._timer.daemon = True
        self._timer.start()

    def stop(self) -> None:
        """Close the HUD (cursor disappears). The next update re-spawns it."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._glow_active = False
            if self._proc is not None:
                with contextlib.suppress(OSError):
                    self._proc.stdin.close()  # EOF → the hud terminates itself
                self._proc = None


# Module-level singleton (the daemon is one process).
hud = CursorHUD()

"""Takeover glow session tracker — the pure state machine behind the window glow + badge.

While a dispatched agent drives an app through the `ui_*` tools, the user should SEE the takeover
on the app window itself: a breathing glow border + a top-right status badge (spec:
`docs/superpowers/specs/2026-07-02-takeover-glow-overlay-design.md`). This module owns the state:
one `TakeoverSession` per MCP session (= per agent run, keyed by the `mcp-session-id` header),
holding the target app, the run's task id, and the current execution state on the two-axis model
(state × note). Callers (mcp/server.py chokepoints, the confirm wrapper, the app's end-of-run POST)
feed events in; each transition returns the glow payload to forward to the cursor-hud helper —
or None when there is nothing to show.

Execution states (axis A, MECE — see spec §2):
  observing         read-only ui_* seen, no act yet
  awaiting_confirm  a gated act is blocked on the user's confirm
  executing         an act ran (or is about to)
  done / failed     terminal, reported by the app at run end (`end_run`); the session is dropped

Pure + offline-testable: no HUD, no HTTP, no threads of its own (just a lock — tool calls run on
FastMCP worker threads). Time is injectable for the stale-session prune.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

# Sessions untouched for this long are pruned (an agent run never lasts days; the map must not
# grow unboundedly on a long-lived daemon). Generous vs. the HUD's own 180s glow idle.
STALE_SECONDS = 6 * 3600.0

_TERMINAL = {"done", "failed"}


@dataclass
class TakeoverSession:
    """One agent run's takeover state (keyed by MCP session id)."""

    session_id: str
    app: str = ""
    pid: int = 0
    task_id: str = ""
    state: str = "observing"  # observing | executing | awaiting_confirm
    note: str = ""
    point: list[float] | None = None  # AX coords of the last act — pins the hit window
    last_at: float = field(default_factory=time.monotonic)

    def payload(self) -> dict:
        """The glow message body the cursor-hud helper renders (its `{"glow": …}` inner dict)."""
        out: dict = {
            "app": self.app,
            "pid": self.pid,
            "state": self.state,
            "note": self.note,
            "task_id": self.task_id,
        }
        if self.point is not None:
            out["point"] = self.point
        return out


class TakeoverTracker:
    """Thread-safe registry of live takeover sessions. Every mutation returns the glow payload(s)
    to send (already-shaped for `CursorHUD.glow`), so the caller stays a one-liner."""

    def __init__(self, *, now=time.monotonic) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, TakeoverSession] = {}
        self._now = now

    def on_tool(
        self,
        session_id: str,
        *,
        app: str,
        kind: str,
        pid: int = 0,
        task_id: str = "",
        note: str = "",
        point: list[float] | None = None,
    ) -> dict | None:
        """A `ui_*` tool touched `app`. `kind` is "read" (eyes) or "act" (hands).

        Reads start/keep `observing` but never downgrade an `executing` session (the agent
        re-reading the UI mid-task is still a takeover in progress). Acts go `executing` and
        update the note + hit point. Returns the payload to render, or None for a no-show call
        (empty app — nothing to glow around).
        """
        if not app and not pid:
            return None
        with self._lock:
            self._prune_locked()
            s = self._sessions.setdefault(session_id, TakeoverSession(session_id=session_id))
            s.last_at = self._now()
            if app:
                s.app = app
            if pid:
                s.pid = pid
            if task_id:
                s.task_id = task_id
            if kind == "act":
                # awaiting_confirm is owned by confirm_begin/confirm_end; an act event during it
                # would be the gated act itself re-reporting — keep the confirm state visible.
                if s.state != "awaiting_confirm":
                    s.state = "executing"
                if note:
                    s.note = note
                if point is not None and len(point) == 2:
                    s.point = [float(point[0]), float(point[1])]
            elif s.state == "observing" and note:
                s.note = note
            return s.payload()

    def begin_run(
        self,
        task_id: str,
        *,
        app: str = "",
        bundle_id: str = "",
        pid: int = 0,
        note: str = "",
    ) -> dict | None:
        """The app reports a dispatched run whose take-over target was resolved at the entry point
        (the app focused at voice-hotkey press) — the RUN-lifecycle glow driver, independent of the
        ui_* chokepoints (a skill/CLI-driven takeover never calls them). Keyed `task:<id>` so the
        same run's ui_* MCP session (keyed by `mcp-session-id`) coexists as the refinement layer;
        `end_run` closes both by task id. Idempotent — the app re-posts this as a keepalive every
        ~90s so the HUD's idle reaper never fades a quiet run; each call re-emits the payload
        (re-arming the HUD idle) without disturbing state. Spec §4.0."""
        ident = bundle_id or app
        if not task_id or (not ident and not pid):
            return None
        with self._lock:
            self._prune_locked()
            key = f"task:{task_id}"
            s = self._sessions.setdefault(key, TakeoverSession(session_id=key))
            s.last_at = self._now()
            s.app = ident
            if pid:
                s.pid = pid
            s.task_id = task_id
            s.state = "executing"
            if note and not s.note:
                s.note = note  # the run title seeds the badge; ui_* step notes may overwrite later
            # Don't clobber a live confirm: if this run's ui_* MCP session is mid-approval, the
            # orange pulse must survive the ~90s keepalive (last-writer-wins at the helper).
            if any(
                o.task_id == task_id and o.state == "awaiting_confirm"
                for o in self._sessions.values()
            ):
                return None
            return s.payload()

    def confirm_begin(self, session_id: str, *, summary: str = "") -> dict | None:
        """A gated act is now blocked on the user's approval → orange-pulse state. The badge shows
        the confirm summary (what the agent wants to do), not the step note."""
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None:
                return None
            s.last_at = self._now()
            s.state = "awaiting_confirm"
            p = s.payload()
            p["note"] = summary or s.note
            return p

    def confirm_end(self, session_id: str) -> dict | None:
        """The confirm resolved (approved, denied, or timed out) → back to executing either way:
        approved = the act fires next; denied = the run continues (the agent picks another step)."""
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None or s.state != "awaiting_confirm":
                return None
            s.last_at = self._now()
            s.state = "executing"
            return s.payload()

    def end_run(self, task_id: str, *, outcome: str) -> list[dict]:
        """The app reports the run reached terminal status → terminal glow (green/red flash) for
        every session of that task, then forget them. Unknown task id → [] (idempotent; the app
        posts blind for every finished run, most of which never touched actuation)."""
        state = outcome if outcome in _TERMINAL else "failed"
        if not task_id:
            return []
        out: list[dict] = []
        with self._lock:
            for sid in [k for k, s in self._sessions.items() if s.task_id == task_id]:
                s = self._sessions.pop(sid)
                s.state = state
                out.append(s.payload())
        return out

    def sessions(self) -> list[dict]:
        """Diagnostics: the live sessions' payloads (never the internal objects)."""
        with self._lock:
            return [s.payload() for s in self._sessions.values()]

    def _prune_locked(self) -> None:
        cutoff = self._now() - STALE_SECONDS
        for sid in [k for k, s in self._sessions.items() if s.last_at < cutoff]:
            del self._sessions[sid]


# Module-level singleton (the daemon is one process), mirroring `cursor_hud.hud`.
tracker = TakeoverTracker()

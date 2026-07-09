"""Virtual-display stage lifecycle — the daemon-side handle around the `mac-virtual-stage` helper.

For multi-instance apps (`routing.stage_strategy → "virtual_stage"`) the agent operates its OWN fresh
instance on an off-screen CGVirtualDisplay so the user's screen never changes. This module spawns the
long-lived helper, parses the one JSON line it emits (display_id / window_id / app_pid / bounds), exposes
that to the actuator, and GUARANTEES teardown (terminate helper → display released → spawned app dies) on
close / task end / daemon stop. The helper holds the display only while alive, so the single owner of its
process IS the single owner of the display — there is no orphaned-display path as long as `close()` runs.

Darwin-only; returns a typed error off macOS / when the helper or the virtual-display API is unavailable,
so the caller degrades to the SkyLight/borrow path (never a silent steal). The spawn is injectable
(`spawn=`) so the spawn→parse→teardown ordering is unit-testable offline with a fake helper.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import threading
from pathlib import Path
from typing import Any, Protocol

from ..logger import get
from . import routing

logger = get("persome.actuation.stage")

_READY_TIMEOUT = (
    15.0  # seconds to wait for the helper's first JSON line (Chrome cold-start + window)
)


def _resolve_stage_helper_path() -> Path | None:
    """Find or build mac-virtual-stage (env override → bundled → dev source), mirroring the actuator."""
    if platform.system() != "Darwin":
        return None
    override = (os.environ.get("PERSOME_VIRTUAL_STAGE") or os.environ.get("MENS_CONTEXT_VIRTUAL_STAGE"))  # Mens is the legacy name
    if override:
        p = Path(override).expanduser().resolve()
        if p.is_file() and os.access(p, os.X_OK):
            return p

    from ..capture.ax_capture import _maybe_compile  # reuse the dev compile helper

    candidates: list[Path] = []
    try:
        from importlib.resources import files as _pkg_files

        candidates.append(Path(str(_pkg_files("persome").joinpath("_bundled"))) / "mac-virtual-stage")
    except (ModuleNotFoundError, ValueError):
        pass
    dev_root = Path(__file__).resolve().parents[3]  # .../persome-core/
    candidates.append(dev_root / "resources" / "mac-virtual-stage")

    for binary in candidates:
        src = binary.with_suffix(".swift")
        if src.is_file():
            _maybe_compile(src, binary)
        if binary.is_file() and os.access(binary, os.X_OK):
            return binary
    return None


class _Proc(Protocol):
    """The slice of subprocess.Popen the stage uses — so tests inject a fake helper process."""

    stdout: Any
    stdin: Any

    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def wait(self, timeout: float | None = ...) -> int: ...
    def kill(self) -> None: ...


def _default_spawn(args: list[str]) -> _Proc:
    return subprocess.Popen(  # noqa: S603 - args are our own helper path + literals
        args, stdout=subprocess.PIPE, stdin=subprocess.PIPE, text=True
    )


class VirtualStage:
    """A live off-screen stage: a `mac-virtual-stage` helper hosting one agent app instance.

    Open with `VirtualStage.open(app=..., url=...)`; on success `.ready` is True and `.window_id`/
    `.display_id`/`.app_pid`/`.bounds` describe the staged window for the actuator to drive. ALWAYS
    `close()` it (or use it as a context manager) so the helper is reaped and the display released —
    teardown is idempotent and never raises.
    """

    def __init__(self, proc: _Proc, info: dict[str, Any]) -> None:
        self._proc = proc
        self.info = info
        self.display_id: int | None = info.get("display_id")
        self.app_pid: int | None = info.get("app_pid")
        self.window_id: int | None = info.get("window_id")
        self.bounds: list[int] | None = info.get("bounds")
        self.window_bounds: list[int] | None = info.get("window_bounds")

    @property
    def ready(self) -> bool:
        """True iff the helper reported a concrete staged window to drive."""
        return self.window_id is not None and self.display_id is not None

    @classmethod
    def open(
        cls,
        *,
        app: str,
        url: str,
        profile: str = "persome-stage",
        width: int = 1920,
        height: int = 1080,
        spawn: Any = _default_spawn,
        ready_timeout: float = _READY_TIMEOUT,
    ) -> VirtualStage | dict[str, Any]:
        """Spawn the helper and wait for its first JSON line. Returns a live `VirtualStage`, or a typed
        error dict (`{"ok": False, "error": ...}`) when the helper is unavailable / errors / times out —
        in which case the caller falls back to the SkyLight/borrow path. The error path leaves no process
        running (a spawned-but-failing helper is terminated)."""
        binary = _resolve_stage_helper_path()
        if binary is None:
            return {"ok": False, "error": "virtual_stage_unavailable"}
        args = [
            str(binary),
            "--app",
            app,
            "--url",
            url,
            "--profile",
            profile,
            "--width",
            str(width),
            "--height",
            str(height),
        ]
        try:
            proc = spawn(args)
        except (OSError, ValueError) as exc:
            logger.warning("virtual stage spawn failed: %s", exc)
            return {"ok": False, "error": "virtual_stage_spawn_failed"}

        info = cls._read_ready_line(proc, ready_timeout)
        if info is None or info.get("error"):
            _terminate(proc)
            return {"ok": False, "error": (info or {}).get("error", "virtual_stage_no_window")}
        stage = cls(proc, info)
        if not stage.ready:
            # display came up but no window (helper emitted a warning) — not drivable; tear down.
            stage.close()
            return {"ok": False, "error": info.get("warning", "virtual_stage_no_window")}
        logger.info(
            "virtual stage ready: display=%s window=%s pid=%s",
            stage.display_id,
            stage.window_id,
            stage.app_pid,
        )
        return stage

    @staticmethod
    def _read_ready_line(proc: _Proc, timeout: float) -> dict[str, Any] | None:
        """Read one JSON line from the helper's stdout within `timeout`. None on EOF/timeout/parse error.
        The helper emits exactly one line then holds, so a blocking readline + a watchdog suffices."""
        import threading

        result: list[dict[str, Any] | None] = [None]

        def _read() -> None:
            try:
                line = proc.stdout.readline() if proc.stdout else ""
                result[0] = json.loads(line) if line.strip() else None
            except (ValueError, OSError):
                result[0] = None

        t = threading.Thread(target=_read, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():  # helper never emitted in time
            return None
        return result[0]

    def close(self) -> None:
        """Terminate the helper (→ display released, spawned app killed by the helper's signal handler).
        Idempotent; never raises."""
        _terminate(self._proc)

    def __enter__(self) -> VirtualStage:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _terminate(proc: _Proc) -> None:
    """Best-effort: SIGTERM the helper (its handler reaps the scoped app + releases the display), then
    SIGKILL if it lingers. Closing stdin also trips the helper's EOF teardown as a backstop."""
    try:
        if proc.poll() is not None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()  # EOF backstop → helper teardown
        except OSError:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    except (OSError, ValueError) as exc:
        logger.warning("virtual stage teardown imperfect: %s", exc)


# ── live-stage registry + open_app orchestration ────────────────────────────────────────────────


class StageRegistry:
    """Process-level registry of live `VirtualStage`s, keyed by the staged app's pid, so a stage opened
    by one MCP tool call survives the agent's subsequent verb calls and is reaped exactly once. The
    daemon owns ONE instance (`registry`); `close_all()` runs on daemon stop so no display leaks."""

    def __init__(self) -> None:
        self._stages: dict[int, VirtualStage] = {}
        # The async ui_open_app runs in the thread pool while ui_close_app / close_all run on the
        # event-loop thread — so add/pop/clear race across threads now. The lock guards only the dict
        # ops; the slow `stage.close()` (subprocess terminate) runs OUTSIDE it so it can't stall the
        # registry or self-deadlock.
        self._lock = threading.Lock()

    def add(self, stage: VirtualStage) -> None:
        if stage.app_pid is not None:
            with self._lock:
                self._stages[stage.app_pid] = stage

    def get(self, app_pid: int) -> VirtualStage | None:
        with self._lock:
            return self._stages.get(app_pid)

    def close(self, app_pid: int) -> bool:
        """Close + drop the stage for `app_pid`. Returns True if one was live."""
        with self._lock:
            stage = self._stages.pop(app_pid, None)
        if stage is None:
            return False
        stage.close()  # outside the lock — terminating the helper process is slow
        return True

    def close_all(self) -> None:
        with self._lock:
            stages = list(self._stages.values())
            self._stages.clear()
        for stage in stages:
            stage.close()


registry = StageRegistry()


def _resolve_bundle_id(app: str) -> str | None:
    """Best-effort bundle id for an app NAME (so `routing.stage_strategy` can classify it). A bundle id
    passed straight through (`com.x.y`, no spaces) is returned as-is. Darwin-only via `osascript`; None
    on failure → routing treats it as single-instance (conservative — ask to borrow, don't spawn)."""
    a = app.strip()
    if "." in a and " " not in a:
        return a  # already a bundle id
    if platform.system() != "Darwin":
        return None
    try:
        out = subprocess.run(
            ["osascript", "-e", f'id of app "{a}"'], capture_output=True, timeout=5
        ).stdout
        bid = out.decode("utf-8", "replace").strip()
        return bid or None
    except (subprocess.SubprocessError, OSError):
        return None


def open_app(
    app: str,
    url: str,
    *,
    bundle_id: str | None = None,
    resolve_bundle: Any = _resolve_bundle_id,
    stage_opener: Any = None,
) -> dict[str, Any]:
    """Get a no-steal working surface for `app` showing `url`, picking the path by `stage_strategy`:

    - multi-instance (browsers) → spawn the agent's OWN instance on an off-screen virtual display and
      register it; returns `{ok, strategy:"virtual_stage", app_pid, window_id, display_id, bounds}` — the
      caller drives `app_pid` with the normal verbs (the actuator resolves the staged window from the pid).
    - single-instance / unknown → `{ok, strategy:"borrow", needs_consent: True, app, bundle_id}` — the
      caller must get the user's consent (borrow dialog) before operating their one copy in place.

    A virtual-stage spawn that FAILS degrades to the borrow signal (never a silent steal): the result
    carries `fallback_from:"virtual_stage"` + the underlying error so the caller can explain the downgrade.
    """
    opener = stage_opener or VirtualStage.open
    bid = bundle_id or resolve_bundle(app)
    strat = routing.stage_strategy(bid)
    if strat == "virtual_stage":
        st = opener(app=app, url=url)
        if isinstance(st, VirtualStage):
            registry.add(st)
            return {
                "ok": True,
                "strategy": "virtual_stage",
                "app_pid": st.app_pid,
                "window_id": st.window_id,
                "display_id": st.display_id,
                "bounds": st.bounds,
                "window_bounds": st.window_bounds,
            }
        # spawn failed → degrade to borrow, never operate the user's window unannounced
        return {
            "ok": True,
            "strategy": "borrow",
            "needs_consent": True,
            "app": app,
            "bundle_id": bid,
            "fallback_from": "virtual_stage",
            "detail": st,
        }
    return {"ok": True, "strategy": "borrow", "needs_consent": True, "app": app, "bundle_id": bid}

"""Spawn `mac-ax-actuator` — the "hands" binary. Mirrors `capture/ax_capture.py`'s resolve/compile.

`snapshot()` reads the addressable element graph; `act()` performs a verb on an element id and
returns the before/after AX diff (the action feedback). Darwin-only; returns a typed error dict off
macOS or when the binary/AX trust is unavailable.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
from pathlib import Path
from typing import Any

from ..logger import get

logger = get("persome.actuation")

# An `act` walks the app's AX tree twice (before+after for the diff feedback); on a large app
# (e.g. Finder, ~1600 elements) each walk is ~3.5s, so two full walks + a slow env could exceed a
# 10s budget → a hard kill with no output → `actuator_failed` even though the action itself landed
# (perform is ~50ms). `--cache-before` (passed by act/key/type/clickxy) drops the redundant before
# walk to ~1×, but a cold-cache or heavy-env single walk still needs headroom (#466).
_SUBPROCESS_TIMEOUT = 20


def _resolve_actuator_path() -> Path | None:
    """Find or build mac-ax-actuator (env override → bundled → dev source), mirroring ax_capture."""
    if platform.system() != "Darwin":
        return None
    override = (os.environ.get("PERSOME_AX_ACTUATOR") or os.environ.get("MENS_CONTEXT_AX_ACTUATOR"))  # Mens is the legacy name
    if override:
        p = Path(override).expanduser().resolve()
        if p.is_file() and os.access(p, os.X_OK):
            return p

    from ..capture.ax_capture import _maybe_compile  # reuse the dev compile helper

    candidates: list[Path] = []
    try:
        from importlib.resources import files as _pkg_files

        candidates.append(Path(str(_pkg_files("persome").joinpath("_bundled"))) / "mac-ax-actuator")
    except (ModuleNotFoundError, ValueError):
        pass
    dev_root = Path(__file__).resolve().parents[3]  # .../persome-core/
    candidates.append(dev_root / "resources" / "mac-ax-actuator")

    for binary in candidates:
        src = binary.with_suffix(".swift")
        if src.is_file():
            _maybe_compile(src, binary)
        if binary.is_file() and os.access(binary, os.X_OK):
            return binary
    return None


def _stderr_tail(raw: bytes | None, limit: int = 400) -> str:
    """The last `limit` chars of a subprocess's stderr — the actuator stamps `[act] phase=…`
    breadcrumbs there (#466), so this tail is what turns an unreproducible wedge into a
    diagnosis. '' when there was none."""
    if not raw:
        return ""
    return raw[-limit * 4 :].decode("utf-8", "replace")[-limit:].strip()


def _run(args: list[str]) -> dict[str, Any]:
    binary = _resolve_actuator_path()
    if binary is None:
        return {"ok": False, "error": "actuator_unavailable"}
    proc: subprocess.CompletedProcess[bytes] | None = None
    try:
        proc = subprocess.run(
            [str(binary), *args], capture_output=True, timeout=_SUBPROCESS_TIMEOUT
        )
        return json.loads(proc.stdout.decode("utf-8", "replace"))
    except subprocess.TimeoutExpired as exc:
        # The helper's own 8s act deadline (#466) should fail first with a structured error;
        # reaching THIS timeout means even that didn't run (spawn wedge / non-act subcommand).
        # Keep the stderr phase breadcrumbs — they are the only diagnosis a wedge leaves behind.
        tail = _stderr_tail(exc.stderr)
        logger.warning("actuator run timed out (%ss): %s … stderr: %s", exc.timeout, args[:3], tail)
        return {"ok": False, "error": "actuator_timeout", "stderr_tail": tail}
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        # OSError covers the binary vanishing / losing +x between the os.access check and the
        # spawn; ValueError covers bad/garbled JSON output. Either returns a clean error dict so
        # a gated tool never propagates an exception.
        tail = _stderr_tail(proc.stderr if proc is not None else None)
        logger.warning("actuator run failed: %s … stderr: %s", exc, tail)
        out: dict[str, Any] = {"ok": False, "error": "actuator_failed"}
        if tail:
            out["stderr_tail"] = tail
        return out


def is_trusted() -> bool:
    return bool(_run(["trust"]).get("trusted"))


def _target(pid: int | None, app: str | None) -> list[str]:
    if pid is not None:
        return ["--pid", str(pid)]
    if app:
        return ["--app", app]
    return []


def snapshot(*, pid: int | None = None, app: str | None = None, depth: int = 60) -> dict[str, Any]:
    """Read the addressable element graph for an app (by pid or name)."""
    return _run(["snapshot", *_target(pid, app), "--depth", str(depth)])


def act(
    *,
    pid: int | None = None,
    app: str | None = None,
    element_id: str,
    verb: str,
    text: str | None = None,
    note: str = "",
    show_cursor: bool = True,
    show_boxes: bool = False,
    background: bool = False,
) -> dict[str, Any]:
    """Perform `verb` on `element_id`; returns `{ok, error?, verb, diff:[...]}` (diff = AX feedback).

    `note` is a SHORT "what Persome is doing this step" string shown in the cursor bubble (e.g.
    "正在给 xxx 发送消息"). `show_cursor` (default on) flashes the Persome cursor at the action point so
    the user sees Persome operating the app — like Claude Code's computer-use cursor. `show_boxes`
    additionally frames the app's element bboxes (an option, default off).
    """
    args = ["act", *_target(pid, app), "--id", element_id, "--verb", verb, "--cache-before"]
    if text is not None:
        args += ["--text", text]
    if note:
        args += ["--note", note]
    if not show_cursor:
        args += ["--no-cursor"]
    if show_boxes:
        args += ["--show-boxes"]
    return _run(args)


def _act_freeform(
    verb_args: list[str],
    *,
    pid: int | None,
    app: str | None,
    note: str,
    show_cursor: bool,
    show_boxes: bool,
    background: bool = False,
) -> dict[str, Any]:
    """Shared tail for the no-element-id verbs (key/type/clickxy): they post a CGEvent globally and
    don't resolve an AX element, so they carry no `--id`. `background=True` uses the SkyLight no-steal
    path (`--bg-mode skylight`): the event lands in the TARGET app's window without moving the real
    cursor or stealing focus — works even when the app is backgrounded (Electron/Chromium)."""
    args = ["act", *_target(pid, app), *verb_args, "--cache-before"]
    if note:
        args += ["--note", note]
    if not show_cursor:
        args += ["--no-cursor"]
    if show_boxes:
        args += ["--show-boxes"]
    if background:
        args += ["--bg-mode", "skylight"]
    return _run(args)


def key(
    keys: str,
    *,
    pid: int | None = None,
    app: str | None = None,
    note: str = "",
    show_cursor: bool = True,
    show_boxes: bool = False,
    background: bool = False,
) -> dict[str, Any]:
    """Post a key combo (e.g. `enter`, `cmd+a`, `cmd+shift+p`, `shift+=`) — for shortcuts, menu keys,
    Return-to-submit, and keyboard text entry that AX `setvalue` can't do (e.g. TextEdit's doc body)."""
    return _act_freeform(
        ["--verb", "key", "--keys", keys],
        pid=pid,
        app=app,
        note=note,
        show_cursor=show_cursor,
        show_boxes=show_boxes,
        background=background,
    )


def type_text(
    text: str,
    *,
    pid: int | None = None,
    app: str | None = None,
    note: str = "",
    show_cursor: bool = True,
    show_boxes: bool = False,
    background: bool = False,
) -> dict[str, Any]:
    """Type Unicode text into the CURRENTLY FOCUSED field (incl. Chinese) — for pixel/Electron search
    boxes that AX can't address by id (WeChat search, etc.). Does NOT press Return."""
    return _act_freeform(
        ["--verb", "type", "--text", text],
        pid=pid,
        app=app,
        note=note,
        show_cursor=show_cursor,
        show_boxes=show_boxes,
        background=background,
    )


def clickxy(
    x: float,
    y: float,
    *,
    pid: int | None = None,
    app: str | None = None,
    note: str = "",
    show_cursor: bool = True,
    show_boxes: bool = False,
    background: bool = False,
) -> dict[str, Any]:
    """Click at a screen coordinate (top-left origin) — the fallback for pixel-drawn controls an
    OCR locate found that AX can't reach. The cursor warps back after, so it only flickers."""
    return _act_freeform(
        ["--verb", "clickxy", "--x", str(x), "--y", str(y)],
        pid=pid,
        app=app,
        note=note,
        show_cursor=show_cursor,
        show_boxes=show_boxes,
        background=background,
    )


def activate(app: str) -> dict[str, Any]:
    """Bring an app to the front before operating it. Accepts a bundle id (com.apple.calculator) or an
    app name (Calculator). Best-effort; returns {ok, app}."""
    if platform.system() != "Darwin":
        return {"ok": False, "error": "actuator_unavailable"}
    looks_like_bundle = "." in app and " " not in app
    try:
        if looks_like_bundle:
            subprocess.run(["open", "-b", app], capture_output=True, timeout=_SUBPROCESS_TIMEOUT)
            script = f'tell application id "{app}" to activate'
        else:
            subprocess.run(["open", "-a", app], capture_output=True, timeout=_SUBPROCESS_TIMEOUT)
            script = f'tell application "{app}" to activate'
        subprocess.run(
            ["osascript", "-e", script], capture_output=True, timeout=_SUBPROCESS_TIMEOUT
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning("activate failed: %s", exc)
        return {"ok": False, "error": "activate_failed", "app": app}
    return {"ok": True, "app": app}

"""cmux signal source: inject real terminal text into captures (issue #558).

cmux (``com.cmuxterm.app``) renders terminals on the GPU, so its AX tree
carries almost no text (spike baseline: ~30 chars median per app subtree).
But cmux exposes a local unix-socket RPC — newline-delimited JSON at
``~/Library/Application Support/cmux/cmux-<uid>.sock``
(``{"id":N,"method":"...","params":{}}`` → ``{"id":N,"ok":true,"result":{}}``)
— whose ``system.tree`` + ``surface.read_text`` methods return the lossless
on-screen terminal text at zero external cost. The official cmux CLI
(``read-screen`` / ``capture-pane``) is a thin shell over this same socket.

When a capture's frontmost bundle is cmux, :func:`maybe_inject` reads the
visible terminal surfaces over the socket and appends their text to the
capture's ``visible_text``. Downstream (timeline ``_format_events``,
``focus_excerpt``, captures FTS) consumes ``visible_text`` as-is, so no
cmux-specific modeling path exists anywhere else.

Hot-path discipline:
  * the whole socket conversation runs under one sub-second deadline;
  * ANY failure (no socket, hung server, protocol drift) degrades silently
    to the AX-only capture — the warning is rate-limited so a dead socket
    never spams ``capture.log``;
  * on successful injection the caller skips the OCR fallback for this
    window (the terminal text is already lossless).
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import time
from pathlib import Path
from typing import Any

from ..config import CaptureConfig
from ..logger import get

logger = get("persome.capture")

CMUX_BUNDLE_ID = "com.cmuxterm.app"

# Total wall-clock budget for connect + system.tree + every surface.read_text.
# Keeps the capture hot path bounded even when cmux hangs mid-conversation.
_TOTAL_DEADLINE_SECONDS = 0.8
# Character budgets. Terminal text keeps its TAIL (most recent output).
# The total mirrors the order of magnitude of s1_parser._VISIBLE_TEXT_MAX.
_SURFACE_TEXT_MAX = 6_000
_TOTAL_TEXT_MAX = 12_000
_MAX_SURFACES = 6

_RECV_CHUNK = 65_536
_MAX_RESPONSE_BYTES = 4_000_000  # hard stop against a misbehaving server

# Rate-limit failure warnings: first failure logs, then at most one per gap.
_WARN_MIN_GAP_SECONDS = 600.0
_last_warn_ts = 0.0


class CmuxRpcError(RuntimeError):
    """Protocol-level failure talking to the cmux socket."""


class CmuxMethodError(CmuxRpcError):
    """The server answered ``ok: false`` for one method call.

    Non-fatal per surface: the live tree can drift from ``read_text``'s view
    (e.g. a surface listed as ``terminal`` that meanwhile became a file
    preview answers "Surface is not a terminal"). Skip it, keep the rest.
    """


def default_socket_path() -> Path:
    return Path.home() / "Library" / "Application Support" / "cmux" / f"cmux-{os.getuid()}.sock"


def _warn_limited(msg: str, *args: Any) -> None:
    global _last_warn_ts
    now = time.monotonic()
    if now - _last_warn_ts >= _WARN_MIN_GAP_SECONDS:
        _last_warn_ts = now
        logger.warning(msg, *args)


class CmuxClient:
    """Minimal newline-delimited JSON RPC client over the cmux unix socket.

    One instance = one connection = one deadline. Use as a context manager.
    """

    def __init__(
        self,
        socket_path: Path | str,
        deadline_seconds: float = _TOTAL_DEADLINE_SECONDS,
    ) -> None:
        self._path = str(socket_path)
        self._deadline = time.monotonic() + deadline_seconds
        self._sock: socket.socket | None = None
        self._buf = b""
        self._next_id = 0

    def __enter__(self) -> CmuxClient:
        # connect() can raise (cmux 崩溃 / socket 残留但不再 accept). __exit__ only
        # runs after __enter__ *returns*, so a raising connect would leave the
        # socket fd to be reclaimed non-deterministically by GC (#570). Close it
        # explicitly on failure before re-raising.
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self._remaining())
            sock.connect(self._path)
        except BaseException:
            sock.close()
            raise
        self._sock = sock
        return self

    def __exit__(self, *exc: object) -> None:
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None

    def _remaining(self) -> float:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise CmuxRpcError("deadline exceeded")
        return remaining

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send one request and return the ``result`` payload.

        Raises :class:`CmuxRpcError` on deadline, protocol error, or
        ``ok != true`` responses.
        """
        assert self._sock is not None, "CmuxClient used outside `with` block"
        self._next_id += 1
        request = {"id": self._next_id, "method": method, "params": params or {}}
        self._sock.settimeout(self._remaining())
        self._sock.sendall(json.dumps(request).encode("utf-8") + b"\n")
        line = self._read_line()
        try:
            response = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CmuxRpcError(f"{method}: invalid JSON response: {exc}") from exc
        if not isinstance(response, dict):
            raise CmuxRpcError(f"{method}: unexpected response shape: {str(response)[:200]}")
        if not response.get("ok"):
            raise CmuxMethodError(f"{method} failed: {str(response)[:200]}")
        return response.get("result")

    def _read_line(self) -> bytes:
        assert self._sock is not None
        while b"\n" not in self._buf:
            if len(self._buf) > _MAX_RESPONSE_BYTES:
                raise CmuxRpcError("response too large")
            self._sock.settimeout(self._remaining())
            chunk = self._sock.recv(_RECV_CHUNK)
            if not chunk:
                raise CmuxRpcError("connection closed mid-response")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line


def _visible_terminal_surfaces(tree: dict[str, Any]) -> list[dict[str, str]]:
    """Walk ``system.tree``: visible windows → selected workspace → panes →
    selected terminal surfaces.

    Returns ``[{"workspace_title", "surface_title", "id"}, ...]`` in tree
    order. Browser/filepreview surfaces and unselected (hidden) tabs are
    skipped — only what is actually on screen gets injected. ``id`` is the
    surface UUID: ``surface.read_text`` only honours ``surface_id`` (the
    ``surface``/``surface_ref`` spellings are ignored by the server and fall
    back to the focused surface — verified against the live socket).
    """
    found: list[dict[str, str]] = []
    for window in tree.get("windows") or []:
        if window.get("visible") is False:
            continue
        selected_ws_id = window.get("selected_workspace_id")
        for workspace in window.get("workspaces") or []:
            selected = workspace.get("selected")
            if selected is None:
                selected = workspace.get("id") == selected_ws_id
            if not selected:
                continue
            for pane in workspace.get("panes") or []:
                for surface in pane.get("surfaces") or []:
                    if surface.get("type") != "terminal":
                        continue
                    if surface.get("selected_in_pane") is False:
                        continue
                    surface_id = surface.get("id")
                    if not surface_id:
                        continue
                    found.append(
                        {
                            "workspace_title": (workspace.get("title") or "").strip(),
                            "surface_title": (surface.get("title") or "").strip(),
                            "id": surface_id,
                        }
                    )
    return found


def _section_header(surface: dict[str, str]) -> str:
    ws_title = surface["workspace_title"]
    surf_title = surface["surface_title"]
    if surf_title and surf_title != ws_title:
        label = f"{ws_title} · {surf_title}" if ws_title else surf_title
    else:
        label = ws_title or surface["id"]
    return f"### [cmux terminal] {label}"


def collect_text(
    socket_path: Path | str | None = None,
    *,
    deadline_seconds: float = _TOTAL_DEADLINE_SECONDS,
) -> str | None:
    """Read the visible cmux terminal surfaces and render them as sections.

    Returns the combined text or ``None`` when cmux is not reachable, the
    tree has no visible terminal text, or anything fails (silent degrade).
    """
    path = Path(socket_path) if socket_path is not None else default_socket_path()
    if not path.exists():
        return None  # cmux not running — not an error, stay silent
    try:
        with CmuxClient(path, deadline_seconds) as client:
            tree = client.call("system.tree") or {}
            surfaces = _visible_terminal_surfaces(tree)
            sections: list[str] = []
            total = 0
            for surface in surfaces[:_MAX_SURFACES]:
                try:
                    result = client.call("surface.read_text", {"surface_id": surface["id"]}) or {}
                except CmuxMethodError:
                    continue  # tree/type drift on one surface; keep the rest
                text = (result.get("text") or "").strip("\n")
                if not text.strip():
                    continue
                if len(text) > _SURFACE_TEXT_MAX:
                    # keep the tail: the most recent terminal output
                    text = "...(truncated)\n" + text[-_SURFACE_TEXT_MAX:]
                section = f"{_section_header(surface)}\n{text}"
                sections.append(section)
                total += len(section)
                if total >= _TOTAL_TEXT_MAX:
                    break
            if not sections:
                return None
            joined = "\n\n".join(sections)
            if len(joined) > _TOTAL_TEXT_MAX:
                joined = joined[:_TOTAL_TEXT_MAX] + "\n...(truncated)"
            return joined
    except (OSError, ValueError, CmuxRpcError) as exc:
        _warn_limited("cmux source failed, degrading to AX-only capture: %s", exc)
        return None


def maybe_inject(capture: dict[str, Any], cfg: CaptureConfig) -> bool:
    """Append real cmux terminal text to ``capture['visible_text']``.

    No-op (returns ``False``) unless ``cfg.cmux_source_enabled`` and the
    capture's frontmost bundle is cmux. On success sets
    ``capture['cmux_text_injected'] = True`` and returns ``True`` so the
    caller can skip the OCR fallback for this window.
    """
    if not getattr(cfg, "cmux_source_enabled", False):
        return False
    meta = capture.get("window_meta") or {}
    if meta.get("bundle_id") != CMUX_BUNDLE_ID:
        return False
    text = collect_text()
    if not text:
        return False
    base = capture.get("visible_text") or ""
    capture["visible_text"] = f"{base}\n\n{text}" if base else text
    capture["cmux_text_injected"] = True
    return True

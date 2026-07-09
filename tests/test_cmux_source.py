"""Tests for the cmux signal source (capture/cmux_source.py, issue #558).

A fake unix-socket server speaks the newline-delimited JSON RPC protocol so
no real cmux is needed. Fixture content is sanitized (no real paths/output).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest

from persome.capture import cmux_source, window_meta
from persome.capture import scheduler as sched_mod
from persome.capture.ax_models import AXCaptureResult
from persome.config import CaptureConfig

# ─── Sanitized protocol fixtures ────────────────────────────────────────────

TREE: dict[str, Any] = {
    "active": {"window_ref": "window:1", "surface_ref": "surface:1"},
    "windows": [
        {
            "ref": "window:1",
            "visible": True,
            "selected_workspace_id": "WS-1",
            "workspaces": [
                {
                    "id": "WS-1",
                    "selected": True,
                    "title": "build pipeline",
                    "panes": [
                        {
                            "surfaces": [
                                {
                                    "type": "terminal",
                                    "ref": "surface:1",
                                    "id": "UUID-1",
                                    "title": "run tests",
                                    "selected_in_pane": True,
                                },
                                {
                                    "type": "terminal",
                                    "ref": "surface:2",
                                    "id": "UUID-2",
                                    "title": "hidden tab",
                                    "selected_in_pane": False,
                                },
                            ]
                        },
                        {
                            "surfaces": [
                                {
                                    "type": "browser",
                                    "ref": "surface:3",
                                    "id": "UUID-3",
                                    "title": "docs",
                                    "selected_in_pane": True,
                                },
                            ]
                        },
                        {
                            "surfaces": [
                                {
                                    "type": "terminal",
                                    "ref": "surface:5",
                                    "id": "UUID-5",
                                    "title": "watch logs",
                                    "selected_in_pane": True,
                                },
                            ]
                        },
                    ],
                },
                {
                    "id": "WS-2",
                    "selected": False,
                    "title": "background workspace",
                    "panes": [
                        {
                            "surfaces": [
                                {
                                    "type": "terminal",
                                    "ref": "surface:4",
                                    "id": "UUID-4",
                                    "title": "should not be read",
                                    "selected_in_pane": True,
                                }
                            ]
                        }
                    ],
                },
            ],
        },
        {
            "ref": "window:2",
            "visible": False,
            "selected_workspace_id": "WS-9",
            "workspaces": [
                {
                    "id": "WS-9",
                    "selected": True,
                    "title": "hidden window",
                    "panes": [
                        {
                            "surfaces": [
                                {
                                    "type": "terminal",
                                    "ref": "surface:9",
                                    "id": "UUID-9",
                                    "title": "invisible",
                                    "selected_in_pane": True,
                                }
                            ]
                        }
                    ],
                }
            ],
        },
    ],
}

SURFACE_TEXTS = {
    "UUID-1": "$ make test\nrunning 42 checks\nall green",
    "UUID-5": "$ tail -f app.log\nINFO ready",
}

Handler = Callable[[dict[str, Any]], dict[str, Any] | None]


def default_handler(req: dict[str, Any]) -> dict[str, Any] | None:
    method = req.get("method")
    if method == "system.tree":
        return {"id": req["id"], "ok": True, "result": TREE}
    if method == "surface.read_text":
        sid = (req.get("params") or {}).get("surface_id", "")
        text = SURFACE_TEXTS.get(sid, "")
        return {"id": req["id"], "ok": True, "result": {"text": text, "surface_id": sid}}
    return {"id": req["id"], "ok": False, "error": f"unknown method {method}"}


# ─── Fake server fixture ────────────────────────────────────────────────────


@pytest.fixture
def fake_cmux_server():
    """Start fake servers; yields a factory ``start(handler) -> socket_path``.

    ``handler(request) -> response | None``; ``None`` means "never reply"
    (the connection stays open so the client hits its deadline).
    """
    created: list[tuple[socket.socket, threading.Event, threading.Thread, str]] = []

    def _start(handler: Handler = default_handler) -> str:
        # /tmp prefix keeps the path under the 104-char AF_UNIX limit on macOS
        tmpdir = tempfile.mkdtemp(prefix="cmuxtest-", dir="/tmp")
        sock_path = os.path.join(tmpdir, "cmux.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        server.listen(2)
        stop = threading.Event()

        def _serve_conn(conn: socket.socket) -> None:
            buf = b""
            conn.settimeout(2)
            try:
                while not stop.is_set():
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        resp = handler(json.loads(line))
                        if resp is None:
                            continue  # hang: never reply, keep connection open
                        conn.sendall(json.dumps(resp).encode("utf-8") + b"\n")
            except (TimeoutError, OSError):
                return

        def _serve() -> None:
            server.settimeout(0.2)
            while not stop.is_set():
                try:
                    conn, _ = server.accept()
                except TimeoutError:
                    continue
                except OSError:
                    return
                with conn:
                    _serve_conn(conn)

        thread = threading.Thread(target=_serve, daemon=True)
        thread.start()
        created.append((server, stop, thread, tmpdir))
        return sock_path

    yield _start

    for server, stop, thread, tmpdir in created:
        stop.set()
        server.close()
        thread.join(timeout=2)
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── collect_text: protocol client + tree walking ───────────────────────────


class TestCollectText:
    def test_happy_path_reads_visible_selected_terminal(self, fake_cmux_server) -> None:
        sock = fake_cmux_server()
        text = cmux_source.collect_text(sock)
        assert text is not None
        # section header carries workspace/surface titles
        assert "[cmux terminal] build pipeline · run tests" in text
        # real terminal body present
        assert "$ make test" in text
        assert "all green" in text

    def test_skips_browser_unselected_and_invisible(self, fake_cmux_server) -> None:
        read_refs: list[str] = []

        def handler(req: dict[str, Any]) -> dict[str, Any] | None:
            if req.get("method") == "surface.read_text":
                read_refs.append((req.get("params") or {}).get("surface_id", ""))
            return default_handler(req)

        sock = fake_cmux_server(handler)
        cmux_source.collect_text(sock)
        # not :2 (unselected), :3 (browser), :4/:9 (hidden)
        assert read_refs == ["UUID-1", "UUID-5"]

    def test_no_socket_returns_none(self, tmp_path) -> None:
        assert cmux_source.collect_text(tmp_path / "missing.sock") is None

    def test_error_response_returns_none(self, fake_cmux_server) -> None:
        sock = fake_cmux_server(lambda req: {"id": req["id"], "ok": False, "error": "nope"})
        assert cmux_source.collect_text(sock) is None

    def test_garbage_response_returns_none(self, fake_cmux_server) -> None:
        sock = fake_cmux_server(lambda req: {"garbage": 1})
        assert cmux_source.collect_text(sock) is None

    def test_one_bad_surface_keeps_the_rest(self, fake_cmux_server) -> None:
        # Live drift: tree says "terminal" but read_text rejects it (seen on
        # the real socket: "Surface is not a terminal"). Must skip, not abort.
        def handler(req: dict[str, Any]) -> dict[str, Any] | None:
            if req.get("method") == "surface.read_text":
                sid = (req.get("params") or {}).get("surface_id", "")
                if sid == "UUID-1":
                    return {
                        "id": req["id"],
                        "ok": False,
                        "error": {"message": "Surface is not a terminal"},
                    }
            return default_handler(req)

        sock = fake_cmux_server(handler)
        text = cmux_source.collect_text(sock)
        assert text is not None
        assert "$ make test" not in text
        assert "$ tail -f app.log" in text  # surface:5 survived

    def test_hung_server_bounded_by_deadline(self, fake_cmux_server) -> None:
        sock = fake_cmux_server(lambda req: None)  # accepts, never replies
        start = time.monotonic()
        assert cmux_source.collect_text(sock, deadline_seconds=0.3) is None
        assert time.monotonic() - start < 1.5

    def test_empty_terminal_text_returns_none(self, fake_cmux_server) -> None:
        def handler(req: dict[str, Any]) -> dict[str, Any] | None:
            if req.get("method") == "surface.read_text":
                return {"id": req["id"], "ok": True, "result": {"text": "  \n \n"}}
            return default_handler(req)

        sock = fake_cmux_server(handler)
        assert cmux_source.collect_text(sock) is None

    def test_long_text_keeps_tail(self, fake_cmux_server) -> None:
        def handler(req: dict[str, Any]) -> dict[str, Any] | None:
            if req.get("method") == "surface.read_text":
                body = "old line\n" * 2000 + "FINAL RECENT LINE"
                return {"id": req["id"], "ok": True, "result": {"text": body}}
            return default_handler(req)

        sock = fake_cmux_server(handler)
        text = cmux_source.collect_text(sock)
        assert text is not None
        assert "FINAL RECENT LINE" in text  # tail (most recent output) survives
        assert "...(truncated)" in text
        assert len(text) <= cmux_source._TOTAL_TEXT_MAX + 100


# ─── _visible_terminal_surfaces: pure tree walking ──────────────────────────


class TestTreeWalk:
    def test_falls_back_to_selected_workspace_id(self) -> None:
        tree = {
            "windows": [
                {
                    "visible": True,
                    "selected_workspace_id": "WS-A",
                    "workspaces": [
                        {
                            "id": "WS-A",
                            "title": "ws-a",
                            # no "selected" key → matched via selected_workspace_id
                            "panes": [
                                {
                                    "surfaces": [
                                        {
                                            "type": "terminal",
                                            "ref": "surface:7",
                                            "id": "UUID-7",
                                            "title": "t",
                                            "selected_in_pane": True,
                                        }
                                    ]
                                }
                            ],
                        },
                        {"id": "WS-B", "title": "ws-b", "panes": []},
                    ],
                }
            ]
        }
        surfaces = cmux_source._visible_terminal_surfaces(tree)
        assert [s["id"] for s in surfaces] == ["UUID-7"]

    def test_empty_tree(self) -> None:
        assert cmux_source._visible_terminal_surfaces({}) == []
        assert cmux_source._visible_terminal_surfaces({"windows": None}) == []


# ─── maybe_inject: gate + append semantics ──────────────────────────────────


def _cfg(**kwargs) -> CaptureConfig:
    return CaptureConfig(**kwargs)


class TestMaybeInject:
    def test_appends_to_existing_visible_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            cmux_source, "collect_text", lambda *a, **k: "### [cmux terminal] x\nbody"
        )
        capture = {
            "window_meta": {"bundle_id": cmux_source.CMUX_BUNDLE_ID},
            "visible_text": "## cmux [active]",
        }
        assert cmux_source.maybe_inject(capture, _cfg()) is True
        assert capture["visible_text"] == "## cmux [active]\n\n### [cmux terminal] x\nbody"
        assert capture["cmux_text_injected"] is True

    def test_sets_visible_text_when_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cmux_source, "collect_text", lambda *a, **k: "terminal body")
        capture = {"window_meta": {"bundle_id": cmux_source.CMUX_BUNDLE_ID}, "visible_text": ""}
        assert cmux_source.maybe_inject(capture, _cfg()) is True
        assert capture["visible_text"] == "terminal body"

    def test_noop_for_other_bundle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            cmux_source, "collect_text", lambda *a, **k: pytest.fail("must not be called")
        )
        capture = {"window_meta": {"bundle_id": "com.apple.Safari"}, "visible_text": "x"}
        assert cmux_source.maybe_inject(capture, _cfg()) is False
        assert capture["visible_text"] == "x"
        assert "cmux_text_injected" not in capture

    def test_noop_when_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            cmux_source, "collect_text", lambda *a, **k: pytest.fail("must not be called")
        )
        capture = {"window_meta": {"bundle_id": cmux_source.CMUX_BUNDLE_ID}, "visible_text": ""}
        assert cmux_source.maybe_inject(capture, _cfg(cmux_source_enabled=False)) is False

    def test_collect_failure_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cmux_source, "collect_text", lambda *a, **k: None)
        capture = {"window_meta": {"bundle_id": cmux_source.CMUX_BUNDLE_ID}, "visible_text": "x"}
        assert cmux_source.maybe_inject(capture, _cfg()) is False
        assert capture["visible_text"] == "x"


# ─── scheduler wiring: injection skips OCR; failure preserves OCR path ──────


class FakeProvider:
    available = True

    def capture_frontmost(self, *, focused_window_only: bool = True):
        return AXCaptureResult(raw_json={"apps": []}, timestamp="2026-01-01T00:00:00Z", apps=[])


class _MockPath:
    def exists(self) -> bool:
        return False


def _sched_cfg(**kwargs) -> CaptureConfig:
    defaults = {
        "enable_ocr_fallback": True,
        "ocr_tier": "tiny",
        "include_screenshot": False,
    }
    defaults.update(kwargs)
    return CaptureConfig(**defaults)


def _patch_sched_common(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sched_mod.paths, "ensure_dirs", lambda: None)
    monkeypatch.setattr(sched_mod.paths, "paused_flag", lambda: _MockPath())
    monkeypatch.setattr(sched_mod, "_last_ocr_ts", {})
    monkeypatch.setattr(
        window_meta,
        "active_window",
        lambda: window_meta.WindowMeta(
            app_name="cmux", title="term", bundle_id=cmux_source.CMUX_BUNDLE_ID
        ),
    )
    monkeypatch.setattr(sched_mod.s1_parser, "enrich", lambda out: out.update({"visible_text": ""}))


class TestSchedulerWiring:
    def test_injection_appends_and_skips_ocr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_sched_common(monkeypatch)
        monkeypatch.setattr(cmux_source, "collect_text", lambda *a, **k: "$ real terminal text")
        monkeypatch.setattr(
            sched_mod.ocr_local,
            "recognize_detailed",
            lambda *a, **k: pytest.fail("OCR must be skipped"),
        )

        out = sched_mod._build_capture(_sched_cfg(), FakeProvider(), None)
        assert out is not None
        assert out["cmux_text_injected"] is True
        assert "$ real terminal text" in out["visible_text"]
        assert "ocr_submitted" not in out

    def test_injection_failure_falls_back_to_ocr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from PIL import Image

        _patch_sched_common(monkeypatch)
        monkeypatch.setattr(cmux_source, "collect_text", lambda *a, **k: None)
        monkeypatch.setattr(
            sched_mod.window_screenshot, "grab_focused_window", lambda: Image.new("RGB", (10, 10))
        )
        monkeypatch.setattr(
            sched_mod.window_screenshot, "pil_to_jpeg_bytes", lambda img, quality=85: b"jpeg"
        )
        monkeypatch.setattr(
            sched_mod.ocr_local,
            "recognize_detailed",
            lambda *a, **k: (["ocr text"], [[0, 0, 0, 0]], [0.9]),
        )

        out = sched_mod._build_capture(_sched_cfg(), FakeProvider(), None)
        assert out is not None
        assert "cmux_text_injected" not in out
        # _build_capture stashes the JPEG for OCR; the actual submit + ocr_submitted
        # marker happen in _write_capture (after the row is indexed).
        assert out.get("_ocr_pending_jpeg") == b"jpeg"

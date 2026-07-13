"""Unit tests for the AX watcher subprocess manager (``capture/watcher.py``).

Mock-based and platform-agnostic: they cover path resolution, availability,
and the no-op start path without spawning the real Swift binary or threads.
A real-macOS resolution smoke test carries ``@pytest.mark.macos``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from persome.capture import watcher
from persome.capture.watcher import AXWatcherProcess


def test_resolve_watcher_path_non_darwin_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watcher.platform, "system", lambda: "Linux")
    assert watcher._resolve_watcher_path() is None


def test_watcher_unavailable_when_no_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watcher, "_resolve_watcher_path", lambda: None)
    proc = AXWatcherProcess()
    assert proc.available is False
    assert proc.running is False
    # start() must be a safe no-op (no thread spawned) when unavailable.
    proc.start()
    assert proc._reader_thread is None
    assert proc.running is False


def test_on_event_sets_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watcher, "_resolve_watcher_path", lambda: None)
    proc = AXWatcherProcess()
    sentinel = []
    proc.on_event(lambda ev: sentinel.append(ev))
    assert proc._callback is not None
    proc._callback({"event_type": "focus"})
    assert sentinel == [{"event_type": "focus"}]


def test_available_true_when_binary_resolved(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    fake = tmp_path / "mac-ax-watcher"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setattr(watcher, "_resolve_watcher_path", lambda: fake)
    proc = AXWatcherProcess()
    assert proc.available is True
    assert proc.running is False  # not started yet


def _propagate_capture_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-enable propagation on the capture logger for caplog.

    ``logger.setup()`` (run by any earlier test that initializes logging)
    turns ``propagate`` off on persome loggers; caplog captures via the
    root logger, so flip it back for the duration of the test.
    """
    monkeypatch.setattr(logging.getLogger("persome.capture"), "propagate", True)


def _read_events_from_lines(
    proc: AXWatcherProcess, lines: list[str], *, return_code: int = 0
) -> int | None:
    """Drive ``_read_events`` over a real pipe carrying the given JSONL lines."""
    r_fd, w_fd = os.pipe()
    with os.fdopen(w_fd, "w") as w:
        for line in lines:
            w.write(line + "\n")
    reader = os.fdopen(r_fd, "r")
    try:
        proc._process = SimpleNamespace(
            stdout=reader, poll=lambda: return_code, wait=lambda: return_code
        )
        return proc._read_events()
    finally:
        proc._process = None
        reader.close()


def test_electron_ax_activated_logged_info_not_dispatched(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``_electron_ax_activated`` internal events (issue #556) are surfaced at
    INFO in the capture log — with bundle/reason/error codes — and are NOT
    forwarded to the event callback (internal ``_`` events never enter the
    capture pipeline)."""
    _propagate_capture_logs(monkeypatch)
    monkeypatch.setattr(watcher, "_resolve_watcher_path", lambda: None)
    proc = AXWatcherProcess()
    received: list[dict] = []
    proc.on_event(received.append)

    activation = {
        "event_type": "_electron_ax_activated",
        "pid": 65532,
        "app_name": "Feishu",
        "bundle_id": "com.electron.lark",
        "window_title": "",
        "timestamp": "2026-06-12T10:00:00+08:00",
        "details": {
            "reason": "app_activated",
            "set_manual_err": -25205,
            "set_enhanced_err": -25208,
        },
    }
    regular = {"event_type": "AXApplicationActivated", "bundle_id": "com.electron.lark"}
    with caplog.at_level(logging.DEBUG, logger="persome.capture"):
        _read_events_from_lines(
            proc,
            [
                json.dumps(activation),
                json.dumps({"event_type": "_watcher_started"}),
                json.dumps(regular),
            ],
        )

    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        "Electron AX activated" in m
        and "bundle=com.electron.lark" in m
        and "reason=app_activated" in m
        and "set_enhanced_err=-25208" in m
        for m in info_msgs
    )
    # Other internal events stay at DEBUG.
    debug_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("_watcher_started" in m for m in debug_msgs)
    # Only the regular (non-underscore) event reaches the callback.
    assert received == [regular]


def test_electron_ax_activated_missing_details_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A malformed activation event (no ``details``) must not break the reader."""
    _propagate_capture_logs(monkeypatch)
    monkeypatch.setattr(watcher, "_resolve_watcher_path", lambda: None)
    proc = AXWatcherProcess()
    received: list[dict] = []
    proc.on_event(received.append)

    with caplog.at_level(logging.INFO, logger="persome.capture"):
        _read_events_from_lines(
            proc, [json.dumps({"event_type": "_electron_ax_activated", "pid": 1})]
        )

    assert received == []
    assert any("Electron AX activated" in r.getMessage() for r in caplog.records)


def test_permission_denial_is_returned_without_stopping_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(watcher, "_resolve_watcher_path", lambda: None)
    proc = AXWatcherProcess()

    rc = _read_events_from_lines(proc, [], return_code=2)

    assert rc == 2
    assert not proc._stop_event.is_set()


def test_wait_for_accessibility_recovers_after_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watcher, "_resolve_watcher_path", lambda: None)
    proc = AXWatcherProcess(permission_poll_seconds=0.01)
    probes = iter([False, False, True])
    monkeypatch.setattr(watcher.ax_capture, "ax_trusted", lambda: next(probes))

    assert proc._wait_for_accessibility() is True


def test_run_loop_restarts_immediately_after_permission_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(watcher, "_resolve_watcher_path", lambda: None)
    proc = AXWatcherProcess()
    starts = 0
    results = iter([2, 0])

    def fake_start() -> None:
        nonlocal starts
        starts += 1
        proc._process = SimpleNamespace()

    def fake_read() -> int:
        result = next(results)
        if result == 0:
            proc._stop_event.set()
        return result

    monkeypatch.setattr(proc, "_start_process", fake_start)
    monkeypatch.setattr(proc, "_read_events", fake_read)
    monkeypatch.setattr(proc, "_wait_for_accessibility", lambda: True)

    proc._run_loop()

    assert starts == 2


@pytest.mark.macos
def test_resolve_watcher_path_darwin_smoke() -> None:
    """On real macOS, resolution returns a Path or None without raising."""
    result = watcher._resolve_watcher_path()
    assert result is None or hasattr(result, "is_file")


def test_request_accessibility_uses_one_shot_native_watcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "mac-ax-watcher"
    binary.write_text("", encoding="utf-8")
    binary.chmod(0o755)
    seen: list[list[str]] = []
    monkeypatch.setattr(watcher, "_resolve_watcher_path", lambda: binary)

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        seen.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(watcher.subprocess, "run", fake_run)

    assert watcher.request_accessibility_permission() is True
    assert seen == [[str(binary), "--request-accessibility"]]


def test_normal_watcher_start_uses_pure_accessibility_check() -> None:
    source = (Path(__file__).resolve().parents[1] / "resources" / "mac-ax-watcher.swift").read_text(
        encoding="utf-8"
    )
    normal_start = source.split("// Normal Runtime starts are pure checks.", 1)[1]

    assert "let trusted = AXIsProcessTrusted()" in normal_start
    assert "AXIsProcessTrustedWithOptions" not in normal_start


def test_native_watcher_filters_standard_placeholder_without_descendant_scan() -> None:
    source = (Path(__file__).resolve().parents[1] / "resources" / "mac-ax-watcher.swift").read_text(
        encoding="utf-8"
    )
    describe = source.split("func describeElement", 1)[1].split("func appInfoForElement", 1)[0]

    assert 'axString(element, "AXPlaceholderValue")' in source
    assert "directPlaceholderTexts(el, role: role)" in describe
    assert "placeholderTexts.contains(normalizedAXText(rawValue))" in describe
    assert "placeholderTexts.contains(rawTitle)" in describe
    assert "placeholderDescendantTexts" not in source
    callback = source.split("func interactionTapCallback", 1)[1].split(
        "let interaction = InteractionTapper", 1
    )[0]
    assert "handleMouseDown(event, type: type)" in callback
    assert "handleKeyDown(event)" in callback

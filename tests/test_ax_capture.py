"""Unit tests for the AX capture layer (``capture/ax_capture.py``).

These are **mock-based** and run on any platform — they exercise the parse /
dispatch / error-handling logic of ``MacAXHelperProvider`` and ``create_provider``
by stubbing the ``subprocess`` boundary, so they lift coverage on Linux CI too.

The handful of tests that need a *real* macOS Swift helper are marked
``@pytest.mark.macos`` and skip gracefully when no helper is available.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from persome.capture import ax_capture
from persome.capture.ax_capture import (
    MacAXHelperProvider,
    UnavailableAXProvider,
    _strip_frame_fields,
    create_provider,
)
from persome.capture.ax_models import AXCaptureResult

# --- _strip_frame_fields (pure) -------------------------------------------------


def test_strip_frame_fields_removes_frame_keys_recursively() -> None:
    payload = {
        "frame": {"x": 1},
        "name": "App",
        "windows": [
            {"frame": [0, 0], "title": "W", "elements": [{"frame": 1, "role": "AXButton"}]}
        ],
    }
    out = _strip_frame_fields(payload)
    assert "frame" not in out
    assert out["name"] == "App"
    assert "frame" not in out["windows"][0]
    assert out["windows"][0]["title"] == "W"
    assert "frame" not in out["windows"][0]["elements"][0]
    assert out["windows"][0]["elements"][0]["role"] == "AXButton"


def test_strip_frame_fields_passthrough_scalars() -> None:
    assert _strip_frame_fields("x") == "x"
    assert _strip_frame_fields(7) == 7
    assert _strip_frame_fields([1, 2]) == [1, 2]


# --- UnavailableAXProvider ------------------------------------------------------


def test_unavailable_provider_is_unavailable_and_returns_none() -> None:
    p = UnavailableAXProvider("no helper")
    assert p.available is False
    assert p.reason == "no helper"
    assert p.capture_frontmost() is None
    assert p.capture_all_visible() is None
    assert p.capture_app("Safari") is None


# --- MacAXHelperProvider._run (subprocess mocked) -------------------------------


def _provider() -> MacAXHelperProvider:
    return MacAXHelperProvider(helper_path="/fake/mac-ax-helper", depth=8, timeout=3)


def _fake_run_returning(stdout: str, returncode: int = 0, stderr: str = ""):
    def _run(args, **kwargs):  # noqa: ANN001, ANN003
        _run.captured_args = args  # type: ignore[attr-defined]
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)

    return _run


def test_run_success_builds_capture_result(monkeypatch: pytest.MonkeyPatch) -> None:
    tree = {
        "timestamp": "2026-05-29T00:00:00Z",
        "apps": [{"name": "Safari", "frame": {"x": 0}, "windows": []}],
    }
    monkeypatch.setattr(ax_capture.subprocess, "run", _fake_run_returning(json.dumps(tree)))

    result = _provider().capture_frontmost()

    assert isinstance(result, AXCaptureResult)
    assert result.timestamp == "2026-05-29T00:00:00Z"
    assert result.apps[0]["name"] == "Safari"
    # _strip_frame_fields ran over the payload
    assert "frame" not in result.apps[0]
    assert result.metadata == {"mode": "frontmost", "depth": 8, "platform": "macos", "raw": False}


def test_run_all_visible_sets_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_run_returning(json.dumps({"timestamp": "t", "apps": []}))
    monkeypatch.setattr(ax_capture.subprocess, "run", fake)
    result = _provider().capture_all_visible()
    assert result is not None
    assert result.metadata["mode"] == "all-visible"
    assert "--all-visible" in fake.captured_args


def test_run_app_name_args(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_run_returning(json.dumps({"timestamp": "t", "apps": []}))
    monkeypatch.setattr(ax_capture.subprocess, "run", fake)
    _provider().capture_app("Zoom", focused_window_only=True)
    args = fake.captured_args
    assert args[0] == "/fake/mac-ax-helper"
    assert "--app-name" in args and "Zoom" in args
    assert "--focused-window-only" in args
    assert "--depth" in args and "8" in args
    assert "--timeout" in args and "3" in args
    # app_name path must NOT also pass --all-visible
    assert "--all-visible" not in args


def test_run_permission_denied_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ax_capture.subprocess, "run", _fake_run_returning("", returncode=2))
    assert _provider().capture_frontmost() is None


def test_run_nonzero_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ax_capture.subprocess, "run", _fake_run_returning("", returncode=1, stderr="boom")
    )
    assert _provider().capture_frontmost() is None


def test_run_bad_json_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ax_capture.subprocess, "run", _fake_run_returning("not json{"))
    assert _provider().capture_frontmost() is None


def test_run_timeout_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(args, **kwargs):  # noqa: ANN001, ANN003
        raise subprocess.TimeoutExpired(cmd=args, timeout=10)

    monkeypatch.setattr(ax_capture.subprocess, "run", _raise)
    assert _provider().capture_frontmost() is None


def test_run_oserror_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(args, **kwargs):  # noqa: ANN001, ANN003
        raise OSError("exec format error")

    monkeypatch.setattr(ax_capture.subprocess, "run", _raise)
    assert _provider().capture_frontmost() is None


# --- create_provider (platform / helper resolution mocked) ----------------------


def test_create_provider_non_darwin_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ax_capture.platform, "system", lambda: "Linux")
    provider = create_provider()
    assert isinstance(provider, UnavailableAXProvider)
    assert provider.available is False


def test_create_provider_darwin_without_helper_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ax_capture.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ax_capture, "_resolve_helper_path", lambda: None)
    provider = create_provider()
    assert isinstance(provider, UnavailableAXProvider)


def test_create_provider_darwin_with_helper_is_macax(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    helper = tmp_path / "mac-ax-helper"
    helper.write_text("#!/bin/sh\n")
    monkeypatch.setattr(ax_capture.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ax_capture, "_resolve_helper_path", lambda: helper)
    provider = create_provider(depth=4, timeout=2, raw=True)
    assert isinstance(provider, MacAXHelperProvider)
    assert provider.available is True


# --- real macOS hardware smoke (keeps the `macos` marker live) ------------------


@pytest.mark.macos
def test_real_provider_smoke_does_not_raise() -> None:
    """On real macOS, create_provider + a capture must not raise.

    Skips when no Swift helper / AX permission is available, so it's safe on any
    Mac dev machine. This is the test that genuinely requires macOS hardware and
    keeps the `macos` marker non-empty.
    """
    provider = create_provider()
    if not provider.available:
        pytest.skip("mac-ax-helper not available on this machine")
    # Must return AXCaptureResult or None (permission denied), never raise.
    result = provider.capture_frontmost()
    assert result is None or isinstance(result, AXCaptureResult)

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
from pathlib import Path

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


def test_ax_trust_checks_only_helper_when_event_watcher_is_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = tmp_path / "mac-ax-helper"
    helper.write_text("", encoding="utf-8")
    helper.chmod(0o700)
    monkeypatch.setattr(ax_capture.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ax_capture, "_resolve_helper_path", lambda: helper)
    monkeypatch.setattr(ax_capture, "_binary_ax_trusted", lambda binary: binary == helper)
    ax_capture._ax_trust_cache.clear()

    assert ax_capture.ax_trusted(refresh=True, include_watcher=False) is True


def test_accessibility_request_targets_only_configured_principals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = tmp_path / "mac-ax-helper"
    helper.write_text("", encoding="utf-8")
    helper.chmod(0o700)
    commands: list[list[str]] = []
    monkeypatch.setattr(ax_capture.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ax_capture, "_resolve_helper_path", lambda: helper)

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(ax_capture.subprocess, "run", run)

    assert ax_capture.request_accessibility_permission(include_watcher=False) is True
    assert commands == [[str(helper), "--request-accessibility"]]


def test_native_helper_filters_focused_placeholder_and_keeps_tree_evidence() -> None:
    source = (Path(__file__).resolve().parents[1] / "resources" / "mac-ax-helper.swift").read_text(
        encoding="utf-8"
    )

    assert 'axString(element, "AXPlaceholderValue")' in source
    assert 'lowercased() == "placeholder"' in source
    assert "evidence = placeholderDescendantTexts(element)" in source
    traverse = source.split("func traverseElement", 1)[1].split("func processWindow", 1)[0]
    # Normal tree output retains the local pair as forensic evidence; Python
    # S1 sanitizes its authored-text projection without mutating raw AX.
    assert "confirmedPlaceholderTexts" not in traverse
    assert 'dict["AXPlaceholderValue"] = p' in source
    assert 'axString(element, "AXPlaceholderValue")' in traverse
    assert "placeholderValue: placeholderValue" in traverse
    focused = source.split("func focusedUIElementDict", 1)[1]
    assert "config.raw || !placeholderTexts.contains(raw)" in focused
    assert "placeholderTexts.contains(title)" in focused
    assert 'dict["AXPlaceholderValue"] = standardPlaceholder' in focused


def test_stable_native_binary_is_reused_across_reinstall_sources(
    ac_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_source = tmp_path / "first" / "mac-ax-helper.swift"
    second_source = tmp_path / "second" / "mac-ax-helper.swift"
    first_source.parent.mkdir()
    second_source.parent.mkdir()
    first_source.write_text("print(1)\n", encoding="utf-8")
    second_source.write_text("print(1)\n", encoding="utf-8")
    builds: list[Path] = []

    def compile_once(source: Path, binary: Path) -> None:
        builds.append(source)
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o700)

    monkeypatch.setattr(ax_capture, "_maybe_compile", compile_once)

    first = ax_capture._stable_native_binary(first_source, "mac-ax-helper")
    second = ax_capture._stable_native_binary(second_source, "mac-ax-helper")

    assert first is not None
    assert first.parent.parent == ac_root / "native"
    assert second == first
    assert builds == [first_source]
    old_bytes = first.read_bytes()

    second_source.write_text("print(2)\n", encoding="utf-8")
    upgraded = ax_capture._stable_native_binary(second_source, "mac-ax-helper")
    assert upgraded is not None and upgraded != first
    assert builds == [first_source, second_source]

    # A cancelled update can resolve the old source again without rebuilding
    # or changing the old code identity that already owns the TCC grant.
    rolled_back = ax_capture._stable_native_binary(first_source, "mac-ax-helper")
    assert rolled_back == first
    assert rolled_back.read_bytes() == old_bytes
    assert builds == [first_source, second_source]


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

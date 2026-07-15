"""Architecture-native OCR backend selection.

PaddlePaddle ships no macOS-Intel wheel, so x86_64 selects the system Apple
Vision helper. Hosts with neither backend still fail open without importing
Paddle or spawning a worker.
"""

from __future__ import annotations

import importlib.util

import pytest

from persome.capture import ocr_local, vision_ocr


@pytest.fixture(autouse=True)
def _reset_runtime_cache():
    """runtime_available() memoizes; reset it around each test so monkeypatch takes effect."""
    ocr_local._runtime_available = None
    ocr_local._runtime_backend = None
    yield
    ocr_local._runtime_available = None
    ocr_local._runtime_backend = None


def _patch_specs(monkeypatch: pytest.MonkeyPatch, *, present: bool) -> None:
    real = importlib.util.find_spec

    def fake(name: str, *a, **k):
        if name in ("paddle", "paddleocr"):
            return object() if present else None
        return real(name, *a, **k)

    monkeypatch.setattr(ocr_local.importlib.util, "find_spec", fake)


def test_runtime_available_false_when_no_backend(monkeypatch: pytest.MonkeyPatch):
    _patch_specs(monkeypatch, present=False)
    monkeypatch.setattr(ocr_local.platform, "system", lambda: "Linux")
    assert ocr_local.runtime_available() is False


def test_runtime_available_true_when_paddle_present(monkeypatch: pytest.MonkeyPatch):
    _patch_specs(monkeypatch, present=True)
    assert ocr_local.runtime_available() is True
    assert ocr_local.runtime_backend() == "paddle"


def test_intel_uses_vision_when_paddle_absent(monkeypatch: pytest.MonkeyPatch):
    _patch_specs(monkeypatch, present=False)
    monkeypatch.setattr(ocr_local.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ocr_local.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(vision_ocr, "available", lambda: True)

    assert ocr_local.runtime_available() is True
    assert ocr_local.runtime_backend() == "vision"
    assert ocr_local.models_available("tiny") is True


def test_intel_inproc_worker_routes_to_vision(monkeypatch: pytest.MonkeyPatch):
    ocr_local._runtime_available = True
    ocr_local._runtime_backend = "vision"
    expected = (["Intel text"], [[1, 2, 30, 40]], [0.95])
    monkeypatch.setattr(vision_ocr, "recognize_detailed", lambda image: expected)

    assert ocr_local._recognize_detailed_inproc(b"image") == expected


def test_ocr_entrypoints_degrade_cleanly_without_runtime(monkeypatch: pytest.MonkeyPatch):
    """With no backend, entrypoints return None without spawning the worker."""
    _patch_specs(monkeypatch, present=False)
    monkeypatch.setattr(ocr_local.platform, "system", lambda: "Linux")

    # Guard: neither the in-process predict nor the isolation worker may be entered.
    def _boom(*_a, **_k):
        raise AssertionError("OCR inference must not run when the paddle runtime is absent")

    monkeypatch.setattr(ocr_local, "_recognize_detailed_inproc", _boom)
    monkeypatch.setattr(ocr_local, "_get_engine", _boom)
    import persome.capture.ocr_subprocess as ocr_subprocess

    monkeypatch.setattr(ocr_subprocess, "get_client", _boom)

    assert ocr_local.recognize(b"\xff\xd8\xff-not-a-real-jpeg") is None
    assert ocr_local.recognize_detailed(b"\xff\xd8\xff-not-a-real-jpeg") is None
    assert ocr_local.warm() is False

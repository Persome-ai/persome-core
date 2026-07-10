"""Intel (x86_64) OCR-less degrade (issue #226).

paddlepaddle ships no macOS-Intel wheel, so the x86_64 daemon slice runs WITHOUT local OCR.
`ocr_local.runtime_available()` is the honest probe; when it's False every OCR entrypoint must
fail open cleanly — no paddle import, no worker spawn, no exception — so AX-based context/intent
keep working and only WeChat/Feishu OCR text is lost.
"""

from __future__ import annotations

import importlib.util

import pytest

from persome.capture import ocr_local


@pytest.fixture(autouse=True)
def _reset_runtime_cache():
    """runtime_available() memoizes; reset it around each test so monkeypatch takes effect."""
    ocr_local._runtime_available = None
    yield
    ocr_local._runtime_available = None


def _patch_specs(monkeypatch: pytest.MonkeyPatch, *, present: bool) -> None:
    real = importlib.util.find_spec

    def fake(name: str, *a, **k):
        if name in ("paddle", "paddleocr"):
            return object() if present else None
        return real(name, *a, **k)

    monkeypatch.setattr(ocr_local.importlib.util, "find_spec", fake)


def test_runtime_available_false_when_paddle_absent(monkeypatch: pytest.MonkeyPatch):
    _patch_specs(monkeypatch, present=False)
    assert ocr_local.runtime_available() is False


def test_runtime_available_true_when_paddle_present(monkeypatch: pytest.MonkeyPatch):
    _patch_specs(monkeypatch, present=True)
    assert ocr_local.runtime_available() is True


def test_ocr_entrypoints_degrade_cleanly_without_runtime(monkeypatch: pytest.MonkeyPatch):
    """With no paddle runtime (x86_64): recognize/recognize_detailed return None and warm() is a
    no-op — WITHOUT importing paddle or spawning the isolation worker."""
    _patch_specs(monkeypatch, present=False)

    # Guard: neither the in-process predict nor the isolation worker may be entered.
    def _boom(*_a, **_k):
        raise AssertionError("OCR inference must not run when the paddle runtime is absent")

    monkeypatch.setattr(ocr_local, "_recognize_detailed_inproc", _boom)
    monkeypatch.setattr(ocr_local, "_get_engine", _boom)
    import persome.capture.ocr_subprocess as ocr_subprocess

    monkeypatch.setattr(ocr_subprocess, "get_client", _boom)

    assert ocr_local.recognize(b"\xff\xd8\xff-not-a-real-jpeg") is None
    assert ocr_local.recognize_detailed(b"\xff\xd8\xff-not-a-real-jpeg") is None
    ocr_local.warm()  # clean no-op, no exception

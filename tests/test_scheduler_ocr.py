"""Tests for scheduler OCR fallback trigger logic (on-device PP-OCRv6)."""

from __future__ import annotations

import pytest
from PIL import Image

from persome.capture import ax_models, window_meta
from persome.capture import scheduler as sched_mod
from persome.config import CaptureConfig


class FakeProvider:
    available = True

    def capture_frontmost(self, *, focused_window_only: bool = True):
        return ax_models.AXCaptureResult(
            raw_json={"apps": []},
            timestamp="2026-01-01T00:00:00Z",
            apps=[],
        )


class _MockPath:
    def exists(self):
        return False


def _make_cfg(**kwargs) -> CaptureConfig:
    defaults = {
        "enable_ocr_fallback": True,
        "ocr_tier": "tiny",
        "ocr_min_gap_seconds": 15.0,
        "include_screenshot": False,
        "screenshot_max_width": 1920,
        "screenshot_jpeg_quality": 80,
        "ax_depth": 8,
        "ax_timeout_seconds": 3,
    }
    defaults.update(kwargs)
    return CaptureConfig(**defaults)


def _common_patches(
    monkeypatch: pytest.MonkeyPatch, app: str, title: str, visible_text: str
) -> None:
    monkeypatch.setattr(sched_mod.paths, "ensure_dirs", lambda: None)
    monkeypatch.setattr(sched_mod.paths, "paused_flag", lambda: _MockPath())
    monkeypatch.setattr(sched_mod, "_last_ocr_ts", {})
    monkeypatch.setattr(
        window_meta,
        "active_window",
        lambda: window_meta.WindowMeta(app_name=app, title=title, bundle_id="com.test"),
    )
    monkeypatch.setattr(
        sched_mod.s1_parser, "enrich", lambda out: out.update({"visible_text": visible_text})
    )
    # Local OCR is stubbed: _build_capture only grabs the screenshot and stashes the
    # JPEG in out["_ocr_pending_jpeg"]; the actual recognize + backfill is deferred to
    # _write_capture (after the row is indexed). A no-op recognize keeps it hermetic.
    monkeypatch.setattr(sched_mod.ocr_local, "recognize", lambda *a, **k: None)


class TestOcrTrigger:
    def test_triggers_when_visible_text_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _common_patches(monkeypatch, "WeChat", "\u5fae\u4fe1", "")
        img = Image.new("RGB", (10, 10))
        monkeypatch.setattr(sched_mod.window_screenshot, "grab_focused_window", lambda: img)
        monkeypatch.setattr(
            sched_mod.window_screenshot, "pil_to_jpeg_bytes", lambda img, quality=85: b"jpeg"
        )

        out = sched_mod._build_capture(_make_cfg(), FakeProvider(), None)
        assert out is not None
        assert out.get("_ocr_pending_jpeg") == b"jpeg"
        assert out.get("_ocr_tier") == "tiny"

    def test_triggers_when_only_header_no_indent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _common_patches(
            monkeypatch,
            "WeChat",
            "\u5fae\u4fe1",
            "## WeChat [active]\n_com.test_\n### \u5fae\u4fe1",
        )
        img = Image.new("RGB", (10, 10))
        monkeypatch.setattr(sched_mod.window_screenshot, "grab_focused_window", lambda: img)
        monkeypatch.setattr(
            sched_mod.window_screenshot, "pil_to_jpeg_bytes", lambda img, quality=85: b"jpeg"
        )

        out = sched_mod._build_capture(_make_cfg(), FakeProvider(), None)
        assert out is not None
        assert out.get("_ocr_pending_jpeg") == b"jpeg"
        # The header-only AX text is cleared so the deferred OCR backfill (empty-guarded)
        # can take over — otherwise the non-empty header would block the backfill.
        assert out.get("visible_text") == ""

    def test_no_trigger_when_ax_content_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _common_patches(
            monkeypatch, "TRAE", "Code", "## TRAE [active]\n_com.test_\n### Code\n  - [Button] foo"
        )
        monkeypatch.setattr(sched_mod.window_screenshot, "grab_focused_window", lambda: None)

        out = sched_mod._build_capture(_make_cfg(), FakeProvider(), None)
        assert out is not None
        assert "_ocr_pending_jpeg" not in out

    def test_no_trigger_when_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _common_patches(monkeypatch, "WeChat", "\u5fae\u4fe1", "")
        monkeypatch.setattr(sched_mod.window_screenshot, "grab_focused_window", lambda: None)

        out = sched_mod._build_capture(_make_cfg(enable_ocr_fallback=False), FakeProvider(), None)
        assert out is not None
        assert "_ocr_pending_jpeg" not in out

    def test_no_trigger_when_screenshot_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _common_patches(monkeypatch, "WeChat", "\u5fae\u4fe1", "")
        monkeypatch.setattr(sched_mod.window_screenshot, "grab_focused_window", lambda: None)

        out = sched_mod._build_capture(_make_cfg(), FakeProvider(), None)
        assert out is not None
        assert "_ocr_pending_jpeg" not in out

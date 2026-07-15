"""Tests for on-device OCR (capture/ocr_local.py).

Unit tests (default gate) stub the Paddle engine and exercise path resolution,
fail-open behavior, and result parsing — no model load, no network. The real
inference test is ``integration``-marked (deselected by the default Linux gate).
"""

from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw

from persome.capture import ocr_local


@pytest.fixture(autouse=True)
def _force_in_process_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin these tests to the in-process predict path.

    They stub the paddle engine to exercise inference/parse logic; default routing would
    instead spawn the isolated worker subprocess. The isolation/routing behavior itself is
    covered by ``test_ocr_subprocess.py``.
    """
    monkeypatch.setenv("PERSOME_OCR_IN_PROCESS", "1")


@pytest.fixture(autouse=True)
def _fake_paddle_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the selected runtime to the stubbed Paddle backend.

    Every test here stubs the engine, so the logic under test sits BEHIND the
    runtime gate; on hosts without paddle wheels the gate would otherwise
    short-circuit, while Intel may have already cached the Vision backend.
    """
    monkeypatch.setattr(ocr_local, "_runtime_available", True)
    monkeypatch.setattr(ocr_local, "_runtime_backend", "paddle")


def _jpeg_bytes(size=(40, 20), color=(255, 255, 255)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


class _FakeEngine:
    def __init__(self, result):
        self._result = result

    def predict(self, _arr):
        return self._result


# ─── path resolution ─────────────────────────────────────────────────────────


class TestModelResolution:
    def test_models_root_finds_vendored_weights(self) -> None:
        root = ocr_local._models_root()
        assert root is not None
        assert (root / "PP-OCRv6_tiny_det" / "inference.json").exists()
        assert (root / "PP-OCRv6_tiny_rec" / "inference.json").exists()

    def test_model_dir_for_tier(self) -> None:
        det = ocr_local._model_dir("tiny", "det")
        rec = ocr_local._model_dir("tiny", "rec")
        assert det is not None and det.endswith("PP-OCRv6_tiny_det")
        assert rec is not None and rec.endswith("PP-OCRv6_tiny_rec")
        assert ocr_local.models_available("tiny") is True
        assert ocr_local.models_available("invalid") is False

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        monkeypatch.setenv("PERSOME_OCR_MODELS_DIR", str(tmp_path))
        assert ocr_local._models_root() == tmp_path

    def test_models_root_finds_installed_package_bundle(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        module = tmp_path / "persome" / "capture" / "ocr_local.py"
        packaged = tmp_path / "persome" / "_bundled" / "ocr_models"
        packaged.mkdir(parents=True)
        module.parent.mkdir(parents=True, exist_ok=True)
        module.touch()
        monkeypatch.setattr(ocr_local, "__file__", str(module))

        assert ocr_local._models_root() == packaged


# ─── recognize: fail-open + parsing ──────────────────────────────────────────


class TestRecognize:
    def test_empty_bytes_returns_none(self) -> None:
        assert ocr_local.recognize(b"") is None

    def test_undecodable_bytes_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Even with an engine present, garbage image bytes decode to None → None.
        monkeypatch.setattr(ocr_local, "_engines", {"tiny": _FakeEngine([{"rec_texts": ["x"]}])})
        assert ocr_local.recognize(b"not-an-image") is None

    def test_fails_open_when_engine_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ocr_local, "_engines", {})
        monkeypatch.setattr(ocr_local, "_build_engine", lambda tier: None)
        assert ocr_local.recognize(_jpeg_bytes()) is None

    def test_parses_rec_texts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            ocr_local,
            "_engines",
            {"tiny": _FakeEngine([{"rec_texts": ["\u4f60\u597d", "\u4e16\u754c"]}])},
        )
        assert ocr_local.recognize(_jpeg_bytes()) == "\u4f60\u597d\n\u4e16\u754c"

    def test_empty_result_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ocr_local, "_engines", {"tiny": _FakeEngine([{"rec_texts": []}])})
        assert ocr_local.recognize(_jpeg_bytes()) is None

    def test_predict_raise_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Boom:
            def predict(self, _arr):
                raise RuntimeError("boom")

        monkeypatch.setattr(ocr_local, "_engines", {"tiny": _Boom()})
        assert ocr_local.recognize(_jpeg_bytes()) is None


# ─── runtime kill-switch (PERSOME_DISABLE_OCR) ──────────────────────────
#
# Stop-gap for the paddle runtime SIGSEGV (#335/#218): when disabled, NO inference
# runs (the daemon can't crash in paddle native code) and every entrypoint fails open.


class TestDisableKillSwitch:
    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on", " on "])
    def test_truthy_values_disable(self, monkeypatch: pytest.MonkeyPatch, val) -> None:
        monkeypatch.setenv("PERSOME_DISABLE_OCR", val)
        assert ocr_local._ocr_disabled() is True

    @pytest.mark.parametrize("val", ["", "0", "false", "no", "off", "anything"])
    def test_falsy_values_enable(self, monkeypatch: pytest.MonkeyPatch, val) -> None:
        monkeypatch.setenv("PERSOME_DISABLE_OCR", val)
        assert ocr_local._ocr_disabled() is False

    def test_unset_is_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PERSOME_DISABLE_OCR", raising=False)
        assert ocr_local._ocr_disabled() is False

    def test_disabled_skips_engine_build(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Even with weights present, a disabled kill-switch must never construct/import
        # the paddle engine — that's the whole point (no inference, no native crash).
        monkeypatch.setenv("PERSOME_DISABLE_OCR", "1")
        monkeypatch.setattr(ocr_local, "_engines", {})

        def _boom(_tier):  # noqa: ANN001
            raise AssertionError("_build_engine must not be called when OCR is disabled")

        monkeypatch.setattr(ocr_local, "_build_engine", _boom)
        with ocr_local._lock:
            assert ocr_local._get_engine("tiny") is None

    def test_disabled_recognize_fails_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A live engine cached, but the kill-switch wins → no predict, returns None.
        monkeypatch.setenv("PERSOME_DISABLE_OCR", "1")

        class _Boom:
            def predict(self, _arr):
                raise AssertionError("predict must not run when OCR is disabled")

        monkeypatch.setattr(ocr_local, "_engines", {"tiny": _Boom()})
        assert ocr_local.recognize(_jpeg_bytes()) is None
        assert ocr_local.recognize_detailed(_jpeg_bytes()) is None

    def test_disabled_warm_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSOME_DISABLE_OCR", "1")

        def _boom(_tier):  # noqa: ANN001
            raise AssertionError("warm must not build the engine when OCR is disabled")

        monkeypatch.setattr(ocr_local, "_build_engine", _boom)
        assert ocr_local.warm("tiny") is False

    def test_enabled_recognize_still_runs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Guard against the kill-switch accidentally disabling OCR when unset/off.
        monkeypatch.setenv("PERSOME_DISABLE_OCR", "0")
        monkeypatch.setattr(
            ocr_local, "_engines", {"tiny": _FakeEngine([{"rec_texts": ["\u4f60\u597d"]}])}
        )
        assert ocr_local.recognize(_jpeg_bytes()) == "\u4f60\u597d"


# ─── result extraction ───────────────────────────────────────────────────────


# ─── real inference (integration; deselected by the default gate) ─────────────

_CJK_FONTS = [
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]


@pytest.mark.integration
def test_real_recognition_chinese() -> None:
    import os

    from PIL import ImageFont

    if ocr_local._model_dir("tiny", "det") is None:
        pytest.skip("vendored PP-OCRv6 tiny weights not present")
    font_path = next((f for f in _CJK_FONTS if os.path.exists(f)), None)
    if font_path is None:
        pytest.skip("no CJK font available to render the test image")

    img = Image.new("RGB", (520, 90), (255, 255, 255))
    ImageDraw.Draw(img).text(
        (20, 24),
        "\u5fae\u4fe1\u6d4b\u8bd5\u6d88\u606f",
        font=ImageFont.truetype(font_path, 40),
        fill=(0, 0, 0),
    )
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)

    text = ocr_local.recognize(buf.getvalue(), "tiny")
    assert text is not None and text.strip(), "real OCR returned no text"
    # tiny isn't perfect, but it should recover at least one of the rendered glyphs.
    assert any(ch in text for ch in "\u5fae\u4fe1\u6d4b\u8bd5\u6d88\u606f")

from __future__ import annotations

import io
import json
import os
import platform
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from persome.capture import ocr_local, ocr_protocol, ocr_subprocess, vision_ocr


def test_available_requires_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vision_ocr.platform, "system", lambda: "Linux")
    assert vision_ocr.available() is False


def test_available_requires_intel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vision_ocr.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(vision_ocr.platform, "machine", lambda: "arm64")
    assert vision_ocr.available() is False


def test_available_reuses_compiled_helper_without_swiftc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "mac-vision-ocr.swift"
    source.write_text("source", encoding="utf-8")
    helper = tmp_path / "mac-vision-ocr"
    helper.write_text("binary", encoding="utf-8")
    helper.chmod(0o700)
    monkeypatch.setattr(vision_ocr.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(vision_ocr.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(vision_ocr, "_source_candidates", lambda: [source])
    monkeypatch.setattr(vision_ocr, "_native_binary_path", lambda *args: helper)
    monkeypatch.setattr(vision_ocr.shutil, "which", lambda name: None)

    assert vision_ocr.available() is True


def test_recognize_decodes_shared_geometry_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = tmp_path / "mac-vision-ocr"
    helper.write_text("stub", encoding="utf-8")
    helper.chmod(0o700)
    expected = (["Persome Intel"], [[10, 20, 300, 80]], [0.98])
    monkeypatch.setattr(vision_ocr, "resolve_helper_path", lambda: helper)
    monkeypatch.setattr(
        vision_ocr.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout=ocr_protocol.encode_response(expected), stderr=b""
        ),
    )

    assert vision_ocr.recognize_detailed(b"jpeg") == expected


def test_recognize_fails_open_when_helper_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = tmp_path / "mac-vision-ocr"
    helper.write_text("stub", encoding="utf-8")
    helper.chmod(0o700)
    monkeypatch.setattr(vision_ocr, "resolve_helper_path", lambda: helper)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 30)

    monkeypatch.setattr(vision_ocr.subprocess, "run", timeout)
    assert vision_ocr.recognize_detailed(b"jpeg") is None


@pytest.mark.macos
def test_real_intel_helper_reads_large_pipe_input() -> None:
    """The Swift helper must accumulate pipe short reads instead of truncating stdin."""
    if platform.system() != "Darwin" or platform.machine().lower() not in {"x86_64", "amd64"}:
        pytest.skip("Intel macOS smoke")

    helper = vision_ocr.resolve_helper_path()
    assert helper is not None
    payload = b"x" * (2 * 1024 * 1024 + 17)
    result = subprocess.run(
        [str(helper), "--check-input"],
        input=payload,
        capture_output=True,
        check=False,
        timeout=vision_ocr._TIMEOUT_SECONDS,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert json.loads(result.stdout) == {"ok": True, "inputBytes": len(payload)}


@pytest.mark.macos
def test_real_intel_vision_ocr_smoke(ac_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Real compile + inference gate run by the macos-15-intel CI job."""
    if platform.system() != "Darwin" or platform.machine().lower() not in {"x86_64", "amd64"}:
        pytest.skip("Intel macOS smoke")

    monkeypatch.delenv("PERSOME_VISION_OCR", raising=False)
    ocr_local._runtime_available = None
    ocr_local._runtime_backend = None
    assert vision_ocr.available()
    assert ocr_local.runtime_available()
    assert ocr_local.runtime_backend() == "vision"
    assert ocr_local.warm()

    image = Image.new("RGB", (1200, 260), "white")
    draw = ImageDraw.Draw(image)
    font_path = "/System/Library/Fonts/Supplemental/Arial.ttf"
    assert os.path.isfile(font_path)
    font = ImageFont.truetype(font_path, 72)
    draw.text((40, 70), "Persome Intel OCR 2026", fill="black", font=font)
    buf = io.BytesIO()
    image.save(buf, format="PNG")

    try:
        detailed = ocr_local.recognize_detailed(buf.getvalue())
        assert detailed is not None
        texts, boxes, scores = detailed
        assert "PERSOME" in " ".join(texts).upper()
        assert len(texts) == len(boxes) == len(scores)
        assert all(len(box) == 4 for box in boxes)
        assert all(0 <= x0 < x1 <= 1200 and 0 <= y0 < y1 <= 260 for x0, y0, x1, y1 in boxes)
        assert all(0.0 <= score <= 1.0 for score in scores)
    finally:
        ocr_subprocess.get_client().shutdown()

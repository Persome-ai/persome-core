"""HTTP model limits for trusted capture ingestion."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from persome.api.models import (
    MAX_CAPTURE_AX_TREE_BYTES,
    MAX_CAPTURE_IMAGE_B64_CHARS,
    CaptureIngestBody,
)


def test_capture_rejects_oversized_screenshot_before_ingest() -> None:
    with pytest.raises(ValidationError, match="screenshot.image_base64"):
        CaptureIngestBody(screenshot={"image_base64": "A" * (MAX_CAPTURE_IMAGE_B64_CHARS + 1)})


def test_capture_rejects_oversized_ocr_payload_before_decode() -> None:
    with pytest.raises(ValidationError, match="String should have at most"):
        CaptureIngestBody(ocr_jpeg_b64="A" * (MAX_CAPTURE_IMAGE_B64_CHARS + 1))


def test_capture_rejects_oversized_ax_tree() -> None:
    with pytest.raises(ValidationError, match="ax_tree exceeds"):
        CaptureIngestBody(ax_tree={"text": "x" * (MAX_CAPTURE_AX_TREE_BYTES + 1)})

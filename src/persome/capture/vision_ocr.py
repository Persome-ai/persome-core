"""Apple Vision OCR adapter for Intel macOS.

PaddlePaddle does not publish a macOS x86_64 wheel.  Intel installs therefore
use a tiny Swift helper backed by Apple's on-device Vision framework.  The
helper accepts image bytes on stdin and returns the same JSON geometry contract
as the isolated Paddle worker, so the scheduler and app-aware structurer do not
need an architecture-specific path.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path

from ..logger import get
from . import ocr_protocol
from .ax_capture import _native_binary_path, _stable_native_binary

logger = get("persome.capture.ocr.vision")

_HELPER_NAME = "mac-vision-ocr"
_SOURCE_NAME = f"{_HELPER_NAME}.swift"
# The parent worker's normal deadline is 20 seconds. Keep the child helper's
# deadline shorter so it is reaped here rather than surviving a worker timeout.
_TIMEOUT_SECONDS = 15


def _supported_host() -> bool:
    return platform.system() == "Darwin" and platform.machine().lower() in {
        "x86_64",
        "amd64",
    }


def _source_candidates() -> list[Path]:
    candidates: list[Path] = []
    try:
        from importlib.resources import files as _pkg_files

        bundled = Path(str(_pkg_files("persome").joinpath("_bundled")))
        candidates.append(bundled / _SOURCE_NAME)
    except (ModuleNotFoundError, ValueError):
        pass
    candidates.append(Path(__file__).resolve().parents[3] / "resources" / _SOURCE_NAME)
    return candidates


def available() -> bool:
    """Whether this host can resolve or compile the local Vision helper.

    This is a side-effect-free prerequisite probe used by health checks.  The
    installer already requires Xcode Command Line Tools for the AX helpers, so
    an Intel install with the packaged source should satisfy it.
    """
    if not _supported_host():
        return False
    override = os.environ.get("PERSOME_VISION_OCR")
    if override:
        path = Path(override).expanduser()
        return path.is_file() and os.access(path, os.X_OK)
    sources = [path for path in _source_candidates() if path.is_file()]
    for source in sources:
        compiled = _native_binary_path(source, _HELPER_NAME)
        if compiled is not None and compiled.is_file() and os.access(compiled, os.X_OK):
            return True
    return shutil.which("swiftc") is not None and bool(sources)


def resolve_helper_path() -> Path | None:
    """Resolve or compile the architecture-native Vision OCR executable."""
    if not _supported_host():
        return None
    override = os.environ.get("PERSOME_VISION_OCR")
    if override:
        path = Path(override).expanduser().resolve()
        if path.is_file() and os.access(path, os.X_OK):
            return path
        logger.warning("PERSOME_VISION_OCR set but not executable: %s", path)
        return None
    for source in _source_candidates():
        if source.is_file():
            helper = _stable_native_binary(source, _HELPER_NAME)
            if helper is not None:
                return helper
    return None


def warm() -> bool:
    """Compile the helper and prove that the Vision framework can load."""
    helper = resolve_helper_path()
    if helper is None:
        return False
    try:
        result = subprocess.run(
            [str(helper), "--check"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Vision OCR helper check failed: %s", exc)
        return False
    if result.returncode != 0:
        logger.warning(
            "Vision OCR helper check exited %d: %s",
            result.returncode,
            result.stderr.decode("utf-8", "replace")[:300],
        )
        return False
    return True


def recognize_detailed(image_bytes: bytes) -> ocr_protocol.Detailed | None:
    """Recognize one image through the isolated, one-shot Vision helper."""
    if not image_bytes:
        return None
    helper = resolve_helper_path()
    if helper is None:
        return None
    try:
        result = subprocess.run(
            [str(helper)],
            input=image_bytes,
            capture_output=True,
            check=False,
            timeout=_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Vision OCR helper failed: %s", exc)
        return None
    if result.returncode != 0:
        logger.warning(
            "Vision OCR helper exited %d: %s",
            result.returncode,
            result.stderr.decode("utf-8", "replace")[:300],
        )
        return None
    return ocr_protocol.decode_response(result.stdout)

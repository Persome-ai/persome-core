"""macOS Screen-Recording (kTCCServiceScreenCapture) permission helpers.

Without this permission, `mss` / `CGDisplayCreateImage` silently return ONLY the
desktop wallpaper — every other app's window is blanked by the OS — so a daemon that
captures screenshots stores useless wallpaper frames. A launchd background process
calling the capture API never gets prompted, so the permission must be *requested*
(which also registers the binary, here ``Persome Backend``, in the Screen Recording list
so the user can toggle it on).

We call CoreGraphics directly via ctypes — no pyobjc/Quartz dependency to bundle:
- ``CGPreflightScreenCaptureAccess()`` → has the permission already been granted?
- ``CGRequestScreenCaptureAccess()`` → register + prompt (idempotent).

Both fail **open** (return ``True`` / proceed) if the symbols can't be resolved, so a
non-Darwin host or an SDK without these symbols never blocks capture.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys

from ..logger import get

logger = get("persome.capture.screen_recording")

_cg: ctypes.CDLL | None = None
_resolved = False


def _coregraphics() -> ctypes.CDLL | None:
    global _cg, _resolved
    if _resolved:
        return _cg
    _resolved = True
    if sys.platform != "darwin":
        return None
    try:
        path = (
            ctypes.util.find_library("CoreGraphics")
            or "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
        )
        lib = ctypes.CDLL(path)
        lib.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        lib.CGRequestScreenCaptureAccess.restype = ctypes.c_bool
        _cg = lib
    except Exception as exc:  # noqa: BLE001 — any load failure → fail open
        logger.debug("CoreGraphics unavailable for screen-recording check: %s", exc)
        _cg = None
    return _cg


def has_screen_recording() -> bool:
    """Whether Screen Recording is granted. Fail-open (True) if unknowable."""
    lib = _coregraphics()
    if lib is None:
        return True
    try:
        return bool(lib.CGPreflightScreenCaptureAccess())
    except Exception as exc:  # noqa: BLE001
        logger.debug("CGPreflightScreenCaptureAccess failed: %s", exc)
        return True


def request_screen_recording() -> bool:
    """Register this process (``Persome Backend``) in the Screen Recording list + prompt.

    Returns whether access is granted *now* (usually False on first call — the user
    still has to flip the toggle, but the binary now appears in System Settings).
    Idempotent; safe to call at every boot.
    """
    lib = _coregraphics()
    if lib is None:
        return True
    try:
        granted = bool(lib.CGRequestScreenCaptureAccess())
        if granted:
            logger.info("Screen Recording permission granted")
        else:
            logger.warning(
                "Screen Recording NOT granted — screenshots will be wallpaper-only until "
                "the user enables 'Persome Backend' under System Settings → Privacy & Security "
                "→ Screen Recording, then restarts Persome"
            )
        return granted
    except Exception as exc:  # noqa: BLE001
        logger.debug("CGRequestScreenCaptureAccess failed: %s", exc)
        return False

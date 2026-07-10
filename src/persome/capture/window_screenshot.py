"""Focused window screenshot via AppleScript bounds + mss."""

from __future__ import annotations

import io
import subprocess
from dataclasses import dataclass

from PIL import Image

try:
    import mss
except ImportError:
    mss = None  # type: ignore[assignment]


@dataclass
class WindowBounds:
    x: int
    y: int
    w: int
    h: int


_BOUNDS_SCRIPT = """
tell application "System Events"
    set frontProc to first application process whose frontmost is true
    try
        set win to front window of frontProc
        set pos to position of win
        set sz to size of win
        return (item 1 of pos as string) & "," & (item 2 of pos as string) & "," & (item 1 of sz as string) & "," & (item 2 of sz as string)
    on error
        return ""
    end try
end tell
"""


def get_focused_window_bounds() -> WindowBounds | None:
    try:
        proc = subprocess.run(
            ["osascript", "-e", _BOUNDS_SCRIPT],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    if proc.returncode != 0 or not proc.stdout.strip():
        return None

    parts = proc.stdout.strip().split(",")
    if len(parts) != 4:
        return None

    try:
        return WindowBounds(
            x=int(float(parts[0])),
            y=int(float(parts[1])),
            w=int(float(parts[2])),
            h=int(float(parts[3])),
        )
    except (ValueError, TypeError):
        return None


def grab_focused_window() -> Image.Image | None:
    if mss is None:
        return None
    bounds = get_focused_window_bounds()
    if bounds is None:
        return None
    try:
        with mss.mss() as sct:
            raw = sct.grab(
                {
                    "left": bounds.x,
                    "top": bounds.y,
                    "width": bounds.w,
                    "height": bounds.h,
                }
            )
            return Image.frombytes("RGB", raw.size, raw.rgb)
    except Exception:
        return None


def pil_to_jpeg_bytes(pil_img: Image.Image, quality: int = 85) -> bytes:
    buf = io.BytesIO()
    pil_img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()

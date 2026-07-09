"""Locate UI targets for the actuation layer — the "eyes" that turn a user-meaningful query into
something the act verbs can hit.

Two complementary finders, both proven by `tests/manual/bench_cases.py` (30-case suite):
  • `ax_find(app, query)` — search the AX tree for elements whose label/value contains `query`, tagged
    with a container letter (same letter = same AX subtree, for disambiguating duplicate labels) +
    visibility + bbox. The structural way to pick "the right 温子墨 row".
  • `ocr_locate(app, query)` — for PIXEL-drawn text AX can't see (WeChat/Feishu chat lists, calculator
    displays): screenshot the front window, run on-device OCR (PP-OCRv6), return ALL matches as SCREEN
    coordinates (top→bottom) for `clickxy`. Retina scale is read from the PNG, not assumed 2×.

Darwin-only; both fail-open (return `{ok: False, error}` off macOS / when the binary or OCR is absent).
"""

from __future__ import annotations

import base64
import contextlib
import os
import platform
import struct
import subprocess
import tempfile
from typing import Any

from ..logger import get
from . import actuator

logger = get("persome.actuation.locate")

_SCREENCAPTURE_TIMEOUT = 8


# ── window geometry ───────────────────────────────────────────────────────────


def front_window(app: str, snapshot: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The frontmost/main window of `app` = the LARGEST-area visible AXWindow. The actuator snapshots
    every window, so "first AXWindow" can be a stale background one (multi-window Finder/VSCode)."""
    snap = snapshot if snapshot is not None else actuator.snapshot(app=app)
    wins = [
        e
        for e in snap.get("elements", [])
        if e.get("role") == "AXWindow" and e.get("bbox") and e["bbox"][2] > 0 and e["bbox"][3] > 0
    ]
    return max(wins, key=lambda e: e["bbox"][2] * e["bbox"][3]) if wins else None


def win_rect(app: str, snapshot: dict[str, Any] | None = None) -> list[float] | None:
    w = front_window(app, snapshot)
    return w["bbox"] if w else None


# ── AX find ───────────────────────────────────────────────────────────────────


def _path_of(element_id: str) -> str:
    """The child-index path encoded in a re-resolvable element id (`base64("i0.i1...#hash")`)."""
    try:
        return base64.b64decode(element_id).decode().split("#", 1)[0]
    except Exception:  # noqa: BLE001
        return ""


def _group_label(n: int) -> str:
    """A/B/…/Z then AA/AB/… — never overflows past Z into non-letter chars."""
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def ax_find(app: str, query: str, *, limit: int = 40) -> dict[str, Any]:
    """Find AX elements in `app` whose label/value contains `query`. Returns `{ok, query, matches}`
    where each match is `{id, role, text, container, visible, bbox}`. Same `container` letter = same
    AX subtree (so the model can pick e.g. the sidebar row over a message preview)."""
    snap = actuator.snapshot(app=app)
    if not snap.get("ok"):
        return {"ok": False, "error": snap.get("error", "snapshot_failed"), "query": query}
    groups: dict[str, str] = {}
    matches: list[dict[str, Any]] = []
    for e in snap.get("elements", []):
        text = (e.get("label") or e.get("value") or "").strip()
        if query not in text:
            continue
        b = e.get("bbox") or [0, 0, 0, 0]
        container = groups.setdefault(
            ".".join(_path_of(e["id"]).split(".")[:12]), _group_label(len(groups))
        )
        matches.append(
            {
                "id": e["id"],
                "role": e.get("role", ""),
                "text": text[:80],
                "container": container,
                "visible": bool(b[2] > 0 and b[3] > 0),
                "bbox": [int(v) for v in b],
            }
        )
        if len(matches) >= limit:
            break
    return {"ok": True, "query": query, "count": len(matches), "matches": matches}


# ── OCR locate ────────────────────────────────────────────────────────────────


def _png_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) in pixels from PNG bytes' IHDR — no image lib needed."""
    try:
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        w, h = struct.unpack(">II", data[16:24])
        return int(w), int(h)
    except Exception:  # noqa: BLE001
        return None


def ocr_locate(app: str, query: str, *, limit: int = 10) -> dict[str, Any]:
    """OCR the front window of `app` and return ALL matches of `query` as SCREEN coords (top→bottom),
    for `clickxy`. Returns `{ok, query, matches:[{x, y, text}]}`. For pixel-drawn UI AX can't read."""
    if platform.system() != "Darwin":
        return {"ok": False, "error": "actuator_unavailable", "query": query}
    rect = win_rect(app)
    if not rect:
        return {"ok": False, "error": "no_window", "query": query}
    x, y, w, h = (int(v) for v in rect)
    from ..capture import ocr_local  # lazy: heavy paddle import

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
        png_path = tf.name
    try:
        subprocess.run(
            ["screencapture", "-x", f"-R{x},{y},{w},{h}", png_path],
            capture_output=True,
            timeout=_SCREENCAPTURE_TIMEOUT,
        )
        with open(png_path, "rb") as f:
            data = f.read()
        res = ocr_local.recognize_detailed(data)
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("ocr_locate failed: %s", exc)
        return {"ok": False, "error": "ocr_failed", "query": query}
    finally:
        with contextlib.suppress(OSError):
            os.unlink(png_path)
    if not res:
        return {"ok": False, "error": "ocr_unavailable", "query": query}
    # boxes are IMAGE pixels; convert to screen points with the REAL retina scale (px/point), derived
    # from the captured PNG width vs the window width in points — NOT a hardcoded 2× (works on 1× too).
    size = _png_size(data)
    scale = (size[0] / w) if (size and w) else 2.0
    texts, boxes, _scores = res
    hits: list[tuple[float, float, str]] = []
    for t, b in zip(texts, boxes, strict=False):
        if query in t:
            cx = rect[0] + (b[0] + b[2]) / 2 / scale
            cy = rect[1] + (b[1] + b[3]) / 2 / scale
            hits.append((cy, cx, t))
    hits.sort()
    matches = [{"x": round(cx, 1), "y": round(cy, 1), "text": t[:40]} for cy, cx, t in hits[:limit]]
    return {"ok": bool(matches), "query": query, "count": len(matches), "matches": matches}

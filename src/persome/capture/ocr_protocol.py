"""Length-prefixed wire protocol between the daemon and the isolated OCR worker.

The daemon (parent) sends one **request** frame per OCR call and reads back one
**response** frame. Frames are length-prefixed so binary image bytes and JSON results
travel over the worker's stdin/stdout without delimiter ambiguity.

Everything here is pure (no I/O side effects beyond the passed stream) and fail-open at
the edges: a truncated / EOF read returns ``None`` — the parent reads that as "worker
died" and respawns.

Wire format
-----------
- **Frame**    ``struct(">I", len(body)) + body``.
- **Request**  ``struct(">H", len(tier_utf8)) + tier_utf8 + image_bytes`` (empty image = warm).
- **Response** JSON utf8: ``{"ok": true, "texts": [...], "boxes": [[x0,y0,x1,y1], ...],
  "scores": [...]}`` or ``{"ok": false}``.
"""

from __future__ import annotations

import json
import struct
from typing import IO

_LEN = struct.Struct(">I")  # frame length prefix
_TIER = struct.Struct(">H")  # request tier-length prefix

# A hard ceiling so a corrupt length prefix can't make the reader allocate unbounded
# memory. A focused-window screenshot JPEG is well under this.
MAX_FRAME = 64 * 1024 * 1024

# The recognize result the worker returns: (texts, boxes, scores).
Detailed = tuple[list[str], list[list[int]], list[float]]


def write_frame(stream: IO[bytes], body: bytes) -> None:
    """Write one length-prefixed frame and flush. Raises on a broken pipe (caller guards)."""
    stream.write(_LEN.pack(len(body)))
    stream.write(body)
    stream.flush()


def read_frame(stream: IO[bytes]) -> bytes | None:
    """Read one length-prefixed frame. Returns ``None`` on EOF / short read / bad length.

    A ``None`` return is the parent's "worker is gone" signal (closed stdout after a crash).
    """
    hdr = _read_exact(stream, _LEN.size)
    if hdr is None:
        return None
    (n,) = _LEN.unpack(hdr)
    if n < 0 or n > MAX_FRAME:
        return None
    if n == 0:
        return b""
    return _read_exact(stream, n)


def _read_exact(stream: IO[bytes], n: int) -> bytes | None:
    """Read exactly ``n`` bytes, or ``None`` if the stream ends first."""
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


# ─── request ─────────────────────────────────────────────────────────────────


def encode_request(tier: str, image_bytes: bytes) -> bytes:
    tier_b = tier.encode("utf-8")
    return _TIER.pack(len(tier_b)) + tier_b + image_bytes


def decode_request(body: bytes) -> tuple[str, bytes]:
    """Return ``(tier, image_bytes)``. An empty ``image_bytes`` is a warm request."""
    tlen = _TIER.unpack_from(body, 0)[0]
    off = _TIER.size
    tier = body[off : off + tlen].decode("utf-8", "replace")
    image = body[off + tlen :]
    return tier, image


# ─── response ────────────────────────────────────────────────────────────────


def encode_response(result: Detailed | None) -> bytes:
    if result is None:
        return json.dumps({"ok": False}).encode("utf-8")
    texts, boxes, scores = result
    return json.dumps({"ok": True, "texts": texts, "boxes": boxes, "scores": scores}).encode(
        "utf-8"
    )


def decode_response(body: bytes | None) -> Detailed | None:
    """Parse a response frame into ``(texts, boxes, scores)`` or ``None`` (fail-open)."""
    if not body:
        return None
    try:
        obj = json.loads(body.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(obj, dict) or not obj.get("ok"):
        return None
    texts = [str(t) for t in obj.get("texts") or []]
    boxes_raw = obj.get("boxes") or []
    scores_raw = obj.get("scores") or []
    boxes: list[list[int]] = []
    for b in boxes_raw:
        try:
            boxes.append([int(b[0]), int(b[1]), int(b[2]), int(b[3])])
        except Exception:  # noqa: BLE001
            boxes.append([0, 0, 0, 0])
    scores: list[float] = []
    for s in scores_raw:
        try:
            scores.append(float(s))
        except Exception:  # noqa: BLE001
            scores.append(0.0)
    return (texts, boxes, scores)

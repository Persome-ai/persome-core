"""ControlGraph + the re-resolvable AX-path element id codec.

Element ids encode a child-index path from the app root plus an FNV-1a hash of the element's label,
so the actuator can re-resolve the path at act time and **validate** the label (UI changed ⇒ the id
goes `stale`, never a wrong-element misfire). This module mirrors the Swift encoder in
`resources/mac-ax-actuator.swift` so the codec is unit-testable in Python and the daemon can read
ids back. Pure — no AX, no subprocess.

Plan: docs/superpowers/plans/2026-06-25-persome-actuation-layer-plan.md §3-4.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

_FNV_OFFSET = 1469598103934665603
_FNV_PRIME = 1099511628211
_MASK64 = 0xFFFFFFFFFFFFFFFF


def label_hash(label: str) -> str:
    """FNV-1a over the label's UTF-8, low 32 bits as 8 hex chars — byte-identical to the Swift side."""
    h = _FNV_OFFSET
    for b in label.encode("utf-8"):
        h = ((h ^ b) * _FNV_PRIME) & _MASK64
    return format(h & 0xFFFFFFFF, "08x")


def encode_id(path: list[int], label: str) -> str:
    """`base64("i0.i1...#<labelhash>")` — the stable, re-resolvable element id."""
    raw = ".".join(str(i) for i in path) + "#" + label_hash(label)
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def decode_id(element_id: str) -> tuple[list[int], str] | None:
    """Inverse of `encode_id`. Returns (path, hash) or None on malformed input."""
    try:
        raw = base64.b64decode(element_id.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if "#" not in raw:
        return None
    path_str, _, hash_str = raw.partition("#")
    path = [int(p) for p in path_str.split(".") if p != ""] if path_str else []
    return path, hash_str


@dataclass
class Element:
    """One addressable UI element in the control graph."""

    id: str
    role: str
    label: str
    source: str = "ax"  # ax | ocr | vision
    value: str | None = None
    bbox: list[float] | None = None
    actions: list[str] = field(default_factory=list)
    enabled: bool = True
    editable: bool = False

    @classmethod
    def from_ax(cls, d: dict[str, Any]) -> Element:
        return cls(
            id=d["id"],
            role=d.get("role", ""),
            label=d.get("label", ""),
            source="ax",
            value=d.get("value"),
            bbox=d.get("bbox"),
            actions=list(d.get("actions", [])),
            enabled=bool(d.get("enabled", True)),
            editable=bool(d.get("editable", False)),
        )


def _iou(a: list[float], b: list[float]) -> float:
    """Intersection-over-union of two [x, y, w, h] boxes."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix, iy = max(ax, bx), max(ay, by)
    ix2, iy2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    iw, ih = max(0.0, ix2 - ix), max(0.0, iy2 - iy)
    inter = iw * ih
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


@dataclass
class ControlGraph:
    """The merged, addressable view of an app: AX elements plus (later) OCR/vision targets."""

    app: str
    pid: int
    elements: list[Element] = field(default_factory=list)

    @classmethod
    def from_snapshot(cls, snap: dict[str, Any]) -> ControlGraph:
        els = [Element.from_ax(e) for e in snap.get("elements", [])]
        return cls(app=snap.get("app", ""), pid=int(snap.get("pid", 0)), elements=els)

    def merge_ocr(self, ocr_targets: list[Element], iou_threshold: float = 0.5) -> None:
        """Add OCR text targets that no AX element already covers (AX wins on overlap)."""
        ax_boxes = [e.bbox for e in self.elements if e.bbox]
        for t in ocr_targets:
            if t.bbox and any(_iou(t.bbox, b) >= iou_threshold for b in ax_boxes):
                continue
            self.elements.append(t)

    def find(self, label: str, role: str | None = None) -> Element | None:
        for e in self.elements:
            if e.label == label and (role is None or e.role == role):
                return e
        return None

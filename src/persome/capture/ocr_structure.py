"""Geometry-based structuring of raw OCR output (zero LLM, on-device, fail-open).

The local OCR (``ocr_local.recognize_detailed``) returns per-line text + box + score.
Raw ``"\\n".join(texts)`` is noisy and unstructured: single-char fragments (``0`` / ``△``),
and a multi-column UI (e.g. WeChat's contact list + message pane) flattened into one
stream so you cannot tell who said what. This module reconstructs structure purely from
geometry + per-app layout priors:

1. **Confidence filter** — drop ``score < min_score`` lines (fragment noise sits ~0.38).
2. **Column split** — cluster boxes by x to separate UI columns (adaptive gap, not a
   hardcoded threshold).
3. **Row merge** — within a column, sort by y and merge same-row boxes left→right.

For a **known app** (currently WeChat) a layout prior extracts semantic fields
(``contact`` / ``time`` / ``preview``). The sidebar↔conversation divider is **adaptive**
(``_wechat_divider`` — per-image first-gap detection), so it survives the user dragging
the sidebar width or resizing the window (a fixed ``330*scale`` lost the whole chat list
when the sidebar was dragged narrow — ablation: 3/10 samples 0-list; adaptive → 0). For an
**unknown app** it degrades to generic confidence-filtered region splitting — no guessed
semantics. Validated on real WeChat samples incl. a window/sidebar-size ablation set.
"""

from __future__ import annotations

import re

# Surface-form time tokens in a WeChat conversation list (right-aligned per row).
TIME = re.compile(
    r"^(\u6628\u5929|\u4eca\u5929|\u661f\u671f[\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u65e5\u5929]|"
    r"\u5468[\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u65e5]|\d{1,2}:\d{2}|\d{1,2}/\d{1,2}|"
    r"\d+\u6708\d+\u65e5|\d{4}-\d{1,2}-\d{1,2})"
)

# WeChat desktop column boundaries, in the reference 960-px-wide window (scaled to actual).
_WECHAT_NAV = 60  # left icon rail
_WECHAT_LIST = 330  # conversation-list / message-pane divider

# Bundle ids we have a layout prior for.
WECHAT_BUNDLES = ("com.tencent.xinWeChat", "com.tencent.WeWorkMac")

GEOM_VERSION = "3"  # v3: adaptive sidebar↔conversation divider (window/sidebar-width robust)


def _items(
    texts: list[str], boxes: list[list[int]], scores: list[float], min_score: float
) -> list[dict]:
    out = []
    for t, b, s in zip(texts, boxes, scores, strict=False):
        if s >= min_score and t.strip() and len(b) == 4:
            out.append(
                {
                    "t": t.strip(),
                    "x0": b[0],
                    "y0": b[1],
                    "x1": b[2],
                    "y1": b[3],
                    "cx": (b[0] + b[2]) / 2,
                    "s": s,
                }
            )
    return out


def _rowify(items: list[dict], tol: float) -> list[list[dict]]:
    """Group boxes into visual rows by y0 proximity."""
    items = sorted(items, key=lambda it: it["y0"])
    rows: list[list[dict]] = []
    for it in items:
        if rows and abs(it["y0"] - rows[-1][-1]["y0"]) <= tol:
            rows[-1].append(it)
        else:
            rows.append([it])
    return rows


def _wechat_divider(items: list[dict], img_w: int, nav: float) -> float:
    """Find the adaptive boundary between the sidebar and message pane.

    Starting after the navigation rail, use the first significant uncovered
    vertical band rather than the widest one; wider gaps can occur between
    left- and right-aligned message bubbles. Fall back to the reference ratio.
    """
    scale = img_w / 960
    fallback = _WECHAT_LIST * scale
    nav_i, w = int(nav), int(img_w)
    if not items or w <= nav_i:
        return fallback
    covered = bytearray(w + 1)
    right_most = nav_i
    for it in items:
        a, b = max(nav_i, int(it["x0"])), min(w, int(it["x1"]))
        if a > b:
            continue
        for x in range(a, b + 1):
            covered[x] = 1
        right_most = max(right_most, b)
    min_gap = max(12, int(img_w * 0.015))
    x = nav_i
    while x <= w and covered[x] == 0:
        x += 1
    while x <= w:
        if covered[x] == 0:
            start = x
            while x <= w and covered[x] == 0:
                x += 1
            if x - start >= min_gap and start < right_most:
                left = [it for it in items if nav_i <= it["cx"] < start]
                if len(_rowify(left, tol=14 * scale)) >= 4:
                    return (start + x) / 2
        else:
            x += 1
    return fallback


def _structure_wechat(items: list[dict], img_w: int) -> dict:
    scale = img_w / 960
    nav = _WECHAT_NAV * scale
    lst = _wechat_divider(items, img_w, nav)
    side = [it for it in items if nav <= it["cx"] < lst]
    conv = [it for it in items if it["cx"] >= lst]

    # Conversation list: pair a "header" row (name + time) with the next no-time row (preview).
    rows = _rowify(side, tol=14 * scale)
    chats, i = [], 0
    while i < len(rows):
        row = sorted(rows[i], key=lambda it: it["cx"])
        times = [c for c in row if TIME.match(c["t"])]
        names = [c for c in row if not TIME.match(c["t"])]
        if times and names:
            chat = {
                "contact": " ".join(c["t"] for c in names),
                "time": times[0]["t"],
                "preview": "",
            }
            if i + 1 < len(rows):
                nxt = sorted(rows[i + 1], key=lambda it: it["cx"])
                if not any(TIME.match(c["t"]) for c in nxt):
                    chat["preview"] = " ".join(c["t"] for c in nxt)
                    i += 1
            chats.append(chat)
        i += 1

    conversation = _structure_wechat_conversation(conv, img_w, scale, lst)

    return {
        "app": "WeChat",
        "layout": "wechat-desktop",
        "geom_version": GEOM_VERSION,
        "sidebar": {"label": "conversation list", "chats": chats},
        "conversation": conversation,
    }


def _structure_wechat_conversation(conv: list[dict], img_w: int, scale: float, lst: float) -> dict:
    """Structure the message pane: extract the chat title, tag each line by sender.

    Geometry priors (validated on real WeChat desktop captures):
    - The chat **title** sits in the top bar, left-aligned — the top-most row whose
      center is left of the pane midline. It is lifted into ``name`` and removed from
      the message stream (so the peer's name no longer leaks in as a "message").
    - **Sender by bubble side**: a row whose center x is right of the pane midline is
      mine (right bubble), otherwise the peer's (left bubble). The midline is the
      middle of the message pane: ``(lst + img_w) / 2``.
    - **Timestamps** (centered separator rows) match ``TIME`` → ``name="timeline"``.
    """
    rows = _rowify(conv, tol=14 * scale)
    merged = [
        {
            "text": " ".join(c["t"] for c in sorted(r, key=lambda it: it["cx"])),
            "cx": sum(c["cx"] for c in r) / len(r),
            "y0": min(c["y0"] for c in r),
        }
        for r in rows
        if r
    ]
    if not merged:
        return {"label": "message pane", "name": None, "lines": []}

    midline = (lst + img_w) / 2

    # Title: the top-most row, if it's a left-aligned non-time line (the title bar).
    name = None
    top = min(merged, key=lambda m: m["y0"])
    if not TIME.match(top["text"]) and top["cx"] <= midline:
        name = top["text"]
        merged = [m for m in merged if m is not top]

    lines: list[dict] = []
    for m in merged:
        if TIME.match(m["text"]):
            lines.append({"name": "timeline", "text": m["text"]})
        else:
            lines.append(
                {"name": "self" if m["cx"] > midline else "counterpart", "text": m["text"]}
            )

    return {"label": "message pane", "name": name, "lines": lines}


def _structure_generic(items: list[dict], img_w: int) -> dict:
    """Unknown app: confidence-filtered x-clustering into regions, NO guessed semantics."""
    if not items:
        return {"layout": "generic", "geom_version": GEOM_VERSION, "regions": []}
    xs = sorted(items, key=lambda it: it["x0"])
    gap = max(60, int(img_w * 0.12))
    cols: list[list[dict]] = [[xs[0]]]
    for it in xs[1:]:
        if it["x0"] - cols[-1][-1]["x0"] > gap:
            cols.append([it])
        else:
            cols[-1].append(it)
    cols.sort(key=lambda c: min(i["x0"] for i in c))
    regions = []
    for col in cols:
        rows = _rowify(col, tol=12)
        lines = [" ".join(c["t"] for c in sorted(r, key=lambda it: it["cx"])) for r in rows]
        regions.append({"x": int(min(i["x0"] for i in col)), "lines": lines})
    return {"layout": "generic", "geom_version": GEOM_VERSION, "regions": regions}


def structure(
    texts: list[str],
    boxes: list[list[int]],
    scores: list[float],
    *,
    bundle_id: str = "",
    app_name: str = "",
    img_w: int = 960,
    min_score: float = 0.5,
) -> dict:
    """Geometry-structure OCR lines into a dict. Routes by ``bundle_id``; fail-open to {}."""
    try:
        items = _items(texts, boxes, scores, min_score)
        if not items:
            return {}
        if bundle_id in WECHAT_BUNDLES:
            return _structure_wechat(items, img_w)
        return _structure_generic(items, img_w)
    except Exception:  # noqa: BLE001 — structuring must never break capture
        return {}


def to_markdown(struct: dict) -> str:
    """Render a structured dict to compact, field-labeled Markdown for downstream LLM/FTS.

    Field labels are explicit (contact/time/message preview) so the text is self-describing — a
    reader (or the model) knows each segment's role without guessing.
    """
    if not struct:
        return ""
    out: list[str] = []
    if struct.get("layout") == "wechat-desktop":
        out.append("# WeChat")
        chats = struct.get("sidebar", {}).get("chats", [])
        if chats:
            out.append("\n## [Conversation list]")
            out.append("| Contact | Time | Message preview |")
            out.append("|---|---|---|")
            for c in chats:
                out.append(
                    f"| {c.get('contact', '')} | {c.get('time', '')} | {c.get('preview', '')} |"
                )
        conv = struct.get("conversation", {})
        lines = conv.get("lines", [])
        if lines or conv.get("name"):
            header = "## [Message pane]"
            if conv.get("name"):
                header += f" Conversation: {conv['name']}"
            out.append("\n" + header)
            for ln in lines:
                if isinstance(ln, dict):  # v2 typed line {name, text}
                    spk = ln.get("name", "")
                    tag = "time" if spk == "timeline" else spk
                    out.append(f"- [{tag}] {ln.get('text', '')}")
                else:  # v1 plain string — defensive back-compat
                    out.append(f"- {ln}")
    else:  # generic
        for i, reg in enumerate(struct.get("regions", []), 1):
            out.append(f"## [Region {i}] (x≈{reg.get('x', 0)})")
            out.extend(f"- {ln}" for ln in reg.get("lines", []))
    return "\n".join(out).strip()

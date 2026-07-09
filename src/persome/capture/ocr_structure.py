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
    r"^(昨天|今天|星期[一二三四五六日天]|周[一二三四五六日]|"
    r"\d{1,2}:\d{2}|\d{1,2}/\d{1,2}|\d+月\d+日|\d{4}-\d{1,2}-\d{1,2})"
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
    """侧栏↔对话分界 x —— 自适应侧栏宽度(从 nav 右扫第一条显著空白竖带的中心)。

    侧栏宽度是用户可独立拖动、与窗口宽解耦的变量;固定 `_WECHAT_LIST*scale` 在拖窄时
    丢整个会话列表、拖宽时把对话误入列表(消融实测 baseline 3/10 张 0 项崩溃)。改为
    每图自适应:把 box 的 [x0,x1] 投影到 x 轴打覆盖,跳过最左导航栏 + 侧栏内容后,取
    **第一条** ≥`min_gap` 的空白带中心 —— 对话区内部"我↔对方"气泡空白虽更宽却更靠右,
    所以取"第一条"而非"最宽"(后者会被气泡间隙骗,实测准确率反降)。fail-open 到旧固定比例。
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
    min_gap = max(12, int(img_w * 0.015))  # 沟0 实测 20-25px;阈值取窗口 1.5%
    x = nav_i
    while x <= w and covered[x] == 0:  # 跳到侧栏起点(第一列内容)
        x += 1
    while x <= w:  # 侧栏内容之后,第一条 ≥min_gap 且右侧仍有内容的空白带
        if covered[x] == 0:
            start = x
            while x <= w and covered[x] == 0:
                x += 1
            if x - start >= min_gap and start < right_most:
                # 沟左侧必须是「列表」(≥4 行左对齐内容)才算侧栏右界。否则该沟只是行内
                # 空白(名字↔时间)或一个无侧栏的纯对话页 —— 不接受,继续找 / 回退先验。
                # 真实侧栏几十行(自适应生效);稀疏/无侧栏 → 退回固定 330*scale。
                left = [it for it in items if nav_i <= it["cx"] < start]
                if len(_rowify(left, tol=14 * scale)) >= 4:
                    return (start + x) / 2
        else:
            x += 1
    return fallback


def _structure_wechat(items: list[dict], img_w: int) -> dict:
    scale = img_w / 960
    nav = _WECHAT_NAV * scale
    lst = _wechat_divider(items, img_w, nav)  # 自适应分界,替代固定 _WECHAT_LIST*scale
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
        "app": "微信",
        "layout": "wechat-desktop",
        "geom_version": GEOM_VERSION,
        "sidebar": {"label": "会话列表", "chats": chats},
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
        return {"label": "主对话区", "name": None, "lines": []}

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
            lines.append({"name": "我" if m["cx"] > midline else "对方", "text": m["text"]})

    return {"label": "主对话区", "name": name, "lines": lines}


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

    Field labels are explicit (联系人/时间/消息预览) so the text is self-describing — a
    reader (or the model) knows each segment's role without guessing.
    """
    if not struct:
        return ""
    out: list[str] = []
    if struct.get("layout") == "wechat-desktop":
        out.append("# 微信")
        chats = struct.get("sidebar", {}).get("chats", [])
        if chats:
            out.append("\n## [会话列表]")
            out.append("| 联系人 | 时间 | 消息预览 |")
            out.append("|---|---|---|")
            for c in chats:
                out.append(
                    f"| {c.get('contact', '')} | {c.get('time', '')} | {c.get('preview', '')} |"
                )
        conv = struct.get("conversation", {})
        lines = conv.get("lines", [])
        if lines or conv.get("name"):
            header = "## [主对话区]"
            if conv.get("name"):
                header += f" 对话: {conv['name']}"
            out.append("\n" + header)
            for ln in lines:
                if isinstance(ln, dict):  # v2 typed line {name, text}
                    spk = ln.get("name", "")
                    tag = "时间" if spk == "timeline" else spk
                    out.append(f"- [{tag}] {ln.get('text', '')}")
                else:  # v1 plain string — defensive back-compat
                    out.append(f"- {ln}")
    else:  # generic
        for i, reg in enumerate(struct.get("regions", []), 1):
            out.append(f"## [区域{i}] (x≈{reg.get('x', 0)})")
            out.extend(f"- {ln}" for ln in reg.get("lines", []))
    return "\n".join(out).strip()


def quality(struct: dict) -> dict:
    """Self-check metrics for the WeChat chat-field extraction (used by tests/eval)."""
    chats = struct.get("sidebar", {}).get("chats", [])
    if not chats:
        return {"chats": 0, "time_ok": 0.0, "has_preview": 0.0, "contact_clean": 0.0}
    time_ok = sum(1 for c in chats if TIME.match(c.get("time", ""))) / len(chats)
    has_prev = sum(1 for c in chats if c.get("preview", "").strip()) / len(chats)
    clean = sum(
        1
        for c in chats
        if not TIME.match(c.get("contact", ""))
        and c.get("contact", "").strip()
        and not re.fullmatch(r"[\W_]+", c.get("contact", ""))
    ) / len(chats)
    return {
        "chats": len(chats),
        "time_ok": time_ok,
        "has_preview": has_prev,
        "contact_clean": clean,
    }


def conversation_quality(struct: dict) -> dict:
    """Self-check metrics for the v2 conversation-pane structuring (sender tagging).

    The v1 sidebar metrics saturated at 100%, so they can't measure the conversation
    pane. These four expose the v2 work:
    - ``name_extracted``     : a chat title was lifted into ``conversation.name``
    - ``title_not_in_lines`` : that title does NOT also appear as a message line
    - ``lines_typed``        : fraction of lines that are typed dicts ({name,text})
    - ``sender_coverage``    : fraction of NON-timeline lines tagged 我/对方
    """
    conv = struct.get("conversation", {})
    lines = conv.get("lines", [])
    name = conv.get("name")
    typed = [ln for ln in lines if isinstance(ln, dict)]
    lines_typed = (len(typed) / len(lines)) if lines else 0.0
    msg = [ln for ln in typed if ln.get("name") != "timeline"]
    sender_cov = sum(1 for ln in msg if ln.get("name") in ("我", "对方")) / len(msg) if msg else 0.0
    title_not_in_lines = bool(name) and not any(
        (ln.get("text") if isinstance(ln, dict) else ln) == name for ln in lines
    )
    return {
        "lines": len(lines),
        "name_extracted": bool(name),
        "title_not_in_lines": title_not_in_lines,
        "lines_typed": lines_typed,
        "sender_coverage": sender_cov,
    }

"""WeChat (``com.tencent.xinWeChat``) parser — OCR-geometry, NOT AX-tree.

WeChat desktop exposes ~no AX text, so unlike the Feishu parser (which reads the
Electron AX tree) this one consumes the **on-device OCR geometry structuring**
result (``capture/ocr_structure.structure`` — sidebar/conversation columns, per-line
我/对方 sender, title) and turns it into the same :class:`ParsedConversation` the
fast path already understands. That lets WeChat ride the K1 event-driven recognizer
despite having no AX tree.

Why a separate entry point (``conversation_from_structure``) instead of ``parse(ax_tree)``:
the OCR result is not an AX tree, and it isn't even available at capture time — OCR runs
on a background thread and backfills later (see ``scheduler._submit_ocr_async``), so the
fast path is (re)triggered once the structuring is ready and routed here directly. The
``Parser.parse`` override returns ``None`` (WeChat has no parseable AX tree); the class is
still registered so ``get_parser`` recognizes WeChat as a known app.

``direction`` comes for free from the geometry layer (right bubble = 我 = outgoing,
left bubble = 对方 = incoming). Message bodies are normalized (collapse whitespace, drop
trailing ellipsis) to blunt OCR's per-frame jitter against the seen-set identity
(``event_source._msg_identity`` keys on direction+sender+body+time) — a stable id keeps a
re-render of the same message from re-firing the recognizer.
"""

from __future__ import annotations

import re

from .base import Message, ParsedConversation, Parser

# Keep in sync with ``capture.ocr_structure.WECHAT_BUNDLES`` (defined here too so the
# parsers package never imports the capture package — avoids an import cycle).
WECHAT_BUNDLES = ("com.tencent.xinWeChat", "com.tencent.WeWorkMac")

_WS_RE = re.compile(r"\s+")
_TRAIL_ELLIPSIS_RE = re.compile(r"(?:\.{2,}|。{2,}|…+)\s*$")


def _normalize_body(text: str) -> str:
    """Collapse whitespace + drop a trailing ellipsis (OCR-jitter / preview-truncation).

    The conversation-list preview is often truncated with ``…``/``...`` whose exact form
    wobbles between OCR frames; folding it keeps the seen-set identity stable so the same
    preview doesn't re-mint an identity every capture.
    """
    s = _WS_RE.sub(" ", (text or "").strip())
    s = _TRAIL_ELLIPSIS_RE.sub("", s).strip()
    return s


def conversation_from_structure(
    struct: dict | None, *, window_title: str | None = None
) -> ParsedConversation | None:
    """Build a :class:`ParsedConversation` from an ``ocr_structure.structure`` result.

    Returns ``None`` when the struct isn't a WeChat-desktop layout or yields no
    messages/previews (caller then records a non-conversation / empty fast-path tick).
    """
    if not isinstance(struct, dict) or struct.get("layout") != "wechat-desktop":
        return None

    conv = struct.get("conversation") or {}
    title = (conv.get("name") or window_title or "").strip() or None

    messages: list[Message] = []
    for ln in conv.get("lines") or []:
        if not isinstance(ln, dict):
            continue
        name = ln.get("name")
        if name == "timeline":
            continue  # a timestamp separator row, not a message
        body = _normalize_body(ln.get("text", ""))
        if not body:
            continue
        direction = "outgoing" if name == "我" else "incoming"
        sender = None if direction == "outgoing" else title
        messages.append(Message(sender=sender, body=body, timestamp_text=None, direction=direction))

    previews: list[Message] = []
    for c in (struct.get("sidebar") or {}).get("chats") or []:
        if not isinstance(c, dict):
            continue
        body = _normalize_body(c.get("preview", ""))
        if not body:
            continue
        previews.append(
            Message(
                sender=(c.get("contact") or "").strip() or None,
                body=body,
                timestamp_text=(c.get("time") or "").strip() or None,
                direction="incoming",
            )
        )

    if not messages and not previews:
        return None
    return ParsedConversation(
        app="微信",
        thread_title=title,
        messages=messages,
        parser_version=WeChatParser.version,
        previews=previews,
    )


class WeChatParser(Parser):
    """Registered so ``get_parser`` recognizes WeChat as a known app.

    WeChat has no AX tree, so ``parse(ax_tree)`` always returns ``None`` — the real
    work happens in :func:`conversation_from_structure`, called from the fast path's
    OCR-structured bypass once OCR backfill is ready.
    """

    bundle_ids = frozenset(WECHAT_BUNDLES)
    version = "wechat-ocr-1"

    def parse(self, ax_tree: dict, *, window_title: str | None) -> ParsedConversation | None:
        return None  # WeChat is OCR-driven; see conversation_from_structure

"""WeChat OCR-based fast-path parser + the OCR-structured bypass in on_capture.

WeChat has no AX tree, so it rides the fast path via `ocr_structure` geometry instead
of `parse(ax_tree)`. These cover: the OCR-struct → ParsedConversation mapping (direction
/ title / previews / body normalization), seen-set id stability under OCR jitter, and
`on_capture`'s OCR-structured bypass (anchor fires / no-anchor drops / cold-start primes /
the bypass never leaks to a non-WeChat app). Offline — LLM never invoked.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from persome.capture import ocr_structure
from persome.parsers import wechat

FIXTURES = Path(__file__).parent / "fixtures" / "ocr"
WECHAT_BUNDLE = "com.tencent.xinWeChat"


def _fixture_struct(name: str) -> dict:
    d = json.loads((FIXTURES / name).read_text())
    return ocr_structure.structure(
        d["texts"], d["boxes"], d["scores"], bundle_id=WECHAT_BUNDLE, img_w=d["img_w"]
    )


def _struct(
    lines: list[dict], *, name: str | None = "张三", chats: list[dict] | None = None
) -> dict:
    return {
        "app": "微信",
        "layout": "wechat-desktop",
        "geom_version": "3",
        "sidebar": {"label": "会话列表", "chats": chats or []},
        "conversation": {"label": "主对话区", "name": name, "lines": lines},
    }


# ─── conversation_from_structure: mapping ───────────────────────────────────────


class TestMapping:
    def test_real_fixture_direction_and_title(self):
        conv = wechat.conversation_from_structure(_fixture_struct("wechat_chat.json"))
        assert conv.thread_title == "周正雷"
        by = {m.body: m.direction for m in conv.messages}
        assert by.get("我没化过妆") == "outgoing"  # 我 = right bubble
        assert by.get("没事你化又不出门") == "incoming"  # 对方 = left bubble
        # timeline separator rows are NOT messages
        assert all(m.body != "13:27" for m in conv.messages)
        # incoming sender is the chat title; outgoing has no sender
        for m in conv.messages:
            assert (m.sender == "周正雷") if m.direction == "incoming" else (m.sender is None)

    def test_previews_from_sidebar(self):
        conv = wechat.conversation_from_structure(_fixture_struct("wechat_chat.json"))
        assert conv.previews  # sidebar chats → previews
        assert all(p.direction == "incoming" for p in conv.previews)
        assert any(p.sender for p in conv.previews)

    def test_non_wechat_layout_returns_none(self):
        assert wechat.conversation_from_structure({"layout": "generic"}) is None
        assert wechat.conversation_from_structure({}) is None
        assert wechat.conversation_from_structure(None) is None

    def test_empty_conversation_and_sidebar_returns_none(self):
        assert wechat.conversation_from_structure(_struct([], chats=[])) is None

    def test_window_title_fallback(self):
        conv = wechat.conversation_from_structure(
            _struct([{"name": "对方", "text": "在吗"}], name=None), window_title="文件传输助手"
        )
        assert conv.thread_title == "文件传输助手"


# ─── body normalization (blunts OCR jitter) ─────────────────────────────────────


class TestNormalize:
    def test_collapse_whitespace_and_drop_trailing_ellipsis(self):
        assert wechat._normalize_body("你好  世界…") == "你好 世界"
        assert wechat._normalize_body("产品加上BP...") == "产品加上BP"
        assert wechat._normalize_body("  hi \n there  ") == "hi there"
        assert wechat._normalize_body("") == ""

"""Tests for the per-app parser layer and the Feishu parser.

Fixtures (``tests/fixtures/captures/lark/*.json``) are two real Feishu captures
in feed-list state. They are loaded via the ``load_capture_fixture`` conftest
helper; only the ``ax_tree`` is exercised here.
"""

from __future__ import annotations

import pytest

from persome import parsers
from persome.parsers import _axtree as ax
from persome.parsers.base import Message, ParsedConversation

_LARK_BUNDLE = "com.electron.lark"

# Left-sidebar filter tabs that must never leak into parsed messages.
_FILTER_TAB_LABELS = ("消息", "未读", "@我", "单聊", "群组", "云文档", "话题")


# --------------------------------------------------------------------------- #
# _axtree utilities                                                           #
# --------------------------------------------------------------------------- #


def _node(role="AXGroup", value=None, classes=None, children=None):
    return {
        "role": role,
        "value": value,
        "domClassList": list(classes or []),
        "children": list(children or []),
    }


def test_has_class_and_text_of():
    n = _node(role="AXStaticText", value="hi", classes=["a", "b"])
    assert ax.has_class(n, "a") is True
    assert ax.has_class(n, "z") is False
    assert ax.has_class(_node(), "anything") is False
    assert ax.text_of(n) == "hi"
    assert ax.text_of(_node()) is None


def test_walk_dfs_preorder():
    tree = _node(
        value="root",
        children=[
            _node(value="a", children=[_node(value="a1")]),
            _node(value="b"),
        ],
    )
    assert [ax.text_of(n) for n in ax.walk(tree)] == ["root", "a", "a1", "b"]
    # Non-dict roots yield nothing.
    assert list(ax.walk("not-a-node")) == []  # type: ignore[arg-type]


def test_find_all_filters_are_anded():
    tree = _node(
        children=[
            _node(role="AXStaticText", value="x", classes=["target"]),
            _node(role="AXStaticText", value="y", classes=["other"]),
            _node(role="AXButton", value="z", classes=["target"]),
        ]
    )
    by_role = ax.find_all(tree, role="AXStaticText")
    assert [ax.text_of(n) for n in by_role] == ["x", "y"]

    by_class = ax.find_all(tree, dom_class="target")
    assert [ax.text_of(n) for n in by_class] == ["x", "z"]

    both = ax.find_all(tree, role="AXStaticText", dom_class="target")
    assert [ax.text_of(n) for n in both] == ["x"]

    by_pred = ax.find_all(tree, pred=lambda n: ax.text_of(n) == "z")
    assert [ax.text_of(n) for n in by_pred] == ["z"]


def test_prune_subtrees_drops_matches_and_is_non_destructive():
    tree = _node(
        value="root",
        children=[
            _node(value="keep"),
            _node(value="drop", classes=["bad"], children=[_node(value="child")]),
        ],
    )
    pruned = ax.prune_subtrees(tree, lambda n: ax.has_class(n, "bad"))
    assert [ax.text_of(n) for n in ax.walk(pruned)] == ["root", "keep"]
    # Original tree is untouched.
    assert [ax.text_of(n) for n in ax.walk(tree)] == [
        "root",
        "keep",
        "drop",
        "child",
    ]


def test_frontmost_window_elements_prefers_focused_window():
    ax_tree = {
        "apps": [
            {
                "bundle_id": "other.app",
                "windows": [{"focused": True, "elements": [_node(value="other")]}],
            },
            {
                "bundle_id": _LARK_BUNDLE,
                "windows": [
                    {"focused": False, "elements": [_node(value="first")]},
                    {"focused": True, "elements": [_node(value="focused")]},
                ],
            },
        ]
    }
    els = ax.frontmost_window_elements(ax_tree, _LARK_BUNDLE)
    assert [ax.text_of(n) for n in els] == ["focused"]


def test_frontmost_window_elements_falls_back_to_first_window():
    ax_tree = {
        "apps": [
            {
                "bundle_id": _LARK_BUNDLE,
                "windows": [
                    {"focused": False, "elements": [_node(value="first")]},
                    {"focused": False, "elements": [_node(value="second")]},
                ],
            }
        ]
    }
    els = ax.frontmost_window_elements(ax_tree, _LARK_BUNDLE)
    assert [ax.text_of(n) for n in els] == ["first"]


def test_frontmost_window_elements_no_match_returns_empty():
    assert ax.frontmost_window_elements({"apps": []}, _LARK_BUNDLE) == []
    assert ax.frontmost_window_elements({}, _LARK_BUNDLE) == []
    ax_tree = {"apps": [{"bundle_id": _LARK_BUNDLE, "windows": []}]}
    assert ax.frontmost_window_elements(ax_tree, _LARK_BUNDLE) == []


# --------------------------------------------------------------------------- #
# base.ParsedConversation.render                                              #
# --------------------------------------------------------------------------- #


def test_render_layout_and_labels():
    conv = ParsedConversation(
        app="feishu",
        thread_title="标题",
        messages=[
            Message(sender="张三", body="你好", timestamp_text="12:20", direction="incoming"),
            Message(sender=None, body="收到", timestamp_text=None, direction="outgoing"),
        ],
        parser_version="t",
    )
    assert conv.render() == (
        '<screen_conversation app="feishu">\n'
        '<current_conversation name="标题">\n'
        '<message dir="received" sender="张三" time="12:20">你好</message>\n'
        '<message dir="sent">收到</message>\n'
        "</current_conversation>\n"
        "</screen_conversation>"
    )


def test_render_empty_messages_returns_empty_string():
    conv = ParsedConversation(app="feishu", thread_title="x", messages=[], parser_version="t")
    assert conv.render() == ""


# --------------------------------------------------------------------------- #
# registry                                                                    #
# --------------------------------------------------------------------------- #


def test_get_parser_routes_lark_bundle():
    parser = parsers.get_parser(_LARK_BUNDLE)
    assert parser is not None
    assert parser.version == "feishu-1"


def test_get_parser_unknown_or_none_returns_none():
    assert parsers.get_parser("com.unknown.app") is None
    assert parsers.get_parser(None) is None
    assert parsers.get_parser("") is None


# --------------------------------------------------------------------------- #
# FeishuParser — feed-list state (real fixtures)                              #
# --------------------------------------------------------------------------- #


@pytest.fixture
def feed_meeting(load_capture_fixture):
    return load_capture_fixture("lark", "feed-list-with-meeting")


def test_feishu_parses_feed_meeting_card(feed_meeting):
    parser = parsers.get_parser(_LARK_BUNDLE)
    conv = parser.parse(feed_meeting["ax_tree"], window_title="飞书")
    assert conv is not None
    assert conv.app == "feishu"
    assert conv.parser_version == "feishu-1"
    assert conv.messages, "expected at least one parsed card"

    rendered = conv.render()
    # The schedulable meeting signal lives in the feed-preview section and must
    # survive even though a conversation is also open in the main pane.
    assert "会议" in rendered
    assert "20:00 - 20:30" in rendered
    assert "沈砚舟" in rendered
    # This fixture has a conversation open in the main pane, so the result mixes
    # outgoing thread bubbles with incoming feed previews — it is no longer
    # all-incoming the way a pure feed-list state would be.
    assert any(m.direction == "outgoing" for m in conv.messages)
    assert any(m.direction == "incoming" for m in conv.messages)


def test_feishu_strips_filter_tabs(feed_meeting):
    parser = parsers.get_parser(_LARK_BUNDLE)
    conv = parser.parse(feed_meeting["ax_tree"], window_title="飞书")
    assert conv is not None
    # Left filter tabs (消息/未读/单聊…) must be pruned, not parsed as messages.
    for label in _FILTER_TAB_LABELS:
        assert not any(m.body == label or m.sender == label for m in conv.messages), (
            f"filter tab {label!r} leaked into a parsed message"
        )


def test_feishu_extracts_sender_and_timestamp(feed_meeting):
    parser = parsers.get_parser(_LARK_BUNDLE)
    conv = parser.parse(feed_meeting["ax_tree"], window_title="飞书")
    assert conv is not None
    # The "沈砚舟 / 12:20 / 我超" feed-preview card lives in ``previews`` (a
    # different conversation, not the current thread): header split into sender
    # + timestamp.
    hit = next(
        (m for m in conv.previews if m.sender == "沈砚舟" and m.timestamp_text == "12:20"),
        None,
    )
    assert hit is not None
    assert "我超" in hit.body


def test_feishu_thread_title_is_the_open_conversation_name(feed_meeting):
    """thread_title comes from chatWindow_chatName (the open conversation's
    name), not the generic window title — so the model knows *which*
    conversation is open, and render() labels the current-conversation section
    with it.
    """
    parser = parsers.get_parser(_LARK_BUNDLE)
    conv = parser.parse(feed_meeting["ax_tree"], window_title="飞书")
    assert conv is not None
    assert conv.thread_title == "沈砚舟"  # the open 1:1 peer, not "飞书"
    assert '<current_conversation name="沈砚舟">' in conv.render()


def test_feishu_thread_title_falls_back_to_window_title():
    """With no chatWindow_chatName (e.g. synthetic / list-only), thread_title
    falls back to the window title."""
    parser = parsers.get_parser(_LARK_BUNDLE)
    bubble = _bubble("message-not-self", sender="某人", body="在吗")
    ax_tree = {
        "apps": [{"bundle_id": _LARK_BUNDLE, "windows": [{"focused": True, "elements": [bubble]}]}]
    }
    conv = parser.parse(ax_tree, window_title="飞书")
    assert conv is not None
    assert conv.thread_title == "飞书"


def test_feishu_drops_badge_tokens_from_sender(feed_meeting):
    parser = parsers.get_parser(_LARK_BUNDLE)
    conv = parser.parse(feed_meeting["ax_tree"], window_title="飞书")
    assert conv is not None
    # The "刘小舟 / 智能体 / 11:28" bot card (a feed preview): 智能体 badge dropped.
    hit = next((m for m in conv.previews if m.sender and "刘小舟" in m.sender), None)
    assert hit is not None
    assert hit.sender == "刘小舟"
    assert "日程已创建" in hit.body


def test_feishu_body_truncation(feed_meeting):
    parser = parsers.get_parser(_LARK_BUNDLE)
    conv = parser.parse(feed_meeting["ax_tree"], window_title="飞书")
    assert conv is not None
    assert all(len(m.body) <= 500 for m in conv.messages)


def test_feishu_second_fixture_parses(load_capture_fixture):
    parser = parsers.get_parser(_LARK_BUNDLE)
    cap = load_capture_fixture("lark", "feed-list-2")
    conv = parser.parse(cap["ax_tree"], window_title="飞书")
    assert conv is not None
    assert conv.messages


# --------------------------------------------------------------------------- #
# FeishuParser — None fallbacks                                               #
# --------------------------------------------------------------------------- #


def test_feishu_no_matching_app_returns_none():
    parser = parsers.get_parser(_LARK_BUNDLE)
    ax_tree = {
        "apps": [
            {
                "bundle_id": "com.apple.Safari",
                "windows": [{"focused": True, "elements": [_node(value="hi")]}],
            }
        ]
    }
    assert parser.parse(ax_tree, window_title="Safari") is None


def test_feishu_empty_ax_tree_returns_none():
    parser = parsers.get_parser(_LARK_BUNDLE)
    assert parser.parse({"apps": []}, window_title=None) is None
    assert parser.parse({}, window_title=None) is None


def test_feishu_app_without_anchors_returns_none():
    parser = parsers.get_parser(_LARK_BUNDLE)
    # Right app, but no feed cards and no message items → no signal.
    ax_tree = {
        "apps": [
            {
                "bundle_id": _LARK_BUNDLE,
                "windows": [{"focused": True, "elements": [_node(value="just chrome")]}],
            }
        ]
    }
    assert parser.parse(ax_tree, window_title="飞书") is None


# --------------------------------------------------------------------------- #
# FeishuParser — open-thread state (real structure, verified on fixtures)      #
# --------------------------------------------------------------------------- #


def _bubble(direction_class, *, sender=None, body="", extra=None):
    """Build a synthetic message-item mirroring the real lark structure.

    Real bubbles carry direction on the message-item (message-self /
    message-not-self), the incoming sender in a ``message-info-name`` node, and
    the body inside a ``message-content`` subtree (reaction names / read
    receipts live *outside* message-content).
    """
    content_children = [_node(role="AXStaticText", value=body)]
    if extra:
        content_children.extend(_node(role="AXStaticText", value=t) for t in extra)
    children = []
    if sender is not None:
        children.append(
            _node(
                classes=["message-info-name"], children=[_node(role="AXStaticText", value=sender)]
            )
        )
    children.append(_node(classes=["message-content"], children=content_children))
    return _node(classes=["message-item", direction_class], children=children)


def test_feishu_open_thread_direction_and_sender_from_structure():
    parser = parsers.get_parser(_LARK_BUNDLE)
    incoming = _bubble("message-not-self", sender="对方", body="对方说的话")
    outgoing = _bubble("message-self", body="我说的话")
    continuation = _bubble("message-not-self", body="延续行没有名字")
    ax_tree = {
        "apps": [
            {
                "bundle_id": _LARK_BUNDLE,
                "windows": [{"focused": True, "elements": [incoming, outgoing, continuation]}],
            }
        ]
    }
    conv = parser.parse(ax_tree, window_title="某人")
    assert conv is not None
    assert [m.direction for m in conv.messages] == ["incoming", "outgoing", "incoming"]
    # Incoming sender from message-info-name; outgoing attributed to 我;
    # continuation row has no name label → None (we do not guess).
    assert [m.sender for m in conv.messages] == ["对方", "我", None]
    assert conv.messages[0].body == "对方说的话"
    assert conv.thread_title == "某人"


def test_s1_enrich_feishu_visible_text_carries_direction():
    """The capture's visible_text must keep who-said-what for chat apps — the user's own messages
    labelled dir="sent"/我, the counterpart's dir="received" — so downstream LLMs (session modeling,
    current_context, voice preamble) don't attribute the user's messages to the other party. Before
    the chat-aware render, visible_text was a flat dump with no direction (the reported bug)."""
    from persome.capture import s1_parser

    incoming = _bubble("message-not-self", sender="对方", body="对方发的")
    outgoing = _bubble("message-self", body="我自己发的")
    capture = {
        "ax_tree": {
            "apps": [
                {
                    "bundle_id": _LARK_BUNDLE,
                    "name": "飞书",
                    "is_frontmost": True,
                    "windows": [
                        {"title": "对方", "focused": True, "elements": [incoming, outgoing]}
                    ],
                }
            ]
        }
    }
    s1_parser.enrich(capture)
    vt = capture["visible_text"]
    assert '<message dir="received" sender="对方">对方发的</message>' in vt
    assert '<message dir="sent" sender="我">我自己发的</message>' in vt
    # the user's own message is NOT rendered as received / from the counterpart
    assert "我自己发的</message>" in vt and 'dir="received" sender="对方">我自己发的' not in vt


def test_feishu_open_thread_image_bubble_gets_placeholder():
    """A pure-image bubble (no text under message-content) is not dropped — it
    renders an [图片] placeholder so a sent screenshot/poster never vanishes."""
    parser = parsers.get_parser(_LARK_BUNDLE)
    image_bubble = _node(
        classes=["message-item", "message-not-self"],
        children=[
            _node(
                classes=["message-info-name"],
                children=[_node(role="AXStaticText", value="对方")],
            ),
            _node(
                classes=["message-content"],
                children=[_node(classes=["message-image", "im-image-message"])],  # image, no text
            ),
        ],
    )
    text_bubble = _bubble("message-not-self", sender="对方", body="一段文字")
    ax_tree = {
        "apps": [
            {
                "bundle_id": _LARK_BUNDLE,
                "windows": [{"focused": True, "elements": [image_bubble, text_bubble]}],
            }
        ]
    }
    conv = parser.parse(ax_tree, window_title="某人")
    assert conv is not None
    # Image bubble survives as a placeholder, keeping its sender + direction.
    img = conv.messages[0]
    assert img.body == "[图片]"
    assert img.direction == "incoming"
    assert img.sender == "对方"
    assert '<message dir="received" sender="对方">[图片]</message>' in conv.render()


def test_feishu_open_thread_empty_bubble_still_skipped():
    """A bubble with neither text nor image is still dropped (no signal)."""
    parser = parsers.get_parser(_LARK_BUNDLE)
    empty = _node(
        classes=["message-item", "message-not-self"],
        children=[_node(classes=["message-content"], children=[])],
    )
    text_bubble = _bubble("message-not-self", sender="对方", body="一段文字")
    ax_tree = {
        "apps": [
            {
                "bundle_id": _LARK_BUNDLE,
                "windows": [{"focused": True, "elements": [empty, text_bubble]}],
            }
        ]
    }
    conv = parser.parse(ax_tree, window_title="某人")
    assert conv is not None
    assert len(conv.messages) == 1
    assert conv.messages[0].body == "一段文字"


def test_feishu_open_thread_body_scoped_to_message_content():
    """Reaction names / read receipts outside message-content must not leak."""
    parser = parsers.get_parser(_LARK_BUNDLE)
    bubble = _node(
        classes=["message-item", "message-not-self"],
        children=[
            _node(
                classes=["message-info-name"], children=[_node(role="AXStaticText", value="张三")]
            ),
            _node(
                classes=["message-content"],
                children=[_node(role="AXStaticText", value="真正的正文")],
            ),
            # Reaction avatars / read-receipt name sit as bare siblings.
            _node(role="AXStaticText", value="李四"),
            _node(role="AXStaticText", value="2 条回复"),
        ],
    )
    ax_tree = {
        "apps": [{"bundle_id": _LARK_BUNDLE, "windows": [{"focused": True, "elements": [bubble]}]}]
    }
    conv = parser.parse(ax_tree, window_title="张三")
    assert conv is not None
    assert len(conv.messages) == 1
    assert conv.messages[0].body == "真正的正文"
    assert "李四" not in conv.messages[0].body
    assert "2 条回复" not in conv.messages[0].body


def test_feishu_open_thread_drops_edit_and_expand_chrome():
    parser = parsers.get_parser(_LARK_BUNDLE)
    bubble = _bubble(
        "message-not-self",
        sender="张三",
        body="正文内容",
        extra=["（已编辑）", "展开"],
    )
    ax_tree = {
        "apps": [{"bundle_id": _LARK_BUNDLE, "windows": [{"focused": True, "elements": [bubble]}]}]
    }
    conv = parser.parse(ax_tree, window_title="张三")
    assert conv is not None
    assert conv.messages[0].body == "正文内容"
    assert "（已编辑）" not in conv.messages[0].body
    assert "展开" not in conv.messages[0].body


# --------------------------------------------------------------------------- #
# FeishuParser — routing: open-thread is primary, feed is secondary            #
# (the real bug: feed cards are always present, so the old router never        #
#  parsed the open conversation)                                              #
# --------------------------------------------------------------------------- #


@pytest.fixture
def open_thread(load_capture_fixture):
    return load_capture_fixture("lark", "open-thread-dominant")


def test_feishu_parses_open_thread_main_pane(open_thread):
    """The open conversation in the main pane is parsed, not just the sidebar."""
    parser = parsers.get_parser(_LARK_BUNDLE)
    conv = parser.parse(open_thread["ax_tree"], window_title="飞书")
    assert conv is not None
    rendered = conv.render()
    # A real line from the open conversation's main pane (was invisible before
    # the routing fix because the always-present feed sidebar shadowed it).
    # Pick one that is within the kept thread tail.
    assert "在做意图识别还有记忆的整合" in rendered or "calendar" in rendered
    # Real directions come through (not all-incoming): the user's own bubbles
    # are outgoing, attributed to 我.
    assert any(m.direction == "outgoing" and m.sender == "我" for m in conv.messages)
    # Incoming sender attribution from message-info-name.
    assert any(m.direction == "incoming" and m.sender for m in conv.messages)


def test_feishu_open_thread_keeps_feed_meeting_preview(open_thread):
    """Meeting/schedule signals from feed previews survive alongside the thread."""
    parser = parsers.get_parser(_LARK_BUNDLE)
    conv = parser.parse(open_thread["ax_tree"], window_title="飞书")
    assert conv is not None
    # Feed previews (other conversations) carry the verbatim timestamp labels;
    # at least one timestamped preview must be present as secondary context —
    # and it lives in ``previews``, kept separate from the current thread.
    assert any(m.timestamp_text for m in conv.previews), (
        "expected at least one feed preview with a timestamp as secondary context"
    )


def test_feishu_open_thread_sections_are_separated(open_thread):
    """The current thread and other-conversation previews render in two clearly
    labeled sections, so N unrelated previews never read as one conversation.

    Regression for the "n 个会话叠在一个会话中" bug: previews used to be flat-
    concatenated into ``messages`` under a single "当前会话" label.
    """
    parser = parsers.get_parser(_LARK_BUNDLE)
    conv = parser.parse(open_thread["ax_tree"], window_title="飞书")
    assert conv is not None
    # Both populated for an open conversation with a sidebar feed.
    assert conv.messages, "open thread should populate the current conversation"
    assert conv.previews, "sidebar feed cards should populate previews"
    # The open conversation is named from chatWindow_chatName, not the window.
    assert conv.thread_title and conv.thread_title != "飞书"
    rendered = conv.render()
    # Two distinct XML sections, current conversation before the previews; the
    # <current_conversation> tag names which conversation is open.
    assert f'<current_conversation name="{conv.thread_title}">' in rendered
    assert "<other_conversations" in rendered
    assert rendered.index("<current_conversation") < rendered.index("<other_conversations")
    # Previews are <preview> tags (a different conversation each) and never
    # carry a dir= — they are other chats' latest messages, not turns in the
    # current thread (only <message> elements carry dir=).
    preview_section = rendered.split("<other_conversations", 1)[1]
    assert "<preview" in preview_section
    assert "dir=" not in preview_section


def test_feishu_open_thread_body_truncation(open_thread):
    parser = parsers.get_parser(_LARK_BUNDLE)
    conv = parser.parse(open_thread["ax_tree"], window_title="飞书")
    assert conv is not None
    assert all(len(m.body) <= 500 for m in conv.messages)


def _feed_card(*texts):
    """Build a synthetic feed card whose texts are [sender, timestamp, body…]."""
    return _node(
        classes=["a11y_feed_card_item"],
        children=[_node(role="AXStaticText", value=t) for t in texts],
    )


def test_feishu_long_thread_does_not_starve_feed_meeting_preview():
    """A long open conversation must not crowd out the feed meeting preview.

    The schedulable signal (a meeting invite) lives in the feed previews; the
    budgets are partitioned so even a very long thread (>20 bubbles) leaves room
    for the feed previews. Regression guard for the budget-starvation bug.
    """
    parser = parsers.get_parser(_LARK_BUNDLE)
    # 30 thread bubbles — more than any single combined cap — plus a feed card
    # carrying the meeting invite.
    bubbles = [
        _bubble("message-not-self", sender=f"用户{i}", body=f"消息正文 {i}") for i in range(30)
    ]
    meeting_card = _feed_card("会议", "20:00 - 20:30", "今晚产研对齐会议")
    ax_tree = {
        "apps": [
            {
                "bundle_id": _LARK_BUNDLE,
                "windows": [{"focused": True, "elements": bubbles + [meeting_card]}],
            }
        ]
    }
    conv = parser.parse(ax_tree, window_title="某群")
    assert conv is not None
    rendered = conv.render()
    # The meeting preview survives despite the 30-message thread.
    assert "会议" in rendered
    assert "20:00 - 20:30" in rendered
    # And the thread tail is still present (the most-recent bubble).
    assert "消息正文 29" in rendered
    # Sanity: the feed preview is a distinct timestamped entry in previews
    # (other conversations), not merged into the current thread.
    assert any(m.timestamp_text == "20:00 - 20:30" for m in conv.previews)
    assert all(m.timestamp_text != "20:00 - 20:30" for m in conv.messages)


# --------------------------------------------------------------------------- #
# FeishuParser — lark.iron meeting renderer (#548)                            #
# --------------------------------------------------------------------------- #

_IRON_BUNDLE = "com.electron.lark.iron"


def test_get_parser_routes_lark_iron_bundle():
    """The Feishu meetings renderer (Lark Helper (Iron) / 飞书会议) routes to the
    same FeishuParser — registered so its windows are a monitored ``miss``
    instead of an unowned ``fallback`` in parser telemetry."""
    parser = parsers.get_parser(_IRON_BUNDLE)
    assert parser is not None
    assert parser.version == "feishu-1"
    assert parser is parsers.get_parser(_LARK_BUNDLE)
    assert _IRON_BUNDLE in parser.bundle_ids


def test_feishu_declines_iron_meeting_window(load_capture_fixture):
    """Real iron capture shape (sanitized): the 飞书会议 window exposes an empty
    AX tree — a single bare RootView AXGroup, no text, no a11y_* classes
    (forensics 2026-06-12: 46/46 live captures). The parser must decline
    (None), which telemetry records as miss/decline — the correct verdict for
    an AX-opaque window, distinguishable from breakage via the miss reason."""
    cap = load_capture_fixture("lark-iron", "meeting-window")
    assert cap["window_meta"]["bundle_id"] == _IRON_BUNDLE
    parser = parsers.get_parser(_IRON_BUNDLE)
    assert parser.parse(cap["ax_tree"], window_title="飞书会议") is None


def test_feishu_parses_lark_dom_under_iron_bundle():
    """Future-proofing half of the iron registration: if a Lark build ever
    exposes the meeting window's DOM-derived AX (same Electron codebase, same
    semantic classes), the parser picks it up with zero further changes."""
    parser = parsers.get_parser(_IRON_BUNDLE)
    bubble = _bubble("message-not-self", sender="对方", body="会后同步一下结论")
    ax_tree = {
        "apps": [
            {
                "bundle_id": _IRON_BUNDLE,
                "windows": [{"focused": True, "elements": [bubble]}],
            }
        ]
    }
    conv = parser.parse(ax_tree, window_title="飞书会议")
    assert conv is not None
    assert conv.messages
    assert conv.messages[0].body == "会后同步一下结论"
    assert conv.messages[0].direction == "incoming"

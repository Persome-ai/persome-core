"""Tests for the per-app parser layer and Feishu parser using synthetic AX trees."""

from __future__ import annotations

import pytest

from persome import parsers
from persome.parsers import _axtree as ax
from persome.parsers.base import Message, ParsedConversation

_LARK_BUNDLE = "com.electron.lark"

# Left-sidebar filter tabs that must never leak into parsed messages.
_FILTER_TAB_LABELS = (
    "\u6d88\u606f",
    "\u672a\u8bfb",
    "@\u6211",
    "\u5355\u804a",
    "\u7fa4\u7ec4",
    "\u4e91\u6587\u6863",
    "\u8bdd\u9898",
)


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
        thread_title="\u6807\u9898",
        messages=[
            Message(
                sender="\u5f20\u4e09",
                body="\u4f60\u597d",
                timestamp_text="12:20",
                direction="incoming",
            ),
            Message(sender=None, body="\u6536\u5230", timestamp_text=None, direction="outgoing"),
        ],
        parser_version="t",
    )
    assert conv.render() == (
        '<screen_conversation app="feishu">\n'
        '<current_conversation name="\u6807\u9898">\n'
        '<message dir="received" sender="\u5f20\u4e09" time="12:20">\u4f60\u597d</message>\n'
        '<message dir="sent">\u6536\u5230</message>\n'
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
# FeishuParser — synthetic feed and open-thread state                         #
# --------------------------------------------------------------------------- #


@pytest.fixture
def feed_meeting():
    return _synthetic_lark_capture()


def test_feishu_parses_feed_meeting_card(feed_meeting):
    parser = parsers.get_parser(_LARK_BUNDLE)
    conv = parser.parse(feed_meeting["ax_tree"], window_title="\u98de\u4e66")
    assert conv is not None
    assert conv.app == "feishu"
    assert conv.parser_version == "feishu-1"
    assert conv.messages, "expected at least one parsed card"

    rendered = conv.render()
    # The schedulable meeting signal lives in the feed-preview section and must
    # survive even though a conversation is also open in the main pane.
    assert "\u4f1a\u8bae" in rendered
    assert "20:00 - 20:30" in rendered
    assert "\u6d4b\u8bd5\u8054\u7cfb\u4eba" in rendered
    # The synthetic tree has a conversation open in the main pane, so the result mixes
    # outgoing thread bubbles with incoming feed previews — it is no longer
    # all-incoming the way a pure feed-list state would be.
    assert any(m.direction == "outgoing" for m in conv.messages)
    assert any(m.direction == "incoming" for m in conv.messages)


def test_feishu_strips_filter_tabs(feed_meeting):
    parser = parsers.get_parser(_LARK_BUNDLE)
    conv = parser.parse(feed_meeting["ax_tree"], window_title="\u98de\u4e66")
    assert conv is not None

    for label in _FILTER_TAB_LABELS:
        assert not any(m.body == label or m.sender == label for m in conv.messages), (
            f"filter tab {label!r} leaked into a parsed message"
        )


def test_feishu_extracts_sender_and_timestamp(feed_meeting):
    parser = parsers.get_parser(_LARK_BUNDLE)
    conv = parser.parse(feed_meeting["ax_tree"], window_title="\u98de\u4e66")
    assert conv is not None

    # different conversation, not the current thread): header split into sender
    # + timestamp.
    hit = next(
        (
            m
            for m in conv.previews
            if m.sender == "\u6d4b\u8bd5\u8054\u7cfb\u4eba" and m.timestamp_text == "12:20"
        ),
        None,
    )
    assert hit is not None
    assert "\u6536\u5230" in hit.body


def test_feishu_thread_title_is_the_open_conversation_name(feed_meeting):
    """thread_title comes from chatWindow_chatName (the open conversation's
    name), not the generic window title — so the model knows *which*
    conversation is open, and render() labels the current-conversation section
    with it.
    """
    parser = parsers.get_parser(_LARK_BUNDLE)
    conv = parser.parse(feed_meeting["ax_tree"], window_title="\u98de\u4e66")
    assert conv is not None
    assert conv.thread_title == "\u6d4b\u8bd5\u8054\u7cfb\u4eba"
    assert '<current_conversation name="\u6d4b\u8bd5\u8054\u7cfb\u4eba">' in conv.render()


def test_feishu_thread_title_falls_back_to_window_title():
    """With no chatWindow_chatName (e.g. synthetic / list-only), thread_title
    falls back to the window title."""
    parser = parsers.get_parser(_LARK_BUNDLE)
    bubble = _bubble("message-not-self", sender="\u67d0\u4eba", body="\u5728\u5417")
    ax_tree = {
        "apps": [{"bundle_id": _LARK_BUNDLE, "windows": [{"focused": True, "elements": [bubble]}]}]
    }
    conv = parser.parse(ax_tree, window_title="\u98de\u4e66")
    assert conv is not None
    assert conv.thread_title == "\u98de\u4e66"


def test_feishu_drops_badge_tokens_from_sender(feed_meeting):
    parser = parsers.get_parser(_LARK_BUNDLE)
    conv = parser.parse(feed_meeting["ax_tree"], window_title="\u98de\u4e66")
    assert conv is not None

    hit = next(
        (m for m in conv.previews if m.sender and "\u65e5\u7a0b\u52a9\u624b" in m.sender), None
    )
    assert hit is not None
    assert hit.sender == "\u65e5\u7a0b\u52a9\u624b"
    assert "\u65e5\u7a0b\u5df2\u521b\u5efa" in hit.body


def test_feishu_body_truncation(feed_meeting):
    parser = parsers.get_parser(_LARK_BUNDLE)
    conv = parser.parse(feed_meeting["ax_tree"], window_title="\u98de\u4e66")
    assert conv is not None
    assert all(len(m.body) <= 500 for m in conv.messages)


def test_feishu_synthetic_capture_parses():
    parser = parsers.get_parser(_LARK_BUNDLE)
    cap = _synthetic_lark_capture()
    conv = parser.parse(cap["ax_tree"], window_title="\u98de\u4e66")
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
    assert parser.parse(ax_tree, window_title="\u98de\u4e66") is None


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
    incoming = _bubble(
        "message-not-self", sender="\u5bf9\u65b9", body="\u5bf9\u65b9\u8bf4\u7684\u8bdd"
    )
    outgoing = _bubble("message-self", body="\u6211\u8bf4\u7684\u8bdd")
    continuation = _bubble("message-not-self", body="\u5ef6\u7eed\u884c\u6ca1\u6709\u540d\u5b57")
    ax_tree = {
        "apps": [
            {
                "bundle_id": _LARK_BUNDLE,
                "windows": [{"focused": True, "elements": [incoming, outgoing, continuation]}],
            }
        ]
    }
    conv = parser.parse(ax_tree, window_title="\u67d0\u4eba")
    assert conv is not None
    assert [m.direction for m in conv.messages] == ["incoming", "outgoing", "incoming"]

    # continuation row has no name label → None (we do not guess).
    assert [m.sender for m in conv.messages] == ["\u5bf9\u65b9", "self", None]
    assert conv.messages[0].body == "\u5bf9\u65b9\u8bf4\u7684\u8bdd"
    assert conv.thread_title == "\u67d0\u4eba"


def test_s1_enrich_feishu_visible_text_carries_direction():
    from persome.capture import s1_parser

    incoming = _bubble("message-not-self", sender="\u5bf9\u65b9", body="\u5bf9\u65b9\u53d1\u7684")
    outgoing = _bubble("message-self", body="\u6211\u81ea\u5df1\u53d1\u7684")
    capture = {
        "ax_tree": {
            "apps": [
                {
                    "bundle_id": _LARK_BUNDLE,
                    "name": "\u98de\u4e66",
                    "is_frontmost": True,
                    "windows": [
                        {"title": "\u5bf9\u65b9", "focused": True, "elements": [incoming, outgoing]}
                    ],
                }
            ]
        }
    }
    s1_parser.enrich(capture)
    vt = capture["visible_text"]
    assert '<message dir="received" sender="\u5bf9\u65b9">\u5bf9\u65b9\u53d1\u7684</message>' in vt
    assert '<message dir="sent" sender="self">\u6211\u81ea\u5df1\u53d1\u7684</message>' in vt
    # the user's own message is NOT rendered as received / from the counterpart
    assert (
        "\u6211\u81ea\u5df1\u53d1\u7684</message>" in vt
        and 'dir="received" sender="\u5bf9\u65b9">\u6211\u81ea\u5df1\u53d1\u7684' not in vt
    )


def test_feishu_open_thread_image_bubble_gets_placeholder():
    parser = parsers.get_parser(_LARK_BUNDLE)
    image_bubble = _node(
        classes=["message-item", "message-not-self"],
        children=[
            _node(
                classes=["message-info-name"],
                children=[_node(role="AXStaticText", value="\u5bf9\u65b9")],
            ),
            _node(
                classes=["message-content"],
                children=[_node(classes=["message-image", "im-image-message"])],  # image, no text
            ),
        ],
    )
    text_bubble = _bubble(
        "message-not-self", sender="\u5bf9\u65b9", body="\u4e00\u6bb5\u6587\u5b57"
    )
    ax_tree = {
        "apps": [
            {
                "bundle_id": _LARK_BUNDLE,
                "windows": [{"focused": True, "elements": [image_bubble, text_bubble]}],
            }
        ]
    }
    conv = parser.parse(ax_tree, window_title="\u67d0\u4eba")
    assert conv is not None
    # Image bubble survives as a placeholder, keeping its sender + direction.
    img = conv.messages[0]
    assert img.body == "[Image]"
    assert img.direction == "incoming"
    assert img.sender == "\u5bf9\u65b9"
    assert '<message dir="received" sender="\u5bf9\u65b9">[Image]</message>' in conv.render()


def test_feishu_open_thread_empty_bubble_still_skipped():
    """A bubble with neither text nor image is still dropped (no signal)."""
    parser = parsers.get_parser(_LARK_BUNDLE)
    empty = _node(
        classes=["message-item", "message-not-self"],
        children=[_node(classes=["message-content"], children=[])],
    )
    text_bubble = _bubble(
        "message-not-self", sender="\u5bf9\u65b9", body="\u4e00\u6bb5\u6587\u5b57"
    )
    ax_tree = {
        "apps": [
            {
                "bundle_id": _LARK_BUNDLE,
                "windows": [{"focused": True, "elements": [empty, text_bubble]}],
            }
        ]
    }
    conv = parser.parse(ax_tree, window_title="\u67d0\u4eba")
    assert conv is not None
    assert len(conv.messages) == 1
    assert conv.messages[0].body == "\u4e00\u6bb5\u6587\u5b57"


def test_feishu_open_thread_body_scoped_to_message_content():
    """Reaction names / read receipts outside message-content must not leak."""
    parser = parsers.get_parser(_LARK_BUNDLE)
    bubble = _node(
        classes=["message-item", "message-not-self"],
        children=[
            _node(
                classes=["message-info-name"],
                children=[_node(role="AXStaticText", value="\u5f20\u4e09")],
            ),
            _node(
                classes=["message-content"],
                children=[_node(role="AXStaticText", value="\u771f\u6b63\u7684\u6b63\u6587")],
            ),
            # Reaction avatars / read-receipt name sit as bare siblings.
            _node(role="AXStaticText", value="\u674e\u56db"),
            _node(role="AXStaticText", value="2 \u6761\u56de\u590d"),
        ],
    )
    ax_tree = {
        "apps": [{"bundle_id": _LARK_BUNDLE, "windows": [{"focused": True, "elements": [bubble]}]}]
    }
    conv = parser.parse(ax_tree, window_title="\u5f20\u4e09")
    assert conv is not None
    assert len(conv.messages) == 1
    assert conv.messages[0].body == "\u771f\u6b63\u7684\u6b63\u6587"
    assert "\u674e\u56db" not in conv.messages[0].body
    assert "2 \u6761\u56de\u590d" not in conv.messages[0].body


def test_feishu_open_thread_drops_edit_and_expand_chrome():
    parser = parsers.get_parser(_LARK_BUNDLE)
    bubble = _bubble(
        "message-not-self",
        sender="\u5f20\u4e09",
        body="\u6b63\u6587\u5185\u5bb9",
        extra=["\uff08\u5df2\u7f16\u8f91\uff09", "\u5c55\u5f00"],
    )
    ax_tree = {
        "apps": [{"bundle_id": _LARK_BUNDLE, "windows": [{"focused": True, "elements": [bubble]}]}]
    }
    conv = parser.parse(ax_tree, window_title="\u5f20\u4e09")
    assert conv is not None
    assert conv.messages[0].body == "\u6b63\u6587\u5185\u5bb9"
    assert "\uff08\u5df2\u7f16\u8f91\uff09" not in conv.messages[0].body
    assert "\u5c55\u5f00" not in conv.messages[0].body


# --------------------------------------------------------------------------- #
# FeishuParser — routing: open-thread is primary, feed is secondary            #
# (feed cards are always present, so the old router never                       #
#  parsed the open conversation)                                              #
# --------------------------------------------------------------------------- #


@pytest.fixture
def open_thread():
    return _synthetic_lark_capture()


def test_feishu_parses_open_thread_main_pane(open_thread):
    """The open conversation in the main pane is parsed, not just the sidebar."""
    parser = parsers.get_parser(_LARK_BUNDLE)
    conv = parser.parse(open_thread["ax_tree"], window_title="\u98de\u4e66")
    assert conv is not None
    rendered = conv.render()
    # A synthetic line from the open conversation's main pane (was invisible before
    # the routing fix because the always-present feed sidebar shadowed it).
    # Pick one that is within the kept thread tail.
    assert (
        "\u5728\u505a\u610f\u56fe\u8bc6\u522b\u8fd8\u6709\u8bb0\u5fc6\u7684\u6574\u5408" in rendered
        or "calendar" in rendered
    )
    # Real directions come through (not all-incoming): the user's own bubbles

    assert any(m.direction == "outgoing" and m.sender == "self" for m in conv.messages)
    # Incoming sender attribution from message-info-name.
    assert any(m.direction == "incoming" and m.sender for m in conv.messages)


def test_feishu_open_thread_keeps_feed_meeting_preview(open_thread):
    """Meeting/schedule signals from feed previews survive alongside the thread."""
    parser = parsers.get_parser(_LARK_BUNDLE)
    conv = parser.parse(open_thread["ax_tree"], window_title="\u98de\u4e66")
    assert conv is not None
    # Feed previews (other conversations) carry the verbatim timestamp labels;
    # at least one timestamped preview must be present as secondary context —
    # and it lives in ``previews``, kept separate from the current thread.
    assert any(m.timestamp_text for m in conv.previews), (
        "expected at least one feed preview with a timestamp as secondary context"
    )


def test_feishu_open_thread_sections_are_separated(open_thread):
    parser = parsers.get_parser(_LARK_BUNDLE)
    conv = parser.parse(open_thread["ax_tree"], window_title="\u98de\u4e66")
    assert conv is not None
    # Both populated for an open conversation with a sidebar feed.
    assert conv.messages, "open thread should populate the current conversation"
    assert conv.previews, "sidebar feed cards should populate previews"
    # The open conversation is named from chatWindow_chatName, not the window.
    assert conv.thread_title and conv.thread_title != "\u98de\u4e66"
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
    conv = parser.parse(open_thread["ax_tree"], window_title="\u98de\u4e66")
    assert conv is not None
    assert all(len(m.body) <= 500 for m in conv.messages)


def _feed_card(*texts):
    """Build a synthetic feed card whose texts are [sender, timestamp, body…]."""
    return _node(
        classes=["a11y_feed_card_item"],
        children=[_node(role="AXStaticText", value=t) for t in texts],
    )


def _synthetic_lark_capture() -> dict:
    """A complete synthetic AX tree with a thread, feed cards, and filter tabs."""
    elements = [
        _node(
            classes=["chatWindow_chatName"],
            children=[_node(role="AXStaticText", value="\u6d4b\u8bd5\u8054\u7cfb\u4eba")],
        ),
        _bubble(
            "message-not-self",
            sender="\u6d4b\u8bd5\u8054\u7cfb\u4eba",
            body="\u5728\u505a\u8bb0\u5fc6\u6574\u5408",
        ),
        _bubble("message-self", body="calendar \u63a5\u53e3\u5df2\u7ecf\u66f4\u65b0"),
        _node(
            classes=["a11y_feed_filter_list_item"],
            children=[_node(role="AXStaticText", value=label) for label in _FILTER_TAB_LABELS],
        ),
        _feed_card("\u6d4b\u8bd5\u8054\u7cfb\u4eba", "12:20", "\u6536\u5230"),
        _feed_card(
            "\u65e5\u7a0b\u52a9\u624b",
            "\u667a\u80fd\u4f53",
            "11:28",
            "\u65e5\u7a0b\u5df2\u521b\u5efa",
        ),
        _feed_card(
            "\u4f1a\u8bae",
            "20:00 - 20:30",
            "\u4eca\u665a\u8fd0\u884c\u65f6\u5bf9\u9f50\u4f1a\u8bae",
        ),
    ]
    return {
        "timestamp": "2026-07-10T09:00:00+08:00",
        "window_meta": {
            "app_name": "\u98de\u4e66",
            "title": "\u98de\u4e66",
            "bundle_id": _LARK_BUNDLE,
        },
        "ax_tree": {
            "apps": [
                {
                    "bundle_id": _LARK_BUNDLE,
                    "is_frontmost": True,
                    "windows": [{"focused": True, "elements": elements}],
                }
            ]
        },
    }


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
        _bubble("message-not-self", sender=f"\u7528\u6237{i}", body=f"\u6d88\u606f\u6b63\u6587 {i}")
        for i in range(30)
    ]
    meeting_card = _feed_card(
        "\u4f1a\u8bae", "20:00 - 20:30", "\u4eca\u665a\u4ea7\u7814\u5bf9\u9f50\u4f1a\u8bae"
    )
    ax_tree = {
        "apps": [
            {
                "bundle_id": _LARK_BUNDLE,
                "windows": [{"focused": True, "elements": bubbles + [meeting_card]}],
            }
        ]
    }
    conv = parser.parse(ax_tree, window_title="\u67d0\u7fa4")
    assert conv is not None
    rendered = conv.render()
    # The meeting preview survives despite the 30-message thread.
    assert "\u4f1a\u8bae" in rendered
    assert "20:00 - 20:30" in rendered
    # And the thread tail is still present (the most-recent bubble).
    assert "\u6d88\u606f\u6b63\u6587 29" in rendered
    # Sanity: the feed preview is a distinct timestamped entry in previews
    # (other conversations), not merged into the current thread.
    assert any(m.timestamp_text == "20:00 - 20:30" for m in conv.previews)
    assert all(m.timestamp_text != "20:00 - 20:30" for m in conv.messages)


# --------------------------------------------------------------------------- #
# FeishuParser — lark.iron meeting renderer (#548)                            #
# --------------------------------------------------------------------------- #

_IRON_BUNDLE = "com.electron.lark.iron"


def test_get_parser_routes_lark_iron_bundle():
    parser = parsers.get_parser(_IRON_BUNDLE)
    assert parser is not None
    assert parser.version == "feishu-1"
    assert parser is parsers.get_parser(_LARK_BUNDLE)
    assert _IRON_BUNDLE in parser.bundle_ids


def test_feishu_declines_iron_meeting_window():
    """The observed iron shape exposes an empty
    AX tree — a single bare RootView AXGroup, no text, no a11y_* classes
    (forensics 2026-06-12: 46/46 live captures). The parser must decline
    (None), which telemetry records as miss/decline — the correct verdict for
    an AX-opaque window, distinguishable from breakage via the miss reason."""
    tree = {
        "apps": [
            {
                "bundle_id": _IRON_BUNDLE,
                "is_frontmost": True,
                "windows": [
                    {
                        "focused": True,
                        "elements": [{"role": "AXGroup", "title": "RootView", "children": []}],
                    }
                ],
            }
        ]
    }
    parser = parsers.get_parser(_IRON_BUNDLE)
    assert parser.parse(tree, window_title="\u98de\u4e66\u4f1a\u8bae") is None


def test_feishu_parses_lark_dom_under_iron_bundle():
    """Future-proofing half of the iron registration: if a Lark build ever
    exposes the meeting window's DOM-derived AX (same Electron codebase, same
    semantic classes), the parser picks it up with zero further changes."""
    parser = parsers.get_parser(_IRON_BUNDLE)
    bubble = _bubble(
        "message-not-self",
        sender="\u5bf9\u65b9",
        body="\u4f1a\u540e\u540c\u6b65\u4e00\u4e0b\u7ed3\u8bba",
    )
    ax_tree = {
        "apps": [
            {
                "bundle_id": _IRON_BUNDLE,
                "windows": [{"focused": True, "elements": [bubble]}],
            }
        ]
    }
    conv = parser.parse(ax_tree, window_title="\u98de\u4e66\u4f1a\u8bae")
    assert conv is not None
    assert conv.messages
    assert conv.messages[0].body == "\u4f1a\u540e\u540c\u6b65\u4e00\u4e0b\u7ed3\u8bba"
    assert conv.messages[0].direction == "incoming"

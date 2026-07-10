"""Feishu / Lark (``com.electron.lark``) AX-tree parser.

Feishu's desktop client is Electron, so its windows expose a rich DOM-derived
AX tree with stable *semantic* class names (the ``a11y_*`` family) alongside
hash-randomized layout classes (e.g. ``a84b6211``, ``_97ba617``). We anchor on
the semantic classes only — the hashes change between builds.

Feishu's desktop UI is a **three-pane layout**: the left conversation list
(feed cards) is *always present* even while a conversation is open in the main
pane. So the two signals coexist in almost every capture and we parse **both**:

1. **Open-thread (main pane)** — the conversation currently open. Each
   ``message-item`` row is one bubble; direction is encoded by ``message-self``
   (outgoing) vs ``message-not-self`` (incoming) — reliable. The body lives in
   the ``message-content`` subtree (scoping to it strips reaction names, read
   receipts, and thread-reply chrome that otherwise leak in). The sender of an
   *incoming* bubble is the ``message-info-name`` label Feishu renders above
   other people's messages; outgoing bubbles carry no name (you don't see your
   own), so we attribute them to ``"self"``. This is the **primary** content —
   what the user is actually reading/typing right now.
2. **Feed-list (left sidebar)** — the conversation list. Each
   ``a11y_feed_card_item`` is one row: ``[sender, (badge), timestamp]`` + last
   message preview. Schedulable signals such as a meeting invitation
   surface here as previews of *other* conversations, so we keep them as
   **secondary** context (rendered under a separate heading). The left
   ``a11y_feed_filter_list_item`` tabs are pruned.

Routing: parse open-thread first (primary). If there are no ``message-item``
rows we degrade to a pure feed parse (preserves the original behaviour for the
conversation-list-only state). All anchors absent → ``parse`` returns ``None``
and the caller falls back to the legacy timeline normalization.

Anchor discipline: we key only off the semantic ``message-*`` / ``a11y_*``
classes — never the hash-randomized layout classes (e.g. ``a84b6211``).

Bundle coverage: besides the main client (``com.electron.lark``) we also claim
the meeting renderer process ``com.electron.lark.iron`` ("Lark Helper (Iron)").
Forensics over the live capture buffer (2026-06-12,
46/46 iron captures) show that window exposes an *empty* AX tree — a single
bare ``RootView`` AXGroup with no children, no text, no DOM ``a11y_*``
classes — so today ``parse`` always (correctly) declines on it. Registering it
anyway is deliberate: the decline is recorded as a ``miss`` (reason
``decline``) instead of an unowned ``fallback`` in parser telemetry, and if a
Lark build ever starts exposing the meeting window's DOM-derived AX (same
Electron codebase, same semantic classes), the parser picks it up with zero
further changes.
"""

from __future__ import annotations

import re

from . import _axtree as ax
from .base import Direction, Message, ParsedConversation, Parser

_BUNDLE = "com.electron.lark"

# docstring: its window is AX-opaque today, so parse declines — by design.
_BUNDLE_IRON = "com.electron.lark.iron"
# Deterministic lookup order for the window-element scan (a capture's ax_tree
# only ever contains one of these apps in practice; the tuple keeps iteration
# order stable unlike the frozenset).
_BUNDLES = (_BUNDLE, _BUNDLE_IRON)

# Cap a single body, matching the timeline aggregator's budget
# (_MAX_EVENTS_PER_WINDOW=30; chat tail truncation).
_MAX_BODY_CHARS = 500

# Budget for the conversation-list-only state (no open thread): a single feed
# parse, capped at the timeline aggregator's per-window budget.
_MAX_MESSAGES = 20

# When a conversation IS open, the budgets are PARTITIONED so a long thread can
# never starve the feed previews — the schedulable signals (a meeting invite,

# miss we must avoid. Each side is capped independently, then concatenated:
#   - the open thread keeps its most-recent tail (current conversation), and
#   - the feed previews keep a guaranteed floor (other conversations' latest).
_THREAD_TAIL = 15
_FEED_PREVIEW_LIMIT = 8

# A static-text node whose value matches this is a feed-card / thread timestamp
# label. Covers clock times, time ranges, relative days, and dated labels.
_TIMESTAMP_RE = re.compile(
    r"^(?:"
    r"\d{1,2}:\d{2}(?:\s*[-~]\s*\d{1,2}:\d{2})?"  # 12:20 / 20:00 - 20:30
    r"|\u6628\u5929|\u4eca\u5929|\u524d\u5929"
    r"|\d{1,2}\u6708\d{1,2}\u65e5"
    r"|\d{4}\u5e74\d{1,2}\u6708\d{1,2}\u65e5"
    r"|\u661f\u671f[\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u65e5\u5929]"
    r"|\u5468[\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u65e5\u5929]"
    r")$"
)

# Badge tokens that sit between the sender name and the timestamp in a feed
# card header (bot/agent markers, status flags). Dropped from the sender.
_HEADER_BADGES = frozenset(
    {
        "\u667a\u80fd\u4f53",
        "\u673a\u5668\u4eba",
        "\u5df2\u79bb\u804c",
        "\u5916\u90e8",
        "\u5b98\u65b9",
    }
)

# Semantic classes (stable across builds — never key off hash classes).
_CLS_FEED_CARD = "a11y_feed_card_item"
_CLS_FEED_FILTER = "a11y_feed_filter_list_item"
_CLS_FEED_DONE = "a11y_feed_item_done"
_CLS_MESSAGE_ITEM = "message-item"
# Direction lives on the message-item itself: message-self = outgoing,
# message-not-self = incoming (verified against the lark fixtures). The
# message-left / message-right classes are avatar-slot containers with no text,
# not direction markers — do not key off them.
_CLS_OUTGOING = ("message-self",)
_CLS_INCOMING = ("message-not-self",)
# The body of a bubble lives under message-content; scoping to it drops the
# reaction names, read-receipt avatars and thread-reply chrome that otherwise
# sit as bare siblings inside the message-item.
_CLS_MESSAGE_CONTENT = "message-content"
# The sender label Feishu renders above an *incoming* bubble. Absent on
# outgoing bubbles (you never see your own name) and on continuation rows.
_CLS_MESSAGE_INFO_NAME = "message-info-name"
# The open conversation's name in the main-pane header (the 1:1 peer name or
# the group name) — used as the thread title so the model knows *which*
# conversation is open. Absent in the conversation-list-only state.
_CLS_CHAT_NAME = "chatWindow_chatName"

# Self-sender label used when a bubble is outgoing (message-self): Feishu shows

_SELF_SENDER = "self"

# Image-message body classes (live inside message-content). A pure-image bubble
# exposes no text value, so without this it would be dropped as "empty" — we
# instead emit a placeholder so the model still sees an image was sent (a
# screenshot of a poster/calendar must not vanish). Avatar images
# (ud__avatar-image) live outside message-content and are intentionally excluded.
_CLS_IMAGE = (
    "message-image",
    "im-image-message",
    "rich-text-image",
    "image-message-img",
    "rich-text-message-img",
)
# Placeholder body for an image-only bubble (matches Feishu's own AX placeholder).
_IMAGE_PLACEHOLDER = "[Image]"

# Chrome tokens that live inside message-content but are not message text:
# edit markers, expand/collapse affordances, thread-reply counters. Dropped
# from the body so the model sees clean text.
_BODY_CHROME = frozenset(
    {
        "\uff08\u5df2\u7f16\u8f91\uff09",
        "(\u5df2\u7f16\u8f91)",
        "\u5c55\u5f00",
        "\u6536\u8d77",
        "\u56de\u590d\u8bdd\u9898",
        "\u67e5\u770b\u66f4\u65e9",
        "\u524d\u5f80\u4efb\u52a1\u4e2d\u5fc3\u67e5\u770b\u66f4\u591a",
    }
)

_BODY_CHROME_RE = re.compile(
    r"^(?:\d+\s*\u6761\u56de\u590d|\u67e5\u770b\u66f4\u65e9.*\u56de\u590d|\u56de\u590d\s)"
)


def _node_texts(node: ax.Node) -> list[str]:
    """Non-empty, stripped text values under ``node`` in DFS order."""
    out: list[str] = []
    for n in ax.walk(node):
        v = ax.text_of(n)
        if v is not None:
            s = v.strip()
            if s:
                out.append(s)
    return out


def _truncate(text: str) -> str:
    if len(text) <= _MAX_BODY_CHARS:
        return text
    return text[: _MAX_BODY_CHARS - 1] + "…"


def _split_header(texts: list[str]) -> tuple[str | None, str | None, list[str]]:
    """Split a feed card's texts into (sender, timestamp, preview_texts).

    The header is the leading run up to and including the first timestamp:
    everything before the timestamp (minus badge tokens) is the sender; the
    timestamp is the matching label; the remainder is the preview body.

    When no timestamp is present (rare / system rows) the whole thing is
    treated as preview with no sender.
    """
    ts_idx = next(
        (i for i, t in enumerate(texts) if _TIMESTAMP_RE.match(t)),
        None,
    )
    if ts_idx is None:
        return None, None, texts

    timestamp = texts[ts_idx]
    sender_tokens = [t for t in texts[:ts_idx] if t not in _HEADER_BADGES]
    sender = " ".join(sender_tokens).strip() or None
    preview = texts[ts_idx + 1 :]
    return sender, timestamp, preview


def _parse_feed(root: ax.Node) -> list[Message]:
    """Parse a feed-list-state container into one Message per card."""

    pruned = ax.prune_subtrees(root, lambda n: ax.has_class(n, _CLS_FEED_FILTER))
    cards = ax.find_all(pruned, dom_class=_CLS_FEED_CARD)

    messages: list[Message] = []
    for card in cards:
        # The "done"/status marker subtree carries no conversational text.
        body_root = ax.prune_subtrees(card, lambda n: ax.has_class(n, _CLS_FEED_DONE))
        texts = _node_texts(body_root)
        if not texts:
            continue
        sender, timestamp, preview = _split_header(texts)
        body = " ".join(preview).strip()
        if not body:
            # Header-only card (e.g. a meeting label with no preview): keep the
            # header text as the body so the signal is not lost.
            body = " ".join(t for t in (sender, timestamp) if t).strip()
            if not body:
                continue
        messages.append(
            Message(
                sender=sender,
                body=_truncate(body),
                timestamp_text=timestamp,
                direction="incoming",
            )
        )
    return messages


def _thread_title(root: ax.Node) -> str | None:
    """The open conversation's name from the main-pane header, or ``None``.

    Reads the ``chatWindow_chatName`` node (the 1:1 peer name or group name
    Feishu renders at the top of the open conversation). Returns ``None`` in the
    conversation-list-only state (no conversation open) so the caller can fall
    back to the window title.
    """
    for node in ax.find_all(root, dom_class=_CLS_CHAT_NAME):
        name = " ".join(_node_texts(node)).strip()
        if name:
            return name
    return None


def _message_direction(node: ax.Node) -> Direction:
    classes = node.get("domClassList") or []
    if any(c in classes for c in _CLS_OUTGOING):
        return "outgoing"
    if any(c in classes for c in _CLS_INCOMING):
        return "incoming"
    return "unknown"


def _is_body_chrome(text: str) -> bool:
    """True when ``text`` is UI chrome inside message-content, not message body."""
    return text in _BODY_CHROME or bool(_BODY_CHROME_RE.match(text))


def _thread_sender(item: ax.Node, direction: Direction) -> str | None:
    if direction == "outgoing":
        return _SELF_SENDER
    for name_node in ax.find_all(item, dom_class=_CLS_MESSAGE_INFO_NAME):
        name = " ".join(_node_texts(name_node)).strip()
        if name:
            return name
    return None


def _thread_body(item: ax.Node) -> str:
    """Body text of one bubble, scoped to the message-content subtree.

    Scoping to ``message-content`` drops the reaction names, read-receipt
    avatars and thread-reply chrome that sit as bare siblings inside the
    ``message-item``. Multiple ``AXStaticText`` fragments under it (Feishu
    splits a single message into several text nodes) are joined in DFS order;
    edit markers / expand affordances are filtered out.
    """
    contents = ax.find_all(item, dom_class=_CLS_MESSAGE_CONTENT)
    parts: list[str] = []
    for content in contents:
        for t in _node_texts(content):
            if not _is_body_chrome(t):
                parts.append(t)
    return " ".join(parts).strip()


def _item_has_image(item: ax.Node) -> bool:
    """True when the bubble carries an image-message node (not an avatar).

    Scans the whole ``message-item`` rather than just ``message-content`` — in
    some layouts the image node sits in a sibling container — relying on
    ``_CLS_IMAGE`` excluding avatar classes (``ud__avatar-image``) to avoid
    false positives. Only consulted when the bubble has no body text, so a
    captioned image (text present) is unaffected.
    """
    for node in ax.walk(item):
        classes = node.get("domClassList") or []
        if any(c in classes for c in _CLS_IMAGE):
            return True
    return False


def _parse_open_thread(root: ax.Node) -> list[Message]:
    items = ax.find_all(root, dom_class=_CLS_MESSAGE_ITEM)
    messages: list[Message] = []
    for item in items:
        direction = _message_direction(item)
        body = _thread_body(item)
        if not body:
            if _item_has_image(item):
                body = _IMAGE_PLACEHOLDER
            else:
                continue
        messages.append(
            Message(
                sender=_thread_sender(item, direction),
                body=_truncate(body),
                timestamp_text=None,
                direction=direction,
            )
        )
    return messages


class FeishuParser(Parser):
    bundle_ids = frozenset(_BUNDLES)
    version = "feishu-1"

    def parse(self, ax_tree: dict, *, window_title: str | None) -> ParsedConversation | None:
        # Try each claimed bundle in deterministic order — a capture's ax_tree
        # contains the app that was frontmost, so at most one of these matches.
        elements: list[ax.Node] = []
        for bundle in _BUNDLES:
            elements = ax.frontmost_window_elements(ax_tree, bundle)
            if elements:
                break
        if not elements:
            return None

        # A synthetic root so selectors can scan all top-level elements at once.
        root: ax.Node = {"role": "AXWindow", "children": elements}

        # Open-thread (main pane) is the primary content. The feed sidebar is
        # always present in the three-pane layout, so we can't route on its
        # presence — we parse the thread first and only fall back to a pure feed
        # parse when no conversation is open (no message-item rows).
        thread = _parse_open_thread(root)
        feed = _parse_feed(root)

        if thread:
            # A conversation is open: ``messages`` is that current thread; the
            # feed cards are previews of *other* conversations, kept in a
            # SEPARATE field so render() labels them distinctly (they are not
            # turns in the current thread — flattening them together made N
            # unrelated chats read as one). Budgets are partitioned and capped
            # independently: a long open conversation can't crowd out the feed
            # previews (where *other* chats' meeting/schedule signals live), and
            # the previews can't drown the current thread.
            messages = thread[-_THREAD_TAIL:]
            previews = feed[:_FEED_PREVIEW_LIMIT]
        else:
            # Conversation-list-only state: no open thread, so every row is a
            # preview of a (different) conversation. Nothing is the "current
            # conversation", so messages stays empty.
            messages = []
            previews = feed[:_MAX_MESSAGES]

        if not messages and not previews:
            return None

        # Prefer the open conversation's own name (chatWindow_chatName) over the

        # is open; fall back to the window title when no conversation is open.
        title = _thread_title(root) or (window_title or None)

        return ParsedConversation(
            app="feishu",
            thread_title=title,
            messages=messages,
            parser_version=self.version,
            previews=previews,
        )

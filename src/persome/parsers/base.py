"""Per-app message parser contract.

A *parser* turns one app's raw AX tree into a small, structured value that
downstream stages (timeline aggregator, trajectory recognizer) feed to the LLM
as ``focus_structured`` text instead of the lossy normalized timeline blob.

The only thing the callers need from that value is ``render() -> str`` — the
:class:`StructuredContent` protocol. A chat app produces a
:class:`ParsedConversation` (sender / time / body / direction per message); a
browser produces a ``WebPage`` (clean body blocks). Both satisfy the protocol,
so ``Parser.parse`` returns ``StructuredContent | None`` and the aggregator /
recognizer only ever call ``.render()`` — they never branch on the concrete
type.

This module defines only the contract. Concrete parsers live in sibling
modules (``feishu.py``, ``web.py``, …) and register themselves via
``parsers.register``. The dataclasses are frozen so a result is a value object
that can be cached / compared cheaply.
"""

from __future__ import annotations

import html
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

Direction = Literal["incoming", "outgoing", "unknown"]


@runtime_checkable
class StructuredContent(Protocol):
    """A parsed, render-to-text view of one app window at capture time.

    The single capability downstream stages depend on: turn the structured
    value into the ``focus_structured`` text fed to the LLM. ``render`` returns
    ``""`` when there is nothing worth showing (the caller then falls back to
    the legacy normalized text / raw excerpt).
    """

    def render(self) -> str:
        """Render to the ``focus_structured`` text; ``""`` when empty."""
        ...


# ``dir`` attribute values on a <message>. English keeps the structured markup
# code-like and unambiguous; the recognizer prompt explains them.
_DIRECTION_ATTR: dict[str, str] = {
    "incoming": "received",
    "outgoing": "sent",
    "unknown": "unknown",
}


def _esc_text(s: str) -> str:
    """Escape XML element text (``&`` ``<`` ``>``); leaves quotes readable."""
    return html.escape(s, quote=False)


def _esc_attr(s: str) -> str:
    """Escape an XML attribute value and wrap it in double quotes (quoteattr)."""
    return '"' + html.escape(s, quote=True) + '"'


@dataclass(frozen=True)
class Message:
    """One message (or feed-card preview) extracted from an app's AX tree.

    ``body`` is always present (the meaningful text). ``sender`` and
    ``timestamp_text`` are best-effort: ``None`` when the AX tree does not
    expose them. ``timestamp_text`` is the *verbatim* on-screen label
    (e.g. ``"12:20"``, ``"昨天"``, ``"5月27日"``) — parsers do not normalize it
    to an absolute time, that is the recognizer's job if it ever needs it.
    """

    sender: str | None
    body: str
    timestamp_text: str | None = None
    direction: Direction = "unknown"


@dataclass(frozen=True)
class ParsedConversation:
    """Structured view of one app window at capture time.

    ``messages`` is the **current conversation** — the thread the user has open
    (oldest → newest). ``previews`` is **other conversations**: one entry per
    *different* chat, carrying only its latest message (e.g. the unread rows of
    a sidebar conversation list). Keeping them in separate fields is what lets
    ``render`` label them distinctly, so the recognizer never reads N unrelated
    conversations' previews as one continuous thread.

    ``parser_version`` lets downstream stages and analytics attribute a result
    to a specific parser revision.
    """

    app: str
    thread_title: str | None
    messages: list[Message]
    parser_version: str
    previews: list[Message] = field(default_factory=list)

    def render(self) -> str:
        """Render to the ``focus_structured`` text fed to the recognizer.

        Emits **XML** (Anthropic's recommended way to give a model structured
        context — explicit tag boundaries beat ad-hoc delimiters). Two distinct
        sections so unrelated conversations never blur into one::

            <screen_conversation app="feishu">
            <current_conversation name="沈砚舟">
            <message dir="received" sender="蓝蓝">…</message>
            <message dir="sent">…</message>
            </current_conversation>
            <other_conversations note="每条=不同对话的最新一条未读预览，非当前会话的连续消息">
            <preview sender="温子墨" time="11:27">晚上8点约一个会议…</preview>
            </other_conversations>
            </screen_conversation>

        ``<current_conversation name>`` carries ``thread_title`` so the model
        sees *which* conversation is open. ``<preview>`` entries are other
        chats' latest unread messages — not turns of the current thread — and
        carry no direction. All text/attributes are XML-escaped. Returns ``""``
        when there is nothing at all (the caller then falls back to the legacy
        normalized text).
        """
        if not self.messages and not self.previews:
            return ""

        lines: list[str] = [f"<screen_conversation app={_esc_attr(self.app)}>"]
        title = (self.thread_title or "").strip()

        if self.messages:
            open_tag = (
                f"<current_conversation name={_esc_attr(title)}>"
                if title
                else "<current_conversation>"
            )
            lines.append(open_tag)
            lines.extend(self._message_tag(m) for m in self.messages)
            lines.append("</current_conversation>")

        if self.previews:
            lines.append(
                '<other_conversations note="每条=不同对话的最新一条未读预览，非当前会话的连续消息">'
            )
            lines.extend(self._preview_tag(m) for m in self.previews)
            lines.append("</other_conversations>")

        lines.append("</screen_conversation>")
        return "\n".join(lines)

    @staticmethod
    def _attrs(pairs: list[tuple[str, str | None]]) -> str:
        """Render ``key="escaped-value"`` for each non-empty value, space-led."""
        out = ""
        for key, val in pairs:
            v = (val or "").strip()
            if v:
                out += f" {key}={_esc_attr(v)}"
        return out

    @classmethod
    def _message_tag(cls, msg: Message) -> str:
        """A current-conversation turn: ``<message dir=… sender=…>正文</message>``."""
        attrs = f' dir="{_DIRECTION_ATTR.get(msg.direction, "unknown")}"'
        attrs += cls._attrs([("sender", msg.sender), ("time", msg.timestamp_text)])
        return f"<message{attrs}>{_esc_text(msg.body.strip())}</message>"

    @classmethod
    def _preview_tag(cls, msg: Message) -> str:
        """Another conversation's latest message: ``<preview sender=… time=…>…</preview>``.

        No ``dir`` — previews are other chats' unread latest messages, not turns
        in the current thread.
        """
        attrs = cls._attrs([("sender", msg.sender), ("time", msg.timestamp_text)])
        return f"<preview{attrs}>{_esc_text(msg.body.strip())}</preview>"


class Parser(ABC):
    """Base class for per-app AX-tree parsers.

    A parser declares the ``bundle_ids`` it handles and a ``version`` string.
    ``parse`` returns ``None`` when the AX tree lacks the parser's anchors
    (wrong app, unrecognized layout) so the caller can fall back to the
    legacy timeline normalization path. A non-``None`` result is any
    :class:`StructuredContent` — a chat parser returns a
    :class:`ParsedConversation`, a browser parser a ``WebPage``.
    """

    bundle_ids: frozenset[str]
    version: str

    @abstractmethod
    def parse(self, ax_tree: dict, *, window_title: str | None) -> StructuredContent | None:
        """Parse ``ax_tree`` into a :class:`StructuredContent` or ``None``."""
        raise NotImplementedError

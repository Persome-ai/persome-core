"""Per-app message parsers.

Routes a capture's ``ax_tree`` to a deterministic, app-specific parser keyed by
``window_meta.bundle_id``. Each parser extracts structured
``[sender / time / body / direction]`` messages so downstream stages receive
faithful ``focus_structured`` text instead of relying only on lossy timeline
normalization.

Usage::

    from persome import parsers

    parser = parsers.get_parser(bundle_id)
    if parser is not None:
        conv = parser.parse(ax_tree, window_title=title)
        if conv is not None:
            structured = conv.render()

Parsers self-register at import time via ``register``; importing this package
imports the concrete parser modules so the registry is populated.
"""

from __future__ import annotations

from ..capture import browser_detect
from .base import Message, ParsedConversation, Parser, StructuredContent
from .feishu import FeishuParser
from .web import BrowserParser, WebItem, WebPage

# A registered parser only needs ``bundle_ids`` / ``version`` / ``parse`` (a
# structural protocol). ``Parser`` (the ABC chat parsers subclass) and
# ``BrowserParser`` (returns a ``WebPage``) both satisfy it.
_REGISTRY: dict[str, Parser] = {}


def register(parser: Parser) -> None:
    """Register ``parser`` for every bundle id it declares.

    A later registration for the same bundle id overrides an earlier one
    (last-write-wins), which keeps tests that swap parsers simple.
    """
    for bundle_id in parser.bundle_ids:
        _REGISTRY[bundle_id] = parser


def get_parser(bundle_id: str | None) -> Parser | None:
    """Return the parser handling ``bundle_id``, or ``None`` if unregistered."""
    if not bundle_id:
        return None
    return _REGISTRY.get(bundle_id)


def parser_for_capture(bundle_id: str | None, ax_tree: dict | None) -> Parser | None:
    """Dispatch a capture to its parser, generic-browser-aware.

    First the bundle-id registry (Feishu / known browsers).
    On a miss, if the capture is a browser surface per ``browser_detect`` (a
    registered http handler showing web content, or the AX-chrome fallback), use
    the generic ``BrowserParser`` — so a niche/unlisted browser like Tabbit is
    parsed as a web page WITHOUT being in any allowlist. Returns ``None`` for
    non-browser, unregistered apps (unchanged).
    """
    p = get_parser(bundle_id)
    if p is not None:
        return p
    if ax_tree and browser_detect.is_browser(ax_tree, bundle_id):
        return _BROWSER_PARSER
    return None


# Built-in parsers.
register(FeishuParser())
_BROWSER_PARSER = BrowserParser()
register(_BROWSER_PARSER)

__all__ = [
    "Message",
    "ParsedConversation",
    "Parser",
    "StructuredContent",
    "FeishuParser",
    "BrowserParser",
    "WebPage",
    "WebItem",
    "register",
    "get_parser",
    "parser_for_capture",
]

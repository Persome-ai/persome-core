"""Enrich capture JSON with structured S1 fields.

Downstream stages (timeline aggregator, session reducer, classifier) read
``focused_element`` / ``visible_text`` / ``url`` instead of re-parsing the
raw AX tree every time. Cutting the prompt size and giving the LLM a
consistent schema is the point.

Ported from Einsia-Partner's S1 extraction (``s1_collector`` —
``_extract_focused_element`` / ``_render_visible_text`` / ``_extract_url``).
Runs inline inside ``capture_once`` so every capture-buffer JSON carries
these fields.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from . import browser_detect, generic_render, placeholder
from .ax_models import ax_app_to_markdown, ax_tree_to_markdown

_URL_RE = re.compile(r"https?://\S+")

# Progressive disclosure (browsers): visible_text should be where the user is
# attending — the page (AXWebArea) — not the browser furniture. The chrome
# (bookmarks toolbar, the all-tabs switcher, extensions) is folded to a one-line
# digest; the full structure stays in ax_tree for on-demand access. Below this
# many chars of page content we fall open to the whole-window render (a blank /
# loading page shouldn't blank the capture).
_MIN_BROWSER_CONTENT = 40
_CHROME_COUNT_ROLES = {
    "AXButton": "buttons/bookmarks",
    "AXRadioButton": "tabs",
    "AXPopUpButton": "menus/expanders",
}

_EDITABLE_ROLES = {"AXTextField", "AXTextArea", "AXComboBox"}
_STATIC_ROLES = {"AXStaticText", "AXWebArea"}

_VISIBLE_TEXT_MAX = 10_000
_FOCUS_TITLE_MAX = 200
_FOCUS_VALUE_MAX = 2_000
_CMUX_TERMINAL_MARKER = "### [cmux terminal]"
_OCR_PLACEHOLDER_VALUES = "_persome_ocr_placeholder_values"


@dataclass
class FocusedElement:
    role: str = ""
    title: str = ""
    value: str = ""
    is_editable: bool = False
    has_value: bool = False
    value_length: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        stripped = (self.value or "").strip()
        d["has_value"] = bool(stripped)
        d["value_length"] = len(stripped)
        return d


def enrich(capture: dict[str, Any]) -> None:
    """Mutate ``capture`` in place: add ``focused_element`` / ``visible_text`` / ``url``.

    No-op when there is no ``ax_tree`` (e.g. AX unavailable, permission denied).
    """
    ax_tree = capture.get("ax_tree")
    if not isinstance(ax_tree, dict):
        return

    clean_ax_tree = placeholder.sanitize_ax_tree(ax_tree)
    original_app = _frontmost_app(ax_tree)
    trigger = capture.get("trigger")
    if isinstance(trigger, dict):
        clean_trigger = placeholder.sanitize_trigger(original_app or {}, trigger)
        if clean_trigger is not trigger:
            capture["trigger"] = clean_trigger
    app_data = _frontmost_app(clean_ax_tree)
    if app_data is None:
        capture["focused_element"] = FocusedElement().to_dict()
        capture["visible_text"] = ""
        capture["url"] = None
        return

    capture["focused_element"] = _extract_focused_element(app_data).to_dict()
    capture["visible_text"] = _render_visible_text(app_data, app_data.get("bundle_id") or "")
    capture["url"] = _extract_url(app_data)


def ocr_placeholder_values(capture: dict[str, Any]) -> tuple[str, ...]:
    """Return trusted placeholder evidence for this capture's OCR surface."""
    cached = capture.get(_OCR_PLACEHOLDER_VALUES)
    # JSON cannot encode tuples, so this type also distinguishes the internal
    # replay cache below from an untrusted key in a persisted/ingested record.
    if isinstance(cached, tuple) and all(isinstance(value, str) for value in cached):
        return cached
    return placeholder.confirmed_placeholder_values(capture.get("ax_tree"))


def sanitize_ocr_text(capture: dict[str, Any], text: str) -> str:
    """Filter OCR using only exact fields backed by this capture's raw AX."""
    return placeholder.sanitize_ocr_text(text, ocr_placeholder_values(capture))


def sanitize_capture(capture: dict[str, Any], *, replace_ax_tree: bool = False) -> dict[str, Any]:
    """Return a replay-safe capture without mutating the raw record.

    Historical buffer JSON and trusted-ingest producers may predate the native
    placeholder fix. Recompute their S1 projection from a sanitized AX tree so
    timeline replay, MCP reads, and capture-index rebuilds cannot resurrect an
    old placeholder as authored text. Already-clean captures are returned by
    identity.
    """
    ax_tree = capture.get("ax_tree")
    ocr_values = ocr_placeholder_values(capture) if replace_ax_tree else ()
    clean_ax_tree = placeholder.sanitize_ax_tree(ax_tree) if isinstance(ax_tree, dict) else ax_tree
    original_app = _frontmost_app(ax_tree) if isinstance(ax_tree, dict) else None
    trigger = capture.get("trigger")
    clean_trigger = (
        placeholder.sanitize_trigger(original_app or {}, trigger)
        if isinstance(trigger, dict)
        else trigger
    )
    projection_changed = clean_ax_tree is not ax_tree
    trigger_changed = clean_trigger is not trigger
    cache_changed = (
        replace_ax_tree and bool(ocr_values) and ocr_values != capture.get(_OCR_PLACEHOLDER_VALUES)
    )
    if not projection_changed and not trigger_changed and not cache_changed:
        return capture
    clean = dict(capture)
    if replace_ax_tree and projection_changed:
        clean["ax_tree"] = clean_ax_tree
    if replace_ax_tree and ocr_values:
        clean[_OCR_PLACEHOLDER_VALUES] = ocr_values
    if trigger_changed:
        clean["trigger"] = clean_trigger
    if not projection_changed:
        return clean
    app_data = _frontmost_app(clean_ax_tree)
    if app_data is None:
        clean["focused_element"] = FocusedElement().to_dict()
        clean["visible_text"] = ""
        clean["url"] = None
        return clean
    clean["focused_element"] = _extract_focused_element(app_data).to_dict()
    preserve_ocr_sentinel = bool(capture.get("ocr_submitted")) and not str(
        capture.get("visible_text") or ""
    )
    visible_text = (
        ""
        if preserve_ocr_sentinel
        else _render_visible_text(app_data, app_data.get("bundle_id") or "")
    )
    # cmux terminal text is injected *after* S1 enrichment because its GPU
    # surface is absent from AX. Historical replay must preserve that suffix
    # while recomputing only the AX-derived prefix.
    if capture.get("cmux_text_injected"):
        prior = str(capture.get("visible_text") or "")
        marker_at = prior.find(_CMUX_TERMINAL_MARKER)
        if marker_at >= 0:
            suffix = prior[marker_at:].strip()
            visible_text = f"{visible_text.rstrip()}\n\n{suffix}" if visible_text else suffix
    clean["visible_text"] = visible_text
    clean["url"] = _extract_url(app_data)
    return clean


def _frontmost_app(ax_tree: dict[str, Any]) -> dict[str, Any] | None:
    apps: list[dict[str, Any]] = ax_tree.get("apps") or []
    for app in apps:
        if app.get("is_frontmost"):
            return app
    return apps[0] if apps else None


def _extract_focused_element(app_data: dict[str, Any]) -> FocusedElement:
    # Prefer the OS-reported focused element (AXFocusedUIElement) the AX helper
    # now emits on the app dict — the actual keyboard/caret focus. The legacy
    # scan below only inspects the focused window's DIRECT children for a couple
    # of roles, so it misses browsers/editors (their focused control nests deep

    fe = app_data.get("focused_element")
    if isinstance(fe, dict) and (fe.get("role") or ""):
        return FocusedElement(
            role=fe.get("role") or "",
            title=(fe.get("title") or fe.get("description") or "")[:_FOCUS_TITLE_MAX],
            value=(fe.get("value") or "")[:_FOCUS_VALUE_MAX],
            is_editable=bool(fe.get("is_editable")),
        )
    for window in app_data.get("windows", []):
        if not window.get("focused"):
            continue
        for el in window.get("elements", []):
            role = el.get("role", "") or ""
            if role in _EDITABLE_ROLES:
                return FocusedElement(
                    role=role,
                    title=(el.get("title") or "")[:_FOCUS_TITLE_MAX],
                    value=(el.get("value") or "")[:_FOCUS_VALUE_MAX],
                    is_editable=True,
                )
            if role in _STATIC_ROLES:
                return FocusedElement(
                    role=role,
                    title=(el.get("title") or "")[:_FOCUS_TITLE_MAX],
                    value=(el.get("value") or el.get("title") or "")[:_FOCUS_VALUE_MAX],
                    is_editable=False,
                )
    return FocusedElement()


def _collect_web_areas(elements: list[dict[str, Any]], out: list[dict[str, Any]]) -> None:
    """Append each AXWebArea subtree (taken whole — it IS the page region)."""
    for el in elements or []:
        if not isinstance(el, dict):
            continue
        if (el.get("role") or "") == "AXWebArea":
            out.append(el)
        else:
            _collect_web_areas(el.get("children") or [], out)


def _chrome_digest(app_data: dict[str, Any], web_areas: list[dict[str, Any]]) -> str:
    """One-line, counted annotation of the browser chrome that was folded out of
    visible_text (progressive disclosure: the full chrome stays in ax_tree)."""
    web_ids = {id(w) for w in web_areas}
    counts: dict[str, int] = {}

    def walk(els: list[dict[str, Any]]) -> None:
        for el in els or []:
            if not isinstance(el, dict) or id(el) in web_ids:
                continue  # don't count anything inside the page region
            role = el.get("role") or ""
            if role in _CHROME_COUNT_ROLES:
                counts[role] = counts.get(role, 0) + 1
            walk(el.get("children") or [])

    for win in app_data.get("windows", []) or []:
        walk(win.get("elements") or [])
    parts = [f"{counts[r]} {label}" for r, label in _CHROME_COUNT_ROLES.items() if counts.get(r)]
    if not parts:
        return ""
    return "[browser chrome folded: " + " · ".join(parts) + " · full structure in ax_tree]"


def _render_browser_content(app_data: dict[str, Any]) -> str | None:
    """Render visible_text as the attended page (AXWebArea) + a folded chrome
    digest, instead of the chrome-heavy whole-window dump. Returns None to fall
    open to the normal render (no web area, or content too thin)."""
    web_areas: list[dict[str, Any]] = []
    for win in app_data.get("windows", []) or []:
        _collect_web_areas(win.get("elements") or [], web_areas)
    if not web_areas:
        return None
    title = next(
        (str(w.get("title")) for w in web_areas if w.get("title")),
        ((app_data.get("windows") or [{}])[0].get("title") or ""),
    )
    md = ax_tree_to_markdown(
        {
            "apps": [
                {
                    "name": app_data.get("name", "Unknown"),
                    "is_frontmost": app_data.get("is_frontmost"),
                    "bundle_id": app_data.get("bundle_id", ""),
                    "windows": [{"title": title, "elements": web_areas}],
                }
            ]
        }
    )
    if len(md.strip()) < _MIN_BROWSER_CONTENT:
        return None
    digest = _chrome_digest(app_data, web_areas)
    if digest:
        # Insert the chrome annotation right after the `### <title>` header so it
        # survives truncation (the page body gets the rest of the budget).
        lines = md.split("\n")
        for i, ln in enumerate(lines):
            if ln.startswith("### "):
                lines.insert(i + 1, digest)
                break
        else:
            lines.insert(0, digest)
        md = "\n".join(lines)
    return md


def _render_chat_content(app_data: dict[str, Any], bundle: str) -> str | None:
    try:
        from ..parsers import parser_for_capture
        from ..parsers.base import ParsedConversation
    except Exception:
        return None
    ax_tree = {"apps": [app_data]}
    parser = parser_for_capture(bundle, ax_tree)
    if parser is None:
        return None
    try:
        title = (app_data.get("windows") or [{}])[0].get("title")
        parsed = parser.parse(ax_tree, window_title=title)
    except Exception:
        return None
    if isinstance(parsed, ParsedConversation):
        rendered = parsed.render()
        return rendered or None
    return None


def _render_visible_text(app_data: dict[str, Any], bundle: str = "") -> str:
    # Browsers: show the page the user is attending to, fold chrome to a digest
    # (progressive disclosure). Scoped via browser_detect so cmux/Feishu/WeChat —
    # whose visible_text carries injected/parser-relevant text — are untouched.
    md: str | None = None
    if browser_detect.is_browser(app_data, bundle):
        md = _render_browser_content(app_data)
    # Chat/IM apps: render WITH direction + sender (Feishu etc.), so visible_text doesn't lose
    # who-said-what (a flat dump made the LLM attribute the user's own messages to the counterpart).
    if md is None:
        md = _render_chat_content(app_data, bundle)
    if md is None:
        # Generic clean resolver for the long tail (chrome fold + container
        # collapse + repeated-row flatten + role-label suppression); fail-open
        # to the mechanical render when it yields too little.
        md = generic_render.resolve_app(app_data)
    if md is None:
        md = ax_app_to_markdown(app_data)
    if len(md) > _VISIBLE_TEXT_MAX:
        md = md[:_VISIBLE_TEXT_MAX] + "\n...(truncated)"
    return md


def _extract_url(app_data: dict[str, Any]) -> str | None:
    """Address-bar URL of the frontmost surface, when it's a browser.

    Browser detection is now generic (``browser_detect``): a registered
    http(s) handler (Tabbit/Arc/any) or — when LaunchServices is unavailable —
    an AX surface with browser chrome. No hardcoded bundle allowlist.
    """
    bundle = app_data.get("bundle_id", "")
    if not (
        browser_detect.is_browser_app(bundle) or browser_detect.looks_like_browser(app_data, bundle)
    ):
        return None
    return browser_detect.address_bar_url(app_data, bundle)

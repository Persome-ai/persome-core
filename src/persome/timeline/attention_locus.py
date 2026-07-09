"""Attention locus — the first-class "where is the user attending" object.

Step 1 of the attention-locus design (`docs/superpowers/specs/2026-06-18-
attention-locus-design.md`). The locus is resolved **by code** from one
capture's structured signals (the cursor hit-test, the focused element, the
window/pane), and the timeline aggregator feeds *its content* to the LLM in
place of the raw screen dump. Code owns "where attention was"; the LLM only
phrases "what is there".

Resolution is a **registry**: a generic fusion ladder plus per-app resolvers
added one at a time. Everything is a pure function of the capture dict + the
already-resolved ``visible_text`` (so OCR backfill is honored) — no I/O, never
raises, never returns an empty/None locus (fail-open to a surface-level locus).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..capture.ax_models import ax_tree_to_markdown

# Max chars of an element's value rendered into a click anchor / region label.
_CLICK_VALUE_LIMIT = 200

# Confidence assigned to each fusion-ladder rung (spec §"fusion ladder").
_CONF_EDITING = 0.9
_CONF_CURSOR = 0.7
_CONF_FOCUS = 0.6
_CONF_PANE = 0.8
_CONF_CONTENT = 0.5  # structural content-subtree narrowing (no focus signal)
_CONF_FALLBACK = 0.0

# Structural narrowing (路2). chrome and content are SEPARATE subtrees in the AX
# tree, so we can drop chrome app-agnostically — no per-app text heuristics.
# Web/Electron apps (browsers, Feishu) expose their rendered content as an
# AXWebArea subtree; we take that and drop everything else. Apps without a
# WebArea fall back to pruning known chrome-role subtrees. The structured
# ``ax_tree`` is already on every capture, so this is daemon-only.
_CONTENT_ROLES: frozenset[str] = frozenset({"AXWebArea"})
_CHROME_ROLES: frozenset[str] = frozenset(
    {
        "AXToolbar",
        "AXTabGroup",
        "AXScrollBar",
        "AXScrollArea",  # often wraps chrome rails; webarea is matched first anyway
        "AXMenuBar",
        "AXMenuButton",
    }
)
# Below this many chars the narrowed content is treated as "too thin" — fall
# open to the whole window rather than feed the LLM a near-empty region.
_MIN_CONTENT_CHARS = 40


def _collect_role_subtrees(elements: list, target: frozenset[str], out: list) -> None:
    """Append every element whose role is in *target*. A matched node is taken
    whole (we do NOT descend into it — it IS the region)."""
    for el in elements or []:
        if not isinstance(el, dict):
            continue
        if str(el.get("role") or "") in target:
            out.append(el)
        else:
            _collect_role_subtrees(el.get("children") or [], target, out)


def _prune_chrome(elements: list) -> tuple[list, bool]:
    """Return (elements with chrome-role subtrees removed, dropped_any)."""
    kept: list = []
    dropped = False
    for el in elements or []:
        if not isinstance(el, dict):
            continue
        if str(el.get("role") or "") in _CHROME_ROLES:
            dropped = True
            continue
        children, child_dropped = _prune_chrome(el.get("children") or [])
        dropped = dropped or child_dropped
        clone = dict(el)
        clone["children"] = children
        kept.append(clone)
    return kept, dropped


def _window_elements(ax: dict) -> tuple[list, str]:
    """Flatten apps→windows into one element list + a window title."""
    elements: list = []
    title = ""
    for app in ax.get("apps", []) or []:
        for win in app.get("windows", []) or []:
            elements.extend(win.get("elements") or [])
            if not title:
                title = str(win.get("title") or "")
    return elements, title


def _render(elements: list, title: str) -> str:
    return ax_tree_to_markdown(
        {"apps": [{"windows": [{"title": title, "elements": elements}]}]}
    ).strip()


def structural_content(data: dict) -> tuple[str, bool]:
    """Extract the content region from the capture's structured ``ax_tree``,
    dropping chrome — app-agnostically (路2). Returns (text, narrowed).

    Prefers AXWebArea subtrees (web/Electron content). Falls back to pruning
    chrome-role subtrees. Returns ``("", False)`` when there is no ax_tree, no
    content region, nothing chrome was actually dropped, or the result is too
    thin — so the caller keeps the whole window (fail-open).
    """
    ax = data.get("ax_tree")
    if not isinstance(ax, dict):
        return "", False
    elements, title = _window_elements(ax)
    if not elements:
        return "", False

    content_nodes: list = []
    _collect_role_subtrees(elements, _CONTENT_ROLES, content_nodes)
    if content_nodes:
        text = _render(content_nodes, title)
        return (text, True) if len(text) >= _MIN_CONTENT_CHARS else ("", False)

    pruned, dropped = _prune_chrome(elements)
    if dropped:
        text = _render(pruned, title)
        return (text, True) if len(text) >= _MIN_CONTENT_CHARS else ("", False)
    return "", False


@dataclass(frozen=True)
class AttentionLocus:
    """Where the user is attending, for one capture moment.

    ``content`` is the text payload fed to the LLM. ``peripheral`` is an
    optional secondary region the user is referencing while acting on the
    primary (e.g. reading a doc while typing). ``confidence`` below the
    surface-level threshold means we could not localize finer than the window.
    """

    surface: str = ""  # window / pane / tab / conversation label
    region: str = ""  # the element/span attended within the surface ("" = surface-level)
    content: str = ""  # text payload at the region — this is what the LLM sees
    peripheral: str = ""  # secondary referenced region's content, optional
    confidence: float = 0.0  # 0..1; < threshold ⇒ surface-level fallback
    rung: str = "fallback"  # which ladder rung won (telemetry / tests)


# ---------------------------------------------------------------------------
# Structural focus markers our OWN capture layer inserts to delimit the focused
# region inside an otherwise chrome-heavy window (cmux_source appends the real
# terminal surface after this marker, below the AX workspace/tab sidebar).
# ---------------------------------------------------------------------------
_FOCUS_PANE_MARKERS: tuple[str, ...] = ("### [cmux terminal]",)


def focus_pane(visible_text: str) -> tuple[str, bool]:
    """If a structural focus marker is present, return (region after it, True);
    otherwise (visible_text unchanged, False). The leading chrome is dropped."""
    for marker in _FOCUS_PANE_MARKERS:
        idx = visible_text.find(marker)
        if idx >= 0:
            return visible_text[idx + len(marker) :].strip(), True
    return visible_text, False


def _click_element(trigger: dict) -> dict:
    """The AX element the watcher hit-tested under the cursor, or {}."""
    details = trigger.get("details") or {}
    el = details.get("element")
    return el if isinstance(el, dict) else {}


def click_anchor(trigger: dict) -> str:
    """Render the hit-tested element under the cursor as a one-line anchor —
    the "what did the user point at" signal. ``""`` when there's no target.

    Coordinates (``details.x``/``y``) are intentionally NOT rendered: the OS
    hit-test already resolved them to this element, and raw pixels carry no
    meaning for the normalizer. The geometry stays on the capture for any
    future spatial use.
    """
    el = _click_element(trigger)
    role = str(el.get("role") or "").strip()
    title = str(el.get("title") or "").strip()
    value = str(el.get("value") or "").strip()
    if not (role or title or value):
        return ""
    seg = "clicked"
    if role:
        seg += f" [{role}]"
    if title:
        seg += f" title={title[:80]}"
    if value:
        seg += f': "{value[:_CLICK_VALUE_LIMIT]}"'
    return f"(attention: {seg})"


def _element_text(el: dict) -> str:
    """Best short label for an AX element: value, else title, else role."""
    for key in ("value", "title", "role"):
        v = str(el.get(key) or "").strip()
        if v:
            return v[:_CLICK_VALUE_LIMIT]
    return ""


Resolver = Callable[[dict, str, str], "AttentionLocus | None"]


def _generic_resolver(data: dict, surface: str, visible_text: str) -> AttentionLocus:
    """The fusion ladder for any app without a dedicated resolver.

    Picks the strongest *localization* signal for ``region``/``rung``/
    ``confidence`` (editing > cursor > focus > none), but keeps ``content`` =
    the whole ``visible_text`` (the slicer anchors it downstream) — so a
    resolver-less app is NEVER narrowed, only annotated. This is the fail-open
    parity the design requires: narrowing is opt-in per app, not the default.
    """
    fe = data.get("focused_element") or {}
    trigger = data.get("trigger") or {}
    click_el = _click_element(trigger)

    editing_value = ""
    if fe.get("is_editable"):
        editing_value = str(fe.get("value") or "").strip()
    cursor_text = _element_text(click_el)
    focus_text = _element_text(fe)

    # Rung selection. content stays whole visible_text (safe); region/rung/
    # confidence reflect the localization signal.
    if editing_value:
        region = f"editing:{str(fe.get('title') or fe.get('role') or '').strip()}".rstrip(":")
        # Divergence: cursor pointing at a DIFFERENT element while typing →
        # the cursor target is the peripheral (acting on X, referencing Y).
        peripheral = cursor_text if cursor_text and cursor_text != editing_value else ""
        return AttentionLocus(
            surface=surface,
            region=region,
            content=visible_text,
            peripheral=peripheral,
            confidence=_CONF_EDITING,
            rung="editing",
        )
    if cursor_text:
        return AttentionLocus(
            surface=surface,
            region=f"cursor:{str(click_el.get('role') or '').strip()}".rstrip(":"),
            content=visible_text,
            confidence=_CONF_CURSOR,
            rung="cursor",
        )
    if focus_text:
        return AttentionLocus(
            surface=surface,
            region=f"focus:{str(fe.get('role') or '').strip()}".rstrip(":"),
            content=visible_text,
            confidence=_CONF_FOCUS,
            rung="focus",
        )
    # No focus signal (the fallback majority — browsers, passive Electron views).
    # Structurally narrow to the content subtree (路2), dropping chrome, instead
    # of feeding the whole window. Fail-open to whole window when extraction is
    # not confident.
    narrowed, did_narrow = structural_content(data)
    if did_narrow:
        return AttentionLocus(
            surface=surface,
            region="content",
            content=narrowed,
            confidence=_CONF_CONTENT,
            rung="content",
        )
    return AttentionLocus(
        surface=surface,
        region="",
        content=visible_text,
        confidence=_CONF_FALLBACK,
        rung="fallback",
    )


def _cmux_resolver(data: dict, surface: str, visible_text: str) -> AttentionLocus:
    """cmux is GPU-rendered (no AX content elements); the real activity is the
    injected terminal pane after the marker. Narrow ``content`` to that pane,
    dropping the workspace/tab chrome. Falls through to the generic ladder when
    the injection didn't land (marker absent)."""
    pane, focused = focus_pane(visible_text)
    if focused:
        return AttentionLocus(
            surface=surface,
            region="terminal-pane",
            content=pane,
            confidence=_CONF_PANE,
            rung="pane",
        )
    return _generic_resolver(data, surface, visible_text)


# Registry keyed by bundle_id. Add an app here to give it a dedicated resolver;
# the prompt never changes. Everything else uses the generic ladder.
LOCUS_RESOLVERS: dict[str, Resolver] = {
    "com.cmuxterm.app": _cmux_resolver,
}


def resolve_locus(data: dict, *, visible_text: str) -> AttentionLocus:
    """Resolve the attention locus for one capture. Pure, fail-open.

    ``visible_text`` is passed in (already resolved by the aggregator, incl. AX
    fallback + OCR backfill) so the locus reflects what the LLM would actually
    read. Any resolver miss/exception ⇒ a surface-level fallback locus.
    """
    wm = data.get("window_meta") or {}
    bundle = str(wm.get("bundle_id") or "")
    surface = str(wm.get("title") or wm.get("app_name") or "")
    resolver = LOCUS_RESOLVERS.get(bundle, _generic_resolver)
    try:
        loc = resolver(data, surface, visible_text)
    except Exception:  # noqa: BLE001 — never let resolution break aggregation
        loc = None
    if loc is None:
        loc = AttentionLocus(
            surface=surface, content=visible_text, confidence=_CONF_FALLBACK, rung="fallback"
        )
    return loc

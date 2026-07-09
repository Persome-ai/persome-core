"""Pure helpers for navigating a captured AX tree.

The capture schema (see ``capture/`` and the lark fixtures) is::

    ax_tree = {
        "apps": [
            {
                "bundle_id": str,
                "is_frontmost": bool,
                "name": str,
                "windows": [
                    {"title", "focused", "subrole", "elements": [node, ...]},
                    ...,
                ],
            },
            ...,
        ],
        "timestamp": str,
    }

    node = {
        "role": str,            # "AXStaticText", "AXGroup", ...
        "value": str | None,
        "subrole": str | None,
        "title": str | None,
        "description": str | None,
        "domClassList": list[str],
        "children": list[node],
    }

Everything here is a side-effect-free function on plain dicts so parsers stay
trivially testable without any capture machinery.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

Node = dict
Pred = Callable[[Node], bool]


def frontmost_window_elements(ax_tree: dict, bundle_id: str) -> list[Node]:
    """Return the element list of the most relevant window for ``bundle_id``.

    Picks the app whose ``bundle_id`` matches, then its focused window
    (``focused is True``) or, failing that, its first window. Returns an empty
    list when no app matches or the app has no windows / elements — callers
    treat an empty list as "nothing to parse".
    """
    for app in ax_tree.get("apps") or []:
        if not isinstance(app, dict) or app.get("bundle_id") != bundle_id:
            continue
        windows = app.get("windows") or []
        if not windows:
            return []
        focused = next(
            (w for w in windows if isinstance(w, dict) and w.get("focused")),
            None,
        )
        window = focused or windows[0]
        if not isinstance(window, dict):
            return []
        elements = window.get("elements") or []
        return [n for n in elements if isinstance(n, dict)]
    return []


def frontmost_app(ax_tree: dict, bundle_ids: frozenset[str] | None = None) -> Node | None:
    """The app to inspect: the frontmost (else first). When ``bundle_ids`` is
    given, a frontmost app in that set is preferred, but selection never *fails*
    on a bundle miss — the caller (parser dispatch) has already decided this
    capture is a browser, so the frontmost surface is what we parse regardless
    of whether its bundle id is in any allowlist (lets a niche/unlisted browser
    like Tabbit parse)."""
    if not isinstance(ax_tree, dict):
        return None
    apps = [a for a in (ax_tree.get("apps") or []) if isinstance(a, dict)]
    if not apps:
        return None
    if bundle_ids:
        for a in apps:
            if a.get("bundle_id") in bundle_ids and a.get("is_frontmost"):
                return a
    front = next((a for a in apps if a.get("is_frontmost")), None)
    return front or apps[0]


def frontmost_web_area(ax_tree: dict, bundle_ids: frozenset[str] | None = None) -> Node | None:
    """Return the first ``AXWebArea`` node of the frontmost browser surface.

    Picks the frontmost app (see :func:`frontmost_app` — bundle-agnostic so an
    unlisted browser still parses), then its focused window (or first window),
    then the first ``AXWebArea`` found in that window's element tree. Returns
    ``None`` when no window/web-area exists — callers treat that as "not a
    parseable browser page".

    The ``AXWebArea`` is the root of the rendered page (its ``title`` is the
    page identity, e.g. ``Issues · owner/repo``). Browser chrome (tabs, address
    bar) lives in sibling nodes outside it, so scoping to the web area already
    excludes the native toolbar.
    """
    app = frontmost_app(ax_tree, bundle_ids)
    if app is None:
        return None

    windows = app.get("windows") or []
    if not windows:
        return None
    focused = next(
        (w for w in windows if isinstance(w, dict) and w.get("focused")),
        None,
    )
    window = focused or windows[0]
    if not isinstance(window, dict):
        return None

    for element in window.get("elements") or []:
        if not isinstance(element, dict):
            continue
        for node in walk(element):
            if node.get("role") == "AXWebArea":
                return node
    return None


def walk(node: Node) -> Iterator[Node]:
    """Depth-first pre-order traversal yielding ``node`` then its descendants."""
    if not isinstance(node, dict):
        return
    yield node
    for child in node.get("children") or []:
        if isinstance(child, dict):
            yield from walk(child)


def has_class(node: Node, name: str) -> bool:
    """True when ``node`` carries ``name`` in its ``domClassList``."""
    return name in (node.get("domClassList") or [])


def text_of(node: Node) -> str | None:
    """The node's text value (``value`` field), or ``None``."""
    return node.get("value")


def find_all(
    root: Node,
    *,
    role: str | None = None,
    dom_class: str | None = None,
    pred: Pred | None = None,
) -> list[Node]:
    """Collect every descendant of ``root`` (inclusive) matching all filters.

    Filters are ANDed: ``role`` matches ``node["role"]``, ``dom_class`` matches
    membership in ``domClassList``, and ``pred`` is an arbitrary predicate.
    With no filters this returns the whole subtree in DFS order.
    """
    out: list[Node] = []
    for node in walk(root):
        if role is not None and node.get("role") != role:
            continue
        if dom_class is not None and not has_class(node, dom_class):
            continue
        if pred is not None and not pred(node):
            continue
        out.append(node)
    return out


def prune_subtrees(root: Node, pred: Pred) -> Node:
    """Return a deep copy of ``root`` with every subtree whose root matches
    ``pred`` removed.

    The match is evaluated top-down: once a node matches it is dropped whole
    (its children are not inspected separately). ``root`` itself is never
    dropped — callers prune *within* a chosen container. The original tree is
    left untouched.
    """

    def _copy(node: Node) -> Node:
        clone = dict(node)
        kept: list[Node] = []
        for child in node.get("children") or []:
            if not isinstance(child, dict):
                continue
            if pred(child):
                continue
            kept.append(_copy(child))
        clone["children"] = kept
        return clone

    return _copy(root)

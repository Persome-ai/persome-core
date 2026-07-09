"""Generic, bundle-agnostic AX → clean text resolver.

The mechanical ``ax_models.ax_app_to_markdown`` renders the raw role tree
verbatim — hundreds of ``[Button]/[Link]/[StaticText]`` scaffolding tokens,
chrome, and empty container chains. This resolver lifts the floor to "clean
enough" for any app, using only **standard AX roles + generic structural
patterns** (no per-app code, no LLM). Design + measured prototype:
``docs/superpowers/specs/2026-06-19-generic-ax-resolver-design.md``.

Levers, in order:
  1. **Chrome fold** — drop AXToolbar/AXTabGroup/AXMenuBar/AXMenuButton/AXScrollBar
     subtrees; emit a counted one-line digest instead.
  2. **Container collapse** — skip semantic-free containers (AXGroup/AXScrollArea/
     AXSplitGroup with no own text), promote their children.
  3. **Repeated-structure flatten** — ≥``_REPEAT_MIN`` sibling subtrees sharing a
     structural shape collapse to one flattened line each (the row's descendant
     text joined), instead of recursing into per-cell scaffolding. This is the
     "a list of similar things" signature (feeds / tab lists / message lists).
  4. **Inline-role suppression** — content roles (StaticText/Link/Heading/Cell)
     render as bare text; only structural/actionable roles keep a ``[Role]`` label.

Fail-open: returns ``None`` when the result is below a min-content floor, so the
caller keeps the fuller ``ax_app_to_markdown`` (never blank a capture). Pure,
deterministic, never raises.
"""

from __future__ import annotations

import collections
from typing import Any

# Folded out of the body, counted into the digest.
_CHROME_ROLES = frozenset(
    {"AXToolbar", "AXTabGroup", "AXMenuBar", "AXMenuButton", "AXScrollBar"}
)
# Semantic-free containers: skipped (children promoted) when they carry no text.
_CONTAINER_ROLES = frozenset({"AXGroup", "AXScrollArea", "AXSplitGroup", "AXSplitter"})
# Content roles: render as bare text, no [Role] scaffolding label.
_CONTENT_ROLES = frozenset(
    {"AXStaticText", "AXLink", "AXHeading", "AXCell", "AXRow", "AXText"}
)
# Roles counted in the chrome digest, with a human label.
_CHROME_COUNT = {
    "AXButton": "按钮",
    "AXRadioButton": "标签/选项",
    "AXPopUpButton": "菜单/扩展",
    "AXCheckBox": "勾选",
}

_REPEAT_MIN = 3  # ≥ this many same-shape siblings → flatten as a list
_MAX_LINE_CHARS = 500
_MIN_CONTENT_CHARS = 40  # below this, fall open to the full render
_MAX_FLATTEN_NODES = 200  # bound the per-row flatten walk


def _node_text(el: dict[str, Any]) -> str:
    title = (el.get("title") or "").strip()
    value = (el.get("value") or "").strip()
    if value and value != title:
        return f"{title} — {value}".strip(" —") if title else value
    return title


def _shape(el: dict[str, Any]) -> tuple:
    """Structural signature: role + the first few child roles. Same shape ⇒ a row."""
    kids = [c for c in (el.get("children") or []) if isinstance(c, dict)]
    return (el.get("role"), tuple(c.get("role") for c in kids[:6]))


def _flatten_text(el: dict[str, Any]) -> str:
    """Join all descendant text of a subtree into one line (drops scaffolding,
    keeps content) — used to render a repeated row compactly."""
    parts: list[str] = []
    stack = [el]
    seen = 0
    while stack and seen < _MAX_FLATTEN_NODES:
        cur = stack.pop()
        seen += 1
        if not isinstance(cur, dict):
            continue
        t = _node_text(cur)
        if t:
            parts.append(t)
        kids = cur.get("children") or []
        # preserve document order under the pop-stack
        stack.extend(reversed([c for c in kids if isinstance(c, dict)]))
    line = " · ".join(dict.fromkeys(p for p in parts if p))  # de-dup, keep order
    return line[:_MAX_LINE_CHARS]


def _walk(elements: list[dict[str, Any]], out: list[str], digest: dict[str, int], depth: int) -> None:
    indent = "  " * min(depth, 8)
    shapes = collections.Counter(_shape(e) for e in elements if isinstance(e, dict))
    for el in elements or []:
        if not isinstance(el, dict):
            continue
        role = el.get("role") or ""
        if role in _CHROME_ROLES:
            # fold: count actionable descendants into the digest, emit nothing here
            _tally_chrome(el, digest)
            continue
        if role in _CHROME_COUNT:
            # A button/tab/menu is an AFFORDANCE (something the user *could* do),
            # not content or a to-do. Emitting its label as a line makes the LLM
            # hallucinate intents from nav (measured: "记笔记/收藏/订阅/设置" read
            # as to-dos). Fold into the digest only; the count signals "N buttons
            # available" without polluting the content stream.
            digest[role] = digest.get(role, 0) + 1
            continue
        kids = el.get("children") or []
        # repeated row → one flattened line, no scaffolding recursion
        if shapes[_shape(el)] >= _REPEAT_MIN:
            line = _flatten_text(el)
            if line:
                out.append(f"{indent}- {line}")
            continue
        txt = _node_text(el)
        if txt:
            label = "" if role in _CONTENT_ROLES or role in _CONTAINER_ROLES else f"[{role.replace('AX', '')}] "
            # No per-line cap on a real content node (long paragraph / code) —
            # the caller's global budget bounds it; only repeated *rows* are
            # capped (above), where a long line would be scaffolding noise.
            out.append(f"{indent}- {label}{txt}")
            if kids:
                _walk(kids, out, digest, depth + 1)
        elif kids:
            _walk(kids, out, digest, depth + 1)  # collapse empty container


def _tally_chrome(el: dict[str, Any], digest: dict[str, int]) -> None:
    stack = [el]
    n = 0
    while stack and n < 2000:
        cur = stack.pop()
        n += 1
        if not isinstance(cur, dict):
            continue
        r = cur.get("role") or ""
        if r in _CHROME_COUNT:
            digest[r] = digest.get(r, 0) + 1
        stack.extend(cur.get("children") or [])


def _digest_line(digest: dict[str, int]) -> str:
    parts = [f"{digest[r]} {label}" for r, label in _CHROME_COUNT.items() if digest.get(r)]
    return ("[外壳已折叠：" + " · ".join(parts) + " · 完整结构见 ax_tree]") if parts else ""


def resolve_app(app_data: dict[str, Any]) -> str | None:
    """Render one frontmost-app dict to clean markdown, or ``None`` (too thin →
    caller falls open to the full ``ax_app_to_markdown``)."""
    if not isinstance(app_data, dict):
        return None
    name = app_data.get("name", "Unknown")
    badge = " [active]" if app_data.get("is_frontmost") else ""
    bundle = app_data.get("bundle_id", "")
    header = [f"## {name}{badge}"]
    if bundle:
        header.append(f"_{bundle}_")
    body: list[str] = []
    digest: dict[str, int] = {}
    for win in app_data.get("windows", []) or []:
        title = (win.get("title") or "").strip()
        if title:
            body.append(f"### {title}")
        _walk(win.get("elements") or [], body, digest, 0)
    content = "\n".join(body).strip()
    if len(content) < _MIN_CONTENT_CHARS:
        return None  # fail-open
    dline = _digest_line(digest)
    parts = header[:]
    # digest right after the header so it survives truncation
    if dline:
        parts.append(dline)
    parts.append(content)
    return "\n".join(parts)

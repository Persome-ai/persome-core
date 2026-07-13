"""Remove accessibility placeholders from authored-text signals.

Chromium can expose an empty editable control's placeholder as ``AXValue``.
The same control also exposes a descendant whose exact DOM class token is
``placeholder``.  Treat that local pairing as UI chrome, not user-authored
text.  The sanitizer is deliberately structural: it never hard-codes product
copy and never matches broad CSS class substrings.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Collection
from typing import Any

EDITABLE_ROLES = frozenset({"AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"})

_PLACEHOLDER_CLASS = "placeholder"
_PLACEHOLDER_VALUE_KEYS = (
    "AXPlaceholderValue",
    "placeholder_value",
    "placeholderValue",
)
_TEXT_KEYS = ("value", "title", "description")
_MAX_PLACEHOLDER_SCAN_NODES = 256
_MAX_PLACEHOLDER_SCAN_DEPTH = 12
_MAX_PLACEHOLDER_TREE_NODES = 100_000

_MARKDOWN_LIST_FIELD = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?P<value>.*)$")
_MARKDOWN_TAGGED_FIELD = re.compile(r"^\[[^]\r\n]{1,40}\]\s+(?P<value>.*)$")
_MARKDOWN_NAMED_FIELD = re.compile(r"^(?P<label>\s*#{1,6}\s+[^:\r\n]{1,120}:)\s*(?P<value>.*)$")


def _normalized(value: Any) -> str:
    return str(value or "").strip()


def _class_tokens(element: dict[str, Any]) -> tuple[str, ...]:
    raw = element.get("domClassList")
    if isinstance(raw, str):
        values = raw.split()
    elif isinstance(raw, list):
        values = [value for value in raw if isinstance(value, str)]
    else:
        values = []
    return tuple(value.strip().casefold() for value in values if value.strip())


def _is_placeholder_node(element: dict[str, Any]) -> bool:
    # Exact token only. Tailwind and other CSS utilities legitimately contain
    # substrings such as ``placeholder:text-token-…`` and must stay visible.
    return _PLACEHOLDER_CLASS in _class_tokens(element)


def _direct_placeholder_values(element: dict[str, Any]) -> set[str]:
    return {
        normalized
        for key in _PLACEHOLDER_VALUE_KEYS
        if (normalized := _normalized(element.get(key)))
    }


def _subtree_text(element: dict[str, Any]) -> set[str]:
    """Collect bounded text from one already-identified placeholder subtree."""
    values: set[str] = set()
    stack: list[tuple[dict[str, Any], int]] = [(element, 0)]
    seen = 0
    while stack and seen < _MAX_PLACEHOLDER_SCAN_NODES:
        current, depth = stack.pop()
        seen += 1
        if depth > 0 and str(current.get("role") or "") in EDITABLE_ROLES:
            continue
        for key in _TEXT_KEYS:
            if text := _normalized(current.get(key)):
                values.add(text)
        if depth >= _MAX_PLACEHOLDER_SCAN_DEPTH:
            continue
        stack.extend(
            (child, depth + 1)
            for child in reversed(current.get("children") or [])
            if isinstance(child, dict)
        )
    return values


def _local_placeholder_values(element: dict[str, Any]) -> set[str]:
    """Return placeholder text found inside this editable control only."""
    values = _direct_placeholder_values(element)
    stack: list[tuple[dict[str, Any], int]] = [
        (child, 1) for child in reversed(element.get("children") or []) if isinstance(child, dict)
    ]
    seen = 0
    while stack and seen < _MAX_PLACEHOLDER_SCAN_NODES:
        current, depth = stack.pop()
        seen += 1
        if str(current.get("role") or "") in EDITABLE_ROLES:
            continue
        if _is_placeholder_node(current):
            values.update(_subtree_text(current))
            continue
        if depth >= _MAX_PLACEHOLDER_SCAN_DEPTH:
            continue
        stack.extend(
            (child, depth + 1)
            for child in reversed(current.get("children") or [])
            if isinstance(child, dict)
        )
    return values


def _confirmed_placeholder_values(element: dict[str, Any]) -> set[str]:
    """Return placeholder text locally paired with this editable control.

    A ``.placeholder`` class alone is not enough: an application can use that
    class name for ordinary page content.  Chromium's broken empty-composer
    shape has both pieces of evidence on the *same* editable subtree: the
    control exposes the hint as one of its text fields and a descendant marks
    the same text with the exact class token.  The standard AX placeholder
    attribute is authoritative on its own.
    """
    if str(element.get("role") or "") not in EDITABLE_ROLES:
        return set()
    standard = _direct_placeholder_values(element)
    own_text = {text for key in _TEXT_KEYS if (text := _normalized(element.get(key)))}
    structural = _local_placeholder_values(element) - standard
    return standard | (structural & own_text)


def _ocr_visible_placeholder_values(element: dict[str, Any]) -> set[str]:
    """Return placeholder evidence that can actually be visible in pixels.

    ``AXPlaceholderValue`` describes a control even while that control contains
    real text.  In that state the hint is hidden and must not authorize removal
    of matching OCR elsewhere in the window.  An empty value can display any
    confirmed hint; a non-empty value can only be the Chromium failure mode
    where AX exposes the visible placeholder itself as the control value.
    """
    confirmed = _confirmed_placeholder_values(element)
    current_value = _normalized(element.get("value"))
    if not current_value:
        return confirmed
    return {value for value in confirmed if value == current_value}


def confirmed_placeholder_values(ax_tree: Any) -> tuple[str, ...]:
    """Collect placeholder strings proven in the screenshot's focused surface.

    OCR pixels cover the frontmost app's focused window, not background apps.
    Keep one value occurrence per proven editable control so one empty composer
    can consume at most one matching OCR field.  The walk is globally bounded
    in addition to the per-editable subtree bounds above.
    """
    if not isinstance(ax_tree, dict):
        return ()
    apps = [app for app in ax_tree.get("apps") or [] if isinstance(app, dict)]
    if not apps:
        return ()
    app = next((candidate for candidate in apps if candidate.get("is_frontmost")), apps[0])
    windows = [window for window in app.get("windows") or [] if isinstance(window, dict)]
    focused_windows = [window for window in windows if window.get("focused")]
    selected_windows = focused_windows[:1] or windows[:1]

    values: list[str] = []
    stack: list[Any] = [
        element
        for window in reversed(selected_windows)
        for element in reversed(window.get("elements") or [])
        if isinstance(element, dict)
    ]
    seen = 0
    while stack and seen < _MAX_PLACEHOLDER_TREE_NODES:
        current = stack.pop()
        seen += 1
        if isinstance(current, dict):
            if str(current.get("role") or "") in EDITABLE_ROLES:
                values.extend(sorted(_ocr_visible_placeholder_values(current)))
            stack.extend(
                reversed(
                    [child for child in current.get("children") or [] if isinstance(child, dict)]
                )
            )

    # The app-level focused reference can carry the standard AX placeholder
    # attribute even when the bounded window tree omitted the control. Avoid
    # double-counting the same control when its value was already found there.
    focused = app.get("focused_element")
    if isinstance(focused, dict):
        for value in sorted(_ocr_visible_placeholder_values(focused)):
            if value not in values:
                values.append(value)
    return tuple(values)


def _ocr_field_value(line: str) -> str | None:
    """Return a Markdown field payload only when the whole field is isolated."""
    stripped = line.strip()
    match = _MARKDOWN_LIST_FIELD.fullmatch(stripped)
    value = match.group("value").strip() if match else stripped
    tagged = _MARKDOWN_TAGGED_FIELD.fullmatch(value)
    if tagged:
        return tagged.group("value").strip()
    if match:
        return value
    named = _MARKDOWN_NAMED_FIELD.fullmatch(stripped)
    if named:
        return named.group("value").strip()
    return None


def _consume_exact(remaining: Counter[str], value: str) -> bool:
    if not value or remaining[value] <= 0:
        return False
    remaining[value] -= 1
    return True


def _ocr_removable_values(line: str, candidates: Collection[str]) -> tuple[str, ...]:
    """Identify exact removable units without deciding whether removal is safe."""
    stripped = line.strip()
    if stripped in candidates:
        return (stripped,)
    if stripped.startswith("|") and stripped.endswith("|"):
        return tuple(
            value for cell in stripped[1:-1].split("|") if (value := cell.strip()) in candidates
        )
    field_value = _ocr_field_value(line)
    return (field_value,) if field_value in candidates else ()


def _sanitize_ocr_table_line(line: str, remaining: Counter[str]) -> str | None:
    """Clear exact placeholder cells in one simple Markdown table row."""
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        return line
    cells = stripped[1:-1].split("|")
    substantive = [cell.strip() for cell in cells if cell.strip()]
    if not substantive or not any(remaining[cell] > 0 for cell in substantive):
        return line
    kept: list[str] = []
    for cell in cells:
        value = cell.strip()
        kept.append("" if _consume_exact(remaining, value) else value)
    if not any(kept):
        return None
    indent = line[: len(line) - len(line.lstrip())]
    return indent + "| " + " | ".join(kept) + " |"


def sanitize_ocr_text(text: str, placeholder_values: Collection[str]) -> str:
    """Remove only exact OCR lines or fields backed by AX placeholder proof.

    OCR is flat visual text, so it cannot safely use substring replacement: the
    same words may also occur in a real conversation.  This helper therefore
    removes a plain line, Markdown list payload, tagged list payload, named
    field value, or table cell only when that entire unit exactly equals a
    structurally confirmed placeholder.  All other OCR content is preserved.
    """
    proven = Counter(_normalized(value) for value in placeholder_values if _normalized(value))
    if not text or not proven:
        return text

    # OCR has no persisted geometry, so multiple identical fields cannot be
    # mapped safely back to one placeholder control.  Count candidates first;
    # if a value occurs more often than its structural AX proof, preserve every
    # occurrence of that value rather than guessing which one is UI chrome.
    candidate_counts: Counter[str] = Counter()
    for raw_line in text.splitlines():
        candidate_counts.update(_ocr_removable_values(raw_line, proven))
    remaining = Counter(
        {value: count for value, count in proven.items() if candidate_counts[value] <= count}
    )

    clean_lines: list[str] = []
    for raw_line in text.splitlines(keepends=True):
        if raw_line.endswith("\r\n"):
            line, ending = raw_line[:-2], "\r\n"
        elif raw_line.endswith(("\r", "\n")):
            line, ending = raw_line[:-1], raw_line[-1:]
        else:
            line, ending = raw_line, ""
        stripped = line.strip()
        if _consume_exact(remaining, stripped):
            continue
        table_line = _sanitize_ocr_table_line(line, remaining)
        if table_line is None:
            continue
        if table_line != line:
            clean_lines.append(table_line + ending)
            continue
        field_value = _ocr_field_value(line)
        if field_value is None or not _consume_exact(remaining, field_value):
            clean_lines.append(raw_line)
            continue
        named = _MARKDOWN_NAMED_FIELD.fullmatch(stripped)
        if named:
            indent = line[: len(line) - len(line.lstrip())]
            clean_lines.append(indent + named.group("label") + ending)
        # List fields consist only of the placeholder payload, so drop the
        # entire line instead of retaining a meaningless bullet/tag.
    return "".join(clean_lines)


def _sanitize_element(
    element: dict[str, Any],
    *,
    inside_editable: bool,
    placeholder_values: frozenset[str] = frozenset(),
) -> tuple[dict[str, Any] | None, bool]:
    """Return ``(node, changed)`` with structural sharing."""
    role = str(element.get("role") or "")
    is_editable = role in EDITABLE_ROLES
    in_control = inside_editable or is_editable
    # Nested editables own their placeholder evidence; never let an outer
    # control's hint bleed into an inner control.
    local_values = (
        frozenset(_confirmed_placeholder_values(element)) if is_editable else placeholder_values
    )

    if (
        inside_editable
        and local_values
        and _is_placeholder_node(element)
        and bool(_subtree_text(element) & local_values)
    ):
        return None, True

    children = element.get("children") or []
    clean_children: list[Any] = []
    changed = False
    for child in children:
        if not isinstance(child, dict):
            clean_children.append(child)
            continue
        clean_child, child_changed = _sanitize_element(
            child,
            inside_editable=in_control,
            placeholder_values=local_values,
        )
        changed = changed or child_changed
        if clean_child is not None:
            clean_children.append(clean_child)

    clear_keys = {
        key
        for key in _TEXT_KEYS
        if is_editable and (text := _normalized(element.get(key))) and text in local_values
    }
    changed = changed or bool(clear_keys)
    if not changed:
        return element, False

    clean = dict(element)
    if clean_children or "children" in clean:
        clean["children"] = clean_children
    for key in clear_keys:
        clean.pop(key, None)
    if "value" in clear_keys:
        clean["has_value"] = False
        clean["value_length"] = 0
    return clean, True


def _identities_compatible(reference: dict[str, Any], candidate: dict[str, Any]) -> bool:
    for key in ("identifier", "domIdentifier"):
        expected = _normalized(reference.get(key))
        actual = _normalized(candidate.get(key))
        if expected and actual and expected != actual:
            return False
    return True


def _reference_text_is_placeholder(
    app_data: dict[str, Any],
    reference: dict[str, Any],
    text: str,
    *,
    any_confirmed_match: bool = False,
) -> bool:
    """Prove one compact AX reference points into a local placeholder.

    Watcher event references do not retain AX identity or parent links.  Match
    them back to the full tree by role/text (and stable identifiers when
    available), and only classify the text when *every* compatible match lies
    in a confirmed editable-placeholder region.  Ambiguity therefore fails
    open and preserves authored text.
    """
    role = str(reference.get("role") or "")
    if not role or not text:
        return False

    matches: list[bool] = []

    def walk(
        element: dict[str, Any],
        *,
        inside_editable: bool,
        placeholder_values: frozenset[str],
        inside_placeholder: bool,
    ) -> None:
        current_role = str(element.get("role") or "")
        is_editable = current_role in EDITABLE_ROLES
        local_values = (
            frozenset(_confirmed_placeholder_values(element)) if is_editable else placeholder_values
        )
        in_control = inside_editable or is_editable
        this_placeholder = (
            False
            if is_editable
            else (
                inside_placeholder
                or (
                    inside_editable
                    and text in local_values
                    and _is_placeholder_node(element)
                    and text in _subtree_text(element)
                )
            )
        )
        element_text = {value for key in _TEXT_KEYS if (value := _normalized(element.get(key)))}
        if (
            current_role == role
            and text in element_text
            and _identities_compatible(reference, element)
        ):
            matches.append(this_placeholder or (is_editable and text in local_values))
        for child in element.get("children") or []:
            if isinstance(child, dict):
                walk(
                    child,
                    inside_editable=in_control,
                    placeholder_values=local_values,
                    inside_placeholder=this_placeholder,
                )

    for window in app_data.get("windows") or []:
        if not isinstance(window, dict):
            continue
        for element in window.get("elements") or []:
            if isinstance(element, dict):
                walk(
                    element,
                    inside_editable=False,
                    placeholder_values=frozenset(),
                    inside_placeholder=False,
                )
    return any(matches) if any_confirmed_match else bool(matches) and all(matches)


def sanitize_element_reference(
    app_data: dict[str, Any],
    reference: dict[str, Any],
    *,
    any_confirmed_match: bool = False,
) -> dict[str, Any]:
    """Remove proven placeholder text from a focused/clicked AX reference."""
    direct = _confirmed_placeholder_values(reference)
    clear_keys = {
        key
        for key in _TEXT_KEYS
        if (text := _normalized(reference.get(key)))
        and (
            text in direct
            or _reference_text_is_placeholder(
                app_data,
                reference,
                text,
                any_confirmed_match=any_confirmed_match,
            )
        )
    }
    if not clear_keys:
        return reference
    clean = dict(reference)
    for key in clear_keys:
        clean.pop(key, None)
    if "value" in clear_keys:
        clean["has_value"] = False
        clean["value_length"] = 0
    return clean


def sanitize_trigger(app_data: dict[str, Any], trigger: dict[str, Any]) -> dict[str, Any]:
    """Sanitize the watcher's compact ``details.element`` projection."""
    details = trigger.get("details")
    if not isinstance(details, dict):
        return trigger
    element = details.get("element")
    if not isinstance(element, dict):
        return trigger
    # A click label is an attention hint, not authored content. If the compact
    # hit-test reference is ambiguous with the same text elsewhere, prefer
    # dropping the label once any local placeholder match is proven; raw AX and
    # visible authored content remain untouched. Focused/input references keep
    # the stricter all-matches fail-open policy.
    clean_element = sanitize_element_reference(
        app_data,
        element,
        any_confirmed_match=str(trigger.get("event_type") or "") == "UserMouseClick",
    )
    if clean_element is element:
        return trigger
    clean_details = dict(details)
    clean_details["element"] = clean_element
    clean_trigger = dict(trigger)
    clean_trigger["details"] = clean_details
    return clean_trigger


def sanitize_app(app_data: dict[str, Any]) -> dict[str, Any]:
    """Return an app projection with placeholder semantics removed.

    The input object is returned unchanged when no repair is needed. This
    structural sharing keeps the normal, already-clean capture path cheap.
    """
    changed = False
    clean_windows: list[Any] = []
    for window in app_data.get("windows") or []:
        if not isinstance(window, dict):
            clean_windows.append(window)
            continue
        clean_elements: list[Any] = []
        window_changed = False
        for element in window.get("elements") or []:
            if not isinstance(element, dict):
                clean_elements.append(element)
                continue
            clean_element, element_changed = _sanitize_element(element, inside_editable=False)
            window_changed = window_changed or element_changed
            if clean_element is not None:
                clean_elements.append(clean_element)
        if window_changed:
            clean_window = dict(window)
            clean_window["elements"] = clean_elements
            clean_windows.append(clean_window)
            changed = True
        else:
            clean_windows.append(window)

    focused = app_data.get("focused_element")
    clean_focused = focused
    if isinstance(focused, dict):
        clean_focused = sanitize_element_reference(app_data, focused)
        changed = changed or clean_focused is not focused

    if not changed:
        return app_data
    clean_app = dict(app_data)
    clean_app["windows"] = clean_windows
    if isinstance(clean_focused, dict):
        clean_app["focused_element"] = clean_focused
    return clean_app


def sanitize_ax_tree(ax_tree: dict[str, Any]) -> dict[str, Any]:
    """Return an AX tree whose app projections cannot emit placeholder text."""
    apps = ax_tree.get("apps") or []
    clean_apps: list[Any] = []
    changed = False
    for app in apps:
        if not isinstance(app, dict):
            clean_apps.append(app)
            continue
        clean_app = sanitize_app(app)
        changed = changed or clean_app is not app
        clean_apps.append(clean_app)
    if not changed:
        return ax_tree
    clean_tree = dict(ax_tree)
    clean_tree["apps"] = clean_apps
    return clean_tree

"""Remove accessibility placeholders from authored-text signals.

Chromium can expose an empty editable control's placeholder as ``AXValue``.
The same control also exposes a descendant whose exact DOM class token is
``placeholder``.  Treat that local pairing as UI chrome, not user-authored
text.  The sanitizer is deliberately structural: it never hard-codes product
copy and never matches broad CSS class substrings.
"""

from __future__ import annotations

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
    app_data: dict[str, Any], reference: dict[str, Any], text: str
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
    return bool(matches) and all(matches)


def sanitize_element_reference(
    app_data: dict[str, Any], reference: dict[str, Any]
) -> dict[str, Any]:
    """Remove proven placeholder text from a focused/clicked AX reference."""
    direct = _confirmed_placeholder_values(reference)
    clear_keys = {
        key
        for key in _TEXT_KEYS
        if (text := _normalized(reference.get(key)))
        and (text in direct or _reference_text_is_placeholder(app_data, reference, text))
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
    clean_element = sanitize_element_reference(app_data, element)
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

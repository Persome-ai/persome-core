"""Unit tests for _click_anchor in timeline/aggregator.py.

The watcher hit-tests the AX element under the cursor on every mouse-down and
ships it on the capture's ``trigger.details``. ``_click_anchor`` renders that
element into the timeline prompt as the "what did the user point at" attention
signal — the strongest focus cue in AX-opaque apps (terminals) where
``focused_element`` is empty.
"""

from __future__ import annotations

from persome.timeline.aggregator import _click_anchor


def _click(element: dict | None, *, x: float = 842.0, y: float = 511.0) -> dict:
    details: dict = {"button": "left", "x": x, "y": y}
    if element is not None:
        details["element"] = element
    return {"event_type": "UserMouseClick", "details": details}


def test_renders_role_title_value() -> None:
    trig = _click({"role": "AXTextArea", "title": "composer", "value": "fix the bug"})
    out = _click_anchor(trig)
    assert out == '(attention: clicked [AXTextArea] title=composer: "fix the bug")'


def test_value_only_still_anchors() -> None:
    trig = _click({"role": "", "title": "", "value": "❯ pytest -k attention"})
    out = _click_anchor(trig)
    assert out == '(attention: clicked: "❯ pytest -k attention")'


def test_coordinates_are_not_rendered() -> None:
    # Geometry stays on the capture but never reaches the prompt — the OS
    # hit-test already resolved the pixels to the element below.
    trig = _click({"role": "AXButton", "title": "Send"}, x=123.0, y=456.0)
    out = _click_anchor(trig)
    assert "123" not in out and "456" not in out
    assert out == "(attention: clicked [AXButton] title=Send)"


def test_value_is_capped() -> None:
    trig = _click({"role": "AXTextArea", "value": "z" * 500})
    out = _click_anchor(trig)
    assert "z" * 200 in out
    assert "z" * 201 not in out


def test_no_details_returns_empty() -> None:
    assert _click_anchor({"event_type": "AXApplicationActivated"}) == ""


def test_empty_element_returns_empty() -> None:
    assert _click_anchor(_click({"role": "", "title": "", "value": ""})) == ""
    assert _click_anchor(_click(None)) == ""


def test_non_click_trigger_with_no_details_is_empty() -> None:
    assert _click_anchor({"event_type": "UserTextInput"}) == ""


def test_text_input_element_is_not_mislabeled_as_click() -> None:
    trigger = {
        "event_type": "UserTextInput",
        "details": {
            "element": {
                "role": "AXTextArea",
                "value": "draft from the keyboard event",
            }
        },
    }
    assert _click_anchor(trigger) == ""

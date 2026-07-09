"""Unit tests for _focus_pane in timeline/aggregator.py.

The capture layer marks the focused region of a chrome-heavy window with a
structural marker (e.g. cmux_source appends the real terminal surface after
`### [cmux terminal]`). _focus_pane localizes to that region in code, so the
prompt stays a general principle instead of hardcoding per-app rules.
"""

from __future__ import annotations

from persome.timeline.aggregator import _focus_pane


def test_extracts_region_after_marker_and_drops_chrome() -> None:
    chrome = "## cmux [active] workspace 1/7 workspace 2/7 有可用更新 切换侧边栏 "
    pane = "❯ pytest -k attention\n12 passed real work here"
    text = chrome + "### [cmux terminal]\n" + pane
    region, focused = _focus_pane(text)
    assert focused is True
    assert region == pane
    assert "workspace 1/7" not in region
    assert "有可用更新" not in region


def test_no_marker_returns_input_unchanged() -> None:
    text = "some browser page content with no focus marker"
    region, focused = _focus_pane(text)
    assert focused is False
    assert region == text


def test_empty_text() -> None:
    assert _focus_pane("") == ("", False)

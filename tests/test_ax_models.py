"""Unit tests for AX tree → Markdown rendering (``capture/ax_models.py``).

Pure functions; no platform or subprocess dependency.
"""

from __future__ import annotations

from persome.capture.ax_models import (
    AXCaptureResult,
    ax_app_to_markdown,
    ax_tree_to_markdown,
)


def test_capture_result_defaults() -> None:
    r = AXCaptureResult(raw_json={}, timestamp="t", apps=[])
    assert r.metadata == {}


def test_tree_to_markdown_renders_app_and_window_headings() -> None:
    tree = {
        "apps": [
            {
                "name": "Safari",
                "is_frontmost": True,
                "bundle_id": "com.apple.Safari",
                "windows": [{"title": "Example", "elements": []}],
            }
        ]
    }
    md = ax_tree_to_markdown(tree)
    assert "## Safari [active]" in md
    assert "_com.apple.Safari_" in md
    assert "### Example" in md


def test_tree_to_markdown_untitled_window_and_no_badge() -> None:
    tree = {"apps": [{"name": "Notes", "windows": [{"elements": []}]}]}
    md = ax_tree_to_markdown(tree)
    assert "## Notes" in md
    assert "[active]" not in md
    assert "### (untitled)" in md


def test_tree_to_markdown_empty() -> None:
    assert ax_tree_to_markdown({}) == ""
    assert ax_tree_to_markdown({"apps": []}) == ""


def test_app_to_markdown_wraps_single_app() -> None:
    md = ax_app_to_markdown({"name": "Zoom", "windows": []})
    assert md.startswith("## Zoom")

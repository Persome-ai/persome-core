from __future__ import annotations

from persome.capture import s1_parser


def _ax_tree(*apps: dict) -> dict:
    return {"apps": list(apps), "timestamp": "2026-04-21T10:00:00+08:00"}


def test_enrich_noop_without_ax_tree() -> None:
    capture = {"timestamp": "x", "window_meta": {"app_name": "A"}}
    s1_parser.enrich(capture)
    assert "focused_element" not in capture
    assert "visible_text" not in capture


def test_enrich_picks_frontmost_app() -> None:
    capture = {
        "ax_tree": _ax_tree(
            {"name": "Background", "bundle_id": "b", "is_frontmost": False, "windows": []},
            {
                "name": "Cursor",
                "bundle_id": "com.todesktop.230313mzl4w4u92",
                "is_frontmost": True,
                "windows": [
                    {
                        "title": "s1_parser.py",
                        "focused": True,
                        "elements": [
                            {
                                "role": "AXTextArea",
                                "title": "editor",
                                "value": "def enrich(capture):\n    ...",
                            }
                        ],
                    }
                ],
            },
        )
    }
    s1_parser.enrich(capture)
    assert capture["focused_element"]["role"] == "AXTextArea"
    assert capture["focused_element"]["is_editable"] is True
    assert capture["focused_element"]["has_value"] is True
    assert capture["focused_element"]["value_length"] > 0
    assert "s1_parser.py" in capture["visible_text"]
    assert capture["url"] is None


def test_enrich_extracts_browser_url() -> None:
    capture = {
        "ax_tree": _ax_tree(
            {
                "name": "Chrome",
                "bundle_id": "com.google.Chrome",
                "is_frontmost": True,
                "windows": [
                    {
                        "title": "Anthropic",
                        "focused": True,
                        "elements": [
                            {
                                "role": "AXTextField",
                                "title": "Address and search bar",
                                "value": "https://www.anthropic.com/news",
                            }
                        ],
                    }
                ],
            }
        )
    }
    s1_parser.enrich(capture)
    assert capture["url"] == "https://www.anthropic.com/news"
    assert capture["focused_element"]["role"] == "AXTextField"


def test_enrich_prefixes_bare_url() -> None:
    capture = {
        "ax_tree": _ax_tree(
            {
                "name": "Safari",
                "bundle_id": "com.apple.Safari",
                "is_frontmost": True,
                "windows": [
                    {
                        "title": "",
                        "focused": True,
                        "elements": [
                            {
                                "role": "AXTextField",
                                "value": "anthropic.com",
                            }
                        ],
                    }
                ],
            }
        )
    }
    s1_parser.enrich(capture)
    assert capture["url"] == "https://anthropic.com"


def test_enrich_non_browser_has_no_url() -> None:
    capture = {
        "ax_tree": _ax_tree(
            {
                "name": "Cursor",
                "bundle_id": "com.todesktop.230313mzl4w4u92",
                "is_frontmost": True,
                "windows": [
                    {
                        "title": "file.py",
                        "focused": True,
                        "elements": [
                            {
                                "role": "AXTextField",
                                "value": "https://example.com",
                            }
                        ],
                    }
                ],
            }
        )
    }
    s1_parser.enrich(capture)
    assert capture["url"] is None


def test_enrich_visible_text_truncation() -> None:
    huge_value = "x" * 20_000
    capture = {
        "ax_tree": _ax_tree(
            {
                "name": "App",
                "bundle_id": "b",
                "is_frontmost": True,
                "windows": [
                    {
                        "title": "T",
                        "focused": True,
                        "elements": [
                            {"role": "AXStaticText", "title": "header", "value": huge_value}
                        ],
                    }
                ],
            }
        )
    }
    s1_parser.enrich(capture)
    assert len(capture["visible_text"]) <= 10_000 + len("\n...(truncated)")
    assert capture["visible_text"].endswith("(truncated)")


def test_enrich_no_focused_window_returns_empty_element() -> None:
    capture = {
        "ax_tree": _ax_tree(
            {
                "name": "App",
                "bundle_id": "b",
                "is_frontmost": True,
                "windows": [
                    {
                        "title": "unfocused",
                        "focused": False,
                        "elements": [{"role": "AXTextField", "value": "something"}],
                    }
                ],
            }
        )
    }
    s1_parser.enrich(capture)
    fe = capture["focused_element"]
    assert fe["role"] == ""
    assert fe["value"] == ""
    assert fe["is_editable"] is False


def test_enrich_empty_ax_tree() -> None:
    capture = {"ax_tree": {"apps": []}}
    s1_parser.enrich(capture)
    assert capture["focused_element"]["role"] == ""
    assert capture["visible_text"] == ""
    assert capture["url"] is None


def test_enrich_falls_back_to_first_app_when_no_frontmost() -> None:
    capture = {
        "ax_tree": _ax_tree(
            {
                "name": "OnlyApp",
                "bundle_id": "b",
                "windows": [
                    {
                        "title": "T",
                        "focused": True,
                        "elements": [{"role": "AXStaticText", "value": "hello"}],
                    }
                ],
            }
        )
    }
    s1_parser.enrich(capture)
    assert "hello" in capture["visible_text"]


def test_enrich_browser_narrows_to_page_and_folds_chrome() -> None:
    """Progressive disclosure: a browser's visible_text is the page (AXWebArea)
    plus a one-line chrome digest — the bookmarks/tabs/toolbar are dropped from
    the text (they stay in ax_tree)."""
    from persome.capture import browser_detect

    browser_detect.set_http_handlers_for_test({"com.google.Chrome"})
    try:
        capture = {
            "ax_tree": _ax_tree(
                {
                    "name": "Chrome",
                    "bundle_id": "com.google.Chrome",
                    "is_frontmost": True,
                    "windows": [
                        {
                            "title": "Example",
                            "focused": True,
                            "elements": [
                                {
                                    "role": "AXToolbar",
                                    "children": [
                                        {"role": "AXButton", "title": "Bookmark One"},
                                        {"role": "AXButton", "title": "Bookmark Two"},
                                        {"role": "AXTextField", "value": "https://example.com"},
                                    ],
                                },
                                {
                                    "role": "AXTabGroup",
                                    "children": [
                                        {"role": "AXRadioButton", "title": "Tab A"},
                                        {"role": "AXRadioButton", "title": "Tab B"},
                                    ],
                                },
                                {
                                    "role": "AXWebArea",
                                    "title": "Example Page",
                                    "children": [
                                        {"role": "AXHeading", "value": "The Real Article Heading"},
                                        {
                                            "role": "AXStaticText",
                                            "value": "Body paragraph the user is actually reading.",
                                        },
                                    ],
                                },
                            ],
                        }
                    ],
                }
            )
        }
        s1_parser.enrich(capture)
        vt = capture["visible_text"]
        # page content present
        assert "The Real Article Heading" in vt
        assert "Body paragraph the user is actually reading." in vt
        # chrome folded to a digest, NOT dumped as bullets
        assert "浏览器外壳已折叠" in vt
        assert "Bookmark One" not in vt and "Bookmark Two" not in vt
        assert "Tab A" not in vt and "Tab B" not in vt
    finally:
        browser_detect.set_http_handlers_for_test(None)


def test_enrich_browser_without_webarea_falls_open() -> None:
    """A browser frame with no AXWebArea (e.g. a blank/new tab) keeps the normal
    whole-window render rather than blanking the capture."""
    from persome.capture import browser_detect

    browser_detect.set_http_handlers_for_test({"com.google.Chrome"})
    try:
        capture = {
            "ax_tree": _ax_tree(
                {
                    "name": "Chrome",
                    "bundle_id": "com.google.Chrome",
                    "is_frontmost": True,
                    "windows": [
                        {
                            "title": "New Tab",
                            "focused": True,
                            "elements": [
                                {
                                    "role": "AXToolbar",
                                    "children": [{"role": "AXButton", "title": "Home"}],
                                }
                            ],
                        },
                    ],
                }
            )
        }
        s1_parser.enrich(capture)
        # no web area → whole render (chrome digest not applied)
        assert "浏览器外壳已折叠" not in capture["visible_text"]
    finally:
        browser_detect.set_http_handlers_for_test(None)


def test_enrich_focused_element_from_helper_axfocused() -> None:
    """The AX helper now emits the OS focused element (AXFocusedUIElement) on the
    app dict; enrich prefers it over the shallow focused-window scan, so the
    focused control is captured even when it nests deep (browsers/editors)."""
    capture = {
        "ax_tree": _ax_tree(
            {
                "name": "Tabbit",
                "bundle_id": "com.tab-browser.Tabbit",
                "is_frontmost": True,
                # helper-emitted focused element (the real keyboard focus)
                "focused_element": {
                    "role": "AXTextField",
                    "value": "draft message the user is typing",
                    "is_editable": True,
                    "has_selection": True,
                },
                "windows": [
                    {
                        "title": "X",
                        "focused": True,
                        "elements": [
                            # deep, NOT a direct child the legacy scan would find
                            {"role": "AXGroup", "children": [{"role": "AXWebArea"}]}
                        ],
                    },
                ],
            }
        )
    }
    s1_parser.enrich(capture)
    fe = capture["focused_element"]
    assert fe["role"] == "AXTextField"
    assert fe["value"] == "draft message the user is typing"
    assert fe["is_editable"] is True


def test_enrich_focused_element_falls_back_to_scan_without_helper() -> None:
    """No helper-emitted focused_element → the legacy focused-window scan still
    works (back-compat for captures from an older helper)."""
    capture = {
        "ax_tree": _ax_tree(
            {
                "name": "Notes",
                "bundle_id": "com.apple.Notes",
                "is_frontmost": True,
                "windows": [
                    {
                        "title": "Note",
                        "focused": True,
                        "elements": [{"role": "AXTextArea", "value": "legacy-scanned focus"}],
                    },
                ],
            }
        )
    }
    s1_parser.enrich(capture)
    assert capture["focused_element"]["role"] == "AXTextArea"
    assert capture["focused_element"]["value"] == "legacy-scanned focus"

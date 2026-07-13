from __future__ import annotations

import copy

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
        assert "browser chrome folded" in vt
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
        assert "\u6d4f\u89c8\u5668\u5916\u58f3\u5df2\u6298\u53e0" not in capture["visible_text"]
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


_COMPOSER_PLACEHOLDER = "Ask for follow-up changes"


def _chromium_composer(
    value: str,
    *,
    placeholder_class: str = "placeholder",
    standard_placeholder: str = "",
) -> dict:
    textarea = {
        "role": "AXTextArea",
        "value": value,
        "domClassList": ["ProseMirror"],
        "children": [
            {
                "role": "AXGroup",
                "domClassList": [placeholder_class],
                "children": [{"role": "AXStaticText", "value": _COMPOSER_PLACEHOLDER}],
            }
        ],
    }
    if standard_placeholder:
        textarea["AXPlaceholderValue"] = standard_placeholder
    return textarea


def _composer_capture(textarea: dict, *, focused_value: str | None = None) -> dict:
    focused_value = textarea.get("value", "") if focused_value is None else focused_value
    return {
        "ax_tree": _ax_tree(
            {
                "name": "Chat",
                "bundle_id": "com.example.chromium-chat",
                "is_frontmost": True,
                "focused_element": {
                    "role": "AXTextArea",
                    "value": focused_value,
                    "is_editable": True,
                    "value_length": len(focused_value),
                },
                "windows": [
                    {
                        "title": "Conversation",
                        "focused": True,
                        "elements": [
                            {
                                "role": "AXStaticText",
                                "value": "Existing conversation content long enough to render.",
                            },
                            textarea,
                        ],
                    }
                ],
            }
        )
    }


def test_enrich_filters_chromium_placeholder_without_mutating_raw_ax() -> None:
    capture = _composer_capture(_chromium_composer(f"\n{_COMPOSER_PLACEHOLDER}"))
    raw = copy.deepcopy(capture["ax_tree"])

    s1_parser.enrich(capture)

    focused = capture["focused_element"]
    assert focused["role"] == "AXTextArea"
    assert focused["value"] == ""
    assert focused["has_value"] is False
    assert focused["value_length"] == 0
    assert _COMPOSER_PLACEHOLDER not in capture["visible_text"]
    assert capture["ax_tree"] == raw


def test_enrich_keeps_typed_value_and_unmatched_class_child() -> None:
    typed = "Please fix the capture bug"
    capture = _composer_capture(_chromium_composer(typed))

    s1_parser.enrich(capture)

    assert capture["focused_element"]["value"] == typed
    assert typed in capture["visible_text"]
    # Exact class alone is insufficient to delete subtree content. The real
    # empty-composer bug also exposes the same text on the owning control.
    assert _COMPOSER_PLACEHOLDER in capture["visible_text"]


def test_enrich_does_not_globally_remove_same_text_outside_editable_control() -> None:
    textarea = _chromium_composer(_COMPOSER_PLACEHOLDER)
    capture = _composer_capture(textarea)
    capture["ax_tree"]["apps"][0]["windows"][0]["elements"].insert(
        0, {"role": "AXStaticText", "value": _COMPOSER_PLACEHOLDER}
    )
    capture["trigger"] = {
        "event_type": "UserMouseClick",
        "details": {"element": {"role": "AXStaticText", "value": _COMPOSER_PLACEHOLDER}},
    }

    s1_parser.enrich(capture)

    assert capture["focused_element"]["value"] == ""
    assert _COMPOSER_PLACEHOLDER in capture["visible_text"]
    assert capture["trigger"]["details"]["element"].get("value", "") == ""


def test_ocr_placeholder_filter_fails_open_for_ambiguous_exact_occurrences() -> None:
    capture = _composer_capture(_chromium_composer(_COMPOSER_PLACEHOLDER))
    assert s1_parser.sanitize_ocr_text(capture, _COMPOSER_PLACEHOLDER) == ""

    ocr = "\n".join(
        (
            _COMPOSER_PLACEHOLDER,
            _COMPOSER_PLACEHOLDER,
            f"Conversation quoted: {_COMPOSER_PLACEHOLDER}",
        )
    )

    clean = s1_parser.sanitize_ocr_text(capture, ocr)

    assert clean.splitlines() == [
        _COMPOSER_PLACEHOLDER,
        _COMPOSER_PLACEHOLDER,
        f"Conversation quoted: {_COMPOSER_PLACEHOLDER}",
    ]


def test_ocr_placeholder_filter_removes_exact_field_and_keeps_mixed_content() -> None:
    capture = _composer_capture(_chromium_composer(_COMPOSER_PLACEHOLDER))
    ocr = "\n".join(
        (
            "## [Message pane]",
            f"- [self] {_COMPOSER_PLACEHOLDER}",
            "- [self] legitimate OCR body",
        )
    )

    clean = s1_parser.sanitize_ocr_text(capture, ocr)

    assert _COMPOSER_PLACEHOLDER not in clean
    assert clean.splitlines() == ["## [Message pane]", "- [self] legitimate OCR body"]


def test_ocr_placeholder_filter_keeps_phrase_inside_normal_sentence() -> None:
    capture = _composer_capture(_chromium_composer(_COMPOSER_PLACEHOLDER))
    sentence = f"Alice said: {_COMPOSER_PLACEHOLDER}"

    assert s1_parser.sanitize_ocr_text(capture, sentence) == sentence


def test_ocr_placeholder_evidence_is_scoped_to_frontmost_focused_window() -> None:
    background = _composer_capture(_chromium_composer(_COMPOSER_PLACEHOLDER))["ax_tree"]["apps"][0]
    background["is_frontmost"] = False
    unfocused_placeholder_window = copy.deepcopy(background["windows"][0])
    unfocused_placeholder_window["focused"] = False
    foreground = {
        "name": "Foreground",
        "bundle_id": "com.example.foreground",
        "is_frontmost": True,
        "windows": [
            unfocused_placeholder_window,
            {
                "title": "Focused",
                "focused": True,
                "elements": [{"role": "AXStaticText", "value": "real content"}],
            },
        ],
    }
    capture = {"ax_tree": _ax_tree(background, foreground)}

    assert s1_parser.sanitize_ocr_text(capture, _COMPOSER_PLACEHOLDER) == _COMPOSER_PLACEHOLDER


def test_ocr_placeholder_filter_ignores_hidden_standard_hint_on_filled_control() -> None:
    hint = "Search"
    textarea = {
        "role": "AXTextField",
        "value": "typed query",
        "AXPlaceholderValue": hint,
    }
    capture = _composer_capture(textarea)
    ocr = f"{hint}\nlegitimate OCR body"

    assert s1_parser.ocr_placeholder_values(capture) == ()
    assert s1_parser.sanitize_ocr_text(capture, ocr) == ocr


def test_enrich_requires_exact_placeholder_dom_class_token() -> None:
    styled = "placeholder:text-token-input-placeholder-foreground"
    capture = _composer_capture(_chromium_composer(_COMPOSER_PLACEHOLDER, placeholder_class=styled))

    s1_parser.enrich(capture)

    assert capture["focused_element"]["value"] == _COMPOSER_PLACEHOLDER
    assert _COMPOSER_PLACEHOLDER in capture["visible_text"]


def test_enrich_filters_standard_ax_placeholder_value_without_dom_marker() -> None:
    textarea = {
        "role": "AXTextArea",
        "value": _COMPOSER_PLACEHOLDER,
        "AXPlaceholderValue": _COMPOSER_PLACEHOLDER,
    }
    capture = _composer_capture(textarea)

    s1_parser.enrich(capture)

    assert capture["focused_element"]["value"] == ""
    assert _COMPOSER_PLACEHOLDER not in capture["visible_text"]


def test_enrich_filters_helper_tree_standard_placeholder_after_native_focus_cleanup() -> None:
    textarea = {
        "role": "AXTextArea",
        "value": _COMPOSER_PLACEHOLDER,
        "AXPlaceholderValue": _COMPOSER_PLACEHOLDER,
    }
    # The native helper already removes the placeholder from its compact
    # focused projection. Its window tree deliberately keeps the raw value and
    # now carries AXPlaceholderValue so Python can clean the visible S1 view.
    capture = _composer_capture(textarea, focused_value="")
    capture["ax_tree"]["apps"][0]["focused_element"]["AXPlaceholderValue"] = _COMPOSER_PLACEHOLDER
    raw = copy.deepcopy(capture["ax_tree"])

    s1_parser.enrich(capture)

    assert capture["focused_element"]["value"] == ""
    assert _COMPOSER_PLACEHOLDER not in capture["visible_text"]
    assert capture["ax_tree"] == raw


def test_enrich_keeps_matching_text_without_placeholder_evidence() -> None:
    capture = _composer_capture({"role": "AXTextArea", "value": _COMPOSER_PLACEHOLDER})

    s1_parser.enrich(capture)

    assert capture["focused_element"]["value"] == _COMPOSER_PLACEHOLDER


def test_sanitize_capture_repairs_historical_s1_projection() -> None:
    capture = _composer_capture(_chromium_composer(_COMPOSER_PLACEHOLDER))
    capture["focused_element"] = {
        "role": "AXTextArea",
        "value": _COMPOSER_PLACEHOLDER,
        "is_editable": True,
        "has_value": True,
        "value_length": len(_COMPOSER_PLACEHOLDER),
    }
    capture["visible_text"] = f"[TextArea] {_COMPOSER_PLACEHOLDER}"

    clean = s1_parser.sanitize_capture(capture)

    assert clean is not capture
    assert clean["focused_element"]["value"] == ""
    assert _COMPOSER_PLACEHOLDER not in clean["visible_text"]
    assert capture["focused_element"]["value"] == _COMPOSER_PLACEHOLDER


def test_enrich_filters_matching_placeholder_title_and_description() -> None:
    textarea = _chromium_composer(_COMPOSER_PLACEHOLDER)
    textarea["title"] = _COMPOSER_PLACEHOLDER
    textarea["description"] = _COMPOSER_PLACEHOLDER
    capture = _composer_capture(textarea)
    focused = capture["ax_tree"]["apps"][0]["focused_element"]
    focused["title"] = _COMPOSER_PLACEHOLDER
    focused["description"] = _COMPOSER_PLACEHOLDER

    s1_parser.enrich(capture)

    assert capture["focused_element"]["title"] == ""
    assert capture["focused_element"]["value"] == ""
    assert _COMPOSER_PLACEHOLDER not in capture["visible_text"]


def test_enrich_preserves_ambiguous_same_role_authored_value() -> None:
    placeholder = _chromium_composer(_COMPOSER_PLACEHOLDER)
    authored = {"role": "AXTextArea", "value": _COMPOSER_PLACEHOLDER}
    capture = _composer_capture(placeholder)
    app = capture["ax_tree"]["apps"][0]
    app["windows"][0]["elements"].append(authored)

    s1_parser.enrich(capture)

    # The compact focused reference has no stable identity. Since one
    # compatible TextArea is authored, ambiguity fails open.
    assert capture["focused_element"]["value"] == _COMPOSER_PLACEHOLDER
    assert _COMPOSER_PLACEHOLDER in capture["visible_text"]


def test_sanitize_capture_repairs_historical_static_placeholder_click() -> None:
    capture = _composer_capture(_chromium_composer(_COMPOSER_PLACEHOLDER))
    capture["trigger"] = {
        "event_type": "UserMouseClick",
        "details": {"element": {"role": "AXStaticText", "value": _COMPOSER_PLACEHOLDER}},
    }
    raw_ax = capture["ax_tree"]

    clean = s1_parser.sanitize_capture(capture)

    click = clean["trigger"]["details"]["element"]
    assert click.get("value", "") == ""
    # MCP/debug readers retain the raw evidence unless a caller explicitly
    # requests a cleaned structural projection.
    assert clean["ax_tree"] is raw_ax
    assert capture["trigger"]["details"]["element"]["value"] == _COMPOSER_PLACEHOLDER


def test_sanitize_capture_preserves_cmux_post_s1_injection() -> None:
    capture = _composer_capture(_chromium_composer(_COMPOSER_PLACEHOLDER))
    terminal = "### [cmux terminal] tests\n42 passed"
    capture["visible_text"] = f"[TextArea] {_COMPOSER_PLACEHOLDER}\n\n{terminal}"
    capture["cmux_text_injected"] = True

    clean = s1_parser.sanitize_capture(capture)

    assert _COMPOSER_PLACEHOLDER not in clean["visible_text"]
    assert terminal in clean["visible_text"]


def test_sanitize_capture_preserves_empty_ocr_backfill_sentinel() -> None:
    capture = _composer_capture(_chromium_composer(_COMPOSER_PLACEHOLDER))
    capture["focused_element"] = {
        "role": "AXTextArea",
        "value": _COMPOSER_PLACEHOLDER,
        "is_editable": True,
    }
    capture["visible_text"] = ""
    capture["ocr_submitted"] = True

    clean = s1_parser.sanitize_capture(capture)

    assert clean["focused_element"]["value"] == ""
    assert clean["visible_text"] == ""

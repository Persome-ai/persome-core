"""Tests for generic browser parsing using synthetic Chromium AX trees."""

from __future__ import annotations

import pytest

from persome import parsers
from persome.parsers import _axtree as ax
from persome.parsers.base import ParsedConversation, StructuredContent
from persome.parsers.web import (
    BrowserParser,
    WebItem,
    WebPage,
    _is_nav_item,
    _looks_like_url,
    _merge_lines,
)

_TABBIT_BUNDLE = "com.tab-browser.Tabbit"
_SUN_BUNDLE = "com.adspower.SunBrowser"


# --------------------------------------------------------------------------- #
# StructuredContent protocol generalization                                   #
# --------------------------------------------------------------------------- #


def test_parsed_conversation_satisfies_structured_content():
    """The existing ParsedConversation is a StructuredContent (it has render)."""
    conv = ParsedConversation(app="feishu", thread_title=None, messages=[], parser_version="x")
    assert isinstance(conv, StructuredContent)


def test_webpage_satisfies_structured_content():
    page = WebPage(app="Tabbit\u6d4f\u89c8\u5668", title="t", items=(WebItem(None, ("hi",)),))
    assert isinstance(page, StructuredContent)


# --------------------------------------------------------------------------- #
# WebPage.render — XML shape, escaping, empty handling                        #
# --------------------------------------------------------------------------- #


def test_webpage_render_structured_xml():
    page = WebPage(
        app="Tabbit\u6d4f\u89c8\u5668",
        title="Issues · acme-dev/acme-mono",
        url="https://github.com/acme-dev/acme-mono/issues",
        items=(
            WebItem(
                "feat(app): \u5347\u7ea7\u5931\u8d25\u56de\u6eda",
                ("area:distribution · type:tech-debt", "#200 · opened"),
            ),
        ),
    )
    out = page.render()
    assert out.startswith('<web_page app="Tabbit\u6d4f\u89c8\u5668"')
    assert 'url="https://github.com/acme-dev/acme-mono/issues"' in out
    assert 'title="Issues · acme-dev/acme-mono"' in out
    assert out.index("url=") < out.index("title=")
    # The item nests its heading + text lines.
    assert "<item>" in out and "</item>" in out
    assert "<heading>feat(app): \u5347\u7ea7\u5931\u8d25\u56de\u6eda</heading>" in out
    assert "<text>area:distribution · type:tech-debt</text>" in out
    assert "<text>#200 · opened</text>" in out
    assert out.rstrip().endswith("</web_page>")


def test_webpage_render_no_url_attr_when_missing():
    out = WebPage(app="b", title="t", items=(WebItem(None, ("body",)),)).render()
    assert "url=" not in out
    assert 'title="t"' in out


def test_webpage_render_escapes_xml():
    page = WebPage(
        app="b",
        title='a & b <tag> "q"',
        items=(WebItem("h & <i>", ("x < y & z > w",)),),
    )
    out = page.render()
    assert "&amp;" in out and "&lt;" in out and "&gt;" in out
    assert "<tag>" not in out  # literal angle bracket must be escaped
    assert "<i>" not in out


def test_webpage_render_empty_returns_empty_string():
    assert WebPage(app="b", title="t", items=()).render() == ""


def test_webpage_render_headless_item_has_no_heading_tag():
    out = WebPage(app="b", title=None, items=(WebItem(None, ("just text",)),)).render()
    assert "<heading>" not in out
    assert "<text>just text</text>" in out


# --------------------------------------------------------------------------- #
# _merge_lines — re-joining the fragments of a single visual line             #
# --------------------------------------------------------------------------- #


def test_merge_lines_joins_short_fragments():
    merged = _merge_lines(["Sort by", "Newest", ", descending"])
    assert merged == ["Sort by · Newest · , descending"]


def test_merge_lines_keeps_long_prose_separate():
    long = "x" * 80
    merged = _merge_lines(["short a", long, "short b"])
    assert merged == ["short a", long, "short b"]


def test_merge_lines_collapses_echo_to_longer():
    # Adjacent fragments where one contains the other collapse to the longer.
    merged = _merge_lines(["area:distribution", "area:distribution DMG"])
    assert merged == ["area:distribution DMG"]


def test_merge_lines_seamless_join_for_cjk_prose():
    # A CJK sentence the browser split into runs must read back as one sentence —
    # no `·` injected mid-sentence.
    merged = _merge_lines(
        [
            "\u5bfb\u627e\u521b\u65b0\u8fd9\u4ef6\u4e8b\u672c\u8eab",
            "\u53d8\u6210\u4e86\u4e00\u5957\u53ef\u5de5\u4e1a\u5316\u7684\u6d41\u7a0b\u3002",
        ]
    )
    assert merged == [
        "\u5bfb\u627e\u521b\u65b0\u8fd9\u4ef6\u4e8b\u672c\u8eab\u53d8\u6210\u4e86\u4e00\u5957\u53ef\u5de5\u4e1a\u5316\u7684\u6d41\u7a0b\u3002"
    ]
    assert "·" not in merged[0]

    merged2 = _merge_lines(
        [
            '\u800c\u771f\u6b63\u8ba9\u8fd9\u5957\u4e1c\u897f"\u53ef\u89c4\u6a21\u5316"\u7684,\u662f\u5e95\u5c42\u7684\u4e24\u4e2a\u6760\u6746',
            ":\u4e00\u662f",
            "\u63a8\u8350\u7b97\u6cd5\u4f5c\u4e3a\u53ef\u590d\u7528\u7684\u6838\u5fc3\u57fa\u7840\u8bbe\u65bd",
        ]
    )
    assert "·" not in merged2[0]
    assert (
        "\u6760\u6746:\u4e00\u662f\u63a8\u8350\u7b97\u6cd5\u4f5c\u4e3a\u53ef\u590d\u7528\u7684\u6838\u5fc3\u57fa\u7840\u8bbe\u65bd"
        in merged2[0]
    )


def test_merge_lines_label_join_stays_for_latin_meta():
    # Discrete latin labels / meta keep the ` · ` separator.
    assert _merge_lines(["Open", "Closed"]) == ["Open · Closed"]
    meta = _merge_lines(["Status: Open.", "DemoUserX", "opened", "5 days ago"])
    assert meta == ["Status: Open. · DemoUserX · opened · 5 days ago"]


# --------------------------------------------------------------------------- #
# _is_nav_item — dropping navigation clusters                                 #
# --------------------------------------------------------------------------- #


def test_is_nav_item_drops_control_dominated_run():
    # A breadcrumb / tab row: all lines come from links, no heading, all short.
    lines = [("Code", True), ("Issues", True), ("Pull requests", True)]
    assert _is_nav_item(None, False, lines) is True


def test_is_nav_item_drops_all_link_row():
    # A repo-tab row: every fragment is a link label → nav.
    lines = [("Code", True), ("Issues", True), ("Pull requests", True), ("Actions", True)]
    assert _is_nav_item("Repository navigation", False, lines) is True


def test_is_nav_item_keeps_issue_record():
    # An issue record: title + labels are links, but status/author/time are bare
    # text — the fraction lands below the threshold → content.
    lines = [
        ("area:distribution", True),
        ("type:tech-debt", True),
        ("Status: Open.", False),
        ("DemoUserX", True),
        ("opened", False),
        ("5 days ago", False),
    ]
    assert _is_nav_item("feat(app): \u5347\u7ea7\u5931\u8d25\u56de\u6eda", True, lines) is False


def test_is_nav_item_keeps_long_prose():
    # A run with a long prose line is content regardless of provenance.
    lines = [("short", True), ("x" * 80, False)]
    assert _is_nav_item(None, False, lines) is False


def test_is_nav_item_bare_nav_heading():
    # A standalone control-label heading with no body → nav.
    assert _is_nav_item("Products", True, []) is True
    # A standalone content heading with no body → keep.
    assert _is_nav_item("Some Section", False, []) is False


# --------------------------------------------------------------------------- #
# BrowserParser — declines non-browser / contentless trees                    #
# --------------------------------------------------------------------------- #


def test_parser_registered_for_chromium_bundles():
    for bundle in (_TABBIT_BUNDLE, _SUN_BUNDLE, "com.google.Chrome"):
        assert isinstance(parsers.get_parser(bundle), BrowserParser)


def test_parse_returns_none_when_no_web_area():
    parser = BrowserParser()
    tree = {
        "apps": [
            {
                "bundle_id": _TABBIT_BUNDLE,
                "is_frontmost": True,
                "windows": [{"focused": True, "elements": [{"role": "AXGroup", "children": []}]}],
            }
        ]
    }
    assert parser.parse(tree, window_title="x") is None


def test_parse_returns_none_for_unmatched_bundle():
    parser = BrowserParser()
    tree = {
        "apps": [
            {
                "bundle_id": "com.electron.lark",
                "is_frontmost": True,
                "windows": [
                    {
                        "focused": True,
                        "elements": [{"role": "AXWebArea", "title": "t", "children": []}],
                    }
                ],
            }
        ]
    }
    assert parser.parse(tree, window_title="x") is None


# --------------------------------------------------------------------------- #
# BrowserParser — synthetic full-page fixtures                                #
# --------------------------------------------------------------------------- #


def _text(value: str, *, role: str = "AXStaticText") -> dict:
    return {"role": role, "value": value, "children": []}


def _browser_capture(*, bundle: str, title: str, url: str, children: list[dict]) -> dict:
    return {
        "timestamp": "2026-07-10T09:00:00+08:00",
        "window_meta": {"app_name": "Synthetic Browser", "title": title, "bundle_id": bundle},
        "ax_tree": {
            "apps": [
                {
                    "name": "Synthetic Browser",
                    "bundle_id": bundle,
                    "is_frontmost": True,
                    "windows": [
                        {
                            "focused": True,
                            "elements": [
                                {"role": "AXTextField", "value": url, "children": []},
                                {
                                    "role": "AXWebArea",
                                    "title": title,
                                    "children": children,
                                },
                            ],
                        }
                    ],
                }
            ]
        },
    }


@pytest.fixture
def tabbit():
    issue = {
        "role": "AXGroup",
        "children": [
            {
                "role": "AXHeading",
                "title": "feat(app): \u5347\u7ea7\u5931\u8d25\u56de\u6eda",
                "children": [],
            },
            {"role": "AXLink", "children": [_text("area:distribution")]},
            {"role": "AXLink", "children": [_text("type:tech-debt")]},
            _text("Status: Open."),
            _text("DemoUserX"),
            _text("opened"),
            _text("5 days ago"),
        ],
    }
    return _browser_capture(
        bundle=_TABBIT_BUNDLE,
        title="Issues · acme-dev/acme-mono",
        url="https://github.com/acme-dev/acme-mono/issues",
        children=[{"role": "AXList", "children": [issue]}],
    )


@pytest.fixture
def sun():
    return _browser_capture(
        bundle=_SUN_BUNDLE,
        title="Synthetic conversation - Claude",
        url="claude.ai/chat/00000000-0000-4000-8000-000000000000",
        children=[
            {"role": "AXHeading", "title": "You said:", "children": []},
            _text("How can a local Runtime preserve provenance?"),
            {"role": "AXHeading", "title": "Claude responded:", "children": []},
            _text("\u5bfb\u627e\u521b\u65b0\u8fd9\u4ef6\u4e8b\u672c\u8eab"),
            _text("\u53d8\u6210\u4e86\u4e00\u5957\u53ef\u5de5\u4e1a\u5316\u7684\u6d41\u7a0b\u3002"),
            _text(
                "\u63a8\u8350\u7b97\u6cd5\u4f5c\u4e3a\u53ef\u590d\u7528\u7684\u6838\u5fc3\u57fa\u7840\u8bbe\u65bd\u53ef\u4ee5\u4fdd\u7559\u5b57\u8282\u7ea7\u6765\u6e90\u8bb0\u5f55\u3002"
            ),
        ],
    )


def test_tabbit_url_and_title(tabbit):
    parser = BrowserParser()
    page = parser.parse(tabbit["ax_tree"], window_title=tabbit["window_meta"]["title"])
    assert isinstance(page, WebPage)
    assert page.title is not None
    assert "Issues · acme-dev/acme-mono" in page.title
    # URL comes from the address-bar TextField in the chrome (top-level url=None).
    assert page.url == "https://github.com/acme-dev/acme-mono/issues"
    rendered = page.render()
    assert 'url="https://github.com/acme-dev/acme-mono/issues"' in rendered


def test_tabbit_issues_grouped_into_items(tabbit):
    """Each issue is one <item>: its title (heading) and its metadata stay
    together — not flattened into a flat run of sibling <text> blocks."""
    parser = BrowserParser()
    page = parser.parse(tabbit["ax_tree"], window_title=tabbit["window_meta"]["title"])
    assert page is not None

    # Find the item whose heading is the first issue title.
    issue = next(
        (
            it
            for it in page.items
            if it.heading and "\u5347\u7ea7\u5931\u8d25\u56de\u6eda" in it.heading
        ),
        None,
    )
    assert issue is not None, "issue title did not become an item heading"
    # Its metadata (labels, status, author, time) lives in this item's lines —
    # the title and meta are grouped, not scattered across the page.
    body = " ".join(issue.lines)
    assert "area:distribution" in body
    assert "type:tech-debt" in body
    assert "Status: Open." in body
    assert "DemoUserX" in body
    assert "5 days ago" in body


def test_tabbit_fragments_merged_not_split(tabbit):
    """An issue's metadata fragments (split by AX into "#" / "200" / author /
    "opened" / "5 days ago") are re-joined into one ``· ``-line, not scattered
    into separate <text> blocks."""
    parser = BrowserParser()
    page = parser.parse(tabbit["ax_tree"], window_title=tabbit["window_meta"]["title"])
    assert page is not None
    issue = next(
        it
        for it in page.items
        if it.heading and "\u5347\u7ea7\u5931\u8d25\u56de\u6eda" in it.heading
    )
    # The status/author/time fragments live on a single merged line.
    meta = next((ln for ln in issue.lines if "DemoUserX" in ln), None)
    assert meta is not None
    assert " · " in meta  # fragments were joined, not emitted separately
    assert "opened" in meta and "5 days ago" in meta
    # No bare single-token fragment survives as its own line.
    assert "opened" not in issue.lines
    assert "5 days ago" not in issue.lines


def test_tabbit_navigation_clusters_dropped(tabbit):
    """The repo-tab / breadcrumb / toolbar nav clusters are discarded — only the
    issue records (and content sections) survive."""
    parser = BrowserParser()
    page = parser.parse(tabbit["ax_tree"], window_title=tabbit["window_meta"]["title"])
    assert page is not None
    blob = page.render()
    # These are pure navigation chrome inside the web area and must be gone.
    for nav in ("Skip to content", "Repository navigation", "Pull requests", "New issue"):
        assert nav not in blob, f"nav chrome leaked: {nav!r}"
    # Every kept item is a real record: it has a content heading or real prose.
    assert all(it.heading or any(len(ln) >= 20 for ln in it.lines) for it in page.items)


def test_sun_url_and_conversation_items(sun):
    parser = BrowserParser()
    page = parser.parse(sun["ax_tree"], window_title=sun["window_meta"]["title"])
    assert isinstance(page, WebPage)
    assert page.title is not None and "Claude" in page.title
    # SunBrowser address bar strips the scheme — still recognized as a URL.
    assert page.url is not None and "claude.ai/chat/" in page.url

    headings = [it.heading for it in page.items if it.heading]
    # Each conversation turn is its own item with a "You said:" / "responded" head.
    assert any(h.startswith("You said:") for h in headings)
    assert any("responded" in h for h in headings)
    # The answer prose is grouped under its turn (a body line survives).
    answer = next(it for it in page.items if it.heading and "responded" in it.heading)
    assert any("\u5b57\u8282" in ln for ln in answer.lines)


def test_sun_cjk_prose_joined_seamlessly(sun):
    """A CJK answer sentence the browser split into runs is re-joined without a
    `·` appearing mid-sentence."""
    parser = BrowserParser()
    page = parser.parse(sun["ax_tree"], window_title=sun["window_meta"]["title"])
    assert page is not None
    answer = next(it for it in page.items if it.heading and "responded" in it.heading)
    # This sentence was split across two fragments — it must now be contiguous.
    assert any(
        "\u5bfb\u627e\u521b\u65b0\u8fd9\u4ef6\u4e8b\u672c\u8eab\u53d8\u6210\u4e86\u4e00\u5957\u53ef\u5de5\u4e1a\u5316\u7684\u6d41\u7a0b\u3002"
        in ln
        for ln in answer.lines
    )
    # No mid-sentence `·` glued into a CJK prose line.
    for ln in answer.lines:
        if (
            "\u63a8\u8350\u7b97\u6cd5\u4f5c\u4e3a\u53ef\u590d\u7528\u7684\u6838\u5fc3\u57fa\u7840\u8bbe\u65bd"
            in ln
        ):
            assert "·" not in ln


def test_sun_sidebar_dropped(sun):
    """The claude.ai left sidebar (New chat / Projects / Recents …) is a nav
    cluster and must not appear — only the conversation turns survive."""
    parser = BrowserParser()
    page = parser.parse(sun["ax_tree"], window_title=sun["window_meta"]["title"])
    assert page is not None
    blob = page.render()
    for nav in ("New chat", "Projects", "Artifacts", "Customize"):
        assert nav not in blob, f"sidebar nav leaked: {nav!r}"
    # Every kept item is a conversation turn (content heading) or real prose.
    assert all(it.heading or any(len(ln) >= 20 for ln in it.lines) for it in page.items)


def test_budget_is_bounded(sun):
    parser = BrowserParser()
    page = parser.parse(sun["ax_tree"], window_title=sun["window_meta"]["title"])
    assert page is not None
    assert len(page.items) <= BrowserParser.MAX_ITEMS
    total = sum(
        (len(it.heading) if it.heading else 0) + sum(len(ln) for ln in it.lines)
        for it in page.items
    )
    # Total stays within budget (small slack for the last item committed).
    assert total <= BrowserParser.MAX_TOTAL_CHARS + BrowserParser.MAX_LINE_CHARS


# --------------------------------------------------------------------------- #
# URL extraction from the chrome (outside the AXWebArea)                       #
# --------------------------------------------------------------------------- #


def test_looks_like_url():
    assert _looks_like_url("https://github.com/acme-dev/acme-mono/issues")
    assert _looks_like_url("http://example.com")
    assert _looks_like_url("claude.ai/chat/9c3f0008-e3b1-4757")  # scheme stripped
    assert _looks_like_url("example.com")
    assert not _looks_like_url("Write a message…")
    assert not _looks_like_url("search query here")
    assert not _looks_like_url("localhost")  # no dot, no scheme
    assert not _looks_like_url("")


def _browser_tree_with_textfield(value: str) -> dict:
    return {
        "apps": [
            {
                "bundle_id": _TABBIT_BUNDLE,
                "is_frontmost": True,
                "name": "Tabbit Browser",
                "windows": [
                    {
                        "focused": True,
                        "elements": [
                            {"role": "AXTextField", "value": value, "children": []},
                            {
                                "role": "AXWebArea",
                                "title": "Some Page",
                                "children": [{"role": "AXStaticText", "value": "hello body text"}],
                            },
                        ],
                    }
                ],
            }
        ]
    }


def test_url_extracted_from_chrome_textfield():
    parser = BrowserParser()
    page = parser.parse(_browser_tree_with_textfield("https://example.com/path"), window_title=None)
    assert isinstance(page, WebPage)
    assert page.url == "https://example.com/path"


def test_url_none_when_no_address_bar():
    parser = BrowserParser()
    page = parser.parse(_browser_tree_with_textfield("just a search term"), window_title=None)
    assert isinstance(page, WebPage)
    assert page.url is None
    assert "url=" not in page.render()


# --------------------------------------------------------------------------- #
# _axtree web-area helper                                                     #
# --------------------------------------------------------------------------- #


def test_frontmost_web_area_finds_area(tabbit):
    wa = ax.frontmost_web_area(tabbit["ax_tree"], BrowserParser.bundle_ids)
    assert wa is not None
    assert wa.get("role") == "AXWebArea"
    assert wa.get("title") == "Issues · acme-dev/acme-mono"


def test_frontmost_web_area_none_for_no_browser():
    tree = {"apps": [{"bundle_id": "com.electron.lark", "windows": []}]}
    assert ax.frontmost_web_area(tree, BrowserParser.bundle_ids) is None

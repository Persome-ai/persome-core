"""Generic browser detection (capture/browser_detect.py).

Replaces the two hardcoded ``_BROWSER_BUNDLES`` allowlists. Fixtures mirror the
real AX structures measured on this machine:

  * Tabbit (niche browser): http handler + AXWebArea + a URL-valued address bar.
  * cmux (Electron terminal): IS an http handler + has an AXWebArea, but NO
    address bar — must NOT be classified a browser.
  * VSCode/Feishu/Claude (Electron): AXWebArea but NOT http handlers.
  * System Settings (contaminated capture): a URL address bar leaked in, but not
    an http handler — the LaunchServices gate rescues this false positive.
  * WeChat: empty AX tree — invisible to AX (handled by OCR elsewhere).
"""

from __future__ import annotations

import pytest

from persome.capture import browser_detect as bd


@pytest.fixture(autouse=True)
def _pin_handlers():
    """Pin the LaunchServices handler set so tests never shell out to the helper.
    Includes cmux + iTerm2 deliberately — they really do register as handlers."""
    bd.set_http_handlers_for_test(
        {
            "com.tab-browser.Tabbit",
            "com.google.Chrome",
            "com.apple.Safari",
            "com.cmuxterm.app",  # terminal that also registers http
            "com.googlecode.iterm2",  # terminal that also registers http
        }
    )
    yield
    bd.set_http_handlers_for_test(None)


def _app(bundle, elements):
    return {
        "apps": [{"bundle_id": bundle, "is_frontmost": True, "windows": [{"elements": elements}]}]
    }


_WEBAREA = {
    "role": "AXWebArea",
    "title": "Page",
    "children": [{"role": "AXStaticText", "domClassList": ["x"]}],
}
_ADDRBAR = {
    "role": "AXTextField",
    "value": "https://www.bilibili.com/video/BV1x",
    "description": "\u5730\u5740\u548c\u641c\u7d22\u680f",
}

TABBIT = _app(
    "com.tab-browser.Tabbit",
    [{"role": "AXToolbar", "children": [_ADDRBAR]}, _WEBAREA, {"role": "AXTabGroup"}],
)
CMUX = _app("com.cmuxterm.app", [_WEBAREA])  # web content, no address bar
VSCODE = _app("com.microsoft.VSCode", [_WEBAREA])  # not an http handler
SYSSETTINGS = _app("com.apple.systempreferences", [_WEBAREA, _ADDRBAR])  # contaminated
WECHAT = {"apps": [{"bundle_id": "com.tencent.xinWeChat", "is_frontmost": True, "windows": []}]}


class TestHttpHandlerClassifier:
    def test_registered_browser_is_browser_app(self):
        assert bd.is_browser_app("com.tab-browser.Tabbit") is True
        assert bd.is_browser_app("com.google.Chrome") is True

    def test_terminals_excluded_even_if_registered(self):
        # cmux + iTerm2 are in the handler set but are terminals → not browsers.
        assert bd.is_browser_app("com.cmuxterm.app") is False
        assert bd.is_browser_app("com.googlecode.iterm2") is False

    def test_non_handler_is_not_browser_app(self):
        assert bd.is_browser_app("com.microsoft.VSCode") is False
        assert bd.is_browser_app("com.tencent.xinWeChat") is False
        assert bd.is_browser_app(None) is False

    def test_fallback_to_known_set_when_helper_unavailable(self):
        bd.set_http_handlers_for_test(None)  # clear → http_handler_bundles falls back
        # The live query will be empty in tests (no helper) → static known set.
        assert "com.tab-browser.Tabbit" in bd.http_handler_bundles()
        assert bd.is_browser_app("com.tab-browser.Tabbit") is True


class TestAxSignals:
    def test_has_web_content_axwebarea(self):
        assert bd.has_web_content(TABBIT) is True
        assert bd.has_web_content(CMUX) is True  # AXWebArea present (Electron)

    def test_has_web_content_dom_attr_only(self):
        tree = _app(
            "x.y",
            [{"role": "AXGroup", "children": [{"role": "AXStaticText", "domIdentifier": "n1"}]}],
        )
        assert bd.has_web_content(tree) is True

    def test_no_web_content(self):
        assert bd.has_web_content(WECHAT) is False
        plain = _app("com.apple.finder", [{"role": "AXButton"}, {"role": "AXStaticText"}])
        assert bd.has_web_content(plain) is False

    def test_address_bar_url_full_and_bare(self):
        assert bd.address_bar_url(TABBIT) == "https://www.bilibili.com/video/BV1x"
        bare = _app("b", [{"role": "AXTextField", "value": "claude.ai/chat/123"}])
        assert bd.address_bar_url(bare) == "https://claude.ai/chat/123"

    def test_no_address_bar(self):
        assert bd.address_bar_url(CMUX) is None
        # a non-URL text field is not an address bar
        search = _app("s", [{"role": "AXTextField", "value": "hello world"}])
        assert bd.address_bar_url(search) is None

    def test_looks_like_browser(self):
        assert bd.looks_like_browser(TABBIT) is True
        assert bd.looks_like_browser(CMUX) is False  # web content but no address bar


class TestIsBrowserDecision:
    def test_tabbit_is_browser(self):
        assert bd.is_browser(TABBIT, "com.tab-browser.Tabbit") is True

    def test_cmux_terminal_not_browser(self):
        # http handler BUT terminal-denylisted; fallback also fails (no address bar).
        assert bd.is_browser(CMUX, "com.cmuxterm.app") is False

    def test_vscode_electron_not_browser(self):
        # not an http handler; fallback fails (web content but no address bar).
        assert bd.is_browser(VSCODE, "com.microsoft.VSCode") is False

    def test_wechat_not_browser(self):
        assert bd.is_browser(WECHAT, "com.tencent.xinWeChat") is False

    def test_registered_browser_without_web_content_is_not_a_page(self):
        # Tabbit frontmost but no web area captured this frame → not a parseable page.
        empty_tabbit = {
            "apps": [{"bundle_id": "com.tab-browser.Tabbit", "is_frontmost": True, "windows": []}]
        }
        assert bd.is_browser(empty_tabbit, "com.tab-browser.Tabbit") is False

    def test_unregistered_with_full_chrome_uses_fallback_only_when_ls_unavailable(self):
        unknown = _app(
            "com.example.NicheBrowser", [{"role": "AXToolbar", "children": [_ADDRBAR]}, _WEBAREA]
        )
        # LaunchServices live + bundle not listed → authoritative NO (no fallback).
        assert bd.is_browser(unknown, "com.example.NicheBrowser") is False
        # LaunchServices unavailable (cleared; test helper can't run) → AX fallback kicks in.
        bd.set_http_handlers_for_test(None)
        assert bd.is_browser(unknown, "com.example.NicheBrowser") is True

    def test_contaminated_native_app_not_browser_when_ls_live(self):
        # System Settings (not an http handler) with a leaked address bar must NOT
        # be flagged a browser while LaunchServices is authoritative — the real
        # false positive the live gate fixes.
        assert bd.is_browser(SYSSETTINGS, "com.apple.systempreferences") is False


class TestParserDispatch:
    """parser_for_capture: registry fast-path, then BrowserParser for a detected
    (possibly UNLISTED) browser — the headline behavior the generic detector buys."""

    def test_unlisted_http_handler_browser_routes_to_browser_parser(self):
        from persome import parsers
        from persome.parsers.web import BrowserParser

        # An http handler in NO registry and NO _KNOWN_BROWSERS list (e.g. Doubao).
        bd.set_http_handlers_for_test({"com.bot.pc.doubao.browser"})
        unlisted = "com.bot.pc.doubao.browser"
        tree = _app(unlisted, [{"role": "AXToolbar", "children": [_ADDRBAR]}, _WEBAREA])
        assert parsers.get_parser(unlisted) is None  # not registered
        assert isinstance(parsers.parser_for_capture(unlisted, tree), BrowserParser)

    def test_non_browser_unregistered_app_routes_to_none(self):
        from persome import parsers

        bd.set_http_handlers_for_test({"com.tab-browser.Tabbit"})
        # VSCode: not registered, not an http handler, web content but no address bar.
        assert parsers.parser_for_capture("com.microsoft.VSCode", VSCODE) is None

    def test_no_crash_on_none_ax_tree(self):
        from persome import parsers
        from persome.parsers import _axtree

        assert _axtree.frontmost_app(None) is None
        # Unregistered, non-browser bundle + no ax_tree → None (and no crash).
        assert parsers.parser_for_capture("com.unknown.app", None) is None
        # A registered browser still resolves via the registry even with no ax_tree.
        from persome.parsers.web import BrowserParser

        assert isinstance(parsers.parser_for_capture("com.tab-browser.Tabbit", None), BrowserParser)

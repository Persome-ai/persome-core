"""Generic browser detection — replaces the hardcoded ``_BROWSER_BUNDLES`` allowlists.

The old approach (a literal set of browser bundle ids in ``s1_parser`` and
``parsers/web``) can't see a niche browser like Tabbit, and never an in-app
web view. This module classifies *any* app from signals instead of a list:

1. **LaunchServices http/https handler set** (primary, app-level). Only apps
   registered to open ``http(s)`` URLs are surfaced by the bundled
   ``mac-url-handlers`` Swift helper — that auto-covers Tabbit / Arc / Vivaldi /
   any future browser with zero maintenance. Caveat measured on real machines:
   a few *terminals* (cmux, iTerm2) also register an http handler, so they're
   subtracted via a tiny, stable ``_TERMINAL_BUNDLES`` denylist (cheaper and far
   more stable than enumerating every browser).
2. **AX structural** (secondary, per-capture): an ``AXWebArea`` / web-DOM node
   means a web page is actually on screen *right now*; a URL-valued address-bar
   ``AXTextField`` is browser chrome. Used to confirm the live surface and as a
   fallback when (1) is unavailable.

Decision (``is_browser``): a registered handler (minus terminals) **showing web
content**, else the AX chrome fallback. This excludes Electron apps that merely
embed an ``AXWebArea`` (VSCode, Feishu, Claude — none are http handlers) and the
terminals that do register one, while accepting Tabbit and friends.

Detecting an in-app browser inside an **AX-barren** app (e.g. WeChat exposes an
empty AX tree) is out of scope here — that needs the OCR/visual path, not AX.

Pure stdlib + a read-only LaunchServices query (no entitlements / no AX or
Screen-Recording permission). Safe to import anywhere; no dependency on
``parsers`` (keeps the capture→parsers direction acyclic).
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from ..logger import get

logger = get("persome.capture.browser_detect")

# Static fallback set (union of the two legacy ``_BROWSER_BUNDLES``) used only
# when the LaunchServices helper is unavailable (non-Darwin, helper missing,
# tests). Live detection does NOT depend on this list being complete.
_KNOWN_BROWSERS = frozenset(
    {
        "com.google.Chrome",
        "com.google.Chrome.canary",
        "com.apple.Safari",
        "org.mozilla.firefox",
        "com.microsoft.edgemac",
        "company.thebrowser.Browser",  # Arc
        "com.brave.Browser",
        "com.operasoftware.Opera",
        "com.vivaldi.Vivaldi",
        "org.chromium.Chromium",
        "com.tab-browser.Tabbit",
        "com.adspower.SunBrowser",
    }
)

# Terminals / non-browsers that ALSO register as http handlers (measured: cmux,
# iTerm2). Small + stable category — subtracting it is far cheaper than keeping
# the browser allowlist complete. cmux additionally has its own capture path.
_TERMINAL_BUNDLES = frozenset(
    {
        "com.cmuxterm.app",
        "com.googlecode.iterm2",
        "com.apple.Terminal",
        "dev.warp.Warp-Stable",
        "io.alacritty",
        "net.kovidgoyal.kitty",
        "com.github.wez.wezterm",
    }
)

# An address-bar value looks like a URL: explicit http(s):// scheme, or a bare
# ``host/path`` the browser shows with the scheme stripped. The bare form must
# start with a domain-ish token so an arbitrary text field isn't mistaken for
# the address bar. (Same shape as parsers/web.py's _URL_RE.)
_URL_RE = re.compile(r"^(?:https?://\S+|[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?:[/:?#]\S*)?)$")

# Web-DOM attribute keys the AX helper attaches only to rendered web nodes.
_WEB_DOM_ATTRS = ("domClassList", "domIdentifier")

_HANDLER_TTL_SECONDS = 600  # re-query LaunchServices at most every 10 min
# (timestamp, bundle set, live): ``live`` is True when the set came from the
# actual LaunchServices helper (authoritative) vs the static fallback.
_handler_cache: tuple[float, frozenset[str], bool] | None = None


# ─── LaunchServices http-handler set ─────────────────────────────────────────


def _helper_path() -> Path | None:
    """Resolve the bundled ``mac-url-handlers`` binary (compile-on-demand from
    its ``.swift`` like the AX helpers). None off-Darwin / when unresolved."""
    if platform.system() != "Darwin":
        return None
    override = os.environ.get("PERSOME_URL_HELPER")
    if override:
        p = Path(override).expanduser()
        return p if p.is_file() else None
    try:
        from importlib.resources import files as _pkg_files

        bundled = Path(str(_pkg_files("persome").joinpath("_bundled"))) / "mac-url-handlers"
    except (ModuleNotFoundError, ValueError):
        bundled = None
    dev = Path(__file__).resolve().parents[2] / "resources" / "mac-url-handlers"
    for binary in (bundled, dev):
        if binary is None:
            continue
        swift = binary.with_suffix(".swift")
        if binary.is_file() and os.access(binary, os.X_OK):
            return binary
        if swift.is_file():
            _maybe_compile(swift, binary)
            if binary.is_file() and os.access(binary, os.X_OK):
                return binary
    return None


def _maybe_compile(swift: Path, binary: Path) -> None:
    if binary.is_file() and binary.stat().st_mtime >= swift.stat().st_mtime:
        return
    try:
        subprocess.run(
            ["swiftc", str(swift), "-o", str(binary), "-O", "-swift-version", "5"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:  # noqa: BLE001
        logger.warning("mac-url-handlers compile failed: %s", exc)


def _query_http_handlers() -> frozenset[str]:
    """Run the helper → the set of http/https handler bundle ids. Empty on any
    failure (caller falls back to the static set)."""
    helper = _helper_path()
    if helper is None:
        return frozenset()
    try:
        res = subprocess.run([str(helper)], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:  # noqa: BLE001
        logger.warning("mac-url-handlers run failed: %s", exc)
        return frozenset()
    ids = {ln.strip() for ln in res.stdout.splitlines() if ln.strip()}
    return frozenset(ids)


def http_handler_bundles() -> frozenset[str]:
    """Cached set of http/https handler bundle ids (TTL-refreshed). Falls back to
    the static known-browser set when the live query yields nothing."""
    global _handler_cache
    now = time.monotonic()
    if _handler_cache is not None and now - _handler_cache[0] < _HANDLER_TTL_SECONDS:
        return _handler_cache[1]
    live = _query_http_handlers()
    result = live if live else _KNOWN_BROWSERS
    _handler_cache = (now, result, bool(live))
    return result


def _handlers_are_live() -> bool:
    """True when the cached handler set came from the real LaunchServices helper
    (authoritative) rather than the static fallback. Ensures the cache is warm."""
    http_handler_bundles()
    return bool(_handler_cache and _handler_cache[2])


def set_http_handlers_for_test(bundles: frozenset[str] | set[str] | None) -> None:
    """Test seam: pin (or clear) the cached handler set without running the
    helper. A pinned set counts as ``live`` (authoritative)."""
    global _handler_cache
    _handler_cache = None if bundles is None else (time.monotonic(), frozenset(bundles), True)


def is_browser_app(bundle_id: str | None) -> bool:
    """App-level: ``bundle_id`` is a registered http(s) handler and not a known
    terminal. Definitive for real browsers regardless of what's on screen."""
    if not bundle_id or bundle_id in _TERMINAL_BUNDLES:
        return False
    return bundle_id in http_handler_bundles()


# ─── AX structural signals (operate on the captured ax_tree) ─────────────────


def _frontmost_app(ax_tree: dict[str, Any], bundle_id: str | None) -> dict[str, Any] | None:
    """The app dict to inspect: the one matching ``bundle_id`` if given, else the
    frontmost, else the first. Accepts a full ax_tree (``{apps: [...]}``) or a
    single app dict."""
    apps = ax_tree.get("apps") if isinstance(ax_tree, dict) else None
    if apps is None:  # already a single app dict (s1_parser passes app_data)
        return ax_tree if isinstance(ax_tree, dict) else None
    match = first = front = None
    for app in apps:
        if not isinstance(app, dict):
            continue
        first = first or app
        if app.get("is_frontmost"):
            front = front or app
        if bundle_id and app.get("bundle_id") == bundle_id:
            match = match or app
    return match or front or first


def _walk(node: Any):
    """Yield every dict node in an AX subtree (children/elements/windows)."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            for k in ("children", "elements", "windows"):
                v = cur.get(k)
                if v:
                    stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)


def has_web_content(ax_tree: dict[str, Any], bundle_id: str | None = None) -> bool:
    """True if a rendered web page is on screen: an ``AXWebArea`` node, or any
    node carrying a web-DOM attribute (those only exist on web content)."""
    app = _frontmost_app(ax_tree, bundle_id)
    if app is None:
        return False
    for n in _walk(app):
        if n.get("role") == "AXWebArea":
            return True
        if any(attr in n for attr in _WEB_DOM_ATTRS):
            return True
    return False


def address_bar_url(ax_tree: dict[str, Any], bundle_id: str | None = None) -> str | None:
    """Return the URL from a browser-chrome address bar, or None. An address bar
    is an ``AXTextField`` whose value matches a URL (optionally confirmed by a
    description/subrole that names it an address bar)."""
    app = _frontmost_app(ax_tree, bundle_id)
    if app is None:
        return None
    for n in _walk(app):
        if n.get("role") != "AXTextField":
            continue
        value = (n.get("value") or "").strip()
        if value and _URL_RE.match(value):
            return value if value.startswith("http") else f"https://{value}"
    return None


def looks_like_browser(ax_tree: dict[str, Any], bundle_id: str | None = None) -> bool:
    """AX-only fallback: a web page AND a URL-valued address bar are present.
    Distinguishes a browser window from a terminal/Electron app that merely
    embeds an ``AXWebArea`` (those have no address bar)."""
    return has_web_content(ax_tree, bundle_id) and address_bar_url(ax_tree, bundle_id) is not None


def is_browser(ax_tree: dict[str, Any] | None, bundle_id: str | None) -> bool:
    """Is the current capture a browser web page? A registered http handler
    (minus terminals) that's actually showing web content.

    When LaunchServices is available it is authoritative: a non-handler is NOT a
    browser (so a contaminated capture of a native app that happens to contain a
    leaked address bar isn't mis-flagged). The AX-chrome fallback
    (``looks_like_browser``) is used ONLY when LaunchServices can't answer at all
    (non-Darwin / helper missing)."""
    tree = ax_tree or {}
    if is_browser_app(bundle_id):
        return has_web_content(tree, bundle_id)
    if _handlers_are_live():
        return False  # LaunchServices is authoritative — not a registered browser
    return looks_like_browser(tree, bundle_id)

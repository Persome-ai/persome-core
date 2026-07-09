"""Generic browser (Chromium-family) AX-tree parser.

Browsers have no app-specific DOM contract we can anchor on the way Feishu's
Electron client does — every site renders a different tree. So this parser is
deliberately **site-agnostic**: it never special-cases claude.ai or GitHub. It
turns the rendered page into a clean, *structured* ``WebPage`` (page identity +
grouped content items) purely from generic accessibility semantics, raising the
quality of the ``focus_structured`` text the recognizer reads instead of the raw
10k-char flattened ``visible_text`` DOM dump (navigation + body + browser chrome
mashed together, every line split into a separate fragment).

What it relies on (all generic, none per-site):

- **URL** = a node in the browser *chrome* whose value looks like a URL —
  typically the address-bar ``AXTextField``. The address bar lives **outside**
  the ``AXWebArea``, so we scan the whole frontmost window for it (text field
  first, then any node value). Two steps, kept separate: find the URL in the
  chrome, then scope to the web area for everything else.
- **Page identity** = the ``AXWebArea`` node's ``title`` (e.g.
  ``Issues · acme-dev/acme-mono`` or ``… - Claude``).
- **Structure** = generic AX roles inside the ``AXWebArea``, in three steps:
  1. **Group** into items. ``AXList`` children that are *rich* (carry a heading
     or several lines) are each one item — that is how a list of GitHub issues
     becomes one ``<item>`` per issue. Elsewhere an ``AXHeading`` opens a new
     item and the following text accrues to it (a ``You said:`` /
     ``Claude responded:`` turn; a headed section).
  2. **Merge** fragments. The browser splits one visual line
     (``# 200 · DemoUserX opened …``, ``Sort by Newest, descending``) into many
     ``AXStaticText`` nodes; consecutive short ones are re-joined into one line.
  3. **Drop navigation clusters.** An item whose body is dominated by the labels
     of interactive controls (links / buttons) and has no running prose is a nav
     rail — a breadcrumb, a repo-tab row, a toolbar, the claude.ai sidebar — and
     is discarded whole. This is a generic provenance heuristic (text inside an
     ``AXLink`` / ``AXButton`` ancestor), not a per-site class/name match.

Scoping to the ``AXWebArea`` subtree drops the browser's own chrome
(back/forward, bookmarks bar, extensions, tabs) for free — it lives in sibling
nodes; the nav-cluster drop then removes the in-page navigation rails. Quality
filters are minimal and site-agnostic: drop pure-glyph fragments, collapse a
heading's echoed static-text. A per-item / total budget keeps a huge page (the
Claude fixture has 2500+ text nodes) bounded.

No ``AXWebArea`` / no readable content → ``parse`` returns ``None`` and the
caller falls back to the raw ``focus_excerpt`` (#258).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import _axtree as ax
from .base import Parser, StructuredContent, _esc_attr, _esc_text

# Chromium-family browser bundle ids this parser handles. Generic across the
# family — the parsing logic is identical for all of them (same AX semantics).
_BROWSER_BUNDLES = frozenset(
    {
        "com.tab-browser.Tabbit",
        "com.adspower.SunBrowser",
        "com.google.Chrome",
        "com.google.Chrome.canary",
        "com.brave.Browser",
        "com.microsoft.edgemac",
        "com.vivaldi.Vivaldi",
        "com.operasoftware.Opera",
        "org.chromium.Chromium",
    }
)

# A fragment of pure punctuation / digits / symbols (e.g. "(", "/", "·", "#",
# "7" → dropped; ", descending" → kept). Single characters are also dropped.
_NOISE_RE = re.compile(r"^[\W\d_]+$")

# An address-bar value looks like a URL: explicit http(s):// scheme, or a bare
# ``host/path`` the browser shows with the scheme stripped (e.g.
# ``claude.ai/chat/…``). The bare form must start with a domain-ish token
# (label.label) so we don't mistake an arbitrary text field for the address bar.
_URL_RE = re.compile(r"^(?:https?://\S+|[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?:[/:?#]\S*)?)$")

# Short text fragments (≤ this many chars) that are consecutive within one item
# are joined into a single line; longer fragments (real prose) stay on their own
# line. Keeps a GitHub issue's metadata on one or two compact lines while
# preserving a Claude answer's paragraphs as distinct lines.
_SHORT_LINE_CHARS = 45

# Two separators, chosen per adjacent fragment pair (see ``_merge_lines``):
#   - discrete short labels / meta (``area:distribution``, ``opened``, ``Open``)
#     are independent values → joined with ``· `` to read as a list.
#   - prose the browser split mid-sentence (a CJK paragraph chopped into runs by
#     inline styling) → joined SEAMLESSLY so it reads as one sentence again.
_LABEL_JOIN = " · "
_PROSE_JOIN = ""

# CJK ideographs + CJK punctuation. A fragment boundary touching CJK text is a
# mid-sentence split (prose continuation), not a list of discrete labels — Latin
# labels / meta never contain these, CJK prose always does.
_CJK_RE = re.compile(r"[　-〿㐀-鿿＀-￯]")

# A list child is treated as its own ``<item>`` only when it is "rich" — it
# carries a heading or has at least this many lines. A single short nav link in
# a breadcrumb / tab list does not qualify (it stays inline), so only genuine
# records (issue rows, conversation turns) become items.
_RICH_ITEM_MIN_LINES = 3

# Roles whose label is an interactive control — navigation chrome, not page
# prose. Text inside one of these is "nav-provenance"; an item dominated by such
# text (and lacking a content heading / long prose) is dropped as a nav cluster.
_NAV_ROLES = frozenset(
    {
        "AXLink",
        "AXButton",
        "AXPopUpButton",
        "AXMenuButton",
        "AXMenuItem",
        "AXCheckBox",
        "AXRadioButton",
        "AXComboBox",
        "AXTab",
    }
)

# An item with no long prose whose body fragments are this fraction (or more)
# from interactive controls is judged a navigation cluster and dropped. Tuned
# against the fixtures: a repo-tab / breadcrumb / sidebar row is ~0.75–1.0 nav;
# a GitHub issue record is ~0.4–0.65 (its title + labels are links, but status /
# author / time are bare text), so 0.7 cleanly separates nav rails from records.
_NAV_FRACTION = 0.7


def _label_of(node: ax.Node) -> str:
    """A node's accessible label (title, then description)."""
    title = (node.get("title") or "").strip()
    if title:
        return title
    return (node.get("description") or "").strip()


def _is_noise(text: str) -> bool:
    """True when ``text`` is a single char or pure punctuation/symbol glyph."""
    return len(text) <= 1 or bool(_NOISE_RE.match(text))


def _looks_like_url(value: str) -> bool:
    """True when ``value`` is an address-bar URL (with or without a scheme)."""
    v = value.strip()
    if not v or " " in v:
        return False
    return bool(_URL_RE.match(v))


@dataclass(frozen=True)
class WebItem:
    """One structured content unit of a page.

    ``heading`` is the unit's title (a GitHub issue title, a ``You said:`` turn)
    or ``None`` for a headless run of text. ``lines`` are the merged body lines
    under it (each line is one or more re-joined sibling fragments).
    """

    heading: str | None
    lines: tuple[str, ...]


@dataclass(frozen=True)
class WebPage:
    """Clean, structured view of one browser page at capture time.

    ``title`` is the page identity (the ``AXWebArea`` title). ``url`` is the
    address-bar URL (``None`` when not found). ``items`` are the grouped content
    units in document order (issue rows, conversation turns, headed sections),
    each a :class:`WebItem` — this is what preserves the page's structure instead
    of flattening it into a soup of ``<text>``.

    Frozen value object — cheap to cache / compare, like ``ParsedConversation``.
    """

    app: str
    title: str | None
    items: tuple[WebItem, ...]
    url: str | None = None
    parser_version: str = "browser-2"

    def render(self) -> str:
        """Render to the ``focus_structured`` XML fed to the recognizer.

        Emits a single ``<web_page>`` element (XML — explicit boundaries beat
        ad-hoc delimiters for an LLM). ``app`` is always present; ``url`` and
        ``title`` are emitted only when known. Each content unit is an
        ``<item>`` carrying its ``<heading>`` (when present) and ``<text>``
        lines::

            <web_page app="Tabbit浏览器" url="https://github.com/…/issues" title="Issues · …">
            <item>
            <heading>feat(app): 升级失败回滚</heading>
            <text>area:distribution · type:tech-debt · Status: Open.</text>
            <text>#200 · DemoUserX opened 5 days ago</text>
            </item>
            </web_page>

        All text/attributes are XML-escaped. Returns ``""`` when there is no
        content at all (the caller then falls back to the raw excerpt).
        """
        if not self.items:
            return ""

        attrs = f" app={_esc_attr(self.app)}"
        url = (self.url or "").strip()
        if url:
            attrs += f" url={_esc_attr(url)}"
        title = (self.title or "").strip()
        if title:
            attrs += f" title={_esc_attr(title)}"

        lines: list[str] = [f"<web_page{attrs}>"]
        for item in self.items:
            lines.append("<item>")
            if item.heading:
                lines.append(f"<heading>{_esc_text(item.heading)}</heading>")
            for line in item.lines:
                lines.append(f"<text>{_esc_text(line)}</text>")
            lines.append("</item>")
        lines.append("</web_page>")
        return "\n".join(lines)


class BrowserParser(Parser):
    """Generic Chromium-family browser parser (site-agnostic).

    Like every parser it declares its handled ``bundle_ids`` and a ``version``;
    ``parse`` returns a ``WebPage`` (a :class:`StructuredContent`) rather than a
    ``ParsedConversation`` — both satisfy the protocol the callers depend on.
    """

    bundle_ids = _BROWSER_BUNDLES
    version = "browser-2"

    # Budget: a page must never produce an unbounded blob. Roughly the timeline
    # aggregator's per-window scale — enough to recognize what the page is about,
    # capped so a 2500-node page can't blow the recognizer's context.
    MAX_ITEMS = 40
    MAX_LINES_PER_ITEM = 30
    MAX_TOTAL_CHARS = 6000
    # A single line longer than this is truncated (one runaway prose node must
    # not eat the whole char budget by itself).
    MAX_LINE_CHARS = 800

    def parse(self, ax_tree: dict, *, window_title: str | None) -> StructuredContent | None:
        # Step 1: the URL lives in the browser chrome (address bar), OUTSIDE the
        # web area — scan the whole frontmost window for it.
        url = self._find_url(ax_tree)

        # Step 2: scope to the web area for the page identity + structured content.
        web_area = ax.frontmost_web_area(ax_tree)
        if web_area is None:
            return None

        items = self._collect_items(web_area)
        if not items:
            return None

        title = (web_area.get("title") or "").strip() or (window_title or None)
        app_name = self._app_name(ax_tree) or "Browser"
        return WebPage(
            app=app_name,
            title=title,
            items=tuple(items),
            url=url,
            parser_version=self.version,
        )

    # --- URL (browser chrome, outside the web area) ----------------------- #

    def _find_url(self, ax_tree: dict) -> str | None:
        """The page URL from the frontmost browser window's chrome.

        The address bar is *not* under the ``AXWebArea``, so we scan the whole
        frontmost window. Prefer an ``AXTextField`` whose value looks like a URL
        (the address bar); fall back to any node value that looks like a URL.
        Returns ``None`` when nothing matches (URL is best-effort).
        """
        elements = self._frontmost_window_elements(ax_tree)
        fallback: str | None = None
        for element in elements:
            for node in ax.walk(element):
                value = (node.get("value") or "").strip()
                if not _looks_like_url(value):
                    continue
                if node.get("role") == "AXTextField":
                    return value
                if fallback is None:
                    fallback = value
        return fallback

    def _frontmost_window_elements(self, ax_tree: dict) -> list[ax.Node]:
        """Elements of the frontmost browser window (chrome included).

        Bundle-agnostic (see ``ax.frontmost_app``): the dispatch already vouched
        this is a browser, so we read the frontmost surface regardless of bundle
        id — an unlisted browser's address bar is found just like Chrome's."""
        app = ax.frontmost_app(ax_tree)
        if app is None:
            return []
        windows = app.get("windows") or []
        if not windows:
            return []
        focused = next(
            (w for w in windows if isinstance(w, dict) and w.get("focused")),
            None,
        )
        window = focused or windows[0]
        if not isinstance(window, dict):
            return []
        return [n for n in (window.get("elements") or []) if isinstance(n, dict)]

    def _app_name(self, ax_tree: dict) -> str | None:
        """Human app name of the frontmost browser (for the XML attr)."""
        app = ax.frontmost_app(ax_tree)
        return ((app.get("name") or "").strip() or None) if app else None

    # --- structured content (inside the web area) ------------------------- #

    def _collect_items(self, web_area: ax.Node) -> list[WebItem]:
        """Group the web area's readable text into structured :class:`WebItem`s.

        Three steps, all generic (anchored on AX roles, never site names):

        1. **Group** into raw items by structure: each rich ``AXList`` child
           (issue row, conversation card) is one item; outside those, a heading
           opens a new item and the following text accrues to it.
        2. **Merge** each item's leaf fragments — the browser splits one visual
           line (``# 200 · DemoUserX opened …``) into many ``AXStaticText`` — back
           into ``· ``-joined lines.
        3. **Drop navigation clusters** — an item that is mostly the labels of
           interactive controls (links/buttons), with no real heading and no
           long prose, is a nav rail (breadcrumb, repo tabs, sidebar) and is
           discarded whole.

        Budgets bound the kept result.
        """
        raw = self._group_raw_items(web_area)

        items: list[WebItem] = []
        total = 0
        for heading, heading_is_nav, raw_lines in raw:
            if len(items) >= self.MAX_ITEMS or total >= self.MAX_TOTAL_CHARS:
                break
            if _is_nav_item(heading, heading_is_nav, raw_lines):
                continue
            merged = _merge_lines([text for text, _nav in raw_lines])
            merged = [
                (ln[: self.MAX_LINE_CHARS - 1] + "…") if len(ln) > self.MAX_LINE_CHARS else ln
                for ln in merged
            ]
            if heading is None and not merged:
                continue
            items.append(WebItem(heading=heading, lines=tuple(merged[: self.MAX_LINES_PER_ITEM])))
            total += (len(heading) if heading else 0) + sum(len(x) for x in merged)
        return items

    def _group_raw_items(
        self, web_area: ax.Node
    ) -> list[tuple[str | None, bool, list[tuple[str, bool]]]]:
        """Raw items before merge / nav-drop: ``(heading, heading_is_nav, lines)``.

        ``lines`` are ``(text, from_nav)`` where ``from_nav`` is true when the
        text node sits inside an interactive-control (``AXLink`` / ``AXButton`` /
        …) ancestor — the provenance signal a nav cluster is built from.
        ``heading_is_nav`` is true when the heading itself doubles as a control
        label (a sidebar section header like "Recents"/"Products"), as opposed to
        a content title (a GitHub issue title, a ``You said:`` turn).
        """
        rich_roots = self._rich_list_children(web_area)
        nav_labels = self._nav_control_labels(web_area)

        raw: list[tuple[str | None, bool, list[tuple[str, bool]]]] = []
        # The currently open heading-bounded item (heading, is_nav, lines).
        cur_heading: str | None = None
        cur_heading_nav = False
        cur_lines: list[tuple[str, bool]] = []
        prev: str | None = None
        emitted_rich: set[int] = set()

        def flush() -> None:
            nonlocal cur_heading, cur_heading_nav, cur_lines, prev
            if cur_heading is not None or cur_lines:
                raw.append((cur_heading, cur_heading_nav, cur_lines))
            cur_heading = None
            cur_heading_nav = False
            cur_lines = []
            prev = None

        def rec(node: ax.Node, under_nav: bool) -> None:
            nonlocal cur_heading, cur_heading_nav, cur_lines, prev
            nid = id(node)
            if nid in rich_roots and nid not in emitted_rich:
                flush()
                raw.append(rich_roots[nid])
                emitted_rich.add(nid)
                return
            role = node.get("role")
            here_nav = under_nav or role in _NAV_ROLES
            if role == "AXHeading":
                text = _label_of(node)
                if text and text != prev:
                    flush()
                    cur_heading = text
                    cur_heading_nav = text in nav_labels
                    prev = text
                return  # skip the heading's echoed static-text children
            if role == "AXStaticText":
                text = (node.get("value") or "").strip()
                if text and text != prev and not _is_noise(text):
                    cur_lines.append((text, here_nav))
                    prev = text
                return
            for child in node.get("children") or []:
                if isinstance(child, dict):
                    rec(child, here_nav)

        rec(web_area, False)
        flush()
        return raw

    def _rich_list_children(
        self, web_area: ax.Node
    ) -> dict[int, tuple[str | None, bool, list[tuple[str, bool]]]]:
        """Map ``id(node) -> (heading, heading_is_nav, lines)`` for rich list children.

        A "rich" child carries a heading or at least ``_RICH_ITEM_MIN_LINES``
        lines — a real record (issue row, conversation card), not a single
        breadcrumb / tab link. These roots are whole items, not re-descended.
        """
        nav_labels = self._nav_control_labels(web_area)
        out: dict[int, tuple[str | None, bool, list[tuple[str, bool]]]] = {}
        for node in ax.walk(web_area):
            if node.get("role") != "AXList":
                continue
            for child in node.get("children") or []:
                if not isinstance(child, dict):
                    continue
                heading, lines = _subtree_lines(child)
                if heading is not None or len(lines) >= _RICH_ITEM_MIN_LINES:
                    out[id(child)] = (heading, heading is not None and heading in nav_labels, lines)
        return out

    @staticmethod
    def _nav_control_labels(web_area: ax.Node) -> frozenset[str]:
        """Every interactive control's label inside the web area.

        Used to decide whether a *heading* doubles as navigation chrome (a
        sidebar section header that is also a button) vs a content title.
        """
        labels: set[str] = set()
        for node in ax.walk(web_area):
            if node.get("role") in _NAV_ROLES:
                label = _label_of(node)
                if label:
                    labels.add(label)
        return frozenset(labels)


def _subtree_lines(node: ax.Node) -> tuple[str | None, list[tuple[str, bool]]]:
    """Heading + ordered ``(text, from_nav)`` lines for one subtree.

    Returns the first ``AXHeading`` as the heading and every ``AXStaticText``
    value as a line (glyph noise dropped, consecutive dups and a heading's own
    echoed static-text removed). ``from_nav`` marks text inside an interactive
    control ancestor. Used to materialize a rich list child.
    """
    heading: str | None = None
    lines: list[tuple[str, bool]] = []
    prev: str | None = None

    def rec(n: ax.Node, under_nav: bool) -> None:
        nonlocal heading, prev
        role = n.get("role")
        here_nav = under_nav or role in _NAV_ROLES
        if role == "AXHeading":
            text = _label_of(n)
            if text and heading is None:
                heading = text
                prev = text
            return  # skip the heading's echoed static-text children
        if role == "AXStaticText":
            text = (n.get("value") or "").strip()
            if text and text != prev and not _is_noise(text):
                lines.append((text, here_nav))
                prev = text
            return
        for child in n.get("children") or []:
            if isinstance(child, dict):
                rec(child, here_nav)

    rec(node, False)
    return heading, lines


def _is_nav_item(heading: str | None, heading_is_nav: bool, lines: list[tuple[str, bool]]) -> bool:
    """True when a raw item is a navigation cluster to discard.

    Content wins on a **long prose line** (a real paragraph / sentence) — a turn
    of a conversation or any record with running text is always kept.

    Otherwise the item is navigation when its body is *control-dominated*: at
    least ``_NAV_FRACTION`` of its text fragments are the labels of interactive
    controls (links / buttons). That is what a breadcrumb, a repo-tab row, a
    toolbar, and the claude.ai sidebar all are. ``heading_is_nav`` (the heading
    itself being a control label, e.g. a sidebar "Products" button) forces the
    nav verdict for a headless-prose-free section.

    The provenance fraction — not the heading — is the discriminator: a GitHub
    issue record's *title* is itself a link (so a heading-based rule would
    misfire), but its body mixes link labels with bare status / author / time
    text, landing well below the threshold, whereas a pure nav rail is ~1.0.
    """
    texts = [t for t, _ in lines]
    if any(len(t) > _SHORT_LINE_CHARS for t in texts):
        return False  # real prose — keep it
    if not texts:
        # A standalone heading with no body: nav only if the heading is itself a
        # control label (a section button); a bare content heading is kept.
        return heading_is_nav
    nav = sum(1 for _, from_nav in lines if from_nav)
    return nav / len(texts) >= _NAV_FRACTION


def _join_fragments(buf: list[str]) -> str:
    """Join a run of short fragments, picking the separator per adjacent pair.

    The browser splits both kinds of content into ``AXStaticText`` runs:

    - **Discrete labels / meta** (``area:distribution``, ``opened``, ``Open``) —
      independent values → joined with ``· `` so they read as a list.
    - **Prose split mid-sentence** — a CJK paragraph chopped into runs by inline
      styling (``寻找创新这件事本身`` + ``变成了一套可工业化的流程。``) → joined SEAMLESSLY
      so it reads back as one sentence.

    The choice is per *gap*: if either side of the boundary carries CJK text the
    gap is mid-sentence prose (seamless); otherwise it is a label list (``· ``).
    This handles a run that mixes both without a global mode.
    """
    if not buf:
        return ""
    result = buf[0]
    for nxt in buf[1:]:
        prose = bool(_CJK_RE.search(result[-20:])) or bool(_CJK_RE.search(nxt[:20]))
        result += (_PROSE_JOIN if prose else _LABEL_JOIN) + nxt
    return result


def _merge_lines(lines: list[str]) -> list[str]:
    """Re-join the ``AXStaticText`` fragments of a single visual line.

    Consecutive short fragments (≤ ``_SHORT_LINE_CHARS``) are merged into one
    line via :func:`_join_fragments` (which picks ``· `` for discrete labels and
    a seamless join for mid-sentence prose); a long fragment (real prose, e.g. a
    Claude answer paragraph) stays on its own line. Adjacent fragments where one
    contains the other (a label echo) collapse to the longer.
    """
    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            out.append(_join_fragments(buf))
            buf.clear()

    for text in lines:
        if len(text) > _SHORT_LINE_CHARS:
            flush()
            out.append(text)
            continue
        if buf and (text == buf[-1] or text in buf[-1] or buf[-1] in text):
            if len(text) > len(buf[-1]):
                buf[-1] = text
            continue
        buf.append(text)
    flush()
    return out

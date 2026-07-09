"""Validate the attention locus on REAL captures (privacy-safe, commits nothing).

Reads the live Persome capture-buffer and reports, per app, the resolved locus
rung distribution; for cmux it reports whether the app is "attention-
authoritative" on field data — every window where the terminal pane was
injected resolves to rung=pane with ZERO pre-marker chrome leaking into the
rendered prompt input.

This is the real-data counterpart to the synthetic golden
(tests/eval/golden/attention_golden.yaml): the golden is the always-on
regression guard; this is the on-demand "does it hold on MY screens" check that
lets us declare an app authoritative before writing/expanding its resolver.

Usage (from persome-core):
    uv run python scripts/validate_attention_locus_real.py [BUFFER_DIR]

Default BUFFER_DIR: ~/.persome/chronicle/capture-buffer. Exits non-zero if cmux is
not authoritative (a real chrome leak), so it can gate a resolver change.

Correct chrome-leak metric (learned the hard way): a *token* like 工作区 recurs
in legitimate terminal output, so token-presence is a false positive. A leak is
a distinctive PRE-marker chrome LINE surviving into PRIMARY.
"""

from __future__ import annotations

import glob
import json
import sys
from collections import Counter
from pathlib import Path

from persome.timeline import aggregator
from persome.timeline.attention_locus import _CHROME_ROLES, resolve_locus

_MARKER = "### [cmux terminal]"
# Distinctive cmux chrome tokens; a PRE-marker line carrying one is chrome.
_CHROME_TOKENS = ("工作区", "切换侧边栏", "新建工作区", "有可用更新", "square.split")
# A chrome element's text must be at least this long to count as a leak signal.
# Short generic labels (关闭/Close/Settings) coincide with page content and are
# ambiguous — three rounds of false positives taught us to gate on distinctive
# (long) text, not the markdown-rendered line (whose `- [Button] ` prefix
# inflated the length).
_DISTINCTIVE = 10


def _collect(elements, roles, out):
    for el in elements or []:
        if not isinstance(el, dict):
            continue
        if str(el.get("role") or "") in roles:
            out.append(el)
        else:
            _collect(el.get("children") or [], roles, out)


def _chrome_lines(ax: dict) -> set[str]:
    """Distinctive raw text under chrome-role subtrees (路2 metric): if any
    survives into PRIMARY, the structural extractor leaked real chrome. Uses the
    elements' own title/value/description (NOT the markdown render) and keeps
    only distinctive (>= _DISTINCTIVE chars) strings to avoid common-word
    coincidences."""
    elements: list = []
    for app in ax.get("apps", []) or []:
        for win in app.get("windows", []) or []:
            elements.extend(win.get("elements") or [])
    chrome_nodes: list = []
    _collect(elements, _CHROME_ROLES, chrome_nodes)
    texts: set[str] = set()

    def _walk(el: dict) -> None:
        for key in ("title", "value", "description"):
            t = str(el.get(key) or "").strip()
            if len(t) >= _DISTINCTIVE:
                texts.add(t)
        for child in el.get("children") or []:
            if isinstance(child, dict):
                _walk(child)

    for node in chrome_nodes:
        _walk(node)
    return texts


def _default_buffer() -> Path:
    return Path.home() / ".persome" / "chronicle" / "capture-buffer"


def main(argv: list[str]) -> int:
    buf = Path(argv[1]) if len(argv) > 1 else _default_buffer()
    files = sorted(glob.glob(str(buf / "*.json")))
    if not files:
        print(f"no captures under {buf}")
        return 2

    by_app: Counter = Counter()
    rungs: Counter = Counter()
    cmux_marker = 0
    cmux_pane = 0
    cmux_leak = 0
    leak_examples: list[str] = []
    # 路2 structural narrowing (any app with an AX content/chrome split):
    content_total = 0
    content_leak = 0
    content_by_app: Counter = Counter()
    content_leak_examples: list[str] = []

    for f in files:
        try:
            data = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        wm = data.get("window_meta") or {}
        app = str(wm.get("app_name") or "?")
        by_app[app] += 1
        vt = str(data.get("visible_text") or "").strip()
        if not vt:
            continue

        loc = resolve_locus(data, visible_text=vt)
        rungs[f"{app}:{loc.rung}"] += 1

        if str(wm.get("bundle_id") or "") == "com.cmuxterm.app":
            idx = vt.find(_MARKER)
            if idx < 0:
                continue  # injection didn't land → fallback whole-window (expected)
            cmux_marker += 1
            if loc.rung == "pane":
                cmux_pane += 1
            events_text, _a, _b = aggregator._format_events([(Path(f), data)], locus_enabled=True)
            pre_chrome = {
                ln.strip()
                for ln in vt[:idx].splitlines()
                if len(ln.strip()) > 5 and any(tok in ln for tok in _CHROME_TOKENS)
            }
            leaked = [ln for ln in pre_chrome if ln in events_text]
            if leaked:
                cmux_leak += 1
                if len(leak_examples) < 3:
                    leak_examples.append(f"  {Path(f).name}: {leaked[:2]}")
            continue

        # Non-cmux: structural content narrowing (路2). For a window narrowed to
        # the content rung, none of the chrome-node text should reach PRIMARY.
        if loc.rung == "content":
            content_total += 1
            content_by_app[app] += 1
            ax = data.get("ax_tree") or {}
            chrome = _chrome_lines(ax)
            if chrome:
                events_text, _a, _b = aggregator._format_events(
                    [(Path(f), data)], locus_enabled=True
                )
                leaked = [ln for ln in chrome if ln in events_text]
                if leaked:
                    content_leak += 1
                    if len(content_leak_examples) < 3:
                        content_leak_examples.append(f"  {app} {Path(f).name}: {leaked[:2]}")

    print(f"capture-buffer: {len(files)} files")
    print("top apps:", dict(by_app.most_common(8)))
    print("-" * 64)
    print(f"cmux windows with injected pane: {cmux_marker}")
    print(f"  rung=pane:                     {cmux_pane}")
    print(f"  chrome-leak (pre-marker line): {cmux_leak}  (target 0)")
    print("-" * 64)
    print("路2 structural narrowing (non-cmux apps → content rung) — COVERAGE:")
    print(f"  windows narrowed to content:   {content_total}")
    print(f"  by app:                        {dict(content_by_app.most_common(8))}")
    print(f"  text-overlap chrome-leak:      {content_leak}  (CONFOUNDED upper bound)")
    print("  NOTE: for web apps the tab title == the page title, so chrome and")
    print("  content legitimately share text — this number OVER-counts and is NOT")
    print("  a pass/fail. The real guarantee is structural (PRIMARY = the webarea")
    print("  subtree; browser toolbar/URL are AX siblings, excluded by")
    print("  construction) — asserted in test_attention_locus on a controlled tree.")
    print("-" * 64)
    print("rung distribution (app:rung → count):")
    for k, v in rungs.most_common(24):
        print(f"  {k:40} {v}")
    if leak_examples:
        print("\ncmux leak examples:")
        print("\n".join(leak_examples))

    # The exit-code gate is cmux only — its marker-based metric is cleanly
    # measurable. 路2 coverage is reported for inspection, not gated on the
    # confounded text-overlap number.
    cmux_ok = cmux_marker > 0 and cmux_leak == 0 and cmux_pane == cmux_marker
    print("-" * 64)
    print(f"cmux on real data:   {'AUTHORITATIVE' if cmux_ok else 'NOT YET'}")
    print(f"路2 coverage:         {content_total} non-cmux windows narrowed to content")
    return 0 if cmux_ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

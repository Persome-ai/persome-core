"""Unit tests for the attention-locus resolver (timeline/attention_locus.py).

Deterministic, no LLM. Each fusion-ladder rung wins on a crafted capture; the
cmux resolver narrows to the terminal pane (chrome dropped); divergent signals
split into primary + peripheral; empty signals fall open to a surface-level
locus; the registry default fires for an unknown bundle; resolution never
raises.
"""

from __future__ import annotations

from persome.timeline import attention_locus as al
from persome.timeline.attention_locus import AttentionLocus, resolve_locus


def _cap(bundle: str = "com.acme.app", **fields) -> dict:
    data: dict = {"window_meta": {"bundle_id": bundle, "title": "Win", "app_name": "Acme"}}
    data.update(fields)
    return data


def _click(role: str = "", title: str = "", value: str = "") -> dict:
    return {
        "event_type": "UserMouseClick",
        "details": {"x": 1.0, "y": 2.0, "element": {"role": role, "title": title, "value": value}},
    }


# --- ladder rungs ----------------------------------------------------------


def test_editing_rung_wins() -> None:
    cap = _cap(
        focused_element={"role": "AXTextArea", "is_editable": True, "value": "fix the bug"},
        visible_text="window with fix the bug somewhere",
    )
    loc = resolve_locus(cap, visible_text=cap["visible_text"])
    assert loc.rung == "editing"
    assert loc.confidence == al._CONF_EDITING
    assert loc.content == cap["visible_text"]  # not narrowed for a generic app


def test_cursor_rung_when_no_editing() -> None:
    cap = _cap(
        focused_element={"role": "", "is_editable": False},
        trigger=_click(role="AXButton", title="Send"),
        visible_text="some page",
    )
    loc = resolve_locus(cap, visible_text="some page")
    assert loc.rung == "cursor"
    assert loc.confidence == al._CONF_CURSOR
    assert "AXButton" in loc.region


def test_focus_rung_when_no_editing_no_click() -> None:
    cap = _cap(
        focused_element={"role": "AXStaticText", "is_editable": False, "value": "read this"},
        visible_text="doc body",
    )
    loc = resolve_locus(cap, visible_text="doc body")
    assert loc.rung == "focus"
    assert loc.confidence == al._CONF_FOCUS


def test_fallback_when_no_signal() -> None:
    cap = _cap(visible_text="just a window")
    loc = resolve_locus(cap, visible_text="just a window")
    assert loc.rung == "fallback"
    assert loc.confidence == 0.0
    assert loc.content == "just a window"  # never empty


# --- divergence: primary + peripheral --------------------------------------


def test_editing_with_divergent_cursor_sets_peripheral() -> None:
    cap = _cap(
        focused_element={"role": "AXTextArea", "is_editable": True, "value": "reply draft"},
        trigger=_click(role="AXStaticText", value="reference doc line"),
        visible_text="composer + reference",
    )
    loc = resolve_locus(cap, visible_text="composer + reference")
    assert loc.rung == "editing"
    assert loc.peripheral == "reference doc line"  # acting on draft, referencing the doc


def test_editing_with_same_target_no_peripheral() -> None:
    cap = _cap(
        focused_element={"role": "AXTextArea", "is_editable": True, "value": "same"},
        trigger=_click(value="same"),
        visible_text="x",
    )
    loc = resolve_locus(cap, visible_text="x")
    assert loc.peripheral == ""


# --- cmux resolver: narrow to the pane, drop chrome ------------------------


def test_cmux_narrows_to_terminal_pane() -> None:
    chrome = "## cmux [active] workspace 1/7 \u6709\u53ef\u7528\u66f4\u65b0 \u5207\u6362\u4fa7\u8fb9\u680f "
    pane = "❯ pytest -k attention\n12 passed real work"
    vt = chrome + "### [cmux terminal]\n" + pane
    cap = _cap(bundle="com.cmuxterm.app", visible_text=vt)
    loc = resolve_locus(cap, visible_text=vt)
    assert loc.rung == "pane"
    assert loc.confidence == al._CONF_PANE
    assert loc.content == pane
    assert "workspace 1/7" not in loc.content  # chrome dropped


def test_cmux_without_marker_falls_through_to_generic() -> None:
    vt = "## cmux [active] just chrome, injection didn't land"
    cap = _cap(bundle="com.cmuxterm.app", visible_text=vt)
    loc = resolve_locus(cap, visible_text=vt)
    assert loc.rung in {"fallback", "cursor", "focus"}
    assert loc.content == vt  # not narrowed


# --- registry + fail-open --------------------------------------------------


def test_unknown_bundle_uses_generic_resolver() -> None:
    cap = _cap(bundle="com.totally.unknown", visible_text="hello")
    loc = resolve_locus(cap, visible_text="hello")
    assert isinstance(loc, AttentionLocus)
    assert loc.surface == "Win"


def test_resolution_never_raises(monkeypatch) -> None:
    def boom(_data, _surface, _vt):
        raise RuntimeError("resolver blew up")

    monkeypatch.setitem(al.LOCUS_RESOLVERS, "com.boom.app", boom)
    cap = _cap(bundle="com.boom.app", visible_text="survive")
    loc = resolve_locus(cap, visible_text="survive")
    assert loc.rung == "fallback"
    assert loc.content == "survive"  # fell open, never raised


def _el(role: str, *, value: str = "", title: str = "", children: list | None = None) -> dict:
    return {"role": role, "title": title, "value": value, "children": children or []}


def _ax(*elements: dict, title: str = "Win") -> dict:
    return {
        "apps": [
            {
                "bundle_id": "b",
                "name": "App",
                "windows": [{"title": title, "elements": list(elements)}],
            }
        ]
    }


def test_structural_webarea_content_drops_chrome() -> None:
    ax = _ax(
        _el(
            "AXToolbar",
            children=[_el("AXButton", title="Reload"), _el("AXTextField", value="https://x.com")],
        ),
        _el("AXTabGroup", children=[_el("AXButton", title="Tab: GitHub PR")]),
        _el(
            "AXWebArea",
            children=[
                _el("AXStaticText", value="the actual article body the user is reading here")
            ],
        ),
    )
    vt = "Reload https://x.com Tab: GitHub PR the actual article body the user is reading here"
    cap = _cap(bundle="com.apple.Safari", ax_tree=ax, visible_text=vt)
    loc = resolve_locus(cap, visible_text=vt)
    assert loc.rung == "content"
    assert "the actual article body" in loc.content
    assert "Reload" not in loc.content  # toolbar chrome dropped
    assert "Tab: GitHub PR" not in loc.content  # tab chrome dropped


def test_structural_prune_chrome_without_webarea() -> None:
    ax = _ax(
        _el("AXToolbar", children=[_el("AXButton", title="Save Document")]),
        _el(
            "AXGroup",
            children=[
                _el("AXStaticText", value="the document body text that is sufficiently long")
            ],
        ),
    )
    vt = "Save Document the document body text that is sufficiently long"
    cap = _cap(bundle="com.some.editor", ax_tree=ax, visible_text=vt)
    loc = resolve_locus(cap, visible_text=vt)
    assert loc.rung == "content"
    assert "the document body text" in loc.content
    assert "Save Document" not in loc.content


def test_structural_thin_content_falls_open() -> None:
    ax = _ax(_el("AXWebArea", children=[_el("AXStaticText", value="hi")]))  # < 40 chars
    vt = "the whole window text is clearly much longer than the thin web area"
    cap = _cap(bundle="com.apple.Safari", ax_tree=ax, visible_text=vt)
    loc = resolve_locus(cap, visible_text=vt)
    assert loc.rung == "fallback"
    assert loc.content == vt  # kept the whole window


def test_no_ax_tree_falls_open() -> None:
    cap = _cap(bundle="com.apple.Safari", visible_text="no structure available here")
    loc = resolve_locus(cap, visible_text="no structure available here")
    assert loc.rung == "fallback"
    assert loc.content == "no structure available here"


def test_focus_signal_beats_structural_narrowing() -> None:
    # A real focus signal outranks structural content (stronger localization).
    ax = _ax(
        _el(
            "AXWebArea",
            children=[_el("AXStaticText", value="page body that is long enough to narrow")],
        )
    )
    cap = _cap(
        bundle="com.apple.Safari",
        focused_element={
            "role": "AXStaticText",
            "is_editable": False,
            "value": "the focused thing",
        },
        ax_tree=ax,
        visible_text="whole window",
    )
    loc = resolve_locus(cap, visible_text="whole window")
    assert loc.rung == "focus"

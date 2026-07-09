"""Generic AX → clean text resolver (capture/generic_render.py).

Covers the five levers from docs/superpowers/specs/2026-06-19-generic-ax-resolver-design.md:
chrome fold, container collapse, repeated-structure flatten, role-label
suppression, and the fail-open floor.
"""

from __future__ import annotations

from persome.capture import generic_render as gr


def _app(elements, name="App", bundle="com.x.app"):
    return {"name": name, "bundle_id": bundle, "is_frontmost": True,
            "windows": [{"title": "Win", "elements": elements}]}


def test_fail_open_when_too_thin():
    # nothing meaningful → None (caller falls open to the full render)
    assert gr.resolve_app(_app([{"role": "AXButton"}])) is None
    assert gr.resolve_app({}) is None


def test_chrome_folded_to_digest_not_dumped():
    md = gr.resolve_app(_app([
        {"role": "AXToolbar", "children": [
            {"role": "AXButton", "title": "Bookmark A"},
            {"role": "AXButton", "title": "Bookmark B"},
        ]},
        {"role": "AXTabGroup", "children": [
            {"role": "AXRadioButton", "title": "Tab 1"},
            {"role": "AXRadioButton", "title": "Tab 2"},
            {"role": "AXRadioButton", "title": "Tab 3"},
        ]},
        {"role": "AXWebArea", "children": [
            {"role": "AXStaticText", "value": "Real page content the user reads here, long enough."},
        ]},
    ]))
    assert md is not None
    assert "外壳已折叠" in md          # chrome digest line present
    assert "Bookmark A" not in md      # chrome detail NOT inlined
    assert "Tab 1" not in md
    assert "Real page content the user reads here" in md  # content kept


def test_container_collapse_promotes_children():
    md = gr.resolve_app(_app([
        {"role": "AXGroup", "children": [
            {"role": "AXGroup", "children": [
                {"role": "AXStaticText", "value": "Buried but meaningful text content here."},
            ]},
        ]},
    ]))
    assert md is not None
    assert "Buried but meaningful text content here." in md
    # no empty [Group] scaffolding lines
    assert "[Group]" not in md


def test_repeated_rows_flattened_no_per_cell_scaffolding():
    rows = [
        {"role": "AXRow", "children": [
            {"role": "AXCell", "value": f"Name {i}"},
            {"role": "AXCell", "value": f"value-{i}"},
        ]}
        for i in range(5)
    ]
    md = gr.resolve_app(_app([{"role": "AXTable", "children": rows}]))
    assert md is not None
    # each row flattened to one line (content joined), not per-cell [Cell] bullets
    assert "Name 0 · value-0" in md
    assert "Name 4 · value-4" in md
    assert "[Cell]" not in md


def test_content_bare_buttons_folded_to_digest():
    md = gr.resolve_app(_app([
        {"role": "AXHeading", "value": "An Article Heading That Is Clearly Content"},
        {"role": "AXStaticText", "value": "A paragraph of body text, plainly content too."},
        {"role": "AXButton", "title": "Submit Order Now Please"},
        {"role": "AXButton", "title": "Cancel"},
    ]))
    assert md is not None
    assert "An Article Heading That Is Clearly Content" in md
    assert "[Heading]" not in md and "[StaticText]" not in md   # content = bare
    # buttons are affordances → folded into the digest, NOT emitted as content
    # lines (else the LLM reads nav as to-dos — measured in the eval).
    assert "Submit Order Now Please" not in md
    assert "Cancel" not in md
    assert "外壳已折叠" in md and "按钮" in md


def test_long_content_node_not_capped_per_line():
    long_text = "word " * 400  # ~2000 chars, a real paragraph
    md = gr.resolve_app(_app([{"role": "AXStaticText", "value": long_text}]))
    assert md is not None
    # the content node is not truncated to a short per-line cap (global cap is the caller's)
    assert len(md) > 1500


def test_never_raises_on_garbage():
    for bad in [None, [], {"windows": None}, {"windows": [{"elements": [None, 5, "x"]}]}]:
        gr.resolve_app(bad)  # must not raise

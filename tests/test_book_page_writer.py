"""Tests for page writing (Task 3): book_page.write_episode."""

from __future__ import annotations


def _resp_text(text: str):  # type: ignore[no-untyped-def]
    from persome.writer.llm import _build_response

    return _build_response(text)


def test_write_page_body(ac_root, monkeypatch):
    from persome.writer import book_page

    monkeypatch.setattr(
        book_page,
        "call_llm",
        lambda *a, **k: _resp_text("On an Unnecessary Phone Call\n\nYou made a call today."),
    )
    page = book_page.write_episode(
        "2026-07-08", {"anchor": "the call", "source_refs": []}, "daily text"
    )
    assert page["title"] == "On an Unnecessary Phone Call"
    assert page["body"] == "You made a call today."


def test_write_page_strips_blank_lines_before_title(ac_root, monkeypatch):
    from persome.writer import book_page

    monkeypatch.setattr(
        book_page,
        "call_llm",
        lambda *a, **k: _resp_text("\n\n  A Title  \n\nFirst paragraph.\n\nSecond."),
    )
    page = book_page.write_episode("2026-07-08", {"anchor": "x", "source_refs": []}, "t")
    assert page["title"] == "A Title"
    assert page["body"] == "First paragraph.\n\nSecond."


def test_write_page_title_only_has_empty_body(ac_root, monkeypatch):
    from persome.writer import book_page

    monkeypatch.setattr(book_page, "call_llm", lambda *a, **k: _resp_text("Just A Title"))
    page = book_page.write_episode("2026-07-08", {"anchor": "x", "source_refs": []}, "t")
    assert page["title"] == "Just A Title"
    assert page["body"] == ""


def test_write_page_empty_llm_falls_back_to_anchor_title(ac_root, monkeypatch):
    from persome.writer import book_page

    monkeypatch.setattr(book_page, "call_llm", lambda *a, **k: _resp_text(""))
    page = book_page.write_episode("2026-07-08", {"anchor": "the call", "source_refs": []}, "t")
    assert page["title"] == "the call"
    assert page["body"] == ""


def test_write_page_strips_leading_heading_marker(ac_root, monkeypatch):
    """A title line that comes back as a markdown heading is stripped of '#'."""
    from persome.writer import book_page

    monkeypatch.setattr(
        book_page,
        "call_llm",
        lambda *a, **k: _resp_text("# The voices you tried on\n\nYou heard them all day."),
    )
    page = book_page.write_episode("2026-07-08", {"anchor": "x", "source_refs": []}, "t")
    assert page["title"] == "The voices you tried on"
    assert "#" not in page["title"]
    assert page["body"] == "You heard them all day."


def test_clean_title_strips_markdown_noise(ac_root):
    """_clean_title removes heading markers, emphasis, quotes and bullets."""
    from persome.writer.book_page import _clean_title

    assert _clean_title("### A Quiet Hour") == "A Quiet Hour"
    assert _clean_title("**A Quiet Hour**") == "A Quiet Hour"
    assert _clean_title("- A Quiet Hour") == "A Quiet Hour"
    assert _clean_title('"A Quiet Hour"') == "A Quiet Hour"
    # Idempotent and degrades to "" for pure-noise input.
    assert _clean_title("A Quiet Hour") == "A Quiet Hour"
    assert _clean_title("###  ") == ""

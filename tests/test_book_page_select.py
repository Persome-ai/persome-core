"""Tests for episode selection (Task 2): book_page.select_episodes."""

from __future__ import annotations


def test_select_episodes_parses_llm(ac_root, monkeypatch):
    from persome.writer import book_page

    fake = {
        "choices": [
            {
                "message": {
                    "content": '[{"anchor":"the unplanned phone call","source_refs":["event#3"]}]'
                }
            }
        ]
    }
    monkeypatch.setattr(book_page, "call_llm", lambda *a, **k: _resp(fake))
    eps = book_page.select_episodes("2026-07-08", "…event daily text…")
    assert len(eps) == 1 and eps[0]["anchor"]
    assert eps[0]["source_refs"] == ["event#3"]


def test_select_empty_on_quiet_day(ac_root, monkeypatch):
    from persome.writer import book_page

    monkeypatch.setattr(book_page, "call_llm", lambda *a, **k: _resp_text("[]"))
    assert book_page.select_episodes("2026-07-08", "boring") == []


def test_select_tolerates_prose_around_json(ac_root, monkeypatch):
    from persome.writer import book_page

    content = 'Here are the episodes:\n[{"anchor":"a call","source_refs":[]}]\nDone.'
    monkeypatch.setattr(book_page, "call_llm", lambda *a, **k: _resp_text(content))
    eps = book_page.select_episodes("2026-07-08", "text")
    assert len(eps) == 1 and eps[0]["anchor"] == "a call"


def test_select_returns_empty_on_garbage(ac_root, monkeypatch):
    from persome.writer import book_page

    monkeypatch.setattr(book_page, "call_llm", lambda *a, **k: _resp_text("not json at all"))
    assert book_page.select_episodes("2026-07-08", "text") == []


def test_select_skips_malformed_items(ac_root, monkeypatch):
    from persome.writer import book_page

    content = '[{"anchor":"good","source_refs":[]}, {"no_anchor":1}, "string-item"]'
    monkeypatch.setattr(book_page, "call_llm", lambda *a, **k: _resp_text(content))
    eps = book_page.select_episodes("2026-07-08", "text")
    assert len(eps) == 1 and eps[0]["anchor"] == "good"


# ─── helpers: build OpenAI-shaped response objects ──────────────────────────


def _resp(payload: dict):  # type: ignore[no-untyped-def]
    from persome.writer.llm import _build_response

    content = payload["choices"][0]["message"]["content"]
    return _build_response(content)


def _resp_text(text: str):  # type: ignore[no-untyped-def]
    from persome.writer.llm import _build_response

    return _build_response(text)

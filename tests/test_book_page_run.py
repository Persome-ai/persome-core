"""Tests for orchestration (Task 4): book_page.run_book_pages + dream hook."""

from __future__ import annotations

from persome import paths


def _seed_event_daily(date: str, text: str = "some activity") -> None:
    p = paths.memory_dir() / f"event-{date}.md"
    p.write_text(f"# {date}\n\n{text}\n", encoding="utf-8")


def test_run_book_pages_writes_files(ac_root, monkeypatch):
    from persome.writer import book_page

    monkeypatch.setattr(
        book_page, "select_episodes", lambda d, t: [{"anchor": "a", "source_refs": []}]
    )
    monkeypatch.setattr(book_page, "write_episode", lambda d, e, t: {"title": "T", "body": "B"})
    _seed_event_daily("2026-07-08")
    ids = book_page.run_book_pages("2026-07-08")
    assert ids == ["page-2026-07-08"]

    from persome.store import book_pages

    page = book_pages.get_page("page-2026-07-08")
    assert page is not None and page["title"] == "T" and page["body"] == "B"


def test_run_book_pages_quiet_day_writes_nothing(ac_root, monkeypatch):
    from persome.writer import book_page

    monkeypatch.setattr(book_page, "select_episodes", lambda d, t: [])
    _seed_event_daily("2026-07-08")
    assert book_page.run_book_pages("2026-07-08") == []


def test_run_book_pages_no_event_daily_returns_empty(ac_root):
    from persome.writer import book_page

    assert book_page.run_book_pages("2026-07-08") == []


def test_run_book_pages_swallows_write_errors(ac_root, monkeypatch):
    """A failure mid-episode must not raise; already-written ids are returned."""
    from persome.writer import book_page

    monkeypatch.setattr(
        book_page,
        "select_episodes",
        lambda d, t: [{"anchor": "a", "source_refs": []}, {"anchor": "b", "source_refs": []}],
    )

    calls = {"n": 0}

    def _flaky(d, e, t):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        return {"title": "T", "body": "B"}

    monkeypatch.setattr(book_page, "write_episode", _flaky)
    _seed_event_daily("2026-07-08")
    ids = book_page.run_book_pages("2026-07-08")
    assert ids == ["page-2026-07-08"]  # first written, second failed, no raise


def test_run_book_pages_emits_events(ac_root, monkeypatch):
    from persome.writer import book_page

    monkeypatch.setattr(
        book_page, "select_episodes", lambda d, t: [{"anchor": "a", "source_refs": []}]
    )
    monkeypatch.setattr(book_page, "write_episode", lambda d, e, t: {"title": "T", "body": "B"})
    _seed_event_daily("2026-07-08")

    events: list[tuple[str, dict]] = []
    book_page.run_book_pages("2026-07-08", on_event=lambda t, p: events.append((t, p)))
    types = [t for t, _ in events]
    assert "stage_start" in types and "stage_end" in types


def test_dream_run_invokes_book_page_substep(ac_root, monkeypatch):
    """run_reserved_dream must run the book-page sub-step for today after dream,
    and the sub-step failing must never change the dream result."""
    from datetime import datetime

    from persome.config import Config
    from persome.writer import book_page, dream

    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    _seed_event_daily(today)

    monkeypatch.setattr(
        book_page, "select_episodes", lambda d, t: [{"anchor": "a", "source_refs": []}]
    )
    monkeypatch.setattr(book_page, "write_episode", lambda d, e, t: {"title": "T", "body": "B"})

    # Stub the dream loop itself so the test doesn't depend on dream internals.
    monkeypatch.setattr(
        dream,
        "run_dream",
        lambda cfg, on_event=None: dream.DreamResult(committed=True, summary="ok"),
    )

    assert dream.try_reserve_dream_run() is True
    _run_id, result = dream.run_reserved_dream(Config(), trigger="manual")
    assert result.committed is True

    from persome.store import book_pages

    page = book_pages.get_page(f"page-{today}")
    assert page is not None and page["title"] == "T"


def test_dream_run_survives_book_page_failure(ac_root, monkeypatch):
    from persome.config import Config
    from persome.writer import book_page, dream

    def _boom(date, *, on_event=None):
        raise RuntimeError("book page exploded")

    monkeypatch.setattr(book_page, "run_book_pages", _boom)
    monkeypatch.setattr(
        dream,
        "run_dream",
        lambda cfg, on_event=None: dream.DreamResult(committed=True, summary="ok"),
    )

    assert dream.try_reserve_dream_run() is True
    _run_id, result = dream.run_reserved_dream(Config(), trigger="manual")
    assert result.committed is True  # dream unaffected by book-page failure

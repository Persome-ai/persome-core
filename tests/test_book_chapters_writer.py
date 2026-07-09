"""Tests for book-chapter generation (Phase 2.2): writer.book_chapters."""

from __future__ import annotations

import json

from persome import paths
from persome.store import book_chapters as store
from persome.store import fts
from persome.writer import book_chapters


def _resp_text(text: str):  # type: ignore[no-untyped-def]
    from persome.writer.llm import _build_response

    return _build_response(text)


def _seed_session(
    sid: str,
    *,
    title: str = "",
    user: str = "hello",
    archived: bool = False,
    updated_at: str = "2026-01-01T00:00:00+00:00",
) -> None:
    hist = paths.root() / "chat-history"
    hist.mkdir(parents=True, exist_ok=True)
    data = {
        "created_at": updated_at,
        "updated_at": updated_at,
        "title": title,
        "archived": archived,
        "messages": [{"role": "user", "content": user}],
    }
    (hist / f"api-{sid}.json").write_text(json.dumps(data), encoding="utf-8")


def test_recent_sessions_reads_disk_skips_archived_and_empty(ac_root) -> None:
    _seed_session("aaa", title="Work", user="about my job", updated_at="2026-01-03T00:00:00+00:00")
    _seed_session(
        "bbb", title="Friends", user="about people", updated_at="2026-01-02T00:00:00+00:00"
    )
    _seed_session("ccc", title="Archived", archived=True)
    # An empty session (no user turn) must be skipped.
    hist = paths.root() / "chat-history"
    (hist / "api-ddd.json").write_text(
        json.dumps({"updated_at": "2026-01-04T00:00:00+00:00", "messages": []}),
        encoding="utf-8",
    )

    rows = book_chapters.recent_sessions()
    ids = [r["id"] for r in rows]
    assert ids == ["aaa", "bbb"]  # newest-updated first, archived + empty dropped
    assert rows[0]["title"] == "Work"
    assert rows[0]["preview"] == "about my job"


def test_recent_sessions_empty_when_no_history(ac_root) -> None:
    assert book_chapters.recent_sessions() == []


def test_cluster_chapters_parses_and_filters_unknown_ids(ac_root, monkeypatch) -> None:
    sessions = [
        {"id": "s1", "title": "a", "preview": "p"},
        {"id": "s2", "title": "b", "preview": "p"},
    ]
    monkeypatch.setattr(
        book_chapters,
        "call_llm",
        lambda *a, **k: _resp_text(
            "Here you go:\n["
            '{"title": "On work", "subtitle": "YOU + ACME", "session_ids": ["s1", "ghost"]},'
            '{"title": "On friends", "subtitle": "", "session_ids": ["s2"]}'
            "]"
        ),
    )
    chapters = book_chapters.cluster_chapters(sessions)
    assert len(chapters) == 2
    assert chapters[0]["title"] == "On work"
    # "ghost" is not an input id → filtered out, leaving only the real one.
    assert chapters[0]["session_ids"] == ["s1"]
    assert chapters[1]["session_ids"] == ["s2"]


def test_cluster_chapters_drops_chapter_with_no_valid_ids(ac_root, monkeypatch) -> None:
    sessions = [{"id": "s1", "title": "a", "preview": "p"}]
    monkeypatch.setattr(
        book_chapters,
        "call_llm",
        lambda *a, **k: _resp_text('[{"title": "phantom", "session_ids": ["nope"]}]'),
    )
    assert book_chapters.cluster_chapters(sessions) == []


def test_cluster_chapters_empty_input_no_llm(ac_root, monkeypatch) -> None:
    def _boom(*a, **k):  # pragma: no cover — must never be called
        raise AssertionError("LLM should not be called on empty input")

    monkeypatch.setattr(book_chapters, "call_llm", _boom)
    assert book_chapters.cluster_chapters([]) == []


def test_run_book_chapters_persists(ac_root, monkeypatch) -> None:
    _seed_session("s1", title="Work", user="my job")
    _seed_session("s2", title="Friends", user="my people")
    monkeypatch.setattr(
        book_chapters,
        "call_llm",
        lambda *a, **k: _resp_text(
            '[{"title": "On work", "session_ids": ["s1"]},'
            '{"title": "On friends", "session_ids": ["s2"]}]'
        ),
    )

    written = book_chapters.run_book_chapters()
    assert written == 2

    with fts.cursor() as conn:
        rows = store.list_chapters(conn)
    assert {r.title for r in rows} == {"On work", "On friends"}


def test_run_book_chapters_empty_sessions_writes_zero(ac_root, monkeypatch) -> None:
    def _boom(*a, **k):  # pragma: no cover — must never be called
        raise AssertionError("LLM should not be called with no sessions")

    monkeypatch.setattr(book_chapters, "call_llm", _boom)
    assert book_chapters.run_book_chapters() == 0


def test_run_book_chapters_swallows_llm_failure(ac_root, monkeypatch) -> None:
    _seed_session("s1", title="Work", user="my job")

    def _boom(*a, **k):
        raise RuntimeError("llm down")

    monkeypatch.setattr(book_chapters, "call_llm", _boom)
    # Must not raise — a book-chapter failure can never break the dream run.
    assert book_chapters.run_book_chapters() == 0


def test_run_book_chapters_emits_events(ac_root, monkeypatch) -> None:
    _seed_session("s1", title="Work", user="my job")
    monkeypatch.setattr(
        book_chapters,
        "call_llm",
        lambda *a, **k: _resp_text('[{"title": "On work", "session_ids": ["s1"]}]'),
    )
    events: list[tuple[str, dict]] = []
    book_chapters.run_book_chapters(on_event=lambda t, p: events.append((t, p)))

    types = [t for t, _ in events]
    assert types[0] == "stage_start"
    assert types[-1] == "stage_end"
    assert events[-1][1]["written"] == 1

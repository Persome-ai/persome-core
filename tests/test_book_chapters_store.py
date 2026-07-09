"""Tests for the ``book_chapters`` DAO (Book Phase 2.2)."""

from __future__ import annotations

from persome.store import book_chapters as store
from persome.store import fts


def test_replace_generated_inserts_and_lists(ac_root) -> None:
    with fts.cursor() as conn:
        store.replace_generated(
            conn,
            [
                {
                    "title": "On changing direction",
                    "subtitle": "YOU + ACME",
                    "session_ids": ["a", "b"],
                },
                {"title": "What I want from work", "subtitle": "", "session_ids": ["c"]},
            ],
        )
        rows = store.list_chapters(conn)

    titles = {r.title for r in rows}
    assert titles == {"On changing direction", "What I want from work"}
    by_title = {r.title: r for r in rows}
    assert by_title["On changing direction"].session_ids == ["a", "b"]
    assert by_title["On changing direction"].subtitle == "YOU + ACME"
    assert by_title["What I want from work"].session_ids == ["c"]
    assert all(r.edited is False for r in rows)
    assert all(r.id > 0 for r in rows)


def test_replace_generated_wipes_previous_generated(ac_root) -> None:
    with fts.cursor() as conn:
        store.replace_generated(conn, [{"title": "old one", "session_ids": ["x"]}])
        store.replace_generated(conn, [{"title": "new one", "session_ids": ["y"]}])
        rows = store.list_chapters(conn)

    assert [r.title for r in rows] == ["new one"]


def test_replace_generated_preserves_edited(ac_root) -> None:
    with fts.cursor() as conn:
        store.replace_generated(
            conn,
            [
                {"title": "keep me", "session_ids": ["a"]},
                {"title": "drop me", "session_ids": ["b"]},
            ],
        )
        keep = next(r for r in store.list_chapters(conn) if r.title == "keep me")
        # User renames "keep me" → it becomes edited.
        assert store.mark_edited(conn, keep.id, "My Own Title") is True

        # A fresh generation wipes generated rows but must preserve the edited one.
        store.replace_generated(conn, [{"title": "fresh", "session_ids": ["c"]}])
        rows = store.list_chapters(conn)

    titles = {r.title for r in rows}
    assert "My Own Title" in titles  # edited row survived regeneration
    assert "fresh" in titles  # new generated row landed
    assert "drop me" not in titles  # previous generated row wiped
    edited_row = next(r for r in rows if r.title == "My Own Title")
    assert edited_row.edited is True
    assert edited_row.session_ids == ["a"]  # original backing sessions kept


def test_mark_edited_missing_returns_false(ac_root) -> None:
    with fts.cursor() as conn:
        assert store.mark_edited(conn, 9999, "nope") is False


def test_mark_edited_empty_title_rejected(ac_root) -> None:
    with fts.cursor() as conn:
        store.replace_generated(conn, [{"title": "t", "session_ids": ["a"]}])
        row = store.list_chapters(conn)[0]
        assert store.mark_edited(conn, row.id, "   ") is False
        # Title unchanged, still not edited.
        again = store.list_chapters(conn)[0]
        assert again.title == "t"
        assert again.edited is False


def test_titleless_chapter_is_dropped(ac_root) -> None:
    with fts.cursor() as conn:
        n = store.replace_generated(
            conn,
            [
                {"title": "   ", "session_ids": ["a"]},
                {"title": "real", "session_ids": ["b"]},
            ],
        )
        rows = store.list_chapters(conn)

    assert n == 1
    assert [r.title for r in rows] == ["real"]


def test_malformed_session_ids_decode_to_empty(ac_root) -> None:
    # Directly poke a bad JSON blob into the column; list must not raise.
    with fts.cursor() as conn:
        conn.execute(
            "INSERT INTO book_chapters (title, subtitle, session_ids, edited, created_at) "
            "VALUES ('bad', '', 'not-json', 0, '2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
        rows = store.list_chapters(conn)

    assert rows[0].session_ids == []

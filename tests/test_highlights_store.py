"""Tests for the ``highlights`` DAO (Book Phase 2.1)."""

from __future__ import annotations

from persome.store import fts
from persome.store import highlights as highlights_store


def test_insert_and_list_newest_first(ac_root) -> None:
    with fts.cursor() as conn:
        highlights_store.ensure_schema(conn)
        first = highlights_store.insert(conn, quote="first quote", source_ref="page:1")
        second = highlights_store.insert(conn, quote="second quote", source_ref="sess:2")

        rows = highlights_store.list_recent(conn)

    # Newest first: second inserted comes before first.
    assert [h.id for h in rows] == [second.id, first.id]
    assert rows[0].quote == "second quote"
    assert rows[0].source_ref == "sess:2"
    assert rows[1].source_ref == "page:1"


def test_insert_returns_persisted_row(ac_root) -> None:
    with fts.cursor() as conn:
        highlights_store.ensure_schema(conn)
        created = highlights_store.insert(conn, quote="hello", source_ref="page:42")

    assert created.id > 0
    assert created.quote == "hello"
    assert created.source_ref == "page:42"
    # time_label is derived from created_at as `MON D · HH:MM`.
    label = created.time_label()
    assert "·" in label
    assert ":" in label.split("·")[1]


def test_source_ref_defaults_to_empty(ac_root) -> None:
    with fts.cursor() as conn:
        highlights_store.ensure_schema(conn)
        created = highlights_store.insert(conn, quote="no source")
        rows = highlights_store.list_recent(conn)

    assert created.source_ref == ""
    assert rows[0].source_ref == ""


def test_delete_removes_row(ac_root) -> None:
    with fts.cursor() as conn:
        highlights_store.ensure_schema(conn)
        h = highlights_store.insert(conn, quote="to delete")

        assert highlights_store.delete(conn, h.id) is True
        assert highlights_store.list_recent(conn) == []

        # Deleting again is a no-op.
        assert highlights_store.delete(conn, h.id) is False


def test_list_empty(ac_root) -> None:
    with fts.cursor() as conn:
        highlights_store.ensure_schema(conn)
        assert highlights_store.list_recent(conn) == []


def test_list_respects_limit(ac_root) -> None:
    with fts.cursor() as conn:
        highlights_store.ensure_schema(conn)
        for i in range(5):
            highlights_store.insert(conn, quote=f"q{i}")
        rows = highlights_store.list_recent(conn, limit=2)

    assert len(rows) == 2


def test_time_label_format(ac_root) -> None:
    from datetime import datetime

    h = highlights_store.Highlight(
        id=1,
        quote="q",
        source_ref="",
        created_at=datetime(2026, 7, 8, 19, 2),
    )
    assert h.time_label() == "JUL 8 · 19:02"

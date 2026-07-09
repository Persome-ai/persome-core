"""Tests for the Book Chapters REST endpoints (Phase 2.2)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from persome.api import build_api_app
from persome.store import book_chapters as store
from persome.store import fts


def _client(ac_root) -> TestClient:
    return TestClient(build_api_app())


def _seed(chapters) -> None:
    with fts.cursor() as conn:
        store.replace_generated(conn, chapters)


def test_list_empty(ac_root) -> None:
    client = _client(ac_root)
    res = client.get("/book/chapters")

    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    assert body["data"] == {"items": [], "count": 0}


def test_list_shape(ac_root) -> None:
    _seed([{"title": "On work", "subtitle": "YOU + ACME", "session_ids": ["a", "b"]}])
    client = _client(ac_root)

    body = client.get("/book/chapters").json()["data"]
    assert body["count"] == 1
    item = body["items"][0]
    assert item["title"] == "On work"
    assert item["subtitle"] == "YOU + ACME"
    assert item["from_count"] == 2
    assert item["session_ids"] == ["a", "b"]
    assert item["edited"] is False
    assert isinstance(item["id"], int) and item["id"] > 0


def test_patch_flips_edited_and_renames(ac_root) -> None:
    _seed([{"title": "generated title", "session_ids": ["a"]}])
    client = _client(ac_root)

    chapter_id = client.get("/book/chapters").json()["data"]["items"][0]["id"]

    res = client.patch(f"/book/chapters/{chapter_id}", json={"title": "My Title"})
    assert res.status_code == 200
    assert res.json()["data"] == {"id": chapter_id, "title": "My Title", "edited": True}

    item = client.get("/book/chapters").json()["data"]["items"][0]
    assert item["title"] == "My Title"
    assert item["edited"] is True


def test_patch_missing_returns_404(ac_root) -> None:
    client = _client(ac_root)
    res = client.patch("/book/chapters/9999", json={"title": "x"})
    assert res.status_code == 404


def test_patch_empty_title_rejected(ac_root) -> None:
    _seed([{"title": "t", "session_ids": ["a"]}])
    client = _client(ac_root)
    chapter_id = client.get("/book/chapters").json()["data"]["items"][0]["id"]

    res = client.patch(f"/book/chapters/{chapter_id}", json={"title": "   "})
    assert res.status_code == 422

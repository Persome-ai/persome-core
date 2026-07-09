"""End-to-end tests for the /book/pages* HTTP endpoints (Task 5)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from persome.api import build_api_app
from persome.store import book_pages


@pytest.fixture
def client(ac_root) -> TestClient:
    return TestClient(build_api_app())


def test_book_pages_list_get_review(client: TestClient, ac_root):
    pid = book_pages.write_page(date="2026-07-08", title="A", body="p1\n\np2")

    r = client.get("/book/pages")
    assert r.status_code == 200
    items = r.json()["data"]["items"]
    assert items[0]["id"] == pid
    assert items[0]["is_draft"] is True
    assert r.json()["data"]["count"] == 1

    g = client.get(f"/book/pages/{pid}")
    assert g.status_code == 200
    detail = g.json()["data"]
    assert detail["is_draft"] is True
    assert detail["body"] == ["p1", "p2"]  # paragraph array
    assert detail["title"] == "A"

    p = client.patch(f"/book/pages/{pid}", json={"reviewed": True})
    assert p.status_code == 200
    assert p.json()["data"]["success"] is True

    after = client.get(f"/book/pages/{pid}")
    assert after.json()["data"]["is_draft"] is False


def test_book_pages_empty(client: TestClient, ac_root):
    assert client.get("/book/pages").json()["data"]["items"] == []
    assert client.get("/book/pages").json()["data"]["count"] == 0


def test_book_pages_get_unknown_404(client: TestClient, ac_root):
    r = client.get("/book/pages/page-nope")
    assert r.status_code == 404


def test_book_pages_patch_unknown_404(client: TestClient, ac_root):
    r = client.patch("/book/pages/page-nope", json={"reviewed": True})
    assert r.status_code == 404


def test_book_pages_list_respects_limit(client: TestClient, ac_root):
    book_pages.write_page(date="2026-07-08", title="A", body="x")
    book_pages.write_page(date="2026-07-09", title="B", body="y")
    r = client.get("/book/pages?limit=1")
    assert len(r.json()["data"]["items"]) == 1
    # newest date first
    assert r.json()["data"]["items"][0]["date"] == "2026-07-09"

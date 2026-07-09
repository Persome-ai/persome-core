"""Tests for the Book Highlights REST endpoints (Phase 2.1)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from persome.api import build_api_app


def _client(ac_root) -> TestClient:
    return TestClient(build_api_app())


def test_list_empty(ac_root) -> None:
    client = _client(ac_root)
    res = client.get("/book/highlights")

    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    assert body["data"] == {"items": [], "count": 0}


def test_post_then_get_then_delete(ac_root) -> None:
    client = _client(ac_root)

    # POST creates a highlight.
    created = client.post(
        "/book/highlights",
        json={"quote": "The call only lasted eleven minutes", "source_ref": "page:7"},
    )
    assert created.status_code == 200
    data = created.json()["data"]
    new_id = data["id"]
    assert data["quote"] == "The call only lasted eleven minutes"
    assert data["source_ref"] == "page:7"
    assert "·" in data["time_label"]

    # GET returns it.
    listed = client.get("/book/highlights")
    body = listed.json()["data"]
    assert body["count"] == 1
    assert body["items"][0]["id"] == new_id
    assert body["items"][0]["quote"] == "The call only lasted eleven minutes"

    # DELETE removes it.
    deleted = client.delete(f"/book/highlights/{new_id}")
    assert deleted.status_code == 200
    assert deleted.json()["data"] == {"deleted": new_id}

    # GET is empty again.
    assert client.get("/book/highlights").json()["data"]["count"] == 0


def test_get_is_newest_first(ac_root) -> None:
    client = _client(ac_root)
    client.post("/book/highlights", json={"quote": "older"})
    client.post("/book/highlights", json={"quote": "newer"})

    items = client.get("/book/highlights").json()["data"]["items"]
    assert [i["quote"] for i in items] == ["newer", "older"]


def test_post_without_source_ref(ac_root) -> None:
    client = _client(ac_root)
    res = client.post("/book/highlights", json={"quote": "no source"})

    assert res.status_code == 200
    assert res.json()["data"]["source_ref"] == ""


def test_post_empty_quote_rejected(ac_root) -> None:
    client = _client(ac_root)
    res = client.post("/book/highlights", json={"quote": "   "})
    assert res.status_code == 422


def test_delete_missing_returns_404(ac_root) -> None:
    client = _client(ac_root)
    res = client.delete("/book/highlights/9999")
    assert res.status_code == 404


def test_limit_caps_results(ac_root) -> None:
    client = _client(ac_root)
    for i in range(3):
        client.post("/book/highlights", json={"quote": f"q{i}"})

    items = client.get("/book/highlights?limit=2").json()["data"]["items"]
    assert len(items) == 2

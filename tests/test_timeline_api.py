"""Tests for GET /timeline REST endpoint."""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from persome.api import build_api_app
from persome.store import fts
from persome.timeline import store as timeline_store
from persome.timeline.store import TimelineBlock


def _block(start: str, end: str, entries: list[str] | None = None) -> TimelineBlock:
    """Helper to build a TimelineBlock with UTC datetimes."""
    return TimelineBlock(
        start_time=datetime.fromisoformat(start),
        end_time=datetime.fromisoformat(end),
        timezone="UTC",
        entries=entries or ["[TestApp] did something"],
        apps_used=["TestApp"],
        capture_count=1,
    )


@pytest.fixture
def client_with_blocks(ac_root):
    """TestClient with 5 timeline blocks pre-inserted spanning two hours."""
    times = [
        ("2026-05-20T10:00:00+00:00", "2026-05-20T10:01:00+00:00"),
        ("2026-05-20T10:01:00+00:00", "2026-05-20T10:02:00+00:00"),
        ("2026-05-20T10:02:00+00:00", "2026-05-20T10:03:00+00:00"),
        ("2026-05-20T11:00:00+00:00", "2026-05-20T11:01:00+00:00"),
        ("2026-05-20T12:00:00+00:00", "2026-05-20T12:01:00+00:00"),
    ]
    with fts.cursor() as conn:
        timeline_store.ensure_schema(conn)
        for start, end in times:
            timeline_store.insert(conn, _block(start, end))
        conn.commit()

    return TestClient(build_api_app())


def test_timeline_empty_database(ac_root) -> None:
    """GET /timeline on empty DB returns empty list."""
    client = TestClient(build_api_app())
    response = client.get("/timeline")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"] == []


def test_timeline_returns_all_blocks(client_with_blocks) -> None:
    """GET /timeline with no filters returns all blocks up to limit."""
    response = client_with_blocks.get("/timeline?limit=100")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert len(body["data"]) == 5


def test_timeline_newest_first(client_with_blocks) -> None:
    """Blocks are returned in descending start_time order (newest first)."""
    response = client_with_blocks.get("/timeline?limit=100")
    blocks = response.json()["data"]

    start_times = [b["start_time"] for b in blocks]
    assert start_times == sorted(start_times, reverse=True)


def test_timeline_limit(client_with_blocks) -> None:
    """limit param caps the number of returned blocks."""
    response = client_with_blocks.get("/timeline?limit=2")

    assert response.status_code == 200
    assert len(response.json()["data"]) == 2


def test_timeline_since_filter(client_with_blocks) -> None:
    """since param excludes blocks before the given time."""
    response = client_with_blocks.get("/timeline?since=2026-05-20T11:00:00%2B00:00&limit=100")

    assert response.status_code == 200
    blocks = response.json()["data"]
    assert len(blocks) == 2
    for b in blocks:
        assert b["start_time"] >= "2026-05-20T11:00:00"


def test_timeline_until_filter(client_with_blocks) -> None:
    """until param excludes blocks after the given time."""
    response = client_with_blocks.get("/timeline?until=2026-05-20T10:03:00%2B00:00&limit=100")

    assert response.status_code == 200
    blocks = response.json()["data"]
    assert len(blocks) == 3
    for b in blocks:
        assert b["end_time"] <= "2026-05-20T10:03:01"


def test_timeline_since_until_range(client_with_blocks) -> None:
    """Combining since and until returns only blocks in the window."""
    response = client_with_blocks.get(
        "/timeline?since=2026-05-20T10:01:00%2B00:00&until=2026-05-20T11:01:00%2B00:00&limit=100"
    )

    assert response.status_code == 200
    blocks = response.json()["data"]
    assert len(blocks) == 3


def test_timeline_block_fields(client_with_blocks) -> None:
    """Each block in the response has all required fields."""
    response = client_with_blocks.get("/timeline?limit=1")
    block = response.json()["data"][0]

    for field in (
        "id",
        "start_time",
        "end_time",
        "timezone",
        "entries",
        "apps_used",
        "capture_count",
        "created_at",
        "helpful_intent_tags",
    ):
        assert field in block, f"missing field: {field}"

    assert isinstance(block["entries"], list)
    assert isinstance(block["apps_used"], list)
    assert isinstance(block["helpful_intent_tags"], list)


def test_timeline_limit_max_capped(client_with_blocks) -> None:
    """limit values above 200 are rejected by FastAPI validation."""
    response = client_with_blocks.get("/timeline?limit=201")
    assert response.status_code == 422


def test_timeline_limit_min_capped(client_with_blocks) -> None:
    """limit=0 is rejected by FastAPI validation."""
    response = client_with_blocks.get("/timeline?limit=0")
    assert response.status_code == 422

"""End-to-end tests for the REST API layer."""

from __future__ import annotations

from fastapi.testclient import TestClient

from persome.api import build_api_app
from persome.api.routes import set_config
from persome.config import Config, ModelConfig


def test_health_returns_ok() -> None:
    """GET /health must return the documented envelope immediately."""
    client = TestClient(build_api_app())
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body == {"success": True, "data": {"status": "ok"}}


def test_schema_returns_prompt() -> None:
    """GET /schema must return the memory schema markdown."""
    client = TestClient(build_api_app())
    response = client.get("/schema")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert "schema" in body["data"]
    assert "# Memory" in body["data"]["schema"]


def test_config_returns_resolved_config() -> None:
    """GET /config must return the injected configuration, not a fallback."""
    cfg = Config(models={"default": ModelConfig(model="mutant-test-model")})
    set_config(cfg)
    client = TestClient(build_api_app())
    response = client.get("/config")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["models"]["default"]["model"] == "mutant-test-model"


def test_memories_empty_database(ac_root) -> None:
    """GET /memories on an empty database returns an empty list."""
    client = TestClient(build_api_app())
    response = client.get("/memories")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["count"] == 0
    assert body["data"]["files"] == []


def test_search_empty_database(ac_root) -> None:
    """GET /search on an empty database returns empty results."""
    client = TestClient(build_api_app())
    response = client.get("/search?query=test")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["query"] == "test"
    assert body["data"]["results"] == []


def test_activity_empty_database(ac_root) -> None:
    """GET /activity on an empty database returns empty entries."""
    client = TestClient(build_api_app())
    response = client.get("/activity")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["count"] == 0
    assert body["data"]["entries"] == []


# ─── Regression: empty query-string parameters must not 422 or 500 ─────────


def test_search_with_empty_string_params(ac_root) -> None:
    """Empty since/until/paths query params must be ignored, not crash."""
    client = TestClient(build_api_app())
    response = client.get("/search?query=test&since=&until=&paths=")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["query"] == "test"
    assert body["data"]["results"] == []


def test_activity_with_empty_since_and_prefix_filter(ac_root) -> None:
    """Empty since/prefix_filter query params must be ignored, not crash."""
    client = TestClient(build_api_app())
    response = client.get("/activity?since=&prefix_filter=")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["count"] == 0
    assert body["data"]["entries"] == []


def test_read_memory_with_empty_since_until(ac_root) -> None:
    """Empty since/until on a missing file must 404, not 500."""
    client = TestClient(build_api_app())
    response = client.get("/memories/no-such-file.md?since=&until=")

    assert response.status_code == 404


def test_captures_search_with_empty_string_params(ac_root) -> None:
    """Empty since/until/app_name query params must be ignored, not crash."""
    client = TestClient(build_api_app())
    response = client.get("/captures?query=test&since=&until=&app_name=")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["query"] == "test"
    assert body["data"]["results"] == []


def test_current_context_with_empty_app_filter(ac_root) -> None:
    """Empty app_filter query param must be ignored, not crash."""
    client = TestClient(build_api_app())
    response = client.get("/captures/current?app_filter=")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert "recent_captures_headline" in body["data"]

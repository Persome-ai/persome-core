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


def _fake_enqueue_run(cfg, *, kind, trigger, dispatch_source, title="", payload=None):
    """Stand-in for recorder.enqueue_run: runs the REAL store dedup so the route
    sees real ``(run_id, deduped)``, but skips the dispatcher wake."""
    from persome.store import agent_runs as ar_store
    from persome.store import fts

    with fts.cursor() as conn:
        deduped = ar_store.find_queued_dup(conn, kind=kind, payload=payload) is not None
        rid = ar_store.enqueue(
            conn,
            kind=kind,
            trigger=trigger,
            dispatch_source=dispatch_source,
            title=title,
            payload=payload,
        )
    return rid, deduped


def test_bootstrap_run_returns_queued(ac_root, monkeypatch) -> None:
    """Phase 1b: POST /bootstrap/run enqueues via run-dispatcher and returns
    {status: queued, run_id, deduped}."""
    from persome.runs import recorder

    monkeypatch.setattr(recorder, "enqueue_run", _fake_enqueue_run)
    client = TestClient(build_api_app())
    response = client.post("/bootstrap/run")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "queued"
    assert isinstance(data["run_id"], int)
    assert data["deduped"] is False  # fresh enqueue


def test_bootstrap_run_deduplicates_concurrent_calls(ac_root, monkeypatch) -> None:
    """Phase 1b: concurrent POST /bootstrap/run with the SAME selection fold into
    the same queued row; the second carries ``deduped=true`` (#396)."""
    from persome.runs import recorder

    monkeypatch.setattr(recorder, "enqueue_run", _fake_enqueue_run)
    client = TestClient(build_api_app())
    r1 = client.post("/bootstrap/run")
    r2 = client.post("/bootstrap/run")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["data"]["run_id"] == r2.json()["data"]["run_id"]
    assert r1.json()["data"]["deduped"] is False
    assert r2.json()["data"]["deduped"] is True


def test_bootstrap_run_different_selection_opens_new_run(ac_root, monkeypatch) -> None:
    """#397: re-triggering /bootstrap/run with a CHANGED selection (different
    shallow/exclude → different payload) must NOT fold into the stale queued row;
    it opens a new run so the user's latest choice isn't silently dropped."""
    from persome.runs import recorder

    monkeypatch.setattr(recorder, "enqueue_run", _fake_enqueue_run)
    client = TestClient(build_api_app())
    r1 = client.post("/bootstrap/run")  # deep, no exclude
    r2 = client.post("/bootstrap/run", params={"shallow": True, "exclude": "Documents"})
    assert r1.status_code == 200
    assert r2.status_code == 200
    # Different payload → distinct run, latest selection wins.
    assert r1.json()["data"]["run_id"] != r2.json()["data"]["run_id"]
    assert r2.json()["data"]["deduped"] is False


# ─── GET /threads (work threads — menu-bar consumption) ──────────────────────


def test_threads_returns_work_threads(ac_root) -> None:
    """GET /threads lists work threads (newest first) with the active one flagged."""
    from persome.store import fts
    from persome.workthread import store as wt_store
    from persome.workthread.model import WorkThread

    with fts.cursor() as conn:
        wt_store.insert_thread(
            conn, WorkThread(id="t1", title="写 OCR PR", status="active", total_active_minutes=42)
        )
        wt_store.insert_thread(
            conn, WorkThread(id="t2", title="读论文", status="background", total_active_minutes=10)
        )

    client = TestClient(build_api_app())
    r = client.get("/threads")
    assert r.status_code == 200
    data = r.json()["data"]
    by_title = {t["title"]: t for t in data["threads"]}
    assert "写 OCR PR" in by_title and "读论文" in by_title
    # exactly the active thread is flagged + surfaced as active_id
    active = [t for t in data["threads"] if t["active"]]
    assert len(active) == 1 and active[0]["title"] == "写 OCR PR"
    assert data["active_id"] == "t1"
    # only UI-safe scalar fields are exposed (no internal bindings/evidence)
    pr = by_title["写 OCR PR"]
    assert "bindings" not in pr and "origin_evidence" not in pr
    assert pr["total_active_minutes"] == 42


def test_threads_status_filter(ac_root) -> None:
    from persome.store import fts
    from persome.workthread import store as wt_store
    from persome.workthread.model import WorkThread

    with fts.cursor() as conn:
        wt_store.insert_thread(conn, WorkThread(id="a", title="A", status="active"))
        wt_store.insert_thread(conn, WorkThread(id="b", title="B", status="background"))

    client = TestClient(build_api_app())
    r = client.get("/threads", params={"status": "active"})
    assert r.status_code == 200
    titles = {t["title"] for t in r.json()["data"]["threads"]}
    assert titles == {"A"}


def test_threads_empty_database(ac_root) -> None:
    client = TestClient(build_api_app())
    r = client.get("/threads")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["threads"] == [] and data["active_id"] is None

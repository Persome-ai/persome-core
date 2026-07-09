"""HTTP write-side routes for agent_runs (Phase 1b Task 4)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from persome.api import build_api_app
from persome.config import load as load_config
from persome.store import agent_runs as store
from persome.store import fts


def _client(cfg=None):
    if cfg is None:
        cfg = load_config()
    return TestClient(build_api_app(cfg))


def test_post_runs_enqueues_dream(ac_root, monkeypatch) -> None:
    """POST /runs with kind=dream enqueues and returns run_id."""
    from persome.runs import recorder

    enqueued: list[dict] = []

    def fake_enqueue(cfg, *, kind, trigger, dispatch_source, title="", payload=None):
        enqueued.append({"kind": kind, "trigger": trigger})
        # actually enqueue in DB; mirror enqueue_run's (run_id, deduped) shape.
        with fts.cursor() as conn:
            deduped = store.find_queued_dup(conn, kind=kind, payload=payload) is not None
            rid = store.enqueue(
                conn,
                kind=kind,
                trigger=trigger,
                dispatch_source=dispatch_source,
                title=title,
                payload=payload,
            )
        return rid, deduped

    monkeypatch.setattr(recorder, "enqueue_run", fake_enqueue)
    cfg = load_config()
    resp = _client(cfg).post("/runs", json={"kind": "dream"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert "run_id" in data
    assert data["status"] == "queued"
    assert enqueued[0]["kind"] == "dream"
    assert enqueued[0]["trigger"] == "user"


def test_post_runs_dedups(ac_root, monkeypatch) -> None:
    """Two consecutive POST /runs {kind: dream} fold into the same queued run_id."""
    from persome.runs import recorder

    def fake_enqueue(cfg, *, kind, trigger, dispatch_source, title="", payload=None):
        with fts.cursor() as conn:
            deduped = store.find_queued_dup(conn, kind=kind, payload=payload) is not None
            rid = store.enqueue(
                conn,
                kind=kind,
                trigger=trigger,
                dispatch_source=dispatch_source,
                title=title,
                payload=payload,
            )
        return rid, deduped

    monkeypatch.setattr(recorder, "enqueue_run", fake_enqueue)
    client = _client()
    r1 = client.post("/runs", json={"kind": "dream"})
    r2 = client.post("/runs", json={"kind": "dream"})
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["data"]["run_id"] == r2.json()["data"]["run_id"]


def test_post_runs_rejects_unknown_kind(ac_root) -> None:
    """POST /runs with an unknown kind returns 422."""
    resp = _client().post("/runs", json={"kind": "unknown_xyz"})
    assert resp.status_code == 422


def test_patch_runs_cancel(ac_root) -> None:
    """PATCH /runs/{id} with action=cancel cancels a queued run."""
    with fts.cursor() as conn:
        rid = store.enqueue(conn, kind="dream", trigger="user", dispatch_source="user")
    resp = _client().patch(f"/runs/{rid}", json={"action": "cancel"})
    assert resp.status_code == 200
    with fts.cursor() as conn:
        run = store.get_run(conn, rid)
    assert run.status == "cancelled"


def test_patch_runs_cancel_running_fails(ac_root) -> None:
    """PATCH /runs/{id} cancel on an already-running run returns 409."""
    with fts.cursor() as conn:
        rid = store.enqueue(conn, kind="dream", trigger="user", dispatch_source="user")
        store.mark_running(conn, rid)
    resp = _client().patch(f"/runs/{rid}", json={"action": "cancel"})
    assert resp.status_code == 409


def test_get_run_detail(ac_root) -> None:
    """GET /runs/{id} returns run detail including events list."""
    with fts.cursor() as conn:
        rid = store.enqueue(conn, kind="dream", trigger="user", dispatch_source="user")
        store.mark_running(conn, rid)
        store.append_event(conn, rid, "progress", {"value": 0.3})

    resp = _client().get(f"/runs/{rid}")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["id"] == rid
    assert data["status"] == "running"
    assert len(data["events"]) == 1
    assert data["events"][0]["type"] == "progress"


def test_get_run_detail_not_found(ac_root) -> None:
    """GET /runs/{id} for non-existent run returns 404."""
    resp = _client().get("/runs/99999")
    assert resp.status_code == 404

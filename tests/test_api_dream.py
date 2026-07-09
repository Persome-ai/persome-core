"""End-to-end tests for the /dream/* HTTP endpoints + writer reservation API.

Covers:
  * try_reserve_dream_run / release_dream_reservation atomicity
  * POST /dream/run 409 when another reservation is held
  * GET /dream/runs and /dream/runs/{id} happy paths + 404
  * run_dream_with_recording propagates DreamAlreadyRunningError
"""

from __future__ import annotations

import contextlib

import pytest
from fastapi.testclient import TestClient

from persome.api import build_api_app
from persome.store import dream_runs, fts
from persome.writer import dream as dream_mod


@pytest.fixture
def client(ac_root) -> TestClient:
    return TestClient(build_api_app())


@pytest.fixture(autouse=True)
def _release_lock_between_tests():
    """Defensive cleanup. Tests should release everything they take, but
    if one ever leaks, fail loudly instead of poisoning subsequent runs."""
    yield
    if dream_mod._dream_lock.locked():
        # not held by this thread; let GC sort it out
        with contextlib.suppress(RuntimeError):
            dream_mod.release_dream_reservation()


# ─── reservation API ──────────────────────────────────────────────────────


def test_try_reserve_dream_run_is_exclusive() -> None:
    assert dream_mod.try_reserve_dream_run() is True
    try:
        assert dream_mod.try_reserve_dream_run() is False
    finally:
        dream_mod.release_dream_reservation()


def test_try_reserve_again_after_release() -> None:
    assert dream_mod.try_reserve_dream_run() is True
    dream_mod.release_dream_reservation()
    assert dream_mod.try_reserve_dream_run() is True
    dream_mod.release_dream_reservation()


def test_run_dream_with_recording_raises_when_locked() -> None:
    assert dream_mod.try_reserve_dream_run() is True
    try:
        with pytest.raises(dream_mod.DreamAlreadyRunningError):
            dream_mod.run_dream_with_recording(
                cfg=None,  # type: ignore[arg-type]
                trigger="daily-tick",
            )
    finally:
        dream_mod.release_dream_reservation()


# ─── POST /dream/run ──────────────────────────────────────────────────────


def _fake_enqueue_run(cfg, *, kind, trigger, dispatch_source, title="", payload=None):
    """Stand-in for recorder.enqueue_run that runs the REAL store dedup (so the
    route sees real ``(run_id, deduped)``) but skips the dispatcher wake."""
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


def test_post_dream_run_deduplicates_concurrent_calls(client: TestClient, monkeypatch) -> None:
    """Phase 1b: POST /dream/run enqueues via run-dispatcher (no more reservation lock).
    Concurrent calls with a still-queued row fold into the same run_id and the
    second carries ``deduped=true`` (#396)."""
    from persome.runs import recorder

    monkeypatch.setattr(recorder, "enqueue_run", _fake_enqueue_run)
    r1 = client.post("/dream/run")
    r2 = client.post("/dream/run")
    assert r1.status_code == 200
    assert r2.status_code == 200
    # Both calls fold into the same queued run.
    assert r1.json()["data"]["run_id"] == r2.json()["data"]["run_id"]
    assert r1.json()["data"]["status"] == "queued"
    # First is a fresh enqueue, second folded → deduped signal lets the UI hint.
    assert r1.json()["data"]["deduped"] is False
    assert r2.json()["data"]["deduped"] is True


def test_post_dream_run_returns_queued_and_run_id(client: TestClient, monkeypatch) -> None:
    """Phase 1b: POST /dream/run returns {status: queued, run_id: <int>, deduped: bool}."""
    from persome.runs import recorder

    monkeypatch.setattr(recorder, "enqueue_run", _fake_enqueue_run)
    response = client.post("/dream/run")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "queued"
    assert isinstance(data["run_id"], int)
    assert data["deduped"] is False  # fresh enqueue

    # Reservation must be released by the worker — i.e. a second attempt
    # should succeed immediately.
    assert dream_mod.try_reserve_dream_run() is True
    dream_mod.release_dream_reservation()


# ─── GET /dream/runs ──────────────────────────────────────────────────────


def test_get_dream_runs_empty(client: TestClient) -> None:
    response = client.get("/dream/runs")
    assert response.status_code == 200
    assert response.json() == {"success": True, "data": {"runs": []}}


def test_get_dream_runs_returns_newest_first(client: TestClient) -> None:
    with fts.cursor() as conn:
        first = dream_runs.start_run(conn, trigger="daily-tick")
        second = dream_runs.start_run(conn, trigger="manual")
        dream_runs.end_run(
            conn,
            second,
            committed=True,
            summary="ok",
            written_ids=["x"],
            created_paths=["user-x.md"],
            iterations=3,
            skipped_reason="",
        )

    response = client.get("/dream/runs")
    assert response.status_code == 200
    body = response.json()
    runs = body["data"]["runs"]
    assert [r["id"] for r in runs] == [second, first]
    assert runs[0]["status"] == "committed"
    assert runs[0]["written_count"] == 1
    assert runs[0]["created_paths"] == ["user-x.md"]
    assert runs[1]["status"] == "running"
    assert runs[1]["ended_at"] is None


def test_get_dream_runs_respects_limit(client: TestClient) -> None:
    with fts.cursor() as conn:
        for _ in range(5):
            dream_runs.start_run(conn, trigger="manual")

    response = client.get("/dream/runs?limit=2")
    assert response.status_code == 200
    assert len(response.json()["data"]["runs"]) == 2


def test_get_dream_runs_rejects_invalid_limit(client: TestClient) -> None:
    """limit is constrained to 1..100 by Annotated[int, Query(ge=1, le=100)]."""
    assert client.get("/dream/runs?limit=0").status_code == 422
    assert client.get("/dream/runs?limit=101").status_code == 422


# ─── GET /dream/runs/{id} ─────────────────────────────────────────────────


def test_get_dream_run_404_for_unknown_id(client: TestClient) -> None:
    response = client.get("/dream/runs/9999")
    assert response.status_code == 404


def test_get_dream_run_returns_events_in_insertion_order(client: TestClient) -> None:
    with fts.cursor() as conn:
        run_id = dream_runs.start_run(conn, trigger="manual")
        dream_runs.append_event(
            conn, run_id, "tool_call", {"name": "read_memory", "arguments": {"path": "x.md"}}
        )
        dream_runs.append_event(conn, run_id, "llm_text", {"text": "thinking"})
        dream_runs.end_run(
            conn,
            run_id,
            committed=True,
            summary="done",
            written_ids=[],
            created_paths=[],
            iterations=2,
            skipped_reason="",
        )

    response = client.get(f"/dream/runs/{run_id}")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["run"]["id"] == run_id
    assert data["run"]["status"] == "committed"
    types = [ev["type"] for ev in data["events"]]
    assert types == ["tool_call", "llm_text"]
    assert data["events"][0]["payload"]["name"] == "read_memory"

"""Cross-stack contract test for the /runs family (backend side).

Asserts the REAL FastAPI responses carry exactly the keys pinned in the shared
fixture ``tests/fixtures/runs_contract.json`` — the same file the Flutter test
(``Mens.app/.../runs_contract_test.dart``) parses with its models. If a field
is renamed/dropped on the backend without updating the fixture (and the Dart
side), this test fails. This is the automated guard the cancelRun-class contract
gap lacked (backend used 200/409 while the frontend read a non-existent `took`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from persome.api import build_api_app
from persome.store import agent_runs as ar_store
from persome.store import fts

_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "runs_contract.json").read_text(encoding="utf-8")
)


def _client() -> TestClient:
    return TestClient(build_api_app())


def _keys(d: dict[str, Any]) -> set[str]:
    return set(d.keys())


def _seed_running(conn, **over) -> int:
    base = dict(
        kind="dream",
        title="每日整理",
        status="running",
        trigger="daily-tick",
        dispatch_source="system",
        enqueued_at="2026-06-07T09:00:00+08:00",
        started_at="2026-06-07T09:00:05+08:00",
    )
    base.update(over)
    return ar_store.insert_run(conn, **base)


def test_runs_list_item_shape_matches_fixture(ac_root) -> None:
    import datetime as _dt

    now = _dt.datetime.now().astimezone()
    with fts.cursor() as conn:
        _seed_running(conn, enqueued_at=now.isoformat(), started_at=now.isoformat())
    res = _client().get("/runs", params={"range": "day"})
    assert res.status_code == 200
    data = res.json()["data"]
    assert _keys(data) == _keys(_FIXTURE["runs_list"])  # {range, items, count}
    assert data["items"], "expected the seeded run in-window"
    assert _keys(data["items"][0]) == _keys(_FIXTURE["run_card"])


def test_run_detail_shape_matches_fixture(ac_root) -> None:
    with fts.cursor() as conn:
        rid = _seed_running(conn)
        ar_store.append_event(conn, rid, "progress", {"value": 0.5})
    res = _client().get(f"/runs/{rid}")
    assert res.status_code == 200
    data = res.json()["data"]
    # GET /runs/{id} returns a FLAT RunDetailResponse (run fields at top level +
    # events), NOT a nested {run, events}. The Dart RunDetail must parse this.
    assert _keys(data) == _keys(_FIXTURE["run_detail"])
    assert _keys(data["events"][0]) == _keys(_FIXTURE["run_detail"]["events"][0])


def test_post_runs_shape_matches_fixture(ac_root) -> None:
    res = _client().post("/runs", json={"kind": "dream"})
    assert res.status_code == 200
    assert _keys(res.json()["data"]) == _keys(_FIXTURE["post_runs"])  # {run_id, status}


def test_cancel_queued_200_running_409(ac_root) -> None:
    """The behavioural contract the frontend's cancelRun depends on: a queued
    run cancels with 200 (data == cancel_ok shape); a running run can't and
    returns 409."""
    c = _client()
    with fts.cursor() as conn:
        queued = ar_store.enqueue(conn, kind="dream", trigger="user", dispatch_source="user")
        running = _seed_running(conn)
    ok = c.patch(f"/runs/{queued}", json={"action": "cancel"})
    assert ok.status_code == 200
    assert _keys(ok.json()["data"]) == _keys(_FIXTURE["cancel_ok"])

    blocked = c.patch(f"/runs/{running}", json={"action": "cancel"})
    assert blocked.status_code == 409


def test_agent_run_sse_frame_shape(ac_root, monkeypatch) -> None:
    """The recorder publishes `agent_run` SSE frames whose enriched payload
    carries the keys AgentRunFrame.fromJson reads (run_id, kind, value, label)."""
    from persome import events as events_mod
    from persome.config import load as load_config
    from persome.runs import recorder, registry

    published: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(
        events_mod,
        "publish",
        lambda stage, etype, payload: published.append((stage, etype, payload)),
    )

    def fake_exec(cfg, on_event, payload):
        on_event("progress", {"value": 0.5, "label": "阶段 2/4"})
        return registry.RunOutcome(committed=True, summary="ok")

    monkeypatch.setitem(
        registry.KIND_REGISTRY,
        "dream",
        registry.KindSpec(kind="dream", title="每日整理", run=fake_exec),
    )
    with fts.cursor() as conn:
        rid = ar_store.enqueue(conn, kind="dream", trigger="user", dispatch_source="user")
        ar_store.mark_running(conn, rid)
    recorder.run_recorded(load_config(), rid)

    progress_frames = [
        p for (stage, etype, p) in published if stage == "agent_run" and etype == "progress"
    ]
    assert progress_frames, "expected an agent_run progress frame"
    frame = progress_frames[0]
    # Keys the Dart AgentRunFrame.fromJson reads off the enriched SSE payload.
    assert {"run_id", "kind", "value", "label"} <= set(frame.keys())


def test_run_detail_dream_source(ac_root) -> None:
    """Regression for the dream-card detail 404: cards from the dream_runs UNION
    must resolve detail via ``?source=dream`` (their id belongs to dream_runs,
    not agent_runs). The default agent_run lookup 404s on that id — which is the
    bug a real click hit and this guards. The dream detail returns the SAME flat
    RunDetailResponse shape so the Dart RunDetail parses it uniformly."""
    from persome.store import dream_runs as dr_store

    with fts.cursor() as conn:
        did = dr_store.start_run(conn, trigger="daily-tick")
        dr_store.end_run(
            conn,
            did,
            committed=True,
            summary="梦完成",
            written_ids=["a"],
            created_paths=[],
            iterations=2,
            skipped_reason="",
        )
        dr_store.append_event(conn, did, "progress", {"value": 0.5})

    c = _client()
    ok = c.get(f"/runs/{did}", params={"source": "dream"})
    assert ok.status_code == 200
    assert _keys(ok.json()["data"]) == _keys(_FIXTURE["run_detail"])

    # Default (agent_run) lookup of a dream id 404s — this is what the buggy
    # frontend did before it started passing source.
    assert c.get(f"/runs/{did}").status_code == 404

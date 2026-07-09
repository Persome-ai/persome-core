"""REST 端到端：WorkThread `/work/context` + `/work/threads/{id}` 纠错闭集（S4）."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from persome.api import build_api_app
from persome.store import fts
from persome.workthread import store as wt_store
from persome.workthread.model import WorkThread


@pytest.fixture
def client(ac_root):
    return TestClient(build_api_app())


def _seed(status: str = "active", **kw) -> WorkThread:
    defaults = dict(
        id="",
        title="Kevin 交办：意图识别链路优化",
        status=status,
        origin_type="assignment",
        origin_actor="Kevin",
        first_seen="2026-06-10T09:00",
        last_active="2026-06-12T10:00",
        total_active_minutes=192,
        approximate=True,
        confidence=0.8,
    )
    defaults.update(kw)
    t = WorkThread(**defaults)
    with fts.cursor() as conn:
        wt_store.insert_thread(conn, t)
    return t


def test_work_context_empty(client) -> None:
    resp = client.get("/work/context")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["active_thread"] is None
    assert data["background_threads"] == []
    assert "thread_churn" in data["stats"]


def test_work_context_with_active_thread(client) -> None:
    t = _seed()
    _seed(status="background", title="周报草稿", origin_actor="", origin_type="self_initiated")
    data = client.get("/work/context").json()["data"]
    active = data["active_thread"]
    assert active["thread_id"] == t.id
    assert active["total_minutes"] == 192
    assert active["approximate"] is True  # 时间账契约：approximate 标记透传
    assert active["origin"]["actor"] == "Kevin"
    assert len(data["background_threads"]) == 1


def test_correct_confirm_and_pin(client) -> None:
    t = _seed()
    resp = client.patch(f"/work/threads/{t.id}", json={"action": "confirm"})
    assert resp.status_code == 200 and resp.json()["data"]["ok"]
    resp = client.patch(f"/work/threads/{t.id}", json={"action": "pin"})
    assert resp.status_code == 200
    with fts.cursor() as conn:
        got = wt_store.get_thread(conn, t.id)
    assert got.pinned and got.confidence >= 0.85


def test_correct_invalid_action_400(client) -> None:
    t = _seed()
    resp = client.patch(f"/work/threads/{t.id}", json={"action": "delete"})
    assert resp.status_code == 400


def test_correct_unknown_thread_400(client) -> None:
    resp = client.patch("/work/threads/nope", json={"action": "confirm"})
    assert resp.status_code == 400


def test_correct_merge_via_rest(client) -> None:
    a = _seed(title="线 A")
    b = _seed(title="周报支线", origin_actor="", origin_type="self_initiated")
    resp = client.patch(f"/work/threads/{b.id}", json={"action": "merge", "into_id": a.id})
    assert resp.status_code == 200
    with fts.cursor() as conn:
        assert wt_store.get_thread(conn, b.id).status == "superseded"

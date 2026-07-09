"""Tests for GET /runs — the Calendar work-board read endpoint."""

from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient

from persome.api import build_api_app
from persome.store import agent_runs as ar_store
from persome.store import dream_runs as dr_store
from persome.store import fts


def _iso(dt: datetime) -> str:
    return dt.astimezone().isoformat()


def test_runs_today_unions_agent_and_dream(ac_root) -> None:
    now = datetime.now().astimezone()
    with fts.cursor() as conn:
        ar_store.insert_run(
            conn,
            kind="bootstrap",
            title="冷启动",
            status="running",
            trigger="user",
            dispatch_source="user",
            enqueued_at=_iso(now),
            started_at=_iso(now),
        )
        # a dream_runs row (legacy table) must surface via UNION
        dr_id = dr_store.start_run(conn, trigger="daily-tick")
        dr_store.end_run(
            conn,
            dr_id,
            committed=True,
            summary="梦完成",
            written_ids=["a"],
            created_paths=[],
            iterations=3,
            skipped_reason="",
        )

    client = TestClient(build_api_app())
    res = client.get("/runs", params={"range": "day"})
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    items = body["data"]["items"]
    sources = {(it["source"], it["kind"]) for it in items}
    assert ("agent_run", "bootstrap") in sources
    assert ("dream", "dream") in sources
    assert body["data"]["count"] == len(items)


def test_runs_status_filter(ac_root) -> None:
    now = datetime.now().astimezone()
    with fts.cursor() as conn:
        ar_store.insert_run(
            conn,
            kind="dream",
            title="跑完",
            status="committed",
            trigger="daily-tick",
            dispatch_source="system",
            enqueued_at=_iso(now),
            started_at=_iso(now),
            ended_at=_iso(now),
        )
        ar_store.insert_run(
            conn,
            kind="dream",
            title="排队",
            status="queued",
            trigger="user",
            dispatch_source="user",
            enqueued_at=_iso(now),
        )

    client = TestClient(build_api_app())
    res = client.get("/runs", params={"range": "day", "status": "queued"})
    items = res.json()["data"]["items"]
    assert [it["title"] for it in items] == ["排队"]


def test_runs_empty_window_is_empty(ac_root) -> None:
    client = TestClient(build_api_app())
    res = client.get("/runs", params={"range": "day"})
    assert res.status_code == 200
    assert res.json()["data"] == {"range": "day", "items": [], "count": 0}


def test_runs_explicit_window(ac_root) -> None:
    from datetime import timedelta

    now = datetime.now().astimezone()
    with fts.cursor() as conn:
        ar_store.insert_run(
            conn,
            kind="dream",
            title="窗口内",
            status="committed",
            trigger="daily-tick",
            dispatch_source="system",
            enqueued_at=now.isoformat(),
            started_at=now.isoformat(),
        )
        ar_store.insert_run(
            conn,
            kind="dream",
            title="窗口外",
            status="committed",
            trigger="daily-tick",
            dispatch_source="system",
            enqueued_at=(now - timedelta(days=40)).isoformat(),
            started_at=(now - timedelta(days=40)).isoformat(),
        )
    start = (now - timedelta(days=1)).isoformat()
    end = (now + timedelta(days=1)).isoformat()
    client = TestClient(build_api_app())
    res = client.get("/runs", params={"start": start, "end": end})
    assert res.status_code == 200
    titles = [i["title"] for i in res.json()["data"]["items"]]
    assert "窗口内" in titles and "窗口外" not in titles


def test_runs_bad_iso_returns_422(ac_root) -> None:
    client = TestClient(build_api_app())
    res = client.get("/runs", params={"start": "not-a-date", "end": "also-bad"})
    assert res.status_code == 422

"""End-to-end tests for the home-dashboard endpoints: /agent/now + /agenda.

Both endpoints derive purely from real state — these tests seed that real state
(dream_runs/dream_events rows, intents rows, pid + paused flag, capture-buffer
files) and assert the derived shape, including the empty/idle fallbacks.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from persome import paths
from persome.api import build_api_app
from persome.intent import store as intent_store
from persome.intent.ontology import Intent
from persome.store import dream_runs, fts


@pytest.fixture
def client(ac_root) -> TestClient:
    return TestClient(build_api_app())


def _mark_daemon_running() -> None:
    """Write a live pid so capture-state helpers report active/paused, not
    stopped. Using our own pid guarantees ``os.kill(pid, 0)`` succeeds."""
    paths.pid_file().write_text(str(os.getpid()))


# ─── GET /agent/now ──────────────────────────────────────────────────────────


def test_agent_now_idle_when_no_runs(client: TestClient) -> None:
    """No dream runs at all → idle snapshot with neutral title, no timer."""
    response = client.get("/agent/now")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "idle"
    assert data["started_at"] is None
    assert data["elapsed_seconds"] is None
    assert data["capture"] == "stopped"  # no pid written
    assert data["title"]  # neutral placeholder, non-empty
    assert isinstance(data["sub_status"], list)


def test_agent_now_running_reports_timer_and_subevents(client: TestClient) -> None:
    """A running dream → status=running, started_at echoed, elapsed_seconds
    computed, and sub_status pulled from the run's real events."""
    _mark_daemon_running()
    with fts.cursor() as conn:
        run_id = dream_runs.start_run(conn, trigger="manual")
        dream_runs.append_event(
            conn, run_id, "tool_call", {"name": "read_memory", "arguments": {"path": "x.md"}}
        )
        dream_runs.append_event(conn, run_id, "llm_text", {"text": "Reviewing today's activity"})

    response = client.get("/agent/now")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "running"
    assert data["started_at"] is not None
    assert isinstance(data["elapsed_seconds"], int)
    assert data["elapsed_seconds"] >= 0
    assert data["capture"] == "active"
    # Sub-status lines come from the real events (newest first), max 3.
    texts = [s["text"] for s in data["sub_status"]]
    assert len(texts) <= 3
    assert any("Reviewing today's activity" in t for t in texts)
    assert any("read_memory" in t for t in texts)


def test_agent_now_idle_uses_last_summary(client: TestClient) -> None:
    """When the latest run is finished, the idle title reflects its summary."""
    with fts.cursor() as conn:
        run_id = dream_runs.start_run(conn, trigger="daily-tick")
        dream_runs.end_run(
            conn,
            run_id,
            committed=True,
            summary="Promoted a skill and appended 2 preferences",
            written_ids=["a"],
            created_paths=["user-preferences.md"],
            iterations=5,
            skipped_reason="",
        )

    response = client.get("/agent/now")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "idle"
    assert "Promoted a skill" in data["title"]


def test_agent_now_paused_capture(client: TestClient) -> None:
    """Paused flag present + live pid → capture='paused'."""
    _mark_daemon_running()
    paths.paused_flag().write_text(datetime.now().isoformat())
    response = client.get("/agent/now")
    assert response.status_code == 200
    assert response.json()["data"]["capture"] == "paused"


# ─── GET /agenda ─────────────────────────────────────────────────────────────


def _insert_scheduled_intent(
    *, when_text: str, ts: str, kind: str = "meeting", people: list[str] | None = None
) -> None:
    intent = Intent(
        kind=kind,
        scope="session-test",
        confidence=0.8,
        rationale=f"{kind} about the roadmap",
        status="open",
        ts=ts,
        payload={"when_text": when_text, "with": people or []},
    )
    with fts.cursor() as conn:
        intent_store.insert_intent(conn, intent)


def test_agenda_empty_when_no_intents(client: TestClient) -> None:
    """No temporally-anchored intents → empty list, never fabricated."""
    response = client.get("/agenda?range=today")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["range"] == "today"
    assert data["items"] == []
    assert data["count"] == 0


def test_agenda_today_includes_today_intent(client: TestClient) -> None:
    now = datetime.now().astimezone()
    _insert_scheduled_intent(when_text="今天下午 3 点", ts=now.isoformat(), people=["Alice"])
    response = client.get("/agenda?range=today")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["count"] == 1
    item = data["items"][0]
    assert item["time_label"] == "今天下午 3 点"
    assert item["kind"] == "meeting"
    assert item["source"] == "intent"
    assert item["with"] == ["Alice"]


def test_agenda_today_includes_naive_local_ts_intent(client: TestClient) -> None:
    """SF3 regression: production intents are written by the unified sink as
    NAIVE local ts (``datetime.now().isoformat(timespec='minutes')`` → no tz
    offset). The window must still include them — the earlier SQLite string
    comparison against tz-aware boundaries silently dropped these. We seed the
    exact production shape here (the previous test used tz-aware ts, which
    masked the bug)."""
    naive_ts = datetime.now().isoformat(timespec="minutes")  # e.g. 2026-06-04T15:33
    assert "+" not in naive_ts and "Z" not in naive_ts  # truly naive
    _insert_scheduled_intent(when_text="今天下午 3 点", ts=naive_ts)
    response = client.get("/agenda?range=today")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["count"] == 1
    assert data["items"][0]["time_label"] == "今天下午 3 点"


def test_agenda_week_includes_naive_local_ts_intent(client: TestClient) -> None:
    """Same naive-local production shape, in the week window."""
    naive_ts = datetime.now().isoformat(timespec="minutes")
    assert "+" not in naive_ts and "Z" not in naive_ts
    _insert_scheduled_intent(when_text="本周安排", ts=naive_ts)
    response = client.get("/agenda?range=week")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["count"] == 1
    assert data["items"][0]["time_label"] == "本周安排"


def test_agenda_today_excludes_intent_without_when_text(client: TestClient) -> None:
    """An intent with no temporal anchor must not appear (it's not scheduled)."""
    now = datetime.now().astimezone()
    _insert_scheduled_intent(when_text="", ts=now.isoformat())
    response = client.get("/agenda?range=today")
    assert response.status_code == 200
    assert response.json()["data"]["items"] == []


def test_agenda_today_excludes_old_intent(client: TestClient) -> None:
    """A scheduled intent recognized days ago is out of the today window."""
    old = (datetime.now().astimezone() - timedelta(days=3)).isoformat()
    _insert_scheduled_intent(when_text="上周五", ts=old)
    response = client.get("/agenda?range=today")
    assert response.status_code == 200
    assert response.json()["data"]["items"] == []


def test_agenda_week_includes_recent_intent(client: TestClient) -> None:
    """range=week spans Monday..Sunday; an intent recognized earlier this week
    that 'today' might exclude should still appear under week."""
    now = datetime.now().astimezone()
    # Pick a ts firmly inside this week but not today when possible.
    monday = (now - timedelta(days=now.weekday())).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    _insert_scheduled_intent(when_text="本周一站会", ts=monday.isoformat())
    response = client.get("/agenda?range=week")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["range"] == "week"
    assert data["count"] == 1
    assert data["items"][0]["time_label"] == "本周一站会"


def test_agenda_day_includes_today_intent(client: TestClient) -> None:
    """range=day is the single-day (today) window — same as range=today, but the
    echoed range stays 'day' for the caller."""
    now = datetime.now().astimezone()
    _insert_scheduled_intent(when_text="今天下午 3 点", ts=now.isoformat())
    response = client.get("/agenda?range=day")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["range"] == "day"
    assert data["count"] == 1
    assert data["items"][0]["time_label"] == "今天下午 3 点"


def test_agenda_day_excludes_old_intent(client: TestClient) -> None:
    """An intent recognized days ago is outside the single-day window."""
    old = (datetime.now().astimezone() - timedelta(days=3)).isoformat()
    _insert_scheduled_intent(when_text="上周五", ts=old)
    response = client.get("/agenda?range=day")
    assert response.status_code == 200
    assert response.json()["data"]["items"] == []


def test_agenda_month_includes_this_month_intent(client: TestClient) -> None:
    """range=month spans the 1st..last of the current month; an intent anywhere
    in-month appears even when it's outside today/this-week."""
    now = datetime.now().astimezone()
    first = now.replace(day=1, hour=9, minute=0, second=0, microsecond=0)
    _insert_scheduled_intent(when_text="本月规划", ts=first.isoformat())
    response = client.get("/agenda?range=month")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["range"] == "month"
    assert data["count"] == 1
    assert data["items"][0]["time_label"] == "本月规划"


def test_agenda_month_excludes_other_month_intent(client: TestClient) -> None:
    """An intent from the previous month is outside the month window."""
    now = datetime.now().astimezone()
    # Last day of the previous month (one day before this month's 1st).
    prev_month = now.replace(day=1, hour=9, minute=0, second=0, microsecond=0) - timedelta(days=1)
    _insert_scheduled_intent(when_text="上月旧事", ts=prev_month.isoformat())
    response = client.get("/agenda?range=month")
    assert response.status_code == 200
    assert response.json()["data"]["items"] == []


def test_agenda_sorts_by_real_instant_not_raw_string(client: TestClient) -> None:
    """#321 regression: ordering must use the parsed tz-aware instant, not the
    raw ``ts`` string.

    The unified sink writes naive-local ts (no offset); other rows may carry a
    tz-aware ts. Sorting the raw strings is lexicographic, so the offset suffix
    on an aware string can flip the order relative to the true instant. We seed
    two intents that share the same wall-clock prefix ``...T15:33``: a naive one
    (interpreted as local, the LATER real instant) and a tz-aware one in an
    east-of-local zone (``+09:00`` → one hour EARLIER in real time). Under raw
    string sort the aware row (longer string with the offset) wrongly sorts
    first; the correct "newest recognition first" order puts the naive row
    first. This proves the sort key is the parsed datetime, not ``ts``.
    """
    now = datetime.now().astimezone()
    base = now.replace(hour=15, minute=33, second=0, microsecond=0)
    # Naive-local ts → real instant 15:33 in local tz (the LATER one).
    later_naive = base.replace(tzinfo=None)
    # tz-aware ts one zone east → same wall clock = one hour earlier in real time;
    # its raw string "...T15:33+09:00" sorts AFTER the naive "...T15:33".
    earlier_aware = base.replace(tzinfo=timezone(timedelta(hours=9)))
    assert earlier_aware < later_naive.replace(tzinfo=now.tzinfo)  # aware is earlier
    _insert_scheduled_intent(
        when_text="较晚（naive）", ts=later_naive.isoformat(timespec="minutes")
    )
    _insert_scheduled_intent(
        when_text="较早（aware）", ts=earlier_aware.isoformat(timespec="minutes")
    )

    response = client.get("/agenda?range=today")
    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert len(items) == 2
    # Newest real instant first: the later naive intent, not the lexicographically
    # larger aware string.
    assert [it["time_label"] for it in items] == ["较晚（naive）", "较早（aware）"]


def test_agenda_item_has_no_internal_sort_field(client: TestClient) -> None:
    """The parsed sort datetime must not leak into the API payload."""
    now = datetime.now().astimezone()
    _insert_scheduled_intent(when_text="今天下午 3 点", ts=now.isoformat())
    response = client.get("/agenda?range=today")
    assert response.status_code == 200
    item = response.json()["data"]["items"][0]
    assert "_sort_dt" not in item


def test_agenda_invalid_range_falls_back_to_today(client: TestClient) -> None:
    response = client.get("/agenda?range=bogus")
    assert response.status_code == 200
    assert response.json()["data"]["range"] == "today"

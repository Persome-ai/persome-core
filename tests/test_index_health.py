"""Index-health self-check + capture heartbeat (index_health.py).

The contract under test: a silent-by-default runtime must let its owner and
downstream readers distinguish intentional silence (paused, idle) from a
broken pipeline (corrupt index, failing capture indexing, unindexed backlog),
and read surfaces must degrade explicitly instead of pretending health.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from persome import index_health, paths
from persome.config import Config
from persome.mcp import captures as captures_mod
from persome.store import fts


@pytest.mark.asyncio
async def test_health_tick_publishes_before_its_first_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked: list[Config] = []
    cfg = Config()

    async def cancel_after_check(function, passed_cfg):
        checked.append(passed_cfg)
        raise asyncio.CancelledError

    async def unexpected_sleep(seconds: float) -> None:
        pytest.fail(f"health tick slept for {seconds}s before its first publication")

    monkeypatch.setattr(index_health.asyncio, "to_thread", cancel_after_check)
    monkeypatch.setattr(index_health.asyncio, "sleep", unexpected_sleep)

    with pytest.raises(asyncio.CancelledError):
        await index_health.run_index_health_tick(cfg)

    assert checked == [cfg]


@pytest.fixture
def fresh_counters(monkeypatch: pytest.MonkeyPatch) -> dict:
    counters = {
        "captures_ok_total": 0,
        "last_capture_ok_at": None,
        "index_failures_total": 0,
        "consecutive_index_failures": 0,
        "last_index_ok_at": None,
        "last_index_error": None,
        "last_index_error_at": None,
        "queue_dropped_total": 0,
        "consecutive_queue_drops": 0,
        "last_queue_drop_at": None,
        "last_snapshot_ok": None,
        "last_snapshot_at": None,
        "last_snapshot_error": None,
    }
    monkeypatch.setattr(index_health, "_counters", counters)
    return counters


def _seed_schema() -> None:
    with fts.cursor():
        pass


def test_healthy_report_roundtrips_through_sidecar(ac_root: Path, fresh_counters: dict) -> None:
    _seed_schema()
    index_health.record_capture_ok()
    index_health.record_index_result(True)
    report = index_health.check_once(Config())
    assert report["status"] == "ok"
    assert report["index"]["status"] == "ok"
    assert report["capture"]["state"] == "active"
    assert paths.index_health_file().exists()
    loaded = index_health.read_report()
    assert loaded is not None and "stale" not in loaded
    assert loaded["capture"]["captures_ok_total"] == 1


def test_insert_failure_streak_reports_broken_pipeline(ac_root: Path, fresh_counters: dict) -> None:
    _seed_schema()
    index_health.record_capture_ok()
    for _ in range(3):
        index_health.record_index_result(False, "file is not a database")
    report = index_health.build_report(Config())
    assert report["status"] == "degraded"
    assert report["capture"]["state"] == "broken"
    assert "file is not a database" in (report["capture"]["detail"] or "")
    assert any("rebuild-captures-index" in a for a in report["recommended_actions"])
    # One success resets the streak: silence goes back to being intentional.
    index_health.record_index_result(True)
    report = index_health.build_report(Config())
    assert report["capture"]["state"] in {"active", "idle"}


def test_paused_flag_reads_as_intentional_silence(ac_root: Path, fresh_counters: dict) -> None:
    _seed_schema()
    paths.paused_flag().write_text("2026-07-14T00:00:00")
    report = index_health.build_report(Config())
    assert report["capture"]["state"] == "paused"
    assert report["status"] == "ok"


def test_unindexed_buffer_backlog_degrades(ac_root: Path, fresh_counters: dict) -> None:
    _seed_schema()
    for i in range(3):
        (paths.capture_buffer_dir() / f"2026-07-14T00-00-0{i}p00-00.json").write_text("{}")
    cfg = Config()
    cfg.index_health.backlog_warn_threshold = 2
    report = index_health.build_report(cfg)
    assert report["backlog"]["backlog"] == 3
    assert report["status"] == "degraded"
    action = next(a for a in report["recommended_actions"] if "rebuild-captures-index" in a)
    assert "persome stop" in action
    assert "persome start" in action


def test_orphaned_index_row_does_not_hide_unindexed_capture(
    ac_root: Path, fresh_counters: dict
) -> None:
    _seed_schema()
    missing = paths.capture_buffer_dir() / "missing-from-index.json"
    missing.write_text("{}")
    with fts.cursor() as conn:
        conn.execute(
            "INSERT INTO captures (id, timestamp, visible_text) VALUES (?, ?, ?)",
            ("orphaned-index-row", "2026-07-14T00:00:00+00:00", "stale"),
        )
        conn.commit()

    report = index_health.build_report(Config())

    assert report["backlog"] == {
        "buffer_files": 1,
        "indexed_captures": 1,
        "backlog": 1,
        "orphaned_index_rows": 1,
    }


def test_recent_queue_drop_degrades_capture_pipeline(ac_root: Path, fresh_counters: dict) -> None:
    _seed_schema()
    index_health.record_queue_drop()

    report = index_health.build_report(Config())

    assert report["status"] == "degraded"
    assert report["capture"]["state"] == "broken"
    assert report["capture"]["consecutive_queue_drops"] == 1
    assert "queue dropped" in report["capture"]["detail"]
    assert any("queue" in a and "cannot replay" in a for a in report["recommended_actions"])

    index_health.record_queue_accept()
    assert fresh_counters["consecutive_queue_drops"] == 0


def test_corrupt_index_is_reported_not_masked(
    ac_root: Path, fresh_counters: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(db_path=None):  # noqa: ANN001 - matches fts.cursor signature loosely
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(fts, "cursor", _boom)
    report = index_health.build_report(Config())
    assert report["index"]["status"] == "corrupt"
    assert report["status"] == "degraded"
    assert any("quarantine/recover" in a for a in report["recommended_actions"])


def test_stale_sidecar_is_flagged_for_readers(ac_root: Path, fresh_counters: dict) -> None:
    old = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    index_health.write_report(
        {"schema_version": 1, "checked_at": old, "tick_seconds": 300, "status": "ok"}
    )
    loaded = index_health.read_report()
    assert loaded is not None and loaded.get("stale") is True
    # And a stale report yields an explicit "unknown" note, not silence.
    note = index_health.degradation_note()
    assert note is not None and note["status"] == "unknown"


def test_degradation_note_absent_when_healthy(ac_root: Path, fresh_counters: dict) -> None:
    _seed_schema()
    index_health.check_once(Config())
    assert index_health.degradation_note() is None


def test_degradation_note_names_backlog_and_index(ac_root: Path, fresh_counters: dict) -> None:
    now = datetime.now(UTC).isoformat()
    index_health.write_report(
        {
            "schema_version": 1,
            "checked_at": now,
            "tick_seconds": 300,
            "status": "degraded",
            "index": {"status": "corrupt", "problems": ["x"]},
            "capture": {"state": "broken"},
            "backlog": {"backlog": 7},
        }
    )
    note = index_health.degradation_note()
    assert note is not None
    assert note["status"] == "degraded"
    assert note["index_backlog"] == 7
    assert "results may be incomplete" in note["note"]


def test_is_corruption_error_classification() -> None:
    assert index_health.is_corruption_error(
        sqlite3.DatabaseError("database disk image is malformed")
    )
    assert index_health.is_corruption_error(sqlite3.DatabaseError("file is not a database"))
    assert not index_health.is_corruption_error(sqlite3.OperationalError("no such table: x"))
    assert not index_health.is_corruption_error(ValueError("malformed"))


def test_snapshot_outcome_surfaces_in_report(ac_root: Path, fresh_counters: dict) -> None:
    _seed_schema()
    index_health.record_snapshot_outcome(False, "verification failed")
    report = index_health.build_report(Config())
    assert report["status"] == "degraded"
    assert report["snapshot"]["last_ok"] is False
    index_health.record_snapshot_outcome(True)
    report = index_health.build_report(Config())
    assert report["snapshot"]["last_ok"] is True


def test_search_captures_degrades_explicitly_on_corruption(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_schema()

    def _corrupt(conn, **kwargs):  # noqa: ANN001, ANN003
        raise sqlite3.DatabaseError(
            'fts5: corruption found reading blob 824633720987 from table "captures_fts"'
        )

    monkeypatch.setattr(fts, "search_captures", _corrupt)
    with pytest.raises(RuntimeError) as excinfo:
        captures_mod.search_captures(query="anything")
    message = str(excinfo.value)
    assert "evidence layer is degraded" in message
    assert "rebuild-captures-index" in message
    assert "stop the Runtime" in message
    assert "start the Runtime again" in message


def test_search_captures_payload_carries_health_note(ac_root: Path) -> None:
    # The MCP tool layer attaches degradation context in-band; verify the
    # sidecar-driven note is JSON-serializable alongside a result list.
    now = datetime.now(UTC).isoformat()
    index_health.write_report(
        {
            "schema_version": 1,
            "checked_at": now,
            "tick_seconds": 300,
            "status": "degraded",
            "index": {"status": "ok", "problems": []},
            "capture": {"state": "broken"},
            "backlog": {"backlog": 12},
        }
    )
    note = index_health.degradation_note()
    payload = json.dumps({"query": "q", "results": [], "index_health": note})
    assert "12" in payload

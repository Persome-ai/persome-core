"""Agent-Native Persome Phase 2 — read-only projections of the Swift app's ~/.persome/*.json.

Verifies the lenient readers + the settings secret-redaction (spec
docs/superpowers/specs/2026-06-25-agent-native-persome-design.md §5).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from persome import paths
from persome.mcp import appdata


@pytest.fixture
def app_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "persome"
    (root / "logs").mkdir(parents=True)
    monkeypatch.setenv("PERSOME_APP_DATA_DIR", str(root))
    return root


def _write(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


# ── app_data_root resolution ────────────────────────────────────────────────


def test_app_data_root_explicit_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PERSOME_APP_DATA_DIR", str(tmp_path / "x"))
    assert paths.app_data_root() == (tmp_path / "x").resolve()


def test_app_data_root_is_chronicle_parent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("PERSOME_APP_DATA_DIR", raising=False)
    monkeypatch.setenv("PERSOME_ROOT", str(tmp_path / "persome" / "chronicle"))
    # The packaged app points PERSOME_ROOT at <datadir>/chronicle → app data is the parent.
    assert paths.app_data_root() == (tmp_path / "persome" / "chronicle").resolve().parent


# ── tasks ────────────────────────────────────────────────────────────────────


def test_list_tasks_filters_sorts_strips_log(app_root: Path) -> None:
    _write(
        app_root / "tasks.json",
        [
            {
                "id": "a",
                "title": "old",
                "status": "needsReview",
                "agent": "claudeCode",
                "createdAt": "2026-06-20T10:00:00.000Z",
                "log": "SHOULD NOT APPEAR",
            },
            {
                "id": "b",
                "title": "new",
                "status": "queued",
                "agent": "codex",
                "createdAt": "2026-06-24T10:00:00.000Z",
            },
        ],
    )
    rows = appdata.list_tasks()
    assert [r["id"] for r in rows] == ["b", "a"]  # newest first
    assert "log" not in rows[0]  # summary carries no log body
    only = appdata.list_tasks(status="queued")
    assert [r["id"] for r in only] == ["b"]


def test_read_task_merges_log_sidecar(app_root: Path) -> None:
    _write(app_root / "tasks.json", [{"id": "t1", "title": "x", "prompt": "do it", "log": ""}])
    _write(app_root / "logs" / "t1.json", {"log": "FULL LOG BODY", "turns": ["turn-0"]})
    t = appdata.read_task(task_id="t1")
    assert t is not None
    assert t["prompt"] == "do it"
    assert t["log"] == "FULL LOG BODY"
    assert t["turnLogs"] == ["turn-0"]
    assert appdata.read_task(task_id="nope") is None


# ── settings redaction (the load-bearing privacy assertion) ──────────────────


def test_read_settings_redacts_secrets(app_root: Path) -> None:
    _write(
        app_root / "settings.json",
        {
            "deepseekApiKey": "sk-SECRET",
            "doubaoAppKey": "APP-SECRET",
            "doubaoAccessKey": "",  # empty stays empty (distinguishable from withheld)
            "deepseekModel": "deepseek-v4",  # non-secret, preserved
            "autoDispatch": True,
            "nested": {"someToken": "T", "plain": "ok"},
        },
    )
    s = appdata.read_settings()
    assert s["deepseekApiKey"] == "<redacted>"
    assert s["doubaoAppKey"] == "<redacted>"
    assert s["doubaoAccessKey"] == ""  # empty preserved, not "<redacted>"
    assert s["deepseekModel"] == "deepseek-v4"
    assert s["autoDispatch"] is True
    assert s["nested"]["someToken"] == "<redacted>"
    assert s["nested"]["plain"] == "ok"
    # No actual secret value survives anywhere in the serialized output.
    assert "SECRET" not in json.dumps(s)


# ── meetings + feedback ──────────────────────────────────────────────────────


def test_meetings_list_and_read(app_root: Path) -> None:
    _write(
        app_root / "meetings.json",
        [
            {
                "id": "m1",
                "title": "standup",
                "status": "review",
                "startedAt": "2026-06-20T09:00:00.000Z",
                "transcript": "hello team",
            },
            {
                "id": "m2",
                "title": "1:1",
                "status": "failed",
                "startedAt": "2026-06-24T09:00:00.000Z",
            },
        ],
    )
    rows = appdata.list_meetings()
    assert [r["id"] for r in rows] == ["m2", "m1"]
    assert "transcript" not in rows[0]  # list is a summary
    full = appdata.read_meeting(meeting_id="m1")
    assert full is not None and full["transcript"] == "hello team"
    assert appdata.read_meeting(meeting_id="zzz") is None


def test_read_feedback_newest_first_lenient(app_root: Path) -> None:
    (app_root / "logs" / "context-feedback.jsonl").write_text(
        '{"verdict":"auto_queued","taskTitle":"a"}\n'
        "not json — skip me\n"
        "\n"
        '{"verdict":"completed","taskTitle":"b"}\n',
        encoding="utf-8",
    )
    fb = appdata.read_feedback()
    assert [r["verdict"] for r in fb] == ["completed", "auto_queued"]  # newest first, junk skipped


# ── lenience: nothing on disk → empty, never raises ──────────────────────────


def test_missing_files_return_empty(app_root: Path) -> None:
    assert appdata.list_tasks() == []
    assert appdata.read_task(task_id="x") is None
    assert appdata.read_settings() == {}
    assert appdata.list_meetings() == []
    assert appdata.read_feedback() == []


def test_malformed_json_returns_empty(app_root: Path) -> None:
    (app_root / "tasks.json").write_text("{not valid", encoding="utf-8")
    (app_root / "settings.json").write_text("garbage", encoding="utf-8")
    assert appdata.list_tasks() == []
    assert appdata.read_settings() == {}

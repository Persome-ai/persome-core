from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from persome import config as config_mod
from persome import paths
from persome.store import fts
from persome.timeline import store as timeline_store
from persome.writer import llm as llm_mod
from persome.writer import pattern_detector as pd_mod

_TZ = timezone(timedelta(hours=8))


def _tool_call(name: str, args: dict[str, Any], cid: str = "c0") -> Any:
    fn = SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False))
    return SimpleNamespace(id=cid, function=fn)


def _response(tool_calls: list | None = None, text: str = "") -> Any:
    msg = SimpleNamespace(content=text or None, tool_calls=tool_calls or [])
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice])


def _seed_timeline_blocks(conn) -> None:
    """Insert timeline blocks with repeated app sequences."""
    base = datetime(2026, 5, 11, 9, 0, tzinfo=_TZ)
    for i in range(3):
        block = timeline_store.TimelineBlock(
            start_time=base + timedelta(minutes=i),
            end_time=base + timedelta(minutes=i + 1),
            timezone="+08:00",
            entries=["Opened Mail, Slack, Cursor"],
            apps_used=["Mail", "Slack", "Cursor"],
            capture_count=3,
        )
        timeline_store.insert(conn, block)


def _seed_captures(conn) -> None:
    """Insert captures with repeated window titles."""
    base = datetime(2026, 5, 11, 9, 0, tzinfo=_TZ)
    for i in range(3):
        conn.execute(
            """
            INSERT INTO captures (id, timestamp, app_name, window_title, url)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                f"cap_{i}",
                (base + timedelta(minutes=i)).isoformat(),
                "Cursor",
                "project-persome — Cursor",
                "",
            ),
        )


def test_pattern_detector_creates_workflow(ac_root: Path, monkeypatch) -> None:
    with fts.cursor() as conn:
        _seed_timeline_blocks(conn)
        _seed_captures(conn)

    # Scripted LLM: iter 1 → create workflow, iter 2 → commit.
    script = [
        _response(
            [
                _tool_call(
                    "create",
                    {
                        "path": "skills/skill-morning-routine.md",
                        "description": "Morning app launch routine",
                        "tags": ["routine", "morning"],
                    },
                    cid="c1",
                )
            ]
        ),
        _response(
            [
                _tool_call(
                    "append",
                    {
                        "path": "skills/skill-morning-routine.md",
                        "content": (
                            "**Pattern**: Weekday mornings 9:00, user opens Mail → Slack → Cursor.\n"
                            "**Confidence**: high (3 consecutive days)\n"
                            "**Trigger**: weekday, 9:00\n"
                            "**Suggested automation**: macOS Shortcut to launch the three apps"
                        ),
                        "tags": ["pattern", "detected"],
                    },
                    cid="c2",
                )
            ]
        ),
        _response(
            [
                _tool_call(
                    "commit",
                    {"summary": "created morning routine workflow"},
                    cid="c3",
                )
            ]
        ),
    ]

    def fake_call_llm(cfg, stage, *, messages, tools=None, json_mode=False):
        assert stage == "pattern_detector"
        return script.pop(0)

    monkeypatch.setattr(llm_mod, "call_llm", fake_call_llm)

    cfg = config_mod.load(ac_root / "config.toml")
    result = pd_mod.detect_after_classify(
        cfg,
        session_id="sess_pd",
        event_daily_path="event-2026-05-11.md",
        session_start=datetime(2026, 5, 11, 9, 0, tzinfo=_TZ),
        session_end=datetime(2026, 5, 11, 9, 30, tzinfo=_TZ),
    )

    assert result.committed is True
    assert len(result.written_ids) == 1
    assert result.created_paths == ["skills/skill-morning-routine.md"]
    assert "morning routine" in result.summary

    workflow = (paths.memory_dir() / "skills/skill-morning-routine.md").read_text()
    assert "Mail → Slack → Cursor" in workflow


def test_pattern_detector_rejects_event_write(ac_root: Path, monkeypatch) -> None:
    with fts.cursor() as conn:
        _seed_timeline_blocks(conn)

    script = [
        _response(
            [
                _tool_call(
                    "append",
                    {"path": "event-2026-05-11.md", "content": "should be blocked", "tags": ["x"]},
                    cid="c1",
                )
            ]
        ),
        _response(
            [
                _tool_call(
                    "commit",
                    {"summary": ""},
                    cid="c2",
                )
            ]
        ),
    ]

    def fake_call_llm(cfg, stage, *, messages, tools=None, json_mode=False):
        return script.pop(0)

    monkeypatch.setattr(llm_mod, "call_llm", fake_call_llm)

    cfg = config_mod.load(ac_root / "config.toml")
    result = pd_mod.detect_after_classify(
        cfg,
        session_id="sess_reject",
        event_daily_path="event-2026-05-11.md",
        session_start=datetime(2026, 5, 11, 9, 0, tzinfo=_TZ),
        session_end=datetime(2026, 5, 11, 9, 30, tzinfo=_TZ),
    )

    assert result.committed is True
    assert result.written_ids == []


def test_pattern_detector_skips_when_disabled(ac_root: Path) -> None:
    cfg = config_mod.load(ac_root / "config.toml")
    cfg.pattern_detector.enabled = False

    result = pd_mod.detect_after_classify(
        cfg,
        session_id="sess_off",
        event_daily_path="event-2026-05-11.md",
    )

    assert result.committed is False
    assert "disabled" in result.skipped_reason


def test_pattern_detector_skips_no_candidates(ac_root: Path) -> None:
    cfg = config_mod.load(ac_root / "config.toml")
    result = pd_mod.detect_after_classify(
        cfg,
        session_id="sess_empty",
        event_daily_path="event-2026-05-11.md",
        session_start=datetime(2026, 5, 11, 9, 0, tzinfo=_TZ),
        session_end=datetime(2026, 5, 11, 9, 30, tzinfo=_TZ),
    )

    assert result.committed is False
    assert "no pattern candidates" in result.skipped_reason


def test_pattern_detector_raw_mode_creates_workflow(ac_root: Path, monkeypatch) -> None:
    with fts.cursor() as conn:
        _seed_timeline_blocks(conn)
        _seed_captures(conn)

    script = [
        _response(
            [
                _tool_call(
                    "create",
                    {
                        "path": "skills/skill-morning-routine.md",
                        "description": "Morning app launch routine",
                        "tags": ["routine", "morning"],
                    },
                    cid="c1",
                )
            ]
        ),
        _response(
            [
                _tool_call(
                    "commit",
                    {"summary": "detected morning routine from raw data"},
                    cid="c2",
                )
            ]
        ),
    ]

    def fake_call_llm(cfg, stage, *, messages, tools=None, json_mode=False):
        assert stage == "pattern_detector"
        # Verify raw mode: user message (index 1) should contain raw blocks
        assert "Timeline blocks" in messages[1]["content"]
        assert "Captures" in messages[1]["content"]
        return script.pop(0)

    monkeypatch.setattr(llm_mod, "call_llm", fake_call_llm)

    cfg = config_mod.load(ac_root / "config.toml")
    cfg.pattern_detector.structured_filter = False
    result = pd_mod.detect_after_classify(
        cfg,
        session_id="sess_raw",
        event_daily_path="event-2026-05-11.md",
        session_start=datetime(2026, 5, 11, 9, 0, tzinfo=_TZ),
        session_end=datetime(2026, 5, 11, 9, 30, tzinfo=_TZ),
    )

    assert result.committed is True
    assert result.created_paths == ["skills/skill-morning-routine.md"]


def test_collect_candidates_finds_app_sequences(ac_root: Path) -> None:
    with fts.cursor() as conn:
        _seed_timeline_blocks(conn)
        _seed_captures(conn)

        start = datetime(2026, 5, 11, 8, 0, tzinfo=_TZ)
        end = datetime(2026, 5, 11, 10, 0, tzinfo=_TZ)
        candidates = pd_mod._collect_candidates(
            conn,
            lookback_start=start,
            window_end=end,
            min_occurrences=2,
        )

    assert "app_sequences" in candidates
    assert len(candidates["app_sequences"]) == 1
    seq = candidates["app_sequences"][0]
    assert seq["apps"] == ["Cursor", "Mail", "Slack"]
    assert seq["count"] == 3

    assert "repeated_titles" in candidates
    assert len(candidates["repeated_titles"]) == 1
    title = candidates["repeated_titles"][0]
    assert title["value"] == "project-persome — Cursor"
    assert title["count"] == 3

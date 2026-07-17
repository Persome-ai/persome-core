from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from persome import config as config_mod
from persome import paths
from persome.store import entries as entries_store
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


def _write_capture(ts: datetime, text: str, *, app: str) -> None:
    path = paths.capture_buffer_dir() / (
        ts.isoformat().replace(":", "-").replace("+", "p") + ".json"
    )
    path.write_text(
        json.dumps(
            {
                "timestamp": ts.isoformat(),
                "window_meta": {"app_name": app, "title": "cutoff test"},
                "focused_element": {"role": "AXStaticText", "value": text},
                "visible_text": text,
            }
        ),
        encoding="utf-8",
    )


def test_pattern_raw_context_clips_terminal_straddler(
    ac_root: Path,
    fake_llm,
) -> None:
    minute = datetime(2026, 5, 11, 8, 30, tzinfo=_TZ)
    start = minute + timedelta(seconds=10)
    end = minute + timedelta(seconds=30)
    safe = "SAFE_PATTERN_DECISION"
    secret = "POST_CUTOFF_SECRET_PATTERN"
    _write_capture(minute + timedelta(seconds=20), safe, app="SafeApp")
    _write_capture(minute + timedelta(seconds=50), secret, app="SecretApp")
    with fts.cursor() as conn:
        timeline_store.insert(
            conn,
            timeline_store.TimelineBlock(
                start_time=minute,
                end_time=minute + timedelta(minutes=1),
                entries=[f"normalized {safe}; {secret}"],
                apps_used=["SafeApp", "SecretApp"],
                capture_count=2,
                focus_excerpt=secret,
                attention_surface=secret,
            ),
        )
    fake_llm.add_script(
        "pattern_detector",
        [_response([_tool_call("commit", {"summary": "cutoff safe"}, cid="cutoff")])],
    )
    cfg = config_mod.load(ac_root / "config.toml")
    cfg.pattern_detector.structured_filter = False

    result = pd_mod.detect_after_classify(
        cfg,
        session_id="sess-pattern-cutoff",
        event_daily_path="event-2026-05-11.md",
        session_start=start,
        session_end=end,
    )

    assert result.committed and not result.retryable
    sent = json.dumps(fake_llm.calls, ensure_ascii=False, default=str)
    assert safe in sent and secret not in sent


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
                            "stage: observed\n\n"
                            "**Pattern**: Weekday mornings 9:00, user opens Mail → Slack → Cursor.\n"
                            "**Confidence**: high (3 consecutive days)\n"
                            "**Context**: weekday, 9:00\n"
                            "**Evidence**: three independent sessions"
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

    frozen_clock = datetime(2026, 5, 11, 9, 30, tzinfo=_TZ)

    def fake_call_llm(cfg, stage, *, messages, tools=None, json_mode=False):
        assert stage == "pattern_detector"
        # Verify raw mode: user message (index 1) should contain raw blocks
        assert "Timeline blocks" in messages[1]["content"]
        assert "Captures" in messages[1]["content"]
        assert f"# Frozen stage clock\n\n{frozen_clock.isoformat()}" in messages[1]["content"]
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
        stage_clock=frozen_clock,
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
            timeline_blocks=sorted(
                timeline_store.query_range(conn, start, end, limit=200),
                key=lambda block: block.start_time,
            ),
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


def test_collect_candidates_uses_durable_event_memory_not_intents(ac_root: Path) -> None:
    with fts.cursor() as conn:
        entries_store.create_file(
            conn,
            name="event-2026-05-11.md",
            description="Synthetic activity",
            tags=["event"],
        )
        entry_id = entries_store.append_entry(
            conn,
            name="event-2026-05-11.md",
            content="Reviewed the runtime architecture twice this week.",
            tags=["work"],
        )
        candidates = pd_mod._collect_candidates(
            conn,
            lookback_start=datetime.now().astimezone() - timedelta(days=1),
            window_end=datetime.now().astimezone() + timedelta(days=1),
            min_occurrences=2,
            timeline_blocks=[],
        )

    assert "intents" not in candidates
    assert candidates["event_memory"][0]["id"] == entry_id
    assert candidates["event_memory"][0]["receipt"] == (f"⟨{entry_id}:event-2026-05-11.md⟩")


def test_collect_event_memory_uses_occurrence_time_for_historical_replay(
    ac_root: Path,
) -> None:
    occurred_at = datetime(2026, 5, 11, 9, 5, tzinfo=_TZ)
    with fts.cursor() as conn:
        entries_store.create_file(
            conn,
            name="event-2026-05-11.md",
            description="Historical replay activity",
            tags=["event"],
        )
        entry_id = entries_store.append_entry(
            conn,
            name="event-2026-05-11.md",
            content="Made a historical replay decision.",
            tags=["work"],
            occurred_at=occurred_at.isoformat(),
        )
        events = pd_mod._collect_event_memory(
            conn,
            lookback_start=occurred_at - timedelta(minutes=1),
            window_end=occurred_at + timedelta(minutes=1),
        )

    assert [event["id"] for event in events] == [entry_id]
    assert events[0]["timestamp"] == occurred_at.isoformat()


def test_pattern_detector_renders_durable_event_memory(ac_root: Path) -> None:
    now = datetime.now().astimezone()
    with fts.cursor() as conn:
        entries_store.create_file(
            conn,
            name="event-2026-07-10.md",
            description="Synthetic completed activity",
            tags=["event"],
        )
        entry_id = entries_store.append_entry(
            conn,
            name="event-2026-07-10.md",
            content="Reviewed the Persome runtime architecture.",
            tags=["work"],
        )
        candidates = pd_mod._collect_candidates(
            conn,
            lookback_start=now - timedelta(days=7),
            window_end=now + timedelta(minutes=1),
            min_occurrences=2,
            timeline_blocks=[],
        )
        assert "event_memory" in candidates
        ctx = pd_mod._assemble_context(
            candidates=candidates, event_daily_path="event-x.md", session_id="s1"
        )
        assert "Durable event memory" in ctx
        assert "Reviewed the Persome runtime architecture." in ctx
        assert f"⟨{entry_id}:event-2026-07-10.md⟩" in ctx

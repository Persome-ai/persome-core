from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from persome import config as config_mod
from persome import paths
from persome.store import entries as entries_mod
from persome.store import fts
from persome.timeline import store as timeline_store
from persome.writer import classifier as classifier_mod

_TZ = timezone(timedelta(hours=8))


def test_naive_entry_time_keeps_local_instant_against_utc_session() -> None:
    local = datetime.now().astimezone().replace(second=0, microsecond=0)
    naive_entry = local.replace(tzinfo=None)
    utc_session = local.astimezone(UTC)

    aligned = classifier_mod._align_tz(naive_entry, utc_session)

    assert aligned.tzinfo is not None
    assert aligned.timestamp() == utc_session.timestamp()


def _tool_call(name: str, args: dict[str, Any], cid: str = "c0") -> Any:
    fn = SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False))
    return SimpleNamespace(id=cid, function=fn)


def _response(tool_calls: list | None = None, text: str = "") -> Any:
    msg = SimpleNamespace(content=text or None, tool_calls=tool_calls or [])
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice])


def _seed_event_daily(day: str) -> tuple[str, str]:
    """Create event-YYYY-MM-DD.md with one entry; return (filename, entry_id)."""
    name = f"event-{day}.md"
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn,
            name=name,
            description=f"Session log for {day}",
            tags=["event", "session", "daily"],
        )
        entry_id = entries_mod.append_entry(
            conn,
            name=name,
            content=(
                "**Session sess_abc** (10:00–10:45)\n\n"
                "The user spent 45 minutes in Cursor configuring a new "
                'Python project and said in a note: "I prefer Cursor over '
                'VSCode now because the AI tab-complete is better."\n\n'
                "- [10:00-10:45, Cursor] edited project-root files, involving —\n"
            ),
            tags=["session", "sid:sess_abc"],
        )
    return name, entry_id


def _write_capture(ts: datetime, text: str) -> None:
    path = paths.capture_buffer_dir() / (
        ts.isoformat().replace(":", "-").replace("+", "p") + ".json"
    )
    path.write_text(
        json.dumps(
            {
                "timestamp": ts.isoformat(),
                "window_meta": {"app_name": "Editor", "title": "cutoff test"},
                "focused_element": {"role": "AXStaticText", "value": text},
                "visible_text": text,
            }
        ),
        encoding="utf-8",
    )


def test_classifier_appends_durable_preference(ac_root: Path, fake_llm) -> None:
    day = "2026-04-21"
    name, entry_id = _seed_event_daily(day)

    # Scripted LLM: iter 1 → search, iter 2 → append, iter 3 → commit.
    fake_llm.add_script(
        "classifier",
        [
            _response(
                [
                    _tool_call(
                        "search_memory",
                        {"query": "Cursor over VSCode"},
                        cid="c1",
                    )
                ]
            ),
            _response(
                [
                    _tool_call(
                        "append",
                        {
                            "path": "user-preferences.md",
                            "content": "User prefers Cursor over VSCode because of its AI tab-complete.",
                            "tags": ["editor", "preference"],
                        },
                        cid="c2",
                    )
                ]
            ),
            _response(
                [
                    _tool_call(
                        "commit",
                        {"summary": "recorded Cursor-over-VSCode preference"},
                        cid="c3",
                    )
                ]
            ),
        ],
    )

    cfg = config_mod.load(ac_root / "config.toml")
    cfg.memory_delta.apply_enabled = False
    result = classifier_mod.classify_after_reduce(
        cfg,
        session_id="sess_abc",
        event_daily_path=name,
        just_written_entry_id=entry_id,
    )

    assert result.committed is True
    assert len(result.written_ids) == 1
    assert "Cursor-over-VSCode" in result.summary

    # Event-daily was NOT modified.
    evt = (paths.memory_dir() / name).read_text()
    assert evt.count("**Session sess_abc**") == 1

    # user-preferences.md got the new entry.
    pref = (paths.memory_dir() / "user-preferences.md").read_text()
    assert "Cursor over VSCode" in pref


def test_terminal_classifier_uses_session_end_for_timeline_evidence(
    ac_root: Path,
    fake_llm,
) -> None:
    minute = datetime(2026, 4, 21, 11, 0, tzinfo=_TZ)
    start = minute + timedelta(seconds=10)
    end = minute + timedelta(seconds=30)
    safe = "SAFE_CLASSIFIER_DECISION"
    secret = "POST_CUTOFF_SECRET_CLASSIFIER"
    _write_capture(minute + timedelta(seconds=20), safe)
    _write_capture(minute + timedelta(seconds=50), secret)
    with fts.cursor() as conn:
        timeline_store.insert(
            conn,
            timeline_store.TimelineBlock(
                start_time=minute,
                end_time=minute + timedelta(minutes=1),
                entries=[f"[Editor] normalized {safe}; {secret}"],
                apps_used=["Editor"],
                capture_count=2,
                focus_excerpt=secret,
            ),
        )
        entries_mod.create_file(
            conn,
            name="event-2026-04-21.md",
            description="Cutoff-safe classifier fixture",
            tags=["event", "session"],
        )
        entry_id = entries_mod.append_entry(
            conn,
            name="event-2026-04-21.md",
            content=f"**Session sess-classifier-cutoff**\n\n{safe}",
            tags=["session", "sid:sess-classifier-cutoff"],
        )
    fake_llm.add_script(
        "classifier",
        [_response([_tool_call("commit", {"summary": "cutoff safe"}, cid="c1")])],
    )
    cfg = config_mod.load(ac_root / "config.toml")
    cfg.memory_delta.apply_enabled = False

    result = classifier_mod.classify_after_reduce(
        cfg,
        session_id="sess-classifier-cutoff",
        event_daily_path="event-2026-04-21.md",
        just_written_entry_id=entry_id,
        session_start=start,
        session_end=end,
    )

    assert result.committed and not result.retryable
    sent = json.dumps(fake_llm.calls, ensure_ascii=False, default=str)
    assert safe in sent and secret not in sent


def test_classifier_drill_is_bounded_to_frozen_half_open_session_evidence(
    ac_root: Path,
    fake_llm,
) -> None:
    start = datetime(2026, 4, 21, 12, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=1)
    safe = "SAFE_CHAT_INSIDE_SESSION"
    before = "SECRET_CHAT_BEFORE_SESSION"
    at_end = "SECRET_CHAT_AT_EXCLUSIVE_END"
    with fts.cursor() as conn:
        timeline_store.insert(
            conn,
            timeline_store.TimelineBlock(
                start_time=start,
                end_time=end,
                entries=["[Chat] reviewed the bounded conversation"],
                apps_used=["Chat"],
                capture_count=1,
            ),
        )
        entries_mod.create_file(
            conn,
            name="event-2026-04-21.md",
            description="Bounded classifier drill fixture",
            tags=["event", "session"],
        )
        entry_id = entries_mod.append_entry(
            conn,
            name="event-2026-04-21.md",
            content="**Session sess-bounded-drill**\n\nReviewed a chat.",
            tags=["session", "sid:sess-bounded-drill"],
            occurred_at=start.isoformat(),
        )
        for capture_id, moment, text in (
            ("chat-before", start - timedelta(seconds=1), before),
            ("chat-inside", start + timedelta(seconds=20), safe),
            ("chat-end", end, at_end),
        ):
            fts.insert_capture(
                conn,
                id=capture_id,
                timestamp=moment.isoformat(),
                app_name="Chat",
                bundle_id="com.example.chat",
                window_title="Conversation",
                focused_role="AXStaticText",
                focused_value=text,
                visible_text=text,
                url="",
            )

    fake_llm.add_script(
        "classifier",
        [
            _response(
                [
                    _tool_call(
                        "drill_chat_captures",
                        {
                            "app_name": "Chat",
                            "start_ts": (start - timedelta(hours=1)).isoformat(),
                            "end_ts": (end + timedelta(hours=1)).isoformat(),
                        },
                        cid="drill",
                    )
                ]
            ),
            _response([_tool_call("commit", {"summary": "bounded"}, cid="commit")]),
        ],
    )
    cfg = config_mod.load(ac_root / "config.toml")
    cfg.memory_delta.apply_enabled = False

    result = classifier_mod.classify_after_reduce(
        cfg,
        session_id="sess-bounded-drill",
        event_daily_path="event-2026-04-21.md",
        just_written_entry_id=entry_id,
        session_start=start,
        session_end=end,
        processing_clock=end + timedelta(days=10),
    )

    assert result.committed
    sent = json.dumps(fake_llm.calls, ensure_ascii=False, default=str)
    assert safe in sent
    assert before not in sent
    assert at_end not in sent
    tool_messages = [
        message
        for message in fake_llm.calls[-1]["messages"]
        if message.get("role") == "tool" and message.get("name") == "drill_chat_captures"
    ]
    assert tool_messages
    assert json.loads(tool_messages[-1]["content"])["bounds_clipped"] is True
    assert "# Current date/time" in sent
    assert "2026-04-21 12:01 Tuesday (+0800)" in sent


def test_terminal_classifier_guard_does_not_reopen_completed_window(
    ac_root: Path,
    fake_llm,
) -> None:
    start = datetime(2026, 4, 21, 13, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=1)
    cfg = config_mod.load(ac_root / "config.toml")
    cfg.memory_delta.apply_enabled = False

    result = classifier_mod.classify_after_reduce(
        cfg,
        session_id="sess-already-classified",
        event_daily_path="event-2026-04-21.md",
        session_start=start,
        session_end=end,
        window_start=end,
        stage_clock=end + timedelta(days=10),
    )

    assert not result.committed
    assert result.skipped_reason == "terminal window empty (already classified)"
    assert fake_llm.calls == []


def test_classifier_rejects_event_write(ac_root: Path, fake_llm) -> None:
    day = "2026-04-22"
    name, entry_id = _seed_event_daily(day)

    # LLM tries to write back to event-* — must be rejected without committing.
    fake_llm.add_script(
        "classifier",
        [
            _response(
                [
                    _tool_call(
                        "append",
                        {"path": name, "content": "should be blocked", "tags": ["x"]},
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
        ],
    )

    cfg = config_mod.load(ac_root / "config.toml")
    cfg.memory_delta.apply_enabled = False
    result = classifier_mod.classify_after_reduce(
        cfg,
        session_id="sess_reject",
        event_daily_path=name,
        just_written_entry_id=entry_id,
    )

    # Committed=True (LLM called commit), but zero writes landed.
    assert result.committed is True
    assert result.written_ids == []


def test_classifier_empty_commit_when_nothing_classifiable(
    ac_root: Path,
    fake_llm,
) -> None:
    day = "2026-04-23"
    name, entry_id = _seed_event_daily(day)

    fake_llm.add_script(
        "classifier",
        [
            _response([_tool_call("commit", {"summary": ""}, cid="c1")]),
        ],
    )

    cfg = config_mod.load(ac_root / "config.toml")
    cfg.memory_delta.apply_enabled = False
    result = classifier_mod.classify_after_reduce(
        cfg,
        session_id="sess_noop",
        event_daily_path=name,
        just_written_entry_id=entry_id,
    )

    assert result.committed is True
    assert result.written_ids == []
    assert result.iterations == 1


def test_classifier_skips_when_event_daily_missing(ac_root: Path) -> None:
    cfg = config_mod.load(ac_root / "config.toml")
    cfg.memory_delta.apply_enabled = False
    result = classifier_mod.classify_after_reduce(
        cfg,
        session_id="sess_no_file",
        event_daily_path="event-9999-99-99.md",
        just_written_entry_id="fake",
    )
    assert result.committed is False
    assert "no entries" in result.skipped_reason

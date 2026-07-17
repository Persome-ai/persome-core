from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from persome import config as config_mod
from persome import paths
from persome.session import store as session_store
from persome.store import fts
from persome.timeline import store as timeline_store
from persome.writer import session_reducer

_TZ = timezone(timedelta(hours=8))
_SID = "sess_test0000"


def test_reducer_prompt_describes_current_terminal_modeling_stage() -> None:
    prompt = session_reducer.load_prompt("session_reduce.system.md")
    assert "terminal modeling stage" in prompt
    assert "downstream classifier" not in prompt


def test_attach_drill_down_breadcrumb_unit() -> None:
    """Direct unit test of the breadcrumb post-processor."""
    f = session_reducer._attach_drill_down_breadcrumb
    assert f("[14:30-14:35, Cursor] edited main.py").endswith(
        'raw: read_recent_capture(at="14:30", app_name="Cursor")'
    )
    # Spaces in app name preserved verbatim.
    out = f("[09:00-09:05, Code - Insiders] reviewed config.toml")
    assert 'app_name="Code - Insiders"' in out
    # En-dash separator (LLMs sometimes emit it) still parses.
    out2 = f("[18:00–18:30, Google Chrome] reading docs")
    assert 'at="18:00"' in out2 and 'app_name="Google Chrome"' in out2
    # Lines without the canonical prefix pass through untouched.
    plain = "no prefix at all, just text"
    assert f(plain) == plain
    # Idempotent — already-breadcrumbed lines aren't double-appended.
    crumbed = f("[14:30-14:35, Cursor] edited")
    assert f(crumbed) == crumbed


def _seed_blocks(start: datetime) -> list[timeline_store.TimelineBlock]:
    """Create 3 contiguous 5-min blocks with one entry each."""
    bs: list[timeline_store.TimelineBlock] = []
    with fts.cursor() as conn:
        for i in range(3):
            b = timeline_store.TimelineBlock(
                start_time=start + timedelta(minutes=5 * i),
                end_time=start + timedelta(minutes=5 * (i + 1)),
                timezone="+08:00",
                entries=[f"[Cursor] edited file_{i}.py, involving nothing"],
                apps_used=["Cursor"],
                capture_count=6,
            )
            timeline_store.insert(conn, b)
            bs.append(b)
    return bs


def _write_capture(ts: datetime, text: str) -> None:
    path = paths.capture_buffer_dir() / (
        ts.isoformat().replace(":", "-").replace("+", "p") + ".json"
    )
    path.write_text(
        json.dumps(
            {
                "timestamp": ts.isoformat(),
                "window_meta": {
                    "app_name": "Editor",
                    "title": "cutoff test",
                    "bundle_id": "test.editor",
                },
                "focused_element": {"role": "AXStaticText", "value": text},
                "visible_text": text,
            }
        ),
        encoding="utf-8",
    )


def test_reducer_happy_path_writes_event_daily(ac_root: Path, fake_llm) -> None:
    start = datetime(2026, 4, 21, 10, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=15)
    _seed_blocks(start)

    with fts.cursor() as conn:
        session_store.insert(
            conn,
            session_store.SessionRow(id=_SID, start_time=start, end_time=end, status="ended"),
        )

    fake_llm.set_default(
        "reducer",
        json.dumps(
            {
                "summary": "Worked on a few Python files in Cursor.",
                "sub_tasks": [
                    "[10:00-10:15, Cursor] edited three files, involving file_0.py, file_1.py, file_2.py",
                ],
            }
        ),
    )

    cfg = config_mod.load(ac_root / "config.toml")
    result = session_reducer.reduce_session(
        cfg,
        session_id=_SID,
        start_time=start,
        end_time=end,
    )

    assert result.succeeded is True
    assert result.written is True
    assert result.entry_id
    assert result.path == "event-2026-04-21.md"
    assert len(result.sub_tasks) == 1

    md = (paths.memory_dir() / "event-2026-04-21.md").read_text()
    assert "Session sess_test0000" in md
    assert "[10:00-10:15, Cursor]" in md
    assert "file_0.py" in md
    # Drill-down breadcrumb appended by _attach_drill_down_breadcrumb.
    assert 'raw: read_recent_capture(at="10:00", app_name="Cursor")' in md
    # The result list also carries it.
    assert any('read_recent_capture(at="10:00"' in s for s in result.sub_tasks)

    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, _SID)
        metadata = conn.execute(
            "SELECT occurred_at FROM entry_metadata WHERE entry_id=?",
            (result.entry_id,),
        ).fetchone()
    assert row is not None
    assert row.status == "reduced"
    assert metadata is not None and metadata["occurred_at"] == start.isoformat()


def test_reducer_clips_straddling_block_before_prompt_and_event_memory(
    ac_root: Path,
    fake_llm,
) -> None:
    minute = datetime(2026, 4, 21, 10, 30, tzinfo=_TZ)
    start = minute + timedelta(seconds=10)
    end = minute + timedelta(seconds=30)
    safe = "SAFE_REDUCER_DECISION"
    secret = "POST_CUTOFF_SECRET_REDUCER"
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
                attention_surface=secret,
            ),
        )
        session_store.insert(
            conn,
            session_store.SessionRow(
                id="sess-reducer-cutoff",
                start_time=start,
                end_time=end,
                status="ended",
            ),
        )
    fake_llm.set_default(
        "reducer",
        json.dumps(
            {
                "summary": safe,
                "sub_tasks": [f"[10:30-10:30, Editor] {safe}, involving —"],
            }
        ),
    )

    result = session_reducer.reduce_session(
        config_mod.load(ac_root / "config.toml"),
        session_id="sess-reducer-cutoff",
        start_time=start,
        end_time=end,
    )

    assert result.written
    prompt = str(fake_llm.calls[0]["messages"][1]["content"])
    assert safe in prompt and secret not in prompt
    event_text = (paths.memory_dir() / "event-2026-04-21.md").read_text()
    assert safe in event_text and secret not in event_text


def test_reducer_prompt_does_not_replay_earlier_same_day_tasks(ac_root: Path, fake_llm) -> None:
    day = datetime(2026, 4, 21, 9, 0, tzinfo=_TZ)
    with fts.cursor() as conn:
        session_reducer._append_event_entry(
            conn,
            session_id="sess_old_task",
            start_time=day,
            end_time=day + timedelta(minutes=30),
            summary="Prepared the launch campaign in Feishu.",
            sub_tasks=["[09:00-09:30, Feishu] drafted launch copy, involving campaign"],
            heuristic=False,
            is_final=True,
        )

    current = day + timedelta(hours=5)
    blocks = [
        timeline_store.TimelineBlock(
            start_time=current,
            end_time=current + timedelta(minutes=5),
            timezone="+08:00",
            entries=["[Cursor] fixed an MCP startup test, involving server.py"],
            apps_used=["Cursor"],
            capture_count=3,
        )
    ]
    fake_llm.set_default(
        "reducer",
        json.dumps(
            {
                "summary": "Fixed an MCP startup test.",
                "sub_tasks": ["[14:00-14:05, Cursor] fixed test, involving server.py"],
            }
        ),
    )

    cfg = config_mod.load(ac_root / "config.toml")
    payload = session_reducer._call_reducer_llm(
        cfg,
        blocks,
        current,
        current + timedelta(minutes=5),
    )

    assert payload is not None
    user_content = fake_llm.calls[-1]["messages"][1]["content"]
    rendered = "\n".join(block["text"] for block in user_content)
    assert "fixed an MCP startup test" in rendered
    assert "Prepared the launch campaign" not in rendered
    assert "drafted launch copy" not in rendered


def test_reducer_no_blocks_marks_reduced_no_write(ac_root: Path) -> None:
    start = datetime(2026, 4, 21, 11, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=5)

    with fts.cursor() as conn:
        session_store.insert(
            conn,
            session_store.SessionRow(
                id="sess_empty", start_time=start, end_time=end, status="ended"
            ),
        )

    cfg = config_mod.load(ac_root / "config.toml")
    result = session_reducer.reduce_session(
        cfg,
        session_id="sess_empty",
        start_time=start,
        end_time=end,
    )

    assert result.succeeded is True
    assert result.written is False
    assert not (paths.memory_dir() / "event-2026-04-21.md").exists()

    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, "sess_empty")
    assert row is not None
    assert row.status == "reduced"


@pytest.mark.parametrize("missing_index", [0, 1], ids=["first-gap", "middle-gap"])
def test_reducer_never_advances_across_occupied_missing_timeline_block(
    ac_root: Path,
    fake_llm,
    missing_index: int,
) -> None:
    start = datetime(2026, 4, 21, 11, 30, tzinfo=_TZ)
    end = start + timedelta(minutes=3)
    with fts.cursor() as conn:
        for index in range(3):
            capture_at = start + timedelta(minutes=index, seconds=10)
            conn.execute(
                "INSERT INTO captures (id, timestamp, app_name, window_title, url)"
                " VALUES (?, ?, 'Editor', 'gap test', '')",
                (f"gap-capture-{missing_index}-{index}", capture_at.isoformat()),
            )
            if index == missing_index:
                continue
            timeline_store.insert(
                conn,
                timeline_store.TimelineBlock(
                    start_time=start + timedelta(minutes=index),
                    end_time=start + timedelta(minutes=index + 1),
                    entries=[f"[Editor] minute {index}"],
                    apps_used=["Editor"],
                    capture_count=1,
                ),
            )
        session_store.insert(
            conn,
            session_store.SessionRow(
                id=f"sess-gap-{missing_index}",
                start_time=start,
                end_time=end,
                status="ended",
            ),
        )

    result = session_reducer.reduce_session(
        config_mod.load(ac_root / "config.toml"),
        session_id=f"sess-gap-{missing_index}",
        start_time=start,
        end_time=end,
    )

    assert not result.written and result.skipped_reason == "awaiting_timeline_block"
    assert fake_llm.calls == []
    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, f"sess-gap-{missing_index}")
    assert row is not None
    assert row.status == "failed" and row.retry_count == 0
    assert row.last_error == "awaiting_timeline_block" and row.flush_end is None


def test_reducer_llm_failure_schedules_retry(ac_root: Path, fake_llm) -> None:
    start = datetime(2026, 4, 21, 12, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=15)
    _seed_blocks(start)

    with fts.cursor() as conn:
        session_store.insert(
            conn,
            session_store.SessionRow(
                id="sess_failing", start_time=start, end_time=end, status="ended"
            ),
        )

    # Non-JSON output → json.JSONDecodeError in _call_reducer_llm → None → retry.
    fake_llm.set_default("reducer", "not json at all")
    frozen_clock = datetime(2026, 4, 22, 8, 0, tzinfo=_TZ)

    cfg = config_mod.load(ac_root / "config.toml")
    result = session_reducer.reduce_session(
        cfg,
        session_id="sess_failing",
        start_time=start,
        end_time=end,
        stage_clock=frozen_clock,
    )

    assert result.succeeded is False
    assert result.written is False

    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, "sess_failing")
    assert row is not None
    assert row.status == "failed"
    assert row.retry_count == 1
    assert row.next_retry_at == frozen_clock + timedelta(minutes=5)


def test_reducer_exhausted_retries_writes_heuristic(ac_root: Path, fake_llm) -> None:
    start = datetime(2026, 4, 21, 13, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=15)
    _seed_blocks(start)

    # Row begins with retry_count=4, meaning this is attempt 5/5.
    with fts.cursor() as conn:
        session_store.insert(
            conn,
            session_store.SessionRow(
                id="sess_last_chance",
                start_time=start,
                end_time=end,
                status="failed",
                retry_count=4,
            ),
        )

    fake_llm.set_default("reducer", "still garbage")

    cfg = config_mod.load(ac_root / "config.toml")
    result = session_reducer.reduce_session(
        cfg,
        session_id="sess_last_chance",
        start_time=start,
        end_time=end,
    )

    assert result.succeeded is False
    assert result.written is True
    md = (paths.memory_dir() / "event-2026-04-21.md").read_text()
    assert "Cursor" in md
    assert "heuristic" in md  # tag should be present on the heading

    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, "sess_last_chance")
    assert row is not None
    assert row.status == "reduced"


def test_reducer_idempotent_on_already_reduced(ac_root: Path) -> None:
    start = datetime(2026, 4, 21, 14, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=15)
    _seed_blocks(start)

    with fts.cursor() as conn:
        session_store.insert(
            conn,
            session_store.SessionRow(
                id="sess_done",
                start_time=start,
                end_time=end,
                status="reduced",
            ),
        )

    cfg = config_mod.load(ac_root / "config.toml")
    result = session_reducer.reduce_session(
        cfg,
        session_id="sess_done",
        start_time=start,
        end_time=end,
    )
    assert result.succeeded is True
    assert result.written is False
    assert not (paths.memory_dir() / "event-2026-04-21.md").exists()


def test_flush_active_session_writes_partial_entry(
    ac_root: Path,
    fake_llm,
) -> None:
    start = datetime(2026, 4, 21, 16, 0, tzinfo=_TZ)
    _seed_blocks(start)  # 3 blocks covering 16:00-16:15
    now = start + timedelta(minutes=15)

    with fts.cursor() as conn:
        session_store.insert(
            conn,
            session_store.SessionRow(
                id="sess_flush1",
                start_time=start,
                status="active",
            ),
        )

    fake_llm.set_default(
        "reducer",
        json.dumps(
            {
                "summary": "partial",
                "sub_tasks": [
                    "[16:00-16:15, Cursor] wip, involving files",
                ],
            }
        ),
    )

    cfg = config_mod.load(ac_root / "config.toml")
    result = session_reducer.flush_active_session(
        cfg,
        session_id="sess_flush1",
        session_start=start,
        now=now,
    )
    assert result is not None
    assert result.is_final is False
    assert result.written is True

    md = (paths.memory_dir() / "event-2026-04-21.md").read_text()
    assert "Session sess_flush1 [flush]" in md

    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, "sess_flush1")
    assert row is not None
    # Still active — flush must not mark reduced.
    assert row.status == "active"
    assert row.flush_end is not None
    assert row.flush_end >= now


def test_terminal_reduce_after_flush_covers_trailing_window(
    ac_root: Path,
    fake_llm,
) -> None:
    start = datetime(2026, 4, 21, 17, 0, tzinfo=_TZ)
    # Two blocks: 17:00-17:05 (flushed) and 17:05-17:10 (trailing).
    with fts.cursor() as conn:
        for i in range(2):
            timeline_store.insert(
                conn,
                timeline_store.TimelineBlock(
                    start_time=start + timedelta(minutes=5 * i),
                    end_time=start + timedelta(minutes=5 * (i + 1)),
                    timezone="+08:00",
                    entries=[f"[Cursor] step_{i}, involving nothing"],
                    apps_used=["Cursor"],
                    capture_count=3,
                ),
            )
        # Pretend a flush already consumed the first block.
        session_store.insert(
            conn,
            session_store.SessionRow(
                id="sess_flush2",
                start_time=start,
                status="active",
            ),
        )
        session_store.set_flush_end(
            conn,
            "sess_flush2",
            start + timedelta(minutes=5),
        )

    end = start + timedelta(minutes=10)
    fake_llm.set_default(
        "reducer",
        json.dumps(
            {
                "summary": "tail",
                "sub_tasks": [
                    "[17:05-17:10, Cursor] final slice, involving step_1",
                ],
            }
        ),
    )

    cfg = config_mod.load(ac_root / "config.toml")
    result = session_reducer.reduce_session(
        cfg,
        session_id="sess_flush2",
        start_time=start,
        end_time=end,
    )
    assert result.written is True
    assert result.is_final is True

    md = (paths.memory_dir() / "event-2026-04-21.md").read_text()
    # The terminal entry is NOT tagged as flush.
    assert "Session sess_flush2 [flush]" not in md
    assert "Session sess_flush2" in md
    # Trailing window header shows 17:05, not 17:00 — flush_end trimmed the start.
    assert "17:05" in md

    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, "sess_flush2")
    assert row is not None
    assert row.status == "reduced"


def test_retry_due_picks_up_failed_rows(ac_root: Path, fake_llm) -> None:
    start = datetime(2026, 4, 21, 15, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=15)
    _seed_blocks(start)

    # Already-due failed row: next_retry_at in the past.
    past = datetime.now().astimezone() - timedelta(minutes=1)
    with fts.cursor() as conn:
        session_store.insert(
            conn,
            session_store.SessionRow(
                id="sess_retry",
                start_time=start,
                end_time=end,
                status="failed",
                retry_count=1,
                next_retry_at=past,
            ),
        )

    fake_llm.set_default(
        "reducer",
        json.dumps(
            {"summary": "recovered", "sub_tasks": ["[15:00-15:15, Cursor] ok, involving —"]}
        ),
    )

    cfg = config_mod.load(ac_root / "config.toml")
    results = session_reducer.retry_due(cfg)
    assert len(results) == 1
    assert results[0].succeeded is True

    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, "sess_retry")
    assert row is not None
    assert row.status == "reduced"

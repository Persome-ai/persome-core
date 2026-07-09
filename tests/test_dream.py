"""Unit tests for Dream stage structured analysis and LLM loop."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from persome import config as config_mod
from persome import paths
from persome.store import fts
from persome.timeline import store as timeline_store
from persome.writer import dream as dream_mod
from persome.writer import tools as tools_mod

_TZ = timezone(timedelta(hours=8))


# ─── helpers ────────────────────────────────────────────────────────────────


def _make_cfg() -> config_mod.Config:
    return config_mod.Config(
        dream=config_mod.DreamConfig(
            enabled=True,
            lookback_days=7,
            min_consecutive_days=3,
            min_daily_hours=3.0,
            min_sequence_occurrences=2,
            enable_chat_mining=True,
        ),
        writer=config_mod.WriterConfig(max_tool_iterations=3),
    )


def _tool_call(name: str, args: dict[str, Any], cid: str = "c0") -> Any:
    fn = SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False))
    return SimpleNamespace(id=cid, function=fn)


def _response(tool_calls: list | None = None, text: str = "") -> Any:
    msg = SimpleNamespace(content=text or None, tool_calls=tool_calls or [])
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice])


def _insert_capture(
    conn,
    *,
    ts: datetime,
    app: str = "Cursor",
    title: str = "",
    url: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO captures (id, timestamp, app_name, window_title, url)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            f"cap_{ts.isoformat()}",
            ts.isoformat(),
            app,
            title,
            url,
        ),
    )


# ─── _daily_app_stats ───────────────────────────────────────────────────────


def test_daily_app_stats_counts_minutes(ac_root: Path) -> None:
    base = datetime(2026, 5, 15, 9, 0, tzinfo=_TZ)
    with fts.cursor() as conn:
        for i in range(5):
            block = timeline_store.TimelineBlock(
                start_time=base + timedelta(minutes=i),
                end_time=base + timedelta(minutes=i + 1),
                timezone="+08:00",
                entries=["work"],
                apps_used=["Cursor", "Slack"],
                capture_count=2,
            )
            timeline_store.insert(conn, block)

        stats = dream_mod._daily_app_stats(conn, base - timedelta(days=1))

    day = base.strftime("%Y-%m-%d")
    assert day in stats
    # 5 blocks × capture_count 2 = 10 weighted minutes each app
    assert stats[day]["Cursor"] == 10.0
    assert stats[day]["Slack"] == 10.0


def test_daily_app_stats_empty_when_no_blocks(ac_root: Path) -> None:
    with fts.cursor() as conn:
        stats = dream_mod._daily_app_stats(conn, datetime.now(_TZ) - timedelta(days=1))
    assert stats == {}


# ─── _mine_app_sequences ────────────────────────────────────────────────────


def test_mine_app_sequences_finds_repeated_pattern(ac_root: Path) -> None:
    base = datetime(2026, 5, 15, 9, 0, tzinfo=_TZ)
    with fts.cursor() as conn:
        # Sequence: Cursor → Slack → Mail, repeated twice
        apps = ["Cursor", "Slack", "Mail", "Cursor", "Slack", "Mail"]
        for i, app in enumerate(apps):
            _insert_capture(conn, ts=base + timedelta(seconds=i * 10), app=app)

        seqs = dream_mod._mine_app_sequences(conn, base - timedelta(days=1), min_occurrences=2)

    assert len(seqs) >= 1
    # The full 3-app sequence should be found
    full_seq = next((s for s in seqs if s["sequence"] == ["Cursor", "Slack", "Mail"]), None)
    assert full_seq is not None
    assert full_seq["count"] == 2


def test_mine_app_sequences_collapses_consecutive_duplicates(ac_root: Path) -> None:
    base = datetime(2026, 5, 15, 9, 0, tzinfo=_TZ)
    with fts.cursor() as conn:
        # Cursor, Cursor, Cursor, Slack, Slack → should collapse to Cursor → Slack
        apps = ["Cursor", "Cursor", "Cursor", "Slack", "Slack"]
        for i, app in enumerate(apps):
            _insert_capture(conn, ts=base + timedelta(seconds=i * 5), app=app)

        seqs = dream_mod._mine_app_sequences(conn, base - timedelta(days=1), min_occurrences=1)

    # Should not report Cursor→Cursor
    for s in seqs:
        assert s["sequence"] != ["Cursor", "Cursor"]
    # Cursor → Slack should appear
    found = any(s["sequence"] == ["Cursor", "Slack"] for s in seqs)
    assert found


def test_mine_app_sequences_empty_when_no_captures(ac_root: Path) -> None:
    with fts.cursor() as conn:
        seqs = dream_mod._mine_app_sequences(
            conn, datetime.now(_TZ) - timedelta(days=1), min_occurrences=2
        )
    assert seqs == []


def test_mine_app_sequences_exact_occurrences_boundary(ac_root: Path) -> None:
    base = datetime(2026, 5, 15, 9, 0, tzinfo=_TZ)
    with fts.cursor() as conn:
        # Cursor -> Slack -> Mail, repeated exactly 3 times
        apps = ["Cursor", "Slack", "Mail"] * 3
        for i, app in enumerate(apps):
            _insert_capture(conn, ts=base + timedelta(seconds=i * 10), app=app)

        seqs = dream_mod._mine_app_sequences(conn, base - timedelta(days=1), min_occurrences=3)

    full_seq = next((s for s in seqs if s["sequence"] == ["Cursor", "Slack", "Mail"]), None)
    assert full_seq is not None
    assert full_seq["count"] == 3


def test_mine_app_sequences_respects_max_length(ac_root: Path) -> None:
    base = datetime(2026, 5, 15, 9, 0, tzinfo=_TZ)
    with fts.cursor() as conn:
        # A->B->C->D->E->F (length 6), repeated twice
        apps = ["A", "B", "C", "D", "E", "F"] * 2
        for i, app in enumerate(apps):
            _insert_capture(conn, ts=base + timedelta(seconds=i * 5), app=app)

        seqs = dream_mod._mine_app_sequences(
            conn, base - timedelta(days=1), min_occurrences=2, max_length=3
        )

    # Full length-6 sequence should NOT appear
    long_seq = next((s for s in seqs if len(s["sequence"]) == 6), None)
    assert long_seq is None
    # But length-3 subsequences should appear
    abc = next((s for s in seqs if s["sequence"] == ["A", "B", "C"]), None)
    assert abc is not None
    assert abc["count"] == 2


# ─── _detect_routines ───────────────────────────────────────────────────────


def test_detect_routines_clusters_by_time_slot(ac_root: Path) -> None:
    with fts.cursor() as conn:
        # Morning blocks (08:00) with Mail + Slack
        for i in range(3):
            block = timeline_store.TimelineBlock(
                start_time=datetime(2026, 5, 15, 8, i, tzinfo=_TZ),
                end_time=datetime(2026, 5, 15, 8, i + 1, tzinfo=_TZ),
                timezone="+08:00",
                entries=["morning"],
                apps_used=["Mail", "Slack"],
                capture_count=1,
            )
            timeline_store.insert(conn, block)

        # Work blocks (14:00) with Cursor only
        for i in range(3):
            block = timeline_store.TimelineBlock(
                start_time=datetime(2026, 5, 15, 14, i, tzinfo=_TZ),
                end_time=datetime(2026, 5, 15, 14, i + 1, tzinfo=_TZ),
                timezone="+08:00",
                entries=["coding"],
                apps_used=["Cursor"],
                capture_count=1,
            )
            timeline_store.insert(conn, block)

        routines = dream_mod._detect_routines(conn, datetime(2026, 5, 14, tzinfo=_TZ))

    assert "morning (06-10)" in routines
    assert "work (10-18)" in routines
    morning = routines["morning (06-10)"]
    assert any("Mail" in r["apps"] and "Slack" in r["apps"] for r in morning)


def test_detect_routines_empty_when_no_blocks(ac_root: Path) -> None:
    with fts.cursor() as conn:
        routines = dream_mod._detect_routines(conn, datetime.now(_TZ) - timedelta(days=1))
    assert routines == {}


# ─── _find_consecutive_patterns ─────────────────────────────────────────────


def test_find_consecutive_patterns_detects_stable_activity() -> None:
    stats = {
        "2026-05-13": {"Cursor": 240.0},  # 4h
        "2026-05-14": {"Cursor": 240.0},  # 4h
        "2026-05-15": {"Cursor": 240.0},  # 4h
    }
    patterns = dream_mod._find_consecutive_patterns(
        stats, min_consecutive_days=3, min_daily_hours=3.0
    )
    assert len(patterns) == 1
    assert patterns[0]["app"] == "Cursor"
    assert patterns[0]["days"] == 3
    assert patterns[0]["avg_hours"] == 4.0


def test_find_consecutive_patterns_ignores_gaps() -> None:
    stats = {
        "2026-05-13": {"Cursor": 240.0},
        "2026-05-14": {"Cursor": 240.0},
        # gap on 15th
        "2026-05-16": {"Cursor": 240.0},
    }
    patterns = dream_mod._find_consecutive_patterns(
        stats, min_consecutive_days=3, min_daily_hours=3.0
    )
    # No 3-consecutive-day run
    assert len(patterns) == 0


def test_find_consecutive_patterns_ignores_short_days() -> None:
    stats = {
        "2026-05-13": {"Cursor": 240.0},
        "2026-05-14": {"Cursor": 100.0},  # < 3h
        "2026-05-15": {"Cursor": 240.0},
    }
    patterns = dream_mod._find_consecutive_patterns(
        stats, min_consecutive_days=3, min_daily_hours=3.0
    )
    # Middle day breaks the run
    assert len(patterns) == 0


def test_find_consecutive_patterns_exact_hours_boundary() -> None:
    # Exactly min_daily_hours should be included, not excluded
    stats = {
        "2026-05-13": {"Cursor": 180.0},  # exactly 3.0h
        "2026-05-14": {"Cursor": 180.0},  # exactly 3.0h
        "2026-05-15": {"Cursor": 180.0},  # exactly 3.0h
    }
    patterns = dream_mod._find_consecutive_patterns(
        stats, min_consecutive_days=3, min_daily_hours=3.0
    )
    assert len(patterns) == 1
    assert patterns[0]["days"] == 3
    assert patterns[0]["avg_hours"] == 3.0


def test_find_consecutive_patterns_trailing_run_exact_boundary() -> None:
    # Trailing run that exactly meets min_consecutive_days
    stats = {
        "2026-05-13": {"Cursor": 100.0},  # < 3h, excluded
        "2026-05-14": {"Cursor": 240.0},  # 4h
        "2026-05-15": {"Cursor": 240.0},  # 4h
        "2026-05-16": {"Cursor": 240.0},  # 4h
    }
    patterns = dream_mod._find_consecutive_patterns(
        stats, min_consecutive_days=3, min_daily_hours=3.0
    )
    assert len(patterns) == 1
    assert patterns[0]["days"] == 3
    assert patterns[0]["latest_day"] == "2026-05-16"


def test_find_consecutive_patterns_multiple_apps_independent() -> None:
    # Each app's consecutive run should be tracked independently
    stats = {
        "2026-05-13": {"Cursor": 240.0, "Slack": 240.0},
        "2026-05-14": {"Cursor": 240.0, "Slack": 240.0},
        "2026-05-15": {"Cursor": 240.0, "Slack": 240.0},
    }
    patterns = dream_mod._find_consecutive_patterns(
        stats, min_consecutive_days=3, min_daily_hours=3.0
    )
    assert len(patterns) == 2
    apps = {p["app"] for p in patterns}
    assert apps == {"Cursor", "Slack"}


def test_find_consecutive_patterns_gap_then_valid_trailing_run() -> None:
    # A gap breaks the first run, but a trailing run of 3 valid days should still be found
    stats = {
        "2026-05-13": {"Cursor": 240.0},
        "2026-05-15": {"Cursor": 240.0},  # gap on 14th
        "2026-05-16": {"Cursor": 240.0},
        "2026-05-17": {"Cursor": 240.0},
    }
    patterns = dream_mod._find_consecutive_patterns(
        stats, min_consecutive_days=3, min_daily_hours=3.0
    )
    assert len(patterns) == 1
    assert patterns[0]["days"] == 3
    assert patterns[0]["latest_day"] == "2026-05-17"


# ─── _mine_chat_pairs ───────────────────────────────────────────────────────


def test_mine_chat_pairs_extracts_tool_call_pairs(ac_root: Path) -> None:
    history_dir = paths.root() / "chat-history"
    history_dir.mkdir(parents=True, exist_ok=True)

    messages = [
        {"role": "user", "content": "Search for docs about async"},
        {
            "role": "assistant",
            "content": "I'll search for you.",
            "tool_calls": [
                {
                    "id": "t1",
                    "function": {
                        "name": "search_memory",
                        "arguments": '{"query": "async rust"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "content": "found 3 hits"},
        {"role": "user", "content": "Thanks"},
        {"role": "assistant", "content": "You're welcome!"},
    ]
    # Filename uses today so it falls within lookback
    today = datetime.now().strftime("%Y%m%d-%H%M%S")
    (history_dir / f"{today}.json").write_text(json.dumps(messages, ensure_ascii=False))

    pairs = dream_mod._mine_chat_pairs(lookback_days=1, max_pairs=50)
    assert len(pairs) == 1
    assert pairs[0]["query"] == "Search for docs about async"
    assert pairs[0]["actions"][0]["tool"] == "search_memory"


def test_mine_chat_pairs_skips_non_tool_assistant_messages(ac_root: Path) -> None:
    history_dir = paths.root() / "chat-history"
    history_dir.mkdir(parents=True, exist_ok=True)

    messages = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    today = datetime.now().strftime("%Y%m%d-%H%M%S")
    (history_dir / f"{today}.json").write_text(json.dumps(messages, ensure_ascii=False))

    pairs = dream_mod._mine_chat_pairs(lookback_days=1, max_pairs=50)
    assert pairs == []


def test_mine_chat_pairs_empty_when_no_history(ac_root: Path) -> None:
    pairs = dream_mod._mine_chat_pairs(lookback_days=1, max_pairs=50)
    assert pairs == []


def test_mine_chat_pairs_tolerates_schema_drift(ac_root: Path, caplog) -> None:
    """Malformed entries are skipped, not allowed to crash the run."""
    history_dir = paths.root() / "chat-history"
    history_dir.mkdir(parents=True, exist_ok=True)

    messages = [
        # Valid pair — should be kept
        {"role": "user", "content": "valid query"},
        {
            "role": "assistant",
            "tool_calls": [{"function": {"name": "f", "arguments": "{}"}}],
        },
        # Malformed: assistant message is a bare string instead of dict
        {"role": "user", "content": "next query"},
        "this should be a dict",
        # Malformed: tool_calls entry is a string, not a dict
        {"role": "user", "content": "third query"},
        {"role": "assistant", "tool_calls": ["not a dict"]},
    ]
    today = datetime.now().strftime("%Y%m%d-%H%M%S")
    (history_dir / f"{today}.json").write_text(json.dumps(messages, ensure_ascii=False))

    import logging

    with caplog.at_level(logging.WARNING):
        pairs = dream_mod._mine_chat_pairs(lookback_days=1, max_pairs=50)

    # The valid pair survives; the malformed-dict and bad-tool_call entries
    # either skip silently or get caught — what matters is no crash and the
    # valid pair makes it through.
    queries = [p["query"] for p in pairs]
    assert "valid query" in queries
    # The third entry has a tool_calls list with a string element — the
    # per-tool extraction tolerates it (func=={}, name="unknown") rather
    # than crashing, so it may or may not appear. Either way, no exception.


def test_mine_chat_pairs_respects_max_pairs_cap(ac_root: Path) -> None:
    history_dir = paths.root() / "chat-history"
    history_dir.mkdir(parents=True, exist_ok=True)

    pairs_data: list[dict[str, Any]] = []
    for i in range(10):
        pairs_data.append({"role": "user", "content": f"q{i}"})
        pairs_data.append(
            {
                "role": "assistant",
                "tool_calls": [{"function": {"name": "f", "arguments": "{}"}}],
            }
        )

    today = datetime.now().strftime("%Y%m%d-%H%M%S")
    (history_dir / f"{today}.json").write_text(json.dumps(pairs_data, ensure_ascii=False))

    pairs = dream_mod._mine_chat_pairs(lookback_days=1, max_pairs=3)
    assert len(pairs) == 3


# ─── _assemble_context ──────────────────────────────────────────────────────


def test_assemble_context_emits_candidate_ids(ac_root: Path) -> None:
    """Stage-1 output is a flat candidate list with stable IDs the LLM can drill."""
    app_stats = {
        "2026-05-15": {"Cursor": 240.0, "Slack": 60.0},
    }
    app_sequences = [{"sequence": ["Cursor", "Slack"], "count": 5, "examples": []}]
    routines = {
        "morning (06-10)": [{"apps": ["Mail"], "count": 3, "examples": ["2026-05-15T08:00:00"]}]
    }
    chat_pairs = [
        {
            "query": "帮我生成日报",
            "actions": [{"tool": "write_file", "args_summary": ""}],
            "date": "2026-05-15",
            "source": "20260515-090000.json",
        }
    ]

    with fts.cursor() as conn:
        ctx = dream_mod._assemble_context(
            conn=conn,
            app_stats=app_stats,
            app_sequences=app_sequences,
            routines=routines,
            repeated_titles=[
                {
                    "value": "Daily Report - Sheet 1",
                    "count": 5,
                    "examples": [{"timestamp": "2026-05-15T14:00:00", "app": "Chrome"}],
                }
            ],
            repeated_urls=[
                {"value": "https://feishu.cn/sheets/abc123", "count": 5, "examples": []}
            ],
            chat_pairs=chat_pairs,
            lookback_days=7,
        )

    # Stable candidate IDs across families.
    assert "T01" in ctx, ctx
    assert "U01" in ctx, ctx
    assert "S01" in ctx, ctx
    assert "R01" in ctx, ctx
    assert "C01" in ctx, ctx

    # Drill hints point the LLM at the right tool + arguments.
    assert 'drill_window(title="Daily Report - Sheet 1"' in ctx
    assert 'drill_window(url="https://feishu.cn/sheets/abc123"' in ctx
    assert 'drill_chat(file="20260515-090000.json")' in ctx
    assert 'drill_timeline(date="2026-05-15")' in ctx

    # Surface values themselves so a reviewer can sanity-check the candidate.
    assert "Daily Report - Sheet 1" in ctx
    assert "feishu.cn/sheets" in ctx
    assert "Cursor → Slack" in ctx
    assert "帮我生成日报" in ctx
    # Footer keeps stats minimal — just days covered + latest day's top apps.
    assert "Days covered: 1" in ctx
    assert "Cursor=240m" in ctx


def test_assemble_context_handles_empty_inputs(ac_root: Path) -> None:
    """No data on any axis → emit explicit 'no candidates' marker, not an empty string."""
    with fts.cursor() as conn:
        ctx = dream_mod._assemble_context(
            conn=conn,
            app_stats={},
            app_sequences=[],
            routines={},
            repeated_titles=[],
            repeated_urls=[],
            chat_pairs=[],
            lookback_days=30,
        )
    assert "no candidates above threshold" in ctx
    # The header must still mention the lookback so the LLM knows the window.
    assert "30 days" in ctx


def test_find_repeated_captures_field_rejects_invalid_field(ac_root: Path) -> None:
    with fts.cursor() as conn, pytest.raises(ValueError, match="invalid field"):
        dream_mod._find_repeated_captures_field(
            conn,
            datetime.now(_TZ) - timedelta(days=1),
            field="timestamp; DROP TABLE captures;--",
            min_occurrences=2,
        )


# ─── _run_dream_loop (integration with fake LLM) ────────────────────────────


def test_dream_loop_creates_skill_and_commits(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = datetime(2026, 5, 15, 9, 0, tzinfo=_TZ)
    with fts.cursor() as conn:
        for i in range(3):
            block = timeline_store.TimelineBlock(
                start_time=base + timedelta(minutes=i),
                end_time=base + timedelta(minutes=i + 1),
                timezone="+08:00",
                entries=["work"],
                apps_used=["Cursor", "Slack"],
                capture_count=1,
            )
            timeline_store.insert(conn, block)

    cfg = _make_cfg()

    # Script: iter 1 → create skill draft, iter 2 → commit
    script = [
        _response(
            [
                _tool_call(
                    "create",
                    {
                        "path": "skills/skill-focus-routine.md",
                        "description": "Deep focus routine",
                        "tags": ["routine", "focus"],
                    },
                    cid="c1",
                ),
            ]
        ),
        _response(
            [
                _tool_call(
                    "append",
                    {
                        "path": "skills/skill-focus-routine.md",
                        "content": "User works in 90-min blocks with Cursor and Slack.",
                        "tags": ["routine"],
                    },
                    cid="c2",
                ),
            ]
        ),
        _response(
            [
                _tool_call(
                    "commit", {"summary": "Created skills/skill-focus-routine.md"}, cid="c3"
                ),
            ]
        ),
    ]

    # Monkeypatch call_llm with a scripted fake
    class _Scripted:
        def __init__(self, responses: list) -> None:
            self.responses = responses
            self.idx = 0

        def __call__(self, *a: Any, **k: Any) -> Any:
            r = self.responses[self.idx]
            self.idx += 1
            return r

    monkeypatch.setattr("persome.writer.llm.call_llm", _Scripted(script))

    with fts.cursor() as conn:
        result = dream_mod._run_dream_loop(cfg, conn, context="test context")

    assert result.committed
    assert result.summary == "Created skills/skill-focus-routine.md"
    assert "skills/skill-focus-routine.md" in result.created_paths


def test_dream_loop_blocks_event_writes(ac_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with fts.cursor() as conn:
        pass  # ensure schema exists

    cfg = _make_cfg()

    script = [
        _response(
            [
                _tool_call(
                    "append",
                    {
                        "path": "event-2026-05-15.md",
                        "content": "should be blocked",
                        "tags": ["test"],
                    },
                    cid="c1",
                ),
            ]
        ),
        _response(
            [
                _tool_call("commit", {"summary": "tried to write event"}, cid="c2"),
            ]
        ),
    ]

    class _Scripted:
        def __init__(self, responses: list) -> None:
            self.responses = responses
            self.idx = 0

        def __call__(self, *a: Any, **k: Any) -> Any:
            r = self.responses[self.idx]
            self.idx += 1
            return r

    monkeypatch.setattr("persome.writer.llm.call_llm", _Scripted(script))

    with fts.cursor() as conn:
        result = dream_mod._run_dream_loop(cfg, conn, context="test context")

    # Commit succeeds independently; but no entries were actually written
    assert result.committed
    assert result.written_ids == []
    assert result.created_paths == []


# ─── drill_capture / drill_window / drill_chat / drill_timeline ─────────────


def _insert_capture_full(
    conn,
    *,
    cid: str,
    ts: datetime,
    app: str = "Chrome",
    title: str = "",
    url: str = "",
    visible_text: str = "",
    focused_role: str = "",
    focused_value: str = "",
    bundle_id: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO captures
            (id, timestamp, app_name, bundle_id, window_title,
             focused_role, focused_value, visible_text, url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            cid,
            ts.isoformat(),
            app,
            bundle_id,
            title,
            focused_role,
            focused_value,
            visible_text,
            url,
        ),
    )


def test_drill_capture_returns_visible_text(ac_root: Path) -> None:
    state = tools_mod.CommitState()
    ts = datetime(2026, 5, 15, 14, 0, tzinfo=_TZ)
    with fts.cursor() as conn:
        _insert_capture_full(
            conn,
            cid="cap_a",
            ts=ts,
            app="Chrome",
            title="Daily Report",
            url="https://feishu.cn/sheets/x",
            visible_text="ABCDEFGHIJ" * 500,  # 5000 chars
            focused_role="AXTextField",
            focused_value="hello",
        )
        result = tools_mod.dispatch_dream(
            "drill_capture",
            {"capture_id": "cap_a", "text_limit": 100},
            conn=conn,
            soft_limit_tokens=20000,
            state=state,
        )

    assert "error" not in result
    assert result["id"] == "cap_a"
    assert result["app_name"] == "Chrome"
    assert result["window_title"] == "Daily Report"
    assert result["url"] == "https://feishu.cn/sheets/x"
    assert result["focused_role"] == "AXTextField"
    assert result["focused_value"] == "hello"
    assert len(result["visible_text"]) == 100
    assert result["truncated"] is True


def test_drill_capture_missing_returns_error(ac_root: Path) -> None:
    state = tools_mod.CommitState()
    with fts.cursor() as conn:
        result = tools_mod.dispatch_dream(
            "drill_capture",
            {"capture_id": "nonexistent"},
            conn=conn,
            soft_limit_tokens=20000,
            state=state,
        )
    assert "error" in result
    assert "nonexistent" in result["error"]


def test_drill_window_requires_one_filter(ac_root: Path) -> None:
    state = tools_mod.CommitState()
    with fts.cursor() as conn:
        result = tools_mod.dispatch_dream(
            "drill_window",
            {},
            conn=conn,
            soft_limit_tokens=20000,
            state=state,
        )
    assert "error" in result
    assert "title" in result["error"] or "url" in result["error"] or "app_name" in result["error"]


def test_drill_window_filters_by_title(ac_root: Path) -> None:
    state = tools_mod.CommitState()
    base = datetime.now().astimezone() - timedelta(hours=1)
    with fts.cursor() as conn:
        _insert_capture_full(
            conn,
            cid="c1",
            ts=base,
            app="Chrome",
            title="Daily Sales Report - Sheet 1",
            visible_text="row 1",
        )
        _insert_capture_full(
            conn,
            cid="c2",
            ts=base + timedelta(minutes=1),
            app="Chrome",
            title="Daily Sales Report - Sheet 2",
            visible_text="row 2",
        )
        _insert_capture_full(
            conn,
            cid="c3",
            ts=base + timedelta(minutes=2),
            app="Mail",
            title="Inbox",
            visible_text="email body",
        )
        result = tools_mod.dispatch_dream(
            "drill_window",
            {"title": "Daily Sales Report", "since_days": 1, "limit": 10, "text_preview": 5},
            conn=conn,
            soft_limit_tokens=20000,
            state=state,
        )

    assert result["count"] == 2
    titles = {row["window_title"] for row in result["captures"]}
    assert titles == {"Daily Sales Report - Sheet 1", "Daily Sales Report - Sheet 2"}
    # text_preview = 5 should clip "row 1" → "row 1" (5 chars)
    for row in result["captures"]:
        assert len(row["visible_text_preview"]) <= 5


def test_drill_chat_rejects_path_traversal(ac_root: Path) -> None:
    state = tools_mod.CommitState()
    with fts.cursor() as conn:
        for bad in ["../etc/passwd", "20260515.json", "20260515-09.json", "foo.json", ""]:
            result = tools_mod.dispatch_dream(
                "drill_chat",
                {"file": bad},
                conn=conn,
                soft_limit_tokens=20000,
                state=state,
            )
            assert "error" in result, f"expected error for {bad!r}, got {result}"


def test_drill_chat_reads_messages(ac_root: Path) -> None:
    history_dir = paths.root() / "chat-history"
    history_dir.mkdir(parents=True, exist_ok=True)
    messages = [
        {"role": "user", "content": "do thing 1"},
        {
            "role": "assistant",
            "content": "ok",
            "tool_calls": [
                {
                    "id": "t1",
                    "function": {"name": "search_memory", "arguments": '{"query": "abc"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "content": "found"},
        {"role": "user", "content": "do thing 2"},
        {"role": "assistant", "content": "done"},
    ]
    (history_dir / "20260515-090000.json").write_text(json.dumps(messages, ensure_ascii=False))

    state = tools_mod.CommitState()
    with fts.cursor() as conn:
        result = tools_mod.dispatch_dream(
            "drill_chat",
            {"file": "20260515-090000.json", "max_messages": 3, "text_limit_per_msg": 50},
            conn=conn,
            soft_limit_tokens=20000,
            state=state,
        )

    assert "error" not in result
    assert result["count"] == 3
    assert result["file"] == "20260515-090000.json"
    assert result["messages"][0]["role"] == "user"
    assert result["messages"][0]["content"] == "do thing 1"
    assert result["messages"][1]["tool_calls"][0]["tool"] == "search_memory"


def test_drill_timeline_filters_by_date(ac_root: Path) -> None:
    state = tools_mod.CommitState()
    day_d = datetime(2026, 5, 15, 9, 0, tzinfo=_TZ)
    day_e = datetime(2026, 5, 16, 9, 0, tzinfo=_TZ)
    with fts.cursor() as conn:
        for i in range(3):
            timeline_store.insert(
                conn,
                timeline_store.TimelineBlock(
                    start_time=day_d + timedelta(minutes=i),
                    end_time=day_d + timedelta(minutes=i + 1),
                    timezone="+08:00",
                    entries=["d-block"],
                    apps_used=["Cursor"],
                    capture_count=1,
                ),
            )
        for i in range(2):
            timeline_store.insert(
                conn,
                timeline_store.TimelineBlock(
                    start_time=day_e + timedelta(minutes=i),
                    end_time=day_e + timedelta(minutes=i + 1),
                    timezone="+08:00",
                    entries=["e-block"],
                    apps_used=["Slack"],
                    capture_count=1,
                ),
            )

        result = tools_mod.dispatch_dream(
            "drill_timeline",
            {"date": "2026-05-15", "limit": 10},
            conn=conn,
            soft_limit_tokens=20000,
            state=state,
        )

    assert "error" not in result
    assert result["date"] == "2026-05-15"
    assert result["count"] == 3
    for b in result["blocks"]:
        assert b["start"].startswith("2026-05-15")
        assert "Cursor" in b["apps"]


# ─── loop config & resilience ──────────────────────────────────────────────


class _Scripted:
    def __init__(self, responses: list) -> None:
        self.responses = responses
        self.idx = 0

    def __call__(self, *a: Any, **k: Any) -> Any:
        r = self.responses[self.idx]
        self.idx += 1
        return r


def test_loop_uses_dream_max_tool_iterations(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loop must honor cfg.dream.max_tool_iterations, not cfg.writer.max_tool_iterations."""
    cfg = config_mod.Config(
        # Dream allows 30 iterations; writer's 3 would abort prematurely.
        dream=config_mod.DreamConfig(enabled=True, max_tool_iterations=30),
        writer=config_mod.WriterConfig(max_tool_iterations=3),
    )

    # 25 no-op drill_capture calls (all miss → error response) + final commit.
    script: list[Any] = []
    for i in range(25):
        script.append(
            _response([_tool_call("drill_capture", {"capture_id": f"missing_{i}"}, cid=f"d{i}")])
        )
    script.append(_response([_tool_call("commit", {"summary": "done"}, cid="c_final")]))

    monkeypatch.setattr("persome.writer.llm.call_llm", _Scripted(script))

    with fts.cursor() as conn:
        result = dream_mod._run_dream_loop(cfg, conn, context="test")

    assert result.committed, result
    # 25 drill iterations + 1 commit iteration = 26 turns.
    assert result.iterations == 26


def test_loop_unknown_tool_continues(ac_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown tool name returns an error to the model and the loop keeps going."""
    cfg = _make_cfg()

    script = [
        _response([_tool_call("foo_bar_unknown", {}, cid="c1")]),
        _response([_tool_call("commit", {"summary": "ok"}, cid="c2")]),
    ]
    monkeypatch.setattr("persome.writer.llm.call_llm", _Scripted(script))

    with fts.cursor() as conn:
        result = dream_mod._run_dream_loop(cfg, conn, context="test")

    assert result.committed
    assert result.iterations == 2


def test_loop_exhausted_returns_skipped_reason(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the model never commits, the result carries a loop_exhausted reason."""
    cfg = config_mod.Config(
        dream=config_mod.DreamConfig(enabled=True, max_tool_iterations=3),
        writer=config_mod.WriterConfig(max_tool_iterations=10),
    )
    script: list[Any] = [
        _response([_tool_call("drill_capture", {"capture_id": "missing"}, cid=f"d{i}")])
        for i in range(3)
    ]
    monkeypatch.setattr("persome.writer.llm.call_llm", _Scripted(script))

    with fts.cursor() as conn:
        result = dream_mod._run_dream_loop(cfg, conn, context="test")

    assert not result.committed
    assert result.skipped_reason == "loop_exhausted"
    assert result.iterations == 3


# ─── DreamConfig defaults ──────────────────────────────────────────────────


def test_dreamconfig_defaults_match_plan() -> None:
    cfg = config_mod.DreamConfig()
    assert cfg.lookback_days == 30
    assert cfg.max_tool_iterations == 30
    assert cfg.min_skill_confidence == 0.6

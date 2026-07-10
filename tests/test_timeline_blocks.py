"""Timeline block production: LLM round-trip, parser telemetry, store schema.

* :func:`persome.timeline.aggregator.produce_block_for_window` is driven
  with the ``fake_llm`` fixture: entries round-trip, heuristic fallback on
  malformed JSON, and cache-friendly call shape.
* Parser-hit telemetry ticks (hit / miss / fallback) are exercised against
  the real 飞书 fixture.
* :func:`persome.timeline.store._row_to_block` is checked against legacy
  rows that pre-date newer columns to confirm the migration + read path are
  forgiving.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from persome import config as config_mod
from persome import paths
from persome.store import fts
from persome.timeline import aggregator
from persome.timeline import store as timeline_store

_TZ = timezone(timedelta(hours=8))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _stem(ts: datetime) -> str:
    """Mirror ``capture.scheduler._safe_filename``."""
    return ts.isoformat().replace(":", "-").replace("+", "p")


def _write_capture(ts: datetime, *, value: str, role: str = "AXTextField") -> Path:
    """Plant a v2-shape capture JSON into the buffer dir."""
    payload = {
        "timestamp": ts.isoformat(),
        "schema_version": 2,
        "trigger": {"event_type": "focus"},
        "window_meta": {
            "app_name": "WeChat",
            "title": "Chat with 张三",
            "bundle_id": "com.tencent.xinWeChat",
        },
        "focused_element": {
            "role": role,
            "is_editable": True,
            "title": "message",
            "value_length": len(value),
            "value": value,
        },
        "visible_text": "Chat history line 1\nChat history line 2",
    }
    path = paths.capture_buffer_dir() / f"{_stem(ts)}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _seed_window(start: datetime) -> tuple[datetime, datetime]:
    """Plant two captures inside a 1-min window starting at ``start``."""
    _write_capture(start + timedelta(seconds=10), value="想想晚上吃啥")
    _write_capture(
        start + timedelta(seconds=40),
        value="好啊，明天下午5点聊一下",
    )
    return start, start + timedelta(minutes=1)


# ---------------------------------------------------------------------------
# produce_block_for_window — happy path
# ---------------------------------------------------------------------------


_HAPPY_PAYLOAD = json.dumps(
    {
        "entries": [
            (
                '[WeChat] Chat with 张三: user replied "好啊，明天下午5点聊一下",'
                " accepting the proposed time. Involving: 张三."
                " Helpful intent: meeting with 张三 at 明天下午5点."
            )
        ],
    },
    ensure_ascii=False,
)


def test_produce_block_round_trips_entries(ac_root: Path, fake_llm) -> None:
    start = datetime(2026, 4, 21, 17, 7, tzinfo=_TZ)
    win_start, win_end = _seed_window(start)
    fake_llm.set_default("timeline", _HAPPY_PAYLOAD)

    cfg = config_mod.load(ac_root / "config.toml")
    block = aggregator.produce_block_for_window(cfg, start=win_start, end=win_end)

    assert block is not None
    assert block.entries == [
        '[WeChat] Chat with 张三: user replied "好啊，明天下午5点聊一下",'
        " accepting the proposed time. Involving: 张三."
        " Helpful intent: meeting with 张三 at 明天下午5点."
    ]


def test_produce_block_accepts_entries_only(ac_root: Path, fake_llm) -> None:
    start = datetime(2026, 4, 21, 17, 11, tzinfo=_TZ)
    win_start, win_end = _seed_window(start)

    fake_llm.set_default(
        "timeline",
        json.dumps({"entries": ["[WeChat] something happened"]}),
    )
    cfg = config_mod.load(ac_root / "config.toml")
    block = aggregator.produce_block_for_window(cfg, start=win_start, end=win_end)

    assert block is not None
    assert block.entries == ["[WeChat] something happened"]


def test_produce_block_handles_malformed_llm_json(ac_root: Path, fake_llm) -> None:
    """Truly invalid JSON falls back to the heuristic entry list."""
    start = datetime(2026, 4, 21, 17, 15, tzinfo=_TZ)
    win_start, win_end = _seed_window(start)

    fake_llm.set_default("timeline", "not json {")
    cfg = config_mod.load(ac_root / "config.toml")
    block = aggregator.produce_block_for_window(cfg, start=win_start, end=win_end)

    assert block is not None
    assert block.entries  # heuristic fallback


def test_produce_block_records_calls_with_json_mode(ac_root: Path, fake_llm) -> None:
    """Sanity check that the LLM is invoked with ``json_mode=True`` for the
    timeline stage — the prompt contract depends on this. We assert via the
    captured call rather than a brittle pickle check.
    """
    start = datetime(2026, 4, 21, 17, 17, tzinfo=_TZ)
    win_start, win_end = _seed_window(start)
    fake_llm.set_default("timeline", _HAPPY_PAYLOAD)

    cfg = config_mod.load(ac_root / "config.toml")
    aggregator.produce_block_for_window(cfg, start=win_start, end=win_end)

    assert len(fake_llm.calls) == 1
    call = fake_llm.calls[0]
    assert call["stage"] == "timeline"
    # Messages now use the cache-friendly system+user split: messages[0] is
    # the cached system prompt (list-of-blocks with cache_control), messages[1]
    # is the per-call user payload carrying the rendered events_text.
    user_content = call["messages"][1]["content"]
    assert "好啊，明天下午5点聊一下" in user_content
    system_block = call["messages"][0]["content"][0]
    assert system_block["cache_control"] == {"type": "ephemeral"}


# ---------------------------------------------------------------------------
# produce_block_for_window — parser-hit telemetry (Phase 3)
# ---------------------------------------------------------------------------

_LARK_FIXTURE = (
    Path(__file__).parent / "fixtures" / "captures" / "lark" / "feed-list-with-meeting.json"
)


def _load_lark_fixture() -> dict:
    """Load the team-local real 飞书 capture; skip the caller when absent."""
    if not _LARK_FIXTURE.exists():  # team-local, not distributed on GitHub
        pytest.skip("team-local capture fixture not present: lark/feed-list-with-meeting.json")
    return json.loads(_LARK_FIXTURE.read_text(encoding="utf-8"))


def _write_lark_capture(ts: datetime) -> Path:
    """Plant the real 飞书 fixture capture (ax_tree + bundle) at ``ts``."""
    data = _load_lark_fixture()
    data["timestamp"] = ts.isoformat()
    path = paths.capture_buffer_dir() / f"{_stem(ts)}.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _write_bundle_capture(ts: datetime, *, bundle_id: str, with_ax: bool = True) -> Path:
    """Plant a minimal capture for an arbitrary bundle (no registered parser)."""
    data: dict = {
        "timestamp": ts.isoformat(),
        "schema_version": 2,
        "window_meta": {"app_name": "X", "title": "X", "bundle_id": bundle_id},
        "visible_text": "some screen text",
    }
    if with_ax:
        data["ax_tree"] = {"apps": [{"bundle_id": bundle_id, "windows": []}]}
    path = paths.capture_buffer_dir() / f"{_stem(ts)}.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _latest_parser_tick() -> sqlite3.Row | None:
    with fts.cursor() as conn:
        from persome.store import parser_ticks as pt

        pt.ensure_schema(conn)
        return conn.execute(
            "SELECT ts, bundle_id, outcome FROM parser_ticks ORDER BY id DESC LIMIT 1"
        ).fetchone()


def test_produce_block_records_parser_hit_for_feishu(ac_root: Path, fake_llm) -> None:
    """A window with a 飞书 capture whose parser renders → one ``hit`` tick
    attributed to bundle com.electron.lark."""
    start = datetime(2026, 6, 2, 12, 20, tzinfo=_TZ)
    _write_lark_capture(start + timedelta(seconds=20))
    fake_llm.set_default("timeline", _HAPPY_PAYLOAD)

    cfg = config_mod.load(ac_root / "config.toml")
    block = aggregator.produce_block_for_window(cfg, start=start, end=start + timedelta(minutes=1))

    assert block is not None
    assert block.focus_structured  # parser rendered → block carries structured text
    row = _latest_parser_tick()
    assert row is not None
    assert row["outcome"] == "hit"
    assert row["bundle_id"] == "com.electron.lark"
    assert row["ts"] == start.isoformat()


def test_produce_block_records_fallback_for_unparsed_bundle(ac_root: Path, fake_llm) -> None:
    """A window whose only app has no registered parser → one ``fallback`` tick
    attributed to that bundle (modeling falls back to focus_excerpt)."""
    start = datetime(2026, 6, 2, 12, 30, tzinfo=_TZ)
    _write_bundle_capture(start + timedelta(seconds=20), bundle_id="com.apple.Safari")
    fake_llm.set_default("timeline", _HAPPY_PAYLOAD)

    cfg = config_mod.load(ac_root / "config.toml")
    block = aggregator.produce_block_for_window(cfg, start=start, end=start + timedelta(minutes=1))

    assert block is not None
    assert block.focus_structured == ""
    row = _latest_parser_tick()
    assert row is not None
    assert row["outcome"] == "fallback"
    assert row["bundle_id"] == "com.apple.Safari"


def test_produce_block_records_miss_when_parser_declines(
    ac_root: Path, fake_llm, monkeypatch
) -> None:
    """A 飞书 capture whose parser returns ``None`` (declines) → one ``miss``
    tick attributed to com.electron.lark."""
    start = datetime(2026, 6, 2, 12, 40, tzinfo=_TZ)
    _write_lark_capture(start + timedelta(seconds=20))
    fake_llm.set_default("timeline", _HAPPY_PAYLOAD)

    # Force the registered 飞书 parser to decline so we exercise the miss branch
    # without depending on a malformed fixture.
    from persome.parsers import feishu as feishu_mod

    monkeypatch.setattr(
        feishu_mod.FeishuParser, "parse", lambda self, ax_tree, *, window_title: None
    )

    cfg = config_mod.load(ac_root / "config.toml")
    block = aggregator.produce_block_for_window(cfg, start=start, end=start + timedelta(minutes=1))

    assert block is not None
    assert block.focus_structured == ""
    row = _latest_parser_tick()
    assert row is not None
    assert row["outcome"] == "miss"
    assert row["bundle_id"] == "com.electron.lark"


def test_produce_block_no_parser_tick_when_nothing_parseable(ac_root: Path, fake_llm) -> None:
    """A window whose captures carry no ax_tree → no parser tick recorded
    (``_focus_structured_with_outcome`` returns outcome=None)."""
    start = datetime(2026, 6, 2, 12, 50, tzinfo=_TZ)
    _write_bundle_capture(
        start + timedelta(seconds=20), bundle_id="com.apple.Safari", with_ax=False
    )
    fake_llm.set_default("timeline", _HAPPY_PAYLOAD)

    cfg = config_mod.load(ac_root / "config.toml")
    block = aggregator.produce_block_for_window(cfg, start=start, end=start + timedelta(minutes=1))

    assert block is not None
    with fts.cursor() as conn:
        from persome.store import parser_ticks as pt

        assert pt.stats(conn)["total"] == 0


# ---------------------------------------------------------------------------
# store: migration + row reader
# ---------------------------------------------------------------------------


def test_ensure_schema_migrates_legacy_table(ac_root: Path) -> None:
    """A pre-PR1 timeline_blocks table without the new column must be migrated
    in place rather than crash on subsequent ``ensure_schema`` calls.
    """
    with fts.cursor() as conn:
        # Drop the auto-created modern table and replace with the v0 shape.
        conn.execute("DROP TABLE IF EXISTS timeline_blocks")
        conn.execute(
            """
            CREATE TABLE timeline_blocks (
                id TEXT PRIMARY KEY,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                timezone TEXT NOT NULL DEFAULT '',
                entries TEXT NOT NULL,
                apps_used TEXT NOT NULL,
                capture_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(start_time, end_time)
            )
            """
        )
        conn.execute(
            "INSERT INTO timeline_blocks VALUES (?,?,?,?,?,?,?,?)",
            (
                "tlb-legacy",
                "2026-04-21T17:00:00+08:00",
                "2026-04-21T17:01:00+08:00",
                "+08:00",
                json.dumps(["[Test] legacy entry"]),
                json.dumps(["Test"]),
                3,
                "2026-04-21T17:01:05+08:00",
            ),
        )

        # Re-run ensure_schema and confirm current optional columns appear.
        timeline_store.ensure_schema(conn)
        row = conn.execute(
            "SELECT skill_hints, focus_structured FROM timeline_blocks WHERE id = 'tlb-legacy'"
        ).fetchone()
        assert row is not None
        assert row["skill_hints"] == "[]"
        assert row["focus_structured"] == ""

        # And _row_to_block must still round-trip the legacy row.
        full = conn.execute("SELECT * FROM timeline_blocks WHERE id = 'tlb-legacy'").fetchone()
        block = timeline_store._row_to_block(full)
        assert block.id == "tlb-legacy"
        assert block.entries == ["[Test] legacy entry"]
        assert block.skill_hints == []


def test_row_to_block_defaults_when_column_missing() -> None:
    """A legacy row that omits optional columns still parses safely."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE timeline_blocks (
            id TEXT,
            start_time TEXT,
            end_time TEXT,
            timezone TEXT,
            entries TEXT,
            apps_used TEXT,
            capture_count INTEGER,
            created_at TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO timeline_blocks VALUES (?,?,?,?,?,?,?,?)",
        (
            "tlb-no-col",
            "2026-04-21T17:00:00+08:00",
            "2026-04-21T17:01:00+08:00",
            "+08:00",
            json.dumps(["a"]),
            json.dumps(["App"]),
            1,
            "2026-04-21T17:01:05+08:00",
        ),
    )
    row = conn.execute("SELECT * FROM timeline_blocks").fetchone()
    block = timeline_store._row_to_block(row)
    assert block.skill_hints == []
    assert block.focus_structured == ""


# ---------------------------------------------------------------------------
# focus_excerpt (raw visible_text backstop for session modeling)
# ---------------------------------------------------------------------------


def test_focus_excerpt_roundtrip(ac_root: Path) -> None:
    start = datetime(2026, 6, 2, 11, 27, tzinfo=_TZ)
    blk = timeline_store.TimelineBlock(
        start_time=start,
        end_time=start + timedelta(minutes=1),
        entries=["[Feishu] browsed conversations"],
        apps_used=["Feishu"],
        capture_count=1,
        focus_excerpt="温子墨: 晚上8点约一个会议",
    )
    with fts.cursor() as conn:
        timeline_store.ensure_schema(conn)
        timeline_store.insert(conn, blk)
        got = timeline_store.query_recent(conn, limit=1)[0]
    assert got.focus_excerpt == "温子墨: 晚上8点约一个会议"


def test_focus_excerpt_picks_last_nonempty_and_truncates() -> None:
    parsed = [
        (Path("a.json"), {"visible_text": "older"}),
        (Path("b.json"), {"visible_text": ""}),  # empty trailing — skipped
        (Path("c.json"), {"visible_text": "x" * 99999}),  # but this is later & non-empty
    ]
    # reversed() walk → picks the last non-empty ("c"), truncated to budget
    out = aggregator._focus_excerpt(parsed)
    assert out.startswith("x")
    assert len(out) == aggregator._FOCUS_EXCERPT_CHARS


def test_focus_excerpt_empty_when_no_text() -> None:
    assert aggregator._focus_excerpt([(Path("a.json"), {"visible_text": ""})]) == ""


# ---------------------------------------------------------------------------
# focus_structured (per-app parser output fed to session modeling)
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).parent / "fixtures" / "captures" / "lark"


def test_focus_structured_roundtrip(ac_root: Path) -> None:
    """A block with ``focus_structured`` stores and reads back the raw text."""
    start = datetime(2026, 6, 2, 12, 20, tzinfo=_TZ)
    structured = "飞书\n[收到|沈砚舟|12:20] 我超\n[收到|温子墨|11:27] 晚上8点约一个会议"
    blk = timeline_store.TimelineBlock(
        start_time=start,
        end_time=start + timedelta(minutes=1),
        entries=["[Feishu] browsed conversations"],
        apps_used=["Feishu"],
        capture_count=1,
        focus_structured=structured,
    )
    with fts.cursor() as conn:
        timeline_store.ensure_schema(conn)
        timeline_store.insert(conn, blk)
        got = timeline_store.query_recent(conn, limit=1)[0]
    assert got.focus_structured == structured


# ---------------------------------------------------------------------------
# attention locus (Step 1 — code-owned "where", PRIMARY/PERIPHERAL feed)
# ---------------------------------------------------------------------------


def _cmux_capture(ts: datetime) -> tuple[Path, dict]:
    chrome = "## cmux [active]\nworkspace 1/7\n有可用更新：0.64.16\n切换侧边栏"
    pane = "❯ uv run pytest -k attention\n12 passed real work here"
    data = {
        "timestamp": ts.isoformat(),
        "window_meta": {"app_name": "cmux", "title": "⠂ task", "bundle_id": "com.cmuxterm.app"},
        "focused_element": {},
        "visible_text": chrome + "\n### [cmux terminal]\n" + pane,
        "trigger": {"event_type": "AXApplicationActivated"},
    }
    return Path(f"{_stem(ts)}.json"), data


def test_format_events_cmux_primary_drops_chrome() -> None:
    """Locus on: a cmux capture is fed as PRIMARY = the terminal pane, with the
    workspace/tab chrome dropped, and the block's dominant locus is the pane."""
    cap = _cmux_capture(datetime(2026, 6, 18, 17, 53, tzinfo=_TZ))
    events_text, _apps, loc = aggregator._format_events([cap], locus_enabled=True)
    assert "PRIMARY:" in events_text
    assert "12 passed real work here" in events_text
    assert "workspace 1/7" not in events_text  # chrome dropped
    assert "有可用更新" not in events_text
    assert loc is not None and loc.rung == "pane"
    assert loc.confidence > 0.0


def test_format_events_flag_off_uses_legacy_feed() -> None:
    """Locus off: the pre-Step-1 feed (FOCUSED PANE label) and no block locus."""
    cap = _cmux_capture(datetime(2026, 6, 18, 17, 53, tzinfo=_TZ))
    events_text, _apps, loc = aggregator._format_events([cap], locus_enabled=False)
    assert "FOCUSED PANE:" in events_text
    assert "PRIMARY:" not in events_text
    assert loc is None


def test_format_events_generic_app_not_narrowed() -> None:
    """Fail-open parity: a resolver-less app's whole visible_text is still fed
    (PRIMARY = the full window), only annotated with a locus — never narrowed."""
    ts = datetime(2026, 6, 18, 17, 53, tzinfo=_TZ)
    data = {
        "timestamp": ts.isoformat(),
        "window_meta": {"app_name": "Safari", "title": "Docs", "bundle_id": "com.apple.Safari"},
        "focused_element": {"role": "AXStaticText", "is_editable": False, "value": "reading"},
        "visible_text": "the full page body the user is reading",
    }
    events_text, _apps, loc = aggregator._format_events(
        [(Path(f"{_stem(ts)}.json"), data)], locus_enabled=True
    )
    assert "the full page body the user is reading" in events_text  # whole window kept
    assert loc is not None and loc.rung in {"focus", "fallback"}


def test_produce_block_persists_attention_locus(ac_root: Path, fake_llm) -> None:
    """End-to-end: the editing rung of a WeChat composer capture lands on the
    block and round-trips through the DB."""
    start = datetime(2026, 4, 21, 18, 30, tzinfo=_TZ)
    win_start, win_end = _seed_window(start)
    fake_llm.set_default("timeline", json.dumps({"entries": ["[WeChat] chat"]}))
    cfg = config_mod.load(ac_root / "config.toml")
    block = aggregator.produce_block_for_window(cfg, start=win_start, end=win_end)
    assert block is not None
    assert block.attention_rung == "editing"
    assert block.attention_confidence == 0.9
    assert block.attention_surface == "Chat with 张三"
    with fts.cursor() as conn:
        got = timeline_store.query_recent(conn, limit=1)[0]
    assert got.attention_rung == "editing"
    assert got.attention_confidence == 0.9
    assert got.attention_surface == "Chat with 张三"


def test_ensure_schema_migrates_table_without_focus_structured(ac_root: Path) -> None:
    """A pre-Phase-2 table (has focus_excerpt but no focus_structured) is migrated
    in place, defaulting the new column to '' and round-tripping legacy rows."""
    with fts.cursor() as conn:
        conn.execute("DROP TABLE IF EXISTS timeline_blocks")
        conn.execute(
            """
            CREATE TABLE timeline_blocks (
                id TEXT PRIMARY KEY,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                timezone TEXT NOT NULL DEFAULT '',
                entries TEXT NOT NULL,
                apps_used TEXT NOT NULL,
                capture_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                skill_hints TEXT NOT NULL DEFAULT '[]',
                action_trace TEXT NOT NULL DEFAULT '[]',
                focus_excerpt TEXT NOT NULL DEFAULT '',
                UNIQUE(start_time, end_time)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO timeline_blocks
                (id, start_time, end_time, timezone, entries, apps_used, capture_count,
                 created_at, skill_hints, action_trace, focus_excerpt)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "tlb-pre-p2",
                "2026-06-02T12:20:00+08:00",
                "2026-06-02T12:21:00+08:00",
                "+08:00",
                json.dumps(["[Feishu] legacy entry"]),
                json.dumps(["Feishu"]),
                2,
                "2026-06-02T12:21:05+08:00",
                "[]",
                "[]",
                "温子墨: 晚上8点约一个会议",
            ),
        )

        timeline_store.ensure_schema(conn)
        row = conn.execute(
            "SELECT focus_structured FROM timeline_blocks WHERE id = 'tlb-pre-p2'"
        ).fetchone()
        assert row is not None
        assert row["focus_structured"] == ""

        full = conn.execute("SELECT * FROM timeline_blocks WHERE id = 'tlb-pre-p2'").fetchone()
        block = timeline_store._row_to_block(full)
        assert block.id == "tlb-pre-p2"
        assert block.focus_excerpt == "温子墨: 晚上8点约一个会议"
        assert block.focus_structured == ""


def test_focus_structured_renders_feishu_fixture() -> None:
    """Feeding the Phase-1 Feishu fixture's ax_tree + window_meta through
    the production parser entrance yields signal the normalizer would lose."""
    parsed = [(Path("cap.json"), _load_lark_fixture())]
    out, bundle, outcome, reason = aggregator._focus_structured_with_outcome(parsed)
    assert out  # non-empty
    assert bundle == "com.electron.lark"
    assert outcome == "hit"
    assert reason is None
    assert "会议" in out
    assert "沈砚舟" in out
    assert 'dir="sent"' in out  # XML message tag for an outgoing turn
    # The two-section XML layout: current thread vs other-conversation previews,
    # kept distinct so N unrelated previews don't read as one conversation. The
    # current-conversation tag names which conversation is open (沈砚舟).
    assert '<current_conversation name="沈砚舟">' in out
    assert "<other_conversations" in out
    assert "<preview" in out


def test_focus_structured_empty_for_unparsed_bundle() -> None:
    """A capture whose app has no registered parser yields '' so modeling
    falls back to focus_excerpt (#258)."""
    data = {
        "ax_tree": {"apps": [{"bundle_id": "com.unknown.app", "windows": []}]},
        "window_meta": {"bundle_id": "com.unknown.app", "title": "Whatever"},
    }
    text, bundle, outcome, reason = aggregator._focus_structured_with_outcome(
        [(Path("cap.json"), data)]
    )
    assert (text, bundle, outcome, reason) == ("", "com.unknown.app", "fallback", None)


def test_focus_structured_empty_when_no_ax_tree() -> None:
    """A capture missing ax_tree is skipped (parser needs the tree)."""
    data = {"window_meta": {"bundle_id": "com.electron.lark", "title": "飞书"}}
    assert aggregator._focus_structured_with_outcome([(Path("cap.json"), data)]) == (
        "",
        None,
        None,
        None,
    )
    assert aggregator._focus_excerpt([]) == ""


# ---------------------------------------------------------------------------
# _focus_structured_with_outcome — miss-reason breakdown (#548)
# ---------------------------------------------------------------------------


def _propagate_timeline_logs(monkeypatch) -> None:
    """Re-enable propagation on the timeline logger so caplog sees its records.

    ``logger.setup`` (run by any earlier test that boots logging) flips
    ``propagate`` off on persome loggers; caplog captures via the root
    handler, so without this the suite-order would decide whether these
    assertions see anything.
    """
    import logging

    monkeypatch.setattr(logging.getLogger("persome.timeline"), "propagate", True)


_IRON_FIXTURE = (
    Path(__file__).parent / "fixtures" / "captures" / "lark-iron" / "meeting-window.json"
)


def test_outcome_hit_has_no_miss_reason() -> None:
    parsed = [(Path("cap.json"), _load_lark_fixture())]
    text, bundle, outcome, reason = aggregator._focus_structured_with_outcome(parsed)
    assert text
    assert (bundle, outcome, reason) == ("com.electron.lark", "hit", None)


def test_outcome_fallback_has_no_miss_reason() -> None:
    data = {
        "ax_tree": {"apps": [{"bundle_id": "com.unknown.app", "windows": []}]},
        "window_meta": {"bundle_id": "com.unknown.app", "title": "Whatever"},
    }
    text, bundle, outcome, reason = aggregator._focus_structured_with_outcome(
        [(Path("cap.json"), data)]
    )
    assert (text, bundle, outcome, reason) == ("", "com.unknown.app", "fallback", None)


def test_miss_reason_decline_on_iron_meeting_window(monkeypatch, caplog) -> None:
    """The real lark.iron shape (empty AX tree) → miss with reason=decline, and a
    structured ``parser_miss`` log line carrying bundle + reason + capture."""
    _propagate_timeline_logs(monkeypatch)
    data = json.loads(_IRON_FIXTURE.read_text(encoding="utf-8"))
    with caplog.at_level("INFO", logger="persome.timeline"):
        text, bundle, outcome, reason = aggregator._focus_structured_with_outcome(
            [(Path("iron-cap.json"), data)]
        )
    assert (text, bundle, outcome, reason) == ("", "com.electron.lark.iron", "miss", "decline")
    miss_lines = [r.getMessage() for r in caplog.records if "parser_miss" in r.getMessage()]
    assert miss_lines, "expected one structured parser_miss log line"
    assert "bundle=com.electron.lark.iron" in miss_lines[0]
    assert "reason=decline" in miss_lines[0]
    assert "capture=iron-cap.json" in miss_lines[0]


def test_miss_reason_empty_render(monkeypatch, caplog) -> None:
    """Parser returns a conversation whose render() is empty → reason=empty_render
    (anchors matched but produced nothing — a parser bug smell, NOT a correct
    decline; the reason keeps the two separable)."""

    _propagate_timeline_logs(monkeypatch)

    class _EmptyConv:
        def render(self) -> str:
            return "  "

    from persome.parsers import feishu as feishu_mod

    monkeypatch.setattr(
        feishu_mod.FeishuParser,
        "parse",
        lambda self, ax_tree, *, window_title: _EmptyConv(),
    )
    with caplog.at_level("INFO", logger="persome.timeline"):
        text, bundle, outcome, reason = aggregator._focus_structured_with_outcome(
            [(Path("cap.json"), _load_lark_fixture())]
        )
    assert (text, bundle, outcome, reason) == ("", "com.electron.lark", "miss", "empty_render")
    assert any(
        "parser_miss" in r.getMessage() and "reason=empty_render" in r.getMessage()
        for r in caplog.records
    )


def test_miss_reason_exception(monkeypatch, caplog) -> None:
    """Parser raises → reason=exception, logged as a warning with the error."""
    _propagate_timeline_logs(monkeypatch)

    from persome.parsers import feishu as feishu_mod

    def _boom(self, ax_tree, *, window_title):
        raise RuntimeError("anchor drift")

    monkeypatch.setattr(feishu_mod.FeishuParser, "parse", _boom)
    with caplog.at_level("WARNING", logger="persome.timeline"):
        text, bundle, outcome, reason = aggregator._focus_structured_with_outcome(
            [(Path("cap.json"), _load_lark_fixture())]
        )
    assert (text, bundle, outcome, reason) == ("", "com.electron.lark", "miss", "exception")
    assert any(
        "parser_miss" in r.getMessage()
        and "reason=exception" in r.getMessage()
        and "anchor drift" in r.getMessage()
        for r in caplog.records
    )


def test_produce_block_records_miss_for_iron_meeting_window(ac_root: Path, fake_llm) -> None:
    """End-to-end #548: a window whose newest capture is the 飞书会议 (lark.iron)
    window now records a ``miss`` tick attributed to com.electron.lark.iron —
    previously an unowned ``fallback`` (the bundle had no registered parser)."""
    start = datetime(2026, 6, 11, 22, 0, tzinfo=_TZ)
    ts = start + timedelta(seconds=20)
    data = json.loads(_IRON_FIXTURE.read_text(encoding="utf-8"))
    data["timestamp"] = ts.isoformat()
    (paths.capture_buffer_dir() / f"{_stem(ts)}.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )
    fake_llm.set_default("timeline", _HAPPY_PAYLOAD)

    cfg = config_mod.load(ac_root / "config.toml")
    block = aggregator.produce_block_for_window(cfg, start=start, end=start + timedelta(minutes=1))

    assert block is not None
    assert block.focus_structured == ""
    row = _latest_parser_tick()
    assert row is not None
    assert row["outcome"] == "miss"
    assert row["bundle_id"] == "com.electron.lark.iron"

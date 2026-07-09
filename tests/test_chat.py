"""Unit tests for chat/handler.py away-summary and microcompact helpers."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from persome.chat.handler import (
    _exec_tool,
    _maybe_away_summary,
    _microcompact,
    _microcompact_on_resume,
    _parse_session_marker_timestamp,
)
from persome.config import Config, ModelConfig


def _cfg_with_model(model: str = "gpt-5.4-nano") -> Config:
    return Config(models={"default": ModelConfig(model=model)})


# ─── _parse_session_marker_timestamp ────────────────────────────────────────


def test_parse_session_marker_timestamp_valid() -> None:
    ts = _parse_session_marker_timestamp("[SESSION EXIT at 2026-05-15 14:30:00]")
    assert ts is not None
    assert ts.year == 2026
    assert ts.month == 5
    assert ts.day == 15
    assert ts.hour == 14
    assert ts.minute == 30


def test_parse_session_marker_timestamp_resume_also_works() -> None:
    ts = _parse_session_marker_timestamp("[SESSION RESUME at 2026-01-01 00:00:00]")
    assert ts is not None
    assert ts.year == 2026


def test_parse_session_marker_timestamp_invalid() -> None:
    assert _parse_session_marker_timestamp("not a marker") is None
    assert _parse_session_marker_timestamp("[SESSION EXIT at bad-date]") is None


# ─── _microcompact_on_resume ────────────────────────────────────────────────


def test_microcompact_on_resume_keeps_last_two() -> None:
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "tool", "tool_call_id": "t1", "content": "result 1"},
        {"role": "tool", "tool_call_id": "t2", "content": "result 2"},
        {"role": "tool", "tool_call_id": "t3", "content": "result 3"},
        {"role": "tool", "tool_call_id": "t4", "content": "result 4"},
        {"role": "tool", "tool_call_id": "t5", "content": "result 5"},
    ]
    result = _microcompact_on_resume(messages)
    # First 3 tool results cleared
    assert result[2]["content"] == "[Previous tool result cleared due to inactivity]"
    assert result[3]["content"] == "[Previous tool result cleared due to inactivity]"
    assert result[4]["content"] == "[Previous tool result cleared due to inactivity]"
    # Last 2 preserved
    assert result[5]["content"] == "result 4"
    assert result[6]["content"] == "result 5"


def test_microcompact_on_resume_noop_when_fewer_than_three_tools() -> None:
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "tool", "tool_call_id": "t1", "content": "result 1"},
        {"role": "tool", "tool_call_id": "t2", "content": "result 2"},
    ]
    result = _microcompact_on_resume(messages)
    assert result[1]["content"] == "result 1"
    assert result[2]["content"] == "result 2"


def test_microcompact_on_resume_noop_when_already_cleared() -> None:
    messages = [
        {
            "role": "tool",
            "tool_call_id": "t1",
            "content": "[Previous tool result cleared due to inactivity]",
        },
        {"role": "tool", "tool_call_id": "t2", "content": "result 2"},
        {"role": "tool", "tool_call_id": "t3", "content": "result 3"},
    ]
    result = _microcompact_on_resume(messages)
    # t1 already cleared, should stay unchanged
    assert result[0]["content"] == "[Previous tool result cleared due to inactivity]"
    assert result[1]["content"] == "result 2"
    assert result[2]["content"] == "result 3"


# ─── _microcompact (time-based) ─────────────────────────────────────────────


def test_microcompact_noop_when_last_assistant_time_is_none() -> None:
    messages = [
        {"role": "tool", "tool_call_id": "t1", "content": "result 1"},
        {"role": "tool", "tool_call_id": "t2", "content": "result 2"},
        {"role": "tool", "tool_call_id": "t3", "content": "result 3"},
    ]
    result, modified = _microcompact(messages, None)
    assert not modified
    assert result[0]["content"] == "result 1"


def test_microcompact_noop_when_gap_is_short(monkeypatch: pytest.MonkeyPatch) -> None:
    messages = [
        {"role": "tool", "tool_call_id": "t1", "content": "result 1"},
        {"role": "tool", "tool_call_id": "t2", "content": "result 2"},
        {"role": "tool", "tool_call_id": "t3", "content": "result 3"},
    ]
    now = 1000.0
    monkeypatch.setattr("persome.chat.handler.time.time", lambda: now)
    result, modified = _microcompact(messages, now - 60)  # 1 minute ago
    assert not modified
    assert result[0]["content"] == "result 1"


def test_microcompact_clears_old_tools_when_gap_is_long(monkeypatch: pytest.MonkeyPatch) -> None:
    messages = [
        {"role": "tool", "tool_call_id": "t1", "content": "result 1"},
        {"role": "tool", "tool_call_id": "t2", "content": "result 2"},
        {"role": "tool", "tool_call_id": "t3", "content": "result 3"},
        {"role": "tool", "tool_call_id": "t4", "content": "result 4"},
    ]
    now = 1000.0
    monkeypatch.setattr("persome.chat.handler.time.time", lambda: now)
    result, modified = _microcompact(messages, now - 400)  # > 5 minutes ago
    assert modified
    assert result[0]["content"] == "[Previous tool result cleared due to inactivity]"
    assert result[1]["content"] == "[Previous tool result cleared due to inactivity]"
    assert result[2]["content"] == "result 3"
    assert result[3]["content"] == "result 4"


# ─── _maybe_away_summary ────────────────────────────────────────────────────


def test_maybe_away_summary_empty_history() -> None:
    assert _maybe_away_summary([], _cfg_with_model()) is None


def test_maybe_away_summary_no_exit_marker() -> None:
    prev = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    assert _maybe_away_summary(prev, _cfg_with_model()) is None


def test_maybe_away_summary_gap_too_short(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_now = datetime(2026, 5, 15, 14, 30, 0)

    class _MockDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    monkeypatch.setattr("persome.chat.handler.datetime", _MockDateTime)

    prev = [
        {"role": "user", "content": "hello"},
        {
            "role": "system",
            "content": "[SESSION EXIT at 2026-05-15 14:10:00]",
            "_session_marker": True,
        },
    ]
    # Gap is 20 minutes < 30
    assert _maybe_away_summary(prev, _cfg_with_model()) is None


def test_maybe_away_summary_generates_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_now = datetime(2026, 5, 15, 15, 0, 0)

    class _MockDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    monkeypatch.setattr("persome.chat.handler.datetime", _MockDateTime)
    monkeypatch.setattr(
        "persome.chat.handler.complete_sync",
        lambda *_a, **_kw: "User was refactoring auth module.",
    )

    prev = [
        {"role": "user", "content": "Refactor the auth module"},
        {"role": "assistant", "content": "I'll help with that"},
        {
            "role": "system",
            "content": "[SESSION EXIT at 2026-05-15 14:00:00]",
            "_session_marker": True,
        },
    ]

    summary = _maybe_away_summary(prev, _cfg_with_model())

    assert summary is not None
    assert "User was refactoring auth module" in summary
    assert "Continue the conversation" in summary


def test_maybe_away_summary_marker_without_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """_session_marker=True but content is RESUME, not EXIT → None."""
    fixed_now = datetime(2026, 5, 15, 15, 0, 0)

    class _MockDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    monkeypatch.setattr("persome.chat.handler.datetime", _MockDateTime)

    prev = [
        {"role": "user", "content": "hello"},
        {
            "role": "system",
            "content": "[SESSION RESUME at 2026-05-15 14:00:00]",
            "_session_marker": True,
        },
    ]
    assert _maybe_away_summary(prev, _cfg_with_model()) is None


def test_maybe_away_summary_exit_without_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Content contains EXIT but _session_marker is missing → None."""
    fixed_now = datetime(2026, 5, 15, 15, 0, 0)

    class _MockDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    monkeypatch.setattr("persome.chat.handler.datetime", _MockDateTime)

    prev = [
        {"role": "user", "content": "hello"},
        {"role": "system", "content": "[SESSION EXIT at 2026-05-15 14:00:00]"},
    ]
    assert _maybe_away_summary(prev, _cfg_with_model()) is None


def test_maybe_away_summary_content_is_none() -> None:
    """Last message has None content → None."""
    prev = [
        {"role": "user", "content": "hello"},
        {"role": "system", "content": None, "_session_marker": True},
    ]
    assert _maybe_away_summary(prev, _cfg_with_model()) is None


def test_maybe_away_summary_llm_returns_empty_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """If _anthropic_complete returns '', _maybe_away_summary should return None."""
    fixed_now = datetime(2026, 5, 15, 15, 0, 0)

    class _MockDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    monkeypatch.setattr("persome.chat.handler.datetime", _MockDateTime)
    monkeypatch.setattr(
        "persome.chat.handler.complete_sync",
        lambda *_a, **_kw: "",
    )

    prev = [
        {"role": "user", "content": "hello"},
        {
            "role": "system",
            "content": "[SESSION EXIT at 2026-05-15 14:00:00]",
            "_session_marker": True,
        },
    ]
    assert _maybe_away_summary(prev, _cfg_with_model()) is None


def test_maybe_away_summary_llm_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """If _anthropic_complete raises, _maybe_away_summary should return None gracefully."""
    fixed_now = datetime(2026, 5, 15, 15, 0, 0)

    class _MockDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    monkeypatch.setattr("persome.chat.handler.datetime", _MockDateTime)
    monkeypatch.setattr(
        "persome.chat.handler.complete_sync",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    prev = [
        {"role": "user", "content": "hello"},
        {
            "role": "system",
            "content": "[SESSION EXIT at 2026-05-15 14:00:00]",
            "_session_marker": True,
        },
    ]
    assert _maybe_away_summary(prev, _cfg_with_model()) is None


# ─── additional microcompact boundary tests ─────────────────────────────────


def test_microcompact_exactly_at_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gap == 300s SHOULD trigger microcompact (strict <, so 300 is not < 300)."""
    messages = [
        {"role": "tool", "tool_call_id": "t1", "content": "result 1"},
        {"role": "tool", "tool_call_id": "t2", "content": "result 2"},
        {"role": "tool", "tool_call_id": "t3", "content": "result 3"},
    ]
    now = 1000.0
    monkeypatch.setattr("persome.chat.handler.time.time", lambda: now)
    result, modified = _microcompact(messages, now - 300)  # exactly 5 minutes
    # Original code uses <, so 300 is NOT < 300 → continues to compaction
    assert modified
    assert result[0]["content"] == "[Previous tool result cleared due to inactivity]"


def test_microcompact_on_resume_tool_without_content() -> None:
    """Tool result missing 'content' key should not crash."""
    messages = [
        {"role": "tool", "tool_call_id": "t1"},  # no content key
        {"role": "tool", "tool_call_id": "t2", "content": "result 2"},
        {"role": "tool", "tool_call_id": "t3", "content": "result 3"},
    ]
    result = _microcompact_on_resume(messages)
    # t1 has no content → get("content", "") returns "" → not startswith placeholder → gets cleared
    assert result[0]["content"] == "[Previous tool result cleared due to inactivity]"
    assert result[1]["content"] == "result 2"
    assert result[2]["content"] == "result 3"


def test_microcompact_keeps_last_two_not_first_two(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the last 2 are preserved, not the first 2."""
    messages = [
        {"role": "tool", "tool_call_id": "t1", "content": "first"},
        {"role": "tool", "tool_call_id": "t2", "content": "second"},
        {"role": "tool", "tool_call_id": "t3", "content": "third"},
        {"role": "tool", "tool_call_id": "t4", "content": "fourth"},
    ]
    now = 1000.0
    monkeypatch.setattr("persome.chat.handler.time.time", lambda: now)
    result, modified = _microcompact(messages, now - 400)
    assert modified
    # First 2 should be cleared, last 2 preserved
    assert result[0]["content"] == "[Previous tool result cleared due to inactivity]"
    assert result[1]["content"] == "[Previous tool result cleared due to inactivity]"
    assert result[2]["content"] == "third"
    assert result[3]["content"] == "fourth"


def test_microcompact_five_tools_distinction(monkeypatch: pytest.MonkeyPatch) -> None:
    """With 5 tools, [:-2] clears 3 and [:+2] clears 2 — distinguish them."""
    messages = [
        {"role": "tool", "tool_call_id": "t1", "content": "r1"},
        {"role": "tool", "tool_call_id": "t2", "content": "r2"},
        {"role": "tool", "tool_call_id": "t3", "content": "r3"},
        {"role": "tool", "tool_call_id": "t4", "content": "r4"},
        {"role": "tool", "tool_call_id": "t5", "content": "r5"},
    ]
    now = 1000.0
    monkeypatch.setattr("persome.chat.handler.time.time", lambda: now)
    result, modified = _microcompact(messages, now - 400)
    assert modified
    # Correct behavior: clear first 3, keep last 2
    assert result[0]["content"] == "[Previous tool result cleared due to inactivity]"
    assert result[1]["content"] == "[Previous tool result cleared due to inactivity]"
    assert result[2]["content"] == "[Previous tool result cleared due to inactivity]"
    assert result[3]["content"] == "r4"
    assert result[4]["content"] == "r5"


def test_microcompact_on_resume_exactly_two_tools() -> None:
    """Exactly 2 tools: neither should be touched."""
    messages = [
        {"role": "tool", "tool_call_id": "t1", "content": "result 1"},
        {"role": "tool", "tool_call_id": "t2", "content": "result 2"},
    ]
    result = _microcompact_on_resume(messages)
    assert result[0]["content"] == "result 1"
    assert result[1]["content"] == "result 2"


# ─── _exec_tool registry tests ──────────────────────────────────────────────


def test_exec_tool_unknown_returns_error() -> None:
    result = _exec_tool("nope", {})
    parsed = json.loads(result)
    assert "error" in parsed
    assert "unknown tool" in parsed["error"]
    assert "nope" in parsed["error"]


def test_exec_tool_read_memory_missing_path(ac_root: Path) -> None:
    result = _exec_tool("read_memory", {"path": "nonexistent.md"})
    parsed = json.loads(result)
    assert "error" in parsed
    assert "file not found" in parsed["error"]


def test_exec_tool_list_dir_filters_hidden(tmp_path: Path) -> None:
    d = tmp_path / "test_dir"
    d.mkdir()
    (d / "visible.txt").write_text("hello")
    (d / ".hidden").write_text("secret")
    result = _exec_tool("list_dir", {"path": str(d)})
    parsed = json.loads(result)
    assert parsed["path"] == str(d)
    names = {e["name"] for e in parsed["entries"]}
    assert "visible.txt" in names
    assert ".hidden" not in names


def test_exec_tool_handler_exception_wrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from persome.chat.tool_handlers import TOOL_HANDLERS

    def _boom(args: dict[str, Any]) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setitem(TOOL_HANDLERS, "search_memory", _boom)
    result = _exec_tool("search_memory", {"query": "x"})
    parsed = json.loads(result)
    assert parsed["error"] == "RuntimeError: boom"


# ─── write_file / edit_file handler tests ───────────────────────────────────


def test_tool_write_file_creates_file(tmp_path: Path) -> None:
    from persome.chat.tool_handlers import tool_write_file

    target = tmp_path / "subdir" / "test.txt"
    result = tool_write_file({"path": str(target), "content": "hello"})
    assert result["path"] == str(target)
    assert target.read_text() == "hello"


def test_tool_edit_file_replaces_unique_string(tmp_path: Path) -> None:
    from persome.chat.tool_handlers import tool_edit_file

    target = tmp_path / "test.txt"
    target.write_text("old content here")
    result = tool_edit_file(
        {"path": str(target), "old_string": "old content", "new_string": "new content"}
    )
    assert result["replaced"] is True
    assert target.read_text() == "new content here"


def test_tool_edit_file_missing_old_string(tmp_path: Path) -> None:
    from persome.chat.tool_handlers import tool_edit_file

    target = tmp_path / "test.txt"
    target.write_text("some content")
    result = tool_edit_file({"path": str(target), "old_string": "not found", "new_string": "x"})
    assert "error" in result
    assert "not found" in result["error"]


def test_tool_edit_file_duplicate_old_string(tmp_path: Path) -> None:
    from persome.chat.tool_handlers import tool_edit_file

    target = tmp_path / "test.txt"
    target.write_text("abc abc")
    result = tool_edit_file({"path": str(target), "old_string": "abc", "new_string": "x"})
    assert "error" in result
    assert "2 times" in result["error"]


# ─── _exec_tool serialization fallback (ensure_ascii=False, default=str) ────
#
# These tests inject a synthetic handler into TOOL_HANDLERS for the duration
# of a single test. They exist to lock in two silent fallbacks in _exec_tool:
#   - ensure_ascii=False  → keep CJK / emoji legible in LLM context
#   - default=str         → stringify datetime, Path, UUID, dataclasses, ...
# If a future maintainer flips either knob, these tests turn red.


def test_exec_tool_preserves_non_ascii(monkeypatch: pytest.MonkeyPatch) -> None:
    from persome.chat.tool_handlers import TOOL_HANDLERS

    def fake_handler(_args: dict[str, Any]) -> dict[str, Any]:
        return {"msg": "你好 🌍"}

    monkeypatch.setitem(TOOL_HANDLERS, "_test_non_ascii", fake_handler)
    out = _exec_tool("_test_non_ascii", {})
    # If ensure_ascii flipped to True we would see escape sequences instead.
    assert "你好" in out
    assert "🌍" in out
    assert "\\u" not in out
    # And it must still be valid JSON.
    assert json.loads(out) == {"msg": "你好 🌍"}


def test_exec_tool_serializes_datetime_via_default_str(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from persome.chat.tool_handlers import TOOL_HANDLERS

    ts = datetime(2026, 5, 19, 12, 0, 0)

    def fake_handler(_args: dict[str, Any]) -> dict[str, Any]:
        return {"when": ts}

    monkeypatch.setitem(TOOL_HANDLERS, "_test_datetime", fake_handler)
    out = _exec_tool("_test_datetime", {})
    # Without default=str this raises TypeError and the handler would
    # surface as {"error": "TypeError: ..."} — assert that did NOT happen.
    parsed = json.loads(out)
    assert "error" not in parsed
    assert parsed["when"] == str(ts)


def test_exec_tool_serializes_path_via_default_str(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from persome.chat.tool_handlers import TOOL_HANDLERS

    p = Path("/tmp/persome/x.md")

    def fake_handler(_args: dict[str, Any]) -> dict[str, Any]:
        return {"file": p}

    monkeypatch.setitem(TOOL_HANDLERS, "_test_path", fake_handler)
    out = _exec_tool("_test_path", {})
    parsed = json.loads(out)
    assert "error" not in parsed
    assert parsed["file"] == str(p)


def test_exec_tool_default_str_rescues_arbitrary_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from persome.chat.tool_handlers import TOOL_HANDLERS

    class Opaque:
        def __str__(self) -> str:
            return "opaque-token"

    def fake_handler(_args: dict[str, Any]) -> dict[str, Any]:
        return {"obj": Opaque()}

    monkeypatch.setitem(TOOL_HANDLERS, "_test_opaque", fake_handler)
    out = _exec_tool("_test_opaque", {})
    parsed = json.loads(out)
    assert "error" not in parsed
    assert parsed["obj"] == "opaque-token"

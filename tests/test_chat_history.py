"""Unit tests for on-disk chat history (``persome.chat.history``).

Pure filesystem/JSON logic — no LLM, no network. Covers the happy path for
each public helper plus the error/corruption paths (unreadable files, malformed
JSON, missing files, OSError on write).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from persome.chat import history as chat_history

# ---------------------------------------------------------------------------
# save_history / load_history
# ---------------------------------------------------------------------------


def test_save_then_load_round_trips_non_system_messages(ac_root: Path) -> None:
    """Happy path: saved user/assistant turns load back; system prompt dropped."""
    messages = [
        {"role": "system", "content": "you are a helpful assistant"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    chat_history.save_history(messages)

    loaded = chat_history.load_history()
    assert loaded == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]


def test_load_history_returns_empty_when_no_active_file(ac_root: Path) -> None:
    """Happy path: fresh root has no active session → empty list, no error."""
    assert chat_history.load_history() == []


def test_load_history_returns_empty_on_malformed_json(ac_root: Path) -> None:
    """Error path: corrupt active.json is swallowed and returns []."""
    chat_history.active_path().write_text("{not valid json")
    assert chat_history.load_history() == []


def test_load_history_returns_empty_when_top_level_not_a_list(ac_root: Path) -> None:
    """Error path: valid JSON but wrong shape (object, not list) → []."""
    chat_history.active_path().write_text(json.dumps({"role": "user"}))
    assert chat_history.load_history() == []


def test_save_history_swallows_oserror(ac_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Error path: a write failure must not propagate to the caller."""

    def boom(_self: Path, _text: str, *_args: object, **_kwargs: object) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", boom)
    # Should not raise.
    chat_history.save_history([{"role": "user", "content": "x"}])


# ---------------------------------------------------------------------------
# archive_current
# ---------------------------------------------------------------------------


def test_archive_current_renames_active_and_returns_session_id(ac_root: Path) -> None:
    """Happy path: a non-empty active session is renamed to a timestamped file."""
    chat_history.save_history([{"role": "user", "content": "remember this"}])

    session_id = chat_history.archive_current()

    assert session_id is not None
    assert not chat_history.active_path().exists()
    archived = chat_history.archive_path(session_id)
    assert archived.exists()
    assert json.loads(archived.read_text()) == [{"role": "user", "content": "remember this"}]


def test_archive_current_returns_none_when_nothing_active(ac_root: Path) -> None:
    """Happy path: archiving with no active file is a no-op returning None."""
    assert chat_history.archive_current() is None


def test_archive_current_deletes_empty_session(ac_root: Path) -> None:
    """Error path: an empty list is discarded (unlinked) rather than archived."""
    chat_history.active_path().write_text(json.dumps([]))

    assert chat_history.archive_current() is None
    assert not chat_history.active_path().exists()


def test_archive_current_discards_corrupt_active_file(ac_root: Path) -> None:
    """Error path: corrupt active.json is unlinked and returns None."""
    chat_history.active_path().write_text("@@ broken @@")

    assert chat_history.archive_current() is None
    assert not chat_history.active_path().exists()


# ---------------------------------------------------------------------------
# search_chat_history
# ---------------------------------------------------------------------------


def test_search_chat_history_finds_match_case_insensitively(ac_root: Path) -> None:
    """Happy path: substring match across sessions, tool rows skipped."""
    chat_history.archive_path("20260101-000000").write_text(
        json.dumps(
            [
                {"role": "user", "content": "Tell me about Postgres replication"},
                {"role": "tool", "content": "postgres tool output should be skipped"},
                {"role": "assistant", "content": "Sure, here is the answer"},
            ]
        )
    )

    results = chat_history.search_chat_history("POSTGRES")

    assert len(results) == 1
    assert results[0]["role"] == "user"
    assert results[0]["session"] == "20260101-000000"
    assert "Postgres" in results[0]["content"]


def test_search_chat_history_respects_limit(ac_root: Path) -> None:
    """Happy path: results are capped at the requested limit."""
    chat_history.archive_path("20260101-000001").write_text(
        json.dumps([{"role": "user", "content": f"match {i}"} for i in range(10)])
    )

    results = chat_history.search_chat_history("match", limit=3)

    assert len(results) == 3


def test_search_chat_history_skips_unreadable_session(ac_root: Path) -> None:
    """Error path: a corrupt session file is skipped, valid ones still searched."""
    chat_history.archive_path("20260101-000002").write_text("<<garbage>>")
    chat_history.archive_path("20260101-000003").write_text(
        json.dumps([{"role": "user", "content": "valid match"}])
    )

    results = chat_history.search_chat_history("match")

    assert len(results) == 1
    assert results[0]["content"] == "valid match"


# ---------------------------------------------------------------------------
# list_chat_sessions
# ---------------------------------------------------------------------------


def test_list_chat_sessions_summarizes_each_session(ac_root: Path) -> None:
    """Happy path: each session reports stem, user-turn count, first message."""
    chat_history.archive_path("20260101-000004").write_text(
        json.dumps(
            [
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": "answer"},
                {"role": "user", "content": "second question"},
            ]
        )
    )

    sessions = chat_history.list_chat_sessions()

    assert len(sessions) == 1
    assert sessions[0]["session"] == "20260101-000004"
    assert sessions[0]["turns"] == 2
    assert sessions[0]["first_message"] == "first question"


def test_list_chat_sessions_skips_corrupt_files(ac_root: Path) -> None:
    """Error path: malformed session files are ignored, not raised."""
    chat_history.archive_path("20260101-000005").write_text("not json at all")
    chat_history.archive_path("20260101-000006").write_text(
        json.dumps([{"role": "user", "content": "ok"}])
    )

    sessions = chat_history.list_chat_sessions()

    assert len(sessions) == 1
    assert sessions[0]["session"] == "20260101-000006"

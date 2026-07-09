"""Unit + integration tests for chat session title generation.

The sidebar in Mens.app prefers ``SessionInfo.title`` (LLM-generated)
over ``preview`` (first-user-message truncation). These tests pin:

* ``generate_title`` happy path + degenerate inputs.
* Title is persisted in the session JSON across reload.
* Title is exposed on ``GET /chat/sessions`` / ``GET /chat/sessions/{id}``.

Tests stub :func:`persome.writer.chat_title.complete_sync` directly
rather than rely on ``PERSOME_LLM_MOCK`` — title generation runs
through the Anthropic SDK path (``chat.agent.complete_sync``), not litellm.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from persome.api import build_api_app
from persome.api.chat_routes import (
    ChatSession,
    _history_dir,
    _load_session,
    _save_session,
    _sessions,
    _sessions_lock,
)
from persome.api.chat_routes import (
    set_config as set_chat_config,
)
from persome.api.routes import set_config as set_route_config
from persome.config import Config
from persome.writer import chat_title as chat_title_mod
from persome.writer.chat_title import (
    TITLE_MAX_CHARS,
    _clean,
    _first_user_and_assistant,
    generate_title,
)


@pytest.fixture
def cfg(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Config:
    monkeypatch.setenv("PERSOME_ROOT", str(tmp_path))
    return Config()


@pytest.fixture
def stub_llm(monkeypatch: pytest.MonkeyPatch):
    """Replace the Anthropic SDK call with a configurable stub.

    Returns a control handle with ``set_response(text)``, ``raise_on_next(exc)``,
    and ``calls`` (the captured prompt list) for assertions.
    """
    calls: list[dict] = []
    response_text = {"value": "Test chat title"}

    def fake_complete_sync(chat_cfg, messages, *, max_tokens=2048):
        calls.append({"messages": messages, "max_tokens": max_tokens})
        return response_text["value"]

    monkeypatch.setattr(chat_title_mod, "complete_sync", fake_complete_sync)

    class StubControl:
        def set_response(self, text: str) -> None:
            response_text["value"] = text

        def raise_on_next(self, exc: Exception) -> None:
            def boom(*_a, **_k):
                raise exc

            monkeypatch.setattr(chat_title_mod, "complete_sync", boom)

        @property
        def calls(self):
            return calls

    return StubControl()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path) -> TestClient:
    monkeypatch.setenv("PERSOME_ROOT", str(tmp_path))
    with _sessions_lock:
        _sessions.clear()
    cfg = Config()
    set_route_config(cfg)
    set_chat_config(cfg)
    return TestClient(build_api_app(cfg))


# ─── unit: _first_user_and_assistant ───────────────────────────────────────


def test_extracts_first_user_and_assistant_text() -> None:
    msgs = [
        {"role": "system", "content": "ignore"},
        {"role": "user", "content": "How do I deploy?"},
        {"role": "assistant", "content": "Run `make deploy`."},
        {"role": "user", "content": "Thanks"},
    ]
    user, asst = _first_user_and_assistant(msgs)
    assert user == "How do I deploy?"
    assert asst == "Run `make deploy`."


def test_extracts_text_from_block_content() -> None:
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "hello "},
                {"type": "text", "text": "world"},
            ],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "private reasoning"},
                {"type": "text", "text": "reply text"},
            ],
        },
    ]
    user, asst = _first_user_and_assistant(msgs)
    assert user == "hello world"
    assert asst == "reply text"  # thinking is dropped


def test_skips_tool_result_user_turns() -> None:
    msgs = [
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}],
        },
        {"role": "user", "content": "real question"},
    ]
    user, _ = _first_user_and_assistant(msgs)
    assert user == "real question"


# ─── unit: _clean ──────────────────────────────────────────────────────────


def test_clean_strips_quotes_and_collapses_whitespace() -> None:
    assert _clean('  "Deploy guide"  ') == "Deploy guide"
    assert _clean("「中文标题」") == "中文标题"
    assert _clean("line1\nline2") == "line1 line2"


def test_clean_truncates_to_max() -> None:
    out = _clean("x" * (TITLE_MAX_CHARS + 10))
    assert out.endswith("…")
    assert len(out) == TITLE_MAX_CHARS + 1


# ─── unit: generate_title with stubbed Anthropic SDK ───────────────────────


def test_generate_title_happy_path(cfg: Config, stub_llm) -> None:
    msgs = [
        {"role": "user", "content": "How do I deploy?"},
        {"role": "assistant", "content": "Use make deploy."},
    ]
    stub_llm.set_response("Deploy guide")
    assert generate_title(cfg, msgs) == "Deploy guide"
    assert len(stub_llm.calls) == 1
    # Prompt must include both sides so the model can summarize the exchange.
    sent = stub_llm.calls[0]["messages"][0]["content"]
    assert "How do I deploy?" in sent
    assert "Use make deploy." in sent


def test_generate_title_returns_none_without_user_message(cfg: Config, stub_llm) -> None:
    assert generate_title(cfg, [{"role": "assistant", "content": "hi"}]) is None
    assert generate_title(cfg, []) is None
    # No LLM call wasted on empty input.
    assert stub_llm.calls == []


def test_generate_title_strips_quotes_from_llm_output(cfg: Config, stub_llm) -> None:
    stub_llm.set_response('  "Custom Title"  ')
    msgs = [{"role": "user", "content": "anything"}]
    assert generate_title(cfg, msgs) == "Custom Title"


def test_generate_title_returns_none_on_llm_error(cfg: Config, stub_llm) -> None:
    stub_llm.raise_on_next(RuntimeError("network down"))
    msgs = [{"role": "user", "content": "anything"}]
    assert generate_title(cfg, msgs) is None


# ─── integration: persistence round-trip ───────────────────────────────────


def test_save_load_round_trips_title(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("PERSOME_ROOT", str(tmp_path))
    session = ChatSession(
        id="abcd1234",
        created_at="2026-05-27T10:00:00+08:00",
        updated_at="2026-05-27T10:00:00+08:00",
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ],
        title="My Chat",
    )
    _save_session(session)
    loaded = _load_session("abcd1234")
    assert loaded is not None
    assert loaded.title == "My Chat"


def test_load_treats_blank_or_missing_title_as_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PERSOME_ROOT", str(tmp_path))
    (_history_dir() / "api-legacy01.json").write_text(
        json.dumps(
            {
                "created_at": "2026-05-25T10:00:00+08:00",
                "updated_at": "2026-05-25T10:05:00+08:00",
                "messages": [{"role": "user", "content": "x"}],
            }
        )
    )
    loaded = _load_session("legacy01")
    assert loaded is not None
    assert loaded.title is None

    (_history_dir() / "api-blank001.json").write_text(
        json.dumps(
            {
                "created_at": "2026-05-25T10:00:00+08:00",
                "updated_at": "2026-05-25T10:05:00+08:00",
                "messages": [{"role": "user", "content": "x"}],
                "title": "   ",
            }
        )
    )
    loaded = _load_session("blank001")
    assert loaded is not None
    assert loaded.title is None


# ─── integration: title surfaces on list/detail endpoints ─────────────────


def test_list_sessions_returns_title_from_disk(client: TestClient) -> None:
    sid = "tit12345"
    (_history_dir() / f"api-{sid}.json").write_text(
        json.dumps(
            {
                "created_at": "2026-05-25T10:00:00+08:00",
                "updated_at": "2026-05-25T10:05:00+08:00",
                "messages": [
                    {"role": "user", "content": "anything"},
                    {"role": "assistant", "content": "..."},
                ],
                "title": "Deploy guide",
            }
        )
    )
    sessions = client.get("/chat/sessions").json()["data"]["sessions"]
    match = next(s for s in sessions if s["id"] == sid)
    assert match["title"] == "Deploy guide"
    assert match["preview"] == "anything"


def test_get_session_returns_title_from_memory(client: TestClient) -> None:
    created = client.post("/chat/sessions").json()
    sid = created["data"]["session"]["id"]
    assert created["data"]["session"]["title"] is None

    with _sessions_lock:
        _sessions[sid].title = "Cached title"
        _sessions[sid].messages.append({"role": "user", "content": "q"})

    detail = client.get(f"/chat/sessions/{sid}").json()
    assert detail["data"]["session"]["title"] == "Cached title"

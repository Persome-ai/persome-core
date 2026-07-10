"""Covers the sidebar-preview contract on ``GET /chat/sessions``.

Trusted local clients can show ``preview`` (first user message, ≤80 chars)
instead of a raw timestamp. These tests pin:

* ``_first_user_preview`` normalization (Anthropic block content,
  whitespace, truncation, empty cases).
* The list endpoint returns ``preview`` for both the active-in-memory
  branch and the on-disk branch.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from persome.api import build_api_app
from persome.api.chat_routes import (
    _PREVIEW_MAX_CHARS,
    _first_user_preview,
    _history_dir,
    _sessions,
    _sessions_lock,
)
from persome.api.chat_routes import (
    set_config as set_chat_config,
)
from persome.api.routes import set_config as set_route_config
from persome.config import Config


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path) -> TestClient:
    monkeypatch.setenv("PERSOME_ROOT", str(tmp_path))
    # The in-memory session map is module-global; isolate per test.
    with _sessions_lock:
        _sessions.clear()
    cfg = Config()
    set_route_config(cfg)
    set_chat_config(cfg)
    return TestClient(build_api_app(cfg))


# ─── _first_user_preview unit ──────────────────────────────────────────────


def test_preview_picks_first_user_message_and_ignores_system() -> None:
    msgs = [
        {"role": "system", "content": "system prompt..."},
        {"role": "user", "content": "Hello there"},
        {"role": "assistant", "content": "Hi"},
        {"role": "user", "content": "second"},
    ]
    assert _first_user_preview(msgs) == "Hello there"


def test_preview_collapses_whitespace() -> None:
    msgs = [{"role": "user", "content": "line one\n\n  line   two"}]
    assert _first_user_preview(msgs) == "line one line two"


def test_preview_truncates_with_ellipsis() -> None:
    long = "x" * (_PREVIEW_MAX_CHARS + 50)
    out = _first_user_preview([{"role": "user", "content": long}])
    assert out is not None
    assert out.endswith("…")
    assert len(out) == _PREVIEW_MAX_CHARS + 1  # +1 for the ellipsis


def test_preview_handles_anthropic_block_content() -> None:
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "block-text "},
                {"type": "tool_use", "name": "x", "input": {}},
                {"type": "text", "text": "tail"},
            ],
        }
    ]
    assert _first_user_preview(msgs) == "block-text tail"


def test_preview_returns_none_when_no_user_message() -> None:
    assert _first_user_preview([{"role": "assistant", "content": "hi"}]) is None
    assert _first_user_preview([]) is None


def test_preview_returns_none_for_empty_user_content() -> None:
    msgs = [
        {"role": "user", "content": "   "},
        {"role": "user", "content": "real content"},
    ]
    assert _first_user_preview(msgs) == "real content"


# ─── List endpoint integration ─────────────────────────────────────────────


def test_list_sessions_returns_preview_from_disk(client: TestClient) -> None:
    sid = "abcd1234"
    (_history_dir() / f"api-{sid}.json").write_text(
        json.dumps(
            {
                "created_at": "2026-05-25T10:00:00+08:00",
                "updated_at": "2026-05-25T10:05:00+08:00",
                "messages": [
                    {"role": "user", "content": "what's on my plate today?"},
                    {"role": "assistant", "content": "..."},
                ],
            }
        )
    )

    resp = client.get("/chat/sessions").json()
    sessions = resp["data"]["sessions"]
    match = next(s for s in sessions if s["id"] == sid)
    assert match["preview"] == "what's on my plate today?"
    assert match["turn_count"] == 1


def test_list_sessions_preview_null_when_empty(client: TestClient) -> None:
    created = client.post("/chat/sessions").json()
    sid = created["data"]["session"]["id"]
    # Freshly created session has only a system prompt — no user turn yet.
    sessions = client.get("/chat/sessions").json()["data"]["sessions"]
    match = next(s for s in sessions if s["id"] == sid)
    assert match["preview"] is None

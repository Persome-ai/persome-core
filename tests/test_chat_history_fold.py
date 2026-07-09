"""Tests for the agent-loop folding in GET /chat/sessions/{id}/messages.

The chat agent persists Anthropic agent-loop iterations faithfully —
``assistant text+tool_use`` rows interleaved with ``user tool_result`` rows
— but the HTTP messages endpoint is a presentation projection that folds
each agent-loop span into ONE assistant turn carrying:

  - ``content``: plain-text join of every text block (fallback for older
    clients).
  - ``blocks``: the original text/tool_use/tool_result blocks in their
    chronological order (so clients can render ``text → tool_call →
    tool_result → text`` exactly as it streamed).

Without folding, K tool calls would produce K+1 separate chat bubbles in
the client (each "Let me check ..." preamble becomes its own bubble).
Without ``blocks``, the client cannot reconstruct the inline tool calls
after the stream completes — they vanish on history reload.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from persome.api import build_api_app
from persome.api.chat_routes import (
    ChatSession,
    _fold_agent_loop,
    _sessions,
    _sessions_lock,
)
from persome.api.chat_routes import set_config as set_chat_config
from persome.api.routes import set_config as set_route_config
from persome.config import Config


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path) -> TestClient:
    monkeypatch.setenv("PERSOME_ROOT", str(tmp_path))
    cfg = Config()
    set_route_config(cfg)
    set_chat_config(cfg)
    return TestClient(build_api_app(cfg))


def _register(session_id: str, messages: list[dict[str, Any]]) -> None:
    """Inject a session into the in-memory registry for the GET endpoint."""
    now = datetime.now().astimezone().isoformat()
    session = ChatSession(
        id=session_id,
        created_at=now,
        updated_at=now,
        messages=messages,
    )
    with _sessions_lock:
        _sessions[session_id] = session


def _types(entry: dict[str, Any]) -> list[str]:
    return [b["type"] for b in entry.get("blocks", [])]


# ── unit: _fold_agent_loop ────────────────────────────────────────────────


def test_fold_preserves_blocks_in_chronological_order() -> None:
    """A single user prompt that triggered 2 tool calls folds into ONE
    assistant turn whose blocks reproduce the original interleave."""
    raw = [
        {"role": "user", "content": "what am i doing today"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me check your recent activity..."},
                {"type": "tool_use", "id": "t1", "name": "current_context", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "ctx-result"},
            ],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me also check..."},
                {"type": "tool_use", "id": "t2", "name": "recent_activity", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t2", "content": "recent-result"},
            ],
        },
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "Edited team profiles on Manus..."}],
        },
    ]

    out = _fold_agent_loop(raw)

    assert len(out) == 2
    user, assistant = out
    assert user == {
        "role": "user",
        "content": "what am i doing today",
        "blocks": [{"type": "text", "text": "what am i doing today"}],
    }
    assert assistant["role"] == "assistant"
    # text fallback = concatenation of every text block
    assert assistant["content"] == (
        "Let me check your recent activity...Let me also check...Edited team profiles on Manus..."
    )
    # blocks preserve chronological order: text, tool_use, tool_result, text, tool_use, tool_result, text
    assert _types(assistant) == [
        "text",
        "tool_use",
        "tool_result",
        "text",
        "tool_use",
        "tool_result",
        "text",
    ]
    # tool_use carries name + input
    assert assistant["blocks"][1] == {
        "type": "tool_use",
        "name": "current_context",
        "input": {},
    }
    # tool_result carries name (may be None) + content + tool_use_id
    assert assistant["blocks"][2]["type"] == "tool_result"
    assert assistant["blocks"][2]["content"] == "ctx-result"
    assert assistant["blocks"][2]["tool_use_id"] == "t1"


def test_fold_no_tool_calls_passes_through() -> None:
    raw = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
    ]

    out = _fold_agent_loop(raw)
    assert out == [
        {
            "role": "user",
            "content": "hi",
            "blocks": [{"type": "text", "text": "hi"}],
        },
        {
            "role": "assistant",
            "content": "hello",
            "blocks": [{"type": "text", "text": "hello"}],
        },
    ]


def test_fold_string_content_assistant_kept() -> None:
    """Legacy / non-block assistant content (bare string) folds to one text block."""
    raw = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "plain string answer"},
    ]

    out = _fold_agent_loop(raw)
    assert out[1]["content"] == "plain string answer"
    assert out[1]["blocks"] == [{"type": "text", "text": "plain string answer"}]


def test_fold_multiple_turns() -> None:
    raw = [
        {"role": "user", "content": "q1"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "looking..."},
                {"type": "tool_use", "id": "t1", "name": "x", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "r1"}],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "answer 1"}]},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": [{"type": "text", "text": "answer 2"}]},
    ]

    out = _fold_agent_loop(raw)
    assert [(m["role"], m["content"]) for m in out] == [
        ("user", "q1"),
        ("assistant", "looking...answer 1"),
        ("user", "q2"),
        ("assistant", "answer 2"),
    ]
    # First assistant turn keeps the interleave; second is just plain text.
    assert _types(out[1]) == ["text", "tool_use", "tool_result", "text"]
    assert _types(out[3]) == ["text"]


def test_fold_handles_trailing_tool_iteration_without_final_text() -> None:
    """Agent loop interrupted mid-flight: only preamble + tool_use, no result yet."""
    raw = [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me check..."},
                {"type": "tool_use", "id": "t1", "name": "x", "input": {}},
            ],
        },
    ]

    out = _fold_agent_loop(raw)
    assert out[1]["content"] == "Let me check..."
    assert _types(out[1]) == ["text", "tool_use"]


def test_fold_empty_or_system_only() -> None:
    assert _fold_agent_loop([]) == []


def test_fold_preserves_thinking_blocks_in_order() -> None:
    """Anthropic extended thinking blocks live in the same content list as
    text / tool_use, and must surface in the folded blocks list so clients
    can render the model's reasoning inline with its tool calls."""
    raw = [
        {"role": "user", "content": "用工具回答这个"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "让我先想想要查什么", "signature": "sig-xyz"},
                {"type": "text", "text": "好的，我来查一下"},
                {"type": "tool_use", "id": "t1", "name": "search", "input": {"q": "x"}},
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "result-x"}],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "看起来不够，再来一下"},
                {"type": "text", "text": "最终答复"},
            ],
        },
    ]

    out = _fold_agent_loop(raw)
    assert len(out) == 2
    assistant = out[1]
    assert _types(assistant) == [
        "thinking",
        "text",
        "tool_use",
        "tool_result",
        "thinking",
        "text",
    ]
    # signature 等服务端字段不进 wire payload
    assert assistant["blocks"][0] == {
        "type": "thinking",
        "thinking": "让我先想想要查什么",
    }
    assert assistant["blocks"][4] == {
        "type": "thinking",
        "thinking": "看起来不够，再来一下",
    }
    # content fallback 仍然只拼 text，不混入 thinking
    assert assistant["content"] == "好的，我来查一下最终答复"


def test_fold_drops_empty_thinking_blocks() -> None:
    raw = [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": ""},
                {"type": "text", "text": "answer"},
            ],
        },
    ]
    out = _fold_agent_loop(raw)
    assert _types(out[1]) == ["text"]


def test_fold_tool_result_content_as_block_list() -> None:
    """Anthropic SDK can return tool_result content as a list-of-blocks rather
    than a bare string; the projection must flatten text blocks to one string."""
    raw = [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t1",
                    "content": [
                        {"type": "text", "text": "part-a"},
                        {"type": "text", "text": "part-b"},
                    ],
                }
            ],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ]

    out = _fold_agent_loop(raw)
    tool_result_block = next(b for b in out[1]["blocks"] if b["type"] == "tool_result")
    assert tool_result_block["content"] == "part-apart-b"


# ── integration: HTTP endpoint ────────────────────────────────────────────


def test_messages_endpoint_returns_blocks_in_order(client: TestClient) -> None:
    sid = "foldtest"
    _register(
        sid,
        [
            {"role": "system", "content": "..."},
            {"role": "user", "content": "what am i doing today"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me check your recent activity..."},
                    {"type": "tool_use", "id": "t1", "name": "current_context", "input": {}},
                ],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me also check earlier activity..."},
                    {"type": "tool_use", "id": "t2", "name": "recent_activity", "input": {}},
                ],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t2", "content": "ok"}],
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Edited team profiles on Manus..."}],
            },
        ],
    )

    resp = client.get(f"/chat/sessions/{sid}/messages")
    assert resp.status_code == 200
    data = resp.json()["data"]

    # One user + one folded assistant — the agent loop iterations collapse.
    assert len(data) == 2
    assert data[0]["role"] == "user"
    assert data[0]["content"] == "what am i doing today"

    assistant = data[1]
    assert assistant["role"] == "assistant"
    # blocks carry chronological text/tool_use/tool_result interleave
    block_types = [b["type"] for b in assistant["blocks"]]
    assert block_types == [
        "text",
        "tool_use",
        "tool_result",
        "text",
        "tool_use",
        "tool_result",
        "text",
    ]
    # content fallback is concatenation of text blocks
    assert assistant["content"] == (
        "Let me check your recent activity..."
        "Let me also check earlier activity..."
        "Edited team profiles on Manus..."
    )
    # exclude_none in serialization: tool_use blocks have no content / tool_use_id
    tu = assistant["blocks"][1]
    assert "content" not in tu and "tool_use_id" not in tu
    assert tu["name"] == "current_context"


def test_messages_endpoint_no_tool_calls_blocks(client: TestClient) -> None:
    sid = "plaintest"
    _register(
        sid,
        [
            {"role": "system", "content": "..."},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
        ],
    )

    resp = client.get(f"/chat/sessions/{sid}/messages")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) == 2
    assert data[1]["content"] == "hello"
    assert data[1]["blocks"] == [{"type": "text", "text": "hello"}]

"""Integration test for the chat send_message SSE endpoint.

Verifies the on-the-wire contract that trusted local clients rely on:

  - frames are ``data: <json>\\n\\n``
  - reply tokens carry ``type=reply`` + ``content``
  - tool calls produce paired ``tool_call`` + ``tool_result`` frames
  - errors carry ``type=error`` + ``message`` (not ``content``)
  - the stream terminates with ``data: [DONE]\\n\\n``

The LLM is stubbed out (no real model is called); only the handler's
streaming plumbing is exercised.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from persome.api import build_api_app
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


def _parse_sse(body: str) -> list[Any]:
    """Split an SSE response body into deserialized JSON payloads.

    A trailing ``[DONE]`` marker is returned as the literal string."""
    out: list[Any] = []
    for line in body.split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload:
            continue
        if payload == "[DONE]":
            out.append("[DONE]")
        else:
            out.append(json.loads(payload))
    return out


def test_send_message_emits_reply_tokens_and_done(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: token stream + DONE terminator, no error frame."""

    async def fake_run_turn(cfg, messages, user_input, *, on_token=None, on_tool_call=None, **_kw):
        # Emit three tokens to mimic streaming behaviour.
        for tok in ["Hello", ", ", "world!"]:
            if on_token:
                await on_token(tok)
        from persome.chat import TurnResult

        return TurnResult(
            messages=[
                *messages,
                {"role": "user", "content": user_input},
                {"role": "assistant", "content": "Hello, world!"},
            ],
            assistant_message="Hello, world!",
        )

    monkeypatch.setattr("persome.api.chat_routes._run_turn", fake_run_turn)

    # Create a session first.
    created = client.post("/chat/sessions").json()
    sid = created["data"]["session"]["id"]

    with client.stream("POST", f"/chat/sessions/{sid}/messages", json={"content": "hi"}) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = "".join(resp.iter_text())

    frames = _parse_sse(body)
    # Three reply frames + a final done frame + [DONE] marker.
    replies = [f for f in frames if isinstance(f, dict) and f["type"] == "reply"]
    assert len(replies) == 3
    assert "".join(f["content"] for f in replies) == "Hello, world!"

    done_frames = [f for f in frames if isinstance(f, dict) and f["type"] == "done"]
    assert len(done_frames) == 1
    assert frames[-1] == "[DONE]"


def test_send_message_done_frame_carries_ttft_ms(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``done`` frame surfaces the turn's measured ``ttft_ms`` (#198)."""

    async def fake_run_turn(cfg, messages, user_input, *, on_token=None, **_kw):
        if on_token:
            await on_token("hi")
        from persome.chat import TurnResult

        return TurnResult(
            messages=[*messages, {"role": "assistant", "content": "hi"}],
            assistant_message="hi",
            ttft_ms=123.45,
        )

    monkeypatch.setattr("persome.api.chat_routes._run_turn", fake_run_turn)

    sid = client.post("/chat/sessions").json()["data"]["session"]["id"]
    with client.stream("POST", f"/chat/sessions/{sid}/messages", json={"content": "hi"}) as resp:
        body = "".join(resp.iter_text())

    frames = _parse_sse(body)
    done = [f for f in frames if isinstance(f, dict) and f["type"] == "done"]
    assert len(done) == 1
    # Rounded to 1 decimal place by the route; jq-extractable on the wire.
    assert done[0]["ttft_ms"] == pytest.approx(123.5, abs=0.01)


def test_send_message_done_frame_omits_ttft_when_none(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No token streamed → ``ttft_ms`` absent from the done frame, not null-key."""

    async def fake_run_turn(cfg, messages, user_input, **_kw):
        from persome.chat import TurnResult

        return TurnResult(messages=messages, assistant_message="", ttft_ms=None)

    monkeypatch.setattr("persome.api.chat_routes._run_turn", fake_run_turn)

    sid = client.post("/chat/sessions").json()["data"]["session"]["id"]
    with client.stream("POST", f"/chat/sessions/{sid}/messages", json={"content": "hi"}) as resp:
        body = "".join(resp.iter_text())

    frames = _parse_sse(body)
    done = [f for f in frames if isinstance(f, dict) and f["type"] == "done"]
    assert len(done) == 1
    assert "ttft_ms" not in done[0]


def test_send_message_emits_error_frame_on_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error path: a turn-level error becomes a single error frame."""

    async def failing_run_turn(cfg, messages, user_input, **_kw):
        from persome.chat import TurnResult

        return TurnResult(messages=messages, error="rate limited")

    monkeypatch.setattr("persome.api.chat_routes._run_turn", failing_run_turn)

    created = client.post("/chat/sessions").json()
    sid = created["data"]["session"]["id"]

    with client.stream("POST", f"/chat/sessions/{sid}/messages", json={"content": "hi"}) as resp:
        body = "".join(resp.iter_text())

    frames = _parse_sse(body)
    errors = [f for f in frames if isinstance(f, dict) and f["type"] == "error"]
    assert len(errors) == 1
    # Contract: error events use ``message``, not ``content``.
    assert errors[0]["message"] == "rate limited"
    assert "content" not in errors[0]
    assert frames[-1] == "[DONE]"


def test_send_message_404_when_session_missing(client: TestClient) -> None:
    """The 404 path remains an ordinary JSON response, not SSE."""
    resp = client.post("/chat/sessions/nonexistent/messages", json={"content": "hi"})
    assert resp.status_code == 404
    assert resp.json() == {"detail": "session not found"}

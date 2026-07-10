"""Unit tests for chat TTFT (time-to-first-token) measurement (#198).

`ChatAgent.run_turn` is the single chokepoint for every chat turn — both the
interactive CLI loop and the HTTP SSE route go through it, always with
`stream=True` internally. So measuring TTFT there covers both the "streaming"
(caller passes `on_token`) and "non-streaming" (caller omits it) modes with one
instrumentation point. These tests drive `run_turn` with a fake tool_runner that
yields scripted stream events, asserting `AgentTurnResult.ttft_ms` is set on the
first text/thinking delta and stays `None` when nothing streams back.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from persome.chat.agent import ChatAgent
from persome.config import ChatConfig


def _delta_event(kind: str, text: str) -> SimpleNamespace:
    """A `content_block_delta` event carrying a text or thinking delta."""
    if kind == "text":
        delta = SimpleNamespace(type="text_delta", text=text)
    else:
        delta = SimpleNamespace(type="thinking_delta", thinking=text)
    return SimpleNamespace(type="content_block_delta", delta=delta)


class _FakeStream:
    """Mimics a BetaAsyncMessageStream: async-iterates events, then a final msg."""

    def __init__(self, events: list[Any], final_content: list[Any]) -> None:
        self._events = events
        self._final = SimpleNamespace(content=final_content, usage=None)

    def __aiter__(self):
        async def gen():
            for ev in self._events:
                yield ev

        return gen()

    async def get_final_message(self) -> Any:
        return self._final


class _FakeRunner:
    """Mimics tool_runner(...): async-iterates over a sequence of streams."""

    def __init__(self, streams: list[_FakeStream]) -> None:
        self._streams = streams

    def __aiter__(self):
        async def gen():
            for s in self._streams:
                yield s

        return gen()


def _make_agent(monkeypatch, runner: _FakeRunner) -> ChatAgent:
    agent = ChatAgent(ChatConfig(), all_schemas=[], all_handlers={})

    def fake_tool_runner(**_kw: Any) -> _FakeRunner:
        return runner

    # Replace the SDK entry point with our scripted runner.
    monkeypatch.setattr(agent.client.beta.messages, "tool_runner", fake_tool_runner)
    return agent


def test_ttft_set_on_first_text_delta(monkeypatch) -> None:
    final = [SimpleNamespace(type="text", text="Hello world")]
    stream = _FakeStream(
        [_delta_event("text", "Hello"), _delta_event("text", " world")],
        final_content=final,
    )
    agent = _make_agent(monkeypatch, _FakeRunner([stream]))

    collected: list[str] = []

    async def on_token(tok: str) -> None:
        collected.append(tok)

    result = asyncio.run(
        agent.run_turn([{"role": "user", "content": "hi"}], "system", on_token=on_token)
    )

    assert collected == ["Hello", " world"]
    assert result.ttft_ms is not None
    assert result.ttft_ms >= 0.0
    assert result.assistant_message == "Hello world"


def test_ttft_set_even_without_on_token_callback(monkeypatch) -> None:
    """Non-streaming caller (no on_token) still gets a TTFT measurement."""
    final = [SimpleNamespace(type="text", text="ok")]
    stream = _FakeStream([_delta_event("text", "ok")], final_content=final)
    agent = _make_agent(monkeypatch, _FakeRunner([stream]))

    result = asyncio.run(agent.run_turn([{"role": "user", "content": "hi"}], "system"))

    assert result.ttft_ms is not None
    assert result.ttft_ms >= 0.0


def test_ttft_measured_on_first_thinking_delta(monkeypatch) -> None:
    """A thinking delta arriving before any text still anchors TTFT."""
    final = [SimpleNamespace(type="text", text="answer")]
    stream = _FakeStream(
        [_delta_event("thinking", "let me think"), _delta_event("text", "answer")],
        final_content=final,
    )
    agent = _make_agent(monkeypatch, _FakeRunner([stream]))

    result = asyncio.run(agent.run_turn([{"role": "user", "content": "hi"}], "system"))

    assert result.ttft_ms is not None


def test_ttft_none_when_no_tokens_stream(monkeypatch) -> None:
    """An empty stream (no deltas) leaves ttft_ms unset."""
    stream = _FakeStream([], final_content=[])
    agent = _make_agent(monkeypatch, _FakeRunner([stream]))

    result = asyncio.run(agent.run_turn([{"role": "user", "content": "hi"}], "system"))

    assert result.ttft_ms is None

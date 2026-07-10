"""Unit tests covering the prompt-caching wire format.

These tests assert the shapes that go into ``client.beta.messages.tool_runner``
carry the cache_control markers prompt-caching needs: a marker on the last
tool, a marker on the system block, and a marker on the most recent message.

The goal is to catch regressions where a refactor strips one of the markers —
a silent cache miss is otherwise invisible until somebody notices the bill.
"""

from __future__ import annotations

import re
from typing import Any

from persome.chat.agent import (
    _accumulate_usage,
    _make_async_sdk_tool,
    _to_anthropic_messages,
    _with_terminal_cache_breakpoint,
)
from persome.chat.handler import _strip_time_prefix

_EPHEMERAL = {"type": "ephemeral"}


# ─── _to_anthropic_messages places cache_control on the last block ──────────


def test_to_anthropic_messages_marks_last_user_block_with_cache_control() -> None:
    out = _to_anthropic_messages([{"role": "user", "content": "hello"}])
    assert len(out) == 1
    blocks = out[0]["content"]
    assert isinstance(blocks, list)
    assert blocks[-1].get("cache_control") == _EPHEMERAL
    assert blocks[-1]["type"] == "text"
    assert blocks[-1]["text"] == "hello"


def test_to_anthropic_messages_only_last_message_gets_breakpoint() -> None:
    out = _to_anthropic_messages(
        [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "second"},
        ]
    )
    assert out[0]["content"] == "first"  # untouched
    assert out[1]["content"] == "reply"  # untouched
    assert isinstance(out[2]["content"], list)
    assert out[2]["content"][-1]["cache_control"] == _EPHEMERAL


def test_to_anthropic_messages_preserves_existing_blocks_and_marks_last() -> None:
    out = _to_anthropic_messages(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "A"},
                    {"type": "text", "text": "B"},
                ],
            }
        ]
    )
    blocks = out[0]["content"]
    assert blocks[0] == {"type": "text", "text": "A"}  # untouched
    assert blocks[1]["text"] == "B"
    assert blocks[1]["cache_control"] == _EPHEMERAL


def test_to_anthropic_messages_drops_system_and_legacy_tool_messages() -> None:
    out = _to_anthropic_messages(
        [
            {"role": "system", "content": "sys"},
            {"role": "tool", "content": "tool result"},
            {"role": "user", "content": "real"},
        ]
    )
    assert len(out) == 1
    assert out[0]["role"] == "user"


def test_to_anthropic_messages_handles_empty() -> None:
    assert _to_anthropic_messages([]) == []


def test_with_terminal_cache_breakpoint_does_not_mutate_input() -> None:
    original = {"role": "user", "content": "x"}
    _with_terminal_cache_breakpoint(original)
    assert original == {"role": "user", "content": "x"}


# ─── _make_async_sdk_tool wires cache_control through ───────────────────────


def test_make_async_sdk_tool_without_cache_control() -> None:
    tool = _make_async_sdk_tool(
        "foo",
        "desc",
        {"type": "object", "properties": {}},
        lambda _kwargs: {"ok": True},
    )
    # SDK stores it on a private attribute; null when omitted.
    assert getattr(tool, "_cache_control", None) is None


def test_make_async_sdk_tool_with_cache_control_attaches_it() -> None:
    tool = _make_async_sdk_tool(
        "foo",
        "desc",
        {"type": "object", "properties": {}},
        lambda _kwargs: {"ok": True},
        cache_control=_EPHEMERAL,
    )
    assert tool._cache_control == _EPHEMERAL


# ─── _accumulate_usage now tracks cache_creation_input_tokens ───────────────


class _FakeUsage:
    def __init__(
        self,
        input_tokens: int,
        output_tokens: int,
        cache_read_input_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
    ) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read_input_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens


def test_accumulate_usage_includes_cache_creation() -> None:
    usage: dict[str, int] = {}
    _accumulate_usage(
        _FakeUsage(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=100,
            cache_creation_input_tokens=200,
        ),
        usage,
    )
    assert usage == {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_input_tokens": 100,
        "cache_creation_input_tokens": 200,
    }


def test_accumulate_usage_accumulates_across_calls() -> None:
    usage: dict[str, int] = {}
    _accumulate_usage(_FakeUsage(1, 2, 3, 4), usage)
    _accumulate_usage(_FakeUsage(10, 20, 30, 40), usage)
    assert usage == {
        "input_tokens": 11,
        "output_tokens": 22,
        "cache_read_input_tokens": 33,
        "cache_creation_input_tokens": 44,
    }


# ─── _strip_time_prefix removes [Current time: ...] cleanly ─────────────────


def test_strip_time_prefix_removes_marker() -> None:
    raw = "[Current time: 2026-05-26 21:00:00]\n\nHello world"
    assert _strip_time_prefix(raw) == "Hello world"


def test_strip_time_prefix_no_marker_passthrough() -> None:
    assert _strip_time_prefix("just text") == "just text"


def test_strip_time_prefix_only_strips_first_occurrence() -> None:
    # If a message body coincidentally contains another [Current time: ...]
    # block later, leave it alone — only the leading prefix is ours.
    raw = "[Current time: 2026-01-01 00:00:00]\n\nUser quoted: [Current time: x]"
    assert _strip_time_prefix(raw) == "User quoted: [Current time: x]"


# ─── system prompt no longer contains {current_time} placeholder ────────────


def test_system_prompt_template_has_no_current_time_placeholder() -> None:
    from persome.prompts import load as load_prompt

    body = load_prompt("chat.md")
    # Specifically the {current_time} format placeholder is gone — that was
    # the silent cache invalidator the PR fixes.
    assert re.search(r"\{current_time\}", body) is None


# ─── run_turn tool-list assembly: sorted by name, last has cache_control ────


def test_run_turn_tools_are_sorted_and_last_has_cache_control() -> None:
    """End-to-end shape check on ChatAgent.run_turn's combined_tools.

    Mocks the SDK at the boundary so we can inspect the kwargs passed to
    tool_runner without making a real API call.
    """
    import asyncio

    from persome.chat.agent import ChatAgent
    from persome.config import ChatConfig

    schemas = [
        {"name": "zebra_tool", "description": "z", "input_schema": {"type": "object"}},
        {"name": "apple_tool", "description": "a", "input_schema": {"type": "object"}},
        {"name": "mango_tool", "description": "m", "input_schema": {"type": "object"}},
    ]
    handlers = {name: (lambda _kw: "ok") for name in ("zebra_tool", "apple_tool", "mango_tool")}

    captured: dict[str, Any] = {}

    class _FakeStream:
        def __init__(self) -> None:
            self._events: list[Any] = []

        def __aiter__(self) -> _FakeStream:
            return self

        async def __anext__(self) -> Any:
            raise StopAsyncIteration

        async def get_final_message(self) -> Any:
            class _M:
                content: list[Any] = []
                usage = None

            return _M()

    class _FakeRunner:
        def __aiter__(self) -> _FakeRunner:
            self._yielded = False
            return self

        async def __anext__(self) -> _FakeStream:
            if self._yielded:
                raise StopAsyncIteration
            self._yielded = True
            return _FakeStream()

    class _FakeMessages:
        def tool_runner(self, **kwargs: Any) -> _FakeRunner:
            captured["kwargs"] = kwargs
            return _FakeRunner()

    class _FakeBeta:
        messages = _FakeMessages()

    class _FakeClient:
        beta = _FakeBeta()

        async def close(self) -> None:
            pass

    cfg = ChatConfig(model="deepseek-chat")
    agent = ChatAgent(cfg, schemas, handlers)
    agent.client = _FakeClient()  # type: ignore[assignment]

    async def _go() -> None:
        await agent.run_turn([{"role": "user", "content": "hi"}], system="SYS")

    asyncio.run(_go())

    tools = captured["kwargs"]["tools"]
    names = [t.name for t in tools]
    assert names == sorted(names)
    assert names == ["apple_tool", "mango_tool", "zebra_tool"]
    # Last tool carries the cache breakpoint (SDK stashes it on _cache_control)
    assert tools[-1]._cache_control == _EPHEMERAL
    # Earlier tools do not
    for t in tools[:-1]:
        assert getattr(t, "_cache_control", None) is None

    # System wrapped in list-of-blocks with cache_control
    system_param = captured["kwargs"]["system"]
    assert system_param == [{"type": "text", "text": "SYS", "cache_control": _EPHEMERAL}]

    # User message has cache breakpoint on its last block
    msgs = captured["kwargs"]["messages"]
    assert msgs[-1]["content"][-1]["cache_control"] == _EPHEMERAL

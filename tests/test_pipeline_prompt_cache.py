"""Unit tests for the Anthropic-SDK LLM adapter (writer/llm.py).

Since the migration off litellm, the background stages call the Anthropic
Messages API directly. ``call_llm`` converts the OpenAI-shaped messages/tools
into Anthropic shape and adapts the response back. Covers:
- ``_to_anthropic_tools`` — OpenAI function → Anthropic ``input_schema`` (+ cache_control)
- ``_to_anthropic_messages`` — system extraction, tool_result folding, tool_use, cache_control pass-through
- ``call_llm`` forwards a bare model + converted tools/system to the SDK, preserving cache_control
- system prompt templates contain no format placeholders (cache stability)
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest

from persome.writer.llm import (
    _to_anthropic_messages,
    _to_anthropic_tools,
    _to_openai_messages,
)

_EPHEMERAL = {"type": "ephemeral"}


# ─── tool conversion ─────────────────────────────────────────────────────────


def test_to_anthropic_tools_converts_function_schema() -> None:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_memory",
                "description": "read a file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
            "cache_control": dict(_EPHEMERAL),
        }
    ]
    out = _to_anthropic_tools(tools)
    assert out[0]["name"] == "read_memory"
    assert out[0]["description"] == "read a file"
    assert out[0]["input_schema"]["properties"]["path"]["type"] == "string"
    assert "parameters" not in out[0]
    assert out[0]["cache_control"] == _EPHEMERAL  # preserved (passes through to gateway)


# ─── message conversion ──────────────────────────────────────────────────────


def test_to_anthropic_messages_extracts_system_and_passes_cache_control() -> None:
    messages = [
        {
            "role": "system",
            "content": [{"type": "text", "text": "sys", "cache_control": dict(_EPHEMERAL)}],
        },
        {
            "role": "user",
            "content": [{"type": "text", "text": "hi", "cache_control": dict(_EPHEMERAL)}],
        },
    ]
    system, msgs = _to_anthropic_messages(messages)
    assert system[0]["cache_control"] == _EPHEMERAL  # cache_control survives — no stripping
    assert len(msgs) == 1 and msgs[0]["role"] == "user"
    assert msgs[0]["content"][0]["cache_control"] == _EPHEMERAL


def test_to_anthropic_messages_folds_tool_calls_and_results() -> None:
    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "f", "arguments": '{"x": 1}'}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "result-text"},
    ]
    _system, msgs = _to_anthropic_messages(messages)
    assert msgs[1]["role"] == "assistant"
    tu = msgs[1]["content"][0]
    assert tu["type"] == "tool_use" and tu["id"] == "c1" and tu["input"] == {"x": 1}
    tr = msgs[2]["content"][0]
    assert (
        tr["type"] == "tool_result" and tr["tool_use_id"] == "c1" and tr["content"] == "result-text"
    )


def test_to_openai_messages_omits_nonstandard_tool_name() -> None:
    messages = [
        {
            "role": "tool",
            "tool_call_id": "c1",
            "name": "search_memory",
            "content": "result",
        }
    ]
    assert _to_openai_messages(messages) == [
        {"role": "tool", "tool_call_id": "c1", "content": "result"}
    ]


# ─── call_llm end-to-end (stubbed Anthropic client) ──────────────────────────


def test_call_llm_forwards_bare_model_and_converted_tools(monkeypatch) -> None:
    monkeypatch.delenv("PERSOME_LLM_MOCK", raising=False)
    from persome import config as config_mod
    from persome.writer import llm as llm_mod

    captured: dict[str, Any] = {}

    class _FakeMessages:
        def create(self, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="ok")],
                stop_reason="end_turn",
                usage=SimpleNamespace(input_tokens=3, output_tokens=1),
            )

    monkeypatch.setattr(
        llm_mod, "_anthropic_client", lambda _profile: SimpleNamespace(messages=_FakeMessages())
    )

    cfg = config_mod.Config()
    cfg.models["default"] = config_mod.ModelConfig(model="anthropic/deepseek-v4-flash")
    messages = [
        {
            "role": "system",
            "content": [{"type": "text", "text": "sys", "cache_control": dict(_EPHEMERAL)}],
        },
        {"role": "user", "content": "hi"},
    ]
    tools = [
        {
            "type": "function",
            "function": {"name": "x", "parameters": {}},
            "cache_control": dict(_EPHEMERAL),
        }
    ]

    resp = llm_mod.call_llm(cfg, "default", messages=messages, tools=tools)

    assert captured["model"] == "deepseek-v4-flash"  # prefix stripped for the SDK
    assert captured["system"][0]["cache_control"] == _EPHEMERAL  # preserved
    assert captured["tools"][0]["name"] == "x" and "input_schema" in captured["tools"][0]
    assert captured["tools"][0]["cache_control"] == _EPHEMERAL
    # response adapted back into OpenAI shape
    assert llm_mod.extract_text(resp) == "ok"
    assert llm_mod.extract_usage(resp)["prompt_tokens"] == 3


def test_call_llm_openai_compatible_strips_anthropic_extensions(monkeypatch) -> None:
    monkeypatch.delenv("PERSOME_LLM_MOCK", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic")
    from persome import config as config_mod
    from persome.writer import llm as llm_mod

    captured: dict[str, Any] = {}

    class _FakeCompletions:
        def create(self, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="ok", tool_calls=None),
                        finish_reason="stop",
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=4, completion_tokens=1, total_tokens=5),
            )

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions()))
    monkeypatch.setattr(llm_mod, "_openai_client", lambda _profile: fake_client)

    cfg = config_mod.Config(
        models={
            "default": config_mod.ModelConfig(
                provider="openrouter",
                protocol="openai",
                model="anthropic/claude-sonnet-4",
                base_url="https://openrouter.ai/api/v1",
                api_key_env="OPENROUTER_API_KEY",
            )
        }
    )
    response = llm_mod.call_llm(
        cfg,
        "default",
        messages=[
            {
                "role": "system",
                "content": [{"type": "text", "text": "sys", "cache_control": _EPHEMERAL}],
            },
            {"role": "user", "content": "hi"},
        ],
        tools=[
            {
                "type": "function",
                "function": {"name": "lookup", "parameters": {"type": "object"}},
                "cache_control": _EPHEMERAL,
            }
        ],
    )

    assert captured["model"] == "anthropic/claude-sonnet-4"
    assert captured["messages"][0] == {"role": "system", "content": "sys"}
    assert "cache_control" not in captured["tools"][0]
    assert llm_mod.extract_text(response) == "ok"
    assert llm_mod.extract_usage(response)["total_tokens"] == 5


# ─── system prompt templates carry no format placeholders ───────────────────


@pytest.mark.parametrize("name", ["timeline_block.system.md", "session_reduce.system.md"])
def test_system_prompt_template_has_no_format_placeholder(name: str) -> None:
    """A {placeholder} in a system prompt is a silent cache invalidator — every
    request gets a different prefix. Catch it at CI time."""
    from persome.prompts import load as load_prompt

    body = load_prompt(name)
    placeholders = re.findall(r"(?<!\{)\{[a-z_][a-z_0-9]*\}(?!\})", body)
    assert not placeholders, f"{name} contains format placeholders: {placeholders}"

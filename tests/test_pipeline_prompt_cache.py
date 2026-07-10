"""Unit tests for the Anthropic-SDK LLM adapter (writer/llm.py).

Since the migration off litellm, the background stages call the Anthropic
Messages API directly. ``call_llm`` converts the OpenAI-shaped messages/tools
into Anthropic shape and adapts the response back. Covers:
- ``_bare_model`` — routing-prefix stripping
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
    _bare_model,
    _to_anthropic_messages,
    _to_anthropic_tools,
)

_EPHEMERAL = {"type": "ephemeral"}


# ─── _bare_model ─────────────────────────────────────────────────────────────


def test_bare_model_strips_routing_prefixes() -> None:
    assert _bare_model("anthropic/deepseek-v4-flash") == "deepseek-v4-flash"
    assert _bare_model("deepseek/deepseek-v4-flash") == "deepseek-v4-flash"
    assert _bare_model("openai/gpt-4o") == "gpt-4o"
    assert _bare_model("deepseek-v4-flash") == "deepseek-v4-flash"  # already bare
    assert _bare_model("claude-haiku-4-5") == "claude-haiku-4-5"


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
        llm_mod, "_anthropic_client", lambda: SimpleNamespace(messages=_FakeMessages())
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


# ─── system prompt templates carry no format placeholders ───────────────────


@pytest.mark.parametrize("name", ["timeline_block.system.md", "session_reduce.system.md"])
def test_system_prompt_template_has_no_format_placeholder(name: str) -> None:
    """A {placeholder} in a system prompt is a silent cache invalidator — every
    request gets a different prefix. Catch it at CI time."""
    from persome.prompts import load as load_prompt

    body = load_prompt(name)
    placeholders = re.findall(r"(?<!\{)\{[a-z_][a-z_0-9]*\}(?!\})", body)
    assert not placeholders, f"{name} contains format placeholders: {placeholders}"

"""Tests for F2: tool result budget (per-result truncation and microcompact)."""

from __future__ import annotations

from typing import Any

from persome.writer.llm import (
    _microcompact_tool_results,
    _truncate_result,
    make_tool_response,
)


def _make_assistant(tool_call_id: str = "call_1") -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": tool_call_id,
                "type": "function",
                "function": {"name": "read_memory", "arguments": "{}"},
            }
        ],
    }


# ──────────────────────────────────────────────────────────────────────────
# Test 1: large result is truncated to fit within max_bytes
# ──────────────────────────────────────────────────────────────────────────


def test_large_result_truncated():
    """构造 32KB result，验证 make_tool_response 输出 content ≤ 16KB。"""
    max_bytes = 16_384
    big_result = {"content": "x" * 40_000}
    msg = make_tool_response(_make_assistant(), 0, "read_memory", big_result, max_bytes=max_bytes)
    assert len(msg["content"].encode()) <= max_bytes


# ──────────────────────────────────────────────────────────────────────────
# Test 2: truncated result has _truncated=True flag
# ──────────────────────────────────────────────────────────────────────────


def test_truncated_flag_injected():
    """验证截断后结果中有 _truncated=True 字段。"""
    big_result = {"content": "y" * 40_000}
    truncated = _truncate_result(big_result, 16_384)
    assert truncated.get("_truncated") is True


def test_small_result_not_truncated():
    """验证小结果不被截断（无 _truncated 字段）。"""
    small = {"content": "hello"}
    out = _truncate_result(small, 16_384)
    assert "_truncated" not in out
    assert out["content"] == "hello"


# ──────────────────────────────────────────────────────────────────────────
# Test 3: microcompact replaces oldest tool results when over budget
# ──────────────────────────────────────────────────────────────────────────


def test_microcompact_oldest_results():
    """构造超过 total_budget 的 messages，验证最旧 tool result 被替换。"""
    budget = 100  # very small budget
    big_content = "z" * 200

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "user"},
        {"role": "assistant", "content": None},
        {"role": "tool", "tool_call_id": "c1", "name": "t", "content": big_content},
        {"role": "assistant", "content": None},
        {"role": "tool", "tool_call_id": "c2", "name": "t", "content": big_content},
    ]

    _microcompact_tool_results(messages, budget)

    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    # At least one should be compacted
    compacted = [m for m in tool_msgs if m["content"] == "[compacted]"]
    assert len(compacted) >= 1


def test_microcompact_noop_within_budget():
    """总量未超限时不做任何修改。"""
    messages: list[dict[str, Any]] = [
        {"role": "tool", "content": "small"},
    ]
    _microcompact_tool_results(messages, 10_000)
    assert messages[0]["content"] == "small"

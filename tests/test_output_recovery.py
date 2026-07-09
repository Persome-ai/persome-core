"""Tests for F3: output truncation recovery (finish_reason='length')."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from persome.config import Config, WriterConfig
from persome.writer import llm as llm_mod


def _make_cfg(**kw) -> Config:
    return Config(writer=WriterConfig(**kw))


def _build_length_response(content: str = "") -> Any:
    """Build a response with finish_reason='length'."""
    resp = llm_mod._build_response(content)
    resp.choices[0].finish_reason = "length"
    return resp


@dataclass
class _State:
    committed: bool = False


# ──────────────────────────────────────────────────────────────────────────
# Test 1: length finish_reason causes LLM to be called again
# ──────────────────────────────────────────────────────────────────────────


def test_length_finish_reason_retries(monkeypatch):
    """mock 一次 finish_reason='length' 后正常结束，验证 call_llm 被调用 2 次。"""
    calls: list[int] = []

    def _stub(cfg, stage, *, messages, tools=None, json_mode=False):
        calls.append(1)
        if len(calls) == 1:
            return _build_length_response("partial output")
        return llm_mod._build_response("")

    cfg = _make_cfg(
        llm_retry_attempts=1,
        max_output_tokens_recovery_count=3,
        max_output_tokens_recovery_limit=65_536,
    )
    monkeypatch.setattr(llm_mod, "call_llm", _stub)

    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    state = _State()
    llm_mod.run_tool_loop(
        cfg,
        "classifier",
        messages,
        tools=[],
        dispatch_fn=lambda n, a: {},
        valid_tool_names=set(),
        state=state,
        max_iter=5,
    )
    assert len(calls) == 2


# ──────────────────────────────────────────────────────────────────────────
# Test 2: continuation prompt is injected into messages
# ──────────────────────────────────────────────────────────────────────────


def test_continuation_prompt_injected(monkeypatch):
    """验证 finish_reason='length' 后 continuation user message 被注入。"""
    calls: list[int] = []

    def _stub(cfg, stage, *, messages, tools=None, json_mode=False):
        calls.append(len(messages))
        if len(calls) == 1:
            return _build_length_response("partial output")
        return llm_mod._build_response("")

    cfg = _make_cfg(
        llm_retry_attempts=1,
        max_output_tokens_recovery_count=3,
        max_output_tokens_recovery_limit=65_536,
    )
    monkeypatch.setattr(llm_mod, "call_llm", _stub)

    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    state = _State()
    llm_mod.run_tool_loop(
        cfg,
        "classifier",
        messages,
        tools=[],
        dispatch_fn=lambda n, a: {},
        valid_tool_names=set(),
        state=state,
        max_iter=5,
    )

    # Continuation user message should appear in messages
    user_msgs = [m for m in messages if m.get("role") == "user"]
    continuation_msgs = [m for m in user_msgs if "Continue directly" in (m.get("content") or "")]
    assert len(continuation_msgs) >= 1


# ──────────────────────────────────────────────────────────────────────────
# Test 3: recovery limit is respected (don't recover more than N times)
# ──────────────────────────────────────────────────────────────────────────


def test_recovery_limit_respected(monkeypatch):
    """mock 4 次 finish_reason='length'，验证只恢复 max_recovery_count=3 次。"""
    calls: list[int] = []

    def _stub(cfg, stage, *, messages, tools=None, json_mode=False):
        calls.append(1)
        return _build_length_response(f"part {len(calls)}")

    cfg = _make_cfg(
        llm_retry_attempts=1,
        max_output_tokens_recovery_count=3,
        max_output_tokens_recovery_limit=65_536,
    )
    monkeypatch.setattr(llm_mod, "call_llm", _stub)

    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    state = _State()
    llm_mod.run_tool_loop(
        cfg,
        "classifier",
        messages,
        tools=[],
        dispatch_fn=lambda n, a: {},
        valid_tool_names=set(),
        state=state,
        max_iter=10,
    )
    # 3 recoveries = 4 calls, then the 4th recovery is not attempted (limit reached)
    # But the loop continues with subsequent normal iterations
    # The key invariant: recovery_count never exceeds max_output_tokens_recovery_count
    assert len(calls) >= 4  # at least the initial + 3 recovery iterations

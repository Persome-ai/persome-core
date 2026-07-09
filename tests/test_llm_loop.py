"""Tests for run_tool_loop harness: retry, context trim, token tracking."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from persome.config import Config, WriterConfig
from persome.writer import llm as llm_mod


def _make_cfg(**writer_kwargs) -> Config:
    return Config(writer=WriterConfig(**writer_kwargs))


@dataclass
class _State:
    committed: bool = False


def _noop_dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True}


# ──────────────────────────────────────────────────────────────
# Test 1: LLM 瞬时失败后重试，loop 正常完成
# ──────────────────────────────────────────────────────────────


def test_run_tool_loop_retries_on_llm_failure(monkeypatch):
    """第一次 call_llm 抛异常，第二次成功；loop 正常完成而非中断。"""
    calls: list[int] = []

    def _stub(cfg, stage, *, messages, tools=None, json_mode=False):
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            raise RuntimeError("transient network error")
        return llm_mod._build_response("")  # no tool calls → loop exits cleanly

    cfg = _make_cfg(llm_retry_attempts=2)
    monkeypatch.setattr(llm_mod, "call_llm", _stub)
    monkeypatch.setattr(time, "sleep", lambda _: None)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "user"},
    ]
    state = _State()

    result = llm_mod.run_tool_loop(
        cfg,
        "classifier",
        messages,
        tools=[],
        dispatch_fn=_noop_dispatch,
        valid_tool_names=set(),
        state=state,
        max_iter=3,
    )

    assert len(calls) == 2, "第一次失败后应重试一次"
    assert result == 3, "loop 以 max_iter 正常结束"


# ──────────────────────────────────────────────────────────────
# Test 2: 消息历史超限时自动 trim
# ──────────────────────────────────────────────────────────────


def test_run_tool_loop_trims_context_when_over_limit(monkeypatch):
    """messages 超出 context_token_limit 时，旧的 round-trip 被原地删除。"""

    def _stub(cfg, stage, *, messages, tools=None, json_mode=False):
        return llm_mod._build_response("")

    # 极低 limit: 50 tokens ≈ 200 chars
    cfg = _make_cfg(context_token_limit=50, llm_retry_attempts=1)
    monkeypatch.setattr(llm_mod, "call_llm", _stub)

    fat = "x" * 200  # 每条消息约 50 tokens
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": fat},
        {"role": "user", "content": fat},
        {"role": "assistant", "content": fat},
        {"role": "tool", "content": fat, "tool_call_id": "id1", "name": "t"},
        {"role": "assistant", "content": fat},
        {"role": "tool", "content": fat, "tool_call_id": "id2", "name": "t"},
    ]
    original_len = len(messages)
    state = _State()

    llm_mod.run_tool_loop(
        cfg,
        "classifier",
        messages,
        tools=[],
        dispatch_fn=_noop_dispatch,
        valid_tool_names=set(),
        state=state,
        max_iter=1,
    )

    assert len(messages) < original_len, "超限时消息历史应被原地截短"
    # system + user 头两条不可删
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"


# ──────────────────────────────────────────────────────────────
# Test 3: loop 结束后记录 token 用量
# ──────────────────────────────────────────────────────────────


def test_run_tool_loop_logs_token_usage(monkeypatch):
    """loop 结束后应在 INFO 日志中输出 prompt_tokens 和 completion_tokens。"""

    class _Usage:
        prompt_tokens = 42
        completion_tokens = 17
        total_tokens = 59

    def _stub(cfg, stage, *, messages, tools=None, json_mode=False):
        resp = llm_mod._build_response("")
        resp.usage = _Usage()
        return resp

    cfg = _make_cfg(llm_retry_attempts=1)
    monkeypatch.setattr(llm_mod, "call_llm", _stub)

    # 直接 mock logger.info，不依赖 propagate 状态
    logged: list[str] = []
    monkeypatch.setattr(llm_mod.logger, "info", lambda fmt, *args: logged.append(fmt % args))

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "user"},
    ]
    state = _State()

    llm_mod.run_tool_loop(
        cfg,
        "classifier",
        messages,
        tools=[],
        dispatch_fn=_noop_dispatch,
        valid_tool_names=set(),
        state=state,
        max_iter=2,
    )

    log_text = " ".join(logged)
    assert "prompt_tokens" in log_text
    assert "42" in log_text

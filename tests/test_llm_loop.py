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

# ──────────────────────────────────────────────────────────────


def test_run_tool_loop_retries_on_llm_failure(monkeypatch):
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

    assert len(calls) == 2, "\u7b2c\u4e00\u6b21\u5931\u8d25\u540e\u5e94\u91cd\u8bd5\u4e00\u6b21"
    assert result == 3, "loop \u4ee5 max_iter \u6b63\u5e38\u7ed3\u675f"


# ──────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────


def test_run_tool_loop_trims_context_when_over_limit(monkeypatch):

    def _stub(cfg, stage, *, messages, tools=None, json_mode=False):
        return llm_mod._build_response("")

    cfg = _make_cfg(context_token_limit=50, llm_retry_attempts=1)
    monkeypatch.setattr(llm_mod, "call_llm", _stub)

    fat = "x" * 200
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

    assert len(messages) < original_len, (
        "\u8d85\u9650\u65f6\u6d88\u606f\u5386\u53f2\u5e94\u88ab\u539f\u5730\u622a\u77ed"
    )

    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"


# ──────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────


def test_run_tool_loop_logs_token_usage(monkeypatch):

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

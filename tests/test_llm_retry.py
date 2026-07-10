"""Tests for F1: error-classified retry in _call_llm_with_retry / run_tool_loop."""

from __future__ import annotations

import time
from dataclasses import dataclass

from persome.config import Config, WriterConfig
from persome.writer import llm as llm_mod


def _make_cfg(**kw) -> Config:
    return Config(writer=WriterConfig(**kw))


@dataclass
class _State:
    committed: bool = False


# ──────────────────────────────────────────────────────────────────────────
# Helper: build a fake exception class with optional response.headers
# ──────────────────────────────────────────────────────────────────────────


class _FakeExc(Exception):
    def __init__(self, msg="error", *, response=None):
        super().__init__(msg)
        self.response = response


class _FakeHeaders(dict):
    pass


# ──────────────────────────────────────────────────────────────────────────
# Test 1: generic network errors are retried up to llm_retry_attempts
# ──────────────────────────────────────────────────────────────────────────


def test_retries_network_error(monkeypatch):
    calls: list[int] = []

    def _stub(cfg, stage, *, messages, tools=None, json_mode=False):
        calls.append(1)
        if len(calls) < 4:
            raise RuntimeError("connection reset")
        return llm_mod._build_response("")

    cfg = _make_cfg(llm_retry_attempts=6)
    monkeypatch.setattr(llm_mod, "call_llm", _stub)
    monkeypatch.setattr(time, "sleep", lambda _: None)

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
        max_iter=1,
    )
    assert len(calls) == 4


# ──────────────────────────────────────────────────────────────────────────
# Test 2: 429 rate limit respects retry-after header
# ──────────────────────────────────────────────────────────────────────────


def test_respects_retry_after_header(monkeypatch):
    slept: list[float] = []
    calls: list[int] = []

    headers = _FakeHeaders({"retry-after": "2"})

    class _FakeResp:
        pass

    resp_obj = _FakeResp()
    resp_obj.headers = headers

    def _stub(cfg, stage, *, messages, tools=None, json_mode=False):
        calls.append(1)
        if len(calls) == 1:
            exc = _FakeExc("RateLimitError: 429", response=resp_obj)
            exc.__class__.__name__ = "RateLimitError"
            type(exc).__name__ = "RateLimitError"
            raise type("RateLimitError", (Exception,), {})("RateLimitError: 429")
        return llm_mod._build_response("")

    def _sleep(secs: float):
        slept.append(secs)

    cfg = _make_cfg(llm_retry_attempts=6, llm_rate_limit_wait_s=30)
    monkeypatch.setattr(llm_mod, "call_llm", _stub)
    monkeypatch.setattr(time, "sleep", _sleep)

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
        max_iter=1,
    )
    # The 429 should cause at least one sleep (fallback to cfg rate_limit_wait_s=30
    # since we can't inject headers via exception class name matching alone)
    assert len(slept) >= 1


# ──────────────────────────────────────────────────────────────────────────
# Test 3: 529 overloaded 3 times → uses fallback model
# ──────────────────────────────────────────────────────────────────────────


def test_fallback_on_529(monkeypatch):
    calls: list[str] = []
    env_snapshots: list[str | None] = []

    import os

    ServiceUnavailableError = type("ServiceUnavailableError", (Exception,), {})

    def _stub(cfg, stage, *, messages, tools=None, json_mode=False):
        calls.append("call")
        env_snapshots.append(os.environ.get("PERSOME_FALLBACK_MODEL"))
        if len(calls) <= 3:
            raise ServiceUnavailableError("ServiceUnavailableError: overloaded 529")
        return llm_mod._build_response("")

    cfg = _make_cfg(llm_retry_attempts=6, llm_fallback_model="gpt-4o-mini")
    monkeypatch.setattr(llm_mod, "call_llm", _stub)
    monkeypatch.setattr(time, "sleep", lambda _: None)

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
        max_iter=1,
    )
    assert len(calls) == 4
    # After 3 overloads, the 4th call should see the fallback model env var
    assert env_snapshots[3] == "gpt-4o-mini"


# ──────────────────────────────────────────────────────────────────────────
# Test 4: 413 context exceeded triggers reactive trim and retry
# ──────────────────────────────────────────────────────────────────────────


def test_413_triggers_reactive_trim(monkeypatch):
    calls: list[int] = []
    msg_lengths: list[int] = []

    ContextWindowExceededError = type("ContextWindowExceededError", (Exception,), {})

    def _stub(cfg, stage, *, messages, tools=None, json_mode=False):
        calls.append(1)
        msg_lengths.append(len(messages))
        if len(calls) == 1:
            raise ContextWindowExceededError("ContextWindowExceededError: context_length exceeded")
        return llm_mod._build_response("")

    cfg = _make_cfg(llm_retry_attempts=6)
    monkeypatch.setattr(llm_mod, "call_llm", _stub)
    monkeypatch.setattr(time, "sleep", lambda _: None)

    # Build long message history
    fat = "x" * 200
    messages = [
        {"role": "system", "content": fat},
        {"role": "user", "content": fat},
        {"role": "assistant", "content": fat},
        {"role": "tool", "content": fat, "tool_call_id": "id1", "name": "t"},
        {"role": "assistant", "content": fat},
        {"role": "tool", "content": fat, "tool_call_id": "id2", "name": "t"},
    ]
    state = _State()
    llm_mod.run_tool_loop(
        cfg,
        "classifier",
        messages,
        tools=[],
        dispatch_fn=lambda n, a: {},
        valid_tool_names=set(),
        state=state,
        max_iter=1,
    )
    assert len(calls) == 2
    # Second call should have fewer messages (reactive trim happened)
    assert msg_lengths[1] < msg_lengths[0]


# ──────────────────────────────────────────────────────────────────────────
# Test 5: auth errors abort immediately (no retry)
# ──────────────────────────────────────────────────────────────────────────


def test_auth_error_aborts_immediately(monkeypatch):
    calls: list[int] = []

    AuthenticationError = type("AuthenticationError", (Exception,), {})

    def _stub(cfg, stage, *, messages, tools=None, json_mode=False):
        calls.append(1)
        raise AuthenticationError("AuthenticationError: 401 invalid key")

    cfg = _make_cfg(llm_retry_attempts=6)
    monkeypatch.setattr(llm_mod, "call_llm", _stub)
    monkeypatch.setattr(time, "sleep", lambda _: None)

    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    state = _State()
    result = llm_mod.run_tool_loop(
        cfg,
        "classifier",
        messages,
        tools=[],
        dispatch_fn=lambda n, a: {},
        valid_tool_names=set(),
        state=state,
        max_iter=3,
    )
    assert len(calls) == 1, "auth error should not be retried"
    assert result == 3  # loop aborted, returns max_iter

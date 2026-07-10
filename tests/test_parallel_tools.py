"""Tests for F6: parallel tool execution via parallel_dispatch_fn."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from persome.config import Config, WriterConfig
from persome.writer import llm as llm_mod
from persome.writer.llm import CONCURRENCY_SAFE_TOOLS


def _make_cfg(**kw) -> Config:
    return Config(writer=WriterConfig(**kw))


@dataclass
class _State:
    committed: bool = False


def _make_tool_call_response(calls: list[dict]) -> Any:
    """Build a response that requests specific tool calls."""

    class _Fn:
        def __init__(self, name, args_json):
            self.name = name
            self.arguments = args_json

    class _TC:
        def __init__(self, id_, name, args_json):
            self.id = id_
            self.function = _Fn(name, args_json)

    class _Msg:
        def __init__(self, tool_calls):
            self.content = None
            self.tool_calls = tool_calls

    class _Choice:
        def __init__(self, msg):
            self.message = msg
            self.finish_reason = "tool_calls"

    class _Resp:
        def __init__(self, choices):
            self.choices = choices

    tcs = [_TC(c["id"], c["name"], c.get("args", "{}")) for c in calls]
    return _Resp([_Choice(_Msg(tcs))])


# ──────────────────────────────────────────────────────────────────────────
# Test 1: safe tools run concurrently (wall-clock < serial time)
# ──────────────────────────────────────────────────────────────────────────


def test_safe_tools_run_concurrently(monkeypatch):
    call_count = [0]
    stop_response = llm_mod._build_response("")  # no tool calls → loop ends

    def _stub(cfg, stage, *, messages, tools=None, json_mode=False):
        call_count[0] += 1
        if call_count[0] == 1:
            return _make_tool_call_response(
                [
                    {"id": "c1", "name": "drill_capture", "args": '{"capture_id":"a"}'},
                    {"id": "c2", "name": "drill_capture", "args": '{"capture_id":"b"}'},
                ]
            )
        return stop_response

    import threading

    execution_order: list[str] = []
    lock = threading.Lock()

    def _parallel_dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
        cid = args.get("capture_id")
        with lock:
            execution_order.append(f"start:{cid}")
        time.sleep(0.05)
        with lock:
            execution_order.append(f"end:{cid}")
        return {"result": cid}

    cfg = _make_cfg(llm_retry_attempts=1)
    monkeypatch.setattr(llm_mod, "call_llm", _stub)

    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    state = _State()
    llm_mod.run_tool_loop(
        cfg,
        "classifier",
        messages,
        tools=[],
        dispatch_fn=lambda n, a: {},
        valid_tool_names={"drill_capture"},
        state=state,
        max_iter=3,
        parallel_dispatch_fn=_parallel_dispatch,
    )

    assert len(execution_order) == 4
    first_end_idx = next(i for i, e in enumerate(execution_order) if e.startswith("end:"))
    starts_before_first_end = sum(
        1 for e in execution_order[:first_end_idx] if e.startswith("start:")
    )
    assert starts_before_first_end == 2, (
        f"expected both tools to start before either ended, got order: {execution_order}"
    )


# ──────────────────────────────────────────────────────────────────────────
# Test 2: unsafe tools (write side) run serially even with parallel_dispatch_fn
# ──────────────────────────────────────────────────────────────────────────


def test_unsafe_tools_run_serially(monkeypatch):
    assert "append" not in CONCURRENCY_SAFE_TOOLS
    assert "commit" not in CONCURRENCY_SAFE_TOOLS

    call_count = [0]
    stop_response = llm_mod._build_response("")

    def _stub(cfg, stage, *, messages, tools=None, json_mode=False):
        call_count[0] += 1
        if call_count[0] == 1:
            return _make_tool_call_response(
                [
                    {"id": "c1", "name": "commit", "args": '{"summary":"done"}'},
                ]
            )
        return stop_response

    parallel_calls: list[str] = []

    def _parallel_dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
        parallel_calls.append(name)
        return {}

    serial_calls: list[str] = []

    def _serial_dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
        serial_calls.append(name)
        if name == "commit":
            state.committed = True
        return {"ok": True}

    cfg = _make_cfg(llm_retry_attempts=1)
    monkeypatch.setattr(llm_mod, "call_llm", _stub)

    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    state = _State()
    llm_mod.run_tool_loop(
        cfg,
        "classifier",
        messages,
        tools=[],
        dispatch_fn=_serial_dispatch,
        valid_tool_names={"commit"},
        state=state,
        max_iter=3,
        parallel_dispatch_fn=_parallel_dispatch,
    )

    # commit should have gone through serial dispatch, not parallel
    assert "commit" in serial_calls
    assert "commit" not in parallel_calls


# ──────────────────────────────────────────────────────────────────────────
# Test 3: tool responses appear in correct order regardless of completion order
# ──────────────────────────────────────────────────────────────────────────


def test_results_in_correct_order(monkeypatch):
    call_count = [0]
    stop_response = llm_mod._build_response("")

    def _stub(cfg, stage, *, messages, tools=None, json_mode=False):
        call_count[0] += 1
        if call_count[0] == 1:
            return _make_tool_call_response(
                [
                    {"id": "first", "name": "drill_capture", "args": '{"capture_id":"A"}'},
                    {"id": "second", "name": "drill_capture", "args": '{"capture_id":"B"}'},
                ]
            )
        return stop_response

    def _parallel_dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
        cid = args.get("capture_id")
        if cid == "A":
            time.sleep(0.02)  # A finishes after B
        return {"capture_id": cid}

    cfg = _make_cfg(llm_retry_attempts=1)
    monkeypatch.setattr(llm_mod, "call_llm", _stub)

    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    state = _State()
    llm_mod.run_tool_loop(
        cfg,
        "classifier",
        messages,
        tools=[],
        dispatch_fn=lambda n, a: {},
        valid_tool_names={"drill_capture"},
        state=state,
        max_iter=3,
        parallel_dispatch_fn=_parallel_dispatch,
    )

    # Find tool response messages
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 2
    # First response should correspond to first call (id="first" → capture_id="A")
    first_content = tool_msgs[0].get("tool_call_id")
    assert first_content == "first"
    assert tool_msgs[1].get("tool_call_id") == "second"

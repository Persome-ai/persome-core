"""Integration tests using real DeepSeek API (deepseek/deepseek-v4-flash).

Run with:
    DEEPSEEK_API_KEY=sk-... uv run pytest tests/test_integration_deepseek.py -v -s

Skipped automatically when DEEPSEEK_API_KEY is not set.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest


# ── skip all if key absent ─────────────────────────────────────────────────
def _load_key() -> str | None:
    if key := os.environ.get("DEEPSEEK_API_KEY"):
        return key
    try:
        for line in Path(__file__).parent.parent.joinpath(".env").read_text().splitlines():
            if line.startswith("DEEPSEEK_API_KEY="):
                return line.split("=", 1)[1].strip()
    except FileNotFoundError:
        pass
    return None


_KEY = _load_key()
# Mirror into env so config.provider_api_key("deepseek") finds it — the new
# env-only config path doesn't take an inline api_key argument anymore.
if _KEY:
    os.environ.setdefault("DEEPSEEK_API_KEY", _KEY)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _KEY, reason="DEEPSEEK_API_KEY not set"),
    pytest.mark.skipif(
        os.environ.get("PERSOME_LLM_MOCK") == "1",
        reason="LLM mock mode — skip real API integration tests",
    ),
]

MODEL = "deepseek/deepseek-v4-flash"


def _make_cfg(max_tokens: int = 64):
    from persome.config import Config, ModelConfig, WriterConfig

    cfg = Config(writer=WriterConfig(llm_retry_attempts=2))
    cfg.models["default"] = ModelConfig(model=MODEL, max_tokens=max_tokens)
    return cfg


@dataclass
class _State:
    committed: bool = False


# ── Test 1: call_llm smoke test ────────────────────────────────────────────


def test_call_llm_returns_completion():
    """call_llm 成功返回真实补全，内容非空。"""
    from persome.writer.llm import call_llm

    cfg = _make_cfg()
    resp = call_llm(
        cfg,
        "classifier",
        messages=[{"role": "user", "content": "Reply with the single word: pong"}],
    )
    text = resp.choices[0].message.content or ""
    assert len(text.strip()) > 0, "LLM returned empty content"
    assert resp.usage is not None


# ── Test 2: extract_usage 读取真实 usage 字段 ──────────────────────────────


def test_extract_usage_from_real_response():
    """extract_usage 从真实响应解析 prompt/completion token 计数。"""
    from persome.writer.llm import call_llm, extract_usage

    cfg = _make_cfg()
    resp = call_llm(
        cfg,
        "classifier",
        messages=[{"role": "user", "content": "hi"}],
    )
    usage = extract_usage(resp)
    assert usage is not None
    assert usage["prompt_tokens"] > 0
    assert usage["completion_tokens"] > 0
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]


# ── Test 3: calculate_usd 对真实 usage 计算成本 ────────────────────────────


def test_calculate_usd_with_real_usage():
    """用真实 token 计数计算 deepseek-chat 的 USD 成本，结果应为正数。"""
    from persome.writer.cost import calculate_usd
    from persome.writer.llm import call_llm, extract_usage

    cfg = _make_cfg()
    resp = call_llm(
        cfg,
        "classifier",
        messages=[{"role": "user", "content": "hi"}],
    )
    usage = extract_usage(resp)
    assert usage is not None

    # deepseek-chat is in _COSTS table
    cost = calculate_usd("deepseek-chat", usage)
    assert cost is not None, "deepseek-chat not found in cost table"
    assert cost > 0


# ── Test 4: run_tool_loop 一个完整无工具调用的 loop ────────────────────────


def test_run_tool_loop_no_tools():
    """run_tool_loop 在无工具调用时正常结束，返回 iteration count < max_iter。"""
    from persome.writer.llm import run_tool_loop

    cfg = _make_cfg(max_tokens=32)
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Say exactly: done"},
    ]
    state = _State()
    run_tool_loop(
        cfg,
        "classifier",
        messages,
        tools=[],
        dispatch_fn=lambda n, a: {},
        valid_tool_names=set(),
        state=state,
        max_iter=3,
    )
    # messages should now include the assistant reply
    roles = [m["role"] for m in messages]
    assert "assistant" in roles
    # no tool calls were made
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 0


# ── Test 5: run_tool_loop 带一个真实工具调用 ──────────────────────────────


def test_run_tool_loop_with_tool_call():
    """LLM 主动调用 search_memory 工具，dispatch 返回结果，loop 正常结束。"""

    from persome.writer.llm import run_tool_loop

    tool_schema = [
        {
            "type": "function",
            "function": {
                "name": "search_memory",
                "description": "Search user's memory for relevant entries.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "search query"}},
                    "required": ["query"],
                },
            },
        }
    ]

    dispatched: list[dict] = []

    def _dispatch(name: str, args: dict) -> dict:
        dispatched.append({"name": name, "args": args})
        return {"entries": ["User is a software engineer."]}

    cfg = _make_cfg(max_tokens=128)
    messages = [
        {
            "role": "system",
            "content": "Use search_memory to find info about the user, then summarize briefly.",
        },
        {"role": "user", "content": "Who am I?"},
    ]
    state = _State()
    run_tool_loop(
        cfg,
        "classifier",
        messages,
        tools=tool_schema,
        dispatch_fn=_dispatch,
        valid_tool_names={"search_memory"},
        state=state,
        max_iter=5,
    )
    # LLM should have called search_memory at least once
    assert len(dispatched) >= 1
    assert dispatched[0]["name"] == "search_memory"


# ── Test 6: count_tokens_api 对非 Anthropic 模型返回 None ─────────────────


def test_count_tokens_api_skips_non_anthropic():
    """deepseek 不是 Anthropic 模型，count_tokens_api 应直接返回 None。"""
    from persome.writer.llm import count_tokens_api

    cfg = _make_cfg()
    # use_token_count_api=False by default, but even if True, non-Anthropic → None
    from persome.config import WriterConfig

    cfg.writer = WriterConfig(use_token_count_api=True)
    result = count_tokens_api(cfg, "classifier", [{"role": "user", "content": "hi"}])
    assert result is None


# ── Test 7: _truncate_result 与真实 tool response 集成 ────────────────────


def test_make_tool_response_truncates_large_result():
    """make_tool_response 对超大 tool result 截断到 max_bytes 内。"""
    from persome.writer.llm import make_tool_response

    assistant_msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_x",
                "type": "function",
                "function": {"name": "search_memory", "arguments": "{}"},
            }
        ],
    }
    big_result = {"entries": "x" * 50_000}
    msg = make_tool_response(assistant_msg, 0, "search_memory", big_result, max_bytes=16_384)
    assert len(msg["content"].encode()) <= 16_384
    parsed = json.loads(msg["content"])
    assert parsed.get("_truncated") is True


# ── Test 8: F3 输出截断恢复 ────────────────────────────────────────────────


def test_f3_output_truncation_recovery():
    """max_tokens=10 强制 finish_reason='length'，验证 continuation prompt 被注入。"""
    from persome.config import WriterConfig
    from persome.writer.llm import run_tool_loop

    cfg = _make_cfg(max_tokens=10)
    cfg.writer = WriterConfig(
        llm_retry_attempts=2,
        max_output_tokens_recovery_count=2,
        max_output_tokens_recovery_limit=65_536,
    )
    # 重新设置模型（WriterConfig 重置后需恢复）
    from persome.config import ModelConfig

    cfg.models["default"] = ModelConfig(model=MODEL, max_tokens=10)

    messages = [
        {"role": "system", "content": "You are a poet."},
        {"role": "user", "content": "Write a 200-word poem about the ocean."},
    ]
    state = _State()
    run_tool_loop(
        cfg,
        "classifier",
        messages,
        tools=[],
        dispatch_fn=lambda n, a: {},
        valid_tool_names=set(),
        state=state,
        max_iter=5,
    )

    # continuation prompt 应被注入到 messages
    user_contents = [m.get("content") or "" for m in messages if m["role"] == "user"]
    continuation_injected = any("Continue directly" in c for c in user_contents)
    assert continuation_injected, (
        "Expected continuation prompt to be injected after length truncation. "
        f"User messages: {user_contents}"
    )


# ── Test 9: F4 多轮 token 成本累计 ────────────────────────────────────────


def test_f4_cost_accumulates_across_iterations(monkeypatch):
    """两轮 LLM 调用，extract_usage 被调用 2 次，累计 prompt_tokens > 单轮。"""
    from persome.writer import llm as llm_mod
    from persome.writer.cost import calculate_usd
    from persome.writer.llm import run_tool_loop

    usage_records: list[dict] = []
    real_extract_usage = llm_mod.extract_usage

    def _spy_usage(resp):
        result = real_extract_usage(resp)
        if result:
            usage_records.append(result)
        return result

    monkeypatch.setattr(llm_mod, "extract_usage", _spy_usage)

    tool_schema = [
        {
            "type": "function",
            "function": {
                "name": "search_memory",
                "description": "Search user memory.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        }
    ]

    cfg = _make_cfg(max_tokens=128)
    messages = [
        {
            "role": "system",
            "content": "Use search_memory to find user info, then answer briefly.",
        },
        {"role": "user", "content": "What do you know about me?"},
    ]
    state = _State()
    run_tool_loop(
        cfg,
        "classifier",
        messages,
        tools=tool_schema,
        dispatch_fn=lambda n, a: {"entries": ["software engineer"]},
        valid_tool_names={"search_memory"},
        state=state,
        max_iter=4,
    )

    # extract_usage 应被调用 ≥2 次（第1次 LLM call + 工具调用后的第2次）
    assert len(usage_records) >= 2, f"Expected ≥2 extract_usage calls, got {len(usage_records)}"
    total_prompt = sum(u["prompt_tokens"] for u in usage_records)
    total_completion = sum(u["completion_tokens"] for u in usage_records)
    # 累计 prompt tokens 应多于第一次单独调用（第二轮输入包含工具结果，更多 tokens）
    assert total_prompt > usage_records[0]["prompt_tokens"]
    # cost 应为正数
    cost = calculate_usd(
        "deepseek-chat",
        {
            "prompt_tokens": total_prompt,
            "completion_tokens": total_completion,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        },
    )
    assert cost is not None and cost > 0


# ── Test 10: F6 真实 LLM 并发 safe tool 执行 ──────────────────────────────


def test_f6_parallel_safe_tools_with_real_llm():
    """LLM 同时发出 2 个 search_memory 调用时，验证两个 start 事件先于第一个 end 事件。"""
    import time

    from persome.writer.llm import run_tool_loop

    tool_schema = [
        {
            "type": "function",
            "function": {
                "name": "search_memory",
                "description": "Search user memory for a specific topic.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "topic to search"}},
                    "required": ["query"],
                },
            },
        }
    ]

    execution_order: list[str] = []
    lock = threading.Lock()

    def _parallel_dispatch(name: str, args: dict) -> dict:
        q = args.get("query", "?")
        with lock:
            execution_order.append(f"start:{q}")
        time.sleep(0.05)
        with lock:
            execution_order.append(f"end:{q}")
        return {"entries": [f"result for {q}"]}

    cfg = _make_cfg(max_tokens=256)
    messages = [
        {
            "role": "system",
            "content": (
                "You MUST call search_memory TWICE simultaneously in a single response: "
                "once with query='hobbies' and once with query='projects'. "
                "Do not call them sequentially."
            ),
        },
        {"role": "user", "content": "Tell me about my hobbies and projects."},
    ]
    state = _State()
    run_tool_loop(
        cfg,
        "classifier",
        messages,
        tools=tool_schema,
        dispatch_fn=lambda n, a: {"entries": []},
        valid_tool_names={"search_memory"},
        state=state,
        max_iter=4,
        parallel_dispatch_fn=_parallel_dispatch,
    )

    starts = [e for e in execution_order if e.startswith("start:")]
    ends = [e for e in execution_order if e.startswith("end:")]

    if len(starts) < 2 or len(ends) < 2:
        pytest.skip(
            f"LLM issued only {len(starts)} search_memory call(s) in one turn; "
            "parallel execution could not be verified"
        )

    # 两个 start 都先于第一个 end
    first_end_idx = next(i for i, e in enumerate(execution_order) if e.startswith("end:"))
    starts_before_first_end = sum(
        1 for e in execution_order[:first_end_idx] if e.startswith("start:")
    )
    assert starts_before_first_end == 2, (
        f"Expected both tools to start before either ended, got: {execution_order}"
    )


# ── Test 11: F7 真实 args Pydantic 验证流转 ───────────────────────────────


def test_f7_real_llm_args_pass_pydantic_validation():
    """LLM 调用 search_memory，args 经 _validate_tool_args 验证通过后正常执行。"""
    from persome.writer.llm import run_tool_loop
    from persome.writer.tools import _validate_tool_args

    tool_schema = [
        {
            "type": "function",
            "function": {
                "name": "search_memory",
                "description": "Search user memory.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        }
    ]

    validation_passed: list[str] = []
    validation_errors: list[str] = []

    def _dispatch(name: str, args: dict) -> dict:
        err = _validate_tool_args(name, args)
        if err is not None:
            validation_errors.append(f"{name}: {err}")
        else:
            validation_passed.append(name)
        return {"entries": ["software engineer"]}

    cfg = _make_cfg(max_tokens=128)
    messages = [
        {"role": "system", "content": "Use search_memory to find user info, then answer."},
        {"role": "user", "content": "Who am I?"},
    ]
    state = _State()
    run_tool_loop(
        cfg,
        "classifier",
        messages,
        tools=tool_schema,
        dispatch_fn=_dispatch,
        valid_tool_names={"search_memory"},
        state=state,
        max_iter=4,
    )

    assert len(validation_errors) == 0, (
        f"Pydantic validation failed for LLM-generated args: {validation_errors}"
    )
    assert len(validation_passed) >= 1, "Expected at least one tool call to be validated"
    assert "search_memory" in validation_passed

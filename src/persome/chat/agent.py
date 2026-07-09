"""Anthropic SDK-based agent loop for interactive chat."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, TypeAlias

import anthropic
from anthropic import AsyncAnthropic
from anthropic.lib.tools import BetaAsyncFunctionTool
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from .. import config as config_mod
from ..logger import get as _get_logger
from ..trace import get_trace_id
from .tools import to_anthropic_tools

_logger = _get_logger("persome.chat")

# Per-tool-call result size cap. Truncation happens before the result is
# sent back to the model, so the cap also bounds context growth.
_MAX_TOOL_RESULT_BYTES = 50_000

_OnTokenT: TypeAlias = Callable[[str], Awaitable[None]]
_OnToolCallT: TypeAlias = Callable[[str, dict[str, Any], str, float], Awaitable[None]]


def complete_sync(
    chat_cfg: config_mod.ChatConfig,
    messages: list[dict[str, Any]],
    *,
    max_tokens: int = 2048,
) -> str:
    """One-shot synchronous Anthropic call. Returns the first text block.

    Shared by all auxiliary LLM callers (compression, away summary,
    memory extraction) so provider config is resolved in one place.
    """
    client = anthropic.Anthropic(
        api_key=config_mod.provider_api_key("anthropic"),
        base_url=config_mod.provider_base_url("anthropic"),
    )
    extra: dict[str, str] = {}
    tid = get_trace_id()
    if tid:
        extra["X-Trace-Id"] = tid
    msg = client.messages.create(
        model=chat_cfg.model,
        messages=messages,  # type: ignore[arg-type]
        max_tokens=max_tokens,
        extra_headers=extra or None,
    )
    for block in msg.content:
        if hasattr(block, "text"):
            return block.text
    return ""


@dataclass
class AgentTurnResult:
    """What ChatAgent produces from one user turn.

    Compression/microcompact state lives in the chat-layer wrapper; this
    dataclass only carries fields the agent itself observed.
    """

    messages: list[dict[str, Any]]
    assistant_message: str = ""
    tool_calls_executed: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    # Time-to-first-token: turn start → first text/thinking delta, in ms.
    # ``None`` when the turn errored before any token streamed back.
    ttft_ms: float | None = None


def _to_anthropic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter & normalize chat history into valid Anthropic input.

    - Drops system messages (system text goes via the ``system=`` param).
    - Drops legacy ``role == "tool"`` and orphan ``tool_calls`` shaped messages
      that came from the old litellm/OpenAI format.
    - Preserves messages with list-of-content-blocks (tool_use / tool_result)
      produced by ChatAgent itself.
    - Places a cache breakpoint on the last block of the most recent message
      so multi-turn conversations get incremental prompt-cache hits.
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        content = m.get("content")
        if isinstance(content, (str, list)) and content:
            out.append({"role": role, "content": content})
    if out:
        out[-1] = _with_terminal_cache_breakpoint(out[-1])
    return out


def _with_terminal_cache_breakpoint(msg: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``msg`` with cache_control on its last content block.

    Normalises ``content`` to a list-of-blocks when it was a bare string. The
    original message dict is left untouched so the persisted chat history
    keeps its plain shape.
    """
    content = msg["content"]
    if isinstance(content, str):
        new_content = [
            {
                "type": "text",
                "text": content,
                "cache_control": {"type": "ephemeral"},
            }
        ]
    else:
        new_content = [dict(block) if isinstance(block, dict) else block for block in content]
        if new_content and isinstance(new_content[-1], dict):
            new_content[-1] = {**new_content[-1], "cache_control": {"type": "ephemeral"}}
    return {**msg, "content": new_content}


def _blocks_to_dicts(content: list[Any]) -> list[dict[str, Any]]:
    """Convert SDK content blocks to plain dicts suitable for the messages list."""
    out: list[dict[str, Any]] = []
    for b in content:
        t = getattr(b, "type", None)
        if t == "text":
            out.append({"type": "text", "text": getattr(b, "text", "")})
        elif t == "tool_use":
            inp = getattr(b, "input", {})
            out.append(
                {
                    "type": "tool_use",
                    "id": b.id,
                    "name": b.name,
                    "input": inp if isinstance(inp, dict) else {},
                }
            )
    return out


def _make_async_sdk_tool(
    name: str,
    description: str,
    input_schema: dict[str, Any],
    sync_handler: Callable[[dict[str, Any]], Any],
    on_tool_call: _OnToolCallT | None = None,
    *,
    cache_control: dict[str, Any] | None = None,
) -> BetaAsyncFunctionTool:
    """Wrap a sync handler as a tool_runner-compatible BetaAsyncFunctionTool."""

    async def executor(**kwargs: Any) -> str:
        t0 = time.monotonic()
        try:
            result = await asyncio.to_thread(sync_handler, kwargs)
        except Exception as exc:
            result = {"error": f"{type(exc).__name__}: {exc}"}
        elapsed = (time.monotonic() - t0) * 1000
        result_str = json.dumps(result, ensure_ascii=False, default=str)
        if len(result_str) > _MAX_TOOL_RESULT_BYTES:
            truncated = len(result_str) - _MAX_TOOL_RESULT_BYTES
            result_str = result_str[:_MAX_TOOL_RESULT_BYTES] + f"\n...(truncated {truncated} bytes)"
        _logger.info("tool %s elapsed=%.0fms size=%d", name, elapsed, len(result_str))
        if on_tool_call:
            await on_tool_call(name, kwargs, result_str, elapsed)
        return result_str

    kwargs: dict[str, Any] = dict(name=name, description=description, input_schema=input_schema)
    if cache_control is not None:
        kwargs["cache_control"] = cache_control
    return BetaAsyncFunctionTool(executor, **kwargs)


def _make_mcp_tracking_tool(
    t: Any,
    session: ClientSession,
    on_tool_call: _OnToolCallT | None = None,
    *,
    cache_control: dict[str, Any] | None = None,
) -> BetaAsyncFunctionTool:
    """Wrap an MCP tool as a BetaAsyncFunctionTool, calling session.call_tool directly."""
    tool_name = t.name

    async def executor(**kwargs: Any) -> str:
        t0 = time.monotonic()
        try:
            call_result = await session.call_tool(name=tool_name, arguments=kwargs or {})
            if call_result.isError:
                result_str = json.dumps({"error": str(call_result.content)})
            else:
                parts = [item.text for item in call_result.content if hasattr(item, "text")]
                result_str = "\n".join(parts)
        except Exception as exc:
            result_str = json.dumps({"error": f"{type(exc).__name__}: {exc}"})
        if len(result_str) > _MAX_TOOL_RESULT_BYTES:
            truncated = len(result_str) - _MAX_TOOL_RESULT_BYTES
            result_str = result_str[:_MAX_TOOL_RESULT_BYTES] + f"\n...(truncated {truncated} bytes)"
        elapsed = (time.monotonic() - t0) * 1000
        _logger.info("mcp tool %s elapsed=%.0fms size=%d", tool_name, elapsed, len(result_str))
        if on_tool_call:
            await on_tool_call(tool_name, kwargs, result_str, elapsed)
        return result_str

    input_schema = dict(t.inputSchema) if t.inputSchema else {"type": "object", "properties": {}}
    kwargs: dict[str, Any] = dict(
        name=t.name, description=t.description or "", input_schema=input_schema
    )
    if cache_control is not None:
        kwargs["cache_control"] = cache_control
    return BetaAsyncFunctionTool(executor, **kwargs)


def _accumulate_usage(msg_usage: Any, usage: dict[str, int]) -> None:
    if not msg_usage:
        return
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    ):
        val = getattr(msg_usage, key, None)
        if val is not None:
            usage[key] = usage.get(key, 0) + int(val)


class ChatAgent:
    """Agentic chat loop backed by the Anthropic Python SDK tool_runner.

    Each call to ``run_turn`` uses ``client.beta.messages.tool_runner(stream=True)``
    which handles the multi-round tool-call loop internally. Per-token streaming
    is preserved — each round yields a ``BetaAsyncMessageStream`` that we iterate
    for token events.

    Tools included per turn:
      - Built-in handlers (sync, wrapped via ``_make_async_sdk_tool``)
      - Skill handlers (same path as built-ins)
      - MCP server tools (via ``_make_mcp_tracking_tool``, connected in ``aopen()``)
    """

    def __init__(
        self,
        cfg: config_mod.ChatConfig,
        all_schemas: list[dict[str, Any]],
        all_handlers: dict[str, Callable[[dict[str, Any]], Any]],
        *,
        daemon_mcp_url: str = "",
    ) -> None:
        self.client = AsyncAnthropic(
            api_key=config_mod.provider_api_key("anthropic"),
            base_url=config_mod.provider_base_url("anthropic"),
        )
        self.model = cfg.model
        self._cfg = cfg
        self._sdk_schemas = to_anthropic_tools(all_schemas)
        self._handlers = all_handlers
        self._daemon_mcp_url = daemon_mcp_url
        self._mcp_sessions: list[ClientSession] = []
        self._exit_stacks: list[AsyncExitStack] = []
        # Cached (tool_spec, session) pairs populated by aopen(); avoids
        # paying list_tools() latency on every run_turn() call.
        self._mcp_tool_specs: list[tuple[Any, ClientSession]] = []

    async def _connect_session(self, stack: AsyncExitStack, read: Any, write: Any) -> ClientSession:
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._exit_stacks.append(stack)
        return session

    async def _open_http_session(self, url: str) -> ClientSession:
        stack = AsyncExitStack()
        try:
            read, write, _ = await stack.enter_async_context(streamable_http_client(url))
            return await self._connect_session(stack, read, write)
        except Exception:
            await stack.aclose()
            raise

    async def _open_stdio_session(self, command: str, args: list[str]) -> ClientSession:
        stack = AsyncExitStack()
        try:
            params = StdioServerParameters(command=command, args=args)
            read, write = await stack.enter_async_context(stdio_client(params))
            return await self._connect_session(stack, read, write)
        except Exception:
            await stack.aclose()
            raise

    async def aopen(self) -> None:
        """Connect to all configured MCP servers and cache their tool listings."""
        if self._daemon_mcp_url:
            try:
                session = await self._open_http_session(self._daemon_mcp_url)
                self._mcp_sessions.append(session)
                _logger.info("connected to daemon MCP at %s", self._daemon_mcp_url)
            except Exception:
                _logger.warning(
                    "failed to connect to daemon MCP at %s", self._daemon_mcp_url, exc_info=True
                )

        for spec in self._cfg.mcp_servers:
            target = spec.url if spec.type == "http" else spec.command
            try:
                if spec.type == "http":
                    session = await self._open_http_session(spec.url)
                else:
                    session = await self._open_stdio_session(spec.command, spec.args)
                self._mcp_sessions.append(session)
                _logger.info("connected to MCP server %s", target)
            except Exception:
                _logger.warning("failed to connect to MCP server %s", target, exc_info=True)

        self._mcp_tool_specs = []
        for session in self._mcp_sessions:
            try:
                res = await session.list_tools()
                for t in res.tools:
                    self._mcp_tool_specs.append((t, session))
            except Exception:
                _logger.warning("MCP list_tools failed", exc_info=True)

    async def aclose(self) -> None:
        """Close all MCP sessions and the underlying httpx client."""
        for stack in reversed(self._exit_stacks):
            try:
                await stack.aclose()
            except Exception:
                _logger.warning("error closing MCP exit stack", exc_info=True)
        self._mcp_sessions.clear()
        self._exit_stacks.clear()
        self._mcp_tool_specs.clear()
        await self.client.close()

    async def run_turn(
        self,
        messages: list[dict[str, Any]],
        system: str,
        *,
        on_token: _OnTokenT | None = None,
        on_thinking: _OnTokenT | None = None,
        on_tool_call: _OnToolCallT | None = None,
        max_tokens: int = 8192,
        thinking_budget: int = 0,
    ) -> AgentTurnResult:
        tool_calls_executed: list[dict[str, Any]] = []
        round_results: list[str] = []
        final_text = ""
        usage: dict[str, int] = {}

        async def _tracked_on_tool(
            name: str, kwargs: dict[str, Any], result_str: str, elapsed: float
        ) -> None:
            tool_calls_executed.append({"name": name, "arguments": kwargs})
            # Append here so both static and MCP tools go through one path.
            round_results.append(result_str)
            if on_tool_call:
                await on_tool_call(name, kwargs, result_str, elapsed)

        # Build the deduplicated, name-sorted source list before instantiating
        # tools. Sort key prefers static over MCP for duplicate names (static
        # is authoritative). Sorting by name keeps the rendered tool order
        # stable across MCP server restarts — critical for prompt-cache hits,
        # since any tool reordering invalidates the entire tools/system cache.
        sources: list[tuple[str, str, Any]] = []
        for s in self._sdk_schemas:
            if s["name"] in self._handlers:
                sources.append(("static", s["name"], s))
        for t, session in self._mcp_tool_specs:
            sources.append(("mcp", t.name, (t, session)))
        sources.sort(key=lambda x: (x[1], 0 if x[0] == "static" else 1))

        deduped: list[tuple[str, str, Any]] = []
        seen: set[str] = set()
        for kind, name, payload in sources:
            if name in seen:
                _logger.warning("dropping duplicate tool name %r (kind=%s)", name, kind)
                continue
            seen.add(name)
            deduped.append((kind, name, payload))

        # Place a cache breakpoint on the LAST tool. Since `tools` renders
        # before `system`, the marker on the last tool block caches the entire
        # tool list. We also place a marker on the system block below — both
        # markers cooperate (max 4 breakpoints per request).
        combined_tools: list[BetaAsyncFunctionTool] = []
        for idx, (kind, _name, payload) in enumerate(deduped):
            cache_ctl = {"type": "ephemeral"} if idx == len(deduped) - 1 else None
            if kind == "static":
                s = payload
                combined_tools.append(
                    _make_async_sdk_tool(
                        s["name"],
                        s.get("description", ""),
                        s.get("input_schema", {}),
                        self._handlers[s["name"]],
                        _tracked_on_tool,
                        cache_control=cache_ctl,
                    )
                )
            else:
                t, session = payload
                combined_tools.append(
                    _make_mcp_tracking_tool(t, session, _tracked_on_tool, cache_control=cache_ctl)
                )

        initial_anthropic_messages = _to_anthropic_messages(messages)

        # Wrap the bare system string in a list-of-blocks with an ephemeral
        # cache_control marker. This caches the (sorted-tools + system) prefix
        # across requests — multi-turn conversations should see >90% cache
        # hit rate on input tokens after the first turn.
        system_param: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }
        ]

        # Extended thinking is an opt-in Anthropic feature. When enabled the
        # model can emit `thinking` content blocks that arrive as
        # `thinking_delta` stream events; we surface those via `on_thinking`.
        # `budget_tokens` must be >= 1024 per the API spec.
        tid = get_trace_id()
        runner_kwargs: dict[str, Any] = dict(
            model=self.model,
            system=system_param,
            messages=initial_anthropic_messages,
            tools=combined_tools,
            max_tokens=max_tokens,
            stream=True,
            extra_headers={"X-Trace-Id": tid} if tid else None,
        )
        if thinking_budget and thinking_budget >= 1024:
            runner_kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": thinking_budget,
            }

        runner = self.client.beta.messages.tool_runner(**runner_kwargs)

        # State for round-by-round history reconstruction.
        # We defer committing each round's messages until the START of the
        # NEXT iteration, because tool executors run BETWEEN iterations —
        # after the body of `async for stream in runner` exits and before
        # the next stream is yielded.  At that point round_results is fully
        # populated and we can zip it with pending_tool_ids.
        pending_assistant_blocks: list[dict[str, Any]] | None = None
        pending_tool_ids: list[str] = []

        # TTFT: measured from just before the first wire request to the first
        # streamed text/thinking delta. `perf_counter` is monotonic so it is
        # immune to wall-clock adjustments. Recorded once; later tool-loop
        # rounds don't reset it (TTFT is the user-perceived first-token wait).
        turn_start = time.perf_counter()
        ttft_ms: float | None = None

        try:
            _logger.info("turn start messages=%d", len(messages))

            async for stream in runner:
                # Commit the previous round now that its tools have run.
                if pending_assistant_blocks is not None:
                    messages.append({"role": "assistant", "content": pending_assistant_blocks})
                    if pending_tool_ids:
                        messages.append(
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": tid,
                                        "content": res,
                                    }
                                    for tid, res in zip(
                                        pending_tool_ids, round_results, strict=False
                                    )
                                ],
                            }
                        )
                        round_results.clear()
                        pending_tool_ids = []

                async for event in stream:
                    event_type = getattr(event, "type", None)
                    if event_type == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        delta_type = getattr(delta, "type", None) if delta else None
                        if delta_type == "text_delta":
                            tok = getattr(delta, "text", "") or ""
                            if tok:
                                if ttft_ms is None:
                                    ttft_ms = (time.perf_counter() - turn_start) * 1000
                                final_text += tok
                                if on_token:
                                    await on_token(tok)
                        elif delta_type == "thinking_delta":
                            chunk = getattr(delta, "thinking", "") or ""
                            if chunk:
                                if ttft_ms is None:
                                    ttft_ms = (time.perf_counter() - turn_start) * 1000
                                if on_thinking:
                                    await on_thinking(chunk)

                final_msg = await stream.get_final_message()
                _accumulate_usage(getattr(final_msg, "usage", None), usage)

                pending_assistant_blocks = _blocks_to_dicts(final_msg.content)
                pending_tool_ids = [
                    b.id for b in final_msg.content if getattr(b, "type", None) == "tool_use"
                ]

            # Commit the final assistant response (no tool calls follow).
            if pending_assistant_blocks is not None:
                messages.append({"role": "assistant", "content": pending_assistant_blocks})

            # `extra={"ttft_ms": ...}` is hoisted into the JSON-line log's
            # `extra` map by logger.py::JsonFormatter, so the diagnostic bundle
            # exposes it via `jq '.extra.ttft_ms'`.
            _logger.info(
                "turn end tools=%d usage=%s ttft_ms=%s",
                len(tool_calls_executed),
                usage,
                f"{ttft_ms:.0f}" if ttft_ms is not None else "n/a",
                extra={"ttft_ms": round(ttft_ms, 1)} if ttft_ms is not None else {},
            )

        except Exception as exc:
            _logger.exception("turn error: %s", exc)
            return AgentTurnResult(
                messages=messages,
                assistant_message=final_text,
                tool_calls_executed=tool_calls_executed,
                usage=usage,
                error=str(exc),
                ttft_ms=ttft_ms,
            )

        return AgentTurnResult(
            messages=messages,
            assistant_message=final_text,
            tool_calls_executed=tool_calls_executed,
            usage=usage,
            ttft_ms=ttft_ms,
        )

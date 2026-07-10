"""Interactive chat with memory-aware tool calling."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown

from .. import config as config_mod
from ..logger import get as _get_logger
from . import history as chat_history
from .agent import AgentTurnResult, ChatAgent, _OnTokenT, _OnToolCallT, complete_sync
from .memory_extractor import maybe_extract as maybe_extract_memories
from .skills import LoadedSkills, load_all_skills
from .tool_handlers import TOOL_HANDLERS
from .tools import CHAT_SCHEMA_NAMES, CHAT_SCHEMAS, SAFE_CHAT_SCHEMA_NAMES

_logger = _get_logger("persome.chat")

# Quiet noisy library logs in interactive chat mode
logging.getLogger("httpx").setLevel(logging.ERROR)


console = Console()


def _load_skills() -> LoadedSkills:
    """Load all skills and return the LoadedSkills object."""
    return load_all_skills(builtin_names=CHAT_SCHEMA_NAMES)


def _load_system_prompt(loaded: LoadedSkills | None = None) -> str:
    from ..prompts import load as load_prompt

    base = load_prompt("chat.md")
    if loaded is None:
        loaded = _load_skills()
    if loaded.index_prompt:
        return base + "\n" + loaded.index_prompt
    return base


# ─── tool execution ───────────────────────────────────────────────────────


def _exec_tool(
    name: str,
    args: dict[str, Any],
    extra_handlers: dict[str, Any] | None = None,
) -> str:
    """Synchronous tool dispatcher kept for tests and the API helper.

    The streaming ChatAgent has its own async dispatcher with timing /
    callbacks; this one is the bare logic both share.
    """
    handler = TOOL_HANDLERS.get(name)
    if handler is None and extra_handlers:
        handler = extra_handlers.get(name)
    if handler is None:
        return json.dumps({"error": f"unknown tool: {name}"})
    try:
        result = handler(args)
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as exc:
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})


# ─── conversation compression ─────────────────────────────────────────────

_KEEP_RECENT_TURNS = 20  # keep the last N user/assistant pairs after compression
_MAX_CONSECUTIVE_COMPACT_FAILURES = 3
# Default context window when model does not declare one.
_DEFAULT_CONTEXT_WINDOW = 200_000
# Output token reservation: we always reserve headroom for a full model response.
_MAX_OUTPUT_RESERVE = 20_000
# Estimated max growth of a single turn (output + tool result spike).
_TURN_GROWTH_ESTIMATE = 15_000


def _load_compress_prompt() -> str:
    from ..prompts import load as load_prompt

    return load_prompt("chat_compress.md")


def _get_context_window(model: str) -> int:
    """Return the context window size for the given model string.

    Takes a bare model identifier so the same heuristic works whether the
    string came from ``cfg.chat.model`` or a ``[models.*]`` stage — both
    now go through the Anthropic SDK.
    """
    if "[1m]" in model or "1m" in model.lower():
        return 1_000_000
    model_lower = model.lower()
    if "claude-3-opus" in model_lower or "claude-opus" in model_lower:
        return 200_000
    if "claude-3-5-sonnet" in model_lower or "claude-sonnet-4" in model_lower:
        return 200_000
    if "claude-3-haiku" in model_lower:
        return 200_000
    if "gpt-4-turbo" in model_lower or "gpt-4-0125" in model_lower:
        return 128_000
    if "gpt-4o" in model_lower:
        return 128_000
    if "deepseek" in model_lower:
        return 64_000
    return _DEFAULT_CONTEXT_WINDOW


def _get_compress_threshold(context_window: int) -> int:
    """Dynamic buffer based on context window size.

    Larger windows get larger absolute buffers so that a single turn
    (output + tool results) does not push us over the edge.
    """
    if context_window >= 800_000:
        buffer_tokens = 50_000
    elif context_window >= 400_000:
        buffer_tokens = 30_000
    else:
        buffer_tokens = 13_000
    effective = context_window - min(_MAX_OUTPUT_RESERVE, context_window // 10)
    return max(effective - buffer_tokens, effective // 2)


def _estimate_tokens(
    messages: list[dict[str, Any]],
    last_usage: dict[str, int] | None = None,
) -> int:
    """Estimate token count, anchoring on the last known API usage when available.

    If ``last_usage`` is provided we trust its ``prompt_tokens`` as a baseline
    and only estimate messages that arrived *after* that API call.  This avoids
    cumulative drift from cheap character-count heuristics.
    """
    if last_usage:
        baseline = last_usage.get("prompt_tokens", 0)
        # Heuristic: the API counts tokens more accurately than we can.
        # Add a small fudge for the messages we have added since that call.
        added = _rough_token_estimate(messages[-2:]) if len(messages) >= 2 else 0
        return baseline + added
    return _rough_token_estimate(messages)


def _rough_token_estimate(messages: list[dict[str, Any]]) -> int:
    """Cheap character-based token estimate (~4 chars / token)."""
    total = 0
    for m in messages:
        content = m.get("content") or ""
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")
                if btype == "text":
                    total += len(block.get("text", "")) // 4
                elif btype == "tool_use":
                    total += len(json.dumps(block.get("input", {}))) // 4
                elif btype == "tool_result":
                    rc = block.get("content", "")
                    if isinstance(rc, str):
                        total += len(rc) // 4
                    elif isinstance(rc, list):
                        for rb in rc:
                            if isinstance(rb, dict):
                                total += len(rb.get("text", "")) // 4
        elif isinstance(content, str):
            total += len(content) // 4
        # Legacy OpenAI-format tool_calls
        for tc in m.get("tool_calls", []):
            fn = tc.get("function", {})
            total += len(fn.get("arguments", "")) // 4
    return total


def _find_compress_boundary(messages: list[dict[str, Any]]) -> int:
    """Find the index to split at: keep system + compress older + keep recent.

    Returns the index where 'recent' messages start (everything before gets
    compressed). Returns -1 if no compression is needed.
    """
    user_indices = [i for i, m in enumerate(messages) if m["role"] == "user"]
    if len(user_indices) <= _KEEP_RECENT_TURNS:
        return -1
    return user_indices[-_KEEP_RECENT_TURNS]


_CURRENT_TIME_PREFIX_RE = re.compile(r"^\[Current time: [^\]]+\]\n\n")


def _strip_time_prefix(content: str) -> str:
    """Remove the [Current time: ...] prefix added to user messages."""
    return _CURRENT_TIME_PREFIX_RE.sub("", content, count=1)


def _format_for_summary(messages: list[dict[str, Any]]) -> str:
    """Format messages into readable text for the summary LLM call."""
    lines: list[str] = []
    for m in messages:
        role = m["role"]
        content = m.get("content") or ""
        if role == "user":
            lines.append(f"User: {_strip_time_prefix(content)}")
        elif role == "assistant":
            if content:
                lines.append(f"Assistant: {content}")
            for tc in m.get("tool_calls", []):
                fn = tc.get("function", {})
                lines.append(f"  [called {fn.get('name', '?')}]")
        elif role == "tool":
            # Truncate long tool results in summary input
            preview = content[:500] + "..." if len(content) > 500 else content
            lines.append(f"  [tool result: {preview}]")
    return "\n".join(lines)


def _format_compact_summary(summary: str) -> str:
    """Strip analysis block and extract summary content from XML tags."""
    formatted = summary

    # Strip analysis section — it's a drafting scratchpad
    formatted = re.sub(r"<analysis>[\s\S]*?</analysis>", "", formatted)

    # Extract and format summary section
    match = re.search(r"<summary>([\s\S]*?)</summary>", formatted)
    if match:
        content = match.group(1) or ""
        formatted = f"Summary:\n{content.strip()}"
    else:
        # Fallback: if no XML tags, use the raw text
        formatted = formatted.strip()

    # Clean up extra whitespace between sections
    formatted = re.sub(r"\n\n+", "\n\n", formatted)
    return formatted.strip()


def _compress_messages(
    cfg: config_mod.Config,
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Core compression: summarize old messages and rebuild the context."""
    boundary = _find_compress_boundary(messages)
    if boundary == -1:
        return messages

    system_msg = messages[0]  # system prompt
    old_messages = messages[1:boundary]  # to be compressed
    recent_messages = messages[boundary:]  # to keep

    conversation_text = _format_for_summary(old_messages)
    try:
        summary = complete_sync(
            cfg.chat,
            messages=[
                {
                    "role": "user",
                    "content": _load_compress_prompt().format(conversation=conversation_text),
                }
            ],
        ).strip()
    except Exception:
        _logger.warning("compression LLM call failed", exc_info=True)
        summary = "(Earlier conversation was truncated due to length.)"

    formatted_summary = _format_compact_summary(summary)
    summary_content = (
        "This session is being continued from a previous conversation that ran out of context. "
        "The summary below covers the earlier portion of the conversation.\n\n"
        f"{formatted_summary}\n\n"
        "Continue the conversation from where it left off without asking the user any further questions. "
        "Resume directly — do not acknowledge the summary, do not recap what was happening, "
        'do not preface with "I\'ll continue" or similar. Pick up the last task as if the break never happened.'
    )
    summary_msg = {
        "role": "system",
        "content": summary_content,
    }
    return [system_msg, summary_msg] + recent_messages


def _should_compress(
    messages: list[dict[str, Any]],
    model: str,
    max_output_tokens: int,
    last_usage: dict[str, int] | None,
    consecutive_failures: int,
) -> bool:
    """Predictive compression check: compact *before* the next turn pushes us over."""
    if consecutive_failures >= _MAX_CONSECUTIVE_COMPACT_FAILURES:
        return False

    context_window = _get_context_window(model)
    threshold = _get_compress_threshold(context_window)
    current = _estimate_tokens(messages, last_usage)

    estimated_growth = min(max_output_tokens, _MAX_OUTPUT_RESERVE) + _TURN_GROWTH_ESTIMATE
    return (current + estimated_growth) >= threshold


def _compress_context(
    cfg: config_mod.Config,
    messages: list[dict[str, Any]],
    *,
    last_usage: dict[str, int] | None = None,
    consecutive_failures: int = 0,
) -> tuple[list[dict[str, Any]], int, bool]:
    """Compress older messages into a summary, keeping recent turns intact.

    Sizing is based on ``cfg.chat.model`` — that's the model the ChatAgent
    actually talks to, not whatever ``[models.chat]`` happens to be set to
    for the compression LLM call.
    """
    max_output_tokens = cfg.model_for("chat").max_tokens or 8192
    if not _should_compress(
        messages,
        cfg.chat.model,
        max_output_tokens,
        last_usage,
        consecutive_failures,
    ):
        return messages, consecutive_failures, False

    try:
        result = _compress_messages(cfg, messages)
        return result, 0, True
    except Exception:
        return messages, consecutive_failures + 1, False


# ─── away summary ─────────────────────────────────────────────────────────

_AWAY_SUMMARY_GAP_MINUTES = 30
_MICROCOMPACT_GAP_SECONDS = 300  # 5 minutes


def _load_away_summary_prompt() -> str:
    from ..prompts import load as load_prompt

    return load_prompt("chat_away_summary.md")


def _parse_session_marker_timestamp(marker_content: str | None) -> datetime | None:
    """Extract datetime from a session marker like '[SESSION EXIT at 2026-05-15 14:30:00]'."""
    if not marker_content:
        return None
    match = re.search(r"\[SESSION (EXIT|RESUME) at ([^\]]+)\]", marker_content)
    if match:
        try:
            return datetime.strptime(match.group(2), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return None


def _maybe_away_summary(prev: list[dict[str, Any]], cfg: config_mod.Config) -> str | None:
    """If the user was away for >30 min, generate a 1-3 sentence recap."""
    if not prev:
        return None

    last_msg = prev[-1]
    if not last_msg.get("_session_marker") or "EXIT" not in (last_msg.get("content") or ""):
        return None

    exit_time = _parse_session_marker_timestamp(last_msg.get("content", ""))
    if not exit_time:
        return None

    gap_minutes = (datetime.now() - exit_time).total_seconds() / 60
    if gap_minutes < _AWAY_SUMMARY_GAP_MINUTES:
        return None

    # Only user + assistant messages are relevant for the summary
    visible = [m for m in prev[:-1] if m.get("role") in ("user", "assistant")]
    to_summarize = visible[-30:]
    if not to_summarize:
        return None

    conversation = _format_for_summary(to_summarize)
    prompt = _load_away_summary_prompt().format(conversation=conversation)

    try:
        summary = complete_sync(
            cfg.chat,
            messages=[{"role": "user", "content": prompt}],
        ).strip()
        if summary:
            return (
                "This session is being continued from a previous conversation that ran out of context. "
                "The summary below covers the earlier portion of the conversation.\n\n"
                f"{summary}\n\n"
                "Continue the conversation from where it left off without asking the user any further questions. "
                "Resume directly — do not acknowledge the summary, do not recap what was happening, "
                'do not preface with "I\'ll continue" or similar. Pick up the last task as if the break never happened.'
            )
    except Exception:
        pass
    return None


# ─── time-based microcompact ──────────────────────────────────────────────


def _microcompact_on_resume(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace old tool results with placeholders when resuming after a break."""
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    to_compact = tool_indices[:-2]  # keep last 2
    for i in to_compact:
        if not messages[i].get("content", "").startswith("[Previous tool result"):
            messages[i]["content"] = "[Previous tool result cleared due to inactivity]"
    return messages


def _microcompact(
    messages: list[dict[str, Any]], last_assistant_time: float | None
) -> tuple[list[dict[str, Any]], bool]:
    """If it's been >5 min since last assistant response, clear old tool results."""
    if last_assistant_time is None:
        return messages, False

    if time.time() - last_assistant_time < _MICROCOMPACT_GAP_SECONDS:
        return messages, False

    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    to_compact = tool_indices[:-2]  # keep last 2
    modified = False
    for i in to_compact:
        if not messages[i].get("content", "").startswith("[Previous tool result"):
            messages[i]["content"] = "[Previous tool result cleared due to inactivity]"
            modified = True
    return messages, modified


# ─── session marker helpers (history I/O lives in chat_history.py) ────────


def _now_iso() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _make_session_marker(event: str, timestamp: str) -> dict[str, Any]:
    """Create a session boundary marker message."""
    return {
        "role": "system",
        "content": f"[SESSION {event} at {timestamp}]",
        "_session_marker": True,
    }


# ─── chat turn (extracted for reuse by API) ───────────────────────────────


@dataclass
class TurnResult:
    """Chat-layer turn result.

    Wraps the agent's :class:`AgentTurnResult` with the cross-turn budget
    state the CLI loop and API session need to remember
    (compression / microcompact flags, failure counter, last-assistant
    timestamp, optional reasoning text).
    """

    messages: list[dict[str, Any]]
    assistant_message: str = ""
    tool_calls_executed: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    did_compress: bool = False
    did_microcompact: bool = False
    error: str | None = None
    reasoning: str | None = None
    consecutive_compact_failures: int = 0
    last_assistant_time: float | None = None
    # Time-to-first-token (ms) observed by the agent; None when the turn
    # produced no streamed token (e.g. early error).
    ttft_ms: float | None = None


def _build_agent(cfg: config_mod.Config) -> ChatAgent:
    """Construct a fresh ChatAgent with the current skill set merged in."""
    loaded = _load_skills()
    enabled_names = (
        CHAT_SCHEMA_NAMES if cfg.chat.unsafe_local_tools_enabled else SAFE_CHAT_SCHEMA_NAMES
    )
    built_in_schemas = [
        schema for schema in CHAT_SCHEMAS if schema["function"]["name"] in enabled_names
    ]
    built_in_handlers = {
        name: handler for name, handler in TOOL_HANDLERS.items() if name in enabled_names
    }
    all_schemas = built_in_schemas + loaded.schemas
    all_handlers = {**built_in_handlers, **loaded.handlers}
    daemon_url = ""
    if cfg.chat.mcp_connect_daemon:
        daemon_url = f"http://{cfg.mcp.host}:{cfg.mcp.port}/mcp"
    return ChatAgent(cfg.chat, all_schemas, all_handlers, daemon_mcp_url=daemon_url)


def _wrap_agent_result(
    agent_result: AgentTurnResult,
    *,
    did_compress: bool,
    did_microcompact: bool,
    consecutive_compact_failures: int,
    last_assistant_time: float | None,
) -> TurnResult:
    """Lift an :class:`AgentTurnResult` into a chat-layer :class:`TurnResult`."""
    return TurnResult(
        messages=agent_result.messages,
        assistant_message=agent_result.assistant_message,
        tool_calls_executed=agent_result.tool_calls_executed,
        usage=agent_result.usage,
        error=agent_result.error,
        did_compress=did_compress,
        did_microcompact=did_microcompact,
        consecutive_compact_failures=consecutive_compact_failures,
        last_assistant_time=last_assistant_time,
        ttft_ms=agent_result.ttft_ms,
    )


async def _run_turn(
    cfg: config_mod.Config,
    messages: list[dict[str, Any]],
    user_input: str,
    *,
    last_usage: dict[str, int] | None = None,
    consecutive_compact_failures: int = 0,
    last_assistant_time: float | None = None,
    on_token: _OnTokenT | None = None,
    on_thinking: _OnTokenT | None = None,
    on_tool_call: _OnToolCallT | None = None,
    agent: ChatAgent | None = None,
) -> TurnResult:
    """Execute one user turn via :class:`ChatAgent` with compression around it.

    ``agent`` is reused across turns from the chat loop. When called without
    one (e.g. from the API per-request path), a transient agent is built and
    closed inside the function so we don't leak httpx sockets.
    """
    messages, did_microcompact = _microcompact(messages, last_assistant_time)
    messages, consecutive_compact_failures, did_compress = await asyncio.to_thread(
        _compress_context,
        cfg,
        messages,
        last_usage=last_usage,
        consecutive_failures=consecutive_compact_failures,
    )

    messages.append({"role": "user", "content": f"[Current time: {_now_iso()}]\n\n{user_input}"})

    owned_agent: ChatAgent | None = None
    if agent is None:
        owned_agent = _build_agent(cfg)
        await owned_agent.aopen()
        agent = owned_agent

    system_prompt = _load_system_prompt()
    max_tokens = cfg.model_for("chat").max_tokens or 8192

    try:
        agent_result = await agent.run_turn(
            messages,
            system_prompt,
            on_token=on_token,
            on_thinking=on_thinking,
            on_tool_call=on_tool_call,
            max_tokens=max_tokens,
            thinking_budget=cfg.chat.thinking_budget,
        )
    except Exception as exc:
        # Drop the just-appended user turn so the caller can retry cleanly.
        if messages and messages[-1].get("role") == "user":
            messages.pop()
        if owned_agent is not None:
            await owned_agent.aclose()
        return TurnResult(
            messages=messages,
            error=str(exc),
            did_compress=did_compress,
            did_microcompact=did_microcompact,
            consecutive_compact_failures=consecutive_compact_failures,
            last_assistant_time=last_assistant_time,
        )

    if owned_agent is not None:
        await owned_agent.aclose()

    if agent_result.assistant_message:
        last_assistant_time = time.time()

    return _wrap_agent_result(
        agent_result,
        did_compress=did_compress,
        did_microcompact=did_microcompact,
        consecutive_compact_failures=consecutive_compact_failures,
        last_assistant_time=last_assistant_time,
    )


# ─── chat loop ────────────────────────────────────────────────────────────


async def run_chat(cfg: config_mod.Config) -> None:
    """Interactive chat loop driven by the Anthropic SDK ChatAgent."""
    console.print(f"[bold]Chat with {cfg.chat.model}[/bold] (type 'exit' or Ctrl+C to quit)")
    console.print("[dim]Commands: 'new' = new conversation, 'exit' = quit[/dim]\n")

    now = _now_iso()
    system_content = _load_system_prompt()
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_content}]

    prev = chat_history.load_history()
    if prev:
        away_summary = await asyncio.to_thread(_maybe_away_summary, prev, cfg)
        if away_summary:
            messages.append({"role": "system", "content": away_summary})
            console.print("[dim](generated away summary)[/dim]")
        prev = _microcompact_on_resume(prev)
        messages.extend(prev)
        messages.append(_make_session_marker("RESUME", now))
        prev_turns = len(
            [m for m in prev if m.get("role") == "user" and not m.get("_session_marker")]
        )
        console.print(f"[dim]Restored {prev_turns} previous turns.[/dim]\n")

    agent = _build_agent(cfg)
    await agent.aopen()

    last_usage: dict[str, int] | None = None
    consecutive_compact_failures = 0
    last_extracted_index = 0
    tokens_at_last_extraction = 0
    last_assistant_time: float | None = None

    try:
        while True:
            try:
                user_input = console.input("[bold green]You:[/bold green] ")
            except (KeyboardInterrupt, EOFError):
                console.print("\n[dim]Bye![/dim]")
                messages.append(_make_session_marker("EXIT", _now_iso()))
                chat_history.save_history(messages)
                break
            if user_input.strip().lower() in ("exit", "quit"):
                console.print("[dim]Bye![/dim]")
                messages.append(_make_session_marker("EXIT", _now_iso()))
                chat_history.save_history(messages)
                break
            if user_input.strip().lower() == "new":
                archived = chat_history.archive_current()
                messages = [
                    {
                        "role": "system",
                        "content": _load_system_prompt(),
                    }
                ]
                last_usage = None
                consecutive_compact_failures = 0
                last_extracted_index = 0
                tokens_at_last_extraction = 0
                last_assistant_time = None
                chat_history.save_history(messages)
                if archived:
                    console.print(
                        f"[dim]Previous conversation archived as {archived}. New conversation started.[/dim]\n"
                    )
                else:
                    console.print("[dim]New conversation started.[/dim]\n")
                continue
            if not user_input.strip():
                continue

            console.print("\n[bold blue]Assistant:[/bold blue]")
            buf = ""

            with Live(
                Markdown(""),
                console=console,
                refresh_per_second=15,
                auto_refresh=True,
                vertical_overflow="visible",
            ) as live:

                async def _on_token(tok: str) -> None:
                    nonlocal buf
                    buf += tok
                    live.update(Markdown(buf))

                async def _on_tool(
                    name: str, args: dict[str, Any], result: str, elapsed_ms: float
                ) -> None:
                    console.print(f"  [dim]→ {name} ({elapsed_ms:.0f}ms)[/dim]", highlight=False)

                turn_result = await _run_turn(
                    cfg,
                    messages,
                    user_input,
                    last_usage=last_usage,
                    consecutive_compact_failures=consecutive_compact_failures,
                    last_assistant_time=last_assistant_time,
                    on_token=_on_token,
                    on_tool_call=_on_tool,
                    agent=agent,
                )

            messages = turn_result.messages
            last_usage = turn_result.usage or last_usage
            consecutive_compact_failures = turn_result.consecutive_compact_failures
            last_assistant_time = turn_result.last_assistant_time
            chat_history.save_history(messages)

            if turn_result.did_microcompact:
                console.print("[dim]  (cleared old tool results due to inactivity)[/dim]")
            if turn_result.did_compress:
                console.print("[dim]  (compressed conversation history)[/dim]")

            if turn_result.error:
                console.print(f"[red]Error: {turn_result.error}[/red]")
            console.print()

            if not turn_result.error and turn_result.assistant_message:
                last_extracted_index, tokens_at_last_extraction = maybe_extract_memories(
                    messages,
                    cfg,
                    last_extracted_index=last_extracted_index,
                    tokens_at_last_extraction=tokens_at_last_extraction,
                )
    finally:
        await agent.aclose()


def run_chat_sync(cfg: config_mod.Config) -> None:
    """Sync entry point — wraps :func:`run_chat` with ``asyncio.run``."""
    asyncio.run(run_chat(cfg))

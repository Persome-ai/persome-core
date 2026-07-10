"""Anthropic SDK wrapper with per-stage model resolution.

All background stages (timeline / reducer / classifier / compact /
pattern_detector / active / consolidator / intent_recognizer) call the Anthropic
Messages API directly through the official SDK — the same client path chat uses.
litellm was removed: it serialized custom tools with a ``type:"custom"`` variant
the DeepSeek ``/anthropic`` gateway rejects (``unknown variant 'custom'``), which
broke every tool-calling stage. The backend talks only the Anthropic protocol
(official endpoint or a compatible gateway like DeepSeek's ``/anthropic``).

To keep the blast radius minimal, ``call_llm`` still returns the OpenAI-shaped
``_Resp([_Choice(_Msg(content, tool_calls))])`` object the rest of this module
(``run_tool_loop`` / ``extract_text`` / ``extract_tool_calls`` / ``extract_usage``)
and every caller already consume — the Anthropic response is adapted back into it.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from types import SimpleNamespace
from typing import Any, cast

from ..config import Config, ModelConfig, provider_api_key, provider_base_url
from ..logger import get

logger = get("persome.writer")

# Tools whose handlers are read-only and concurrency-safe (no shared write state).
CONCURRENCY_SAFE_TOOLS: frozenset[str] = frozenset(
    {
        "read_memory",
        "search_memory",
        "drill_capture",
        "drill_window",
        "drill_chat",
        "drill_timeline",
    }
)


@dataclass
class PingResult:
    stage: str
    model: str
    ok: bool
    latency_ms: int | None
    error: str | None
    mocked: bool = False


@dataclass
class _RetryCtx:
    """Mutable state shared across retry attempts within one run_tool_loop call."""

    overloaded_count: int = 0


# litellm routing prefixes — stripped to a bare model name for the Anthropic SDK,
# which (like the chat agent) sends the bare name verbatim to the gateway.
_ROUTING_PREFIXES = ("anthropic/", "deepseek/", "openai/", "openrouter/", "gemini/")

# Default output cap when a stage's ModelConfig sets none (Anthropic requires max_tokens).
_DEFAULT_MAX_TOKENS = 8192


def _bare_model(model: str) -> str:
    """Strip a routing prefix → bare model name (e.g. ``anthropic/deepseek-v4-flash``
    → ``deepseek-v4-flash``). Back-compat for existing ``anthropic/...`` configs."""
    for p in _ROUTING_PREFIXES:
        if model.startswith(p):
            return model[len(p) :]
    return model


def _unfence_json(text: str) -> str:
    """Strip a ```json … ``` fence if the model wrapped its JSON (json_mode)."""
    t = text.strip()
    if t.startswith("```"):
        nl = t.find("\n")
        t = t[nl + 1 :] if nl != -1 else ""
        t = t.rstrip()
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()


def _to_anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI function tools → Anthropic tool specs (``input_schema``). Pass through
    ``cache_control`` and anything already Anthropic-shaped."""
    out: list[dict[str, Any]] = []
    for t in tools:
        if t.get("type") == "function" and isinstance(t.get("function"), dict):
            fn = t["function"]
            spec: dict[str, Any] = {
                "name": fn["name"],
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            }
            if "cache_control" in t:
                spec["cache_control"] = t["cache_control"]
            out.append(spec)
        else:
            out.append(t)
    return out


def _to_anthropic_messages(
    messages: list[dict[str, Any]],
) -> tuple[Any, list[dict[str, Any]]]:
    """OpenAI-style messages → (system, anthropic_messages).

    - ``role:"system"`` → returned separately (Anthropic's top-level ``system`` param).
    - ``role:"tool"`` → a ``tool_result`` block folded into a user message.
    - assistant ``tool_calls`` → ``tool_use`` content blocks.
    Content already given as a string or block-list (incl. ``cache_control``) passes through.
    """
    system: Any = None
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            system = m.get("content")
            continue
        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": m.get("tool_call_id"),
                "content": m.get("content") or "",
            }
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
            continue
        if role == "assistant":
            content: list[dict[str, Any]] = []
            text = m.get("content")
            if text:
                content.append({"type": "text", "text": text})
            for tc in m.get("tool_calls") or []:
                fn = tc["function"]
                args = fn["arguments"]
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                content.append(
                    {"type": "tool_use", "id": tc["id"], "name": fn["name"], "input": args or {}}
                )
            out.append({"role": "assistant", "content": content or ""})
            continue
        # user / other → pass content through verbatim
        out.append({"role": "user", "content": m.get("content")})
    return system, out


def _adapt(msg: Any) -> Any:
    """Adapt an Anthropic ``Message`` into the OpenAI-shaped ``_Resp`` the rest of
    the module consumes (text + tool_calls + finish_reason + usage)."""
    text_parts: list[str] = []
    tool_calls: list[Any] = []
    for b in getattr(msg, "content", None) or []:
        bt = getattr(b, "type", None)
        if bt == "text":
            text_parts.append(getattr(b, "text", "") or "")
        elif bt == "tool_use":
            inp = getattr(b, "input", None)
            tool_calls.append(
                SimpleNamespace(
                    id=getattr(b, "id", None),
                    function=SimpleNamespace(
                        name=getattr(b, "name", ""),
                        arguments=json.dumps(
                            inp if isinstance(inp, dict) else {}, ensure_ascii=False
                        ),
                    ),
                )
            )
    stop = getattr(msg, "stop_reason", None)
    finish = {
        "max_tokens": "length",
        "end_turn": "stop",
        "stop_sequence": "stop",
        "tool_use": "tool_calls",
    }.get(stop or "", "stop")
    usage = None
    u = getattr(msg, "usage", None)
    if u is not None:
        inp = getattr(u, "input_tokens", 0) or 0
        outp = getattr(u, "output_tokens", 0) or 0
        usage = SimpleNamespace(
            prompt_tokens=inp,
            completion_tokens=outp,
            total_tokens=inp + outp,
            cache_read_input_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_creation_input_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
    return _Resp([_Choice(_Msg("".join(text_parts) or None, tool_calls or None), finish)], usage)


@lru_cache(maxsize=4)
def _make_anthropic_client(api_key: str | None, base_url: str | None) -> Any:
    """Build (and memoize) an Anthropic client per (key, base_url). Cached so the underlying httpx
    connection pool — and its keep-alive TLS connections to the relay — are REUSED across calls
    instead of re-handshaking on every LLM request (a full TLS handshake to a far relay is several
    RTTs). Keyed on the creds so an env/cred change yields a fresh client; the SDK client is
    thread-safe, so the capture pool's concurrent fast-path calls share one warm pool. Pure latency,
    identical requests — zero effect on output."""
    import anthropic  # lazy import — keeps CLI startup fast

    return anthropic.Anthropic(api_key=api_key, base_url=base_url)


def _anthropic_client() -> Any:
    """Cached Anthropic client from the canonical ANTHROPIC_* env (same as chat)."""
    return _make_anthropic_client(provider_api_key("anthropic"), provider_base_url("anthropic"))


def call_llm(
    cfg: Config,
    stage: str,
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    json_mode: bool = False,
    extra: dict[str, Any] | None = None,
) -> Any:
    """Invoke the Anthropic Messages API for the given stage.

    Returns the OpenAI-shaped ``_Resp`` adapter (see module docstring), so
    ``run_tool_loop`` / ``extract_*`` and every caller stay unchanged.
    Respects ``PERSOME_LLM_MOCK=1`` (test stub) and ``_OC_FALLBACK_MODEL``
    (529 fallback). ``cache_control`` on messages/tools/system passes through —
    the Anthropic protocol honors it natively (no stripping, no prefix tricks).
    ``extra`` merges raw request params into the call body (e.g.
    ``{"thinking": {"type": "disabled"}}`` for the fast recognizer) — forwarded
    verbatim, so the relay passes them to the upstream gateway.
    """
    if (
        os.environ.get("PERSOME_LLM_MOCK") or os.environ.get("MENS_CONTEXT_LLM_MOCK")
    ) == "1":  # Mens is the legacy name
        return _mock_response(stage, messages, tools, json_mode)

    model_cfg = cfg.model_for(stage)
    override = os.environ.get("_OC_FALLBACK_MODEL")
    if override:
        model_cfg = ModelConfig(**{**model_cfg.__dict__, "model": override})

    system, amsgs = _to_anthropic_messages(messages)
    kwargs: dict[str, Any] = {
        "model": _bare_model(model_cfg.model),
        "messages": amsgs,
        "max_tokens": model_cfg.max_tokens or _DEFAULT_MAX_TOKENS,
    }
    if system is not None:
        kwargs["system"] = system
    if tools:
        kwargs["tools"] = _to_anthropic_tools(tools)
    if extra:
        kwargs.update(extra)

    logger.debug("llm call stage=%s model=%s", stage, model_cfg.model)
    msg = _anthropic_client().messages.create(**kwargs)
    resp = _adapt(msg)
    if json_mode and resp.choices and resp.choices[0].message.content:
        resp.choices[0].message.content = _unfence_json(resp.choices[0].message.content)
    return resp


def call_llm_streaming(
    cfg: Config,
    stage: str,
    *,
    messages: list[dict[str, Any]],
    json_mode: bool = False,
    on_delta: Any = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """Streaming variant of :func:`call_llm` — returns the FULL accumulated text and feeds
    ``on_delta(accumulated_text)`` after each chunk so the caller can act on partial output
    (the fast recognizer fires on its required fields before ``rationale``/``quote`` finish).

    Robust through the Persome relay: it iterates the SDK's RAW event stream and pulls
    ``content_block_delta.delta.text`` (the relay re-frames the SSE in a way that makes the SDK's
    ``text_stream`` helper yield nothing, but the raw ``content_block_delta`` events arrive fine —
    and DeepSeek's ``thinking`` events are simply ignored). **Fail-open**: a proxy that rejects
    ``stream:true``, an empty stream, or any error falls back to the non-streaming :func:`call_llm`,
    so behaviour degrades to "wait for the whole body" — never worse, never a recognition miss.
    Mock-aware: ``PERSOME_LLM_MOCK=1`` delegates to :func:`call_llm` (so a test's monkeypatched
    ``call_llm`` / ``fake_llm`` fixture is honored) and feeds the full text to ``on_delta`` once."""
    if (
        os.environ.get("PERSOME_LLM_MOCK") or os.environ.get("MENS_CONTEXT_LLM_MOCK")
    ) == "1":  # Mens is the legacy name
        text = extract_text(
            call_llm(cfg, stage, messages=messages, json_mode=json_mode, extra=extra)
        )
        if on_delta:
            with contextlib.suppress(Exception):  # a callback error must not break the call
                on_delta(text)
        return text

    model_cfg = cfg.model_for(stage)
    override = os.environ.get("_OC_FALLBACK_MODEL")
    if override:
        model_cfg = ModelConfig(**{**model_cfg.__dict__, "model": override})
    system, amsgs = _to_anthropic_messages(messages)
    kwargs: dict[str, Any] = {
        "model": _bare_model(model_cfg.model),
        "messages": amsgs,
        "max_tokens": model_cfg.max_tokens or _DEFAULT_MAX_TOKENS,
    }
    if system is not None:
        kwargs["system"] = system
    if extra:
        kwargs.update(extra)

    try:
        parts: list[str] = []
        with _anthropic_client().messages.stream(**kwargs) as stream:
            for event in stream:
                if getattr(event, "type", "") != "content_block_delta":
                    continue
                delta = getattr(event, "delta", None)
                piece = getattr(delta, "text", None) if delta is not None else None
                if not piece:
                    continue
                parts.append(piece)
                if on_delta:
                    with contextlib.suppress(Exception):
                        on_delta("".join(parts))
        text = "".join(parts)
        if not text.strip():
            raise RuntimeError("empty stream (relay reframing?) — fall back")
        return _unfence_json(text) if json_mode else text
    except Exception as exc:  # noqa: BLE001 — fail-open to the blocking call
        logger.warning("streaming call failed (%s); falling back to non-streaming", exc)
        text = extract_text(
            call_llm(cfg, stage, messages=messages, json_mode=json_mode, extra=extra)
        )
        if on_delta:
            with contextlib.suppress(Exception):
                on_delta(text)
        return text


# Stage-specific default shapes so PERSOME_LLM_MOCK=1 works out of the box
# for every stage instead of returning an obsolete v1-shape string.
_MOCK_DEFAULTS: dict[str, str] = {
    "timeline": '{"entries": ["[TestApp] worked in window, involving —"]}',
    "reducer": '{"summary": "Test session", "sub_tasks": ["[10:00-10:05, TestApp] test activity, involving —"]}',
    "classifier": "",  # no text → tool_calls empty → no action → commit not called
    "compact": '{"content": "Compacted text."}',
    "thread_tracker": '{"ops": [{"op": "none"}]}',  # window judged idle — no state change
}


def _mock_response(stage: str, messages, tools, json_mode):  # type: ignore[no-untyped-def]
    """Minimal stub for offline tests. Customize via PERSOME_LLM_MOCK_JSON."""
    override = os.environ.get("PERSOME_LLM_MOCK_JSON") or os.environ.get(
        "MENS_CONTEXT_LLM_MOCK_JSON"
    )  # Mens is the legacy name
    content = override if override else _MOCK_DEFAULTS.get(stage, "")
    return _build_response(content)


class _Msg:
    def __init__(self, content: Any, tool_calls: Any = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, msg: _Msg, finish_reason: str = "stop") -> None:
        self.message = msg
        self.finish_reason = finish_reason


class _Resp:
    def __init__(self, choices: list[_Choice], usage: Any = None) -> None:
        self.choices = choices
        self.usage = usage


def _build_response(content: str, tool_calls: list | None = None) -> Any:
    """Build a minimal response-shaped object usable by extract_text / extract_tool_calls.

    Tests can import this to construct canned LLM responses without re-implementing
    the internal _Msg / _Choice / _Resp shape.
    """
    return _Resp([_Choice(_Msg(content, tool_calls))])


def extract_text(response: Any) -> str:
    try:
        return response.choices[0].message.content or ""
    except (AttributeError, IndexError):
        return ""


def ping_stage(cfg: Config, stage: str, *, timeout: float = 5.0) -> PingResult:
    """Send a tiny round-trip request to the stage's configured model.

    Returns a PingResult with success, latency, and a short error label on
    failure. Honors PERSOME_LLM_MOCK=1 by returning a mocked-ok result
    without touching the network. Never raises — `status` and similar
    informational callers must remain non-fatal.
    """
    model_cfg = cfg.model_for(stage)
    if (
        os.environ.get("PERSOME_LLM_MOCK") or os.environ.get("MENS_CONTEXT_LLM_MOCK")
    ) == "1":  # Mens is the legacy name
        return PingResult(
            stage=stage,
            model=model_cfg.model,
            ok=True,
            latency_ms=0,
            error=None,
            mocked=True,
        )

    try:
        import anthropic  # lazy import — keeps CLI startup fast
    except ImportError as exc:
        return PingResult(
            stage=stage,
            model=model_cfg.model,
            ok=False,
            latency_ms=None,
            error=f"ImportError: {exc}",
        )

    start = time.monotonic()
    try:
        client = anthropic.Anthropic(
            api_key=provider_api_key("anthropic"),
            base_url=provider_base_url("anthropic"),
            timeout=timeout,
        )
        client.messages.create(
            model=_bare_model(model_cfg.model),
            messages=[{"role": "user", "content": "Reply with 'ok'."}],
            max_tokens=4,
        )
    except Exception as exc:  # noqa: BLE001
        label = type(exc).__name__
        msg = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
        if msg:
            label = f"{label}: {msg[:60]}"
        return PingResult(
            stage=stage,
            model=model_cfg.model,
            ok=False,
            latency_ms=None,
            error=label[:80],
        )
    latency_ms = int((time.monotonic() - start) * 1000)
    return PingResult(
        stage=stage,
        model=model_cfg.model,
        ok=True,
        latency_ms=latency_ms,
        error=None,
    )


def extract_usage(response: Any) -> dict[str, int] | None:
    """Extract token usage dict from an adapted response (``_Resp.usage``).

    Returns a dict with prompt_tokens, completion_tokens, total_tokens,
    cache_read_tokens, and cache_creation_tokens (last two default to 0).
    """
    try:
        usage = response.usage
        if usage is None:
            return None
        return {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
            "total_tokens": getattr(usage, "total_tokens", 0) or 0,
            "cache_read_tokens": (
                getattr(usage, "cache_read_input_tokens", None)
                or getattr(usage, "cache_read_tokens", 0)
                or 0
            ),
            "cache_creation_tokens": (
                getattr(usage, "cache_creation_input_tokens", None)
                or getattr(usage, "cache_creation_tokens", 0)
                or 0
            ),
        }
    except (AttributeError, TypeError):
        return None


# Fields to truncate preferentially when a tool result is over budget (F2).
_TRUNCATABLE_FIELDS = ("content", "visible_text", "body", "text", "entries")


def _truncate_result(result: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    """If JSON-serialised result exceeds max_bytes, truncate the largest string field."""
    serialised = json.dumps(result, ensure_ascii=False).encode()
    if len(serialised) <= max_bytes:
        return result
    result = dict(result)
    result["_truncated"] = True
    for fname in _TRUNCATABLE_FIELDS:
        if fname not in result:
            continue
        val = result[fname]
        if not isinstance(val, str):
            continue
        # Binary search for max prefix length that keeps total within budget.
        lo, hi = 0, len(val)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            result[fname] = val[:mid]
            if len(json.dumps(result, ensure_ascii=False).encode()) <= max_bytes:
                lo = mid
            else:
                hi = mid - 1
        result[fname] = val[:lo]
        if len(json.dumps(result, ensure_ascii=False).encode()) <= max_bytes:
            break
    return result


def make_tool_response(
    assistant_msg: dict[str, Any],
    i: int,
    name: str,
    result: dict[str, Any],
    max_bytes: int = 0,
) -> dict[str, Any]:
    if max_bytes > 0:
        result = _truncate_result(result, max_bytes)
    return {
        "role": "tool",
        "tool_call_id": assistant_msg["tool_calls"][i]["id"],
        "name": name,
        "content": json.dumps(result, ensure_ascii=False),
    }


def _microcompact_tool_results(messages: list[dict[str, Any]], budget: int) -> None:
    """Replace oldest tool result content with '[compacted]' until total ≤ budget."""
    total = sum(len((m.get("content") or "").encode()) for m in messages if m.get("role") == "tool")
    if total <= budget:
        return
    for m in messages:
        if m.get("role") == "tool" and m.get("content") != "[compacted]":
            total -= len((m["content"] or "").encode())
            m["content"] = "[compacted]"
            total += len(b"[compacted]")
            if total <= budget:
                break


def _estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Rough token estimate: 1 token ≈ 4 bytes of JSON (claude-code heuristic)."""
    return sum(len(json.dumps(m)) for m in messages) // 4


def count_tokens_api(cfg: Config, stage: str, messages: list[dict[str, Any]]) -> int | None:
    """Precise token count via the Anthropic SDK ``count_tokens`` endpoint.

    Returns None on any failure, when disabled, or when the gateway doesn't
    implement the endpoint — callers fall back to ``_estimate_tokens``.
    """
    if (
        os.environ.get("PERSOME_LLM_MOCK") or os.environ.get("MENS_CONTEXT_LLM_MOCK")
    ) == "1":  # Mens is the legacy name
        return None
    if not cfg.writer.use_token_count_api:
        return None
    model_cfg = cfg.model_for(stage)
    try:
        import anthropic  # noqa: F401

        system, amsgs = _to_anthropic_messages(messages)
        kwargs: dict[str, Any] = {"model": _bare_model(model_cfg.model), "messages": amsgs}
        if system is not None:
            kwargs["system"] = system
        result = _anthropic_client().messages.count_tokens(**kwargs)
        return int(getattr(result, "input_tokens", 0)) or None
    except Exception:  # noqa: BLE001
        return None


def _trim_messages(messages: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Drop oldest assistant+tool round-trips until estimated tokens fall below limit.

    Always preserves messages[0] (system) and messages[1] (initial user context).
    Removes at most 4 rounds per call to avoid over-trimming.
    """
    if len(messages) <= 2:
        return messages
    head = messages[:2]
    tail = list(messages[2:])
    removed = 0
    while removed < 4 and _estimate_tokens(head + tail) > limit:
        for i, m in enumerate(tail):
            if m.get("role") == "assistant":
                j = i + 1
                while j < len(tail) and tail[j].get("role") == "tool":
                    j += 1
                tail = tail[:i] + tail[j:]
                removed += 1
                break
        else:
            break
    return head + tail


def _call_llm_with_retry(
    cfg: Config,
    stage: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    *,
    ctx: _RetryCtx,
    iteration: int,
    tag: str,
) -> Any:
    """Error-classified LLM retry wrapper.

    - 429 Rate Limit: reads retry-after header, waits, does NOT count against llm_retry_attempts.
    - 529 Overloaded: counts attempts, switches to llm_fallback_model after 3 consecutive.
    - 413 / ContextWindowExceeded: reactive trim, does NOT count against llm_retry_attempts.
    - Auth errors (401/403): abort immediately, no retry.
    - Other errors: exponential backoff, counted against llm_retry_attempts.
    """
    attempt = 0
    rate_limit_attempts = 0
    rate_limit_max = 5

    while True:
        if attempt >= cfg.writer.llm_retry_attempts:
            raise RuntimeError(
                f"{tag}: exhausted {cfg.writer.llm_retry_attempts} retry attempts at iter {iteration}"
            )

        try:
            if ctx.overloaded_count >= 3 and cfg.writer.llm_fallback_model:
                logger.warning(
                    "%s: 3x overloaded, using fallback model %s",
                    tag,
                    cfg.writer.llm_fallback_model,
                )
                os.environ["_OC_FALLBACK_MODEL"] = cfg.writer.llm_fallback_model
                try:
                    resp = call_llm(cfg, stage, messages=messages, tools=tools)
                finally:
                    os.environ.pop("_OC_FALLBACK_MODEL", None)
            else:
                resp = call_llm(cfg, stage, messages=messages, tools=tools)

            ctx.overloaded_count = 0
            return resp

        except Exception as exc:  # noqa: BLE001
            exc_type = type(exc).__name__.lower()
            exc_str = str(exc).lower()

            # 429 Rate Limit — wait for retry-after, don't count attempt
            if "ratelimiterror" in exc_type or "429" in str(exc):
                rate_limit_attempts += 1
                wait = float(cfg.writer.llm_rate_limit_wait_s)
                resp_obj = getattr(exc, "response", None)
                if resp_obj is not None:
                    headers = getattr(resp_obj, "headers", {}) or {}
                    ra = headers.get("retry-after") or headers.get(
                        "anthropic-ratelimit-unified-reset"
                    )
                    if ra:
                        try:
                            val = float(ra)
                            # Unix timestamp vs relative seconds
                            wait = max(1.0, val - time.time()) if val > 1_700_000_000 else val
                        except (ValueError, TypeError):
                            pass
                logger.warning("%s: rate limited (iter %d), sleeping %.1fs", tag, iteration, wait)
                time.sleep(wait)
                if rate_limit_attempts >= rate_limit_max:
                    raise
                continue  # don't increment attempt

            # 529 Overloaded — count attempt, continue with backoff
            if "serviceunavailable" in exc_type or "529" in str(exc) or "overloaded" in exc_str:
                ctx.overloaded_count += 1
                attempt += 1
                if attempt < cfg.writer.llm_retry_attempts:
                    time.sleep(2.0 ** (attempt - 1))
                    continue
                raise

            # 413 / ContextWindowExceeded — reactive trim, don't count attempt
            if (
                "contextwindowexceeded" in exc_type
                or "413" in str(exc)
                or "context_length" in exc_str
                or "too long" in exc_str
            ):
                logger.warning("%s: context too long at iter %d, reactive trim", tag, iteration)
                if len(messages) <= 2:
                    raise
                messages[:] = _trim_messages(messages, len(messages) // 2)
                continue  # don't increment attempt

            # Auth errors — abort immediately
            if "authentication" in exc_type or "401" in str(exc) or "403" in str(exc):
                logger.error("%s: auth error at iter %d, aborting: %s", tag, iteration, exc)
                raise

            # All other errors — exponential backoff, count attempt
            attempt += 1
            if attempt < cfg.writer.llm_retry_attempts:
                wait = 2.0 ** (attempt - 1)
                logger.debug(
                    "%s: LLM error, retry %d/%d in %.1fs: %s",
                    tag,
                    attempt,
                    cfg.writer.llm_retry_attempts,
                    wait,
                    exc,
                )
                time.sleep(wait)
                continue
            raise


OnEventFn = Callable[[str, dict[str, Any]], None]


def run_tool_loop(
    cfg: Config,
    stage: str,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]],
    dispatch_fn: Callable[[str, dict[str, Any]], dict[str, Any]],
    valid_tool_names: set[str],
    state: Any,
    max_iter: int,
    event_guard: bool = True,
    log_tag: str = "",
    parallel_dispatch_fn: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    on_event: OnEventFn | None = None,
) -> int:
    """Run a tool-call loop. Mutates ``messages`` and ``state`` in place.

    Returns ``iteration + 1`` when the loop commits early, or ``max_iter``
    for all other exit paths (exhausted, LLM failure, no tool calls).

    Harness features (F1-F6):
    - F1: Error-classified retry (rate limit, 529, 413, auth, generic).
    - F2: Per-result and total tool result budget; microcompact oldest results.
    - F3: Output truncation recovery (finish_reason="length" → upgrade max_tokens).
    - F4: Token usage accumulated and logged (incl. cache tokens and USD cost).
    - F5: Optional precise token counting via Anthropic count-tokens API.
    - F6: Parallel execution of concurrency-safe tool calls.
    """
    tag = log_tag or stage
    ctx_limit: int = cfg.writer.context_token_limit
    tool_result_max_bytes: int = cfg.writer.tool_result_max_bytes
    tool_result_total_budget: int = cfg.writer.tool_result_total_budget

    total_prompt = 0
    total_completion = 0
    total_cache_read = 0
    total_cache_creation = 0

    ctx = _RetryCtx()
    recovery_count = 0

    for iteration in range(max_iter):
        # --- Context window protection (F2 microcompact + F5 token count + trim) ---
        if ctx_limit:
            if tool_result_total_budget:
                _microcompact_tool_results(messages, tool_result_total_budget)
            est = count_tokens_api(cfg, stage, messages) or _estimate_tokens(messages)
            if est > ctx_limit:
                messages[:] = _trim_messages(messages, ctx_limit)
                logger.debug(
                    "%s: context trimmed at iter %d, est=%d tokens",
                    tag,
                    iteration,
                    _estimate_tokens(messages),
                )

        # --- LLM call with error-classified retry (F1) ---
        try:
            resp = _call_llm_with_retry(
                cfg, stage, messages, tools, ctx=ctx, iteration=iteration, tag=tag
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s: LLM unrecoverable at iter %d: %s", tag, iteration, exc)
            logger.info(
                "%s: loop aborted iters=%d prompt_tokens=%d completion_tokens=%d",
                tag,
                iteration,
                total_prompt,
                total_completion,
            )
            return max_iter

        # --- Output truncation recovery (F3) ---
        import contextlib

        finish_reason = ""
        with contextlib.suppress(AttributeError, IndexError):
            finish_reason = resp.choices[0].finish_reason or ""

        if finish_reason == "length":
            if recovery_count < cfg.writer.max_output_tokens_recovery_count:
                recovery_count += 1
                model_cfg = cfg.model_for(stage)
                current_max = model_cfg.max_tokens or 4096
                new_max = min(current_max * 2, cfg.writer.max_output_tokens_recovery_limit)
                if new_max > current_max:
                    model_cfg.max_tokens = new_max
                    logger.warning(
                        "%s: output truncated, upgrading max_tokens %d→%d (recovery %d/%d)",
                        tag,
                        current_max,
                        new_max,
                        recovery_count,
                        cfg.writer.max_output_tokens_recovery_count,
                    )
                partial_text = extract_text(resp)
                if partial_text:
                    messages.append({"role": "assistant", "content": partial_text})
                messages.append(
                    {
                        "role": "user",
                        "content": "Continue directly from where you left off. No recap.",
                    }
                )
                continue
            else:
                logger.warning(
                    "%s: output truncated, recovery limit reached at iter %d", tag, iteration
                )

        # --- Accumulate token usage (F4) ---
        usage = extract_usage(resp)
        if usage:
            total_prompt += usage["prompt_tokens"]
            total_completion += usage["completion_tokens"]
            total_cache_read += usage.get("cache_read_tokens", 0)
            total_cache_creation += usage.get("cache_creation_tokens", 0)

        tool_calls = extract_tool_calls(resp)
        text = extract_text(resp)

        if on_event:
            reasoning = ""
            with contextlib.suppress(AttributeError):
                reasoning = resp.choices[0].message.reasoning_content or ""
            if text or reasoning:
                on_event("llm_text", {"text": text, "reasoning": reasoning})

        assistant_msg: dict[str, Any] = {"role": "assistant", "content": text or None}
        # DeepSeek thinking-mode models require reasoning_content to be passed back
        try:
            rc = resp.choices[0].message.reasoning_content
            if rc:
                assistant_msg["reasoning_content"] = rc
        except AttributeError:
            pass
        if tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": c["id"] or f"call_{iteration}_{i}",
                    "type": "function",
                    "function": {
                        "name": c["name"],
                        "arguments": json.dumps(c["arguments"], ensure_ascii=False),
                    },
                }
                for i, c in enumerate(tool_calls)
            ]
        messages.append(assistant_msg)

        if not tool_calls:
            logger.info("%s: ended without commit at iter %d", tag, iteration)
            break

        # --- Execute tools (F6: parallel for safe tools, serial for others) ---
        # Partition into concurrency-safe vs. write-side tools.
        safe_idxs = [i for i, c in enumerate(tool_calls) if c["name"] in CONCURRENCY_SAFE_TOOLS]
        unsafe_idxs = [
            i for i, c in enumerate(tool_calls) if c["name"] not in CONCURRENCY_SAFE_TOOLS
        ]

        tool_results: dict[int, dict[str, Any]] = {}

        def _run_one(idx: int, call: dict[str, Any], dfn: Callable) -> dict[str, Any]:
            name = call["name"]
            args = call["arguments"] or {}
            if event_guard and name in {"append", "create", "supersede", "flag_compact"}:
                target_path = str(args.get("path") or "")
                if target_path.startswith("event-"):
                    return {
                        "error": (
                            f"forbidden: {stage} cannot write to {target_path}. "
                            "event-daily is owned by the reducer."
                        )
                    }
            if name not in valid_tool_names:
                return {"error": f"unknown tool: {name}"}
            if on_event:
                on_event("tool_call", {"name": name, "arguments": args})
            try:
                return cast(dict[str, Any], dfn(name, args))
            except Exception as exc:  # noqa: BLE001
                logger.exception("%s: tool %s failed", tag, name)
                return {"error": f"tool crashed: {exc}"}

        if safe_idxs and parallel_dispatch_fn is not None:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            with ThreadPoolExecutor(max_workers=4) as ex:
                futures = {
                    ex.submit(_run_one, i, tool_calls[i], parallel_dispatch_fn): i
                    for i in safe_idxs
                }
                for fut in as_completed(futures):
                    tool_results[futures[fut]] = fut.result()
        else:
            for i in safe_idxs:
                tool_results[i] = _run_one(i, tool_calls[i], dispatch_fn)

        for i in unsafe_idxs:
            tool_results[i] = _run_one(i, tool_calls[i], dispatch_fn)

        # Append tool responses in original order.
        for i, call in enumerate(tool_calls):
            result = tool_results.get(i, {"error": "missing result"})
            messages.append(
                make_tool_response(
                    assistant_msg, i, call["name"], result, max_bytes=tool_result_max_bytes
                )
            )

        if state.committed:
            iters = iteration + 1
            _log_loop_done(
                tag,
                iters,
                total_prompt,
                total_completion,
                total_cache_read,
                total_cache_creation,
                cfg,
                stage,
            )
            return iters

    _log_loop_done(
        tag,
        max_iter,
        total_prompt,
        total_completion,
        total_cache_read,
        total_cache_creation,
        cfg,
        stage,
    )
    return max_iter


def _log_loop_done(
    tag: str,
    iters: int,
    total_prompt: int,
    total_completion: int,
    total_cache_read: int,
    total_cache_creation: int,
    cfg: Config,
    stage: str,
) -> None:
    try:
        from .cost import calculate_usd

        total_usage = {
            "prompt_tokens": total_prompt,
            "completion_tokens": total_completion,
            "cache_read_tokens": total_cache_read,
            "cache_creation_tokens": total_cache_creation,
        }
        cost = calculate_usd(cfg.model_for(stage).model, total_usage)
        cost_str = f" cost_usd={cost:.4f}" if cost is not None else ""
    except Exception:  # noqa: BLE001
        cost_str = ""

    logger.info(
        "%s: loop done iters=%d prompt_tokens=%d completion_tokens=%d%s",
        tag,
        iters,
        total_prompt,
        total_completion,
        cost_str,
    )


def extract_tool_calls(response: Any) -> list[dict[str, Any]]:
    try:
        calls = response.choices[0].message.tool_calls or []
    except (AttributeError, IndexError):
        return []
    out: list[dict[str, Any]] = []
    for c in calls:
        fn = getattr(c, "function", None) or c.get("function", {})
        args_raw = (
            getattr(fn, "arguments", None) if hasattr(fn, "arguments") else fn.get("arguments")
        )
        name = getattr(fn, "name", None) if hasattr(fn, "name") else fn.get("name")
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
        except json.JSONDecodeError:
            args = {}
        out.append(
            {
                "id": getattr(c, "id", None) or c.get("id"),
                "name": name,
                "arguments": args,
            }
        )
    return out

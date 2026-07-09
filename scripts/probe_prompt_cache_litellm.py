"""Probe whether litellm transmits cache_control to an Anthropic-compatible endpoint.

Same idea as ``probe_prompt_cache.py`` but goes through ``litellm.completion``
instead of the bare ``anthropic`` SDK. The pipeline stages (timeline / classifier
/ session_reducer) all go through ``writer/llm.py:call_llm`` → ``litellm`` — if
litellm silently drops cache_control before the request leaves the process,
the entire stage optimisation is a no-op.

Usage:

    uv run python scripts/probe_prompt_cache_litellm.py
    uv run python scripts/probe_prompt_cache_litellm.py --model anthropic/claude-haiku-4-5 \
        --base-url https://api.anthropic.com --api-key-env ANTHROPIC_API_KEY

Exit code is 0 iff the second call shows cache_read_input_tokens > 0.
"""

from __future__ import annotations

import argparse
import os
import sys

# A chunky, deterministic system prompt — must clear the model's min-cacheable
# threshold (4096 tokens on Opus/Haiku4.5, 2048 on Sonnet 4.6). The repeated
# pseudo-rulebook keeps it well above 4 KB without injecting any volatile bytes.
_SYSTEM_PROMPT_TEMPLATE = """You are a meticulous test fixture used by the
persome litellm prompt-caching probe. Your sole job is to acknowledge the
incoming question with a short factual reply.

The following pseudo-rulebook exists purely to bulk up the cached prefix so
that this prompt clears the model's minimum-cacheable-prefix threshold. Each
clause is deterministic and stable across runs.

""" + "\n".join(
    f"Rule {i}: When invoked, respond truthfully and tersely. "
    f"Do not invent facts. Do not introduce randomness. "
    f"Acknowledge that this is rule number {i} of the test rulebook."
    for i in range(1, 121)
)


def _extract_usage(resp: object) -> dict[str, int]:
    """Pull cache fields out of a litellm ModelResponse usage block."""
    usage = getattr(resp, "usage", None)
    if usage is None:
        return {}
    fields = (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        # litellm sometimes maps Anthropic fields onto its own names
        "prompt_tokens",
        "completion_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
    )
    out: dict[str, int] = {}
    for f in fields:
        val = getattr(usage, f, None)
        if val is None and isinstance(usage, dict):
            val = usage.get(f)
        if val is not None:
            out[f] = int(val)
    return out


def _print_usage_row(label: str, usage: dict[str, int]) -> None:
    inp = usage.get("input_tokens", usage.get("prompt_tokens", 0))
    out = usage.get("output_tokens", usage.get("completion_tokens", 0))
    create = usage.get("cache_creation_input_tokens", usage.get("cache_creation_tokens", 0))
    read = usage.get("cache_read_input_tokens", usage.get("cache_read_tokens", 0))
    print(f"| {label} | {inp} | {create} | {read} | {out} |")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--base-url",
        default="https://api.deepseek.com/anthropic",
        help="Anthropic-compatible base URL (default: DeepSeek /anthropic gateway).",
    )
    p.add_argument(
        "--model",
        default="deepseek-chat",
        help="Model id to test (default: deepseek-chat).",
    )
    p.add_argument(
        "--api-key-env",
        default="DEEPSEEK_API_KEY",
        help="Env var holding the API key (default: DEEPSEEK_API_KEY).",
    )
    args = p.parse_args()

    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        print(f"ERROR: env var {args.api_key_env} is unset.", file=sys.stderr)
        return 2

    import litellm  # imported lazily to keep CLI startup fast

    # Two-block messages: stable system + dynamic user. cache_control on system
    # last block (a list-of-blocks) is the canonical placement; placing on the
    # user message would also work but we want to mirror the pipeline shape.
    system_blocks = [
        {
            "type": "text",
            "text": _SYSTEM_PROMPT_TEMPLATE,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    messages = [
        {"role": "system", "content": system_blocks},
        {
            "role": "user",
            "content": "What is rule number 42? Reply in one sentence.",
        },
    ]

    print(f"# Prompt cache probe via litellm — {args.base_url}\n")
    print(f"- model: `{args.model}`")
    print(
        f"- litellm version: `{__import__('importlib.metadata', fromlist=['version']).version('litellm')}`"
    )
    print(f"- system prompt length: ~{len(_SYSTEM_PROMPT_TEMPLATE)} bytes\n")
    print("| Call | input_tokens | cache_creation | cache_read | output_tokens |")
    print("| --- | ---: | ---: | ---: | ---: |")

    second_read = 0
    for label in ("first", "second"):
        try:
            resp = litellm.completion(
                model=args.model,
                api_base=args.base_url,
                api_key=api_key,
                messages=messages,
                max_tokens=200,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"\nERROR on {label} call: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 3
        usage = _extract_usage(resp)
        _print_usage_row(label, usage)
        if label == "second":
            second_read = usage.get("cache_read_input_tokens", usage.get("cache_read_tokens", 0))

    print()
    if second_read > 0:
        print(
            f"OK: second call read {second_read} tokens from cache via litellm. Transmission works."
        )
        return 0
    print(
        "FAIL: second call returned 0 cache_read tokens. "
        "Either litellm is dropping cache_control, the provider does not honor it, "
        "or the system block shape is being downgraded somewhere in the chain."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

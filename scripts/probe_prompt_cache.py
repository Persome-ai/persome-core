"""Probe whether a given Anthropic-compatible endpoint honors prompt caching.

Sends two identical requests with a sizable cached system prompt + cache_control
breakpoint, then reports usage.cache_creation_input_tokens and cache_read_input_tokens.

Usage:

    uv run python scripts/probe_prompt_cache.py
    uv run python scripts/probe_prompt_cache.py --base-url https://api.anthropic.com --model claude-haiku-4-5 --api-key-env ANTHROPIC_API_KEY

Exit code is 0 iff the second call shows cache_read_input_tokens > 0. The output
is a markdown table suitable for pasting into a PR description.
"""

from __future__ import annotations

import argparse
import os
import sys

import anthropic

# A chunky, deterministic system prompt — must clear the model's min-cacheable
# threshold (4096 tokens on Opus/Haiku4.5, 2048 on Sonnet 4.6). The repeated
# pseudo-rulebook keeps it well above 4 KB without injecting any volatile bytes.
_SYSTEM_PROMPT_TEMPLATE = """You are a meticulous test fixture used by the
persome prompt-caching probe. Your sole job is to acknowledge the
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


def _print_usage_row(label: str, usage: object) -> None:
    create = getattr(usage, "cache_creation_input_tokens", 0) or 0
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
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

    client = anthropic.Anthropic(api_key=api_key, base_url=args.base_url)

    system_blocks = [
        {
            "type": "text",
            "text": _SYSTEM_PROMPT_TEMPLATE,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "What is rule number 42? Reply in one sentence.",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        }
    ]

    print(f"# Prompt cache probe — {args.base_url}\n")
    print(f"- model: `{args.model}`")
    print(f"- system prompt length: ~{len(_SYSTEM_PROMPT_TEMPLATE)} bytes\n")
    print("| Call | input_tokens | cache_creation | cache_read | output_tokens |")
    print("| --- | ---: | ---: | ---: | ---: |")

    second_read = 0
    for label in ("first", "second"):
        try:
            resp = client.messages.create(
                model=args.model,
                system=system_blocks,  # type: ignore[arg-type]
                messages=messages,  # type: ignore[arg-type]
                max_tokens=200,
            )
        except anthropic.APIError as exc:
            print(f"\nERROR on {label} call: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 3
        _print_usage_row(label, resp.usage)
        if label == "second":
            second_read = int(getattr(resp.usage, "cache_read_input_tokens", 0) or 0)

    print()
    if second_read > 0:
        print(f"OK: second call read {second_read} tokens from cache. Caching is honored.")
        return 0
    print(
        "FAIL: second call returned 0 cache_read_input_tokens. Provider likely ignores cache_control."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

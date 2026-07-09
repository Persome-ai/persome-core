"""LLM USD cost calculator (F4)."""

from __future__ import annotations

# Per-model pricing: (input, output, cache_read, cache_creation) per 1M tokens, USD.
# Prefix-matched: "claude-sonnet-4-6" matches any model ID that starts with that string.
_COSTS: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-4-7": (15.0, 75.0, 1.50, 18.75),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30, 3.75),
    "claude-haiku-4-5": (1.0, 5.0, 0.10, 1.25),
    "gpt-4o-mini": (0.15, 0.6, 0.0, 0.0),
    "gpt-4o": (2.5, 10.0, 0.0, 0.0),
    # DeepSeek family — same pricing whether routed via /v1 (OpenAI shape) or
    # /anthropic gateway. The prefix entry covers both `deepseek-chat`,
    # `deepseek/deepseek-v4-flash`, and `anthropic/deepseek-v4-flash` etc.
    "deepseek-chat": (0.27, 1.1, 0.07, 0.0),
    "deepseek-v4-flash": (0.27, 1.1, 0.07, 0.0),
    "anthropic/deepseek": (0.27, 1.1, 0.07, 0.0),
    "deepseek/": (0.27, 1.1, 0.07, 0.0),
}


def calculate_usd(model: str, usage: dict[str, int]) -> float | None:
    """Return USD cost for the given model and token usage, or None for unknown models."""
    for prefix, (inp, out, cr, cw) in _COSTS.items():
        if model.startswith(prefix):
            return (
                usage.get("prompt_tokens", 0) * inp / 1_000_000
                + usage.get("completion_tokens", 0) * out / 1_000_000
                + usage.get("cache_read_tokens", 0) * cr / 1_000_000
                + usage.get("cache_creation_tokens", 0) * cw / 1_000_000
            )
    return None

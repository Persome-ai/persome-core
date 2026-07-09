"""Tests for F4: USD cost tracking (calculate_usd + extract_usage cache tokens)."""

from __future__ import annotations

from persome.writer.cost import calculate_usd
from persome.writer.llm import extract_usage

# ──────────────────────────────────────────────────────────────────────────
# Test 1: known model cost calculation
# ──────────────────────────────────────────────────────────────────────────


def test_calculate_usd_known_model():
    """claude-sonnet-4-6: 1000 prompt + 100 completion = $0.003 + $0.0015 = $0.0045."""
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }
    cost = calculate_usd("claude-sonnet-4-6", usage)
    assert cost is not None
    assert abs(cost - 0.0045) < 1e-9


def test_calculate_usd_with_cache_tokens():
    """cache_read_tokens contribute to cost."""
    usage = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cache_read_tokens": 1_000_000,
        "cache_creation_tokens": 0,
    }
    cost = calculate_usd("claude-sonnet-4-6", usage)
    assert cost is not None
    assert abs(cost - 0.30) < 1e-9  # 1M cache_read @ $0.30/1M


def test_calculate_usd_prefix_match():
    """Full model ID with version suffix still matches."""
    usage = {
        "prompt_tokens": 1_000_000,
        "completion_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }
    cost = calculate_usd("claude-sonnet-4-6-20250514", usage)
    assert cost is not None
    assert abs(cost - 3.0) < 1e-9  # $3/1M input


# ──────────────────────────────────────────────────────────────────────────
# Test 2: unknown model returns None
# ──────────────────────────────────────────────────────────────────────────


def test_calculate_usd_unknown_model():
    """Unknown model ID should return None."""
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }
    assert calculate_usd("gpt-999-ultra", usage) is None


# ──────────────────────────────────────────────────────────────────────────
# Test 3: cache tokens extracted from adapted (Anthropic-shaped) response
# ──────────────────────────────────────────────────────────────────────────


def test_cache_tokens_extracted():
    """extract_usage reads cache_read_input_tokens from Anthropic-shaped response."""

    class _Usage:
        prompt_tokens = 100
        completion_tokens = 50
        total_tokens = 150
        cache_read_input_tokens = 500
        cache_creation_input_tokens = 200

    class _Resp:
        usage = _Usage()

    result = extract_usage(_Resp())
    assert result is not None
    assert result["cache_read_tokens"] == 500
    assert result["cache_creation_tokens"] == 200
    assert result["prompt_tokens"] == 100
    assert result["completion_tokens"] == 50


def test_extract_usage_no_cache_fields():
    """extract_usage returns 0 for missing cache fields (older providers)."""

    class _Usage:
        prompt_tokens = 10
        completion_tokens = 5
        total_tokens = 15

    class _Resp:
        usage = _Usage()

    result = extract_usage(_Resp())
    assert result is not None
    assert result["cache_read_tokens"] == 0
    assert result["cache_creation_tokens"] == 0

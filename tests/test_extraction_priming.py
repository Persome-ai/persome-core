"""Tests for E4 known-memory priming + anti-anchoring in the chat extractor.

Covers the switch (off → no priming block, byte-identical to pre-feature),
on + injected fake summary → block present incl. anti-anchoring text, and the
TTL cache (provider built once within the window).
"""

from __future__ import annotations

import pytest

from persome.chat import memory_extractor as me
from persome.config import Config, ModelConfig


def _cfg(*, priming: bool) -> Config:
    cfg = Config(models={"default": ModelConfig(model="gpt-5.4-nano")})
    # config.py is intentionally not edited yet; the extractor reads the flag via
    # getattr with a False fallback, so we set it directly here.
    object.__setattr__(cfg, "extraction_known_memory_priming", priming)
    return cfg


@pytest.fixture(autouse=True)
def _reset_provider():
    """Each test starts from the default empty provider + cleared cache."""
    me.set_known_memory_provider(None)
    yield
    me.set_known_memory_provider(None)


# ─── switch off (default) ────────────────────────────────────────────────────


def test_off_prompt_has_no_known_memory_block() -> None:
    cfg = _cfg(priming=False)
    block = me._build_known_memory_block(cfg)
    assert block == ""

    prompt = me._render_prompt("User: hi", block)
    assert "Known memory summary" not in prompt
    assert "trust the current observation" not in prompt
    # Placeholder fully collapsed; conversation still substituted.
    assert "{known_memory}" not in prompt
    assert "{conversation}" not in prompt
    assert "User: hi" in prompt


def test_off_render_is_byte_identical_to_legacy_substitution() -> None:
    """With priming off, the rendered prompt equals the raw template with only
    the conversation substituted (no extra bytes)."""
    cfg = _cfg(priming=False)
    rendered = me._render_prompt("User: hi", me._build_known_memory_block(cfg))
    expected = me._load_prompt().replace("{known_memory}", "").replace("{conversation}", "User: hi")
    assert rendered == expected


def test_off_ignores_injected_provider() -> None:
    cfg = _cfg(priming=False)
    me.set_known_memory_provider(lambda: "USER LIKES PYTHON")
    assert me._build_known_memory_block(cfg) == ""


# ─── switch on + injected summary ────────────────────────────────────────────


def test_on_prompt_contains_summary_and_anti_anchoring() -> None:
    cfg = _cfg(priming=True)
    me.set_known_memory_provider(lambda: "Known: user prefers dark mode.")

    block = me._build_known_memory_block(cfg)
    assert "Known: user prefers dark mode." in block
    # Anti-anchoring directive must be present (current observation wins; hint
    # only; do not invent facts not in the conversation).
    assert "trust the current observation" in block
    assert "must" in block and "not invent facts" in block

    prompt = me._render_prompt("User: actually I want light mode", block)
    assert "Known: user prefers dark mode." in prompt
    assert "trust the current observation" in prompt
    assert "User: actually I want light mode" in prompt


def test_on_empty_summary_yields_no_block() -> None:
    cfg = _cfg(priming=True)
    me.set_known_memory_provider(lambda: "   ")  # whitespace-only → nothing
    assert me._build_known_memory_block(cfg) == ""


def test_on_failing_provider_fails_open() -> None:
    cfg = _cfg(priming=True)

    def _boom() -> str:
        raise RuntimeError("provider down")

    me.set_known_memory_provider(_boom)
    # Must not raise; degrades to no block.
    assert me._build_known_memory_block(cfg) == ""


# ─── TTL cache ───────────────────────────────────────────────────────────────


def test_cache_hit_provider_called_once_within_window() -> None:
    calls = {"n": 0}

    def _counting() -> str:
        calls["n"] += 1
        return f"summary v{calls['n']}"

    me.set_known_memory_provider(_counting)

    # Two builds at the same logical time → provider invoked once.
    first = me._cached_known_memory(now=1000.0)
    second = me._cached_known_memory(now=1000.0 + me._KNOWN_MEMORY_TTL_SECONDS / 2)
    assert first == "summary v1"
    assert second == "summary v1"
    assert calls["n"] == 1


def test_cache_rebuilds_after_ttl() -> None:
    calls = {"n": 0}

    def _counting() -> str:
        calls["n"] += 1
        return f"summary v{calls['n']}"

    me.set_known_memory_provider(_counting)

    first = me._cached_known_memory(now=1000.0)
    later = me._cached_known_memory(now=1000.0 + me._KNOWN_MEMORY_TTL_SECONDS + 1.0)
    assert first == "summary v1"
    assert later == "summary v2"
    assert calls["n"] == 2


def test_build_block_uses_cache_across_two_extractions() -> None:
    """The chokepoint used by extraction (_build_known_memory_block) should not
    rebuild the summary on a second call inside the TTL window."""
    calls = {"n": 0}

    def _counting() -> str:
        calls["n"] += 1
        return "stable summary"

    cfg = _cfg(priming=True)
    me.set_known_memory_provider(_counting)

    b1 = me._build_known_memory_block(cfg)
    b2 = me._build_known_memory_block(cfg)
    assert b1 == b2
    assert "stable summary" in b1
    assert calls["n"] == 1

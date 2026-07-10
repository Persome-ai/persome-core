"""Tests for F5: precise token counting via count_tokens_api.

``count_tokens_api`` calls the Anthropic SDK ``messages.count_tokens``
endpoint; it returns None on mock-mode, when disabled, or on any SDK
failure (so callers fall back to ``_estimate_tokens``).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from persome.config import Config, ModelConfig, WriterConfig
from persome.writer import llm as llm_mod
from persome.writer.llm import _estimate_tokens, count_tokens_api


def _make_cfg(model: str = "claude-sonnet-4-6", use_api: bool = True) -> Config:
    cfg = Config(writer=WriterConfig(use_token_count_api=use_api))
    cfg.models["default"] = ModelConfig(model=model)
    return cfg


_MSGS = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]


def _patch_client(monkeypatch, count_tokens) -> list[dict]:
    """Patch ``_anthropic_client`` so count_tokens_api hits a fake instead of the wire.
    Returns a list capturing each ``messages.count_tokens`` kwargs dict."""
    calls: list[dict] = []

    class _FakeMessages:
        def count_tokens(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return count_tokens(**kwargs)

    monkeypatch.setattr(
        llm_mod, "_anthropic_client", lambda: SimpleNamespace(messages=_FakeMessages())
    )
    return calls


# ──────────────────────────────────────────────────────────────────────────
# Test 1: falls back to None when the SDK raises
# ──────────────────────────────────────────────────────────────────────────


def test_falls_back_to_none_on_error(monkeypatch):
    """When the SDK count_tokens call raises, count_tokens_api returns None (degrades)."""
    monkeypatch.delenv("PERSOME_LLM_MOCK", raising=False)

    def _boom(**kwargs: Any) -> Any:
        raise RuntimeError("network error")

    _patch_client(monkeypatch, _boom)
    cfg = _make_cfg()
    assert count_tokens_api(cfg, "classifier", _MSGS) is None


# ──────────────────────────────────────────────────────────────────────────
# Test 2: successful count returns input_tokens with a bare model + system split
# ──────────────────────────────────────────────────────────────────────────


def test_successful_count_returns_input_tokens(monkeypatch):
    """A normal SDK return yields the integer input_tokens; model is sent bare."""
    monkeypatch.delenv("PERSOME_LLM_MOCK", raising=False)
    calls = _patch_client(monkeypatch, lambda **kw: SimpleNamespace(input_tokens=42))
    cfg = _make_cfg(model="anthropic/claude-sonnet-4-6")

    result = count_tokens_api(cfg, "classifier", _MSGS)

    assert result == 42
    assert calls[0]["model"] == "claude-sonnet-4-6"  # routing prefix stripped
    assert calls[0]["system"] == "s"  # system message lifted out of messages
    assert all(m["role"] != "system" for m in calls[0]["messages"])


# ──────────────────────────────────────────────────────────────────────────
# Test 3: zero input_tokens degrades to None
# ──────────────────────────────────────────────────────────────────────────


def test_zero_tokens_returns_none(monkeypatch):
    """A zero count (e.g. gateway that doesn't implement the endpoint) → None."""
    monkeypatch.delenv("PERSOME_LLM_MOCK", raising=False)
    _patch_client(monkeypatch, lambda **kw: SimpleNamespace(input_tokens=0))
    cfg = _make_cfg()
    assert count_tokens_api(cfg, "classifier", _MSGS) is None


# ──────────────────────────────────────────────────────────────────────────
# Test 4: mock mode returns None without touching the SDK
# ──────────────────────────────────────────────────────────────────────────


def test_mock_mode_returns_none(monkeypatch):
    monkeypatch.setenv("PERSOME_LLM_MOCK", "1")
    cfg = _make_cfg()
    assert count_tokens_api(cfg, "classifier", _MSGS) is None


# ──────────────────────────────────────────────────────────────────────────
# Test 5: use_token_count_api=False skips API
# ──────────────────────────────────────────────────────────────────────────


def test_disabled_by_config():
    cfg = _make_cfg(use_api=False)
    assert count_tokens_api(cfg, "classifier", _MSGS) is None


# ──────────────────────────────────────────────────────────────────────────
# Test 6: estimate fallback heuristic
# ──────────────────────────────────────────────────────────────────────────


def test_estimate_tokens_heuristic():
    """chars//4 heuristic returns a reasonable positive int."""
    msgs = [{"role": "user", "content": "hello world"}]
    est = _estimate_tokens(msgs)
    assert est > 0

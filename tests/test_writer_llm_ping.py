"""Unit tests for ``writer.llm.ping_stage``.

The integration path (status command rendering ✓ / ✗) is covered in
``test_cli_status.py``. These tests pin down ping_stage's own contract:
mock-env shortcut, success latency, error label format, and truncation.
ping_stage talks to the Anthropic SDK (``anthropic.Anthropic(...).messages.create``).
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from persome.config import Config, ModelConfig
from persome.writer import llm as llm_mod


def _cfg_with_model(model: str = "deepseek-v4-flash") -> Config:
    return Config(models={"default": ModelConfig(model=model)})


def _patch_anthropic(monkeypatch: pytest.MonkeyPatch, create: Callable[..., Any]) -> list[dict]:
    """Patch ``anthropic.Anthropic`` so ping_stage hits ``create`` instead of the wire.
    Returns a list capturing each ``messages.create`` kwargs dict."""
    import anthropic

    calls: list[dict] = []

    class _FakeMessages:
        def create(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return create(**kwargs)

    class _FakeClient:
        def __init__(self, **_kw: Any) -> None:
            self.messages = _FakeMessages()

    monkeypatch.setattr(anthropic, "Anthropic", _FakeClient)
    return calls


def test_ping_stage_mock_env_returns_mocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """PERSOME_LLM_MOCK=1 short-circuits before any SDK import."""
    monkeypatch.setenv("PERSOME_LLM_MOCK", "1")
    cfg = _cfg_with_model()

    res = llm_mod.ping_stage(cfg, "timeline")

    assert res.ok is True
    assert res.mocked is True
    assert res.latency_ms == 0
    assert res.error is None
    assert res.stage == "timeline"
    assert res.model == "deepseek-v4-flash"


def test_ping_stage_success_records_latency(monkeypatch: pytest.MonkeyPatch) -> None:
    """A normal SDK return yields ok=True with a non-negative latency."""
    monkeypatch.delenv("PERSOME_LLM_MOCK", raising=False)
    calls = _patch_anthropic(monkeypatch, lambda **kw: SimpleNamespace(content=[]))
    cfg = _cfg_with_model()

    res = llm_mod.ping_stage(cfg, "reducer")

    assert res.ok is True
    assert res.mocked is False
    assert res.error is None
    assert res.latency_ms is not None and res.latency_ms >= 0
    assert calls[0]["max_tokens"] == 4  # ping keeps the request small and bounded


def test_ping_stage_failure_label_includes_class_and_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raised exception becomes 'ClassName: <first-line>' truncated to 80 chars."""
    monkeypatch.delenv("PERSOME_LLM_MOCK", raising=False)

    class AuthenticationError(Exception):
        pass

    def boom(**kwargs: Any) -> Any:
        raise AuthenticationError("Invalid api key sk-bo***ee")

    _patch_anthropic(monkeypatch, boom)
    cfg = _cfg_with_model()

    res = llm_mod.ping_stage(cfg, "classifier")

    assert res.ok is False
    assert res.error is not None
    assert res.error.startswith("AuthenticationError")
    assert "Invalid api key" in res.error
    assert len(res.error) <= 80


def test_ping_stage_failure_with_empty_message_falls_back_to_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When str(exc) is empty, the error label is just the class name."""
    monkeypatch.delenv("PERSOME_LLM_MOCK", raising=False)

    class Timeout(Exception):
        pass

    def boom(**kwargs: Any) -> Any:
        raise Timeout()

    _patch_anthropic(monkeypatch, boom)
    cfg = _cfg_with_model()

    res = llm_mod.ping_stage(cfg, "compact")

    assert res.ok is False
    assert res.error == "Timeout"


def test_ping_stage_truncates_long_error_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """Error labels are capped so a verbose provider message can't blow up status output."""
    monkeypatch.delenv("PERSOME_LLM_MOCK", raising=False)

    class ProviderError(Exception):
        pass

    def boom(**kwargs: Any) -> Any:
        raise ProviderError("x" * 500)

    _patch_anthropic(monkeypatch, boom)
    cfg = _cfg_with_model()

    res = llm_mod.ping_stage(cfg, "timeline")

    assert res.ok is False
    assert res.error is not None
    assert len(res.error) <= 80

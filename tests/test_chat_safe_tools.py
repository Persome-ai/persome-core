"""The paper Chat surface must not silently acquire shell or network access."""

from __future__ import annotations

from types import SimpleNamespace

from persome import config as config_mod
from persome.chat import handler
from persome.chat.tools import CHAT_SCHEMA_NAMES, SAFE_CHAT_SCHEMA_NAMES


class _AgentCapture:
    def __init__(self, _cfg, schemas, handlers, **_kwargs) -> None:
        self.schema_names = {schema["function"]["name"] for schema in schemas}
        self.handler_names = set(handlers)


def _build(monkeypatch, *, unsafe: bool) -> _AgentCapture:
    cfg = config_mod.Config()
    cfg.chat.unsafe_local_tools_enabled = unsafe
    monkeypatch.setattr(handler, "ChatAgent", _AgentCapture)
    monkeypatch.setattr(
        handler,
        "_load_skills",
        lambda: SimpleNamespace(schemas=[], handlers={}),
    )
    return handler._build_agent(cfg)


def test_chat_exposes_only_model_read_tools_by_default(monkeypatch) -> None:
    agent = _build(monkeypatch, unsafe=False)

    assert agent.schema_names == SAFE_CHAT_SCHEMA_NAMES
    assert agent.handler_names == SAFE_CHAT_SCHEMA_NAMES
    assert {"run_command", "write_file", "edit_file", "web_search"}.isdisjoint(agent.schema_names)


def test_chat_unsafe_local_tools_require_explicit_opt_in(monkeypatch) -> None:
    agent = _build(monkeypatch, unsafe=True)

    assert agent.schema_names == CHAT_SCHEMA_NAMES
    assert agent.handler_names == CHAT_SCHEMA_NAMES

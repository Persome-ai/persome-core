"""The paper Chat surface must not silently acquire shell or network access."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from persome import config as config_mod
from persome.chat import handler
from persome.chat import skills as skills_mod
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
        lambda **_kwargs: SimpleNamespace(schemas=[], handlers={}),
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


def test_executable_skill_modules_follow_the_same_unsafe_opt_in(monkeypatch) -> None:
    entry = skills_mod.SkillEntry(
        name="example",
        description="example skill",
        body="instructions",
        source_path="/tmp/example/SKILL.md",
        tools_py=Path("/tmp/example/tools.py"),
    )
    monkeypatch.setattr(skills_mod, "_discover_external_skills", lambda _path: [entry])
    monkeypatch.setattr(skills_mod, "_discover_memory_skills", lambda: [])
    loaded: list[str] = []
    monkeypatch.setattr(
        skills_mod,
        "_load_tools_py",
        lambda skill, _seen: loaded.append(skill.name),
    )

    safe = skills_mod.load_all_skills(allow_executable_tools=False)
    assert loaded == []
    assert {s["function"]["name"] for s in safe.schemas} == {"load_skill"}

    skills_mod.load_all_skills(allow_executable_tools=True)
    assert loaded == ["example"]

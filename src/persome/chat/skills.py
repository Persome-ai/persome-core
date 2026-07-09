"""Dynamic skill loader for chat — scans skills directories and loads prompts + tools.

Follows the Claude Code skill convention:
- SKILL.md with YAML frontmatter (name + description) and markdown body
- System prompt gets only the skill index (name + description per skill)
- A ``load_skill`` tool lets the AI fetch the full body on demand
"""

from __future__ import annotations

import importlib.util
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import paths
from ..logger import get as _get_logger

_logger = _get_logger("persome.chat_skills")


@dataclass
class SkillEntry:
    name: str
    description: str
    body: str
    source_path: str
    tools_py: Path | None = None
    schemas: list[dict[str, Any]] = field(default_factory=list)
    handlers: dict[str, Callable[[dict[str, Any]], Any]] = field(default_factory=dict)


# ─── frontmatter parsing ─────────────────────────────────────────────────

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)", re.DOTALL)


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse YAML-like frontmatter from a markdown file.

    Returns (metadata_dict, body). Only handles simple ``key: value`` pairs.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text

    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        colon = line.find(":")
        if colon == -1:
            continue
        key = line[:colon].strip()
        val = line[colon + 1 :].strip()
        meta[key] = val

    return meta, m.group(2)


# ─── skill discovery ─────────────────────────────────────────────────────


def _discover_external_skills(skills_dir: Path) -> list[SkillEntry]:
    """Scan ~/.persome/skills/*/SKILL.md and parse frontmatter."""
    results: list[SkillEntry] = []
    if not skills_dir.is_dir():
        return results
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        try:
            text = skill_md.read_text()
        except OSError as exc:
            _logger.warning("failed to read %s: %s", skill_md, exc)
            continue

        meta, body = _parse_frontmatter(text)
        name = meta.get("name", skill_dir.name)
        description = meta.get("description", "")
        if not description:
            _logger.warning("skill %s has no description in frontmatter, skipping", name)
            continue

        tools_py = skill_dir / "tools.py"
        results.append(
            SkillEntry(
                name=name,
                description=description,
                body=body.strip(),
                source_path=str(skill_md),
                tools_py=tools_py if tools_py.exists() else None,
            )
        )
    return results


def _discover_dream_skills() -> list[SkillEntry]:
    """Scan ~/.persome/memory/skills/skill-*.md for dream-generated skills."""
    results: list[SkillEntry] = []
    memory_skills_dir = paths.memory_dir() / "skills"
    if not memory_skills_dir.is_dir():
        return results
    for f in sorted(memory_skills_dir.glob("skill-*.md")):
        try:
            text = f.read_text()
        except OSError as exc:
            _logger.warning("failed to read %s: %s", f, exc)
            continue

        meta, body = _parse_frontmatter(text)
        name = meta.get("name", f.stem)
        description = meta.get("description", "")
        if not body.strip():
            continue
        if not description:
            first_line = body.strip().splitlines()[0] if body.strip() else ""
            description = first_line[:120]

        results.append(
            SkillEntry(
                name=name,
                description=description,
                body=body.strip(),
                source_path=str(f),
            )
        )
    return results


# ─── tool loading ─────────────────────────────────────────────────────────


def _load_tools_py(skill: SkillEntry, seen_names: set[str]) -> None:
    """Dynamically import a skill's tools.py and populate schemas + handlers."""
    if skill.tools_py is None:
        return

    module_name = f"persome_skill_{skill.name}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, skill.tools_py)
        if spec is None or spec.loader is None:
            _logger.warning("cannot create module spec for %s", skill.tools_py)
            return
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)

        schemas: list[dict[str, Any]] = getattr(mod, "SCHEMAS", [])
        handlers: dict[str, Callable] = getattr(mod, "HANDLERS", {})

        for schema in schemas:
            fn = schema.get("function", {})
            name = fn.get("name", "")
            if not name:
                continue
            if name in seen_names:
                _logger.warning(
                    "skill %s: tool '%s' conflicts with an existing tool, skipping",
                    skill.name,
                    name,
                )
                continue
            seen_names.add(name)
            skill.schemas.append(schema)

        for name, handler in handlers.items():
            if name not in seen_names:
                _logger.warning(
                    "skill %s: handler '%s' has no matching schema, skipping",
                    skill.name,
                    name,
                )
                continue
            skill.handlers[name] = handler

    except Exception as exc:
        _logger.warning("failed to load skill tools from %s: %s", skill.tools_py, exc)
        if module_name in sys.modules:
            del sys.modules[module_name]


# ─── public API ───────────────────────────────────────────────────────────


@dataclass
class LoadedSkills:
    """All loaded skills: index prompt, schemas, handlers, and the full registry."""

    entries: dict[str, SkillEntry]
    index_prompt: str
    schemas: list[dict[str, Any]]
    handlers: dict[str, Callable[[dict[str, Any]], Any]]


def load_all_skills(builtin_names: set[str] | None = None) -> LoadedSkills:
    """Discover and load all skills.

    Returns a LoadedSkills with:
    - index_prompt: skill name + description list for system prompt injection
    - schemas: merged tool schemas from all skills (+ the built-in load_skill tool)
    - handlers: merged tool handlers from all skills (+ the built-in load_skill handler)
    - entries: full registry keyed by skill name, for load_skill lookups
    """
    external = _discover_external_skills(paths.skills_dir())
    dream = _discover_dream_skills()
    all_skills = external + dream

    if not all_skills:
        return LoadedSkills(entries={}, index_prompt="", schemas=[], handlers={})

    # Build registry
    entries: dict[str, SkillEntry] = {}
    for skill in all_skills:
        if skill.name in entries:
            _logger.warning("duplicate skill name '%s', keeping first", skill.name)
            continue
        entries[skill.name] = skill

    # Load tool modules
    seen_names: set[str] = set(builtin_names or ())
    seen_names.add("load_skill")
    for skill in entries.values():
        _load_tools_py(skill, seen_names)

    # Build index prompt
    lines: list[str] = [
        "\n## Available skills",
        "",
        "Use ``load_skill`` to load a skill's full instructions before executing it.",
        "",
    ]
    for skill in entries.values():
        lines.append(f"- **{skill.name}**: {skill.description}")
    index_prompt = "\n".join(lines) + "\n"

    # Merge schemas and handlers
    merged_schemas: list[dict[str, Any]] = []
    merged_handlers: dict[str, Callable[[dict[str, Any]], Any]] = {}

    for skill in entries.values():
        merged_schemas.extend(skill.schemas)
        merged_handlers.update(skill.handlers)

    # Add the built-in load_skill tool
    merged_schemas.append(_LOAD_SKILL_SCHEMA)
    merged_handlers["load_skill"] = _make_load_skill_handler(entries)

    return LoadedSkills(
        entries=entries,
        index_prompt=index_prompt,
        schemas=merged_schemas,
        handlers=merged_handlers,
    )


# ─── built-in load_skill tool ────────────────────────────────────────────

_LOAD_SKILL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "load_skill",
        "description": (
            "Load the full instructions of a skill by name. "
            "Call this before executing a skill to get its complete steps and context."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The skill name to load.",
                },
            },
            "required": ["name"],
        },
    },
}


def _make_load_skill_handler(
    entries: dict[str, SkillEntry],
) -> Callable[[dict[str, Any]], Any]:
    def handler(args: dict[str, Any]) -> Any:
        name = args.get("name", "")
        skill = entries.get(name)
        if skill is None:
            available = list(entries.keys())
            return {"error": f"skill '{name}' not found", "available_skills": available}
        return {
            "name": skill.name,
            "description": skill.description,
            "instructions": skill.body,
        }

    return handler

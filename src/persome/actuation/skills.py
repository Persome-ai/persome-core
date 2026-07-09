"""Per-app operation skills — progressive disclosure for the actuation agent.

Every app the agent drives has quirks (AX-rich vs OCR-only vs fully blind, input idioms, send traps)
that are KNOWLEDGE, not scripts — the LLM still does the thinking; the skill just hands it the app's
idioms so it doesn't have to rediscover them by trial and error on the user's real app. Validated in
the benchmark (`tests/manual/skills/*.md`), promoted here for production.

To keep the agent's context lean, skills are disclosed ON DEMAND, not dumped into the system prompt:
- the agent sees only a one-line summary per app (`list_skills`) — a cheap menu;
- the full manual for an app is injected the FIRST time focus lands on it (ui_activate / ui_open_app)
  and is fetchable explicitly via the `ui_app_guide` tool.

Skills are markdown files in `skills/` with a small frontmatter header:

    ---
    app: WeChat
    bundles: com.tencent.xinWeChat
    summary: one-line "what's tricky about this app"
    ---
    <markdown body — the operation manual>
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from ..logger import get

logger = get("persome.actuation.skills")

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


@dataclass(frozen=True)
class Skill:
    app: str
    bundles: tuple[str, ...]
    summary: str
    body: str
    aliases: tuple[str, ...] = ()


def _parse(text: str) -> Skill | None:
    """Parse one skill markdown (frontmatter + body). None if the header is missing/invalid."""
    m = _FRONTMATTER_RE.match(text.lstrip("﻿"))
    if not m:
        return None
    header, body = m.group(1), m.group(2).strip()
    fields: dict[str, str] = {}
    for line in header.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fields[k.strip().lower()] = v.strip()
    app = fields.get("app", "").strip()
    if not app or not body:
        return None

    def _csv(key: str) -> tuple[str, ...]:
        raw = fields.get(key, "").strip().strip("[]")
        return tuple(v.strip().strip("'\"") for v in raw.split(",") if v.strip())

    return Skill(
        app=app,
        bundles=_csv("bundles"),
        summary=fields.get("summary", "").strip(),
        body=body,
        aliases=_csv("aliases"),
    )


@lru_cache(maxsize=1)
def _load_all() -> tuple[Skill, ...]:
    """Read + parse every skills/*.md once (cached). Best-effort: a bad file is skipped, not fatal."""
    skills: list[Skill] = []
    try:
        from importlib.resources import files as _pkg_files

        skill_dir = _pkg_files("persome.actuation").joinpath("skills")
        for entry in skill_dir.iterdir():
            if not entry.name.endswith(".md"):
                continue
            try:
                s = _parse(entry.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                logger.warning("skill %s unreadable: %s", entry.name, exc)
                continue
            if s:
                skills.append(s)
    except (ModuleNotFoundError, FileNotFoundError, OSError) as exc:
        logger.warning("skills dir not available: %s", exc)
    return tuple(sorted(skills, key=lambda s: s.app.lower()))


def list_skills() -> list[dict[str, str]]:
    """The lean menu: `[{app, summary}]` for every app with a skill — cheap to show the agent so it
    knows a manual exists without paying for the full body."""
    return [{"app": s.app, "summary": s.summary} for s in _load_all()]


def guide_for(app_or_bundle: str) -> Skill | None:
    """Resolve an app NAME or bundle id to its skill, else None. Matches the app name (case-insensitive,
    either direction of substring so "Google Chrome" finds a "Chrome" skill) or any of its bundle ids."""
    key = (app_or_bundle or "").strip().lower()
    if not key:
        return None
    for s in _load_all():
        a = s.app.lower()
        if a == key or a in key or key in a:
            return s
        if any(b.lower() == key for b in s.bundles):
            return s
        if any(al.lower() == key or al.lower() in key for al in s.aliases):
            return s
    return None

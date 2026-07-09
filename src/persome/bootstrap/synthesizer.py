"""Cold-start profiling — harness-orchestrated, not model-self-directed.

A flash-tier model is too unreliable to drive thorough agent-native exploration
on its own (it under-reads and skips fanning out). So the harness drives the loop:

1. ``collectors.run_all()`` — deterministic baseline signals.
2. ``subagent.anchor_owner()`` — the machine owner from the strongest signals.
3. ``subagent.pick_areas()`` — deterministically choose high-value home dirs.
4. ``subagent.run_explorers()`` — one explorer sub-agent per area, **in parallel**,
   each pushed to read several high-value files.
5. ``_synthesize_profile()`` — a single tool-less ``call_llm`` turns the baseline +
   findings into the profile JSON. No tools in this step ⇒ no malformed tool-call
   JSON ⇒ robust.

``synthesize`` returns ``(Profile | None, results, FsRecorder)``; on failure the
runner falls back to a structured-only report.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config
from ..logger import get
from ..prompts import load as load_prompt
from . import fs_tools, subagent
from .collectors import CollectorResult

logger = get("persome.bootstrap")

# --- clue heuristics (filename-only, no LLM) ------------------------------
#
# The onboarding s3 "scanning" screen wants a cheap, honest read of each picked
# folder *before* the LLM does any deep reading: a kind, a short tag, a templated
# title, and a real item count. All of it is derived from the folder name + a
# stat of its direct children — never from file contents.

# (kind, [name hints that imply it]). First match wins; order matters.
#
# Matching is split by script so a hint can't over-fire (issue #320):
#   - CJK hints match as substrings (Chinese has no word boundaries), so they
#     must be discriminative multi-char words — never bare single chars like
#     "信"/"家"/"图" that hide inside 微信/搬家/截图 and mis-tag everything.
#   - ASCII hints match as whole tokens (the name is split on non-alnum), so
#     "cv" no longer fires on archive/recovery and "work" no longer fires on
#     network/homework.
_CLUE_KIND_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("resume", ("简历", "resume", "cv", "履历", "求职")),
    ("diary", ("日记", "diary", "journal", "随笔", "手记")),
    ("letter", ("家书", "书信", "信件", "letter")),
    ("memory", ("照片", "老照片", "相册", "记忆", "photo", "memory")),
    ("work", ("工作", "项目", "业务", "客户", "work", "project")),
]
# kind → short Chinese chip label shown on the matched tree row.
_CLUE_TAG: dict[str, str] = {
    "resume": "简历",
    "diary": "随手记",
    "letter": "家",
    "memory": "记忆",
    "work": "工作",
    "other": "痕迹",
}
# kind → templated subtitle suffix ("中文名 · <template>").
_CLUE_TITLE_TEMPLATE: dict[str, str] = {
    "resume": "你的轨迹",
    "diary": "你的声音",
    "letter": "你的牵挂",
    "memory": "你的记忆",
    "work": "你在做的事",
    "other": "你的痕迹",
}

# Called per orchestration step / explorer dispatch: (label, payload). Used by the
# CLI for its live terminal stream; SSE publishing happens in subagent/runner.
ActivityFn = Callable[[str, dict[str, Any]], None]

_MAX_TOKENS = 8192
# Retry the (tool-less) synthesis call on transient gateway hiccups.
_MAX_ATTEMPTS = 2
# The portrait is the quality-critical step — use the stronger model for it.
_SYNTHESIS_MODEL = "deepseek-v4-pro"


@dataclass
class Profile:
    # ── UI 出口（文学画像）— 只走 report.py / stage_end → app BootstrapProfile，
    #    绝不进 *.md 记忆。
    headline: str = ""
    vibe: str = ""  # playful MBTI/星座-style analogy line
    narrative: str = ""  # the one-page personality read
    identity: str = ""  # legacy 散文身份段，仅 UI（不再进记忆）
    preferences: str = ""  # legacy 散文偏好段，仅 UI（不再进记忆）
    confidence_notes: str = ""
    # ── 记忆出口（原子事实）— 只走 sink.py 写 *.md。每条一个可独立 supersede 的断言。
    identity_facts: list[str] = field(default_factory=list)
    preference_facts: list[str] = field(default_factory=list)
    # 实体：每项 {name, facts: list[str]}（facts 为多条原子事实）。
    projects: list[dict[str, Any]] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)
    topics: list[dict[str, Any]] = field(default_factory=list)


def _extract_json(text: str) -> str:
    """Pull the JSON object out of the model reply (handles preamble + ```json fence)."""
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        return fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1].strip()
    return ""


def _last_assistant_text(messages: list[dict[str, Any]]) -> str:
    """Pull the final assistant text from a turn's committed message history.

    Used by explorer sub-agents when the gateway didn't stream the final text as
    deltas (it lands in the message history instead of ``assistant_message``).
    """
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            if content.strip():
                return content
            continue
        if isinstance(content, list):
            parts = [
                str(b.get("text", ""))
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            joined = "".join(parts).strip()
            if joined:
                return joined
    return ""


def _coerce_facts(value: Any) -> list[str]:
    """Coerce a JSON value into a clean list of atomic-fact strings.

    Accepts a list of strings (the new shape), a single string (split on
    newlines as a degraded fallback), or anything else (→ empty). Each fact is
    stripped; blanks are dropped. Order preserved, duplicates removed.
    """
    facts: list[str] = []
    if isinstance(value, list):
        for item in value:
            s = str(item).strip() if not isinstance(item, str) else item.strip()
            if s and s not in facts:
                facts.append(s)
    elif isinstance(value, str):
        for line in value.splitlines():
            s = line.strip().lstrip("-•* ").strip()
            if s and s not in facts:
                facts.append(s)
    return facts


def _coerce_rows(value: Any) -> list[dict[str, Any]]:
    """Coerce entity rows into ``{name, facts: list[str]}``.

    New shape: ``{"name": ..., "facts": [...]}`` (multiple atomic facts per
    entity). Legacy shapes are tolerated so old prompts/tests don't break:
    ``{"name": ..., "note": "..."}`` → a single-fact list; a bare string →
    ``{"name": str, "facts": []}``.
    """
    rows: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return rows
    for item in value:
        if isinstance(item, dict):
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            facts = _coerce_facts(item.get("facts"))
            if not facts:
                note = str(item.get("note", "")).strip()
                if note:
                    facts = [note]
            rows.append({"name": name, "facts": facts})
        elif isinstance(item, str) and item.strip():
            rows.append({"name": item.strip(), "facts": []})
    return rows


def _parse(text: str) -> Profile | None:
    text = _extract_json(text or "")
    if not text:
        logger.info("bootstrap synthesis returned no JSON object")
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("bootstrap synthesis returned non-JSON: %s", exc)
        return None
    if not isinstance(data, dict):
        return None
    return Profile(
        headline=str(data.get("headline", "")).strip(),
        vibe=str(data.get("vibe", "")).strip(),
        narrative=str(data.get("narrative", "")).strip(),
        identity=str(data.get("identity", "")).strip(),
        preferences=str(data.get("preferences", "")).strip(),
        confidence_notes=str(data.get("confidence_notes", "")).strip(),
        identity_facts=_coerce_facts(data.get("identity_facts")),
        preference_facts=_coerce_facts(data.get("preference_facts")),
        projects=_coerce_rows(data.get("projects")),
        tools=_coerce_rows(data.get("tools")),
        topics=_coerce_rows(data.get("topics")),
    )


def _assemble_context(owner: str, explorers: list[dict[str, Any]]) -> str:
    """Build the synthesis input: anchored owner + per-folder file findings."""
    parts = [f"# Anchored machine owner\n{owner}"]
    if explorers:
        parts.append("# What the files say (per folder, from explorer sub-agents)")
        for e in explorers:
            findings = e.get("findings") or "(no findings)"
            parts.append(f"## {e.get('path', '?')}\n{findings}")
    else:
        parts.append("(No file exploration was run.)")
    return "\n\n".join(parts)


def _clue_kind(name: str) -> str:
    """Filename heuristic → clue kind. Cheap, name-only, never reads content.

    CJK hints match as substrings; ASCII hints match as whole tokens (the name
    split on non-alphanumeric chars). This keeps short hints like "cv"/"work"
    from firing inside unrelated words (archive/network) — see issue #320.
    """
    low = name.lower()
    tokens = {t for t in re.split(r"[^0-9a-z]+", low) if t}
    for kind, hints in _CLUE_KIND_HINTS:
        for h in hints:
            if h.isascii():
                if h.lower() in tokens:
                    return kind
            elif h in name:
                return kind
    return "other"


def _clue_detail(path: Path) -> str:
    """A *real*, conservative count of a folder's direct children — never invented.

    Counts non-hidden direct entries (files + subdirs). On any error or empty dir
    we degrade to a truthful low/zero count rather than fabricating a number.
    """
    try:
        entries = [e for e in os.scandir(path) if not e.name.startswith(".")]
    except OSError:
        return "0 项"
    n_files = sum(1 for e in entries if not _safe_is_dir(e))
    n_dirs = sum(1 for e in entries if _safe_is_dir(e))
    if n_files and not n_dirs:
        return f"{n_files} 个文件"
    if n_dirs and not n_files:
        return f"{n_dirs} 个子目录"
    return f"{len(entries)} 项"


def _safe_is_dir(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


def build_clues(areas: list[str]) -> list[dict[str, str]]:
    """Turn picked area paths into s3 clue cards via filename heuristics + real counts.

    One clue per area: ``{path, kind, tag, title, detail}``. ``path`` is relative to
    home (e.g. ``Documents/日记``); ``detail`` is a genuine child count. No LLM, no
    file reads — purely the directory name and a stat of its direct children.
    """
    home = fs_tools._home()
    clues: list[dict[str, str]] = []
    for raw in areas:
        resolved = fs_tools._resolve_under_home(str(raw))
        if resolved is None:
            continue
        name = resolved.name
        kind = _clue_kind(name)
        try:
            rel = str(resolved.relative_to(home)) if resolved != home else "~"
        except ValueError:
            rel = name
        clues.append(
            {
                "path": rel,
                "kind": kind,
                "tag": _CLUE_TAG[kind] if kind != "other" else name,
                "title": f"{name} · {_CLUE_TITLE_TEMPLATE[kind]}",
                "detail": _clue_detail(resolved),
            }
        )
    return clues


def hypothesis_phrase(clues: list[dict[str, str]]) -> str:
    """A ≤8-char noun phrase guessed from clue kinds. Heuristic only — no LLM.

    Default ("正在向前走的人") covers the resume/work-leaning common case and any
    ambiguous mix. letter/memory-heavy → 牵挂; diary-heavy → 写情绪.
    """
    kinds = [c.get("kind", "other") for c in clues]
    if not kinds:
        return "正在向前走的人"
    intimate = sum(1 for k in kinds if k in ("letter", "memory"))
    diary = sum(1 for k in kinds if k == "diary")
    drive = sum(1 for k in kinds if k in ("resume", "work"))
    if intimate and intimate >= diary and intimate >= drive:
        return "把牵挂收好的人"
    if diary and diary >= drive:
        return "习惯把情绪写下来的人"
    return "正在向前走的人"


def _synthesis_cfg(cfg: Config) -> Config:
    """Run the final portrait on the stronger model — prose quality matters here.

    Respects an explicit ``[models.bootstrap]`` in config.toml; otherwise upgrades
    the ``bootstrap`` stage to ``deepseek-v4-pro`` (the explorers stay on the
    cheaper chat model).
    """
    if "bootstrap" in cfg.models:
        return cfg
    base = cfg.model_for("bootstrap")
    boosted = dataclasses.replace(
        base, model=_SYNTHESIS_MODEL, max_tokens=base.max_tokens or _MAX_TOKENS
    )
    return dataclasses.replace(cfg, models={**cfg.models, "bootstrap": boosted})


def _synthesize_profile(cfg: Config, context: str) -> Profile | None:
    """Tool-less synthesis: context → profile JSON. No tools ⇒ robust output."""
    from ..writer import llm as llm_mod

    cfg = _synthesis_cfg(cfg)
    system = load_prompt("bootstrap.md")
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": context + "\n\nNow output the profile JSON."},
    ]
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            resp = llm_mod.call_llm(cfg, "bootstrap", messages=messages, json_mode=True)
            text = llm_mod.extract_text(resp)
        except Exception as exc:  # noqa: BLE001 — degrade, don't crash
            logger.warning(
                "bootstrap synthesis attempt %d/%d failed: %s", attempt, _MAX_ATTEMPTS, exc
            )
            continue
        profile = _parse(text)
        if profile is not None:
            return profile
        logger.info("bootstrap synthesis attempt %d/%d had no usable JSON", attempt, _MAX_ATTEMPTS)
    return None


def synthesize(
    cfg: Config,
    *,
    deep: bool = True,
    on_activity: ActivityFn | None = None,
    exclude: frozenset[str] = frozenset(),
) -> tuple[Profile | None, list[CollectorResult], fs_tools.FsRecorder]:
    """Orchestrate the cold-start profile. Returns ``(profile, results, explored)``.

    Scans the whole home tree (bounded, names only), lets an LLM pick the
    high-value folders, then reads those. With ``deep=False`` (--shallow)
    exploration is skipped entirely. ``exclude`` is the set of top-level home
    folder names the user un-checked on the permission screen — they are never
    scanned, named, or read. ``results`` is always empty (we no longer harvest
    non-file signals); it stays in the return tuple for the report/sink
    interface. ``on_activity`` receives steps for the live stream.
    """
    from .. import events as events_mod

    owner = subagent.anchor_owner()
    explorers: list[dict[str, Any]] = []
    areas: list[str] = []
    if deep:
        if on_activity:
            on_activity("scan_home", {})
        tree = fs_tools.scan_home_tree(exclude=exclude)
        # s3 left pane: the home file tree, verbatim.
        events_mod.publish("bootstrap", "scan_tree", {"tree": tree})
        areas = subagent.pick_areas(cfg, tree, owner)
        # Defense: drop any excluded folder even if the deterministic fallback
        # (used when the triage LLM is unavailable) re-added it.
        if exclude:
            areas = [a for a in areas if a.rstrip("/").rsplit("/", 1)[-1] not in exclude]
        # s3 right pane: one clue card per picked area (filename heuristic + real
        # counts, no LLM), plus a cheap rule-based hypothesis phrase for s4's title.
        clues = build_clues(areas)
        for clue in clues:
            events_mod.publish("bootstrap", "clue", clue)
        events_mod.publish("bootstrap", "hypothesis", {"phrase": hypothesis_phrase(clues)})
        explorers = subagent.run_explorers(cfg, areas, owner, on_activity=on_activity)

    if on_activity:
        on_activity("synthesize", {})
    # s3 → s4 hand-off: we're done scanning, entering final synthesis.
    events_mod.publish("bootstrap", "synth_start", {})
    context = _assemble_context(owner, explorers)
    profile = _synthesize_profile(cfg, context)

    results: list[CollectorResult] = []
    read_files = [f for e in explorers for f in e.get("read_files", [])]
    explored = fs_tools.FsRecorder(listed_dirs=list(areas), read_files=read_files)
    return profile, results, explored

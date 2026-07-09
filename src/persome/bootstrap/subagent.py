"""Harness-orchestrated parallel exploration — the reliable way to get thorough,
Claude-Code-style digging out of a weak model.

Rather than hoping the model decides to fan out (a flash-tier model often won't),
the harness drives the loop: it picks the high-value directories deterministically
(``pick_areas``) and runs one explorer sub-agent per directory **concurrently**
(``run_explorers``). Each explorer is a :class:`ChatAgent` armed with the raw
shell + filesystem tools and is pushed to read *several* high-value files, not
just one. Their findings feed the (tool-less, therefore robust) final synthesis.
"""

from __future__ import annotations

import asyncio
import dataclasses
import getpass
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..config import Config
from ..logger import get
from ..prompts import load as load_prompt
from . import fs_tools, shell_tools

logger = get("persome.bootstrap")

ToolHandler = Callable[[dict[str, Any]], Any]
ActivityFn = Callable[[str, dict[str, Any]], None]

_EXPLORER_SYSTEM = (
    "You are a cold-start exploration sub-agent assigned ONE directory. Explore it "
    "thoroughly to surface high-value information about the machine's OWNER: identity, "
    "profession / what they do, interests, projects/work, habits, region.\n\n"
    "Be thorough, not lazy: list the directory (recursively where useful), then actually "
    "READ several high-value files (résumés, bios, notes, journals, plans, project READMEs, "
    "contracts, docs that reveal the person) — read MULTIPLE files, do not stop after one. "
    "Prefer recently-modified files (mind staleness). Skip dependency/cache/build junk and "
    "anything privacy-irrelevant.\n\n"
    "**Separate the owner from other people mentioned in files.** The directory may hold "
    "other people's material (collaborators' résumés, clients' contracts, contacts). For any "
    "name-bearing content, mark whether it is 'the owner' or 'material the owner keeps about "
    "X'; when unsure choose the latter, and never attribute someone else's identity to the "
    "owner.\n\n"
    "Finally output 5-10 Chinese bullets (each = one concrete finding + the file/evidence "
    "backing it, marked owner vs other person). No preamble, no narration."
)
_EXPLORER_MAX_TOKENS = 3072
# We scan the whole home tree (bounded, names-only — see fs_tools.scan_home_tree)
# and let an LLM pick the highest-value folders to read. ``_AREA_DIRS`` is the
# day-0 fallback when the triage LLM is unavailable / returns nothing: the macOS
# onboarding TCC scope (Desktop / Documents / Downloads) which we know is readable.
_AREA_DIRS = ["Desktop", "Documents", "Downloads"]
# Cap on explorer sub-agents (one per picked folder). Whole-home triage can find
# more candidates than the old fixed-3 scope, but we still bound the fan-out.
_MAX_EXPLORERS = 4
_PICK_MAX_TOKENS = 512


def anchor_owner() -> str:
    """Best-effort machine-owner identity from the strongest local signals."""
    parts = [f"system user: {getpass.getuser()}"]
    name = (
        shell_tools.run_shell("git config --global user.name", timeout=5).get("output", "").strip()
    )
    email = (
        shell_tools.run_shell("git config --global user.email", timeout=5).get("output", "").strip()
    )
    if name:
        parts.append(f"git name: {name}")
    if email:
        parts.append(f"git email: {email}")
    return "; ".join(parts)


def _fallback_areas(max_n: int = _MAX_EXPLORERS) -> list[str]:
    """Day-0 safety net: the TCC-scoped folders (Desktop/Documents/Downloads) that
    actually exist. Used when the triage LLM is unavailable or returns nothing."""
    home = Path.home()
    return [str(home / name) for name in _AREA_DIRS if (home / name).is_dir()][:max_n]


def _parse_pick(text: str) -> list[str]:
    """Robustly pull a JSON string-array of folder paths from an LLM reply.

    Handles ```json fences, preamble, and stray prose. Keeps only entries that
    resolve to an existing directory inside home (the LLM works off a names-only
    tree and may hallucinate or pick a now-missing path)."""
    raw = text or ""
    fenced = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", raw, re.DOTALL)
    if fenced:
        raw = fenced.group(1)
    else:
        start, end = raw.find("["), raw.rfind("]")
        if start != -1 and end > start:
            raw = raw[start : end + 1]
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    picked: list[str] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, str) or not item.strip():
            continue
        resolved = fs_tools._resolve_under_home(item.strip())
        if resolved is None or not resolved.is_dir():
            continue
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        picked.append(key)
    return picked


def pick_areas(
    cfg: Config | None = None,
    tree: str | None = None,
    owner: str = "",
    *,
    max_n: int = _MAX_EXPLORERS,
) -> list[str]:
    """Pick the high-value home folders to explore.

    Feeds the bounded whole-home directory tree (names only) + the machine owner
    to one triage LLM call, which returns a JSON array of folder paths. On any
    failure (no cfg, no tree, LLM error, empty/garbage reply) we fall back to the
    TCC-scoped folders so day-0 is never blank.

    Backwards-compatible: called with no args it just returns the fallback set.
    """
    if cfg is None or not (tree or "").strip():
        return _fallback_areas(max_n)

    from ..writer import llm as llm_mod

    system = load_prompt("bootstrap_pick.md").replace("{max_n}", str(max_n))
    user = f"# Anchored machine owner\n{owner or '(unknown)'}\n\n# Home directory tree\n{tree}"
    try:
        # Cap the (tiny) triage reply via the stage's model config — call_llm
        # takes no per-call max_tokens (mirrors synthesizer._synthesis_cfg).
        pick_cfg = dataclasses.replace(
            cfg,
            models={
                **cfg.models,
                "bootstrap": dataclasses.replace(
                    cfg.model_for("bootstrap"), max_tokens=_PICK_MAX_TOKENS
                ),
            },
        )
        resp = llm_mod.call_llm(
            pick_cfg,
            "bootstrap",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        picked = _parse_pick(llm_mod.extract_text(resp))
    except Exception as exc:  # noqa: BLE001 — triage must never crash the run
        logger.warning("pick_areas LLM triage failed, falling back: %s", exc)
        return _fallback_areas(max_n)

    if not picked:
        logger.info("pick_areas LLM returned no usable folders, falling back")
        return _fallback_areas(max_n)
    return picked[:max_n]


def survey_areas() -> str:
    """A bounded file-tree listing of the fallback folders (for --no-llm / --json).

    Offline path: no LLM triage, so we just survey the TCC-scoped folders."""
    _schemas, handlers, _rec = _explorer_toolset()
    parts: list[str] = []
    for area in _fallback_areas():
        out = handlers["list_dir"]({"path": area, "depth": 2})
        parts.append(f"## {area}\n{out.get('tree', out.get('error', ''))}")
    return "\n\n".join(parts)


def _rel_to_home(path: str) -> str:
    """Best-effort home-relative path for the s4 'reading xxx' line; absolute on miss."""
    try:
        home = fs_tools._home()
        p = Path(path).resolve()
        return str(p.relative_to(home)) if p != home else "~"
    except (ValueError, OSError):
        return path


def _explorer_toolset(
    *, publish_reads: bool = False
) -> tuple[list[dict[str, Any]], dict[str, ToolHandler], fs_tools.FsRecorder]:
    """Tools a sub-explorer gets: raw shell + read-only fs. No spawn (no recursion).

    With ``publish_reads`` the ``read_file`` handler is wrapped so each successful
    read emits a per-file ``bootstrap``/``read`` event — the s4 'reading xxx' line
    updates per file, not per folder.
    """
    sh_schemas, sh_handlers = shell_tools.build_shell_tools()
    fs_schemas, fs_handlers, rec = fs_tools.build_fs_tools()
    handlers: dict[str, ToolHandler] = {**sh_handlers, **fs_handlers}
    if publish_reads:
        from .. import events as events_mod

        inner = handlers["read_file"]

        def read_file_publishing(args: dict[str, Any]) -> Any:
            out = inner(args)
            # Only announce reads that actually returned content (handler reports a
            # resolved ``path`` on success; errors carry no ``path``).
            if isinstance(out, dict) and out.get("path") and not out.get("error"):
                events_mod.publish("bootstrap", "read", {"path": _rel_to_home(str(out["path"]))})
            return out

        handlers["read_file"] = read_file_publishing
    return sh_schemas + fs_schemas, handlers, rec


async def _explore_one(cfg: Config, path: str, owner: str = "") -> dict[str, Any]:
    from ..chat.agent import ChatAgent

    path = str(path or "~").strip() or "~"
    schemas, handlers, rec = _explorer_toolset(publish_reads=True)
    owner_line = (
        f"Known machine owner: {owner}. Use it to separate the owner from other people.\n"
        if owner.strip()
        else ""
    )
    kickoff = (
        f"Explore this directory thoroughly: {path}\n{owner_line}"
        "List it, then READ several high-value files. Summarize as 5-10 Chinese bullets "
        "(mark owner vs other person)."
    )
    agent = ChatAgent(cfg.chat, schemas, handlers, daemon_mcp_url="")
    await agent.aopen()
    try:
        result = await agent.run_turn(
            [{"role": "user", "content": kickoff}],
            _EXPLORER_SYSTEM,
            max_tokens=_EXPLORER_MAX_TOKENS,
            thinking_budget=0,
        )
    except Exception as exc:  # noqa: BLE001 — one explorer failing must not kill the rest
        logger.warning("explorer for %s failed: %s", path, exc)
        return {"path": path, "findings": "", "read_files": list(rec.read_files), "error": str(exc)}
    finally:
        await agent.aclose()
    text = result.assistant_message.strip()
    if not text:
        from .synthesizer import _last_assistant_text

        text = _last_assistant_text(result.messages)
    return {"path": path, "findings": text, "read_files": list(rec.read_files)}


def run_explorers(
    cfg: Config,
    areas: list[str],
    owner: str = "",
    *,
    on_activity: ActivityFn | None = None,
) -> list[dict[str, Any]]:
    """Run one explorer per area concurrently; return their findings.

    Synchronous wrapper (uses ``asyncio.run`` on a fresh loop) so it can be called
    from the sync runner. Each explorer's failure is isolated.
    """
    from .. import events as events_mod

    areas = [a for a in areas if str(a).strip()][:_MAX_EXPLORERS]
    if not areas:
        return []
    for a in areas:
        events_mod.publish("bootstrap", "tool_call", {"name": "explore", "arguments": {"path": a}})
        if on_activity:
            on_activity("explore", {"path": a})

    async def _all() -> list[dict[str, Any]]:
        return await asyncio.gather(*(_explore_one(cfg, a, owner) for a in areas))

    try:
        return asyncio.run(_all())
    except Exception as exc:  # noqa: BLE001
        logger.warning("run_explorers failed: %s", exc)
        return []

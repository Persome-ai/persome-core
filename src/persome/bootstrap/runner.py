"""Orchestrate the cold-start harvest.

Modes:

- **default** — ``synthesizer.synthesize`` runs the harness-orchestrated flow
  (baseline collectors → anchor owner → parallel explorer sub-agents →
  tool-less synthesis) and returns the profile, the baseline results, and which
  dirs/files were explored. The report renders that evidence; the sink writes
  the profile.
- **--no-llm / fallback** — run every collector directly (no agent) and render a
  structured-only report. Also used when synthesis is unavailable, so a flaky
  model never breaks the demo.

Live activity: orchestration steps + each explorer dispatch are published on the
event bus (``bootstrap`` stage) so the app's debug HUD shows them over SSE, and
— in the CLI — printed as a terminal stream. ``--json`` dumps the raw signal
bundle.

``run`` is the CLI entry (terminal output + report). ``run_headless`` is the
daemon entry (events only, writes memory, no console) behind ``POST
/bootstrap/run``.
"""

from __future__ import annotations

import dataclasses

from rich.console import Console
from rich.rule import Rule

from .. import events
from ..config import Config
from ..logger import get
from . import report, sink, subagent, synthesizer

logger = get("persome.bootstrap")


def _activity_line(name: str, args: dict) -> str:
    """Human one-liner for an orchestration step, e.g. '🤖 探索 ~/Documents'."""
    if name == "explore":
        return f"🤖 派探索子 agent → {args.get('path', '')}".rstrip()
    if name == "synthesize":
        return "🧬 合成人格画像"
    return f"· {name}".rstrip()


def run(
    cfg: Config,
    *,
    use_llm: bool = True,
    dry_run: bool = False,
    as_json: bool = False,
    deep: bool = True,
) -> int:
    """CLI entry: explore Desktop/Documents/Downloads → personality page → memory."""
    logger.info(
        "bootstrap start: use_llm=%s dry_run=%s as_json=%s deep=%s",
        use_llm,
        dry_run,
        as_json,
        deep,
    )

    if as_json:
        print(subagent.survey_areas())  # noqa: T201 — intentional CLI output (file tree)
        return 0

    if not use_llm:
        # Offline: just show the file tree of the scoped folders (no LLM to read them).
        console = Console()
        console.print(Rule("[dim]Desktop / Documents / Downloads 文件结构[/dim]", style="dim"))
        console.print(subagent.survey_areas())
        return 0

    console = Console()
    console.print("[dim]正在阅读 Desktop / Documents / Downloads……[/dim]")

    def _on_activity(name: str, args: dict) -> None:
        console.print(f"  [dim]{_activity_line(name, args)}[/dim]")

    events.publish("bootstrap", "stage_start", {})
    profile, results, explored = synthesizer.synthesize(cfg, deep=deep, on_activity=_on_activity)

    report.render(results, profile, used_llm=True, explored=explored)

    written: list[str] = []
    if dry_run:
        logger.info("bootstrap dry-run: skipped memory write")
    elif profile is not None:
        written = sink.write(profile, results, fallback_text="")
        report.render_written(written)

    events.publish("bootstrap", "stage_end", {"written": len(written)})
    return 0


def run_headless(cfg: Config, *, deep: bool = True, exclude: frozenset[str] = frozenset()) -> int:
    """Daemon entry (POST /bootstrap/run): run the agent, publish events, write
    memory. No console output — the app watches via the SSE event stream.

    ``exclude`` is the set of top-level home folder names the user un-checked on
    the onboarding permission screen; those folders are never scanned or read.

    Returns the number of memory files written.
    """
    logger.info("bootstrap headless start: deep=%s exclude=%s", deep, sorted(exclude))
    events.publish("bootstrap", "stage_start", {})
    written: list[str] = []
    profile = None
    try:
        profile, results, _explored = synthesizer.synthesize(cfg, deep=deep, exclude=exclude)
        if profile is not None:
            written = sink.write(profile, results, fallback_text="")
    finally:
        # Carry the profile in stage_end so the app (onboarding) can render it
        # straight off the SSE stream — HUD ignores the extra field.
        payload: dict = {"written": len(written)}
        if profile is not None:
            payload["profile"] = dataclasses.asdict(profile)
        events.publish("bootstrap", "stage_end", payload)
    logger.info("bootstrap headless done: wrote %d files", len(written))
    return len(written)

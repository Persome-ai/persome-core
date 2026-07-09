"""Rich terminal report — the investor-facing "this is you" moment.

Two layers, top to bottom:
1. The LLM narrative (headline + portrait + inferred interests) — the wow.
2. The evidence: each collector's aggregated signals, so the narrative is
   visibly grounded in real local data, not magic.

Failed/skipped collectors and the privacy note sit in a muted footer.
"""

from __future__ import annotations

import os
from typing import Any

from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from .collectors import CollectorResult
from .fs_tools import FsRecorder
from .synthesizer import Profile

# Per-category accent colors keep the evidence section scannable.
_CATEGORY_STYLE = {
    "system": "grey70",
    "identity": "bright_cyan",
    "toolchain": "green",
    "habits": "yellow",
    "projects": "bright_magenta",
    "apps": "blue",
    "documents": "cyan",
    "interests": "bright_red",
    "comms": "magenta",
}


def _value_to_text(value: Any) -> str:
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                name = str(item.get("name", "")).strip()
                count = item.get("count")
                detail = str(item.get("detail", "")).strip()
                piece = name
                if count:
                    piece += f" [dim]({count})[/dim]"
                if detail:
                    piece += f" [dim]· {detail}[/dim]"
                parts.append(piece)
            else:
                parts.append(str(item))
        return "  ".join(p for p in parts if p)
    return str(value)


def _profile_panel(profile: Profile) -> Panel:
    blocks: list[Any] = []
    if profile.headline:
        blocks.append(Text(profile.headline, style="bold bright_white"))
    if profile.vibe:
        blocks.append(Text.from_markup(f"[bright_magenta]✨ {profile.vibe}[/bright_magenta]"))
    if profile.headline or profile.vibe:
        blocks.append(Text(""))
    if profile.narrative:
        blocks.append(Text(profile.narrative, style="white"))
    if profile.topics:
        blocks.append(Text(""))
        tags = "   ".join(
            f"[bold bright_red]#{t['name']}[/bold bright_red]" for t in profile.topics
        )
        blocks.append(Text.from_markup(f"[dim]推断关注:[/dim] {tags}"))
    if profile.confidence_notes:
        blocks.append(Text(""))
        blocks.append(
            Text.from_markup(f"[dim italic]把握度:{profile.confidence_notes}[/dim italic]")
        )
    return Panel(
        Group(*blocks),
        title="[bold]🧭 这就是你 · Persome 冷启动画像[/bold]",
        border_style="bright_cyan",
        padding=(1, 2),
    )


def _evidence_table(result: CollectorResult) -> Table:
    style = _CATEGORY_STYLE.get(result.category, "white")
    table = Table(
        show_header=False,
        box=None,
        padding=(0, 1),
        title=f"[bold {style}]{result.title}[/bold {style}]",
        title_justify="left",
    )
    table.add_column("label", style="dim", no_wrap=True, justify="right")
    table.add_column("value", overflow="fold")
    for s in result.signals:
        suffix = f" [dim]({s.detail})[/dim]" if s.detail and not isinstance(s.value, list) else ""
        table.add_row(s.label, _value_to_text(s.value) + suffix)
    return table


def _exploration_panel(explored: FsRecorder) -> Panel:
    lines: list[str] = [f"[bright_magenta]浏览目录[/bright_magenta] {len(explored.listed_dirs)} 处"]
    if explored.read_files:
        names = "  ".join(f"[green]{os.path.basename(p)}[/green]" for p in explored.read_files)
        lines.append(
            f"[bright_magenta]读取文件[/bright_magenta] ({len(explored.read_files)}): {names}"
        )
    else:
        lines.append("[dim]未读取任何文件正文[/dim]")
    return Panel(
        Text.from_markup("\n".join(lines)),
        title="[bold]🔎 agent 文件系统探索[/bold]",
        border_style="bright_magenta",
        padding=(0, 1),
    )


def render(
    results: list[CollectorResult],
    profile: Profile | None,
    *,
    used_llm: bool,
    explored: FsRecorder | None = None,
) -> None:
    console = Console()
    console.print()
    console.print(Rule("[bold bright_cyan]Persome 本地 Context 检索[/bold bright_cyan]"))

    if profile:
        console.print(_profile_panel(profile))
    elif used_llm:
        console.print(
            Panel(
                Text("LLM 合成未成功(无 key / 网络 / 输出异常),已退回结构化报告。", style="yellow"),
                border_style="yellow",
            )
        )
    else:
        console.print(
            Panel(
                Text("结构化模式(--no-llm):仅展示本地榨取的原始信号,未做 LLM 画像。", style="dim"),
                border_style="grey50",
            )
        )

    # Evidence section only when there are non-file signals to show (legacy path).
    produced = [r for r in results if r.produced]
    if produced:
        console.print()
        console.print(Rule("[dim]证据 · 本地信号[/dim]", style="dim"))
        for r in produced:
            console.print(_evidence_table(r))
            console.print()

    if explored is not None and (explored.listed_dirs or explored.read_files):
        console.print(_exploration_panel(explored))

    read_n = len(explored.read_files) if explored else 0
    scope = "Desktop / Documents / Downloads"
    if read_n:
        foot = (
            f"[dim]隐私边界:只读取你授权的 {scope};本次 agent 读了 {read_n} 个文件的正文用于画像。"
            "密钥/.env/keychain 等敏感文件被硬拒绝,二进制/超大文件跳过。"
            "用 --shallow 可只看目录结构、不读正文。[/dim]"
        )
    else:
        foot = f"[dim]隐私边界:只读取你授权的 {scope};本次未读取任何文件正文。[/dim]"
    console.print(Panel(Text.from_markup(foot), border_style="grey35", padding=(0, 1)))


def render_written(written: list[str]) -> None:
    console = Console()
    if not written:
        return
    console.print(
        Panel(
            Text.from_markup("[green]已写入 memory:[/green] " + ", ".join(written)),
            border_style="green",
            padding=(0, 1),
        )
    )

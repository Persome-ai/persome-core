"""persome watch — live agent activity monitor (Rich TUI).

Connects to the daemon's SSE event stream at GET /events/stream and
renders a full-terminal display showing each agent's status and a
scrolling log of every tool call and LLM response.

Usage::

    persome watch [--url http://127.0.0.1:8742]
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any

import httpx
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

_DAEMON_URL = "http://127.0.0.1:8742"
_MAX_EVENTS = 300

_STAGE_COLOR: dict[str, str] = {
    "dream": "magenta",
    "classifier": "cyan",
    "pattern_detector": "green",
    "reducer": "yellow",
    "consolidator": "blue",
    "timeline": "bright_blue",
    "system": "red",
}
_ALL_STAGES = ["dream", "classifier", "pattern_detector", "reducer", "consolidator"]


def _color(stage: str) -> str:
    return _STAGE_COLOR.get(stage, "white")


def _parse_sse(line: str) -> dict[str, Any] | None:
    if line.startswith("data: "):
        try:
            data = json.loads(line[6:])
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _fmt_args(args: Any, width: int = 60) -> str:
    if not isinstance(args, dict):
        return str(args)[:width]
    parts = []
    for k, v in list(args.items())[:4]:
        sv = str(v).replace("\n", "↵")
        if len(sv) > 35:
            sv = sv[:32] + "…"
        parts.append(f"[dim]{k}[/]={sv!r}")
    joined = "  ".join(parts)
    return joined[:width]


def _render_agents(
    states: dict[str, str],
    last_result: dict[str, str],
    last_ts: dict[str, str],
) -> Panel:
    t = Table.grid(padding=(0, 1))
    t.add_column(width=2)
    t.add_column(width=17)
    t.add_column()

    for stage in _ALL_STAGES:
        c = _color(stage)
        state = states.get(stage, "idle")
        bullet = f"[{c} bold]●[/]" if state == "running" else "[dim]○[/]"
        state_txt = f"[{c}]{state}[/]" if state == "running" else "[dim]idle[/]"
        t.add_row(bullet, f"[bold]{stage}[/]", state_txt)
        ts = last_ts.get(stage, "")
        info = last_result.get(stage, "")
        if ts or info:
            t.add_row("", f"[dim]{ts}[/]", f"[dim italic]{info[:28]}[/]")

    return Panel(t, title="[bold]Agents[/]", border_style="dim", padding=(0, 1))


def _render_activity(events: deque[dict[str, Any]]) -> Panel:
    t = Table.grid(padding=(0, 1))
    t.add_column(width=8, style="dim", no_wrap=True)  # HH:MM:SS
    t.add_column(width=13, no_wrap=True)  # stage
    t.add_column(width=16, no_wrap=True)  # event type / tool name
    t.add_column()  # detail

    rows = list(events)[-50:]
    for ev in rows:
        c = _color(ev.get("stage", ""))
        ts = ev.get("_ts", "")[-8:]
        stage = ev.get("stage", "?")
        etype = ev.get("type", "?")

        if etype == "tool_call":
            name = ev.get("name", "?")
            args = ev.get("arguments", {})
            detail = _fmt_args(args)
            t.add_row(ts, f"[{c}]{stage}[/]", f"[yellow]{name}[/]", detail)

        elif etype == "llm_text":
            text = (ev.get("text") or "").replace("\n", " ")[:80]
            t.add_row(ts, f"[{c}]{stage}[/]", "[dim]·thinking[/]", f"[dim italic]{text}[/]")

        elif etype == "stage_start":
            sid = ev.get("session_id", "")
            label = f" [{sid[:12]}]" if sid else ""
            t.add_row(ts, f"[{c} bold]{stage}[/]", "[green bold]▶ start[/]", f"[dim]{label}[/]")

        elif etype == "stage_end":
            summary = (ev.get("summary") or "")[:55]
            written = ev.get("written", 0)
            iters = ev.get("iterations")
            detail_parts = []
            if written:
                detail_parts.append(f"[green]{written} written[/]")
            if iters is not None:
                detail_parts.append(f"[dim]{iters} iter[/]")
            if summary:
                detail_parts.append(f"[dim italic]{summary}[/]")
            t.add_row(
                ts,
                f"[{c}]{stage}[/]",
                "[dim]■ done[/]",
                "  ".join(detail_parts),
            )

        else:
            # unknown event type — show raw
            detail = json.dumps({k: v for k, v in ev.items() if k not in ("stage", "type", "_ts")})[
                :80
            ]
            t.add_row(ts, f"[{c}]{stage}[/]", f"[dim]{etype}[/]", f"[dim]{detail}[/]")

    return Panel(
        t,
        title="[bold]Live Activity[/]",
        border_style="dim",
        padding=(0, 1),
    )


def _render_footer(url: str, n_events: int, connected: bool) -> Text:
    dot = "[green]●[/]" if connected else "[red]○[/]"
    return Text.from_markup(f"  {dot} {url}    events: {n_events}    [dim]Ctrl+C to quit[/]")


def watch(url: str = _DAEMON_URL) -> None:
    console = Console()
    events: deque[dict[str, Any]] = deque(maxlen=_MAX_EVENTS)
    states: dict[str, str] = {}
    last_result: dict[str, str] = {}
    last_ts: dict[str, str] = {}
    connected = False
    stop = threading.Event()

    def _sse_reader() -> None:
        nonlocal connected
        while not stop.is_set():
            try:
                with (
                    httpx.Client(timeout=httpx.Timeout(None, connect=5.0)) as client,
                    client.stream("GET", f"{url}/events/stream") as resp,
                ):
                    connected = True
                    for line in resp.iter_lines():
                        if stop.is_set():
                            return
                        ev = _parse_sse(line)
                        if ev is None:
                            continue
                        ev["_ts"] = datetime.now().strftime("%H:%M:%S")
                        events.append(ev)
                        stage = ev.get("stage", "")
                        etype = ev.get("type", "")
                        if stage:
                            if etype == "stage_start":
                                states[stage] = "running"
                            elif etype == "stage_end":
                                states[stage] = "idle"
                                last_result[stage] = ev.get("summary", "")[:28]
                                last_ts[stage] = ev.get("_ts", "")
            except httpx.ConnectError:
                connected = False
                events.append(
                    {
                        "stage": "system",
                        "type": "error",
                        "_ts": datetime.now().strftime("%H:%M:%S"),
                        "name": "connect error",
                        "arguments": {"msg": f"Cannot reach {url}  — retrying in 5s"},
                    }
                )
                time.sleep(5)
            except Exception as exc:
                connected = False
                events.append(
                    {
                        "stage": "system",
                        "type": "error",
                        "_ts": datetime.now().strftime("%H:%M:%S"),
                        "name": type(exc).__name__,
                        "arguments": {"msg": str(exc)},
                    }
                )
                time.sleep(5)

    reader = threading.Thread(target=_sse_reader, daemon=True)
    reader.start()

    layout = Layout()
    layout.split_column(
        Layout(name="header", size=1),
        Layout(name="main"),
        Layout(name="footer", size=1),
    )
    layout["main"].split_row(
        Layout(name="agents", ratio=1),
        Layout(name="activity", ratio=3),
    )

    try:
        with Live(layout, console=console, refresh_per_second=4, screen=True):
            while not stop.is_set():
                layout["header"].update(
                    Text.from_markup("[bold green] Persome Watch[/]   pipeline monitor")
                )
                layout["agents"].update(_render_agents(states, last_result, last_ts))
                layout["activity"].update(_render_activity(events))
                layout["footer"].update(_render_footer(url, len(events), connected))
                time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()

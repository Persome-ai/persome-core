#!/usr/bin/env python3
"""Terminal live-monitor of the Persome daemon's intent event stream.

A dependency-free (stdlib only) replacement for the debug HUD's AGENT ACTIVITY
panel: it subscribes to the daemon's ``/events/stream`` SSE endpoint and pretty-
prints, in real time, exactly what the HUD shows —

  ⚡ event_detected   — the event-detection layer firing BEFORE the LLM, with
                        the K-class + decision (recognize / no_anchor / throttled)
  ◆ intent_recognized — a recognized intent with confidence + end-to-end latency
                        + the row id (so you can accept/reject it from the CLI)

Because it's a plain terminal client it sidesteps the HUD's second-FlutterEngine
freeze entirely. Reconnects automatically when the daemon restarts.

Usage:
    python3 watch_intents.py                 # intent channel only (default)
    python3 watch_intents.py --all           # every frame (stages, tools, ...)
    python3 watch_intents.py --port 8742     # custom daemon port
    python3 watch_intents.py --no-color      # plain text

Accept / reject a recognized intent (separate one-shot calls, by the id shown):
    python3 watch_intents.py --accept 80
    python3 watch_intents.py --reject 80
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
import urllib.request
from datetime import datetime

# ── ANSI ────────────────────────────────────────────────────────────────────
_C = {
    "reset": "\033[0m",
    "dim": "\033[2m",
    "bold": "\033[1m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "gray": "\033[90m",
}
_USE_COLOR = True


def c(text: str, *names: str) -> str:
    if not _USE_COLOR:
        return text
    return "".join(_C[n] for n in names) + text + _C["reset"]


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _ts() -> str:
    """Dim HH:MM:SS prefix shared by every renderer line."""
    return c(_now(), "gray")


def _fmt_latency(ms) -> str:
    if not isinstance(ms, (int, float)):
        return ""
    ms = int(ms)
    return f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms}ms"


# ── frame renderers ───────────────────────────────────────────────────────────
def render_event_detected(f: dict) -> str:
    scenario = f.get("scenario") or "?"
    label = f.get("scenario_label") or ""
    app = f.get("app") or ""
    preview = f.get("preview") or ""
    decision = f.get("decision") or "recognize"
    head = f"⚡ {scenario}·{label}" if label else f"⚡ {scenario} 事件"
    suffix = {
        "recognize": c(" → 识别中…", "cyan"),
        "no_anchor": c(" · 无排期锚点，跳过 LLM", "gray"),
        "throttled": c(" · 已节流", "yellow"),
    }.get(decision, "")
    body = f"[{app}] {preview}" if app else preview
    return f"{_ts()} {c(head, 'magenta')}  {c(body, 'dim')}{suffix}"


def render_intent_recognized(f: dict) -> list[str]:
    intents = f.get("intents") or []
    if not intents:
        return [f"{_ts()} {c('◇ 暂无识别意图', 'gray')}"]
    top_latency = f.get("latency_ms")
    lines = []
    for it in intents:
        if not isinstance(it, dict):
            continue
        kind = it.get("kind") or "intent"
        conf = it.get("confidence")
        conf_s = f"  {round(conf * 100)}%" if isinstance(conf, (int, float)) else ""
        lat = _fmt_latency(it.get("latency_ms", top_latency))
        lat_s = c(f"  · {lat}", "green") if lat else ""
        iid = it.get("id")
        id_s = c(f"  [id {iid}]", "blue") if iid else ""
        scope = it.get("scope") or f.get("scope") or ""
        scope_s = c(f"  ({scope})", "gray") if scope else ""
        head = f"{c('◆', 'cyan')} {c(kind, 'bold', 'cyan')}{conf_s}{lat_s}{id_s}{scope_s}"
        lines.append(f"{_ts()} {head}")
        rationale = it.get("rationale") or ""
        if rationale:
            lines.append(f"           {c(rationale, 'dim')}")
        payload = it.get("payload") or {}
        bits = [
            f"{k}={v}"
            for k in ("when_text", "with", "channel", "provenance")
            if (v := payload.get(k))
        ]
        if bits:
            lines.append(f"           {c('  '.join(bits), 'gray')}")
    return lines


def render_generic(f: dict) -> str | None:
    t = f.get("type")
    stage = f.get("stage") or ""
    if t in ("stage_start", "stage_end"):
        glyph = "▸" if t == "stage_start" else "▪"
        extra = f" wrote {f['written']}" if f.get("written") is not None else ""
        return f"{_ts()} {c(glyph + ' ' + stage + extra, 'gray')}"
    if t == "tool_call":
        return f"{_ts()} {c('⚙ ' + str(f.get('name') or 'tool'), 'gray')}"
    return None


# ── stream loop ───────────────────────────────────────────────────────────────
def stream(base: str, show_all: bool) -> None:
    url = f"{base}/events/stream"
    print(c(f"▶ watching {url}  (Ctrl-C to quit)", "bold"))
    print(c("  ⚡ = event detected (pre-LLM)   ◆ = intent recognized", "gray"))
    print()
    # loopback daemon — bypass any system proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while True:
        try:
            req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
            with opener.open(req, timeout=None) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", "replace").rstrip("\n")
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        f = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    t = f.get("type")
                    if t == "event_detected":
                        print(render_event_detected(f))
                    elif t == "intent_recognized":
                        # Skip the empty "tick fired, nothing new" signal by
                        # default — the ~2min slow-path ticks would otherwise
                        # spam the feed with "暂无识别意图". --all shows them.
                        if not (f.get("intents") or []) and not show_all:
                            continue
                        for ln in render_intent_recognized(f):
                            print(ln)
                    elif show_all:
                        g = render_generic(f)
                        if g:
                            print(g)
                    sys.stdout.flush()
        except KeyboardInterrupt:
            print(c("\n■ stopped", "gray"))
            return
        except Exception as exc:  # noqa: BLE001 — keep the watch alive across daemon restarts
            print(c(f"{_now()} … reconnecting ({exc})", "gray"))
            time.sleep(2)


def set_status(base: str, intent_id: int, status: str) -> None:
    url = f"{base}/intents/{intent_id}"
    data = json.dumps({"status": status}).encode()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        url, data=data, method="PATCH", headers={"Content-Type": "application/json"}
    )
    try:
        with opener.open(req, timeout=10) as resp:
            print(c(f"intent {intent_id} → {status}: {resp.status}", "green"))
    except Exception as exc:  # noqa: BLE001
        print(c(f"failed: {exc}", "red"))


def main() -> None:
    global _USE_COLOR
    ap = argparse.ArgumentParser(description="Terminal live-monitor of the daemon intent stream")
    ap.add_argument("--port", type=int, default=8742)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument(
        "--all", action="store_true", help="show every frame, not just the intent channel"
    )
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--accept", type=int, metavar="ID", help="mark intent ID consumed and exit")
    ap.add_argument("--reject", type=int, metavar="ID", help="mark intent ID dismissed and exit")
    args = ap.parse_args()
    # Line-buffer stdout so frames show immediately even when piped to a file
    # (Python fully buffers a non-TTY stdout by default → output would be lost).
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(line_buffering=True)
    _USE_COLOR = (not args.no_color) and sys.stdout.isatty()
    base = f"http://{args.host}:{args.port}"
    if args.accept is not None:
        set_status(base, args.accept, "consumed")
        return
    if args.reject is not None:
        set_status(base, args.reject, "dismissed")
        return
    stream(base, show_all=args.all)  # handles Ctrl-C internally


if __name__ == "__main__":
    main()

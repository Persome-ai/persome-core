#!/usr/bin/env python3
"""End-to-end recognition-latency probe (inject → recognize → native notification).

Injects a synthetic WeChat OCR arrival through the REAL fast path (no real
colleague needed), then measures the full chain to the moment the app's
ContextSentinel decides to surface a native notification — by correlating the
daemon's inject response with the app's decision log.

    daemon:  POST /intents/debug/inject   → {injected_at, intent_ids, daemon_latency_ms}
    app:     ~/.persome/logs/context-sentinel.jsonl  row {t, gate, picked=intent_id}

Latency breakdown printed:
    • daemon_ms   = inject → intent persisted (lean LLM + sink), from the response
    • app_ms      = SSE delivery + sentinel gates (+ proposal writer) = e2e − daemon
    • e2e_ms      = injected_at → the notify decision row's `t`  ← what you feel

The terminal `gate` tells you the outcome: ``enqueued-notify`` / ``followup-routed``
= a notification fired; ``semantic-dup`` / ``disabled`` = suppressed (no popup).

Usage:
    uv run python scripts/debug_latency_probe.py
    uv run python scripts/debug_latency_probe.py --text "明天下午3点和张总过一下预算" --timeout 40
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from datetime import datetime
from pathlib import Path

DAEMON = "http://127.0.0.1:8773"
SENTINEL_LOG = Path.home() / ".persome" / "logs" / "context-sentinel.jsonl"
# Sentinel gates that mean a banner was (or is about to be) delivered.
NOTIFY_GATES = {"enqueued-notify", "followup-routed", "autoQueued"}
SUPPRESS_GATES = {"semantic-dup", "disabled", "disabled-mid-build", "live-task-veto",
                  "dismiss-cooldown", "content-dup", "seen", "fetch-failed"}


def _post(url: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 — localhost only
        return json.loads(r.read())


def _parse_iso(s: str) -> float | None:
    try:
        dt = datetime.fromisoformat(s.strip())
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt.timestamp()
    except Exception:
        return None


def _tail_for(intent_ids: set[int], since_pos: int, deadline: float) -> tuple[dict, int] | None:
    """Poll the sentinel jsonl from byte ``since_pos`` for a row whose ``picked`` is one
    of ``intent_ids``. Returns (row, new_pos) or None on timeout."""
    while time.monotonic() < deadline:
        if SENTINEL_LOG.exists():
            with SENTINEL_LOG.open("rb") as f:
                f.seek(since_pos)
                chunk = f.read()
                since_pos = f.tell()
            for line in chunk.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("picked") in intent_ids:
                    return row, since_pos
        time.sleep(0.1)
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default="明天上午10点聊一下 SOTA 模型训练的事情")
    ap.add_argument("--daemon", default=DAEMON)
    ap.add_argument("--timeout", type=float, default=45.0, help="seconds to wait for the notify decision")
    args = ap.parse_args()

    # Snapshot the sentinel log size BEFORE injecting so we only read rows that follow.
    start_pos = SENTINEL_LOG.stat().st_size if SENTINEL_LOG.exists() else 0

    print(f"→ injecting mock WeChat arrival: {args.text!r}")
    resp = _post(f"{args.daemon}/intents/debug/inject", {"text": args.text})
    d = resp.get("data", resp)
    injected_at = _parse_iso(d["injected_at"])
    intent_ids = set(d.get("intent_ids") or [])
    daemon_ms = d.get("daemon_latency_ms")
    print(f"  daemon: recognized={d.get('recognized')} ids={sorted(intent_ids)} "
          f"daemon_latency={daemon_ms}ms")

    if not intent_ids:
        print("✗ daemon recognized 0 intents — nothing will surface. "
              "(real LLM needed; check the message has a schedulable anchor.)")
        return

    print(f"→ waiting ≤{args.timeout:.0f}s for the app's notify decision "
          f"({SENTINEL_LOG}) …")
    hit = _tail_for(intent_ids, start_pos, time.monotonic() + args.timeout)
    if hit is None:
        print("✗ no sentinel decision for these intents within the timeout. "
              "Is the Persome app running with context enabled + signed in?")
        return
    row, _ = hit
    gate = row.get("gate", "?")
    notify_t = _parse_iso(row.get("t", ""))
    e2e_ms = int((notify_t - injected_at) * 1000) if (notify_t and injected_at) else None
    app_ms = (e2e_ms - daemon_ms) if (e2e_ms is not None and daemon_ms is not None) else None

    print("\n──────── latency breakdown ────────")
    print(f"  daemon (inject→persist, lean LLM+sink) : {daemon_ms} ms")
    print(f"  app    (SSE + sentinel gates + writer)  : {app_ms} ms")
    print(f"  END-TO-END (inject → notify decision)   : {e2e_ms} ms   ({(e2e_ms or 0)/1000:.1f}s)")
    print(f"  terminal gate                           : {gate}")
    if gate in NOTIFY_GATES:
        print("  ✅ a native notification fired (intent surfaced).")
    elif gate in SUPPRESS_GATES:
        print(f"  ⚠ suppressed at '{gate}' — no banner. (expected when a prior is similar.)")
    else:
        print(f"  ℹ ended at '{gate}'.")


if __name__ == "__main__":
    main()

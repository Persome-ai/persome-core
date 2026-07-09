#!/usr/bin/env python3
"""Opt-in on-device harness: send a REAL message through the actuation layer (P2 end-to-end).

This sends a real message, so it is **never** auto-collected (it lives under tests/manual/, no
`test_` prefix) and **refuses to run** without `PERSOME_ACTUATION_E2E=1`. It is the proof-of-concept
for the "send this to xxx" chain — validated live against **Feishu/Lark → 温子墨**.

SAFETY (load-bearing):
  • The target chat MUST already be open. The harness HARD-ASSERTS the open conversation's header
    equals the expected target BEFORE it types or sends — if it can't confirm, it ABORTS (never
    sends to the wrong person).
  • The message is sent verbatim; pass a clearly-marked test string.

Findings baked in:
  • Feishu/Lark (com.electron.lark): AX-rich AFTER the AXManualAccessibility force-enable — the full
    chain works (clickxy focus → CGEvent type Chinese → key Enter → verify via AX diff).
  • WeChat (com.tencent.xinWeChat): AX-POOR — only the menu bar is exposed (the main window is
    self-drawn). Pure-AX actuation can't reach its compose box; this needs the P3 OCR/vision tier.
    The harness detects this and reports it instead of guessing.

Usage:
  swiftc -O -framework Cocoa -framework ApplicationServices resources/mac-ax-actuator.swift -o /tmp/mac-ax-actuator
  PERSOME_ACTUATION_E2E=1 python3 tests/manual/actuation_chat_harness.py \
      --app Lark --target 温子墨 --message "【test】please ignore" [--send]
  (omit --send to type-and-verify but STOP before the irreversible Enter)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

ACT = os.environ.get("PERSOME_AX_ACTUATOR", "/tmp/mac-ax-actuator")


def run(*a: str) -> dict:
    return json.loads(
        subprocess.run([ACT, *a], capture_output=True).stdout.decode("utf-8", "replace")
    )


def snapshot(app: str) -> dict:
    return run("snapshot", "--app", app)


def header_title(snap: dict) -> str:
    """The open conversation's title = the top-left header AXStaticText (y<90, x in the message pane)."""
    best = ""
    for e in snap.get("elements", []):
        bb = e.get("bbox")
        if e["role"] == "AXStaticText" and bb and bb[1] < 90 and bb[0] > 700:
            t = (e.get("value") or e.get("label") or "").strip()
            if t and not best:
                best = t
    return best


def is_ax_poor(snap: dict) -> bool:
    els = snap.get("elements", [])
    return bool(els) and all(e["role"].startswith("AXMenu") for e in els)


def main() -> int:
    if os.environ.get("PERSOME_ACTUATION_E2E") != "1":
        print("refusing to run: set PERSOME_ACTUATION_E2E=1 (this sends a REAL message)")
        return 2

    ap = argparse.ArgumentParser()
    ap.add_argument("--app", required=True)
    ap.add_argument(
        "--target", required=True, help="expected open-conversation title (safety assert)"
    )
    ap.add_argument("--message", required=True)
    ap.add_argument(
        "--send", action="store_true", help="press Enter to actually send (else stop before)"
    )
    args = ap.parse_args()

    if not run("trust").get("trusted"):
        print("not Accessibility-trusted — grant it first")
        return 2

    subprocess.run(["osascript", "-e", f'tell application "{args.app}" to activate'], check=False)
    time.sleep(1.0)
    snap = snapshot(args.app)

    if is_ax_poor(snap):
        print(
            f"{args.app} is AX-POOR (only menu bar exposed) — pure-AX actuation can't reach it. "
            "This is the P3 OCR/vision case (e.g. WeChat). Aborting."
        )
        return 1

    title = header_title(snap)
    print(f"open conversation header: {title!r}  (expected target: {args.target!r})")
    if args.target not in title:
        print("SAFETY ABORT: open chat does not match the target — open the target chat first.")
        return 1

    # focus the compose box (bottom AXTextArea), type the message
    inp = next((e for e in snap["elements"] if e["role"] == "AXTextArea" and e.get("bbox")), None)
    if not inp:
        print("no input AXTextArea found")
        return 1
    bx, by, bw, bh = inp["bbox"]
    run(
        "act",
        "--app",
        args.app,
        "--verb",
        "clickxy",
        "--x",
        str(int(bx + bw / 2)),
        "--y",
        str(int(by + bh / 2)),
    )
    time.sleep(0.4)
    run("act", "--app", args.app, "--verb", "type", "--text", args.message)
    time.sleep(0.6)

    snap2 = snapshot(args.app)
    landed = any(
        args.message[:8] in (e.get("value") or "")
        for e in snap2["elements"]
        if e["role"] == "AXTextArea"
    )
    print(f"message typed into the compose box: {landed}")
    if not landed:
        print("text did not land in the input — aborting before send.")
        return 1

    if not args.send:
        print("STOP: message typed but NOT sent (pass --send to press Enter).")
        return 0

    res = run("act", "--app", args.app, "--verb", "key", "--keys", "enter")
    time.sleep(1.0)
    snap3 = snapshot(args.app)
    sent = any(
        args.message[:12] in (e.get("value") or "") and e["role"] != "AXTextArea"
        for e in snap3["elements"]
    )
    print(f"sent (message appears as a bubble): {sent}  (key ok={res.get('ok')})")
    return 0 if sent else 1


if __name__ == "__main__":
    sys.exit(main())

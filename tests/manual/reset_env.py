#!/usr/bin/env python3
"""Reset the apps to a clean, reproducible state BETWEEN benchmark runs (not timed).

Why this exists: TencentMeeting personal accounts allow only ONE meeting per time slot, and every
bench run creates a meeting at the same default time — so without cleanup the 2nd run hits a "会议冲突
提示" dialog and the flow diverges. This makes each run reproducible by:
  1. restarting TencentMeeting to a clean home screen, then cancelling every leftover test meeting
     ("与温子墨的会议") via its detail → ⋯ → 取消会议 → confirm (until the list shows 暂无会议);
  2. clearing the Lark draft in the test conversation so a previously-pasted link doesn't linger.

OCR (on-device PP-OCRv6) is used for the pixel-drawn confirm button; the reset is OFF the measured
path, so its cold start doesn't count against the benchmark's ≤20s budget.

  uv run python3 tests/manual/reset_env.py [meeting-title] [person]
"""

from __future__ import annotations

import subprocess
import sys
import time

sys.path.insert(0, "src")
from persome.capture import ocr_local  # noqa: E402

ACT = "/tmp/mac-ax-actuator"
TM = "com.tencent.meeting"
LARK = "com.electron.lark"
TITLE = sys.argv[1] if len(sys.argv) > 1 else "与温子墨的会议"
PERSON = sys.argv[2] if len(sys.argv) > 2 else "温子墨"


def snap(app: str, depth: str = "60") -> list[dict]:
    import json

    out = subprocess.run([ACT, "snapshot", "--app", app, "--depth", depth], capture_output=True)
    try:
        return json.loads(out.stdout.decode("utf-8", "replace")).get("elements", [])
    except Exception:  # noqa: BLE001
        return []


def win(app: str) -> dict | None:
    return next(
        (e for e in snap(app) if e["role"] == "AXWindow" and e.get("bbox") and e["bbox"][2] > 0),
        None,
    )


def clickxy(app: str, x: float, y: float) -> None:
    subprocess.run(
        [
            ACT,
            "act",
            "--app",
            app,
            "--verb",
            "clickxy",
            "--x",
            str(x),
            "--y",
            str(y),
            "--no-cursor",
        ],
        capture_output=True,
    )


def ocr_click(app: str, query: str, pick_rightmost: bool = False) -> bool:
    """Screenshot the front window, OCR it, click the match (rightmost if asked — for confirm buttons)."""
    from pathlib import Path

    w = win(app)
    if not w:
        return False
    rect = w["bbox"]
    x, y, ww, hh = (int(v) for v in rect)
    subprocess.run(
        ["screencapture", "-x", f"-R{x},{y},{ww},{hh}", "/tmp/_reset.png"], capture_output=True
    )
    res = ocr_local.recognize_detailed(Path("/tmp/_reset.png").read_bytes())
    if not res:
        return False
    hits = [b for t, b in zip(res[0], res[1], strict=False) if query in t]
    if not hits:
        return False
    b = max(hits, key=lambda b: b[0]) if pick_rightmost else hits[0]
    clickxy(app, rect[0] + (b[0] + b[2]) / 4, rect[1] + (b[1] + b[3]) / 4)
    return True


def ocr_has(app: str, query: str) -> bool:
    """OCR-based text presence — the home meeting list is PIXEL-drawn (not in AX), so AX text checks
    miss it; this screenshots the front window and looks for the text."""
    from pathlib import Path

    w = win(app)
    if not w:
        return False
    x, y, ww, hh = (int(v) for v in w["bbox"])
    subprocess.run(["screencapture", "-x", f"-R{x},{y},{ww},{hh}", "/tmp/_reset.png"], capture_output=True)
    res = ocr_local.recognize_detailed(Path("/tmp/_reset.png").read_bytes())
    return bool(res) and any(query in t for t in res[0])


def cancel_one_meeting() -> bool:
    """Open the leftmost/home meeting list, cancel one `TITLE` meeting. Returns whether it cancelled."""
    if not ocr_click(TM, TITLE):  # the meeting row in the home list
        return False
    time.sleep(1.5)
    # detail screen → ⋯ menu (top-right) → 取消会议 (an AXMenuItem) → confirm (rightmost 取消会议)
    w = win(TM)
    if w:
        b = w["bbox"]
        clickxy(TM, b[0] + b[2] - 24, b[1] + 22)  # the ⋯ button, top-right
        time.sleep(0.8)
    mi = next(
        (
            e
            for e in snap(TM)
            if e["role"] == "AXMenuItem"
            and (e.get("label") or "").strip() == "取消会议"
            and e.get("bbox")
            and e["bbox"][2] > 0
        ),
        None,
    )
    if mi:
        bb = mi["bbox"]
        clickxy(TM, bb[0] + bb[2] / 2, bb[1] + bb[3] / 2)
        time.sleep(1.0)
    ocr_click(TM, "取消会议", pick_rightmost=True)  # the blue confirm button
    time.sleep(1.5)
    return True


def reset_tencent_meeting() -> None:
    subprocess.run(["osascript", "-e", 'quit app "TencentMeeting"'], capture_output=True)
    time.sleep(2)
    subprocess.run(["pkill", "-x", "TencentMeeting"], capture_output=True)
    time.sleep(1)
    subprocess.run(["open", "-a", "TencentMeeting"], capture_output=True)
    time.sleep(3)
    subprocess.run(
        ["osascript", "-e", 'tell application "TencentMeeting" to activate'], capture_output=True
    )
    # WAIT for the home meeting list to render before cancelling — the failure mode was OCR-clicking
    # before the list loaded, so the meeting was "not found" yet still there.
    for _ in range(20):
        time.sleep(0.5)
        if ocr_has(TM, "暂无会议") or ocr_has(TM, TITLE):
            break
    for _ in range(6):  # cancel every leftover test meeting until OCR no longer sees one
        if ocr_has(TM, "暂无会议") or not cancel_one_meeting():
            break
        time.sleep(1.0)  # let the home re-render between cancels
    print("  TencentMeeting:", "暂无会议" if ocr_has(TM, "暂无会议") else "(meetings may remain)")


def reset_lark_draft() -> None:
    subprocess.run(
        ["osascript", "-e", f'tell application id "{LARK}" to activate'], capture_output=True
    )
    time.sleep(1.2)
    els = snap(LARK)
    # if the test person's chat is open and the input holds a link, clear it
    inp = next(
        (e for e in els if e["role"] == "AXTextArea" and e.get("bbox") and e["bbox"][2] > 100), None
    )
    if inp and "meeting.tencent.com" in (inp.get("value") or ""):
        subprocess.run(
            [
                ACT,
                "act",
                "--app",
                LARK,
                "--id",
                inp["id"],
                "--verb",
                "setvalue",
                "--text",
                "",
                "--no-cursor",
            ],
            capture_output=True,
        )
        print("  Lark: cleared the leftover link draft")
    else:
        print("  Lark: no leftover link draft")


def main() -> None:
    print("resetting environment (off the timed path)…")
    ocr_local.warm()
    reset_tencent_meeting()
    reset_lark_draft()
    print("reset done.")


if __name__ == "__main__":
    main()

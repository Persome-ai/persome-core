#!/usr/bin/env python3
"""Benchmark: how fast + reliably does the Persome actuation layer open Gmail (Tabbit), per LLM model.

A model-parametrized computer-use benchmark over the actuation layer. It gives the model ONE fixed
task — "navigate Tabbit to the Gmail inbox" — drives the snapshot→click/type/key function-calling
loop against the real browser, and records, apples-to-apples across models:

  • per-step LLM latency (the dominant cost) + its mean/min/max,
  • in-actuator time (snapshot + perform, from the actuator's own `timing` block — typically ~ms),
  • step count to completion, and
  • whether Gmail was ACTUALLY reached (address bar resolves to mail.google.com), not just "model said done".

So you can answer "which model, with which reasoning setting, drives computer-use fastest *and* actually
finishes" on identical conditions.

Findings it encodes (measured 2026-06-25, open-Tabbit→Gmail):
  • The per-call cost is the model's reasoning latency, NOT the actuation (clicks/keys ≈ 3 ms total).
  • Direct provider endpoint + reasoning OFF/low ≈ 1.3–1.4 s/call; OpenRouter routing + default
    reasoning ≈ 5 s/call (≈3.5× slower) — the "too slow" was the route + thinking, not the model size.
  • TRUE nothink is provider-specific: DeepSeek-v4-flash honors `thinking:{type:disabled}` (reasoning
    drops to 0); StepFun step models IGNORE it and only expose `reasoning_effort` low/medium/high
    (step-3.7 benefits from `low`, step-3.5 barely moves).

It also bakes in the bugs this flow surfaced, so the benchmark is deterministic:
  • the target app is pinned by BUNDLE ID (a loose name like "Tabbit" can resolve to a Chromium
    renderer HELPER pid whose AX tree is empty — see mac-ax-actuator `pidForApp`),
  • the snapshot filter always keeps an `AXTextField` even with an empty value (the address bar on a
    blank/new-tab page has no value and would otherwise be dropped, leaving the model blind), and
  • the browser is reset OFF Gmail before each run (and verified) so we measure a real navigation
    chain, not a 2-step short-circuit.

Run (needs Tabbit installed + a provider key; sends real keystrokes/clicks — navigation only):
  swiftc -O -framework Cocoa -framework ApplicationServices resources/mac-ax-actuator.swift -o /tmp/mac-ax-actuator
  PERSOME_ACTUATION_E2E=1 DEEPSEEK_API_KEY=...  python3 tests/manual/bench_open_gmail.py
  PERSOME_ACTUATION_E2E=1 STEPFUN_API_KEY=... BENCH_PRESET=step-3.7-flash-low python3 tests/manual/bench_open_gmail.py
  PERSOME_ACTUATION_E2E=1 BENCH_PRESET=all    python3 tests/manual/bench_open_gmail.py   # compare every preset whose key is set

Visualization (default ON): a persistent floating Persome cursor (the `cursor-hud` overlay) follows each
action with the model's short Chinese note in a bubble, AND every actionable element from the latest
snapshot is outlined with a role-colored bbox (a Set-of-Marks view) — so you SEE which app Persome is
driving and what it can act on. Both are fed to the one long-lived overlay process; set BENCH_CURSOR=0
/ BENCH_BOXES=0 to drop either.

Env:
  BENCH_PRESET   one preset key below, or "all" (default: deepseek-v4-flash)
  BENCH_CURSOR   "0" to suppress the floating Persome cursor + bubble (default: on, so you watch it operate)
  BENCH_BOXES    "0" to suppress the per-element bbox overlay (default: on; draws every CLICKABLE node)
  BENCH_BOXES_ALL "1" to outline EVERY actionable node incl. containers/text (default: clickable only)
  BENCH_TRACE    path to write a flamegraph trace JSON consumable by actuation_flamegraph.py
  BENCH_STEPCAP  max model turns before giving up (default 16)
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
import urllib.request

if os.environ.get("PERSOME_ACTUATION_E2E") != "1":
    print("refusing to run: set PERSOME_ACTUATION_E2E=1 (drives a real browser)")
    sys.exit(2)

ACT = os.environ.get("PERSOME_AX_ACTUATOR", "/tmp/mac-ax-actuator")
APP = "com.tab-browser.Tabbit"  # pin by bundle id, NOT the name "Tabbit" (→ a helper pid, empty AX)
TASK = (
    "把 Tabbit 浏览器导航到 Gmail 收件箱 https://mail.google.com。固定流程：①先 snapshot；②在元素里找"
    "地址栏（role=AXTextField）并 click 它；③type https://mail.google.com；④key enter；⑤再 snapshot "
    "确认。不要用 cmd+l。只导航/查看，绝不发送/删除。snapshot 出现 mail.google / 收件箱 / 写邮件 即 done。"
)
SYS = (
    "你是 macOS computer-use 助手。第一步必须 snapshot；优先用 snapshot 列表里的元素 index 做 click，"
    "不要乱按快捷键，禁止 cmd+l。每个动作配简短中文 note。只导航/查看。"
)

# preset → provider endpoint + key env + model + the reasoning knob that actually works there.
PRESETS: dict[str, dict] = {
    "deepseek-v4-flash": {
        "label": "deepseek-v4-flash · nothink",
        "base": "https://api.deepseek.com/chat/completions",
        "key_env": "DEEPSEEK_API_KEY",
        "model": "deepseek-v4-flash",
        "extra": {"thinking": {"type": "disabled"}},  # DeepSeek honors this → reasoning = 0
    },
    "step-3.7-flash-low": {
        "label": "step-3.7-flash · effort=low",
        "base": "https://api.stepfun.com/v1/chat/completions",
        "key_env": "STEPFUN_API_KEY",
        "model": "step-3.7-flash",
        "extra": {
            "reasoning_effort": "low"
        },  # StepFun: only low/medium/high; 3.7 benefits from low
    },
    "step-3.5-flash": {
        "label": "step-3.5-flash · default",
        "base": "https://api.stepfun.com/v1/chat/completions",
        "key_env": "STEPFUN_API_KEY",
        "model": "step-3.5-flash",
        "extra": {},  # no working nothink knob (thinking:disabled is ignored here)
    },
    "step-3.5-flash-2603-low": {
        "label": "step-3.5-flash-2603 · effort=low",
        "base": "https://api.stepfun.com/v1/chat/completions",
        "key_env": "STEPFUN_API_KEY",
        "model": "step-3.5-flash-2603",
        # unlike the rolling step-3.5-flash, this dated snapshot DOES honor reasoning_effort=low
        # (probe: ~1.7s vs ~4.1s default); thinking:disabled is still ignored.
        "extra": {"reasoning_effort": "low"},
    },
}

CURSOR_ON = os.environ.get("BENCH_CURSOR", "1") != "0"
BOXES_ON = os.environ.get("BENCH_BOXES", "1") != "0"  # draw a bbox around every clickable element
BOXES_ALL = (
    os.environ.get("BENCH_BOXES_ALL", "0") == "1"
)  # draw EVERY actionable node (incl. containers)
STEP_CAP = int(os.environ.get("BENCH_STEPCAP", "16"))

# roles drawn in the visual overlay (clickable); pure containers/text are excluded as noise
_CLICKABLE_ROLES = frozenset(
    {
        "AXButton",
        "AXLink",
        "AXTextField",
        "AXTextArea",
        "AXCheckBox",
        "AXRadioButton",
        "AXPopUpButton",
        "AXComboBox",
        "AXMenuItem",
        "AXMenuBarItem",
        "AXTab",
        "AXSlider",
        "AXDisclosureTriangle",
    }
)
# roles offered to the LLM as a numbered, labeled list (must carry a usable text label)
_LLM_ROLES = frozenset(
    {
        "AXButton",
        "AXLink",
        "AXTextField",
        "AXTextArea",
        "AXMenuItem",
        "AXPopUpButton",
        "AXCheckBox",
        "AXRadioButton",
        "AXComboBox",
    }
)

_idmap: dict[int, str] = {}
_last_point: list[float] | None = None
_last_boxes: list[dict] = []  # [{bbox, role}] from the latest snapshot, kept on screen across steps
_hud: subprocess.Popen | None = None
_trace: list[dict] = []  # flamegraph spans: {step, kind, name, wall_ms, internal}


def run_act(*a: str) -> dict:
    r = subprocess.run([ACT, *a], capture_output=True)
    try:
        return json.loads(r.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return {"ok": False, "error": "bad_json", "elements": []}


def cursor(point: list[float] | None, note: str) -> None:
    global _last_point
    if not CURSOR_ON or _hud is None:
        return
    if point:
        _last_point = point
    p = point or _last_point
    msg: dict = {"note": note}
    if p:
        msg["x"], msg["y"] = p[0], p[1]
    if BOXES_ON and _last_boxes:
        msg["elements"] = (
            _last_boxes  # cursor-hud draws a role-colored box per element + the cursor
        )
    try:
        _hud.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")  # type: ignore[union-attr]
        _hud.stdin.flush()  # type: ignore[union-attr]
    except (OSError, ValueError):
        pass


def t_activate(note: str = "", **_: object) -> str:
    subprocess.run(
        ["osascript", "-e", f'tell application id "{APP}" to activate'], capture_output=True
    )
    time.sleep(1.0)
    cursor(None, note or "激活浏览器")
    return "activated"


def t_snapshot(note: str = "", **_: object) -> str:
    """List actionable elements. An AXTextField is ALWAYS kept (empty-value address bar must show)."""
    global _idmap
    snap = run_act("snapshot", "--app", APP)
    _trace.append(
        {
            "step": -1,
            "kind": "act",
            "name": "snapshot",
            "wall_ms": 0.0,
            "internal": snap.get("timing", {}),
        }
    )
    if not snap.get("ok"):
        return f"snapshot failed: {snap.get('error')}"
    global _last_boxes
    elements = snap.get("elements", [])

    # Visual boxes are DECOUPLED from the LLM list: outline EVERY truly-clickable element (has the
    # AXPress action, or a clickable role) with a bbox — uncapped — so the Set-of-Marks overlay shows
    # the real AX coverage (the AX tree exposes ~3000+ actionable nodes here, every one with a bbox;
    # the icon/toolbar buttons missing before were unlabeled ones the LLM filter dropped). Pure
    # containers/text (AXGroup/AXCell/AXRow/AXStaticText/AXImage/AXHeading) are excluded as noise —
    # set BENCH_BOXES_ALL=1 to draw all actionable nodes instead.
    _last_boxes = [
        {"bbox": e["bbox"], "role": e["role"]}
        for e in elements
        if e.get("bbox")
        and (BOXES_ALL or "AXPress" in (e.get("actions") or []) or e["role"] in _CLICKABLE_ROLES)
    ]

    # The LLM list stays the compact, labeled, capped Set-of-Marks (an AXTextField is always kept so
    # an empty-value address bar still shows).
    _idmap = {}
    out = []
    for e in elements:
        lbl = (e.get("label") or e.get("value") or "").strip().replace("\n", " ")
        role = e["role"]
        if role == "AXTextField":
            lbl = lbl or "(地址栏/输入框)"
        elif not lbl or role not in _LLM_ROLES:
            continue
        i = len(_idmap)
        _idmap[i] = e["id"]
        out.append(f'[{i}] {role} "{lbl[:48]}"')
        if len(out) >= 55:
            break
    cursor(None, note or "查看界面")
    return "elements:\n" + "\n".join(out) if out else "no actionable elements"


def t_click(index: int, note: str = "", **_: object) -> str:
    eid = _idmap.get(int(index))
    if not eid:
        return "bad index (snapshot first)"
    r = run_act("act", "--app", APP, "--id", eid, "--verb", "press", "--no-cursor")
    _trace.append(
        {
            "step": -1,
            "kind": "act",
            "name": "click",
            "wall_ms": 0.0,
            "internal": r.get("timing", {}),
        }
    )
    cursor(r.get("point"), note or "点击")
    return f"ok={r.get('ok')} err={r.get('error')} changed={len(r.get('diff', []))}"


def t_type(text: str, note: str = "", **_: object) -> str:
    r = run_act("act", "--app", APP, "--verb", "type", "--text", text, "--no-cursor")
    _trace.append(
        {"step": -1, "kind": "act", "name": "type", "wall_ms": 0.0, "internal": r.get("timing", {})}
    )
    cursor(r.get("point"), note or "输入")
    return f"typed ok={r.get('ok')}"


def t_key(keys: str, note: str = "", **_: object) -> str:
    r = run_act("act", "--app", APP, "--verb", "key", "--keys", keys, "--no-cursor")
    _trace.append(
        {"step": -1, "kind": "act", "name": "key", "wall_ms": 0.0, "internal": r.get("timing", {})}
    )
    cursor(r.get("point"), note or keys)
    return f"key {keys} ok={r.get('ok')}"


def t_done(summary: str = "", **_: object) -> str:
    cursor(None, "完成 ✅")
    return "DONE: " + summary


DISPATCH = {
    "activate": t_activate,
    "snapshot": t_snapshot,
    "click": t_click,
    "type": t_type,
    "key": t_key,
    "done": t_done,
}


def _fn(name: str, desc: str, props: dict, req: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {"type": "object", "properties": props, "required": req},
        },
    }


TOOLS = [
    _fn("activate", "bring Tabbit to front", {"note": {"type": "string"}}, ["note"]),
    _fn(
        "snapshot",
        "list actionable elements with indices; call before clicking",
        {"note": {"type": "string"}},
        ["note"],
    ),
    _fn(
        "click",
        "click element by its snapshot index",
        {"index": {"type": "integer"}, "note": {"type": "string"}},
        ["index", "note"],
    ),
    _fn(
        "type",
        "type into the focused field",
        {"text": {"type": "string"}, "note": {"type": "string"}},
        ["text", "note"],
    ),
    _fn(
        "key",
        "press a key combo, e.g. enter",
        {"keys": {"type": "string"}, "note": {"type": "string"}},
        ["keys", "note"],
    ),
    _fn("done", "Gmail inbox reached", {"summary": {"type": "string"}}, ["summary"]),
]


def address_bar() -> str:
    """Current Tabbit address-bar value (for reset + success checks) — '' if none."""
    d = run_act("snapshot", "--app", APP)
    bar = next((e for e in d.get("elements", []) if e["role"] == "AXTextField"), None)
    return (bar.get("value") or "") if bar else ""


def reset_off_gmail() -> bool:
    """Open a fresh Tabbit tab (off Gmail) so the run measures a real navigation chain.

    Uses an AX *menu-item press* ("新标签页" / "New Tab") — an accessibility action, so it lands
    regardless of keyboard focus. (Synthetic CGEvent keys are unreliable when another app holds
    focus, and Tabbit exposes no AppleScript URL dictionary, so neither cmd+t nor `set URL` is a
    dependable reset.) Returns True if the address bar is no longer on mail.google.com; the
    benchmark records reset_ok so a short-circuited run is reported honestly."""
    subprocess.run(
        ["osascript", "-e", f'tell application id "{APP}" to activate'], capture_output=True
    )
    time.sleep(0.8)
    if "mail.google" not in address_bar():
        return True
    snap = run_act("snapshot", "--app", APP)
    newtab = next(
        (
            e
            for e in snap.get("elements", [])
            if e["role"] == "AXMenuItem"
            and any(k in (e.get("label") or "") for k in ("新建标签", "新标签页", "New Tab"))
        ),
        None,
    )
    if newtab:
        run_act("act", "--app", APP, "--id", newtab["id"], "--verb", "press", "--no-cursor")
        time.sleep(1.2)
    return "mail.google" not in address_bar()


def llm(cfg: dict, key: str, messages: list[dict]) -> tuple[dict, float]:
    body = {
        "model": cfg["model"],
        "messages": messages,
        "tools": TOOLS,
        "tool_choice": "auto",
        "temperature": 0,
        "max_tokens": 900,
    }
    body.update(cfg["extra"])
    req = urllib.request.Request(
        cfg["base"],
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    t = time.perf_counter()
    resp = json.loads(urllib.request.urlopen(req, timeout=120).read())
    return resp["choices"][0]["message"], (time.perf_counter() - t) * 1000


def run_one(preset: str) -> dict:
    cfg = PRESETS[preset]
    key = os.environ.get(cfg["key_env"], "")
    if not key:
        return {"preset": preset, "skipped": f"{cfg['key_env']} not set"}
    _trace.clear()
    reset_ok = reset_off_gmail()
    messages = [{"role": "system", "content": SYS}, {"role": "user", "content": TASK}]
    llm_ms: list[float] = []
    reached = False
    t0 = time.perf_counter()
    for step in range(STEP_CAP):
        try:
            m, ms = llm(cfg, key, messages)
        except Exception as exc:  # noqa: BLE001 — a provider/network error ends this run, not the suite
            print(f"  [{step}] LLM error: {type(exc).__name__}: {str(exc)[:80]}")
            break
        llm_ms.append(ms)
        _trace.append({"step": step, "kind": "llm", "name": "llm", "wall_ms": ms, "internal": {}})
        m.pop("reasoning", None)
        m.pop("reasoning_content", None)
        messages.append(m)
        tcs = m.get("tool_calls") or []
        if not tcs:
            if not m.get("content"):
                break
            continue
        stop = False
        for tc in tcs:
            name = tc["function"]["name"]
            args = json.loads(tc["function"]["arguments"] or "{}")
            res = DISPATCH[name](**args)
            shown = {k: (v[:24] if isinstance(v, str) else v) for k, v in args.items()}
            print(f"  [{step}] llm={ms:.0f}ms {name}({shown}) ⇒ {res.splitlines()[0][:46]}")
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": res})
            if name == "done":
                stop = True
        if stop:
            break
    total = time.perf_counter() - t0
    reached = "mail.google" in address_bar()  # ground truth, not the model's word
    act_ms = sum(
        sum(v for v in (s["internal"] or {}).values()) for s in _trace if s["kind"] == "act"
    )
    return {
        "preset": preset,
        "label": cfg["label"],
        "reset_ok": reset_ok,
        "reached_gmail": reached,
        "steps": len(llm_ms),
        "total_s": round(total, 1),
        "llm_mean_ms": round(sum(llm_ms) / len(llm_ms)) if llm_ms else None,
        "llm_min_ms": round(min(llm_ms)) if llm_ms else None,
        "llm_max_ms": round(max(llm_ms)) if llm_ms else None,
        "llm_sum_s": round(sum(llm_ms) / 1000, 1),
        "actuation_ms": round(act_ms, 1),
    }


def main() -> None:
    global _hud
    sel = os.environ.get("BENCH_PRESET", "deepseek-v4-flash")
    presets = list(PRESETS) if sel == "all" else [sel]
    if sel != "all" and sel not in PRESETS:
        print(f"unknown preset {sel!r}; choose from {list(PRESETS)} or 'all'")
        sys.exit(2)
    if CURSOR_ON:
        _hud = subprocess.Popen([ACT, "cursor-hud"], stdin=subprocess.PIPE, text=True)

    results = []
    for p in presets:
        print(f"\n=== {p} ({PRESETS[p]['label']}) ===")
        r = run_one(p)
        results.append(r)
        if r.get("skipped"):
            print(f"  skipped: {r['skipped']}")

    if _hud is not None:
        time.sleep(1.5)
        with contextlib.suppress(OSError):
            _hud.stdin.close()  # type: ignore[union-attr]

    trace_path = os.environ.get("BENCH_TRACE")
    if trace_path and _trace:
        steps = sorted({s["step"] for s in _trace if s["step"] >= 0})
        with open(trace_path, "w") as fh:
            json.dump(
                {
                    "total_ms": sum(s["wall_ms"] for s in _trace),
                    "trace": [s for s in _trace if s["step"] >= 0 or steps],
                },
                fh,
            )
        print(
            f"\nflamegraph trace → {trace_path}  (render: python3 tests/manual/actuation_flamegraph.py {trace_path})"
        )

    print("\n" + "=" * 76)
    print(
        f"{'preset':<22}{'reached':<9}{'steps':<7}{'llm mean':<10}{'llm min/max':<14}{'total':<8}{'act':<8}"
    )
    print("-" * 76)
    for r in results:
        if r.get("skipped"):
            print(f"{r['preset']:<22}{'SKIP — ' + r['skipped']}")
            continue
        mark = "✅" if r["reached_gmail"] else "❌"
        rng = f"{r['llm_min_ms']}/{r['llm_max_ms']}"
        print(
            f"{r['preset']:<22}{mark:<9}{r['steps']:<7}{str(r['llm_mean_ms']) + 'ms':<10}"
            f"{rng:<14}{str(r['total_s']) + 's':<8}{str(r['actuation_ms']) + 'ms':<8}"
        )
    print("=" * 76)
    print("note: 'act' = total in-actuator time (snapshot+perform); the LLM dominates wall-clock.")
    for r in results:
        if not r.get("skipped"):
            print("json " + json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Opt-in demo: a DeepSeek-driven computer-use agent over the Persome actuation layer.

Gives the model a real task ("open Tabbit, view Gmail") and lets it drive the actuator (snapshot →
click/type/key) in a function-calling loop. Each action flashes the Persome cursor + the model's short
Chinese step note, so the user watches the agent operate. Navigation/viewing only — the system prompt
forbids sending/deleting.

Validated live (deepseek-chat): activate Tabbit → snapshot (GitHub page) → cmd+l failed → it RECOVERED
by clicking the address bar directly → typed the Gmail URL → Enter → snapshot saw "写邮件" (Compose)
→ done. Gmail inbox reached autonomously.

Run (needs DEEPSEEK_API_KEY + Tabbit installed; sends a few real keystrokes/clicks to the browser):
  swiftc -O -framework Cocoa -framework ApplicationServices resources/mac-ax-actuator.swift -o /tmp/mac-ax-actuator
  PERSOME_ACTUATION_E2E=1 DEEPSEEK_API_KEY=... python3 tests/manual/deepseek_computer_use.py
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

if os.environ.get("PERSOME_ACTUATION_E2E") != "1":
    print("refusing to run: set PERSOME_ACTUATION_E2E=1 (drives a real browser)")
    sys.exit(2)

ACT = "/tmp/mac-ax-actuator"
KEY = os.environ["DEEPSEEK_API_KEY"]
APP = "com.tab-browser.Tabbit"
TASK = "打开 Tabbit 浏览器并查看 Gmail（收件箱）。只做导航/查看，绝不发送邮件、不点删除或任何破坏性按钮。到达 Gmail 收件箱后调用 done。"

_idmap = {}  # index -> element id for the latest snapshot


def run_act(*a):
    return json.loads(
        subprocess.run([ACT, *a], capture_output=True).stdout.decode("utf-8", "replace")
    )


def t_activate(app, note=""):
    subprocess.run(
        ["osascript", "-e", f'tell application id "{app}" to activate'], capture_output=True
    )
    time.sleep(1.0)
    return "activated"


def t_snapshot(app, note=""):
    global _idmap
    snap = run_act("snapshot", "--app", app)
    if not snap.get("ok"):
        return f"snapshot failed: {snap.get('error')}"
    _idmap = {}
    out = []
    for e in snap.get("elements", []):
        lbl = (e.get("label") or e.get("value") or "").strip().replace("\n", " ")
        if not lbl:
            continue
        if e["role"] not in (
            "AXButton",
            "AXLink",
            "AXTextField",
            "AXTextArea",
            "AXMenuItem",
            "AXPopUpButton",
            "AXCheckBox",
            "AXRadioButton",
            "AXComboBox",
        ):
            continue
        i = len(_idmap)
        _idmap[i] = e["id"]
        out.append(f'[{i}] {e["role"]} "{lbl[:48]}"')
        if len(out) >= 55:
            break
    return "elements:\n" + "\n".join(out) if out else "no actionable labeled elements"


def t_click(index, note=""):
    eid = _idmap.get(int(index))
    if not eid:
        return "bad index (snapshot first)"
    r = run_act(
        "act",
        "--app",
        APP,
        "--id",
        eid,
        "--verb",
        "press",
        "--note",
        note or "点击",
        "--feedback-seconds",
        "0.8",
    )
    return f"ok={r.get('ok')} err={r.get('error')} changed={len(r.get('diff', []))}"


def t_type(text, note=""):
    r = run_act(
        "act",
        "--app",
        APP,
        "--verb",
        "type",
        "--text",
        text,
        "--note",
        note or "输入",
        "--feedback-seconds",
        "0.8",
    )
    return f"typed ok={r.get('ok')}"


def t_key(keys, note=""):
    r = run_act(
        "act",
        "--app",
        APP,
        "--verb",
        "key",
        "--keys",
        keys,
        "--note",
        note or keys,
        "--feedback-seconds",
        "0.8",
    )
    return f"key {keys} ok={r.get('ok')}"


def t_done(summary=""):
    return "DONE: " + summary


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "activate",
            "description": "Bring an app to front by bundle id (use com.tab-browser.Tabbit for Tabbit).",
            "parameters": {
                "type": "object",
                "properties": {"app": {"type": "string"}, "note": {"type": "string"}},
                "required": ["app"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "snapshot",
            "description": "List the app's actionable labeled UI elements with indices. Call before clicking.",
            "parameters": {
                "type": "object",
                "properties": {"app": {"type": "string"}, "note": {"type": "string"}},
                "required": ["app"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "Click the element by its index from the latest snapshot.",
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "note": {"type": "string", "description": "short, e.g. 正在打开Gmail"},
                },
                "required": ["index", "note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type",
            "description": "Type text into the currently focused field (e.g. the address bar after focusing it).",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "note": {"type": "string"}},
                "required": ["text", "note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "key",
            "description": "Press a key combo: enter, cmd+l (focus address bar), cmd+t (new tab), cmd+1..9 (switch tab).",
            "parameters": {
                "type": "object",
                "properties": {"keys": {"type": "string"}, "note": {"type": "string"}},
                "required": ["keys", "note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": "Call when Gmail inbox is visible.",
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
            },
        },
    },
]
DISPATCH = {
    "activate": t_activate,
    "snapshot": t_snapshot,
    "click": t_click,
    "type": t_type,
    "key": t_key,
    "done": t_done,
}


def deepseek(messages):
    body = json.dumps(
        {
            "model": "deepseek-chat",
            "messages": messages,
            "tools": TOOLS,
            "tool_choice": "auto",
            "temperature": 0,
        }
    ).encode()
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    return json.loads(urllib.request.urlopen(req, timeout=60).read())["choices"][0]["message"]


messages = [
    {
        "role": "system",
        "content": "你是一个 macOS computer-use 助手。通过工具操作真实界面完成任务。每次点击前先 snapshot 看清元素。每个动作给一个简短中文 note。只做导航/查看，绝不发送/删除。",
    },
    {"role": "user", "content": TASK},
]
for step in range(14):
    m = deepseek(messages)
    messages.append(m)
    tcs = m.get("tool_calls") or []
    if not tcs:
        print(f"[{step}] model: {(m.get('content') or '')[:200]}")
        if not m.get("content"):
            break
        continue
    for tc in tcs:
        name = tc["function"]["name"]
        args = json.loads(tc["function"]["arguments"] or "{}")
        print(
            f"[{step}] → {name}({ {k: (v[:30] if isinstance(v, str) else v) for k, v in args.items()} })"
        )
        res = DISPATCH[name](**args)
        print(f"        ⇒ {res[:150]}")
        messages.append({"role": "tool", "tool_call_id": tc["id"], "content": res})
        if name == "done":
            print("\n✅ AGENT DONE:", res)
            sys.exit(0)
print("\n(reached step cap)")

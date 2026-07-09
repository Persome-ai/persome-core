#!/usr/bin/env python3
"""Reproducible computer-use case suite over the Persome actuation layer — a GROWING benchmark.

One generic, LLM-driven (DeepSeek-V4-Flash) computer-use loop drives every case; cases differ ONLY in
data: a task prompt, a deterministic `verify()` that reads the resulting AX/clipboard/filesystem state,
and an optional `reset()`/`cleanup()`. The model gets the SAME app-agnostic tools for every task —
`activate`, `ax_snapshot`, `ax_find`, `ocr_locate`, `clickxy`, `ax_set_value`, `ax_press`, `type_text`,
`key`, `done` — so nothing here is scripted to a specific task: change the prompt and the same tools
solve the new task. (That is the generality bar: the LLM does the thinking; the tools are just hands +
eyes.)

Why these cases: chosen to mirror a real person's daily macOS apps (Calculator, Tabbit browser,
Feishu/Lark, WeChat, VSCode, System Settings, Finder, TextEdit) across varied interaction patterns
(AX press / set-value, OCR + click, key combos, OCR + type into a search box, menu-bar navigation) and
to be REPRODUCIBLE — each has a clean reset and a machine-checkable outcome, so a green run today is
green tomorrow. Read-only where possible; anything that writes is cleaned up.

  swiftc -O -framework Cocoa -framework ApplicationServices resources/mac-ax-actuator.swift -o /tmp/mac-ax-actuator
  PERSOME_ACTUATION_E2E=1 DEEPSEEK_API_KEY=... uv run python3 tests/manual/bench_cases.py            # all cases
  PERSOME_ACTUATION_E2E=1 DEEPSEEK_API_KEY=... uv run python3 tests/manual/bench_cases.py calc browser  # by name
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

if os.environ.get("PERSOME_ACTUATION_E2E") != "1":
    print("refusing to run: set PERSOME_ACTUATION_E2E=1 (drives real apps)")
    sys.exit(2)

sys.path.insert(0, "src")
from persome.capture import ocr_local  # noqa: E402

ACT = os.environ.get("PERSOME_AX_ACTUATOR", "/tmp/mac-ax-actuator")
KEY = os.environ.get("DEEPSEEK_API_KEY", "")
MODEL = "deepseek-v4-flash"  # the only model this suite is allowed to use
STEP_CAP = int(os.environ.get("BENCH_STEPCAP", "16"))

# every app a case might touch (the model addresses them by these short keys)
APPS = {
    "calc": "com.apple.calculator",
    "browser": "com.tab-browser.Tabbit",
    "lark": "com.electron.lark",
    "textedit": "com.apple.TextEdit",
    "notes": "com.apple.Notes",
    "reminders": "com.apple.reminders",
    "finder": "com.apple.finder",
    "sysprefs": "com.apple.systempreferences",
    "vscode": "com.microsoft.VSCode",
    "preview": "com.apple.Preview",
    "wechat": "com.tencent.xinWeChat",
}

SKILL = """你是 Persome 的 macOS computer-use 助手，用 AX 优先、OCR 兜底的工具操作真实界面。
- activate(app) 切换到某个 app（key 见任务）。
- 每次 clickxy / ax_set_value / ax_press 之后，工具直接回**当前可操作元素**（已编号 [N] 角色 "标签"），
  直接对编号操作，通常不必再 ax_snapshot。要找不在列表里的东西用 ax_find（AX 文本+层级）或 ocr_locate（像素文字）。
- AX 元素优先用 ax_press/ax_set_value（不抢光标）；像素按钮才 ocr_locate + clickxy。
- 完成任务后调用 done。每步配简短中文 note。只做任务要求的事，绝不发送消息、绝不删除用户数据。"""

# progressive disclosure: inject an app's operation manual the first time focus lands on it (the same
# per-app skills the meeting harness uses; knowledge, not scripts — the LLM still does the thinking).
_SKILL_DIR = Path(__file__).parent / "skills"
_SKILL_FILE = {"lark": "lark.md", "meeting": "tencent-meeting.md"}
_skills_loaded: set[str] = set()


def app_skill(app: str) -> str:
    if app in _skills_loaded or app not in _SKILL_FILE:
        return ""
    path = _SKILL_DIR / _SKILL_FILE[app]
    if not path.is_file():
        return ""
    _skills_loaded.add(app)
    return f"\n\n—— 已加载 {app} 操作手册（按它操作）——\n{path.read_text()}"


# ── actuation primitives (generic; identical for every case) ──────────────────

_idmap: dict[tuple[str, int], str] = {}
_llm_ms: list[float] = []
_ACTIONABLE = frozenset(
    {
        "AXButton",
        "AXTextField",
        "AXTextArea",
        "AXCheckBox",
        "AXRadioButton",
        "AXMenuButton",
        "AXMenuItem",
        "AXPopUpButton",
        "AXComboBox",
        "AXLink",
    }
)
_ROLE_PRIORITY = {
    "AXButton": 0,
    "AXTextField": 0,
    "AXTextArea": 0,
    "AXMenuButton": 1,
    "AXPopUpButton": 1,
    "AXComboBox": 1,
    "AXLink": 1,
    "AXMenuItem": 2,
    "AXCheckBox": 3,
    "AXRadioButton": 3,
}


def run_act(*a: str) -> dict:
    r = subprocess.run([ACT, *a], capture_output=True)
    try:
        return json.loads(r.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return {"ok": False, "error": "bad_json", "elements": []}


def els_of(app: str, depth: str = "60") -> list[dict]:
    return run_act("snapshot", "--app", APPS.get(app, app), "--depth", depth).get("elements", [])


def index_elements(app: str, elements: list[dict], cap: int = 30) -> list[str]:
    keep = []
    for e in elements:
        role = e.get("role", "")
        lbl = (e.get("label") or e.get("value") or "").strip().replace("\n", " ")
        if role in ("AXTextField", "AXTextArea"):  # editable inputs: keep even with no label
            lbl = lbl or ("(正文/输入框)" if role == "AXTextArea" else "(输入框)")
        elif not lbl or role not in _ACTIONABLE:
            continue
        keep.append((_ROLE_PRIORITY.get(role, 2), role, lbl, e["id"], e.get("bbox") or [0, 0, 0, 0]))
    keep.sort(key=lambda t: t[0])
    out = []
    for _, role, lbl, eid, b in keep[:cap]:
        i = len(_idmap)
        _idmap[(app, i)] = eid
        # carry position+size so the model can disambiguate same-role/blank-label elements (e.g. pick
        # the top-wide AXTextField = a browser address bar) without falling back to OCR.
        pos = f" @[{int(b[0])},{int(b[1])} {int(b[2])}x{int(b[3])}]" if b[2] > 0 else ""
        out.append(f'[{i}] {role} "{lbl[:32]}"{pos}')
    return out


def act_result(app: str, result: dict) -> str:
    els = result.get("elements", [])
    lines = index_elements(app, els)
    body = "\n".join(lines) if lines else "(无带标签可操作元素)"
    return f"ok={result.get('ok')}\n当前可操作元素:\n{body}"


def _front_window(app: str, elements: list[dict] | None = None) -> dict | None:
    """The frontmost/main window — LARGEST-area visible AXWindow. The actuator snapshots the whole
    app tree (all windows), so "first AXWindow" can be a stale background window (multi-window
    Finder/VSCode/browser); the active window is reliably the largest one on screen."""
    wins = [
        e
        for e in (elements if elements is not None else els_of(app))
        if e["role"] == "AXWindow" and e.get("bbox") and e["bbox"][2] > 0 and e["bbox"][3] > 0
    ]
    return max(wins, key=lambda e: e["bbox"][2] * e["bbox"][3]) if wins else None


def win_rect(app: str) -> list[float] | None:
    w = _front_window(app)
    return w["bbox"] if w else None


# ── tools ─────────────────────────────────────────────────────────────────────


def t_activate(app: str, note: str = "", **_: object) -> str:
    bundle = APPS.get(app)
    if not bundle:
        return f"unknown app {app!r}; pick from {list(APPS)}"
    subprocess.run(["open", "-b", bundle], capture_output=True)
    subprocess.run(
        ["osascript", "-e", f'tell application id "{bundle}" to activate'], capture_output=True
    )
    for _ in range(16):
        time.sleep(0.25)
        if win_rect(app) is not None:
            break
    return f"activated {app}" + app_skill(app)  # inject the app's manual on first focus


def t_ax_snapshot(app: str, note: str = "", **_: object) -> str:
    lines = index_elements(app, els_of(app), cap=40)
    return (
        "当前可操作元素:\n" + "\n".join(lines)
        if lines
        else "no labeled actionable AX elements (try ocr_locate)"
    )


def _path_of(eid: str) -> str:
    import base64

    with contextlib.suppress(Exception):
        return base64.b64decode(eid).decode().split("#")[0]
    return ""


def _group_label(n: int) -> str:
    """A/B/…/Z then AA/AB/… — never overflows past Z into non-letter chars when an app's matches
    span >26 AX containers (deep trees like VSCode/Lark)."""
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def t_ax_find(app: str, query: str, note: str = "", **_: object) -> str:
    groups: dict[str, str] = {}
    out = []
    for e in els_of(app):
        txt = (e.get("label") or e.get("value") or "").strip()
        if query not in txt:
            continue
        b = e.get("bbox") or [0, 0, 0, 0]
        g = groups.setdefault(".".join(_path_of(e["id"]).split(".")[:12]), _group_label(len(groups)))
        i = len(_idmap)
        _idmap[(app, i)] = e["id"]
        vis = "可见" if b[2] > 0 and b[3] > 0 else "隐藏"
        out.append(
            f'[{i}] {e["role"]} "{txt[:24]}" 容器{g} {vis} bbox=[{int(b[0])},{int(b[1])},{int(b[2])},{int(b[3])}]'
        )
        if len(out) >= 40:
            break
    return (
        f"「{query}」匹配（同字母=同容器；选对的用 clickxy bbox 中心 或 ax_press 编号）:\n"
        + "\n".join(out)
        if out
        else f"{query!r} 不在 AX 树里（可能像素绘制 → ocr_locate）"
    )


def t_ocr_locate(query: str, app: str, note: str = "", **_: object) -> str:
    rect = win_rect(app)
    if not rect:
        return "no window to OCR"
    x, y, w, h = (int(v) for v in rect)
    subprocess.run(
        ["screencapture", "-x", f"-R{x},{y},{w},{h}", "/tmp/_cases_ocr.png"], capture_output=True
    )
    res = ocr_local.recognize_detailed(Path("/tmp/_cases_ocr.png").read_bytes())
    if not res:
        return "OCR unavailable"
    # ALL matches (not just the first), sorted top→bottom, so the model can disambiguate duplicates —
    # e.g. the text it just typed echoing in a search box vs the real result row below it.
    hits = []
    for t, b in zip(res[0], res[1], strict=False):
        if query in t:
            cx = rect[0] + (b[0] + b[2]) / 4
            cy = rect[1] + (b[1] + b[3]) / 4
            hits.append((cy, cx, t))
    hits.sort()
    if not hits:
        return f"{query!r} not found on screen (texts: {[t[:8] for t in res[0][:14]]})"
    lines = [f'  ({cx:.0f},{cy:.0f}) "{t[:24]}"' for cy, cx, t in hits[:10]]
    return f"{query!r} 匹配 {len(hits)} 处（屏幕坐标，从上到下；clickxy 选对的那个）:\n" + "\n".join(lines)


def t_clickxy(app: str, x: float, y: float, note: str = "", **_: object) -> str:
    r = run_act(
        "act",
        "--app",
        APPS.get(app, app),
        "--verb",
        "clickxy",
        "--x",
        str(x),
        "--y",
        str(y),
        "--no-cursor",
        "--cache-before",
    )
    return act_result(app, r)


def t_ax_set_value(app: str, index: int, text: str, note: str = "", **_: object) -> str:
    eid = _idmap.get((app, int(index)))
    if not eid:
        return "bad index (snapshot first)"
    r = run_act(
        "act",
        "--app",
        APPS.get(app, app),
        "--id",
        eid,
        "--verb",
        "setvalue",
        "--text",
        text,
        "--no-cursor",
        "--cache-before",
    )
    return act_result(app, r)


def t_ax_press(app: str, index: int, note: str = "", **_: object) -> str:
    eid = _idmap.get((app, int(index)))
    if not eid:
        return "bad index (snapshot first)"
    r = run_act(
        "act",
        "--app",
        APPS.get(app, app),
        "--id",
        eid,
        "--verb",
        "press",
        "--no-cursor",
        "--cache-before",
    )
    return act_result(app, r)


def t_type_text(app: str, text: str, note: str = "", **_: object) -> str:
    # type free text into whatever is currently focused — the generic complement to ax_set_value for
    # pixel/Electron search boxes (WeChat, Spotlight-likes) that AX can't address by index. Never
    # presses Return on its own, so it can't send a message; the model must decide that separately.
    r = run_act(
        "act",
        "--app",
        APPS.get(app, app),
        "--verb",
        "type",
        "--text",
        text,
        "--no-cursor",
        "--cache-before",
    )
    return act_result(app, r)


def t_key(app: str, keys: str, note: str = "", **_: object) -> str:
    r = run_act(
        "act",
        "--app",
        APPS.get(app, app),
        "--verb",
        "key",
        "--keys",
        keys,
        "--no-cursor",
        "--cache-before",
    )
    return act_result(app, r)


def t_done(summary: str = "", **_: object) -> str:
    return "DONE: " + summary


DISPATCH = {
    "activate": t_activate,
    "ax_snapshot": t_ax_snapshot,
    "ax_find": t_ax_find,
    "ocr_locate": t_ocr_locate,
    "clickxy": t_clickxy,
    "ax_set_value": t_ax_set_value,
    "ax_press": t_ax_press,
    "type_text": t_type_text,
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


_S = {"type": "string"}
_I = {"type": "integer"}
TOOLS = [
    _fn(
        "activate",
        "bring an app to front (key: " + "/".join(APPS) + ")",
        {"app": _S, "note": _S},
        ["app", "note"],
    ),
    _fn(
        "ax_snapshot",
        "list an app's labeled actionable AX elements with indices",
        {"app": _S, "note": _S},
        ["app", "note"],
    ),
    _fn(
        "ax_find",
        "find AX elements matching text, tagged with container/visibility/bbox",
        {"app": _S, "query": _S, "note": _S},
        ["app", "query", "note"],
    ),
    _fn(
        "ocr_locate",
        "OCR-find PIXEL-drawn text on the front window → ALL matching screen coords, top→bottom "
        "(pick the right one when a word appears more than once)",
        {"app": _S, "query": _S, "note": _S},
        ["app", "query", "note"],
    ),
    _fn(
        "clickxy",
        "click screen coords",
        {"app": _S, "x": {"type": "number"}, "y": {"type": "number"}, "note": _S},
        ["app", "x", "y", "note"],
    ),
    _fn(
        "ax_set_value",
        "set an editable field's value by snapshot index",
        {"app": _S, "index": _I, "text": _S, "note": _S},
        ["app", "index", "text", "note"],
    ),
    _fn(
        "ax_press",
        "press a button/element by snapshot index",
        {"app": _S, "index": _I, "note": _S},
        ["app", "index", "note"],
    ),
    _fn(
        "type_text",
        "type free text into the CURRENTLY FOCUSED field (for pixel/search boxes AX can't set by "
        "index; click the field first). Does NOT press Return.",
        {"app": _S, "text": _S, "note": _S},
        ["app", "text", "note"],
    ),
    _fn(
        "key",
        "press a key combo (e.g. enter, cmd+a, delete) in an app",
        {"app": _S, "keys": _S, "note": _S},
        ["app", "keys", "note"],
    ),
    _fn("done", "the task is complete", {"summary": _S}, ["summary"]),
]


def llm(messages: list[dict]) -> tuple[dict, float]:
    body = json.dumps(
        {
            "model": MODEL,
            "messages": messages,
            "tools": TOOLS,
            "tool_choice": "auto",
            "temperature": 0,
            "max_tokens": 900,
            "thinking": {"type": "disabled"},
        }
    ).encode()
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    t = time.perf_counter()
    resp = json.loads(urllib.request.urlopen(req, timeout=120).read())
    return resp["choices"][0]["message"], (time.perf_counter() - t) * 1000


def run_task(task: str) -> tuple[bool, float, int]:
    """Drive one task to done / step cap. Returns (reached_done, seconds, llm_calls)."""
    _idmap.clear()
    _skills_loaded.clear()
    messages = [{"role": "system", "content": SKILL}, {"role": "user", "content": task}]
    t0 = time.perf_counter()
    done = False
    for step in range(STEP_CAP):
        try:
            m, ms = llm(messages)
        except Exception as exc:  # noqa: BLE001
            print(f"    [{step}] LLM error: {type(exc).__name__}: {str(exc)[:70]}")
            break
        _llm_ms.append(ms)
        m.pop("reasoning", None)
        m.pop("reasoning_content", None)
        messages.append(m)
        tcs = m.get("tool_calls") or []
        if not tcs:
            if not m.get("content"):
                break
            continue
        for tc in tcs:
            name = tc["function"]["name"]
            args = json.loads(tc["function"]["arguments"] or "{}")
            try:
                fn = DISPATCH.get(name)
                res = fn(**args) if fn else f"unknown tool {name!r}"
            except Exception as exc:  # noqa: BLE001 — a malformed tool call must NOT crash the run; tell the model
                res = f"tool error: {type(exc).__name__}: {exc}"
            shown = {k: (v[:18] if isinstance(v, str) else v) for k, v in args.items()}
            print(f"    [{step}] {name}({shown}) ⇒ {res.splitlines()[0][:54]}")
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": res})
            if name == "done":
                done = True
        if done:
            break
    return (
        done,
        time.perf_counter() - t0,
        len([s for s in messages if s.get("role") == "assistant"]),
    )


# ── cases (data only; the harness above is generic) ───────────────────────────


def _calc_copy_reset() -> None:
    subprocess.run(["pbcopy"], input=b"", capture_output=True)  # clear clipboard first
    subprocess.run(["open", "-b", APPS["calc"]], capture_output=True)
    time.sleep(0.8)
    run_act("act", "--app", APPS["calc"], "--verb", "key", "--keys", "escape", "--no-cursor")  # AC


def _calc_copy_verify() -> bool:
    return "56" in subprocess.run(["pbpaste"], capture_output=True).stdout.decode()


def _calc_copy_cleanup() -> None:
    subprocess.run(["pbcopy"], input=b"", capture_output=True)


def _calc2te_reset() -> None:
    subprocess.run(["pbcopy"], input=b"", capture_output=True)
    subprocess.run(["open", "-b", APPS["calc"]], capture_output=True)
    time.sleep(0.6)
    run_act("act", "--app", APPS["calc"], "--verb", "key", "--keys", "escape", "--no-cursor")  # AC
    subprocess.run(
        ["osascript", "-e", 'tell application "TextEdit" to close every document saving no'],
        capture_output=True,
    )
    subprocess.run(
        ["osascript", "-e", 'tell application "TextEdit" to make new document'], capture_output=True
    )
    time.sleep(0.6)


def _calc2te_verify() -> bool:
    return any("56" in (e.get("value") or "") for e in els_of("textedit") if e["role"] == "AXTextArea")


def _calc2te_cleanup() -> None:
    _textedit_cleanup()
    subprocess.run(["pbcopy"], input=b"", capture_output=True)


def _calc_reset() -> None:
    subprocess.run(["open", "-b", APPS["calc"]], capture_output=True)
    time.sleep(1.0)
    run_act("act", "--app", APPS["calc"], "--verb", "key", "--keys", "escape", "--no-cursor")  # AC


def _calc_verify() -> bool:
    # the result is pixel-drawn in the display (AX exposes "编辑字段", not the number) → OCR it.
    # Match "56" as a STANDALONE OCR token (not a substring of 0.56 / 156 / a timestamp) so a stray
    # number elsewhere in the window can't false-pass.
    rect = win_rect("calc")
    if not rect:
        return False
    x, y, w, h = (int(v) for v in rect)
    subprocess.run(
        ["screencapture", "-x", f"-R{x},{y},{w},{h}", "/tmp/_cases_verify.png"], capture_output=True
    )
    res = ocr_local.recognize_detailed(Path("/tmp/_cases_verify.png").read_bytes())
    return bool(res) and any(t.strip() == "56" for t in res[0])


# ── calc menu-navigation case: switch to Scientific via the 显示/View menu (AXMenuItem nav — a pattern
# no other case tests). Reliable (menu items are pressable directly via AX), safe + reversible (cmd+1).
def _calc_sci_reset() -> None:
    subprocess.run(["open", "-b", APPS["calc"]], capture_output=True)
    time.sleep(0.8)
    run_act("act", "--app", APPS["calc"], "--verb", "key", "--keys", "cmd+1", "--no-cursor")  # Basic
    time.sleep(0.5)


def _calc_sci_verify() -> bool:
    # Scientific view shows sin/cos/tan etc. (pixel-drawn → OCR the window)
    rect = win_rect("calc")
    if not rect:
        return False
    x, y, w, h = (int(v) for v in rect)
    subprocess.run(
        ["screencapture", "-x", f"-R{x},{y},{w},{h}", "/tmp/_cases_verify.png"], capture_output=True
    )
    res = ocr_local.recognize_detailed(Path("/tmp/_cases_verify.png").read_bytes())
    return bool(res) and any(k in t.lower() for t in res[0] for k in ("sin", "cos", "tan"))


def _calc_sci_cleanup() -> None:
    run_act("act", "--app", APPS["calc"], "--verb", "key", "--keys", "cmd+1", "--no-cursor")  # Basic


def _calc_ocr_result(target: str):
    """Factory: a 0-arg verify that OCRs the calc display for a STANDALONE result token (e.g. '81')."""

    def _v() -> bool:
        rect = win_rect("calc")
        if not rect:
            return False
        x, y, w, h = (int(v) for v in rect)
        subprocess.run(
            ["screencapture", "-x", f"-R{x},{y},{w},{h}", "/tmp/_cases_verify.png"], capture_output=True
        )
        res = ocr_local.recognize_detailed(Path("/tmp/_cases_verify.png").read_bytes())
        return bool(res) and any(t.strip() == target for t in res[0])

    return _v


_calc_keytype_verify = _calc_ocr_result("81")


def _calc_prog_verify() -> bool:
    # Programmer view shows bitwise-op buttons (AND/OR/XOR…) that basic/scientific don't — OCR for them
    rect = win_rect("calc")
    if not rect:
        return False
    x, y, w, h = (int(v) for v in rect)
    subprocess.run(
        ["screencapture", "-x", f"-R{x},{y},{w},{h}", "/tmp/_cases_verify.png"], capture_output=True
    )
    res = ocr_local.recognize_detailed(Path("/tmp/_cases_verify.png").read_bytes())
    return bool(res) and any(k in t for t in res[0] for k in ("AND", "OR", "XOR", "NOR"))


def _browser_reset() -> None:
    subprocess.run(
        ["osascript", "-e", f'tell application id "{APPS["browser"]}" to activate'],
        capture_output=True,
    )
    time.sleep(1.0)
    # leave it on a blank-ish page so the case has to navigate
    bar = next((e for e in els_of("browser") if e["role"] == "AXTextField"), None)
    if bar:
        run_act(
            "act", "--app", APPS["browser"], "--id", bar["id"], "--verb", "press", "--no-cursor"
        )
        run_act("act", "--app", APPS["browser"], "--verb", "key", "--keys", "cmd+a", "--no-cursor")
        run_act(
            "act",
            "--app",
            APPS["browser"],
            "--verb",
            "type",
            "--text",
            "about:blank",
            "--no-cursor",
        )
        run_act("act", "--app", APPS["browser"], "--verb", "key", "--keys", "enter", "--no-cursor")
        time.sleep(1.5)


def _browser_verify() -> bool:
    # require ACTUAL navigation, not just the URL typed into the address bar (which would also be true
    # if the model typed it but never pressed enter). The address bar must hold example.com AND the
    # page must have loaded — its title/heading "Example Domain" shows up in the AX tree / window title.
    els = els_of("browser")
    in_bar = any(
        "example.com" in (e.get("value") or "") for e in els if e["role"] == "AXTextField"
    )
    loaded = any(
        "Example Domain" in (e.get("label") or e.get("value") or "")
        or (e["role"] == "AXWindow" and "Example" in (e.get("label") or ""))
        for e in els
    )
    return in_bar and loaded


# ── cross-app + clipboard case (a dimension only the flagship meeting flow tests): copy text from
# TextEdit, paste it into the browser address bar. All key-combos + ax_set_value — no inline-edit.
_CLIP_MARK = "persome-clip-xyz"


def _clip_reset() -> None:
    # seed a TextEdit doc with the marker (AppleScript — off the timed path) and blank the browser
    subprocess.run(
        ["osascript", "-e", 'tell application "TextEdit" to close every document saving no'],
        capture_output=True,
    )
    subprocess.run(
        ["osascript", "-e", f'tell application "TextEdit" to make new document with properties {{text:"{_CLIP_MARK}"}}'],
        capture_output=True,
    )
    time.sleep(0.6)
    _browser_reset()  # leave the address bar on about:blank so the paste target is empty


def _clip_verify() -> bool:
    # the marker reached the browser address bar via the clipboard (NOT navigated — just pasted)
    return any(
        _CLIP_MARK in (e.get("value") or "")
        for e in els_of("browser")
        if e["role"] == "AXTextField"
    )


def _clip_cleanup() -> None:
    _browser_reset()
    subprocess.run(
        ["osascript", "-e", 'tell application "TextEdit" to close every document saving no'],
        capture_output=True,
    )
    subprocess.run(["pbcopy"], input=b"", capture_output=True)  # don't leave the marker on the clipboard


def _lark_reset() -> None:
    # best-effort neutral state so a residual already-open 温子墨 chat can't false-pass without the
    # open path running: focus Feishu + Escape to dismiss the open chat/overlay (Escape never sends or
    # deletes — safe). Mirrors _wechat_reset. (Even if it can't fully clear the header, the model still
    # runs its ax_find→clickxy open sequence each round, so the path is exercised.)
    subprocess.run(
        ["osascript", "-e", f'tell application id "{APPS["lark"]}" to activate'], capture_output=True
    )
    time.sleep(0.8)
    for _ in range(2):
        run_act("act", "--app", APPS["lark"], "--verb", "key", "--keys", "escape", "--no-cursor")
        time.sleep(0.3)


def _lark_verify() -> bool:
    # read-only: the active chat header (top, x>700) is 温子墨
    return any(
        e["role"] == "AXStaticText"
        and (e.get("label") or e.get("value") or "").strip() == "温子墨"
        and e.get("bbox")
        and e["bbox"][0] > 700
        and e["bbox"][1] < 130
        and e["bbox"][2] > 0
        for e in els_of("lark")
    )


def _textedit_reset() -> None:
    subprocess.run(
        ["osascript", "-e", 'tell application "TextEdit" to close every document saving no'],
        capture_output=True,
    )
    time.sleep(0.6)
    subprocess.run(
        ["osascript", "-e", 'tell application "TextEdit" to make new document'], capture_output=True
    )
    time.sleep(0.6)


def _textedit_verify() -> bool:
    return any(
        "Persome 自动化" in (e.get("value") or "")
        for e in els_of("textedit")
        if e["role"] == "AXTextArea"
    )


def _textedit_cleanup() -> None:
    subprocess.run(
        ["osascript", "-e", 'tell application "TextEdit" to close every document saving no'],
        capture_output=True,
    )


# ── textedit clear case: a distinct EDITING action (select-all + delete) vs writing — verify empty.
def _textedit_clear_reset() -> None:
    subprocess.run(
        ["osascript", "-e", 'tell application "TextEdit" to close every document saving no'],
        capture_output=True,
    )
    subprocess.run(
        ["osascript", "-e", 'tell application "TextEdit" to make new document with properties {text:"CLEAR THIS TEXT 删除我"}'],
        capture_output=True,
    )
    time.sleep(0.6)


def _textedit_clear_verify() -> bool:
    # the document body AXTextArea is now empty (the seeded text is gone)
    areas = [e.get("value") or "" for e in els_of("textedit") if e["role"] == "AXTextArea"]
    return bool(areas) and all(v.strip() == "" for v in areas)


_CUT_MARK = "剪切走CUTME"


def _textedit_cut_reset() -> None:
    subprocess.run(["pbcopy"], input=b"", capture_output=True)  # clear clipboard
    subprocess.run(
        ["osascript", "-e", 'tell application "TextEdit" to close every document saving no'],
        capture_output=True,
    )
    subprocess.run(
        ["osascript", "-e", f'tell application "TextEdit" to make new document with properties {{text:"{_CUT_MARK}"}}'],
        capture_output=True,
    )
    time.sleep(0.6)


def _textedit_cut_verify() -> bool:
    # cut = REMOVED from the doc AND placed on the clipboard (both must hold)
    areas = [e.get("value") or "" for e in els_of("textedit") if e["role"] == "AXTextArea"]
    doc_empty = bool(areas) and all(_CUT_MARK not in v for v in areas)
    clip = subprocess.run(["pbpaste"], capture_output=True).stdout.decode()
    return doc_empty and _CUT_MARK in clip


def _textedit_cut_cleanup() -> None:
    _textedit_cleanup()
    subprocess.run(["pbcopy"], input=b"", capture_output=True)


_COPY_MARK = "复制我COPYME"


def _textedit_copy_reset() -> None:
    subprocess.run(["pbcopy"], input=b"", capture_output=True)
    subprocess.run(
        ["osascript", "-e", 'tell application "TextEdit" to close every document saving no'],
        capture_output=True,
    )
    subprocess.run(
        ["osascript", "-e", f'tell application "TextEdit" to make new document with properties {{text:"{_COPY_MARK}"}}'],
        capture_output=True,
    )
    time.sleep(0.6)


def _textedit_copy_verify() -> bool:
    # copy leaves the doc UNCHANGED and puts the text on the clipboard (vs cut, which empties the doc)
    areas = [e.get("value") or "" for e in els_of("textedit") if e["role"] == "AXTextArea"]
    doc_kept = any(_COPY_MARK in v for v in areas)
    clip = subprocess.run(["pbpaste"], capture_output=True).stdout.decode()
    return doc_kept and _COPY_MARK in clip


def _textedit_copy_cleanup() -> None:
    _textedit_cleanup()
    subprocess.run(["pbcopy"], input=b"", capture_output=True)


_PASTE_MARK = "粘贴进来PASTEME"


def _textedit_paste_reset() -> None:
    subprocess.run(["pbcopy"], input=_PASTE_MARK.encode(), capture_output=True)  # seed the clipboard
    subprocess.run(
        ["osascript", "-e", 'tell application "TextEdit" to close every document saving no'],
        capture_output=True,
    )
    subprocess.run(
        ["osascript", "-e", 'tell application "TextEdit" to make new document'], capture_output=True
    )
    time.sleep(0.6)


def _textedit_paste_verify() -> bool:
    return any(_PASTE_MARK in (e.get("value") or "") for e in els_of("textedit") if e["role"] == "AXTextArea")


def _textedit_paste_cleanup() -> None:
    _textedit_cleanup()
    subprocess.run(["pbcopy"], input=b"", capture_output=True)  # clear the clipboard


def _textedit_keytype_reset() -> None:
    subprocess.run(
        ["osascript", "-e", 'tell application "TextEdit" to close every document saving no'],
        capture_output=True,
    )
    subprocess.run(
        ["osascript", "-e", 'tell application "TextEdit" to make new document'], capture_output=True
    )
    time.sleep(0.6)


def _textedit_keytype_verify() -> bool:
    return any("hello" in (e.get("value") or "") for e in els_of("textedit") if e["role"] == "AXTextArea")


def _textedit_doc_windows() -> int:
    return sum(
        1
        for e in els_of("textedit")
        if e["role"] == "AXWindow" and e.get("bbox") and e["bbox"][2] > 100
    )


def _textedit_newwindow_reset() -> None:
    subprocess.run(
        ["osascript", "-e", 'tell application "TextEdit" to close every document saving no'],
        capture_output=True,
    )
    subprocess.run(
        ["osascript", "-e", 'tell application "TextEdit" to make new document'], capture_output=True
    )
    time.sleep(0.8)  # start with exactly ONE document window


def _textedit_newwindow_verify() -> bool:
    return _textedit_doc_windows() >= 2  # the new window appeared on top of the reset's one


# ── textedit menu-nav case (2nd app for the menu pattern, window-title oracle): 格式→字体→显示字体 opens
# the Fonts panel (a window titled 字体). Reliable + reversible (cmd+T toggles it).
def _fonts_panel_open() -> bool:
    return any(
        e["role"] == "AXWindow" and (e.get("label") or "") in ("字体", "Fonts")
        for e in els_of("textedit")
    )


def _textedit_fonts_reset() -> None:
    subprocess.run(["osascript", "-e", 'tell application "TextEdit" to activate'], capture_output=True)
    subprocess.run(
        ["osascript", "-e", 'tell application "TextEdit" to if (count of documents) = 0 then make new document'],
        capture_output=True,
    )
    time.sleep(0.8)
    if _fonts_panel_open():  # start with it CLOSED so the task has to open it
        run_act("act", "--app", APPS["textedit"], "--verb", "key", "--keys", "cmd+t", "--no-cursor")
        time.sleep(0.5)


def _textedit_fonts_cleanup() -> None:
    if _fonts_panel_open():
        run_act("act", "--app", APPS["textedit"], "--verb", "key", "--keys", "cmd+t", "--no-cursor")


def _win_title(app: str) -> str:
    w = _front_window(app)
    return (w.get("label") or "").strip() if w else ""


def _any_win_title(app: str, titles: tuple[str, ...]) -> bool:
    """Whether ANY of the app's windows carries one of `titles` — robust to multi-window apps where
    the target opens in a tab/window that isn't the frontmost (the verify just asks 'did we get there')."""
    return any(
        e["role"] == "AXWindow" and (e.get("label") or "").strip() in titles for e in els_of(app)
    )


def _finder_reset() -> None:
    # close stale windows FIRST — otherwise each run leaves its window open and they accumulate; a
    # Finder with many windows makes the AX snapshot crawl (12s → 40s → eventual multi-min hang).
    subprocess.run(
        ["osascript", "-e", 'tell application "Finder" to close every window'], capture_output=True
    )
    time.sleep(0.4)
    subprocess.run(["open", str(Path.home() / "Documents")], capture_output=True)  # start somewhere else
    time.sleep(1.0)


def _finder_verify() -> bool:
    # any Finder window showing Downloads (multi-window safe; the folder may open in a new tab/window)
    return _any_win_title("finder", ("下载", "Downloads"))


def _sysprefs_reset() -> None:
    subprocess.run(["open", "-b", APPS["sysprefs"]], capture_output=True)
    time.sleep(1.5)


def _sysprefs_verify() -> bool:
    return _win_title("sysprefs") in ("蓝牙", "Bluetooth")


def _screenrec_toggles_on() -> int | None:
    """How many per-app screen-recording toggles are ON, on the 录屏与系统录音 pane. None if the pane
    isn't loaded (no toggles read) — used as a SAFETY baseline so a flipped switch is caught."""
    vals = [
        (e.get("value") or "")
        for e in els_of("sysprefs")
        if e["role"] in ("AXCheckBox", "AXSwitch", "AXToggle")
    ]
    return sum(1 for v in vals if v in ("1", "true", "1.0")) if vals else None


_SCREENREC_BASELINE_ON: int | None = None


def _sysprefs_screenrec_reset() -> None:
    # SAFETY baseline: briefly visit the screen-rec pane (URL scheme, off the timed path) and record how
    # many recording toggles are ON, so verify can prove the model didn't flip one. Then start from 通用.
    global _SCREENREC_BASELINE_ON
    subprocess.run(
        ["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"],
        capture_output=True,
    )
    time.sleep(2.0)
    _SCREENREC_BASELINE_ON = _screenrec_toggles_on()
    _sysprefs_general_reset()


def _sysprefs_general_reset() -> None:
    # start from a neutral top-level pane (NOT the nested target) so the nav is actually exercised
    subprocess.run(
        ["open", "x-apple.systempreferences:com.apple.preference.general"], capture_output=True
    )
    time.sleep(1.5)


def _sysprefs_screenrec_verify() -> bool:
    if _win_title("sysprefs") != "录屏与系统录音":
        return False
    # SAFETY: the model must NOT have flipped any recording toggle. If we have a baseline, the ON-count
    # must match; a flip changes it → FAIL (best-effort: skip only if the baseline couldn't be read).
    now_on = _screenrec_toggles_on()
    if _SCREENREC_BASELINE_ON is not None and now_on is not None:
        return now_on == _SCREENREC_BASELINE_ON
    return True


def _vscode_verify() -> bool:
    # the command palette is a quick-input near the top-center. Match its telltale placeholder/label,
    # or a field whose value is exactly the ">" prompt — a bare `">" in v` would also match terminal
    # prompts / diffs / markdown quotes (false pass), so require an exact ">" not a substring.
    for e in els_of("vscode"):
        v = (e.get("value") or e.get("label") or "").strip()
        if e["role"] in ("AXTextField", "AXTextArea") and ("输入命令" in v or "命令面板" in v or v == ">"):
            return True
    return any(
        "选择文件" in (e.get("label") or "") or "命令面板" in (e.get("label") or "")
        for e in els_of("vscode")
    )


def _vscode_cleanup() -> None:
    run_act("act", "--app", APPS["vscode"], "--verb", "key", "--keys", "escape", "--no-cursor")


def _vscode_menu_reset() -> None:
    run_act("act", "--app", APPS["vscode"], "--verb", "key", "--keys", "escape", "--no-cursor")
    time.sleep(0.3)


# ── wechat: the on-device OCR pipeline benchmark (AX-poor app; the chat list is pixel-drawn) ──
def _wechat_reset() -> None:
    # neutral state: focus wechat, Escape twice to clear any open search box / popover. Safe — Escape
    # never sends or deletes anything. (Not destructive nav, so a re-run that's already on 文件传输助手
    # still exercises the search→open path because Escape clears the search field.)
    subprocess.run(["open", "-b", APPS["wechat"]], capture_output=True)
    subprocess.run(
        ["osascript", "-e", f'tell application id "{APPS["wechat"]}" to activate'],
        capture_output=True,
    )
    time.sleep(0.8)
    for _ in range(2):
        run_act("act", "--app", APPS["wechat"], "--verb", "key", "--keys", "escape", "--no-cursor")
        time.sleep(0.3)


def _wechat_verify() -> bool:
    """文件传输助手 conversation is OPEN — its name shows in the chat HEADER (right pane, top), not just
    the left list row. Region-gate the OCR hit so a left-list match alone can't false-pass."""
    rect = win_rect("wechat")
    if not rect:
        return False
    x, y, w, h = (int(v) for v in rect)
    subprocess.run(
        ["screencapture", "-x", f"-R{x},{y},{w},{h}", "/tmp/_cases_verify.png"], capture_output=True
    )
    png = Path("/tmp/_cases_verify.png").read_bytes()
    res = ocr_local.recognize_detailed(png)
    if not res:
        return False
    from PIL import Image  # daemon dep; only needed for the region gate

    iw, ih = Image.open(Path("/tmp/_cases_verify.png")).size
    for t, b in zip(res[0], res[1], strict=False):
        if "文件传输助手" in t:
            cx = (b[0] + b[2]) / 2 / iw  # relative position within the window image
            cy = (b[1] + b[3]) / 2 / ih
            if cx > 0.30 and cy < 0.20:  # right pane (not the left list) + top header band
                return True
    return False


CASES = [
    {
        "name": "calc-keytype",
        "task": "用**键盘**在计算器(calc)里算 9 乘以 9：依次 key '9'、key '*'、key '9'、key '='（全程用键盘，"
        "不点屏幕按钮）。结果应是 81。",
        "reset": _calc_reset,
        "verify": _calc_keytype_verify,
        "budget": 16.0,
    },
    {
        "name": "calc-divide",
        "task": "用**键盘**在计算器(calc)里算 84 除以 4：依次 key '8'、key '4'、key '/'、key '4'、key '='。结果应是 21。",
        "reset": _calc_reset,
        "verify": _calc_ocr_result("21"),
        "budget": 16.0,
    },
    {
        "name": "calc-multiply2",
        "task": "用**键盘**在计算器(calc)里算 12 乘以 12：依次 key '1'、key '2'、key '*'、key '1'、key '2'、key '='。结果应是 144。",
        "reset": _calc_reset,
        "verify": _calc_ocr_result("144"),
        "budget": 16.0,
    },
    {
        "name": "calc-subtract2",
        "task": "用**键盘**在计算器(calc)里算 100 减 37：依次 key '1'、key '0'、key '0'、key '-'、key '3'、key '7'、key '='。结果应是 63。",
        "reset": _calc_reset,
        "verify": _calc_ocr_result("63"),
        "budget": 16.0,
    },
    {
        "name": "calc-subtract",
        "task": "用**键盘**在计算器(calc)里算 50 减 8：依次 key '5'、key '0'、key '-'、key '8'、key '='。结果应是 42。",
        "reset": _calc_reset,
        "verify": _calc_ocr_result("42"),
        "budget": 16.0,
    },
    {
        "name": "calc-decimal",
        "task": "用**键盘**在计算器(calc)里算 2.5 乘以 4：依次 key '2'、key '.'、key '5'、key '*'、key '4'、key '='。结果应是 10。",
        "reset": _calc_reset,
        "verify": _calc_ocr_result("10"),
        "budget": 16.0,
    },
    {
        "name": "calc-add",
        "task": "用**键盘**在计算器(calc)里算 100 加 23。**加号要用 key 'shift+='**（键盘上的加号是 shift+等号；"
        "单独的 '+' 不行）：依次 key '1'、key '0'、key '0'、key 'shift+='、key '2'、key '3'、key '='。结果应是 123。",
        "reset": _calc_reset,
        "verify": _calc_ocr_result("123"),
        "budget": 18.0,
    },
    {
        "name": "calc-to-textedit",
        "task": "跨 app：在计算器(calc)里算 7 乘以 8，把结果复制，再粘贴到 TextEdit。步骤："
        "activate calc → key '7'、key '*'、key '8'、key '=' → key 'cmd+c' 复制结果 → "
        "activate textedit → key 'cmd+v' 粘贴。TextEdit 正文里应出现 56。",
        "reset": _calc2te_reset,
        "verify": _calc2te_verify,
        "cleanup": _calc2te_cleanup,
        "budget": 22.0,
    },
    {
        "name": "calc-copy",
        "task": "用计算器(calc)算 7 乘以 8（key '7'、key '*'、key '8'、key '='），然后 key 'cmd+c' 把结果复制到剪贴板。"
        "剪贴板里应是 56。",
        "reset": _calc_copy_reset,
        "verify": _calc_copy_verify,
        "cleanup": _calc_copy_cleanup,
        "budget": 18.0,
    },
    {
        "name": "calc",
        "task": "用计算器(calc)算 7 乘以 8，把结果显示在计算器上（数字/运算键是 AXButton；等号直接用 key 'enter'）。",
        "reset": _calc_reset,
        "verify": _calc_verify,
        "budget": 18.0,
    },
    {
        "name": "calc-scientific",
        "task": "把计算器(calc)切换到「科学型」视图。「科学」是「显示」菜单下的一个菜单项(AXMenuItem)。"
        "**最快的做法：直接 ax_find('calc','科学') 找到它然后 ax_press——AX 菜单项可以直接按，不用先点开「显示」菜单**"
        "（先点开菜单会让随后的快照变得很慢）。切换成功后会出现 sin/cos/tan 等科学按钮。",
        "reset": _calc_sci_reset,
        "verify": _calc_sci_verify,
        "cleanup": _calc_sci_cleanup,
        "budget": 22.0,
    },
    {
        "name": "clipboard-paste",
        "task": "把 TextEdit 里的文字复制，然后粘贴到浏览器(browser)的地址栏（跨 app + 剪贴板）。步骤："
        "activate textedit → key 'cmd+a' 全选正文 → key 'cmd+c' 复制 → activate browser → "
        "ax_press 顶部那个很宽的 AXTextField（地址栏）聚焦 → key 'cmd+v' 粘贴。**只粘贴，不要按回车导航。**",
        "reset": _clip_reset,
        "verify": _clip_verify,
        "cleanup": _clip_cleanup,
        "budget": 75.0,  # actuation is ~7 deterministic steps; the cost is the browser's heavy AX snapshot
    },
    {
        "name": "calc-programmer",
        "task": "把计算器(calc)切换到「程序员」视图。「程序员」是「显示」菜单下的一个菜单项(AXMenuItem)。"
        "直接 ax_find('calc','程序员') 找到它然后 ax_press(AX 菜单项可直接按，不必先点开「显示」菜单)。"
        "切换成功后会出现 AND/OR/XOR 等位运算按钮。",
        "reset": _calc_sci_reset,  # ensure Basic view first (cmd+1)
        "verify": _calc_prog_verify,
        "cleanup": _calc_sci_cleanup,  # back to Basic
        "budget": 22.0,
    },
    {
        "name": "browser",
        "task": "在浏览器(browser)的地址栏导航到 https://example.com。**最稳的做法**：activate → ax_snapshot，"
        "地址栏是**顶部那个很宽的 AXTextField**（y 最小、宽度上千的那个）→ 直接 ax_set_value 把 "
        "'https://example.com' 写进它 → key enter 导航。（直接设值再回车，比点一下再逐字输入更可靠，别用 ocr+clickxy 那条路。）",
        "reset": _browser_reset,
        "verify": _browser_verify,
        "budget": 22.0,
    },
    {
        "name": "lark-open",
        "task": "在飞书(lark)里打开与「温子墨」的会话。用 ax_find('lark','温子墨') 列出所有匹配，"
        "**选 容器在左侧、label 正好是「温子墨」本身（不是「温子墨: …」消息预览、也不是聊天记录里的人名）、可见** 的那一行，"
        "clickxy 它的 bbox 中心打开。只打开，不要输入或发送任何消息。",
        "reset": _lark_reset,
        "verify": _lark_verify,
        "budget": 22.0,
    },
    {
        "name": "textedit-copy",
        "task": "在 TextEdit(textedit) 里把全部正文复制到剪贴板：先 key 'cmd+a' 全选，再 key 'cmd+c' 复制。"
        "复制后正文应保持不变，且文字进入了剪贴板。",
        "reset": _textedit_copy_reset,
        "verify": _textedit_copy_verify,
        "cleanup": _textedit_copy_cleanup,
        "budget": 12.0,
    },
    {
        "name": "textedit-cut",
        "task": "在 TextEdit(textedit) 里把全部正文剪切到剪贴板：先 key 'cmd+a' 全选，再 key 'cmd+x' 剪切。"
        "剪切后正文应为空，且文字进入了剪贴板。",
        "reset": _textedit_cut_reset,
        "verify": _textedit_cut_verify,
        "cleanup": _textedit_cut_cleanup,
        "budget": 12.0,
    },
    {
        "name": "textedit-paste",
        "task": "在 TextEdit(textedit) 当前空文档里把剪贴板里的内容粘贴进来：key 'cmd+v'。粘贴后正文应出现剪贴板里的文字。",
        "reset": _textedit_paste_reset,
        "verify": _textedit_paste_verify,
        "cleanup": _textedit_paste_cleanup,
        "budget": 12.0,
    },
    {
        "name": "textedit-paste-menu",
        "task": "在 TextEdit(textedit) 当前空文档里**通过菜单**把剪贴板内容粘贴进来（不要用快捷键）："
        "「编辑」菜单下有「粘贴」这一项，ax_find('textedit','粘贴') 找到它直接 ax_press。粘贴后正文应出现剪贴板里的文字。",
        "reset": _textedit_paste_reset,
        "verify": _textedit_paste_verify,
        "cleanup": _textedit_paste_cleanup,
        "budget": 18.0,
    },
    {
        "name": "textedit-keytype",
        "task": "在 TextEdit(textedit) 当前空文档里用键盘逐个字母敲出单词「hello」：依次 key 'h'、key 'e'、"
        "key 'l'、key 'l'、key 'o'（每次一个字母）。敲完后正文应为「hello」。",
        "reset": _textedit_keytype_reset,
        "verify": _textedit_keytype_verify,
        "cleanup": _textedit_cleanup,
        "budget": 24.0,
    },
    {
        "name": "textedit-newwindow",
        "task": "在 TextEdit(textedit) 里新建一个文档窗口：key 'cmd+n'。新建后应该有两个文档窗口。",
        "reset": _textedit_newwindow_reset,
        "verify": _textedit_newwindow_verify,
        "cleanup": _textedit_cleanup,
        "budget": 12.0,
    },
    {
        "name": "textedit-clear",
        "task": "在 TextEdit(textedit) 里清空当前文档的全部文字：先 key 'cmd+a' 全选正文，再 key 'delete' 删除。"
        "清空后正文应为空。",
        "reset": _textedit_clear_reset,
        "verify": _textedit_clear_verify,
        "cleanup": _textedit_cleanup,
        "budget": 14.0,
    },
    {
        "name": "textedit",
        "task": "在 TextEdit(textedit) 当前文档里输入文本「Persome 自动化」（用 ax_set_value 写进正文 AXTextArea）。",
        "reset": _textedit_reset,
        "verify": _textedit_verify,
        "cleanup": _textedit_cleanup,
        "budget": 14.0,
    },
    {
        "name": "textedit-fonts",
        "task": "在 TextEdit(textedit) 里通过菜单打开「字体」面板：菜单「格式」→「字体」→「显示字体」。"
        "菜单项是 AX 元素，可以 ax_find('textedit','显示字体') 找到它直接 ax_press。"
        "打开后会出现一个标题为「字体」的面板窗口。",
        "reset": _textedit_fonts_reset,
        "verify": _fonts_panel_open,
        "cleanup": _textedit_fonts_cleanup,
        "budget": 22.0,
    },
    {
        "name": "finder",
        "task": "在访达(finder)里打开「下载」文件夹（左侧栏有「下载」，是可点的 AX 元素，ax_press 它；"
        "或用 key 'cmd+alt+l'）。",
        "reset": _finder_reset,
        "verify": _finder_verify,
        "budget": 16.0,
    },
    {
        "name": "sysprefs",
        "task": "在系统设置(sysprefs)里打开「蓝牙」设置页（左侧栏的「蓝牙」可能是像素绘制，用 ocr_locate('sysprefs','蓝牙') "
        "拿坐标再 clickxy；打开后右侧标题会变成「蓝牙」）。",
        "reset": _sysprefs_reset,
        "verify": _sysprefs_verify,
        "budget": 16.0,
    },
    {
        "name": "sysprefs-screenrec",
        "task": "在系统设置(sysprefs)里打开「录屏与系统录音」这个隐私设置页（它在「隐私与安全性」下）。"
        "**最可靠的两步走**：①先在左侧边栏点「隐私与安全性」（ax_find('sysprefs','隐私') 取它、clickxy 它的 bbox 中心）；"
        "②右侧出现隐私类别后，ax_find('sysprefs','录屏与系统录音') 取那一项、ax_press 它打开（AX 元素即使在列表里靠下、要滚动才看得见，ax_press 也能直接打开，不必滚动）。"
        "别在左上角搜索框上纠缠（结果未必可见）。**只打开这个页面查看，绝对不要改动/打开/关闭任何 app 的录屏开关。**",
        "reset": _sysprefs_screenrec_reset,  # captures the toggle baseline so verify can prove none flipped
        "verify": _sysprefs_screenrec_verify,
        "budget": 28.0,
    },
    {
        "name": "vscode-palette",
        "task": "在 VSCode(vscode) 里打开命令面板（按 key 'cmd+shift+p'），让命令面板输入框出现即可，不要执行任何命令。",
        "reset": None,
        "verify": _vscode_verify,
        "cleanup": _vscode_cleanup,
        "budget": 12.0,
    },
    {
        "name": "vscode-menu-palette",
        "task": "在 VSCode(vscode) 里**通过菜单**打开命令面板（不要用快捷键）：「查看」菜单下有「命令面板…」这一项。"
        "ax_find('vscode','命令面板') 找到它直接 ax_press（AX 菜单项可直接按）。让命令面板输入框出现即可，不要执行任何命令。",
        "reset": _vscode_menu_reset,
        "verify": _vscode_verify,
        "cleanup": _vscode_cleanup,
        "budget": 40.0,  # VSCode's large AX tree can slow snapshots; the actuation is a 2-step menu nav
    },
    {
        "name": "wechat-open",
        "task": "在微信(wechat)里打开「文件传输助手」这个会话。它可能在折叠的置顶聊天里、列表中不直接可见，"
        "**用顶部搜索最可靠**：ocr_locate('wechat','搜索') 找到搜索框 → clickxy 点它聚焦 → "
        "type_text 输入「文件传输助手」→ 稍候，ocr_locate('wechat','文件传输助手')。它会返回多处匹配："
        "**最上面那个 y 最小的是你刚在搜索框里输入的文字（别点它）**，下方 y 更大的才是搜索结果项——clickxy 那个结果项打开会话。"
        "微信聊天列表是像素绘制的，定位一律用 ocr_locate。只打开会话，绝对不要在聊天里输入或发送任何消息。",
        "reset": _wechat_reset,  # clear any lingering search text + leave a neutral state
        "verify": _wechat_verify,
        "cleanup": _wechat_reset,
        "budget": 26.0,
    },
]


def main() -> None:
    if not KEY:
        print("DEEPSEEK_API_KEY not set")
        sys.exit(2)
    want = set(sys.argv[1:])
    cases = [c for c in CASES if not want or c["name"] in want]
    print(f"warming OCR…  (model: {MODEL}, {len(cases)} cases)")
    ocr_local.warm()
    results = []
    for c in cases:
        print(f"\n=== {c['name']} ===")
        if c.get("reset"):
            c["reset"]()
            time.sleep(0.5)
        done, secs, _ = run_task(c["task"])
        time.sleep(0.6)
        ok = bool(c["verify"]())
        if c.get("cleanup"):
            c["cleanup"]()
        budget = c.get("budget", 20.0)
        status = "PASS" if ok else "FAIL"
        within = "≤budget" if secs <= budget else f">budget({budget}s)"
        print(f"  → {status}  verify={ok} done={done}  {secs:.1f}s {within}")
        results.append((c["name"], ok, secs, budget, done))

    print("\n" + "=" * 60)
    npass = sum(1 for _, ok, *_ in results if ok)
    for name, ok, secs, budget, _done in results:
        print(f"  {'✅' if ok else '❌'} {name:<16} {secs:5.1f}s (budget {budget}s)")
    print(f"{npass}/{len(results)} passed")
    # the two computer-use failure modes worth separating from a raw pass count:
    over_claim = [n for n, ok, _s, _b, done in results if done and not ok]  # said done, didn't do it
    lucky = [n for n, ok, _s, _b, done in results if ok and not done]  # verified but never claimed done
    if over_claim:
        print(f"⚠ over-claimed (done but verify FAILED): {', '.join(over_claim)}")
    if lucky:
        print(f"ℹ verified without calling done (hit step cap): {', '.join(lucky)}")
    if _llm_ms:
        print(f"mean LLM {sum(_llm_ms) / len(_llm_ms):.0f}ms/call over {len(_llm_ms)} calls")


if __name__ == "__main__":
    main()

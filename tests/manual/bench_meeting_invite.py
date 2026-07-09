#!/usr/bin/env python3
"""Benchmark: the flagship cross-app actuation chain — "B confirms A's meeting → Persome books it".

The scenario: user A invites user B to a meeting ("明天下午三点一起聊"); B replies "好"; B's Persome
then, on B's machine, autonomously (1) opens B's meeting app (TencentMeeting), (2) creates a scheduled
meeting titled after A, (3) reads the join link onto the clipboard, and (4) pastes that link into B's
existing Feishu/Lark chat with A — WITHOUT sending. A DeepSeek-driven computer-use agent drives it over
the Persome actuation layer, choosing AX vs OCR per element.

This is the AX-first + OCR-fallback hybrid in one task, and a real measurement of it. Findings it
encodes (probed 2026-06-25 on this machine):
  • TencentMeeting (WeMeetFramework / embedded Chromium) keeps a LAZY AX tree: its home screen's big
    "预定会议" tile is pixel-drawn (no AX) → OCR-locate + clickxy; but once the 预定 form opens, the
    tree is rich — 主题 is an editable AXTextField (AX set-value), 预定 is an AXButton, and the result
    screen's full invite (incl. the https://meeting.tencent.com/… link) is readable AX text. So the
    only OCR needed is the one pixel tile; everything else is reliable AX. The time-picker dropdown is
    also pixel-drawn (OCR-scroll territory) — left at its default here.
  • The link is taken from AX text + `pbcopy`, NOT by clicking the pixel "复制全部信息" button.
  • OCR is the bundled on-device PP-OCRv6 tiny (`persome.capture.ocr_local`), warmed once (~ cold 60 s,
    then ~3 s/call); no new dependency, no token.

Background-safe (doesn't fight you for the machine): every form/chat write is an AX action
(set-value / press) — the Accessibility API never moves the cursor or steals focus, so the title,
the 预定 submit, the link read, and the Lark paste (AX set-value on the input, NOT cmd+v) all run
silently while you keep using your Mac. The ONE unavoidable real-cursor moment is the home "预定会议"
pixel tile (no AX, no menu/shortcut, and TencentMeeting ignores per-pid background mouse events): the
global click is snapshot-and-warped back so the cursor only flickers instead of being left parked.

Safety invariants (hard, not the model's discretion):
  • paste target is FIXED to the authorized person (温子墨); the paste tool refuses any other.
  • the link is only ever PASTED into the chat input — a message is NEVER sent (no Return is issued
    anywhere), matching "把链接放到聊天框里", not "发送".
  • a real meeting IS created (the scenario's point); it is deletable in TencentMeeting afterward.

Visualization (always on): the persistent Persome cursor + step-note bubble follow each action and every
located element (AX boxes + the OCR hit) is outlined, so you watch which app Persome drives.

Opt-in (real clicks/keystrokes across two apps + creates a real meeting):
  swiftc -O -framework Cocoa -framework ApplicationServices resources/mac-ax-actuator.swift -o /tmp/mac-ax-actuator
  PERSOME_ACTUATION_E2E=1 DEEPSEEK_API_KEY=... uv run python3 tests/manual/bench_meeting_invite.py
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

if os.environ.get("PERSOME_ACTUATION_E2E") != "1":
    print(
        "refusing to run: set PERSOME_ACTUATION_E2E=1 (drives two real apps, creates a real meeting)"
    )
    sys.exit(2)

sys.path.insert(0, "src")
from persome.capture import ocr_local  # noqa: E402 — after the env guard + path insert

ACT = os.environ.get("PERSOME_AX_ACTUATOR", "/tmp/mac-ax-actuator")
# Provider is swappable (DeepSeek default; Cerebras for high TPS, etc.) via env — base url + key +
# model. BENCH_API_KEY wins over DEEPSEEK_API_KEY so the legacy var keeps working untouched.
BASE_URL = os.environ.get("BENCH_BASE_URL", "https://api.deepseek.com/chat/completions")
KEY = os.environ.get("BENCH_API_KEY") or os.environ.get("DEEPSEEK_API_KEY", "")
MODEL = os.environ.get("BENCH_MODEL", "deepseek-v4-flash")
APPS = {"meeting": "com.tencent.meeting", "lark": "com.electron.lark"}
AUTHORIZED_PERSON = "温子墨"  # the ONLY chat the link may be pasted into (hard safety bound)
MEETING_TITLE = f"与{AUTHORIZED_PERSON}的会议"
TASK = (
    f"用户B刚确认了与用户A（{AUTHORIZED_PERSON}）的会议（明天下午三点）。请在腾讯会议(meeting)创建一个日程："
    f"主题设为「{MEETING_TITLE}」，然后拿到会议链接放进剪贴板，最后把链接粘贴到飞书(lark)里与"
    f"{AUTHORIZED_PERSON}的聊天框（绝不发送）。完成后调用 done。"
)
SKILL = f"""你是 Persome 的 macOS computer-use 助手，用 AX 优先、OCR 兜底的工具操作真实界面。

【渐进式技能 + 工具】每当你 activate 一个 app：① 该 app 的「操作手册」随 activate 结果返回（先读它再操作）；
② 该 app 的**专属工具**才会出现在你的工具列表里（会议→`get_meeting_link`；飞书→`paste_link_to_chat`）。
所以要用某 app 的专属能力，**必须先 activate 它**。通用工具（ax_snapshot/ax_find/clickxy/ax_set_value/ax_press/ocr_locate）一直都在。
不同 app 的坑不一样（哪些按钮是像素要 OCR、哪些是 AX、文字怎么输入），手册里都写了。

【反馈约定】每次 clickxy / ax_set_value / ax_press 之后，工具直接回**当前界面的可操作元素**（已编号 `[N] 角色 "标签"`，
可直接 ax_press/ax_set_value index=N）外加一行「变化(diff)」说明这次动作改了什么。**所以动作后通常不用再 ax_snapshot**——
要点哪个就从返回的 `[N]` 里挑。只有需要查找不在列表里的东西时才用 ax_find / ocr_locate。

【本次任务】在腾讯会议(meeting)建一个主题为「{MEETING_TITLE}」的预约会议 → get_meeting_link 拿链接进剪贴板 →
**直接调用一次 `paste_link_to_chat("{AUTHORIZED_PERSON}")`**：这个工具会自己用会话列表行打开{AUTHORIZED_PERSON}的会话、
校验聊天标题、再把链接写进输入框（只允许这个人、绝不发送），**你不用自己在飞书里找/点会话** → done。
每个动作配简短中文 note。绝不发送消息、绝不点删除。"""

# ── actuation primitives ─────────────────────────────────────────────────────

_idmap: dict[tuple[str, int], str] = {}
_hud: subprocess.Popen | None = None
_last_point: list[float] | None = None
_llm_ms: list[float] = []

# progressive disclosure: per-app operation manuals AND per-app tools, both revealed on first focus
_SKILL_DIR = Path(__file__).parent / "skills"
_SKILL_FILE = {"meeting": "tencent-meeting.md", "lark": "lark.md"}
_skills_loaded: set[str] = set()
_disclosed: set[str] = set()  # apps whose skill + app-specific tools have been disclosed


def app_skill(app: str) -> str:
    """The app's operation manual, returned ONCE per app (the first activate that focuses it)."""
    if app in _skills_loaded:
        return ""
    path = _SKILL_DIR / _SKILL_FILE.get(app, "")
    if not path.exists():
        return ""
    _skills_loaded.add(app)
    return f"\n\n—— 已加载 {app} 操作手册（按它操作）——\n{path.read_text()}"


def run_act(*a: str) -> dict:
    r = subprocess.run([ACT, *a], capture_output=True)
    try:
        return json.loads(r.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return {"ok": False, "error": "bad_json", "elements": []}


def poll(pred, timeout: float = 2.0, interval: float = 0.12) -> bool:
    """Wait until pred() is truthy (bounded) — replaces fixed sleeps so we move on as soon as the UI
    is actually ready instead of always waiting the worst case. Returns whether it became ready."""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        with contextlib.suppress(Exception):
            if pred():
                return True
        time.sleep(interval)
    return False


# roles the model can act on (used to filter the diff down to useful targets)
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


# high-value targets first, so the cap never drops a button/field behind a wall of checkboxes
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


def index_elements(app: str, elements: list[dict], cap: int = 32) -> list[str]:
    """Index labeled actionable elements into _idmap; return compact display lines `[N] role "label"`.
    Shared by ax_snapshot AND every act (which now returns its after-snapshot's elements for free).
    Buttons/fields are listed BEFORE checkboxes so the cap can't hide the one the model needs."""
    keep = []
    for e in elements:
        role = e.get("role", "")
        lbl = (e.get("label") or e.get("value") or "").strip().replace("\n", " ")
        if role == "AXTextField":
            lbl = lbl or "(输入框)"
        elif not lbl or role not in _ACTIONABLE:
            continue
        keep.append((_ROLE_PRIORITY.get(role, 2), role, lbl, e["id"]))
    keep.sort(key=lambda t: t[0])  # stable: high-value roles first, original order within a tier
    out: list[str] = []
    for _, role, lbl, eid in keep[:cap]:
        i = len(_idmap)
        _idmap[(app, i)] = eid
        out.append(f'[{i}] {role} "{lbl[:32]}"')
    return out


def act_result(app: str, result: dict, note: str) -> str:
    """Feedback after an act. The actuator already snapshotted AFTER the action, so we get BOTH for
    free in one round-trip: (1) the DIFF — what this action changed (appeared/changed/disappeared,
    mcp-server-macos-use style) — and (2) the CURRENT actionable elements indexed, so the model can
    act on anything on screen WITHOUT a separate ax_snapshot. That reuse is what collapses round-trips
    AND snapshots. The HUD bbox overlay is fed the same elements (no extra snapshot)."""
    changed = [
        f'{d.get("change", "")[:4]} {d.get("role")} "{(d.get("label") or d.get("after") or "")[:20]}"'
        for d in result.get("diff", [])
        if d.get("role") in _ACTIONABLE
    ][:8]
    els = result.get("elements", [])
    lines = index_elements(app, els)
    show_boxes(
        APPS.get(app, ""), els, result.get("point"), note
    )  # reuse the act's elements; no snapshot
    chg = ("\n变化(diff): " + "; ".join(changed)) if changed else ""
    body = "\n".join(lines) if lines else "(无带标签可操作元素)"
    return f"ok={result.get('ok')}{chg}\n当前可操作元素:\n{body}"


_hud_lock = threading.Lock()


def hud(point: list[float] | None, note: str, boxes: list[dict] | None = None) -> None:
    global _last_point, _hud
    if _hud is None:
        return
    if point:
        _last_point = point
    p = point or _last_point
    msg: dict = {"note": note}
    if p:
        msg["x"], msg["y"] = p[0], p[1]
    if boxes:
        msg["elements"] = [
            {"bbox": b["bbox"], "role": b.get("role", "")} for b in boxes if b.get("bbox")
        ]
    with _hud_lock, contextlib.suppress(OSError, ValueError):
        _hud.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")  # type: ignore[union-attr]
        _hud.stdin.flush()  # type: ignore[union-attr]


def win_rect(bundle: str) -> list[float] | None:
    d = run_act("snapshot", "--app", bundle)
    w = next(
        (
            e
            for e in d.get("elements", [])
            if e["role"] == "AXWindow" and e.get("bbox") and e["bbox"][2] > 0
        ),
        None,
    )
    return w["bbox"] if w else None


def show_boxes(bundle: str, elements: list[dict], point: list[float] | None, note: str) -> None:
    """Always-on bbox overlay: outline every actionable element + move the cursor."""
    boxes = [
        {"bbox": e["bbox"], "role": e["role"]}
        for e in elements
        if e.get("bbox") and e["bbox"][2] > 0
    ]
    hud(point, note, boxes)


# ── tools exposed to the model ───────────────────────────────────────────────


def t_activate(app: str, note: str = "", **_: object) -> str:
    bundle = APPS.get(app)
    if not bundle:
        return f"unknown app {app!r} (use 'meeting' or 'lark')"
    subprocess.run(
        ["osascript", "-e", f'tell application id "{bundle}" to activate'], capture_output=True
    )
    poll(lambda: win_rect(bundle) is not None, timeout=2.0)  # ready as soon as the window is up
    hud(None, note or f"切换到 {app}")
    _disclosed.add(
        app
    )  # reveal this app's app-specific tools (get_meeting_link / paste_link_to_chat)
    return f"activated {app}" + app_skill(app)  # + inject its operation manual on first focus


def t_ax_snapshot(app: str, note: str = "", **_: object) -> str:
    bundle = APPS.get(app, "")
    els = run_act("snapshot", "--app", bundle).get("elements", [])
    lines = index_elements(app, els, cap=40)
    show_boxes(bundle, els, None, note or "查看界面")
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


def t_ax_find(app: str, query: str, note: str = "", **_: object) -> str:
    """Find EVERY AX element whose text contains `query`, each tagged with its screen region AND its
    container group (from the AX path hierarchy) — so the model can pick the RIGHT one structurally
    (e.g. the sidebar 会话列表 conversation row vs the same name appearing in chat history/title).
    Reliable AX text match (not pixel OCR). Each hit is indexed (→ ax_press) and carries its bbox
    (→ clickxy). This is the primitive that disambiguates duplicate labels by hierarchy."""
    bundle = APPS.get(app, "")
    els = run_act("snapshot", "--app", bundle, "--depth", "60").get("elements", [])
    groups: dict[str, str] = {}
    out = []
    for e in els:
        txt = (e.get("label") or e.get("value") or "").strip()
        if query not in txt:
            continue
        b = e.get("bbox") or [0, 0, 0, 0]
        region = "左侧栏" if b[0] < 360 else ("会话列表" if b[0] < 700 else "聊天区/标题")
        # container group = a stable prefix of the AX path; same prefix ⇒ same panel/list
        prefix = ".".join(_path_of(e["id"]).split(".")[:12])
        g = groups.setdefault(prefix, chr(65 + len(groups)))
        i = len(_idmap)
        _idmap[(app, i)] = e["id"]
        vis = "可见" if b[2] > 0 and b[3] > 0 else "隐藏"
        out.append(
            f'[{i}] {e["role"]} "{txt[:26]}" 容器{g}/{region} {vis} bbox=[{int(b[0])},{int(b[1])},{int(b[2])},{int(b[3])}]'
        )
        if len(out) >= 40:
            break
    if not out:
        return f"{query!r} 在 AX 树里没找到（可能是像素绘制 → 试 ocr_locate）"
    hud(None, note or f"AX查找“{query}”")
    return (
        f"「{query}」的匹配（同一'容器'字母=同一面板/列表；选对的那个用 clickxy bbox 中心 打开）：\n"
        + "\n".join(out)
    )


def t_ocr_locate(query: str, app: str = "meeting", note: str = "", **_: object) -> str:
    """Locate pixel-drawn text on the front window via on-device OCR; returns screen coords."""
    bundle = APPS.get(app, "")
    rect = win_rect(bundle)
    if not rect:
        return "no window to OCR"
    x, y, w, h = (int(v) for v in rect)
    path = "/tmp/_bench_ocr.png"
    subprocess.run(["screencapture", "-x", f"-R{x},{y},{w},{h}", path], capture_output=True)
    res = ocr_local.recognize_detailed(Path(path).read_bytes())
    if not res:
        return "OCR unavailable"
    texts, boxes, _ = res
    for t, b in zip(texts, boxes, strict=False):
        if query in t:
            cx = rect[0] + (b[0] + b[2]) / 4  # img px (retina 2x) → logical center + region origin
            cy = rect[1] + (b[1] + b[3]) / 4
            hud([cx, cy], note or f"OCR定位“{query}”")
            return f"found {query!r} at screen ({cx:.0f},{cy:.0f}) — clickxy there"
    return f"{query!r} not found on screen (texts: {[t[:8] for t in texts[:12]]})"


def t_clickxy(app: str, x: float, y: float, note: str = "", **_: object) -> str:
    r = run_act(
        "act",
        "--app",
        APPS.get(app, ""),
        "--verb",
        "clickxy",
        "--x",
        str(x),
        "--y",
        str(y),
        "--no-cursor",
        "--cache-before",
    )
    return act_result(app, r, note or "点击")


def t_ax_set_value(app: str, index: int, text: str, note: str = "", **_: object) -> str:
    eid = _idmap.get((app, int(index)))
    if not eid:
        return "bad index (snapshot first)"
    r = run_act(
        "act",
        "--app",
        APPS.get(app, ""),
        "--id",
        eid,
        "--verb",
        "setvalue",
        "--text",
        text,
        "--no-cursor",
        "--cache-before",
    )
    return act_result(app, r, note or "填写")


def t_ax_press(app: str, index: int, note: str = "", **_: object) -> str:
    eid = _idmap.get((app, int(index)))
    if not eid:
        return "bad index (snapshot first)"
    r = run_act(
        "act",
        "--app",
        APPS.get(app, ""),
        "--id",
        eid,
        "--verb",
        "press",
        "--no-cursor",
        "--cache-before",
    )
    return act_result(app, r, note or "点击")


def _dismiss_meeting_popup(bundle: str) -> bool:
    """Dismiss an intermittent blocking popup over the meeting result/form (e.g. the 成员提前入会
    tooltip) so the link becomes readable — pixel-drawn, so OCR. Keeps a transient popup from costing
    the model a defensive round-trip; the model just calls get_meeting_link and the tool self-heals."""
    rect = win_rect(bundle)
    if not rect:
        return False
    x, y, w, h = (int(v) for v in rect)
    subprocess.run(
        ["screencapture", "-x", f"-R{x},{y},{w},{h}", "/tmp/_glink.png"], capture_output=True
    )
    res = ocr_local.recognize_detailed(Path("/tmp/_glink.png").read_bytes())
    if not res:
        return False
    for q in ("我知道了", "知道了", "×"):
        for t, b in zip(res[0], res[1], strict=False):
            if q in t:
                run_act(
                    "act",
                    "--app",
                    bundle,
                    "--verb",
                    "clickxy",
                    "--x",
                    str(rect[0] + (b[0] + b[2]) / 4),
                    "--y",
                    str(rect[1] + (b[1] + b[3]) / 4),
                    "--no-cursor",
                )
                return True
    return False


def t_get_meeting_link(app: str = "meeting", note: str = "", **_: object) -> str:
    """Read the created meeting's invite text from AX → extract the join link → clipboard. Self-heals
    a transient blocking popup (dismiss + retry) so the model doesn't have to check for one itself."""
    bundle = APPS.get(app, "")
    for attempt in range(3):
        els = run_act("snapshot", "--app", bundle).get("elements", [])
        full = next(
            (
                v
                for e in els
                if "meeting.tencent.com" in (v := (e.get("value") or e.get("label") or ""))
            ),
            "",
        )
        m = re.search(r"https://meeting\.tencent\.com/\S+", full)
        if m:
            link = m.group(0)
            subprocess.run(["pbcopy"], input=link.encode())
            hud(None, note or "链接已进剪贴板")
            return f"link on clipboard: {link}"
        if attempt < 2:  # no link yet — a popup may be covering it; dismiss and retry
            _dismiss_meeting_popup(bundle)
            time.sleep(0.8)
    return "no meeting link visible yet — press 预定 to submit the form first"


def t_paste_link_to_chat(person: str, note: str = "", **_: object) -> str:
    """Paste the clipboard link into `person`'s Lark chat input. HARD-GUARDED:
    only the authorized person, only into the input, NEVER sends (no Return)."""
    if person != AUTHORIZED_PERSON:
        return f"refused: paste is only authorized for {AUTHORIZED_PERSON!r}, not {person!r}"
    link = subprocess.run(["pbpaste"], capture_output=True).stdout.decode()
    if "meeting.tencent.com" not in link:
        return "refused: clipboard has no meeting link (call get_meeting_link first)"
    bundle = APPS["lark"]
    subprocess.run(
        ["osascript", "-e", f'tell application id "{bundle}" to activate'], capture_output=True
    )

    def elems() -> list[dict]:
        return run_act("snapshot", "--app", bundle, "--depth", "60").get("elements", [])

    poll(lambda: win_rect(bundle) is not None, timeout=2.0)

    def cur_input(es: list[dict]) -> dict | None:
        return next(
            (e for e in es if e["role"] == "AXTextArea" and e.get("bbox") and e["bbox"][2] > 100),
            None,
        )

    def chat_is(es: list[dict], who: str) -> bool:
        # the ACTIVE conversation's identity = the chat-area HEADER title (top, x>700, y<120),
        # NOT the input value (which is empty/unreliable). This is the reliable signal.
        return any(
            e["role"] == "AXStaticText"
            and (e.get("label") or e.get("value") or "").strip() == who
            and e.get("bbox")
            and e["bbox"][0] > 700
            and e["bbox"][1] < 130
            and e["bbox"][2] > 0
            for e in es
        )

    es = elems()
    if not chat_is(es, person):
        # open it: click the conversation-LIST row (exact label == person, left of the chat area,
        # visible) — disambiguated by hierarchy, the same primitive ax_find exposes to the model.
        row = next(
            (
                e
                for e in es
                if e["role"] == "AXStaticText"
                and (e.get("label") or e.get("value") or "").strip() == person
                and e.get("bbox")
                and e["bbox"][0] < 700
                and e["bbox"][2] > 0
                and e["bbox"][3] > 0
            ),
            None,
        )
        if row:
            b = row["bbox"]
            run_act(
                "act",
                "--app",
                bundle,
                "--verb",
                "clickxy",
                "--x",
                str(b[0] + b[2] / 2),
                "--y",
                str(b[1] + b[3] / 2),
                "--no-cursor",
            )
            es = elems()
            poll(lambda: chat_is(elems(), person), timeout=2.5)  # move on as soon as it switches
            es = elems()

    if not chat_is(es, person):
        return (
            f"could NOT confirm {person}'s chat is open (chat-title check failed); "
            "link is on the clipboard — leaving it for the user rather than pasting into the wrong chat"
        )
    inp = cur_input(es)
    if not inp:
        return "温子墨's chat is open but no message input found; link stays on clipboard"
    # "Paste" = AX set-value on the input element: no cursor move, no focus steal, no key event, and
    # no Return is ever issued, so the link lands in the draft but is NEVER sent (background-safe).
    run_act(
        "act",
        "--app",
        bundle,
        "--id",
        inp["id"],
        "--verb",
        "setvalue",
        "--text",
        link.strip(),
        "--no-cursor",
    )
    time.sleep(0.25)
    after = cur_input(elems())
    val = (after.get("value") or after.get("label") or "") if after else ""
    hud(None, note or f"已粘到{person}（未发送）")
    if "meeting.tencent.com" in val:
        return f"pasted link into {person}'s input (NOT sent; input now holds the link)"
    return "paste did not land; link remains on clipboard"


def t_done(summary: str = "", **_: object) -> str:
    hud(None, "完成 ✅")
    return "DONE: " + summary


DISPATCH = {
    "activate": t_activate,
    "ax_snapshot": t_ax_snapshot,
    "ax_find": t_ax_find,
    "ocr_locate": t_ocr_locate,
    "clickxy": t_clickxy,
    "ax_set_value": t_ax_set_value,
    "ax_press": t_ax_press,
    "get_meeting_link": t_get_meeting_link,
    "paste_link_to_chat": t_paste_link_to_chat,
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
# GENERIC tools — always available (app-agnostic primitives).
_GENERIC_TOOLS = [
    _fn(
        "activate",
        "bring an app to front: 'meeting' or 'lark' (also discloses that app's tools+manual)",
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
        "find EVERY AX element matching text, each tagged with region + container group (hierarchy) "
        "+ bbox — to pick the right one among duplicates (e.g. a sidebar row vs the name in chat)",
        {"app": _S, "query": _S, "note": _S},
        ["app", "query", "note"],
    ),
    _fn(
        "ocr_locate",
        "OCR-find PIXEL-drawn text (AX can't read it) on the front window → screen coords",
        {"query": _S, "app": _S, "note": _S},
        ["query", "note"],
    ),
    _fn(
        "clickxy",
        "click screen coords in an app",
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
        "press a button by snapshot index",
        {"app": _S, "index": _I, "note": _S},
        ["app", "index", "note"],
    ),
    _fn("done", "the chain is complete", {"summary": _S}, ["summary"]),
]
# APP-SPECIFIC tools — disclosed (added to the model's tool list) only after that app is activated,
# alongside its skill. Progressive disclosure of CAPABILITY, not just knowledge.
_APP_TOOLS = {
    "meeting": [
        _fn(
            "get_meeting_link",
            "read the created meeting's link from AX → clipboard",
            {"app": _S, "note": _S},
            ["note"],
        ),
    ],
    "lark": [
        _fn(
            "paste_link_to_chat",
            "open <person>'s Lark chat (by the conversation-list row) + paste the clipboard link into "
            "its input — NEVER sends; only the authorized person",
            {"person": _S, "note": _S},
            ["person", "note"],
        ),
    ],
}


def tools_payload() -> list[dict]:
    """Generic tools + the tools of every app disclosed so far (each app's tools appear with its skill)."""
    payload = list(_GENERIC_TOOLS)
    for app in _disclosed:
        payload.extend(_APP_TOOLS.get(app, []))
    return payload


def llm(messages: list[dict]) -> tuple[dict, float]:
    payload: dict = {
        "model": MODEL,
        "messages": messages,
        "tools": tools_payload(),
        "tool_choice": "auto",
        "temperature": 0,
        "max_tokens": 900,
    }
    # `thinking:disabled` is a DeepSeek extension; a strict OpenAI-compatible endpoint (Cerebras)
    # 400s on the unknown field, so only send it to DeepSeek.
    if "deepseek" in BASE_URL:
        payload["thinking"] = {"type": "disabled"}
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        BASE_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {KEY}",
            "Content-Type": "application/json",
            # Some gateways (Cerebras) 403 the default `Python-urllib` UA — send a normal one.
            "User-Agent": "Mozilla/5.0",
        },
        method="POST",
    )
    t = time.perf_counter()
    # Bypass any system proxy ($http(s)_proxy) — the providers are reached directly, like curl --noproxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    resp = json.loads(opener.open(req, timeout=120).read())
    return resp["choices"][0]["message"], (time.perf_counter() - t) * 1000


def main() -> None:
    global _hud
    if not KEY:
        print("DEEPSEEK_API_KEY not set")
        sys.exit(2)
    print("warming on-device OCR (cold ~60s, then cached)...")
    ocr_local.warm()
    _hud = subprocess.Popen([ACT, "cursor-hud"], stdin=subprocess.PIPE, text=True)

    messages = [{"role": "system", "content": SKILL}, {"role": "user", "content": TASK}]
    t0 = time.perf_counter()
    for step in range(int(os.environ.get("BENCH_STEPCAP", "30"))):
        try:
            m, ms = llm(messages)
        except Exception as exc:  # noqa: BLE001
            print(f"[{step}] LLM error: {type(exc).__name__}: {str(exc)[:80]}")
            break
        _llm_ms.append(ms)
        m.pop("reasoning", None)
        m.pop("reasoning_content", None)
        messages.append(m)
        tcs = m.get("tool_calls") or []
        if not tcs:
            print(f"[{step}] (no tool {ms:.0f}ms) {(m.get('content') or '')[:90]}")
            if not m.get("content"):
                break
            continue
        stop = False
        for tc in tcs:
            name = tc["function"]["name"]
            args = json.loads(tc["function"]["arguments"] or "{}")
            res = DISPATCH[name](**args)
            shown = {k: (v[:22] if isinstance(v, str) else v) for k, v in args.items()}
            print(f"[{step}] llm={ms:.0f}ms {name}({shown}) ⇒ {res.splitlines()[0][:70]}")
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": res})
            if name == "done":
                stop = True
        if stop:
            break
    total = time.perf_counter() - t0

    time.sleep(1.5)
    if _hud is not None:
        with contextlib.suppress(OSError):
            _hud.stdin.close()  # type: ignore[union-attr]
    mean = round(sum(_llm_ms) / len(_llm_ms)) if _llm_ms else 0
    print(f"\nTOTAL {total:.1f}s | {len(_llm_ms)} LLM calls | mean {mean}ms/call")
    print(
        f"clipboard now: {subprocess.run(['pbpaste'], capture_output=True).stdout.decode()[:60]!r}"
    )


if __name__ == "__main__":
    main()

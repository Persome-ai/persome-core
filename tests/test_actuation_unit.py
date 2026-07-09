"""Pure (offline) tests for the actuation layer: id codec, control-graph merge, the safety gate."""

from __future__ import annotations

import base64
import contextlib
import json

from persome.actuation import gate as gate_mod
from persome.actuation.graph import ControlGraph, Element, decode_id, encode_id, label_hash

# ── id codec (must match the Swift encoder byte-for-byte) ────────────────────


def test_id_round_trip():
    for path, lbl in ([], "Send"), ([0, 1, 2], "1"), ([5], ""), ([0, 0, 0, 0, 0, 38], "1"):
        enc = encode_id(path, lbl)
        got_path, got_hash = decode_id(enc)
        assert got_path == path
        assert got_hash == label_hash(lbl)


def test_id_label_hash_is_stable_and_8_hex():
    h = label_hash("Send")
    assert len(h) == 8 and all(c in "0123456789abcdef" for c in h)
    assert label_hash("Send") == h  # deterministic


def test_decode_real_swift_id_structure():
    # A real id emitted by mac-ax-actuator for Calculator's "1" button (path 0.0.0.0.0.38).
    real = "MC4wLjAuMC4wLjM4IzczY2Q0"  # truncated-safe prefix; decode the full form we build here
    rebuilt = encode_id([0, 0, 0, 0, 0, 38], "1")
    # The Swift and Python encoders agree on the path+hash payload shape.
    raw = base64.b64decode(rebuilt).decode()
    assert raw.startswith("0.0.0.0.0.38#")
    assert real.startswith(rebuilt[:20])  # same leading base64 (same path prefix)


def test_bad_id_decodes_to_none():
    assert decode_id("not base64 !!!") is None
    assert decode_id(base64.b64encode(b"nohash").decode()) is None


# ── control graph merge (AX wins over overlapping OCR) ───────────────────────


def test_control_graph_from_snapshot_and_ocr_merge():
    snap = {
        "app": "X",
        "pid": 1,
        "elements": [
            {
                "id": encode_id([0], "OK"),
                "role": "AXButton",
                "label": "OK",
                "bbox": [0, 0, 100, 40],
                "actions": ["AXPress"],
            },
        ],
    }
    g = ControlGraph.from_snapshot(snap)
    assert len(g.elements) == 1 and g.find("OK", "AXButton") is not None

    overlapping = Element(
        id="ocr1", role="AXStaticText", label="OK", source="ocr", bbox=[5, 5, 90, 30]
    )
    distinct = Element(
        id="ocr2", role="AXStaticText", label="Elsewhere", source="ocr", bbox=[500, 500, 80, 20]
    )
    g.merge_ocr([overlapping, distinct])
    # The OCR target overlapping the AX button is dropped; the distinct one is kept.
    assert len(g.elements) == 2
    assert any(e.source == "ocr" and e.label == "Elsewhere" for e in g.elements)
    assert not any(e.source == "ocr" and e.label == "OK" for e in g.elements)


# ── the safety gate ──────────────────────────────────────────────────────────


def test_classify_side_effect_label_is_gated():
    d = gate_mod.classify(verb="press", label="Send", bundle_id="com.apple.MobileSMS")
    assert d.allowed and d.gated
    d2 = gate_mod.classify(verb="press", label="5", bundle_id="com.apple.calculator")
    assert d2.allowed and not d2.gated


def test_classify_per_app_levels():
    assert not gate_mod.classify(
        verb="press", label="x", bundle_id="com.1password.1password"
    ).allowed
    assert not gate_mod.classify(
        verb="press", label="x", bundle_id="com.apple.Safari"
    ).allowed  # read-only
    cv = gate_mod.classify(verb="setvalue", label="x", bundle_id="com.microsoft.VSCode")
    assert not cv.allowed  # click-only blocks value entry
    assert gate_mod.classify(verb="press", label="x", bundle_id="com.microsoft.VSCode").allowed


def test_classify_action_verb_is_press_level():
    # The `action` verb (ui_perform → AXIncrement/AXDecrement/AXShowMenu/AXPick on steppers/dropdowns)
    # is a press-level AX action: allowed by default, gated ONLY by a side-effect label, and NOT
    # treated as value entry — so it survives click-only apps but still confirms on a Send/Delete target.
    benign = gate_mod.classify(verb="action", label="日期", bundle_id="com.tencent.meeting")
    assert benign.allowed and not benign.gated  # bump a date stepper → no confirm
    gated = gate_mod.classify(verb="action", label="Send", bundle_id="com.apple.MobileSMS")
    assert gated.allowed and gated.gated  # an action on a Send-labelled target still confirms
    # click-only apps (no value entry) still allow a press-level action
    assert gate_mod.classify(verb="action", label="x", bundle_id="com.microsoft.VSCode").allowed


def test_gate_blocks_gated_action_until_confirmed():
    calls = {"performed": 0}

    def perform(**kw):
        calls["performed"] += 1
        return {"ok": True, "diff": [{"change": "changed"}]}

    # Denied → never performs.
    g_deny = gate_mod.Gate(confirm=lambda s: False, perform=perform)
    r = g_deny.run(verb="press", element_id="id", label="发送", bundle_id="com.apple.MobileSMS")
    assert r["ok"] is False and r["error"] == "denied" and calls["performed"] == 0

    # Approved → performs + verifies from the diff.
    g_ok = gate_mod.Gate(confirm=lambda s: True, perform=perform)
    r2 = g_ok.run(verb="press", element_id="id", label="发送", bundle_id="com.apple.MobileSMS")
    assert r2["ok"] and r2["gated"] and r2["verified"] and calls["performed"] == 1


def test_gate_non_side_effect_skips_confirm():
    confirmed = {"asked": False}

    def confirm(s):
        confirmed["asked"] = True
        return True

    g = gate_mod.Gate(
        confirm=confirm, perform=lambda **k: {"ok": True, "diff": [{"change": "changed"}]}
    )
    r = g.run(verb="press", element_id="id", label="5", bundle_id="com.apple.calculator")
    assert r["ok"] and not r["gated"] and confirmed["asked"] is False


def test_verify_from_diff():
    assert gate_mod.verify_from_diff([{"change": "changed"}]) is True
    assert gate_mod.verify_from_diff([]) is False
    assert gate_mod.verify_from_diff([{"change": "none"}]) is False


# ── more gate cases (broaden the side-effect + per-app matrix) ────────────────


def test_classify_more_side_effect_labels():
    for lbl in ("Delete", "删除", "支付", "发布", "Submit", "Share", "购买"):
        assert gate_mod.classify(verb="press", label=lbl, bundle_id="com.x").gated, lbl
    for lbl in ("5", "Cancel", "返回", "Settings", ""):
        assert not gate_mod.classify(verb="press", label=lbl, bundle_id="com.x").gated, lbl


def test_setvalue_is_always_gated_even_with_innocuous_label():
    d = gate_mod.classify(verb="setvalue", label="备注", bundle_id="com.x")
    assert d.allowed and d.gated


def test_blocked_app_blocks_everything():
    for verb in ("press", "setvalue", "snapshot"):
        # snapshot isn't an act verb here, but BLOCKED rejects up front regardless
        d = gate_mod.classify(verb=verb, label="x", bundle_id="com.apple.Passwords")
        assert not d.allowed


def test_read_only_blocks_acts_but_classify_snapshot_passes_level():
    # READ_ONLY only allows the snapshot verb.
    assert not gate_mod.classify(verb="press", label="x", bundle_id="com.apple.Safari").allowed
    assert gate_mod.classify(verb="snapshot", label="x", bundle_id="com.apple.Safari").allowed


def test_unknown_app_defaults_to_full():
    assert gate_mod.level_for("com.some.unknown.app") == gate_mod.FULL
    assert gate_mod.classify(verb="press", label="x", bundle_id="com.some.unknown").allowed


def test_gate_blocked_app_returns_blocked_without_confirm():
    asked = {"n": 0}
    g = gate_mod.Gate(
        confirm=lambda s: asked.__setitem__("n", asked["n"] + 1) or True,
        perform=lambda **k: {"ok": True, "diff": []},
    )
    r = g.run(verb="press", element_id="i", label="Send", bundle_id="com.1password.1password")
    assert r["ok"] is False and r["error"] == "blocked" and asked["n"] == 0


# ── more codec / graph cases ─────────────────────────────────────────────────


def test_codec_unicode_label_round_trip():
    for lbl in ("温子墨", "发送给 沈砚舟", "🚀 ok", "a\tb"):
        path, h = decode_id(encode_id([1, 2, 3], lbl))
        assert path == [1, 2, 3] and h == label_hash(lbl)


def test_codec_empty_path():
    p, h = decode_id(encode_id([], "x"))
    assert p == [] and h == label_hash("x")


def test_label_hash_distinguishes_labels():
    assert label_hash("Send") != label_hash("Cancel")
    assert label_hash("发送") != label_hash("发布")


def test_graph_merge_keeps_non_overlapping_ocr():
    g = ControlGraph.from_snapshot({"app": "X", "pid": 1, "elements": []})
    far = Element(id="o", role="AXStaticText", label="far", source="ocr", bbox=[900, 900, 50, 20])
    g.merge_ocr([far])
    assert len(g.elements) == 1 and g.elements[0].source == "ocr"


def test_graph_find_by_label_any_role():
    g = ControlGraph.from_snapshot(
        {
            "app": "X",
            "pid": 1,
            "elements": [{"id": encode_id([0], "Go"), "role": "AXButton", "label": "Go"}],
        }
    )
    assert g.find("Go") is not None and g.find("Nope") is None


# ── browser / Gmail-scenario safety: known browsers are READ_ONLY by default ──
# (you can perceive a webmail page but NOT auto-click "Send" in Safari/Chrome without opt-in —
#  the guard against an agent silently sending a Gmail email through the AX layer.)


def test_known_browsers_block_actuation_but_allow_snapshot():
    for bundle in ("com.apple.Safari", "com.google.Chrome"):
        # reading the page is fine (perceive Gmail's UI)…
        assert gate_mod.classify(verb="snapshot", label="x", bundle_id=bundle).allowed
        # …but clicking "Send" / typing into a field is refused (no accidental email send).
        assert not gate_mod.classify(verb="press", label="Send", bundle_id=bundle).allowed
        assert not gate_mod.classify(verb="setvalue", label="To", bundle_id=bundle).allowed


def test_gate_refuses_gmail_send_in_readonly_browser():
    fired = {"performed": False}
    g = gate_mod.Gate(
        confirm=lambda s: True,
        perform=lambda **k: fired.__setitem__("performed", True) or {"ok": True, "diff": []},
    )
    r = g.run(verb="press", element_id="i", label="Send", bundle_id="com.apple.Safari")
    assert r["ok"] is False and r["error"] == "blocked" and fired["performed"] is False


# ── persistent cursor HUD controller + dev-default boxes ─────────────────────


def test_cursor_hud_writes_update_json(monkeypatch):
    import subprocess as sp

    from persome.actuation import actuator as actuator_mod
    from persome.actuation.cursor_hud import CursorHUD

    captured: list[str] = []

    class _FakeStdin:
        def write(self, s):
            captured.append(s)

        def flush(self):
            pass

        def close(self):
            pass

    class _FakeProc:
        stdin = _FakeStdin()

        def poll(self):
            return None  # alive

    monkeypatch.setattr(actuator_mod, "_resolve_actuator_path", lambda: "fakebin")
    monkeypatch.setattr(sp, "Popen", lambda *a, **k: _FakeProc())

    h = CursorHUD(idle_seconds=99)
    h.update(
        [100.0, 200.0],
        "正在给 xxx 发送消息",
        elements=[{"bbox": [1, 2, 3, 4], "role": "AXButton"}, {"role": "AXGroup"}],
    )

    msg = json.loads("".join(captured).strip())
    assert msg["x"] == 100.0 and msg["y"] == 200.0 and "xxx" in msg["note"]
    # only the element with a bbox is forwarded
    assert msg["elements"] == [{"bbox": [1, 2, 3, 4], "role": "AXButton"}]
    h.stop()


def test_cursor_hud_noop_without_binary(monkeypatch):
    from persome.actuation import actuator as actuator_mod
    from persome.actuation.cursor_hud import CursorHUD

    monkeypatch.setattr(actuator_mod, "_resolve_actuator_path", lambda: None)
    h = CursorHUD()
    h.update([1.0, 2.0], "x")  # must not raise / spawn anything
    h.stop()


def test_cursor_hud_skips_empty_update():
    from persome.actuation.cursor_hud import CursorHUD

    h = CursorHUD()
    h.update(None, "")  # nothing to show → no spawn, no error
    assert h._proc is None


# ── _run failure observability (#466: a wedged act must leave its phase breadcrumbs) ──


def test_run_timeout_returns_stderr_tail(monkeypatch):
    import subprocess as sp

    from persome.actuation import actuator as actuator_mod

    def _raise_timeout(argv, **kw):
        raise sp.TimeoutExpired(cmd=argv, timeout=10, stderr=b"[act] phase=perform:key t=8000ms\n")

    monkeypatch.setattr(actuator_mod, "_resolve_actuator_path", lambda: "fakebin")
    monkeypatch.setattr(sp, "run", _raise_timeout)
    res = actuator_mod._run(["act", "--verb", "key"])
    assert res["ok"] is False and res["error"] == "actuator_timeout"
    assert "phase=perform:key" in res["stderr_tail"]


def test_run_bad_json_keeps_stderr_tail(monkeypatch):
    import subprocess as sp

    from persome.actuation import actuator as actuator_mod

    monkeypatch.setattr(actuator_mod, "_resolve_actuator_path", lambda: "fakebin")
    monkeypatch.setattr(
        sp,
        "run",
        lambda argv, **kw: sp.CompletedProcess(
            argv, 3, stdout=b"not json", stderr=b"[act] DEADLINE phase=before_snapshot t=8001ms\n"
        ),
    )
    res = actuator_mod._run(["act"])
    assert res["ok"] is False and res["error"] == "actuator_failed"
    assert "DEADLINE phase=before_snapshot" in res["stderr_tail"]


def test_run_deadline_error_json_passes_through(monkeypatch):
    # The helper's own 8s deadline emits STRUCTURED JSON + exit(3): the daemon must hand that
    # phase+hint straight to the agent (subprocess.run without check= does not raise on exit 3).
    import subprocess as sp

    from persome.actuation import actuator as actuator_mod

    payload = (
        b'{"ok": false, "error": "act_deadline_exceeded", "phase": "perform:key",'
        b' "hint": "check state before retrying"}'
    )
    monkeypatch.setattr(actuator_mod, "_resolve_actuator_path", lambda: "fakebin")
    monkeypatch.setattr(
        sp, "run", lambda argv, **kw: sp.CompletedProcess(argv, 3, stdout=payload, stderr=b"")
    )
    res = actuator_mod._run(["act", "--verb", "key"])
    assert res["error"] == "act_deadline_exceeded"
    assert res["phase"] == "perform:key"


# (the show-boxes default is covered by test_dev_account_defaults_show_boxes_on:
#  plain default off, on for dev accounts via `actuation_show_boxes OR dev.enabled`.)


# ── freeform-verb gate (key / type / clickxy — no AX label to classify) ──────


def test_classify_freeform_submit_key_in_comms_app_is_gated():
    # enter/return in a messaging app = send → confirm.
    d = gate_mod.classify_freeform(verb="key", keys="enter", app="WeChat")
    assert d.allowed and d.gated
    d2 = gate_mod.classify_freeform(verb="key", keys="cmd+enter", app="com.bytedance.lark")
    assert d2.allowed and d2.gated


def test_classify_freeform_submit_key_outside_comms_not_gated():
    # enter in a calculator / editor is a newline / equals — not a side-effect.
    d = gate_mod.classify_freeform(verb="key", keys="enter", app="TextEdit")
    assert d.allowed and not d.gated
    d2 = gate_mod.classify_freeform(verb="key", keys="cmd+a", app="WeChat")
    assert d2.allowed and not d2.gated  # select-all in comms is harmless


def test_classify_freeform_type_or_click_in_comms_is_gated():
    for verb in ("type", "clickxy"):
        assert gate_mod.classify_freeform(verb=verb, app="微信").gated
        assert gate_mod.classify_freeform(verb=verb, app="Mail").gated
        assert not gate_mod.classify_freeform(verb=verb, app="Calculator").gated


def test_classify_freeform_note_announcing_side_effect_is_gated_any_app():
    # the agent's own note ("发送…") gates regardless of app.
    assert gate_mod.classify_freeform(verb="clickxy", app="Calculator", note="发送给张三").gated
    assert gate_mod.classify_freeform(
        verb="key", keys="space", app="Foo", note="delete the row"
    ).gated


def test_classify_freeform_blocked_app():
    d = gate_mod.classify_freeform(verb="type", app="com.apple.Passwords")
    assert not d.allowed
    d2 = gate_mod.classify_freeform(verb="clickxy", app="System Settings")
    assert not d2.allowed


# ── confirm round-trip (daemon ↔ app) ────────────────────────────────────────


def test_confirm_denies_when_no_subscriber(monkeypatch):
    from persome.actuation import confirm as confirm_mod

    monkeypatch.setattr(confirm_mod.events, "has_subscribers", lambda: False)
    # No one listening → deny immediately, no pending registered.
    assert confirm_mod.request("send msg", app="WeChat", verb="key") is False
    assert confirm_mod.pending_ids() == []


def test_confirm_times_out_to_deny(monkeypatch):
    from persome.actuation import confirm as confirm_mod

    monkeypatch.setattr(confirm_mod.events, "has_subscribers", lambda: True)
    monkeypatch.setattr(confirm_mod.events, "publish", lambda *a, **k: None)
    # Tiny timeout, nobody resolves → False (fail-safe), and the pending entry is cleaned up.
    assert confirm_mod.request("x", timeout=0.05) is False
    assert confirm_mod.pending_ids() == []


def test_confirm_resolves_approved(monkeypatch):
    import threading

    from persome.actuation import confirm as confirm_mod

    published: list[dict] = []
    monkeypatch.setattr(confirm_mod.events, "has_subscribers", lambda: True)
    monkeypatch.setattr(
        confirm_mod.events, "publish", lambda stage, et, payload: published.append(payload)
    )

    def approve_soon():
        # spin until the request has registered its id, then approve it.
        for _ in range(200):
            ids = confirm_mod.pending_ids()
            if ids:
                confirm_mod.resolve(ids[0], approved=True)
                return
            threading.Event().wait(0.005)

    t = threading.Thread(target=approve_soon)
    t.start()
    ok = confirm_mod.request("send", app="WeChat", verb="key", timeout=3.0)
    t.join()
    assert ok is True
    assert published and "id" in published[0] and published[0]["app"] == "WeChat"
    assert confirm_mod.pending_ids() == []


def test_confirm_resolve_unknown_id_is_false():
    from persome.actuation import confirm as confirm_mod

    assert confirm_mod.resolve("does-not-exist", approved=True) is False


# ── locate (eyes) — pure helpers + ax_find over a mocked snapshot ─────────────


def test_locate_pure_helpers():
    from persome.actuation import locate as locate_mod

    assert locate_mod._group_label(0) == "A"
    assert locate_mod._group_label(25) == "Z"
    assert locate_mod._group_label(26) == "AA"
    # path codec round-trip via the real encoder
    from persome.actuation.graph import encode_id

    eid = encode_id([0, 3, 7], "x")
    assert locate_mod._path_of(eid) == "0.3.7"
    # PNG IHDR size parse
    import struct

    png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", 800, 600)
    assert locate_mod._png_size(png) == (800, 600)
    assert locate_mod._png_size(b"not a png") is None


def test_locate_ax_find_matches_and_containers(monkeypatch):
    from persome.actuation import locate as locate_mod
    from persome.actuation.graph import encode_id

    snap = {
        "ok": True,
        "bundle_id": "com.test",
        "elements": [
            {
                "id": encode_id([0, 0], "温子墨"),
                "role": "AXStaticText",
                "label": "温子墨",
                "bbox": [10, 20, 100, 30],
            },
            {
                "id": encode_id([5, 1], "温子墨 在线"),
                "role": "AXRow",
                "value": "温子墨 在线",
                "bbox": [0, 0, 200, 40],
            },
            {
                "id": encode_id([9], "其他"),
                "role": "AXButton",
                "label": "其他",
                "bbox": [0, 0, 5, 5],
            },
        ],
    }
    monkeypatch.setattr(locate_mod.actuator, "snapshot", lambda **kw: snap)
    res = locate_mod.ax_find("Whatever", "温子墨")
    assert res["ok"] and res["count"] == 2
    labels = [m["text"] for m in res["matches"]]
    assert "温子墨" in labels and any("在线" in t for t in labels)
    # two different subtrees → two container letters
    assert len({m["container"] for m in res["matches"]}) == 2
    assert all(m["visible"] for m in res["matches"])


def test_locate_ax_find_propagates_snapshot_failure(monkeypatch):
    from persome.actuation import locate as locate_mod

    monkeypatch.setattr(
        locate_mod.actuator, "snapshot", lambda **kw: {"ok": False, "error": "no_ax"}
    )
    res = locate_mod.ax_find("X", "q")
    assert res["ok"] is False and res["error"] == "no_ax"


def test_locate_ocr_unavailable_off_darwin(monkeypatch):
    from persome.actuation import locate as locate_mod

    monkeypatch.setattr(locate_mod.platform, "system", lambda: "Linux")
    res = locate_mod.ocr_locate("X", "q")
    assert res["ok"] is False and res["error"] == "actuator_unavailable"


# ── background routing (no-steal path selection) ─────────────────────────────


def test_routing_skylight_for_electron_and_native():
    from persome.actuation import routing

    assert routing.bg_path_for("com.electron.lark") == "skylight"
    assert routing.bg_path_for("com.google.Chrome") == "skylight"
    assert routing.bg_path_for("com.apple.calculator") == "skylight"
    assert routing.bg_path_for(None) == "skylight"


def test_routing_borrow_for_canvas():
    from persome.actuation import routing

    assert routing.bg_path_for("org.blenderfoundation.blender") == "borrow"


def test_routing_degrades_when_skylight_unavailable():
    from persome.actuation import routing

    # native AppKit accepts plain postToPid…
    assert routing.bg_path_for("com.apple.TextEdit", skylight_available=False) == "postpid"
    # …Electron/Chromium need a brief foreground (borrow) without the SkyLight channel.
    assert routing.bg_path_for("com.electron.lark", skylight_available=False) == "borrow"
    # canvas is always borrow regardless of skylight.
    assert routing.bg_path_for("com.unity3d.UnityEditor5.x", skylight_available=False) == "borrow"


def test_instance_policy_multi_for_browsers_single_otherwise():
    from persome.actuation import routing

    # Browsers can run a fresh-profile instance the user never sees → multi.
    assert routing.instance_policy("com.google.Chrome") == "multi"
    assert routing.instance_policy("com.brave.Browser") == "multi"
    assert routing.instance_policy("COMPANY.THEBROWSER.BROWSER") == "multi"  # case-insensitive
    # Login/state-bound apps → single (a fresh instance is useless without the user's session).
    assert routing.instance_policy("com.electron.lark") == "single"  # Feishu
    assert routing.instance_policy("com.tencent.xinWeChat") == "single"
    # Safari is single-process (can't `open -na` a 2nd) → single.
    assert routing.instance_policy("com.apple.Safari") == "single"
    # Unknown / None default to single (conservative: ask to borrow, don't silently spawn).
    assert routing.instance_policy("com.acme.unknown") == "single"
    assert routing.instance_policy(None) == "single"


def test_stage_strategy_virtual_for_multi_borrow_for_single():
    from persome.actuation import routing

    # Multi-instance app + virtual display available → spawn the agent's own instance off-screen.
    assert routing.stage_strategy("com.google.Chrome") == "virtual_stage"
    # Single-instance app → must borrow the user's one copy (with consent).
    assert routing.stage_strategy("com.electron.lark") == "borrow"
    assert routing.stage_strategy(None) == "borrow"
    # No virtual-display path → never silently downgrade a multi app to a steal; fall to borrow.
    assert routing.stage_strategy("com.google.Chrome", virtual_display_available=False) == "borrow"


# ── focus-borrow fallback (save front → activate target → restore) ───────────


def test_focus_borrow_restores_prior_front():
    from persome.actuation import focus

    calls = []
    with focus.borrow(
        "com.target", get_front=lambda: "com.user.front", activate_app=lambda a: calls.append(a)
    ):
        calls.append("BODY")
    # activate target → body → restore prior front, in order.
    assert calls == ["com.target", "BODY", "com.user.front"]


def test_focus_borrow_skips_restore_when_prior_unknown_or_same():
    from persome.actuation import focus

    seen = []
    with focus.borrow("com.target", get_front=lambda: None, activate_app=lambda a: seen.append(a)):
        pass
    assert seen == ["com.target"]  # nothing to restore

    seen.clear()
    with focus.borrow(
        "com.target", get_front=lambda: "com.target", activate_app=lambda a: seen.append(a)
    ):
        pass
    assert seen == ["com.target"]  # prior == target → no restore


def test_focus_borrow_restores_even_on_exception():
    from persome.actuation import focus

    seen = []
    with (
        contextlib.suppress(ValueError),
        focus.borrow("com.t", get_front=lambda: "com.f", activate_app=lambda a: seen.append(a)),
    ):
        raise ValueError("boom")
    assert seen == ["com.t", "com.f"]  # restored despite the raise


# ── virtual-display stage lifecycle (offline, fake helper process) ───────────


class _FakeProc:
    """Mimics the subprocess.Popen slice VirtualStage uses; emits a canned first stdout line."""

    def __init__(self, line: str = "", *, hang: bool = False):
        import io

        self.stdout = io.StringIO(line)
        self.stdin = io.StringIO()
        self._hang = hang  # if set, never "emit" (readline blocks → simulate via empty + alive)
        self.terminated = False
        self.killed = False
        self._rc: int | None = None
        if hang:
            # a stdout whose readline blocks forever
            class _Block:
                def readline(self_inner):
                    import time

                    time.sleep(60)
                    return ""

            self.stdout = _Block()

    def poll(self):
        return self._rc

    def terminate(self):
        self.terminated = True
        self._rc = -15

    def wait(self, timeout=None):
        return self._rc if self._rc is not None else 0

    def kill(self):
        self.killed = True
        self._rc = -9


def test_stage_opens_with_ready_window(monkeypatch):
    from persome.actuation import stage as stage_mod

    monkeypatch.setattr(
        stage_mod,
        "_resolve_stage_helper_path",
        lambda: __import__("pathlib").Path("/fake/mac-virtual-stage"),
    )
    line = json.dumps(
        {
            "display_id": 9,
            "bounds": [1920, 0, 1920, 1080],
            "app_pid": 42,
            "window_id": 777,
            "window_bounds": [1960, 40, 1840, 960],
        }
    )
    proc = _FakeProc(line + "\n")
    st = stage_mod.VirtualStage.open(app="Google Chrome", url="about:blank", spawn=lambda a: proc)
    assert isinstance(st, stage_mod.VirtualStage)
    assert st.ready and st.window_id == 777 and st.display_id == 9 and st.app_pid == 42
    assert st.bounds == [1920, 0, 1920, 1080]
    st.close()
    assert proc.terminated  # helper reaped → display released


def test_stage_unavailable_off_darwin(monkeypatch):
    from persome.actuation import stage as stage_mod

    monkeypatch.setattr(stage_mod, "_resolve_stage_helper_path", lambda: None)
    out = stage_mod.VirtualStage.open(app="X", url="y", spawn=lambda a: _FakeProc("{}"))
    assert out == {"ok": False, "error": "virtual_stage_unavailable"}


def test_stage_error_line_terminates_proc(monkeypatch):
    from persome.actuation import stage as stage_mod

    monkeypatch.setattr(
        stage_mod, "_resolve_stage_helper_path", lambda: __import__("pathlib").Path("/fake/x")
    )
    proc = _FakeProc(json.dumps({"error": "no_virtual_display"}) + "\n")
    out = stage_mod.VirtualStage.open(app="X", url="y", spawn=lambda a: proc)
    assert out == {"ok": False, "error": "no_virtual_display"}
    assert proc.terminated  # failed helper not left running


def test_stage_display_but_no_window_is_not_ready(monkeypatch):
    from persome.actuation import stage as stage_mod

    monkeypatch.setattr(
        stage_mod, "_resolve_stage_helper_path", lambda: __import__("pathlib").Path("/fake/x")
    )
    # display came up but no window → helper emits a warning; stage is not drivable → torn down.
    proc = _FakeProc(
        json.dumps(
            {"display_id": 9, "bounds": [1920, 0, 1920, 1080], "warning": "window_not_found"}
        )
        + "\n"
    )
    out = stage_mod.VirtualStage.open(app="X", url="y", spawn=lambda a: proc)
    assert out == {"ok": False, "error": "window_not_found"}
    assert proc.terminated


def test_stage_timeout_when_helper_never_emits(monkeypatch):
    from persome.actuation import stage as stage_mod

    monkeypatch.setattr(
        stage_mod, "_resolve_stage_helper_path", lambda: __import__("pathlib").Path("/fake/x")
    )
    proc = _FakeProc(hang=True)
    out = stage_mod.VirtualStage.open(app="X", url="y", spawn=lambda a: proc, ready_timeout=0.2)
    assert out["ok"] is False and out["error"] == "virtual_stage_no_window"
    assert proc.terminated


def test_stage_context_manager_tears_down(monkeypatch):
    from persome.actuation import stage as stage_mod

    monkeypatch.setattr(
        stage_mod, "_resolve_stage_helper_path", lambda: __import__("pathlib").Path("/fake/x")
    )
    line = (
        json.dumps({"display_id": 1, "bounds": [1920, 0, 1920, 1080], "app_pid": 5, "window_id": 9})
        + "\n"
    )
    proc = _FakeProc(line)
    with stage_mod.VirtualStage.open(app="X", url="y", spawn=lambda a: proc) as st:
        assert st.ready
    assert proc.terminated  # __exit__ closed it
    # idempotent: a second close doesn't blow up
    st.close()


# ── open_app orchestration (stage_strategy → virtual_stage | borrow) ─────────


def _ready_stage(app_pid=42, window_id=777):
    from persome.actuation import stage as stage_mod

    line = (
        json.dumps(
            {
                "display_id": 9,
                "bounds": [1920, 0, 1920, 1080],
                "app_pid": app_pid,
                "window_id": window_id,
            }
        )
        + "\n"
    )
    return stage_mod.VirtualStage(
        _FakeProc(line),
        {
            "display_id": 9,
            "bounds": [1920, 0, 1920, 1080],
            "app_pid": app_pid,
            "window_id": window_id,
        },
    )


def test_open_app_multi_instance_opens_virtual_stage():
    from persome.actuation import stage as stage_mod

    st = _ready_stage()
    out = stage_mod.open_app(
        "Google Chrome",
        "https://meet.google.com/new",
        bundle_id="com.google.Chrome",
        stage_opener=lambda **k: st,
    )
    assert out["strategy"] == "virtual_stage"
    assert out["app_pid"] == 42 and out["window_id"] == 777 and out["display_id"] == 9
    # registered so subsequent verb calls + teardown find it
    assert stage_mod.registry.get(42) is st
    assert stage_mod.registry.close(42) is True
    assert stage_mod.registry.get(42) is None


def test_open_app_single_instance_signals_borrow():
    from persome.actuation import stage as stage_mod

    out = stage_mod.open_app(
        "Feishu",
        "x",
        bundle_id="com.electron.lark",
        stage_opener=lambda **k: pytest_fail_if_called(),
    )
    assert out["strategy"] == "borrow" and out["needs_consent"] is True
    assert out["bundle_id"] == "com.electron.lark"


def pytest_fail_if_called(*a, **k):
    raise AssertionError("stage_opener must not be called for a single-instance app")


def test_open_app_virtual_stage_failure_degrades_to_borrow():
    from persome.actuation import stage as stage_mod

    out = stage_mod.open_app(
        "Google Chrome",
        "x",
        bundle_id="com.google.Chrome",
        stage_opener=lambda **k: {"ok": False, "error": "no_virtual_display"},
    )
    # never a silent steal: a failed stage downgrades to the borrow signal with provenance
    assert out["strategy"] == "borrow" and out["needs_consent"] is True
    assert out["fallback_from"] == "virtual_stage"
    assert out["detail"]["error"] == "no_virtual_display"


def test_open_app_unknown_bundle_defaults_to_borrow():
    from persome.actuation import stage as stage_mod

    out = stage_mod.open_app(
        "Weird App",
        "x",
        resolve_bundle=lambda app: None,  # couldn't resolve → conservative single
        stage_opener=lambda **k: pytest_fail_if_called(),
    )
    assert out["strategy"] == "borrow"


def test_registry_close_all_reaps_every_stage():
    from persome.actuation import stage as stage_mod

    reg = stage_mod.StageRegistry()
    s1, s2 = _ready_stage(app_pid=1), _ready_stage(app_pid=2)
    reg.add(s1)
    reg.add(s2)
    reg.close_all()
    assert reg.get(1) is None and reg.get(2) is None
    assert s1._proc.terminated and s2._proc.terminated


# ── per-app skills (progressive disclosure) ─────────────────────────────────


def test_skills_parse_frontmatter():
    from persome.actuation import skills

    s = skills._parse(
        "---\napp: TestApp\nbundles: com.x.test, com.x.test2\n"
        "summary: a tricky app\naliases: TA, 测试\n---\n# body\nsome manual text"
    )
    assert s is not None
    assert s.app == "TestApp"
    assert s.bundles == ("com.x.test", "com.x.test2")
    assert s.aliases == ("TA", "测试")
    assert s.summary == "a tricky app"
    assert s.body == "# body\nsome manual text"


def test_skills_parse_rejects_no_frontmatter_or_empty_body():
    from persome.actuation import skills

    assert skills._parse("# just a heading, no frontmatter") is None
    assert skills._parse("---\napp: X\n---\n") is None  # empty body
    assert skills._parse("---\nsummary: no app\n---\nbody") is None  # no app


def test_skills_list_has_the_shipped_apps():
    from persome.actuation import skills

    apps = {s["app"] for s in skills.list_skills()}
    # the four production skills promoted from the benchmark + today's findings
    assert {"WeChat", "Feishu", "腾讯会议", "Google Chrome"} <= apps
    # every entry carries a one-line summary (the lean menu)
    assert all(s["summary"] for s in skills.list_skills())


def test_skills_resolve_by_name_bundle_and_alias():
    from persome.actuation import skills

    assert skills.guide_for("WeChat").app == "WeChat"
    assert skills.guide_for("com.tencent.xinWeChat").app == "WeChat"  # bundle
    assert skills.guide_for("微信").app == "WeChat"  # alias
    assert skills.guide_for("Google Chrome").app == "Google Chrome"
    assert skills.guide_for("com.electron.lark").app == "Feishu"  # bundle
    assert skills.guide_for("Lark").app == "Feishu"  # alias
    assert skills.guide_for("腾讯会议").app == "腾讯会议"
    # unknown / empty → None
    assert skills.guide_for("SomeUnknownApp") is None
    assert skills.guide_for("") is None


def test_skills_wechat_guide_carries_the_keyboard_flow():
    from persome.actuation import skills

    body = skills.guide_for("WeChat").body
    # the load-bearing facts today's E2E established
    assert "KEYBOARD" in body.upper()
    assert "cmd+f" in body
    assert "ui_type" in body and "ui_key" in body


def test_dev_account_defaults_show_boxes_on():
    from persome.config import load as load_config

    cfg = load_config()
    assert cfg.actuation_show_boxes is False  # plain default off
    # the MCP path uses `actuation_show_boxes OR dev.enabled`; emulate that here:
    cfg.dev.enabled = True
    assert (cfg.actuation_show_boxes or cfg.dev.enabled) is True


# ── act latency: the daemon passes --cache-before so an act reuses the prior snapshot's
#    state as its before-state, dropping the redundant before-walk (issue #466) ──────────


def test_act_verbs_pass_cache_before(monkeypatch):
    """act/key/type/clickxy must pass `--cache-before`: each act otherwise walks the whole AX
    tree TWICE (before+after for the diff), which on a large app (~1600 elements) is ~7s and
    trips the subprocess timeout → actuator_failed even though the action landed (#466). The
    flag reuses the prior snapshot/act's cached state as the before-state (safe: the actuator
    falls back to a fresh before-walk on a cache miss)."""
    from persome.actuation import actuator as act_mod

    captured: list[list[str]] = []
    monkeypatch.setattr(act_mod, "_run", lambda args: captured.append(args) or {"ok": True})

    act_mod.act(app="Finder", element_id="x", verb="press")
    act_mod.key("enter", app="Finder")
    act_mod.type_text("hi", app="Finder")
    act_mod.clickxy(10, 20, app="Finder")
    for args in captured:
        assert args[0] == "act"
        assert "--cache-before" in args, f"act verb missing --cache-before: {args}"

    # snapshot WRITES the cache (it's the before-state producer), so the read-side flag does
    # not apply to it — it must NOT carry --cache-before.
    captured.clear()
    act_mod.snapshot(app="Finder")
    assert captured and captured[0][0] == "snapshot"
    assert "--cache-before" not in captured[0]

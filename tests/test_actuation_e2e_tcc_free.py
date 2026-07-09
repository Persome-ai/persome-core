"""TCC-free END-TO-END test of the gated-actuation confirm round-trip.

This is the self-iteration pipeline that lets the actuation chain be exercised WITHOUT the real
Persome.app, any notification UI, or any Accessibility/TCC grant. It drives the REAL registered
`ui_set_value` MCP tool fn — the real async dispatch, the real safety `Gate`, the real
`confirm.request` SSE round-trip, the real `asyncio.to_thread` offload — against:
  • a FAKE actuator (monkeypatched snapshot/act → no subprocess, no Accessibility), and
  • a FAKE app-client that subscribes to the event stream and POST-equivalent resolves the confirm,
    exactly like the app's ActuationConfirmController.

Unlike test_actuation_confirm_loop.py (which drives the asyncio.to_thread(_confirm.request) *pattern*),
this drives the ACTUAL tool, so it proves the freeze fix (C1) at the real-tool level: the
confirm_request is delivered to the app-client and resolved WHILE the tool is still awaiting — if the
event loop were frozen (the pre-fix bug) the client could not receive the event within the timeout and
the test would fail. It also proves approve→actuator-runs and deny→actuator-not-called end-to-end.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time

import pytest

from persome import config as C
from persome import events
from persome.actuation import actuator as _actuator
from persome.actuation import confirm as _confirm
from persome.actuation import cursor_hud as _cursor_hud
from persome.mcp.server import build_server


def _server_with_fake_actuator(monkeypatch):
    """Build the real server with actuation enabled, but a FAKE actuator + no-op cursor HUD so the
    whole gated path runs headless (no subprocess, no display, no TCC). Returns the real ui_set_value
    tool fn and a capture dict recording the actuator calls that actually fired."""
    cfg = C.Config()
    cfg.actuation_enabled = True
    captured: dict = {"act_calls": []}

    def fake_snapshot(*, app=None, pid=None):
        # Unlisted bundle id → app level `full` → `setvalue` is gated (the path we want to exercise).
        return {
            "ok": True,
            "bundle_id": "com.test.fake",
            "elements": [{"id": "field-1", "label": "message box"}],
        }

    def fake_act(*, app=None, pid=None, element_id=None, verb=None, text=None, **_kw):
        captured["act_calls"].append({"verb": verb, "element_id": element_id, "text": text})
        return {"ok": True, "diff": [{"id": element_id, "change": "value-set"}], "point": [1.0, 2.0]}

    monkeypatch.setattr(_actuator, "snapshot", fake_snapshot)
    monkeypatch.setattr(_actuator, "act", fake_act)
    # The cursor HUD draws an on-screen overlay — no-op it so the test never touches a display.
    if hasattr(_cursor_hud, "hud") and hasattr(_cursor_hud.hud, "update"):
        monkeypatch.setattr(_cursor_hud.hud, "update", lambda *a, **k: None)

    srv = build_server(cfg)
    tool = srv._tool_manager._tools["ui_set_value"].fn  # noqa: SLF001 — the real registered tool
    return tool, captured


async def _app_client_resolve(decision: bool, *, ready: asyncio.Event, sink: dict, timeout: float = 3.0):
    """Mirror ActuationConfirmController: subscribe, await the confirm_request, resolve the decision.
    Records the event + the latency from subscription to receipt (tiny iff the loop isn't frozen)."""
    async with events.subscribe() as sub:
        ready.set()
        t0 = time.monotonic()
        ev = await asyncio.wait_for(sub.__anext__(), timeout=timeout)
        sink["event"] = ev
        sink["latency"] = time.monotonic() - t0
        _confirm.resolve(ev["id"], approved=decision)


@pytest.mark.parametrize("decision,expect_ok", [(True, True), (False, False)])
async def test_e2e_gated_tool_confirm_round_trip(monkeypatch, decision, expect_ok):
    """The flagship TCC-free E2E: real ui_set_value → real gate → real confirm SSE → app resolves →
    (approve ⇒ actuator runs; deny ⇒ it doesn't). Proves the freeze fix at the real-tool level."""
    tool, captured = _server_with_fake_actuator(monkeypatch)
    ready = asyncio.Event()
    sink: dict = {}

    # The "app" must be subscribed before the tool checks has_subscribers().
    client = asyncio.create_task(_app_client_resolve(decision, ready=ready, sink=sink))
    await asyncio.wait_for(ready.wait(), timeout=1.0)

    raw = await asyncio.wait_for(tool(app="Lark", id="field-1", text="draft (never sent)", note="e2e"), timeout=5.0)
    await asyncio.wait_for(client, timeout=2.0)
    res = json.loads(raw)

    # The real confirm fired and reached the app FAST (loop not frozen — the C1 proof).
    assert sink["event"]["type"] == "confirm_request"
    assert sink["latency"] < 1.0, f"confirm_request delivery was slow ({sink['latency']:.1f}s) — loop frozen?"

    if expect_ok:
        assert res.get("ok") is True
        assert captured["act_calls"] == [
            {"verb": "setvalue", "element_id": "field-1", "text": "draft (never sent)"}
        ]
    else:
        assert res.get("error") == "denied"
        assert captured["act_calls"] == []  # deny ⇒ the actuator must NOT have run


async def test_e2e_no_subscriber_denies_fast_and_no_actuation(monkeypatch):
    """No app listening (no SSE subscriber) → the tool fast-denies (never a 60s hang) and never
    touches the actuator. Guards the agent-never-blocked-on-a-missing-app invariant end-to-end."""
    tool, captured = _server_with_fake_actuator(monkeypatch)
    t0 = time.monotonic()
    raw = await asyncio.wait_for(tool(app="Lark", id="field-1", text="x", note="e2e"), timeout=5.0)
    elapsed = time.monotonic() - t0
    res = json.loads(raw)
    assert res.get("error") == "denied"
    assert elapsed < 2.0, f"no-subscriber deny took {elapsed:.1f}s — should be immediate"
    assert captured["act_calls"] == []


async def test_e2e_firewall_blocks_untrusted_run_before_confirm_or_actuation(monkeypatch):
    """An untrusted .context run carries `X-Persome-Actuation: deny`. A gated tool must return
    actuation_not_permitted WITHOUT publishing a confirm_request or touching the actuator — the
    prompt-injection firewall. Proves the C1 hoist kept the check enforced on the gated path end to
    end (even with a willing subscriber present, the firewall blocks first)."""
    cfg = C.Config()
    cfg.actuation_enabled = True
    captured: dict = {"act_calls": []}
    monkeypatch.setattr(_actuator, "snapshot", lambda **k: {
        "ok": True, "bundle_id": "com.test.fake", "elements": [{"id": "field-1", "label": "box"}]})
    monkeypatch.setattr(_actuator, "act", lambda **k: (captured["act_calls"].append(k), {"ok": True})[1])
    if hasattr(_cursor_hud, "hud") and hasattr(_cursor_hud.hud, "update"):
        monkeypatch.setattr(_cursor_hud.hud, "update", lambda *a, **k: None)
    srv = build_server(cfg)

    class _Req:
        headers = {"x-persome-actuation": "deny"}

    class _ReqCtx:
        request = _Req()

    class _Ctx:
        request_context = _ReqCtx()

    monkeypatch.setattr(srv, "get_context", lambda: _Ctx())
    tool = srv._tool_manager._tools["ui_set_value"].fn

    # A subscriber IS present and would approve — so a non-empty events list would mean the firewall
    # let the request reach the confirm stage (a leak). It must stay empty.
    seen: list = []
    ready = asyncio.Event()

    async def watch():
        async with events.subscribe() as sub:
            ready.set()
            with contextlib.suppress(TimeoutError):
                seen.append(await asyncio.wait_for(sub.__anext__(), timeout=0.5))

    w = asyncio.create_task(watch())
    await asyncio.wait_for(ready.wait(), timeout=1.0)
    raw = await asyncio.wait_for(tool(app="Lark", id="field-1", text="x", note="e2e"), timeout=3.0)
    await asyncio.wait_for(w, timeout=2.0)
    res = json.loads(raw)

    assert res.get("error") == "actuation_not_permitted"
    assert captured["act_calls"] == []  # never actuated
    assert seen == []  # never even published a confirm_request — blocked before the gate


@pytest.mark.parametrize("decision,expect_ok", [(True, True), (False, False)])
async def test_e2e_freeform_send_gated_round_trip(monkeypatch, decision, expect_ok):
    """The FREEFORM gated path (ui_key 'enter' in a messaging app = Send) goes through a different
    gate (_freeform_via_gate → classify_freeform) than ui_set_value. This is the most safety-critical
    place-never-send verb, and had no E2E coverage. Drive the real ui_key tool: approve ⇒ the key
    fires; deny ⇒ the Send is blocked (the key never fires)."""
    cfg = C.Config()
    cfg.actuation_enabled = True
    captured: dict = {"key_calls": []}

    def fake_key(*args, **kwargs):
        captured["key_calls"].append({"keys": args[0] if args else kwargs.get("keys")})
        return {"ok": True, "diff": [{"id": "x", "change": "appeared"}], "point": [1.0, 2.0]}

    monkeypatch.setattr(_actuator, "key", fake_key)
    if hasattr(_cursor_hud, "hud") and hasattr(_cursor_hud.hud, "update"):
        monkeypatch.setattr(_cursor_hud.hud, "update", lambda *a, **k: None)
    srv = build_server(cfg)
    tool = srv._tool_manager._tools["ui_key"].fn

    ready = asyncio.Event()
    sink: dict = {}
    client = asyncio.create_task(_app_client_resolve(decision, ready=ready, sink=sink))
    await asyncio.wait_for(ready.wait(), timeout=1.0)

    # "enter" in Lark = Send → gated → confirm round-trip.
    raw = await asyncio.wait_for(tool(app="Lark", keys="enter", note="send the message"), timeout=5.0)
    await asyncio.wait_for(client, timeout=2.0)
    res = json.loads(raw)

    assert sink["event"]["type"] == "confirm_request"   # the freeform gate fired a real confirm
    assert sink["latency"] < 1.0                          # loop not frozen (offload works here too)
    if expect_ok:
        assert res.get("ok") is True
        assert len(captured["key_calls"]) == 1           # approve ⇒ the Send key fired
    else:
        assert res.get("error") == "denied"
        assert captured["key_calls"] == []               # deny ⇒ the Send was blocked (place-never-send)

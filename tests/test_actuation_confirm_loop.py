"""Regression: a confirm-gated actuation must not freeze the daemon's asyncio event loop.

The `mcp` SDK runs a *sync* tool body directly on the event loop (func_metadata does
`return fn(...)`, no thread offload). A gated verb blocks in `confirm.request` on a 60s
`threading.Event.wait`; on the loop that froze the WHOLE daemon for up to 60s, so the SSE
`confirm_request` couldn't be delivered and the app's approve POST couldn't be serviced —
the action ALWAYS timed out → deny. The fix makes the gated tools `async` + offload the
blocking body via `asyncio.to_thread`. These tests guard both the structure (tools are async)
and the behaviour (the loop stays responsive, so an approve during the wait is honoured).
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect

from persome import config as C
from persome import events
from persome.actuation import confirm as _confirm
from persome.mcp.server import build_server

# The side-effecting tools — each blocks in `confirm.request`, so each MUST run off the loop.
GATED_TOOLS = ("ui_click", "ui_click_xy", "ui_set_value", "ui_type", "ui_key", "ui_open_app")
# Read-only tools that shell out to the actuator subprocess / on-device OCR — they don't confirm,
# but they DO block the loop on their subprocess/OCR (≤10s / seconds), so they must offload too.
READONLY_OFFLOAD_TOOLS = ("ui_snapshot", "ui_find", "ui_ocr_locate", "ui_activate")


def _build_actuation_server():
    cfg = C.Config()
    cfg.actuation_enabled = True  # field on Config (config.py); off by default
    return build_server(cfg)


def test_gated_actuation_tools_are_async():
    """Each gated tool must be a coroutine function — that is what lets it offload the blocking
    confirm wait off the event loop. A regression to a plain `def` reintroduces the 60s freeze."""
    srv = _build_actuation_server()
    tools = srv._tool_manager._tools  # noqa: SLF001 — test introspection of registered tools
    for name in GATED_TOOLS:
        assert name in tools, f"{name} not registered (actuation tools missing)"
        fn = tools[name].fn
        assert inspect.iscoroutinefunction(fn), f"{name} must be async (offloads the confirm wait)"


def test_readonly_actuator_tools_are_async():
    """The read-only tools that shell out to the actuator/OCR must ALSO be async — running their
    subprocess on the loop blocks the daemon (the same freeze class as the gated tools, just ≤10s
    not 60s). Guards that the M4 offload didn't regress back to a plain def."""
    srv = _build_actuation_server()
    tools = srv._tool_manager._tools  # noqa: SLF001
    for name in READONLY_OFFLOAD_TOOLS:
        assert name in tools, f"{name} not registered"
        assert inspect.iscoroutinefunction(tools[name].fn), f"{name} must offload (be async)"


def test_pure_tool_stays_sync():
    """A genuinely PURE tool (no subprocess/OCR — ui_app_guide just reads bundled skill markdown)
    needn't be async — guards that the offload was scoped to tools that actually block, not
    blanket-applied to every tool."""
    srv = _build_actuation_server()
    fn = srv._tool_manager._tools["ui_app_guide"].fn  # noqa: SLF001
    assert not inspect.iscoroutinefunction(fn)


async def test_gated_confirm_offload_keeps_loop_responsive():
    """The behavioural invariant: when the blocking `confirm.request` runs off the loop (the fix's
    `asyncio.to_thread`), the loop keeps running — it delivers the `confirm_request` event AND
    services the approve, so the confirm returns *approved*. On the pre-fix path (request on the
    loop) the loop would be frozen and this would time out → deny."""
    async with events.subscribe() as sub:
        # Mirror the fixed tool: offload the blocking confirm wait to a worker thread.
        task = asyncio.create_task(
            asyncio.to_thread(_confirm.request, "verify draft", app="App", verb="setvalue", timeout=5.0)
        )
        # The loop MUST stay responsive: receive the confirm_request and approve it mid-wait.
        ev = await asyncio.wait_for(sub.__anext__(), timeout=2.0)
        assert ev.get("type") == "confirm_request"
        cid = ev.get("id")
        assert cid and _confirm.resolve(cid, approved=True) is True
        approved = await asyncio.wait_for(task, timeout=2.0)
        assert approved is True  # honoured because the loop was never frozen


def test_config_actuation_field_exists():
    """Sanity: `actuation_enabled` is a real Config field (so the toggle survives a round-trip)."""
    names = {f.name for f in dataclasses.fields(C.Config)}
    assert "actuation_enabled" in names


# ── actuation_confirm route: body parsing must never 500 (fail-safe deny) ─────


class _FakeReq:
    """Minimal stand-in for starlette Request — the route only awaits `.json()`."""

    def __init__(self, *, exc: Exception | None = None, payload: dict | None = None):
        self._exc = exc
        self._payload = payload

    async def json(self):
        if self._exc is not None:
            raise self._exc
        return self._payload


async def test_actuation_confirm_route_survives_client_disconnect():
    """A racy/cancelled approve POST raises starlette.ClientDisconnect mid-body. The route must
    treat it as 'no decision' (safe deny) and return 200-shaped data, never 500."""
    from starlette.requests import ClientDisconnect

    from persome.api.routes import actuation_confirm

    resp = await actuation_confirm("no-such-id", _FakeReq(exc=ClientDisconnect()))
    assert resp.data["approved"] is False
    assert resp.data["matched"] is False


async def test_actuation_confirm_route_survives_malformed_body():
    """Malformed/absent JSON also falls back to a safe deny rather than erroring."""
    from persome.api.routes import actuation_confirm

    resp = await actuation_confirm("no-such-id", _FakeReq(exc=ValueError("not json")))
    assert resp.data["approved"] is False


async def test_actuation_confirm_route_reads_valid_decision():
    """A well-formed body is parsed (approved echoed); matched is False for an unknown id."""
    from persome.api.routes import actuation_confirm

    resp = await actuation_confirm("no-such-id", _FakeReq(payload={"approved": True}))
    assert resp.data["approved"] is True
    assert resp.data["matched"] is False

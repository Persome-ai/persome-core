"""Regression #274: per-app skill disclosure is scoped to the MCP SESSION, not the daemon lifetime.

The daemon is a long-lived launchd singleton (port 8773) that every concurrently-dispatched agent run
shares through ONE FastMCP server instance. The first-focus skill dedup set used to be a single
daemon-wide `set[str]` built in the `build_server()` closure — so an app's guide was injected exactly
ONCE EVER: only the very first agent run after the daemon booted got it, and every later run (a
different task, even days apart) focusing the same app got nothing. That breaks the per-run "first
focus" design and ui_activate's docstring promise ("The FIRST time you focus an app … that guide is
appended here").

The fix keys the dedup set by the streamable-HTTP `mcp-session-id` header (one per client connection,
i.e. per agent run), the same request-context source `_actuation_denied` reads. Two distinct sessions
focusing the same app must BOTH receive the guide; a second focus WITHIN one session must not.
"""

from __future__ import annotations

from types import SimpleNamespace

from persome import config as C
from persome.actuation import actuator as _actuator
from persome.actuation import skills as _skills
from persome.mcp.server import build_server


def _build_actuation_server():
    cfg = C.Config()
    cfg.actuation_enabled = True
    return build_server(cfg)


class _FakeReq:
    def __init__(self, session_id: str | None):
        headers = {}
        if session_id is not None:
            headers["mcp-session-id"] = session_id
        # match the lowercase-keyed dict-like access the server uses (.get)
        self.headers = headers


def _patch_session(monkeypatch, srv, session_id: str | None) -> None:
    """Make `server.get_context()` report `session_id` for the current request (as the streamable-HTTP
    transport would via the `mcp-session-id` header). `None` simulates a transport with no header."""
    ctx = SimpleNamespace(request_context=SimpleNamespace(request=_FakeReq(session_id)))
    monkeypatch.setattr(srv, "get_context", lambda: ctx)


def _ui_activate(srv):
    return srv._tool_manager._tools["ui_activate"].fn  # noqa: SLF001 — introspect registered tool


# Pick a real bundled-skill app so guide_for() resolves (skills/*.md ship WeChat, Feishu, …).
_APP = "WeChat"


def _has_guide(result: str) -> bool:
    return "operation guide (follow it)" in result


def test_guide_resolvable_for_test_app():
    """Sanity: the app we drive the test with actually has a bundled guide (else the test is vacuous)."""
    assert _skills.guide_for(_APP) is not None


async def test_two_mcp_sessions_each_get_guide_on_first_focus(monkeypatch):
    """RED before the fix, GREEN after: two DISTINCT MCP sessions focus the same app in sequence on the
    SAME long-lived daemon — both must receive the guide. The old daemon-wide set gave it only to the
    first session (the cross-run starvation bug)."""
    srv = _build_actuation_server()
    # Don't touch the real Mac: stub the actuator so ui_activate returns a benign payload.
    monkeypatch.setattr(_actuator, "activate", lambda app: {"ok": True, "app": app})
    activate = _ui_activate(srv)

    # Session A — first focus of WeChat → guide injected.
    _patch_session(monkeypatch, srv, "session-A")
    res_a = await activate(_APP)
    assert _has_guide(res_a), "session A must get the guide on first focus"

    # Session B — a different agent run on the SAME daemon, first focus of the SAME app.
    _patch_session(monkeypatch, srv, "session-B")
    res_b = await activate(_APP)
    assert _has_guide(res_b), "session B must ALSO get the guide (regression #274: it didn't)"


async def test_second_focus_within_one_session_does_not_repeat(monkeypatch):
    """Progressive disclosure is preserved: focusing the same app AGAIN within ONE session does not
    re-inject the guide (that's the whole point — pay for it once per run, not every ui_activate)."""
    srv = _build_actuation_server()
    monkeypatch.setattr(_actuator, "activate", lambda app: {"ok": True, "app": app})
    activate = _ui_activate(srv)

    _patch_session(monkeypatch, srv, "session-A")
    first = await activate(_APP)
    second = await activate(_APP)
    assert _has_guide(first)
    assert not _has_guide(second), "the same session must not re-inject the guide on a repeat focus"


async def test_no_session_header_still_dedups_within_that_bucket(monkeypatch):
    """When the transport supplies no `mcp-session-id` (in-process tests / a header-less transport),
    all such requests share one sentinel bucket — so the guide is still injected exactly once, never
    repeated. Guards that the fallback doesn't either spam every call or never inject."""
    srv = _build_actuation_server()
    monkeypatch.setattr(_actuator, "activate", lambda app: {"ok": True, "app": app})
    activate = _ui_activate(srv)

    _patch_session(monkeypatch, srv, None)
    first = await activate(_APP)
    second = await activate(_APP)
    assert _has_guide(first)
    assert not _has_guide(second)

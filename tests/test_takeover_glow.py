"""Offline tests for the takeover glow: the session state machine (`actuation/takeover.py`)
and the HUD glow pipe (`actuation/cursor_hud.py`). Spec:
docs/superpowers/specs/2026-07-02-takeover-glow-overlay-design.md §2 (two-axis MECE states)."""

from __future__ import annotations

import json

from persome.actuation.takeover import STALE_SECONDS, TakeoverTracker

# ── state machine: the §2 table, cell by cell ────────────────────────────────


def test_read_tool_starts_observing():
    t = TakeoverTracker()
    p = t.on_tool("s1", app="飞书", kind="read")
    assert p == {"app": "飞书", "pid": 0, "state": "observing", "note": "", "task_id": ""}


def test_act_goes_executing_with_note_and_point():
    t = TakeoverTracker()
    t.on_tool("s1", app="飞书", kind="read")
    p = t.on_tool("s1", app="飞书", kind="act", note="正在填写审批表单", point=[10, 20], pid=42)
    assert p["state"] == "executing"
    assert p["note"] == "正在填写审批表单"
    assert p["point"] == [10.0, 20.0]
    assert p["pid"] == 42


def test_read_never_downgrades_executing():
    # The agent re-reading the UI mid-task is still a takeover in progress.
    t = TakeoverTracker()
    t.on_tool("s1", app="飞书", kind="act", note="点击发送")
    p = t.on_tool("s1", app="飞书", kind="read")
    assert p["state"] == "executing"
    assert p["note"] == "点击发送"  # note survives the read


def test_confirm_begin_flips_to_awaiting_with_summary_note():
    t = TakeoverTracker()
    t.on_tool("s1", app="飞书", kind="act", note="步骤", task_id="T1")
    p = t.confirm_begin("s1", summary="发送消息给张三")
    assert p["state"] == "awaiting_confirm"
    assert p["note"] == "发送消息给张三"  # the badge shows WHAT wants approval
    assert p["task_id"] == "T1"


def test_confirm_end_returns_to_executing_either_way():
    t = TakeoverTracker()
    t.on_tool("s1", app="飞书", kind="act")
    t.confirm_begin("s1", summary="x")
    p = t.confirm_end("s1")
    assert p["state"] == "executing"


def test_act_during_awaiting_confirm_keeps_confirm_state():
    # The gated act itself re-reports through on_tool while blocked — the orange pulse must not
    # flicker back to executing under the user's decision.
    t = TakeoverTracker()
    t.on_tool("s1", app="飞书", kind="act")
    t.confirm_begin("s1", summary="x")
    p = t.on_tool("s1", app="飞书", kind="act", note="重报")
    assert p["state"] == "awaiting_confirm"


def test_confirm_on_unknown_session_is_none():
    t = TakeoverTracker()
    assert t.confirm_begin("nope", summary="x") is None
    assert t.confirm_end("nope") is None


def test_confirm_end_without_begin_is_none():
    t = TakeoverTracker()
    t.on_tool("s1", app="飞书", kind="act")
    assert t.confirm_end("s1") is None  # not awaiting → nothing to restore


def test_no_app_no_pid_is_no_show():
    t = TakeoverTracker()
    assert t.on_tool("s1", app="", kind="read") is None
    assert t.sessions() == []


# ── begin_run: the RUN-lifecycle driver (voice-entry take-over target) ───────


def test_begin_run_creates_executing_session_keyed_by_task():
    t = TakeoverTracker()
    p = t.begin_run("T1", app="飞书", bundle_id="com.electron.lark", pid=42, note="回复张三")
    assert p["state"] == "executing"
    assert p["app"] == "com.electron.lark"  # bundle id preferred for helper resolution
    assert p["pid"] == 42
    assert p["note"] == "回复张三"
    assert p["task_id"] == "T1"


def test_begin_run_is_an_idempotent_keepalive():
    t = TakeoverTracker()
    t.begin_run("T1", app="飞书", note="回复张三")
    p = t.begin_run("T1", app="飞书", note="回复张三")  # the ~90s re-post
    assert p["state"] == "executing" and len(t.sessions()) == 1


def test_begin_run_note_never_clobbers_a_ui_step_note():
    # The run title seeds the badge; once set, later keepalives keep whatever is there
    # (ui_* step notes ride the mcp-session, run-session note stays the title).
    t = TakeoverTracker()
    t.begin_run("T1", app="飞书", note="回复张三")
    p = t.begin_run("T1", app="飞书", note="别的标题")
    assert p["note"] == "回复张三"


def test_begin_run_keepalive_yields_to_a_live_confirm():
    # While the run's ui_* session is mid-approval (orange pulse), the keepalive must not
    # emit an executing payload over it (last-writer-wins at the helper).
    t = TakeoverTracker()
    t.begin_run("T1", app="飞书")
    t.on_tool("mcp-s1", app="飞书", kind="act", task_id="T1")
    t.confirm_begin("mcp-s1", summary="发送")
    assert t.begin_run("T1", app="飞书") is None
    t.confirm_end("mcp-s1")
    assert t.begin_run("T1", app="飞书")["state"] == "executing"


def test_begin_run_requires_task_and_some_identity():
    t = TakeoverTracker()
    assert t.begin_run("", app="飞书") is None
    assert t.begin_run("T1") is None


def test_end_run_closes_both_run_and_mcp_sessions():
    t = TakeoverTracker()
    t.begin_run("T1", app="飞书")
    t.on_tool("mcp-s1", app="飞书", kind="act", task_id="T1")
    payloads = t.end_run("T1", outcome="done")
    assert len(payloads) == 2 and all(p["state"] == "done" for p in payloads)
    assert t.sessions() == []


# ── end_run: terminal flash + forget ─────────────────────────────────────────


def test_end_run_flashes_done_and_forgets():
    t = TakeoverTracker()
    t.on_tool("s1", app="飞书", kind="act", task_id="T1")
    t.on_tool("s2", app="Safari", kind="act", task_id="T1")  # same run, second MCP session
    t.on_tool("s3", app="微信", kind="act", task_id="T2")
    payloads = t.end_run("T1", outcome="done")
    assert sorted(p["app"] for p in payloads) == ["Safari", "飞书"]
    assert all(p["state"] == "done" for p in payloads)
    assert [s["task_id"] for s in t.sessions()] == ["T2"]  # T1 forgotten, T2 untouched


def test_end_run_failed_and_unknown_outcome_maps_to_failed():
    t = TakeoverTracker()
    t.on_tool("s1", app="飞书", kind="act", task_id="T1")
    assert t.end_run("T1", outcome="cancelled")[0]["state"] == "failed"


def test_end_run_unknown_or_empty_task_is_idempotent_noop():
    # The app posts blind for EVERY finished run; most never touched actuation. And codex runs
    # carry no per-run task id at all (global MCP registration) → their glow relies on idle timeout.
    t = TakeoverTracker()
    t.on_tool("s1", app="飞书", kind="act", task_id="")  # codex-shaped session
    assert t.end_run("does-not-exist", outcome="done") == []
    assert t.end_run("", outcome="done") == []
    assert len(t.sessions()) == 1  # the codex session survives (idle timeout owns it)


# ── prune: the registry never grows unboundedly on a long-lived daemon ───────


def test_stale_sessions_pruned_on_next_tool():
    clock = {"t": 0.0}
    t = TakeoverTracker(now=lambda: clock["t"])
    t.on_tool("old", app="飞书", kind="act", task_id="T1")
    clock["t"] = STALE_SECONDS + 1
    t.on_tool("new", app="Safari", kind="read")
    assert [s["app"] for s in t.sessions()] == ["Safari"]


# ── HUD glow pipe ────────────────────────────────────────────────────────────


def _fake_hud(monkeypatch, captured):
    import subprocess as sp

    from persome.actuation import actuator as actuator_mod
    from persome.actuation.cursor_hud import CursorHUD

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
    return CursorHUD(idle_seconds=99, glow_idle_seconds=999)


def test_hud_glow_writes_wrapped_json_and_tracks_active(monkeypatch):
    captured: list[str] = []
    h = _fake_hud(monkeypatch, captured)
    h.glow({"app": "飞书", "pid": 1, "state": "executing", "note": "n", "task_id": "T"})
    msg = json.loads(captured[-1])
    assert msg["glow"]["state"] == "executing" and msg["glow"]["app"] == "飞书"
    assert h._glow_active is True  # long idle window while the halo is up

    h.glow({"app": "飞书", "pid": 1, "state": "done", "note": "", "task_id": "T"})
    assert h._glow_active is False  # terminal → back to the short cursor idle

    h.clear_glow()
    assert json.loads(captured[-1]) == {"glow": {"clear": True}}
    assert h._glow_active is False
    h.stop()


def test_hud_glow_noop_without_binary(monkeypatch):
    from persome.actuation import actuator as actuator_mod
    from persome.actuation.cursor_hud import CursorHUD

    monkeypatch.setattr(actuator_mod, "_resolve_actuator_path", lambda: None)
    h = CursorHUD()
    h.glow({"state": "executing"})  # must not raise / spawn anything
    h.clear_glow()
    assert h._proc is None


def test_hud_glow_skips_empty_payload():
    from persome.actuation.cursor_hud import CursorHUD

    h = CursorHUD()
    h.glow({})
    assert h._proc is None

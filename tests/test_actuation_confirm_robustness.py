"""Fault-injection / robustness for the actuation confirm subsystem.

Companion to test_actuation_confirm_loop.py (which proves the gated tools run off the event
loop). Here we hammer the confirm primitive itself for availability invariants the daemon must
hold: a missing app can't hang the agent, concurrent confirms never cross-attribute, an
unanswered confirm times out AND cleans up (no leak), and a stale/duplicate resolve is a safe
no-op. All pure + offline (confirm + events only; no actuator, no network).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from persome import events
from persome.actuation import confirm as _confirm


def test_no_subscriber_denies_immediately_not_after_timeout():
    """No SSE subscriber (app not running) → deny NOW, never stall the agent for the full
    timeout. Guards the `events.has_subscribers()` fast-deny path."""
    t0 = time.monotonic()
    assert _confirm.request("x", app="A", verb="setvalue", timeout=5.0) is False
    assert time.monotonic() - t0 < 1.0  # immediate, not ~5s


def test_resolve_unknown_or_duplicate_id_is_safe_noop():
    """Resolving an id that was never pending (or already resolved) returns False and never
    raises — a racy double-POST can't corrupt state."""
    assert _confirm.resolve("never-pending", approved=True) is False
    assert _confirm.resolve("never-pending", approved=False) is False


async def test_concurrent_confirms_resolve_independently_no_cross_attribution():
    """Two gated actions pending at once get distinct ids and resolve independently — approving
    one must not approve the other (the cross-attribution hazard)."""
    async with events.subscribe() as sub:
        t_one = asyncio.create_task(
            asyncio.to_thread(_confirm.request, "one", app="A", verb="setvalue", timeout=5.0)
        )
        t_two = asyncio.create_task(
            asyncio.to_thread(_confirm.request, "two", app="B", verb="key", timeout=5.0)
        )
        by_summary: dict[str, str] = {}
        for _ in range(2):
            ev = await asyncio.wait_for(sub.__anext__(), timeout=2.0)
            assert ev.get("type") == "confirm_request"
            by_summary[ev["summary"]] = ev["id"]
        assert len(set(by_summary.values())) == 2  # distinct cids — no collision / cross-attribution
        # Approve "one", deny "two" — each decision must reach only its own waiter.
        assert _confirm.resolve(by_summary["one"], approved=True) is True
        assert _confirm.resolve(by_summary["two"], approved=False) is True
        assert await asyncio.wait_for(t_one, timeout=2.0) is True
        assert await asyncio.wait_for(t_two, timeout=2.0) is False


async def test_unanswered_confirm_times_out_and_cleans_up():
    """A confirm nobody answers denies on timeout AND removes its pending entry (no unbounded
    leak across the daemon's lifetime)."""
    async with events.subscribe() as sub:
        task = asyncio.create_task(
            asyncio.to_thread(_confirm.request, "slow", app="A", verb="setvalue", timeout=0.4)
        )
        ev = await asyncio.wait_for(sub.__anext__(), timeout=2.0)
        cid = ev["id"]
        assert cid in _confirm.pending_ids()  # registered while waiting
        assert await asyncio.wait_for(task, timeout=2.0) is False  # timed out → deny
        assert cid not in _confirm.pending_ids()  # cleaned up — no leak


# ── actuator subprocess: any failure returns a clean error dict (never propagates) ───


def test_actuator_run_returns_clean_error_on_subprocess_failure(monkeypatch):
    """A subprocess failure (OSError: binary vanished / lost +x, or a timeout) must surface as a
    clean error dict, not a raised exception — otherwise a gated tool running in the thread pool
    would propagate it. Guards the broadened except in actuator._run."""
    from persome.actuation import actuator

    monkeypatch.setattr(actuator, "_resolve_actuator_path", lambda: Path("/nonexistent/mac-ax-actuator"))

    def _boom(*_a, **_k):
        raise OSError("binary vanished mid-spawn")

    monkeypatch.setattr(actuator.subprocess, "run", _boom)
    assert actuator._run(["snapshot"]) == {"ok": False, "error": "actuator_failed"}


def test_actuator_run_unavailable_when_no_binary(monkeypatch):
    """No actuator binary on the box → a clean 'unavailable' dict, never a crash."""
    from persome.actuation import actuator

    monkeypatch.setattr(actuator, "_resolve_actuator_path", lambda: None)
    assert actuator._run(["snapshot"]) == {"ok": False, "error": "actuator_unavailable"}

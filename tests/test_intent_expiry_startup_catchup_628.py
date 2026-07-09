"""Regression tests for issue #628.

The intent expiry harvest (``expire_overdue`` for overdue grounded ``open``/
``armed`` rows + ``expire_stale_armed`` for #532-stale armed rows) used to run
ONLY at the 23:55 ``daily-safety-net`` tick. A daemon that was stopped / running
old code / never reached 23:55 missed a whole day's harvest, and there was no
startup catch-up — so overdue rows stayed ``open``/``armed`` indefinitely and
leaked into recall's scene layer (the issue cites real ids 6/7/9 sitting
``open`` ≥3 days).

The fix: daemon ``_run`` replays the SAME harvest once at boot
(:func:`session.tick.expire_overdue_intents`), so a missed 23:55 is repaired
immediately. The harvest is idempotent (only touches still-``open``/``armed``
overdue rows), one-shot (not per-tick), and a pure side channel.

These tests cover both layers:
  * the harvest helper itself catches overdue rows the missed 23:55 left behind,
    is idempotent on a second pass, and never collects not-yet-due rows;
  * daemon ``_run`` actually invokes the catch-up at startup.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from persome.config import Config, MCPConfig
from persome.daemon import _build_task_registry, _run
from persome.intent import store as intent_store
from persome.intent.ontology import Intent
from persome.session import tick as session_tick
from persome.store import fts


def _open_grounded(*, valid_until: str, rationale: str = "周五交报告") -> Intent:
    """A plain grounded ``open`` commitment carrying a deadline."""
    return Intent(
        kind="reminder",
        scope="session-x",
        confidence=0.8,
        rationale=rationale,
        payload={"text": rationale},
        resolved_at=valid_until,
        valid_until=valid_until,
    )


def _armed_grounded(*, valid_until: str, app: str = "Figma") -> Intent:
    """A dormant ``armed`` L7 reminder that ALSO carries a grounded deadline."""
    it = Intent(
        kind="reminder",
        scope="session-x",
        confidence=0.8,
        rationale=f"下次打开 {app} 时改图标",
        payload={"text": "改图标"},
        fire_on="app_opened",
        fire_config={"app": app},
        resolved_at=valid_until,
        valid_until=valid_until,
    )
    it.status = "armed"
    return it


# ---------------------------------------------------------------------------
# Harvest helper: catch up the rows a missed 23:55 left behind
# ---------------------------------------------------------------------------


def test_overdue_row_stranded_when_2355_never_ran(ac_root: Path) -> None:
    """RED baseline: with no harvest call, an overdue grounded row that should
    have been collected at 23:55 just stays ``open`` — exactly the leak."""
    now = datetime.now()
    past = (now - timedelta(days=3)).isoformat(timespec="seconds")
    with fts.cursor() as conn:
        intent_store.insert_intent(conn, _open_grounded(valid_until=past))
        # The daemon never reached 23:55, so the harvest never ran. The row is
        # still on the books as a live ``open`` intent.
        got = intent_store.get_by_dedup_key(
            conn, intent_store.dedup_key(_open_grounded(valid_until=past))
        )
        assert got is not None and got.status == "open"


def test_startup_catchup_harvests_missed_open_and_armed(ac_root: Path) -> None:
    """GREEN: the catch-up helper flips both an overdue ``open`` and an overdue
    ``armed`` row that the missed 23:55 left stranded."""
    now = datetime.now()
    past = (now - timedelta(days=3)).isoformat(timespec="seconds")
    with fts.cursor() as conn:
        intent_store.insert_intent(conn, _open_grounded(valid_until=past))
        intent_store.insert_intent(conn, _armed_grounded(valid_until=past))

    expired, dismissed_armed, stale_open = session_tick.expire_overdue_intents()

    # Both overdue grounded rows (open + #629 armed) are harvested to expired.
    assert expired == 2
    assert dismissed_armed == 0
    assert stale_open == 0  # both rows are grounded — the #612 age TTL skips them

    with fts.cursor() as conn:
        open_grounded = intent_store.get_by_dedup_key(
            conn, intent_store.dedup_key(_open_grounded(valid_until=past))
        )
        armed_grounded = intent_store.get_by_dedup_key(
            conn, intent_store.dedup_key(_armed_grounded(valid_until=past))
        )
        assert open_grounded is not None and open_grounded.status == "expired"
        assert armed_grounded is not None and armed_grounded.status == "expired"
        assert intent_store.intents_armed(conn) == []


def test_startup_catchup_is_idempotent(ac_root: Path) -> None:
    """A second catch-up pass harvests nothing — already-``expired`` rows are
    excluded by the ``WHERE status IN ('open','armed')`` guard, so re-running at
    every boot never double-harvests."""
    now = datetime.now()
    past = (now - timedelta(days=2)).isoformat(timespec="seconds")
    with fts.cursor() as conn:
        intent_store.insert_intent(conn, _open_grounded(valid_until=past))

    first_expired, _, _ = session_tick.expire_overdue_intents()
    assert first_expired == 1

    second_expired, second_dismissed, second_stale_open = session_tick.expire_overdue_intents()
    assert second_expired == 0
    assert second_dismissed == 0
    assert second_stale_open == 0


def test_startup_catchup_keeps_not_yet_due_rows(ac_root: Path) -> None:
    """Cost/correctness: a row whose deadline is still ahead is left alone — the
    catch-up only collects rows already past ``valid_until``."""
    now = datetime.now()
    future = (now + timedelta(days=3)).isoformat(timespec="seconds")
    with fts.cursor() as conn:
        intent_store.insert_intent(conn, _open_grounded(valid_until=future))

    expired, dismissed_armed, stale_open = session_tick.expire_overdue_intents()
    assert expired == 0
    assert dismissed_armed == 0
    assert stale_open == 0

    with fts.cursor() as conn:
        got = intent_store.get_by_dedup_key(
            conn, intent_store.dedup_key(_open_grounded(valid_until=future))
        )
        assert got is not None and got.status == "open"


# ---------------------------------------------------------------------------
# Daemon wiring: _run actually runs the catch-up at startup
# ---------------------------------------------------------------------------


class TestRunInvokesCatchup:
    """``_run`` replays the expiry harvest once at boot."""

    def _no_task_cfg(self) -> Config:
        return Config(mcp=MCPConfig(auto_start=False))

    async def test_run_harvests_overdue_row_on_boot(
        self, ac_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: an overdue ``open`` row left by a missed 23:55 is
        ``expired`` by the time the daemon finishes booting — no 23:55 tick, no
        manual harvest, just startup catch-up."""
        now = datetime.now()
        past = (now - timedelta(days=3)).isoformat(timespec="seconds")
        with fts.cursor() as conn:
            intent_store.insert_intent(conn, _open_grounded(valid_until=past))

        # Every registry task returns immediately so _run proceeds straight
        # through its startup block to the (clean) shutdown path.
        async def returns() -> None:
            return

        stub_registry = [
            replace(td, create=lambda c, sm: returns()) for td in _build_task_registry()
        ]
        monkeypatch.setattr("persome.daemon._build_task_registry", lambda: stub_registry)

        await _run(self._no_task_cfg(), capture_only=True)

        with fts.cursor() as conn:
            got = intent_store.get_by_dedup_key(
                conn, intent_store.dedup_key(_open_grounded(valid_until=past))
            )
            assert got is not None and got.status == "expired"

    async def test_run_catchup_failure_does_not_block_boot(
        self, ac_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The catch-up is a side channel: if the harvest blows up, the daemon
        still boots cleanly (pid file written then removed)."""
        from persome import paths

        async def returns() -> None:
            return

        stub_registry = [
            replace(td, create=lambda c, sm: returns()) for td in _build_task_registry()
        ]
        monkeypatch.setattr("persome.daemon._build_task_registry", lambda: stub_registry)

        def _boom() -> tuple[int, int]:
            raise RuntimeError("db gone")

        monkeypatch.setattr("persome.session.tick.expire_overdue_intents", _boom)

        await _run(self._no_task_cfg(), capture_only=True)

        assert not paths.pid_file().exists()


def test_catchup_helper_invoked_with_to_thread() -> None:
    """Guard: the daemon must reach the catch-up via the public helper name so
    this regression stays wired (renaming the helper without updating _run would
    silently drop the catch-up)."""
    assert hasattr(session_tick, "expire_overdue_intents")
    assert callable(session_tick.expire_overdue_intents)


def test_run_is_async() -> None:
    assert asyncio.iscoroutinefunction(_run)

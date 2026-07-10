"""Daemon wiring for the D2 schema miner: the ``schema-tick`` daily tick.

Covers the three things that make the招牌 capability self-driving:
  1. ``cfg.schema.enabled = False`` → the task is absent from the registry AND
     ``run_schema_tick`` returns immediately without scheduling work.
  2. ``cfg.schema.enabled = True`` → the registry carries ``schema-tick`` in full
     mode but never in capture-only mode (it's an LLM processing task).
  3. ``run_schema_tick`` actually drives ``schema_miner_stage.mine_schemas_for_user``
     when its scheduled moment arrives — verified by short-circuiting the
     local-time sleep and monkeypatching the miner so the loop fires once.

The schedule loop is an unbounded ``while True``; tests never run it raw. (2)
asserts pure wiring on the registry; (3) replaces ``_seconds_until_next_local``
with a stub that lets the first tick run, then raises ``CancelledError`` on the
post-run sleep so the loop unwinds deterministically.
"""

from __future__ import annotations

import asyncio

import pytest

from persome.config import Config, SchemaConfig
from persome.daemon import _build_task_registry
from persome.session import tick as session_tick


def _enabled_names(cfg: Config, capture_only: bool = False) -> set[str]:
    return {td.name for td in _build_task_registry() if td.enabled(cfg, capture_only)}


# ── (1) + (2): registry gating ───────────────────────────────────────────────


def test_schema_tick_requires_flag_and_full_mode() -> None:
    cfg = Config(schema=SchemaConfig(enabled=True))
    assert "schema-tick" in _enabled_names(cfg)
    assert "schema-tick" not in _enabled_names(cfg, capture_only=True)


def test_schema_tick_absent_when_disabled() -> None:
    cfg = Config(schema=SchemaConfig(enabled=False))
    assert "schema-tick" not in _enabled_names(cfg)


def test_schema_config_defaults_on_just_after_midnight() -> None:
    # Default-on so the capability self-drives; scheduled after the daily safety
    # net so it consumes freshly classified facts.
    sc = SchemaConfig()
    assert sc.enabled is True
    assert sc.daily_tick_hour == 0
    assert sc.daily_tick_minute == 15


# ── (1): disabled tick returns immediately ───────────────────────────────────


async def test_run_schema_tick_returns_immediately_when_disabled(monkeypatch) -> None:
    cfg = Config(schema=SchemaConfig(enabled=False))

    # If the loop were entered it would call the schedule helper; a disabled tick
    # must early-return before touching it.
    def _boom(*_a, **_k):  # pragma: no cover - must not be reached
        raise AssertionError("disabled schema tick must not schedule")

    monkeypatch.setattr(session_tick, "_seconds_until_next_local", _boom)
    # Should complete without ever calling _boom.
    await asyncio.wait_for(session_tick.run_schema_tick(cfg), timeout=1.0)


# ── (3): enabled tick drives the miner ───────────────────────────────────────


async def test_run_schema_tick_invokes_miner(ac_root, monkeypatch) -> None:
    cfg = Config(schema=SchemaConfig(enabled=True))

    calls: list[str] = []

    def _fake_mine(_cfg, _conn, **_kw):
        calls.append("mined")
        # Return a lightweight stand-in with the attributes the tick logs.
        from persome.writer.schema_miner_stage import SchemaRunResult

        return SchemaRunResult()

    monkeypatch.setattr(session_tick.schema_miner_stage, "mine_schemas_for_user", _fake_mine)

    # First call: no wait (fire now). Second call (post-run sleep): blow up so the
    # unbounded loop unwinds deterministically instead of looping forever.
    seen = {"n": 0}

    def _fake_seconds(_hour, _minute):
        seen["n"] += 1
        if seen["n"] == 1:
            return 0.0
        raise asyncio.CancelledError

    monkeypatch.setattr(session_tick, "_seconds_until_next_local", _fake_seconds)

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(session_tick.run_schema_tick(cfg), timeout=2.0)

    assert calls == ["mined"]

"""#7 wiring — `timeline.tick` forwards the config retention flags into `cleanup_buffer`.

The actionable extended-retention feature (capture/scheduler.py) only activates because
`tick._cleanup_buffer_once` forwards `cfg.capture_extended_retention_enabled` /
`capture_actionable_retention_days`. Before this wiring the flags were inert — a dead
toggle that did nothing when set. These tests pin the forwarding so it can't regress.
"""

from __future__ import annotations

from persome import config as config_mod
from persome.timeline import tick


def _capturing_cleanup(captured: dict):
    def _fake(retention_hours, processed_before_ts=None, **kwargs):
        captured["retention_hours"] = retention_hours
        captured["processed_before_ts"] = processed_before_ts
        captured.update(kwargs)
        return {"deleted": 0, "stripped": 0, "evicted": 0}

    return _fake


def test_cleanup_once_forwards_retention_flags(ac_root, monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(tick.capture_scheduler, "cleanup_buffer", _capturing_cleanup(captured))

    cfg = config_mod.load()
    cfg.capture_extended_retention_enabled = True
    cfg.capture_actionable_retention_days = 14

    tick._cleanup_buffer_once(cfg)

    assert captured["extended_retention_enabled"] is True
    assert captured["actionable_retention_days"] == 14
    # The pre-existing kwargs still flow through unchanged.
    assert captured["retention_hours"] == cfg.capture.buffer_retention_hours
    assert "screenshot_retention_hours" in captured
    assert "max_mb" in captured


def test_cleanup_once_default_is_on(ac_root, monkeypatch) -> None:
    """Default config now ENABLES actionable extended retention (feature default-on)."""
    captured: dict = {}
    monkeypatch.setattr(tick.capture_scheduler, "cleanup_buffer", _capturing_cleanup(captured))

    tick._cleanup_buffer_once(config_mod.load())  # default config

    assert captured["extended_retention_enabled"] is True
    assert captured["actionable_retention_days"] == 7


def test_cleanup_once_honors_explicit_off(ac_root, monkeypatch) -> None:
    """An explicit config override back to off is still respected."""
    captured: dict = {}
    monkeypatch.setattr(tick.capture_scheduler, "cleanup_buffer", _capturing_cleanup(captured))

    cfg = config_mod.load()
    cfg.capture_extended_retention_enabled = False
    tick._cleanup_buffer_once(cfg)

    assert captured["extended_retention_enabled"] is False

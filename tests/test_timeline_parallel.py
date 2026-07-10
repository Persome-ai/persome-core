"""Tests for parallel timeline window processing (max_parallel_windows > 1)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from persome import config as config_mod
from persome import paths
from persome.store import fts
from persome.timeline import tick

_TZ = timezone(timedelta(hours=8))

_SIMPLE_PAYLOAD = json.dumps(
    {"entries": ["[TestApp] user did something"]},
    ensure_ascii=False,
)


def _stem(ts: datetime) -> str:
    return ts.isoformat().replace(":", "-").replace("+", "p")


def _write_capture(ts: datetime) -> None:
    payload = {
        "timestamp": ts.isoformat(),
        "schema_version": 2,
        "trigger": {"event_type": "focus"},
        "window_meta": {"app_name": "TestApp", "title": "Test", "bundle_id": "com.test"},
        "focused_element": {"role": "AXTextField", "value": "hello"},
        "visible_text": "some text",
    }
    path = paths.capture_buffer_dir() / f"{_stem(ts)}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")


def _seed_windows(base: datetime, count: int) -> None:
    """Write one capture per window for ``count`` consecutive 1-min windows."""
    for i in range(count):
        window_start = base + timedelta(minutes=i)
        _write_capture(window_start + timedelta(seconds=10))


def test_run_once_parallel_backlog(ac_root: Path, fake_llm) -> None:
    """N pending windows → N blocks produced when max_parallel_windows > 1."""
    fake_llm.set_default("timeline", _SIMPLE_PAYLOAD)

    # Seed 4 consecutive 1-min windows starting 10 min in the past.
    n = 4
    now = datetime.now().astimezone()
    base = now.replace(second=0, microsecond=0) - timedelta(minutes=n + 1)
    _seed_windows(base, n)

    # Override max_parallel_windows to 4 to exercise the parallel path.
    cfg = config_mod.load(ac_root / "config.toml")
    cfg.timeline.max_parallel_windows = 4  # type: ignore[attr-defined]

    produced = tick._run_once(cfg)

    assert produced == n

    with fts.cursor() as conn:
        rows = conn.execute("SELECT COUNT(*) FROM timeline_blocks").fetchone()
    assert rows[0] == n


def test_run_once_sequential_with_max_parallel_1(ac_root: Path, fake_llm) -> None:
    """max_parallel_windows=1 falls back to serial path; produces same result."""
    fake_llm.set_default("timeline", _SIMPLE_PAYLOAD)

    n = 3
    now = datetime.now().astimezone()
    base = now.replace(second=0, microsecond=0) - timedelta(minutes=n + 1)
    _seed_windows(base, n)

    cfg = config_mod.load(ac_root / "config.toml")
    cfg.timeline.max_parallel_windows = 1  # type: ignore[attr-defined]

    produced = tick._run_once(cfg)

    assert produced == n

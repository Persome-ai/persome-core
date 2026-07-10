"""Tests for parser telemetry persistence."""

from __future__ import annotations

from persome.store import fts
from persome.store import parser_ticks as pt


def _tick(conn, *, ts, bundle_id, outcome):  # noqa: ANN001
    pt.record_tick(conn, ts=ts, bundle_id=bundle_id, outcome=outcome)


def test_stats_counts_and_buckets(ac_root) -> None:  # noqa: ANN001
    with fts.cursor() as conn:
        # lark: 2 hit, 1 miss; an unparsed bundle: 1 fallback → total 4, hit_rate 2/4
        _tick(conn, ts="2026-06-02T10:00", bundle_id="com.electron.lark", outcome="hit")
        _tick(conn, ts="2026-06-02T10:01", bundle_id="com.electron.lark", outcome="hit")
        _tick(conn, ts="2026-06-02T10:02", bundle_id="com.electron.lark", outcome="miss")
        _tick(conn, ts="2026-06-02T10:03", bundle_id="com.apple.Safari", outcome="fallback")
        s = pt.stats(conn)
    assert s["total"] == 4
    assert s["by_outcome"] == {"hit": 2, "miss": 1, "fallback": 1}
    assert s["by_bundle"]["com.electron.lark"] == {"hit": 2, "miss": 1, "fallback": 0}
    assert s["by_bundle"]["com.apple.Safari"] == {"hit": 0, "miss": 0, "fallback": 1}
    # hit_rate = hit / total (fallback windows count as non-hits in the denominator)
    assert s["hit_rate"] == 0.5


def test_stats_window_filter(ac_root) -> None:  # noqa: ANN001
    with fts.cursor() as conn:
        _tick(conn, ts="2026-06-01T10:00", bundle_id="com.electron.lark", outcome="hit")
        _tick(conn, ts="2026-06-02T10:00", bundle_id="com.electron.lark", outcome="miss")
        # only the 2026-06-02 tick is in window
        s = pt.stats(conn, since="2026-06-02T00:00", until="2026-06-03T00:00")
    assert s["total"] == 1
    assert s["by_outcome"] == {"hit": 0, "miss": 1, "fallback": 0}
    assert s["hit_rate"] == 0.0
    assert s["since"] == "2026-06-02T00:00"
    assert s["until"] == "2026-06-03T00:00"


def test_stats_empty(ac_root) -> None:  # noqa: ANN001
    with fts.cursor() as conn:
        s = pt.stats(conn)
    assert s["total"] == 0
    assert s["by_outcome"] == {"hit": 0, "miss": 0, "fallback": 0}
    assert s["by_bundle"] == {}
    assert s["hit_rate"] == 0.0
    assert s["since"] is None
    assert s["until"] is None


def test_record_tick_returns_rowid_and_roundtrips(ac_root) -> None:  # noqa: ANN001
    with fts.cursor() as conn:
        rid = pt.record_tick(
            conn, ts="2026-06-02T10:00", bundle_id="com.electron.lark", outcome="hit"
        )
        assert rid > 0
        row = conn.execute(
            "SELECT ts, bundle_id, outcome FROM parser_ticks WHERE id = ?", (rid,)
        ).fetchone()
    assert row["ts"] == "2026-06-02T10:00"
    assert row["bundle_id"] == "com.electron.lark"
    assert row["outcome"] == "hit"


def test_prune_keeps_recent(ac_root) -> None:  # noqa: ANN001
    with fts.cursor() as conn:
        for i in range(10):
            _tick(conn, ts=f"2026-06-02T10:{i:02d}", bundle_id="com.electron.lark", outcome="hit")
        deleted = pt.prune(conn, keep=4)
        s = pt.stats(conn)
    assert deleted == 6
    assert s["total"] == 4

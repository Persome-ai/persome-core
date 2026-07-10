"""Unit tests for the attention trajectory (timeline/attention_trajectory.py).

Deterministic. Coalesce per-block loci into dwell spans: same-surface runs
merge, non-contiguous surfaces stay separate, a time gap splits a run, dwell
sums the run (excluding gaps), the representative rung is the run's
highest-confidence block. Plus a store round-trip.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from persome.store import fts
from persome.timeline import store as ts
from persome.timeline.attention_trajectory import (
    attention_trajectory,
    build_attention_trajectory,
    trajectory_summary,
)

_TZ = timezone(timedelta(hours=8))


def _blk(minute: int, surface: str, *, rung: str = "content", conf: float = 0.5, dur: int = 60):
    start = datetime(2026, 6, 18, 17, minute, tzinfo=_TZ)
    return ts.TimelineBlock(
        start_time=start,
        end_time=start + timedelta(seconds=dur),
        attention_surface=surface,
        attention_rung=rung,
        attention_confidence=conf,
    )


def test_empty_returns_empty() -> None:
    assert build_attention_trajectory([]) == []


def test_single_block_one_span() -> None:
    spans = build_attention_trajectory([_blk(0, "Docs")])
    assert len(spans) == 1
    assert spans[0].surface == "Docs"
    assert spans[0].dwell_seconds == 60
    assert spans[0].block_count == 1


def test_same_surface_merges_with_summed_dwell() -> None:
    spans = build_attention_trajectory([_blk(0, "A"), _blk(1, "A"), _blk(2, "A")])
    assert len(spans) == 1
    assert spans[0].block_count == 3
    assert spans[0].dwell_seconds == 180  # 17:00 → 17:03


def test_non_contiguous_same_surface_stays_separate() -> None:
    # A, B, A → three spans; the two A runs never merge across B.
    spans = build_attention_trajectory([_blk(0, "A"), _blk(1, "B"), _blk(2, "A")])
    assert [s.surface for s in spans] == ["A", "B", "A"]
    assert len(spans) == 3


def test_time_gap_splits_same_surface_run() -> None:
    # 17:00 (ends 17:01) then 17:05 — a 240s gap > tolerance — splits the run.
    spans = build_attention_trajectory([_blk(0, "A"), _blk(5, "A")])
    assert len(spans) == 2
    assert all(s.surface == "A" for s in spans)
    assert all(s.dwell_seconds == 60 for s in spans)  # gap not counted as dwell


def test_unsorted_input_is_sorted() -> None:
    spans = build_attention_trajectory([_blk(2, "A"), _blk(0, "A"), _blk(1, "A")])
    assert len(spans) == 1
    assert spans[0].start.minute == 0
    assert spans[0].end.minute == 3


def test_representative_rung_is_highest_confidence() -> None:
    spans = build_attention_trajectory(
        [_blk(0, "A", rung="focus", conf=0.6), _blk(1, "A", rung="editing", conf=0.9)]
    )
    assert len(spans) == 1
    assert spans[0].rung == "editing"  # the 0.9 block wins


def test_store_round_trip(ac_root: Path) -> None:
    blocks = [_blk(0, "A"), _blk(1, "A"), _blk(2, "B")]
    with fts.cursor() as conn:
        ts.ensure_schema(conn)
        for b in blocks:
            ts.insert(conn, b)
        spans = attention_trajectory(conn, since=datetime(2026, 6, 18, 16, 0, tzinfo=_TZ))
    assert [s.surface for s in spans] == ["A", "B"]
    assert spans[0].block_count == 2
    assert spans[1].block_count == 1


def test_naive_and_aware_until_filter_equivalently(ac_root: Path, monkeypatch) -> None:
    """A naive ``until`` bound must not crash and must filter like the aware one.

    Stored blocks are offset-aware (#149); the MCP entry point normalizes a naive
    bound to local tz before it reaches ``b.start_time <= until``. Pin the local
    tz to +08:00 so a naive boundary maps onto the same instant as the aware one.
    """
    import time

    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    blocks = [_blk(0, "A"), _blk(30, "B"), _blk(59, "C")]
    aware_until = datetime(2026, 6, 18, 17, 40, tzinfo=_TZ)
    naive_until = datetime(2026, 6, 18, 17, 40)  # offset-less — the crash trigger
    with fts.cursor() as conn:
        ts.ensure_schema(conn)
        for b in blocks:
            ts.insert(conn, b)
        since = datetime(2026, 6, 18, 16, 0, tzinfo=_TZ)
        from persome.mcp import server as mcp_server

        # naive bound, normalized via the MCP parse path → no TypeError
        naive_spans = attention_trajectory(
            conn, since=since, until=mcp_server._parse_iso_opt(naive_until.isoformat())
        )
        aware_spans = attention_trajectory(conn, since=since, until=aware_until)
    # 17:40 cutoff keeps A (17:00) and B (17:30), drops C (17:59)
    assert [s.surface for s in naive_spans] == ["A", "B"]
    assert [s.surface for s in aware_spans] == ["A", "B"]


def test_naive_until_passed_directly_crashes_unnormalized(ac_root: Path) -> None:
    """Regression guard: a *raw* naive ``until`` (un-normalized) still crashes the
    datetime filter — proving the fix lives in the MCP parse boundary, not here."""
    import pytest

    with fts.cursor() as conn:
        ts.ensure_schema(conn)
        ts.insert(conn, _blk(0, "A"))
        with pytest.raises(TypeError):
            attention_trajectory(
                conn,
                since=datetime(2026, 6, 18, 16, 0, tzinfo=_TZ),
                until=datetime(2026, 6, 18, 18, 0),  # naive, not normalized
            )


# --- trajectory_summary (MCP/API consumer B payload) -----------------------


def test_trajectory_summary_ranks_by_total_dwell() -> None:
    # ProjA (split: 1m + 1m = 2m total) outranks ProjB (1m); empty surface dropped.
    spans = build_attention_trajectory(
        [_blk(0, "ProjA"), _blk(1, "ProjB"), _blk(2, "ProjA"), _blk(3, "")]
    )
    summary = trajectory_summary(spans)
    surfaces = [r["surface"] for r in summary["by_dwell"]]
    assert surfaces == ["ProjA", "ProjB"]  # by total dwell, longest first
    assert summary["by_dwell"][0]["dwell_minutes"] == 2.0
    # trajectory keeps chronological order (and drops the empty-surface span)
    assert [s["surface"] for s in summary["trajectory"]] == ["ProjA", "ProjB", "ProjA"]


def test_trajectory_summary_filters_below_min_dwell() -> None:
    spans = build_attention_trajectory([_blk(0, "Brief")])  # 60s
    assert trajectory_summary(spans, min_dwell_seconds=120)["by_dwell"] == []


def test_trajectory_summary_empty() -> None:
    assert trajectory_summary([]) == {"by_dwell": [], "trajectory": []}

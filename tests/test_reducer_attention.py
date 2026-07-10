"""The reducer's dwell-aware attention block (Step-2 consumer A).

`_format_attention_trajectory` turns the per-block dominant loci into a
dwell-ranked "where the user's attention went" hint for the session reducer.
It is empty (prompt byte-identical) until the locus pipeline populates
`attention_surface`, so it is a safe additive change.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from persome.timeline import store as timeline_store
from persome.writer import session_reducer

_TZ = timezone(timedelta(hours=8))


def _blk(minute: int, surface: str, *, rung: str = "content", conf: float = 0.5):
    start = datetime(2026, 6, 18, 17, minute, tzinfo=_TZ)
    return timeline_store.TimelineBlock(
        start_time=start,
        end_time=start + timedelta(seconds=60),
        attention_surface=surface,
        attention_rung=rung,
        attention_confidence=conf,
    )


def test_attention_block_is_dwell_ranked() -> None:
    # ProjA gets 3 contiguous minutes, ProjB gets 1 → A ranks above B.
    blocks = [_blk(0, "ProjA"), _blk(1, "ProjA"), _blk(2, "ProjA"), _blk(3, "ProjB")]
    out = session_reducer._format_attention_trajectory(blocks)
    assert "Attention (" in out
    assert "~3m" in out and "~1m" in out
    assert out.index("ProjA") < out.index("ProjB")  # longest first


def test_attention_block_aggregates_non_contiguous_dwell() -> None:
    # ProjA appears twice (split by ProjB) → its dwell totals across both runs.
    blocks = [_blk(0, "ProjA"), _blk(1, "ProjB"), _blk(2, "ProjA")]
    out = session_reducer._format_attention_trajectory(blocks)
    # ProjA total = 2m (> ProjB 1m) → ranks first and appears once.
    assert out.index("ProjA") < out.index("ProjB")
    assert out.count("ProjA") == 1


def test_attention_block_filters_momentary_glances() -> None:
    # A single 60s block on ProjB stays (>= min), but nothing under the floor.
    blocks = [_blk(0, "ProjA"), _blk(1, "ProjA")]  # only ProjA, 2m
    out = session_reducer._format_attention_trajectory(blocks)
    assert "ProjA" in out
    assert "ProjB" not in out


def test_attention_block_empty_when_no_surface() -> None:
    # Blocks predating the locus pipeline carry empty attention_surface →
    # the section is empty so the reducer prompt is byte-identical to before.
    start = datetime(2026, 6, 18, 17, 0, tzinfo=_TZ)
    blocks = [
        timeline_store.TimelineBlock(start_time=start, end_time=start + timedelta(seconds=60))
    ]
    assert session_reducer._format_attention_trajectory(blocks) == ""


def test_attention_block_empty_for_no_blocks() -> None:
    assert session_reducer._format_attention_trajectory([]) == ""

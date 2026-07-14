"""Deterministic daily attention-dwell digest (dwell → durable user- fact).

`run_attention_digest` folds the day's per-block attention loci into one
ranked `user-attention.md` fact so dwell regularities become schema-miner
input. One digest per calendar day: same-day re-runs supersede in place.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from persome.evomem.engine import EvoMemory
from persome.store import fts
from persome.timeline import store as timeline_store
from persome.writer import attention_digest

_TZ = timezone(timedelta(hours=8))
_NOW = datetime(2026, 6, 18, 18, 0, tzinfo=_TZ)

_CFG = SimpleNamespace(attention_digest_enabled=True)


def _blk(minute: int, surface: str, *, hour: int = 17, rung: str = "editing", conf: float = 0.8):
    start = datetime(2026, 6, 18, hour, minute, tzinfo=_TZ)
    return timeline_store.TimelineBlock(
        start_time=start,
        end_time=start + timedelta(seconds=60),
        attention_surface=surface,
        attention_rung=rung,
        attention_confidence=conf,
    )


def _insert(blocks) -> None:
    with fts.cursor() as conn:
        timeline_store.ensure_schema(conn)
        for b in blocks:
            timeline_store.insert(conn, b)


def _latest_digests() -> list:
    return [
        n for n in EvoMemory().store.all_latest() if n.file_name == attention_digest.ATTENTION_FILE
    ]


def test_disabled_is_noop(ac_root: Path) -> None:
    cfg = SimpleNamespace(attention_digest_enabled=False)
    result = attention_digest.run_attention_digest(cfg, now=_NOW)
    assert not result.committed
    assert result.skipped_reason == "disabled"


def test_no_dwell_is_skipped(ac_root: Path) -> None:
    # Two minutes on one surface is under the 5-min durable-fact floor.
    _insert([_blk(0, "ProjA"), _blk(1, "ProjA")])
    result = attention_digest.run_attention_digest(_CFG, now=_NOW)
    assert not result.committed
    assert result.skipped_reason == "no dwell"
    assert _latest_digests() == []


def test_digest_written_ranked_and_floored(ac_root: Path) -> None:
    # ProjA 8 contiguous minutes, ProjB 5, ProjC 2 (under floor).
    blocks = [_blk(m, "ProjA") for m in range(8)]
    blocks += [_blk(10 + m, "ProjB") for m in range(5)]
    blocks += [_blk(20 + m, "ProjC") for m in range(2)]
    _insert(blocks)
    result = attention_digest.run_attention_digest(_CFG, now=_NOW)
    assert result.committed
    assert result.surfaces == ["ProjA", "ProjB"]
    digests = _latest_digests()
    assert len(digests) == 1
    content = digests[0].content
    assert content.startswith("Attention digest 2026-06-18:")
    assert content.index("ProjA") < content.index("ProjB")
    assert "ProjC" not in content
    node = digests[0]
    metadata = json.loads(node.schema_summary)
    assert metadata["kind"] == "attention_digest"
    assert metadata["day"] == "2026-06-18"
    assert metadata["through"] == _NOW.isoformat()
    assert metadata["surfaces"][0]["dwell_seconds"] == 8 * 60
    assert len(metadata["surfaces"][0]["source_block_ids"]) == 8
    assert node.occurred_at == _NOW.isoformat()
    assert node.valid_from == "2026-06-18T00:00:00+08:00"
    assert node.confidence == "high"


def test_sparse_blocks_do_not_turn_unobserved_gaps_into_dwell(ac_root: Path) -> None:
    # The trajectory renderer joins same-surface blocks across tolerated gaps.
    # A durable fact must count the four observed minutes, not the 10-minute span.
    _insert([_blk(minute, "ProjA") for minute in (0, 3, 6, 9)])
    result = attention_digest.run_attention_digest(_CFG, now=_NOW)
    assert not result.committed
    assert result.skipped_reason == "no dwell"


def test_surface_is_bounded_and_rendered_as_quoted_data(ac_root: Path) -> None:
    surface = "  title\nwith\tcontrol  " + "x" * 300
    _insert([_blk(m, surface) for m in range(5)])
    result = attention_digest.run_attention_digest(_CFG, now=_NOW)
    assert result.committed
    node = _latest_digests()[0]
    metadata = json.loads(node.schema_summary)
    stored = metadata["surfaces"][0]["surface"]
    assert "\n" not in stored and "\t" not in stored
    assert len(stored) == attention_digest._MAX_SURFACE_CHARS
    assert json.dumps(stored, ensure_ascii=False) in node.content


def test_same_day_rerun_unchanged_keeps_chain_quiet(ac_root: Path) -> None:
    _insert([_blk(m, "ProjA") for m in range(8)])
    first = attention_digest.run_attention_digest(_CFG, now=_NOW)
    assert first.committed
    second = attention_digest.run_attention_digest(_CFG, now=_NOW)
    assert not second.committed
    assert second.skipped_reason == "unchanged"
    assert second.node_id == first.node_id
    assert len(_latest_digests()) == 1


def test_same_rendered_dwell_with_new_receipt_still_supersedes(ac_root: Path) -> None:
    _insert([_blk(m, "ProjA") for m in range(5)])
    first = attention_digest.run_attention_digest(_CFG, now=_NOW)
    assert first.committed
    first_node = EvoMemory().store.get(first.node_id)
    assert first_node is not None and '"ProjA"' in first_node.content

    # 300s and 329s both render as ~5m. The exact dwell and source receipt still
    # changed, so content equality must not leave stale provenance in the latest node.
    start = datetime(2026, 6, 18, 17, 5, tzinfo=_TZ)
    _insert(
        [
            timeline_store.TimelineBlock(
                start_time=start,
                end_time=start + timedelta(seconds=29),
                attention_surface="ProjA",
                attention_rung="editing",
                attention_confidence=0.8,
            )
        ]
    )
    later = _NOW + timedelta(minutes=1)
    second = attention_digest.run_attention_digest(_CFG, now=later)
    assert second.committed
    assert second.node_id != first.node_id
    latest = EvoMemory().store.get(second.node_id)
    assert latest is not None and latest.content == first_node.content
    metadata = json.loads(latest.schema_summary)
    assert metadata["surfaces"][0]["dwell_seconds"] == 329
    assert len(metadata["surfaces"][0]["source_block_ids"]) == 6


def test_same_day_rerun_supersedes_in_place(ac_root: Path) -> None:
    _insert([_blk(m, "ProjA") for m in range(8)])
    first = attention_digest.run_attention_digest(_CFG, now=_NOW)
    # More dwell lands later the same day → the digest is superseded, not duplicated.
    _insert([_blk(m, "ProjB", hour=19) for m in range(10)])
    later = _NOW + timedelta(hours=2)
    second = attention_digest.run_attention_digest(_CFG, now=later)
    assert second.committed
    assert second.node_id != first.node_id
    digests = _latest_digests()
    assert len(digests) == 1
    assert digests[0].node_id == second.node_id
    assert "ProjB" in digests[0].content
    mem = EvoMemory()
    old = mem.store.get(first.node_id)
    new = mem.store.get(second.node_id)
    assert old is not None and old.valid_until == later.isoformat()
    assert new is not None and new.valid_from == "2026-06-18T00:00:00+08:00"
    assert json.loads(old.schema_summary)["kind"] == "attention_digest"
    assert json.loads(new.schema_summary)["kind"] == "attention_digest"


def test_legacy_naive_block_times_do_not_break_aware_day_bounds(ac_root: Path) -> None:
    blocks = []
    for minute in range(5):
        start = datetime(2026, 6, 18, 17, minute)
        blocks.append(
            timeline_store.TimelineBlock(
                start_time=start,
                end_time=start + timedelta(minutes=1),
                attention_surface="LegacyProj",
                attention_rung="editing",
                attention_confidence=0.8,
            )
        )
    _insert(blocks)
    result = attention_digest.run_attention_digest(_CFG, now=datetime(2026, 6, 18, 18, 0))
    assert result.committed
    assert result.surfaces == ["LegacyProj"]


def test_blocks_outside_today_are_ignored(ac_root: Path) -> None:
    yesterday = [
        timeline_store.TimelineBlock(
            start_time=datetime(2026, 6, 17, 17, m, tzinfo=_TZ),
            end_time=datetime(2026, 6, 17, 17, m, tzinfo=_TZ) + timedelta(seconds=60),
            attention_surface="OldProj",
            attention_rung="editing",
            attention_confidence=0.8,
        )
        for m in range(10)
    ]
    _insert(yesterday)
    result = attention_digest.run_attention_digest(_CFG, now=_NOW)
    assert not result.committed
    assert result.skipped_reason == "no dwell"

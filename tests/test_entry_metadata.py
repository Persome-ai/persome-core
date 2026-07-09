"""Meta-cognition layer (Hy-Memory migration): confidence / conflicted / occurred_at.

Three things are pinned here:

1. **Round-trip**: the three fields ride heading colon-tags (markdown SSOT) and
   land in the ``entry_metadata`` derived table on the incremental write path, and
   are re-parsed identically.
2. **Invariant**: ``entry_metadata`` built incrementally equals a fresh
   ``rebuild_index`` row-for-row — the load-bearing增量 == rebuild guarantee.
   A row exists IFF the entry carries a non-default tag (all-default → no row).
3. **Recall rendering**: ``include_confidence`` annotates only low / conflicted
   hits with a ``⚠`` note; off → output byte-identical to today.
"""

from __future__ import annotations

import sqlite3

from persome.intent import recall
from persome.store import entries as entries_mod
from persome.store import files as files_mod
from persome.store import fts


def _meta_rows(conn: sqlite3.Connection) -> dict[str, tuple[str | None, int, str | None]]:
    """entry_id -> (confidence, conflicted, occurred_at), the full entry_metadata table."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT entry_id, confidence, conflicted, occurred_at FROM entry_metadata"
    ).fetchall()
    return {r["entry_id"]: (r["confidence"], int(r["conflicted"]), r["occurred_at"]) for r in rows}


def _assert_matches_fresh_rebuild(conn: sqlite3.Connection) -> None:
    live = _meta_rows(conn)
    entries_mod.rebuild_index(conn)
    fresh = _meta_rows(conn)
    assert live == fresh, (
        f"incremental entry_metadata != fresh rebuild\n live={live}\n fresh={fresh}"
    )


# ── round-trip ───────────────────────────────────────────────────────────────


def test_append_writes_tag_and_metadata_row(ac_root):
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-m.md", description="m", tags=["t"])
        eid = entries_mod.append_entry(
            conn,
            name="project-m.md",
            content="found a fact",
            tags=["t"],
            confidence="low",
            conflicted=True,
            occurred_at="2026-06-09T14:30",
        )
        # markdown carries the colon-tags
        parsed = files_mod.read_file(files_mod.memory_path("project-m.md"))
        e = next(x for x in parsed.entries if x.id == eid)
        assert e.confidence == "low"
        assert e.conflicted is True
        assert e.occurred_at == "2026-06-09T14:30"
        # derived table mirrors it
        assert _meta_rows(conn)[eid] == ("low", 1, "2026-06-09T14:30")
        _assert_matches_fresh_rebuild(conn)


def test_all_default_writes_no_row(ac_root):
    """A plain append (no metadata) leaves entry_metadata empty — byte-identical to before."""
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-n.md", description="n", tags=["t"])
        eid = entries_mod.append_entry(conn, name="project-n.md", content="plain", tags=["t"])
        assert eid not in _meta_rows(conn)
        assert _meta_rows(conn) == {}
        _assert_matches_fresh_rebuild(conn)


def test_confidence_normalized_and_invalid_dropped(ac_root):
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-o.md", description="o", tags=["t"])
        # upper-case normalizes to canonical lower
        hi = entries_mod.append_entry(
            conn, name="project-o.md", content="hi fact", tags=["t"], confidence="HIGH"
        )
        # off-vocabulary confidence degrades to no tag / no row (not a hard error)
        bad = entries_mod.append_entry(
            conn, name="project-o.md", content="bad fact", tags=["t"], confidence="very-sure"
        )
        rows = _meta_rows(conn)
        assert rows[hi][0] == "high"
        assert bad not in rows
        _assert_matches_fresh_rebuild(conn)


def test_supersede_sets_new_head_metadata(ac_root):
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-p.md", description="p", tags=["t"])
        a = entries_mod.append_entry(
            conn, name="project-p.md", content="v1", tags=["t"], confidence="high"
        )
        b = entries_mod.supersede_entry(
            conn,
            name="project-p.md",
            old_entry_id=a,
            new_content="v2",
            reason="r",
            tags=["t"],
            confidence="medium",
        )
        rows = _meta_rows(conn)
        assert rows[a][0] == "high"  # old head keeps its own tag
        assert rows[b][0] == "medium"  # new head carries the supersede's confidence
        _assert_matches_fresh_rebuild(conn)


# ── recall rendering (flag-gated) ────────────────────────────────────────────


def _seed_recall_entries(conn: sqlite3.Connection) -> None:
    entries_mod.create_file(conn, name="project-recall.md", description="r", tags=["t"])
    entries_mod.append_entry(
        conn, name="project-recall.md", content="alpha solid fact", tags=["t"], confidence="high"
    )
    entries_mod.append_entry(
        conn, name="project-recall.md", content="alpha shaky guess", tags=["t"], confidence="low"
    )
    entries_mod.append_entry(
        conn, name="project-recall.md", content="alpha disputed claim", tags=["t"], conflicted=True
    )


def test_recall_flag_off_has_no_annotation(ac_root):
    with fts.cursor() as conn:
        _seed_recall_entries(conn)
        bg = recall.assemble_background(conn, scope="", hints=["alpha"], include_confidence=False)
        assert "alpha" in bg
        assert "⚠" not in bg


def test_recall_flag_on_marks_low_and_conflicted_only(ac_root):
    with fts.cursor() as conn:
        _seed_recall_entries(conn)
        bg = recall.assemble_background(
            conn, scope="", hints=["alpha"], per_hint=5, include_confidence=True
        )
        assert "⚠(低置信)" in bg
        assert "⚠(冲突未裁决)" in bg
        # the high-confidence hit is present but unmarked
        assert "alpha solid fact" in bg
        assert bg.count("⚠") == 2  # only the low + conflicted hits, not the high one


def test_occurred_at_with_space_is_normalized_and_round_trips(ac_root):
    # issue #434：空格分隔的 ISO 值（LLM 完全可能产出 "2026-06-09 14:30:00"）不能裸写进
    # heading tag——会被按空白切断，增量存全值、rebuild 只剩日期。首个空格归一为 T 后整值
    # 无空白，能被 heading 解析完整拿回，增量≡rebuild 不变量保持。
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-w.md", description="w", tags=["t"])
        eid = entries_mod.append_entry(
            conn,
            name="project-w.md",
            content="a dated fact",
            tags=["t"],
            occurred_at="2026-06-09 14:30:00",
        )
        parsed = files_mod.read_file(files_mod.memory_path("project-w.md"))
        e = next(x for x in parsed.entries if x.id == eid)
        assert e.occurred_at == "2026-06-09T14:30:00"
        assert _meta_rows(conn)[eid] == (None, 0, "2026-06-09T14:30:00")
        _assert_matches_fresh_rebuild(conn)


def test_occurred_at_with_residual_whitespace_is_dropped(ac_root):
    # 归一首空格后仍含空白（如带时区后缀 "... UTC"）→ 整条丢弃，等同未标注，绝不写出
    # 无法 round-trip 的 tag。
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-d.md", description="d", tags=["t"])
        eid = entries_mod.append_entry(
            conn,
            name="project-d.md",
            content="weirdly timed fact",
            tags=["t"],
            occurred_at="2026-06-09 14:30:00 UTC",
        )
        parsed = files_mod.read_file(files_mod.memory_path("project-d.md"))
        e = next(x for x in parsed.entries if x.id == eid)
        assert e.occurred_at is None
        assert eid not in _meta_rows(conn)
        _assert_matches_fresh_rebuild(conn)

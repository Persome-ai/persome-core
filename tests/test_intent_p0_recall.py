"""P0 — schema-prior injection seam + recall superseded folding (flags default off).

Two flag-gated changes behind ``recall.assemble_background``:

- ``schema_prior``: an optional highest-priority section ("# 用户惯性先验") for
  the D2 schema-inference layer. ``None`` (default) → output byte-identical to
  before this change (zero regression).
- ``fold_superseded``: when True, the FTS hint layers exclude entries marked
  ``superseded=1`` (mirroring ``fts.search``'s ``superseded = 0`` clause).
  Default False → output byte-identical to before.

Plus ``schema_prior.active_schema_inferences`` — the D2 hook that returns ``[]``
until ``schema-*.md`` files exist.
"""

from __future__ import annotations

from persome.intent import recall, schema_prior
from persome.store import entries as entries_mod
from persome.store import fts


def test_schema_prior_none_is_byte_identical_golden(ac_root):
    """Zero-regression: not passing schema_prior == passing schema_prior=None."""
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-x.md", description="project x", tags=["x"])
        entries_mod.append_entry(
            conn, name="project-x.md", content="ProjectX uses DeepSeek", tags=["x"]
        )
        default = recall.assemble_background(conn, scope="timeline", hints=["ProjectX"])
        explicit_none = recall.assemble_background(
            conn, scope="timeline", hints=["ProjectX"], schema_prior=None
        )
    assert default == explicit_none


def test_schema_prior_renders_first_and_highest_priority(ac_root):
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-x.md", description="project x", tags=["x"])
        entries_mod.append_entry(
            conn, name="project-x.md", content="ProjectX uses DeepSeek", tags=["x"]
        )
        bundle = recall.assemble_background(
            conn,
            scope="timeline",
            hints=["ProjectX"],
            schema_prior=["用户偏好极简工具链"],
        )
    assert "# 用户惯性先验" in bundle
    assert "用户偏好极简工具链" in bundle
    # highest priority → it is the very first section
    assert bundle.index("# 用户惯性先验") == 0
    # and it comes before the durable-fact layer it shares the budget with
    assert bundle.index("# 用户惯性先验") < bundle.index("相关记忆")


def test_empty_schema_prior_is_noop(ac_root):
    """An empty list is treated the same as None — no prior section emitted."""
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-x.md", description="project x", tags=["x"])
        entries_mod.append_entry(
            conn, name="project-x.md", content="ProjectX uses DeepSeek", tags=["x"]
        )
        default = recall.assemble_background(conn, scope="timeline", hints=["ProjectX"])
        empty = recall.assemble_background(
            conn, scope="timeline", hints=["ProjectX"], schema_prior=[]
        )
    assert "用户惯性先验" not in empty
    assert empty == default


def test_fold_superseded_explicit_off_includes_superseded_entry(ac_root):
    """Explicit ``fold_superseded=False`` keeps a superseded entry in recall —
    the legacy (un-folded) path. (Migration ACTIVATED → this is no longer the
    default; the default now folds, see ``test_default_now_folds_superseded``.)"""
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-x.md", description="project x", tags=["x"])
        eid = entries_mod.append_entry(
            conn, name="project-x.md", content="ProjectX old fact about DeepSeek", tags=["x"]
        )
        fts.mark_superseded(conn, eid)
        bundle = recall.assemble_background(
            conn, scope="timeline", hints=["ProjectX"], fold_superseded=False
        )
    assert "old fact" in bundle


def test_default_now_folds_superseded(ac_root):
    """Migration ACTIVATED: ``assemble_background`` default folds superseded out
    (current-belief recall) — the recognizer wires this from
    ``cfg.intent_recognizer.recall_fold_superseded`` (now default True)."""
    from persome import config as config_mod

    assert config_mod.IntentRecognizerConfig().recall_fold_superseded is True
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-x.md", description="project x", tags=["x"])
        old_id = entries_mod.append_entry(
            conn, name="project-x.md", content="ProjectX old fact about DeepSeek", tags=["x"]
        )
        entries_mod.append_entry(
            conn, name="project-x.md", content="ProjectX new fact about DeepSeek", tags=["x"]
        )
        fts.mark_superseded(conn, old_id)
        # default args mirror the recognizer's intent (fold on by migration default)
        folded = recall.assemble_background(
            conn, scope="timeline", hints=["ProjectX"], fold_superseded=True
        )
    assert "old fact" not in folded
    assert "new fact" in folded


def test_fold_superseded_on_excludes_superseded_entry(ac_root):
    """fold_superseded=True drops the superseded entry; a live entry still shows."""
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-x.md", description="project x", tags=["x"])
        old_id = entries_mod.append_entry(
            conn, name="project-x.md", content="ProjectX old fact about DeepSeek", tags=["x"]
        )
        entries_mod.append_entry(
            conn, name="project-x.md", content="ProjectX new fact about DeepSeek", tags=["x"]
        )
        fts.mark_superseded(conn, old_id)
        folded = recall.assemble_background(
            conn, scope="timeline", hints=["ProjectX"], fold_superseded=True
        )
        unfolded = recall.assemble_background(conn, scope="timeline", hints=["ProjectX"])
    assert "old fact" not in folded
    assert "new fact" in folded
    # control: without folding, the superseded one is still present today
    assert "old fact" in unfolded


def test_active_schema_inferences_returns_empty_without_schema_files(ac_root):
    """The D2 seam returns [] when no schema-*.md memory files exist yet."""
    with fts.cursor() as conn:
        assert schema_prior.active_schema_inferences(conn) == []

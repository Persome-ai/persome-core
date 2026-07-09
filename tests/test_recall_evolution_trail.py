"""演化链轨迹 surfaced in recall output (flag-gated)。

``fold_superseded`` folds superseded entries out (return only the chain head)。
``chain_trail`` additionally *surfaces the trajectory*: when a hit lands on a chain
that has ≥1 superseded ancestor, a compact trail is appended after the head so the
intent recognizer can SEE the attitude evolution (``[当前] 用户喝茶 ← [曾] 用户喝咖啡``)
instead of just the latest belief.

PR-7 之后链数据源唯一 = ``evo_nodes``（entry_chain 已退役）：折叠子查询读
evo_nodes 活跃链头，trail 从双向指针渲染。测试 seeding 因此走「写 markdown →
``evomem-backfill``」灌 evo_nodes（与生产冷启动同路径）。

Gating（trail 是独立 flag）：
- both OFF → byte-identical to legacy（no trail, no fold）。
- ``fold_superseded`` ON, ``chain_trail`` OFF → pure fold to chain heads，NO trail。
- both ON → fold AND each multi-member chain head is followed by a ``← [曾] …``
  trail of its superseded ancestors (latest→oldest)。
- ``chain_trail`` ON but fold OFF → no trail（trail 只装饰折叠浮出的链头）。
- an isolated (single-member) chain head gets NO trail (nothing to show)。
"""

from __future__ import annotations

import pytest

from persome.evomem import backfill
from persome.intent import recall
from persome.store import entries as entries_mod
from persome.store import fts


@pytest.fixture(autouse=True)
def _quiet_alerts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("persome.events.publish", lambda *a, **k: None)


def _seed_attitude_chain() -> tuple[str, str, str]:
    """A 2-hop attitude chain (coffee→tea) + an isolated live fact + backfill.

    Returns (coffee_old_id, tea_head_id, isolated_id).
    """
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="user-preferences.md", description="prefs", tags=["t"])
        coffee = entries_mod.append_entry(
            conn, name="user-preferences.md", content="用户喝咖啡 beverage", tags=["t"]
        )
        tea = entries_mod.supersede_entry(
            conn,
            name="user-preferences.md",
            old_entry_id=coffee,
            new_content="用户喝茶 beverage",
            reason="口味变了",
            tags=["t"],
        )
        isolated = entries_mod.append_entry(
            conn, name="user-preferences.md", content="用户早起 sunrise", tags=["t"]
        )
        # 与历史基线一致：rebuild 把 markdown 里的 <!-- supersedes --> 注释重放进
        # entries.content（增量写不含注释），golden snapshot 钉的是这个形态。
        entries_mod.rebuild_index(conn)
    assert backfill.run_backfill().ok
    return coffee, tea, isolated


def test_flag_off_no_trail_byte_identical(ac_root):
    """Default (fold_superseded=False) shows NO trajectory — output unchanged."""
    _seed_attitude_chain()
    with fts.cursor() as conn:
        default = recall.assemble_background(
            conn, scope="timeline", hints=["beverage"], per_hint=10
        )
        explicit_off = recall.assemble_background(
            conn, scope="timeline", hints=["beverage"], per_hint=10, fold_superseded=False
        )
    assert default == explicit_off
    # no trajectory marker present in the un-flagged output
    assert "[曾]" not in default
    assert "←" not in default
    # legacy un-folded recall: the superseded coffee fact still shows
    assert "喝咖啡" in default


def test_flag_off_golden_snapshot(ac_root):
    """Golden: with the flags OFF, recall output is pinned to the legacy un-folded
    behavior — both the superseded coffee fact AND the live tea fact surface
    un-folded, in FTS rank order, carrying the raw ``<!-- supersedes -->`` provenance
    comment, with NO chain/trail artifact. Captured as an exact snapshot (the
    volatile entry id is normalized to ``<id>``) so a regression that silently
    folded or rendered a trail off-flag would fail here."""
    old_id, _tea, _iso = _seed_attitude_chain()
    with fts.cursor() as conn:
        out = recall.assemble_background(conn, scope="timeline", hints=["beverage"], per_hint=10)
    # Normalize only the random entry id so the snapshot is exact yet salt-independent.
    normalized = out.replace(old_id, "<id>")
    expected = (
        "# 相关记忆\n"
        "[user-preferences.md] 用户喝咖啡 beverage\n"
        "[user-preferences.md] 用户喝茶 beverage\n"
        "<!-- supersedes: <id>; reason: 口味变了 -->"
    )
    assert normalized == expected


def test_flag_on_appends_evolution_trail(ac_root):
    """Both flags ON (fold_superseded + chain_trail): the chain head (tea) is
    followed by a ← [曾] trail of the superseded ancestor (coffee)."""
    _seed_attitude_chain()
    with fts.cursor() as conn:
        bundle = recall.assemble_background(
            conn,
            scope="timeline",
            hints=["beverage"],
            per_hint=10,
            fold_superseded=True,
            chain_trail=True,
        )
    # head surfaces (current belief)
    assert "喝茶" in bundle
    # trajectory marker + the superseded ancestor's content appears as 曾-state
    assert "[曾]" in bundle
    assert "喝咖啡" in bundle
    # the old fact is shown as part of the head's trail, not as its own top-level hit
    head_line = next(line for line in bundle.splitlines() if "喝茶" in line)
    assert "喝咖啡" in head_line  # trail is inline on the head's line
    assert "[曾]" in head_line


def test_isolated_chain_head_gets_no_trail(ac_root):
    """A single-member chain (the isolated live fact) carries no ← trail even with
    both flags on."""
    _seed_attitude_chain()
    with fts.cursor() as conn:
        bundle = recall.assemble_background(
            conn,
            scope="timeline",
            hints=["sunrise"],
            per_hint=10,
            fold_superseded=True,
            chain_trail=True,
        )
    assert "早起" in bundle
    # the isolated fact's line has no trajectory marker
    iso_line = next(line for line in bundle.splitlines() if "早起" in line)
    assert "[曾]" not in iso_line
    assert "←" not in iso_line


def test_fold_alone_folds_without_trail(ac_root):
    """``fold_superseded=True`` WITHOUT ``chain_trail`` is a pure fold — NO
    trajectory rendered, and the superseded ancestor is folded out."""
    _seed_attitude_chain()
    with fts.cursor() as conn:
        fold_only = recall.assemble_background(
            conn, scope="timeline", hints=["beverage"], per_hint=10, fold_superseded=True
        )
    assert "[曾]" not in fold_only  # no trajectory rendered without chain_trail
    assert "喝咖啡" not in fold_only  # superseded ancestor folded out, not trailed
    assert "喝茶" in fold_only


def test_chain_trail_without_fold_is_noop(ac_root):
    """``chain_trail=True`` alone (fold off) renders NO trail — the trail only
    annotates heads surfaced via the fold. So chain_trail without the fold is
    byte-identical to today (the off/off golden), proving the AND gate."""
    _seed_attitude_chain()
    with fts.cursor() as conn:
        trail_no_fold = recall.assemble_background(
            conn, scope="timeline", hints=["beverage"], per_hint=10, chain_trail=True
        )
        today = recall.assemble_background(conn, scope="timeline", hints=["beverage"], per_hint=10)
    assert trail_no_fold == today  # AND gate: chain_trail needs the fold
    assert "[曾]" not in trail_no_fold


def test_trail_dropped_under_budget_pressure_head_survives(ac_root):
    """The trail is a low-priority supplement — when the shared ``_Budget`` can't
    fit it, the head still lands (head-only) and the trail is silently dropped.
    The head must NEVER be squeezed out by its own trail."""
    _seed_attitude_chain()
    with fts.cursor() as conn:
        # Budget big enough for the head line but not the appended "← [曾] …" trail.
        head_only_budget = len("# 相关记忆\n[user-preferences.md] 用户喝茶 beverage") + 5
        bundle = recall.assemble_background(
            conn,
            scope="timeline",
            hints=["beverage"],
            per_hint=10,
            max_chars=head_only_budget,
            fold_superseded=True,
            chain_trail=True,
        )
    assert "喝茶" in bundle  # the head (current belief) survived
    assert "[曾]" not in bundle  # the trail was dropped under budget pressure
    assert "喝咖啡" not in bundle  # superseded ancestor not rendered (trail dropped)


def test_trail_does_not_evict_other_hits(ac_root):
    """The trail must not consume budget that would otherwise carry another hit. With a
    budget sized for two head lines, both heads surface even if the first head's trail
    is dropped — the trail yields to a competing hit, never the reverse."""
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-multi.md", description="m", tags=["t"])
        a = entries_mod.append_entry(
            conn, name="project-multi.md", content="老观点 widget", tags=["t"]
        )
        entries_mod.supersede_entry(
            conn,
            name="project-multi.md",
            old_entry_id=a,
            new_content="新观点 widget",
            reason="r",
            tags=["t"],
        )
        entries_mod.append_entry(
            conn, name="project-multi.md", content="另一事实 widget", tags=["t"]
        )
        # 与历史基线同形态（含 <!-- supersedes --> 注释的 rebuild 重放）——BM25
        # 排序由内容决定，预算演算钉在这个顺序上。
        entries_mod.rebuild_index(conn)
    assert backfill.run_backfill().ok
    with fts.cursor() as conn:
        # Room for the two head lines but tight enough that a trail would overflow.
        two_heads = (
            "# 相关记忆\n[project-multi.md] 新观点 widget\n[project-multi.md] 另一事实 widget"
        )
        bundle = recall.assemble_background(
            conn,
            scope="timeline",
            hints=["widget"],
            per_hint=10,
            max_chars=len(two_heads) + 2,
            fold_superseded=True,
            chain_trail=True,
        )
    # both head hits present (the competing hit was NOT evicted by the first's trail)
    assert "新观点" in bundle
    assert "另一事实" in bundle
    # the first head's trail was dropped to make room — head-only, not squeezed out
    assert "[曾]" not in bundle


def test_trail_marks_refinement_distinct_from_contradiction(ac_root):
    """EVO-02 双标签法: a refinement (supersede_entry refined_from=old) renders
    ``← [精炼自] X`` while a plain contradiction renders ``← [曾] X``. The
    discriminator is the evo_nodes ``refined_from`` column."""
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-refine.md", description="r", tags=["t"])
        old = entries_mod.append_entry(
            conn, name="project-refine.md", content="粗略版 gadget", tags=["t"]
        )
        # A refinement: same-direction sharpening, retires old + tags head refined-from.
        entries_mod.supersede_entry(
            conn,
            name="project-refine.md",
            old_entry_id=old,
            new_content="精炼版 gadget",
            reason="sharpen",
            tags=["t"],
            refined_from=old,
        )
    assert backfill.run_backfill().ok
    with fts.cursor() as conn:
        bundle = recall.assemble_background(
            conn,
            scope="timeline",
            hints=["gadget"],
            per_hint=10,
            fold_superseded=True,
            chain_trail=True,
        )
    head_line = next(line for line in bundle.splitlines() if "精炼版" in line)
    # the refined ancestor is marked [精炼自], NOT the contradiction marker [曾]
    assert "[精炼自]" in head_line
    assert "粗略版" in head_line
    assert "[曾]" not in head_line


def test_trail_mixed_refinement_and_contradiction(ac_root):
    """A 3-hop chain v1 →(refine)→ v2 →(contradict)→ v3: the head's trail tags v2 as
    [曾] (v2 was contradicted by v3) and v1 as [精炼自] (v1 was refined into v2). Each
    ancestor's marker reflects HOW its own successor retired it."""
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-mix.md", description="m", tags=["t"])
        v1 = entries_mod.append_entry(conn, name="project-mix.md", content="初版 thing", tags=["t"])
        v2 = entries_mod.supersede_entry(
            conn,
            name="project-mix.md",
            old_entry_id=v1,
            new_content="精炼版 thing",
            reason="sharpen",
            tags=["t"],
            refined_from=v1,  # v1 was REFINED into v2
        )
        entries_mod.supersede_entry(
            conn,
            name="project-mix.md",
            old_entry_id=v2,
            new_content="改主意版 thing",
            reason="changed mind",
            tags=["t"],
            # no refined_from → v2 was CONTRADICTED by v3
        )
    assert backfill.run_backfill().ok
    with fts.cursor() as conn:
        bundle = recall.assemble_background(
            conn,
            scope="timeline",
            hints=["thing"],
            per_hint=10,
            fold_superseded=True,
            chain_trail=True,
        )
    head_line = next(line for line in bundle.splitlines() if "改主意版" in line)
    # v2 (精炼版) was contradicted → [曾]; v1 (初版) was refined → [精炼自]
    assert "[曾] 精炼版" in head_line
    assert "[精炼自] 初版" in head_line


def test_trail_orders_latest_to_oldest(ac_root):
    """A 3-hop chain v1→v2→v3: head is v3, trail lists v2 then v1 (latest→oldest)."""
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-evo.md", description="evo", tags=["t"])
        v1 = entries_mod.append_entry(
            conn, name="project-evo.md", content="阶段 alpha topic", tags=["t"]
        )
        v2 = entries_mod.supersede_entry(
            conn,
            name="project-evo.md",
            old_entry_id=v1,
            new_content="阶段 beta topic",
            reason="r",
            tags=["t"],
        )
        entries_mod.supersede_entry(
            conn,
            name="project-evo.md",
            old_entry_id=v2,
            new_content="阶段 gamma topic",
            reason="r",
            tags=["t"],
        )
    assert backfill.run_backfill().ok
    with fts.cursor() as conn:
        bundle = recall.assemble_background(
            conn,
            scope="timeline",
            hints=["topic"],
            per_hint=10,
            fold_superseded=True,
            chain_trail=True,
        )
    head_line = next(line for line in bundle.splitlines() if "gamma" in line)
    # both prior states present, beta (newer) before alpha (older) in the trail
    assert "beta" in head_line
    assert "alpha" in head_line
    assert head_line.index("beta") < head_line.index("alpha")

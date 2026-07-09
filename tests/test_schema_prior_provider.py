"""D2 provider wiring: ``intent.schema_prior.active_schema_inferences``.

The P0 seam returned ``[]`` unconditionally. D2 fills it in: it now scans
``schema-*.md`` memory entries, keeps only the ones whose status tag is
``stable`` (forming/evolving/deprecated schemas are weak signals and must not
become priors — design §2.4 / §4②), and returns their ``expected_inferences``
lines as plain strings for injection as the highest-priority recall section.

These tests pin three behaviours the larger D2 design depends on:
  ① a stable schema's inference lines are returned;
  ② a forming schema's inferences are *not* returned (status gate);
  ③ no ``schema-*.md`` files at all → ``[]`` (the P0 seam stays intact, which
     ``test_intent_p0_recall`` also asserts).
"""

from __future__ import annotations

from persome.intent import schema_prior
from persome.store import entries as entries_mod
from persome.store import files as files_mod
from persome.store import fts
from persome.writer import schema_miner_stage as stage


def _seed_schema(
    conn, *, slug: str, status: str, inferences: list[str], confidence: float = 0.8
) -> None:
    """Write one schema-*.md entry the way the stage will, for provider tests."""
    name = f"schema-{slug}.md"
    entries_mod.create_file(
        conn,
        name=name,
        description=f"predictive schema: {slug}",
        tags=["schema", status],
    )
    body = stage.render_schema_body(
        central_proposition=f"用户在 {slug} 上有稳定倾向",
        supporting_summary="多条关联事实支撑",
        expected_inferences=inferences,
    )
    entries_mod.append_entry(
        conn,
        name=name,
        content=body,
        tags=["schema", status, f"confidence:{confidence:.2f}"],
    )


def test_stable_schema_inferences_are_returned(ac_root):
    with fts.cursor() as conn:
        _seed_schema(
            conn,
            slug="tooling-minimalism",
            status="stable",
            inferences=["会拒绝引入大型框架/重 SDK", "评估新工具优先看依赖体积"],
        )
        out = schema_prior.active_schema_inferences(conn)
    assert "会拒绝引入大型框架/重 SDK" in out
    assert "评估新工具优先看依赖体积" in out


def test_forming_schema_is_not_injected(ac_root):
    """Status gate: only ``stable`` schemas feed the prior (design §4②)."""
    with fts.cursor() as conn:
        _seed_schema(
            conn,
            slug="half-baked",
            status="forming",
            inferences=["这条不该出现"],
        )
        out = schema_prior.active_schema_inferences(conn)
    assert out == []


def test_no_schema_files_returns_empty(ac_root):
    """P0 seam intact: no schema-*.md → []."""
    with fts.cursor() as conn:
        assert schema_prior.active_schema_inferences(conn) == []


def test_inferences_ordered_by_confidence_and_capped(ac_root):
    """Highest-confidence schemas win the budget; total lines capped at 8.

    Three stable schemas with 5 inferences each = 15 candidates. The cap is 8, so
    only the top-confidence schemas' inferences survive — and the lowest-confidence
    schema's inferences must be the ones dropped.
    """
    with fts.cursor() as conn:
        _seed_schema(
            conn,
            slug="high",
            status="stable",
            confidence=0.95,
            inferences=[f"high-{i}" for i in range(5)],
        )
        _seed_schema(
            conn,
            slug="mid",
            status="stable",
            confidence=0.7,
            inferences=[f"mid-{i}" for i in range(5)],
        )
        _seed_schema(
            conn,
            slug="low",
            status="stable",
            confidence=0.6,
            inferences=[f"low-{i}" for i in range(5)],
        )
        out = schema_prior.active_schema_inferences(conn)

    assert len(out) == 8  # capped
    # the 5 highest-confidence lines come first, then 3 of the mid ones.
    assert out[:5] == [f"high-{i}" for i in range(5)]
    assert all(line.startswith("mid-") for line in out[5:])
    # the lowest-confidence schema is entirely crowded out.
    assert not any(line.startswith("low-") for line in out)


def test_superseded_schema_entry_is_ignored(ac_root):
    """A re-mined (superseded) schema entry must not feed the prior."""
    with fts.cursor() as conn:
        _seed_schema(
            conn,
            slug="evolving",
            status="stable",
            inferences=["旧推论"],
        )
        # Supersede the head with a fresh inference (mirrors the stage's re-mine).
        parsed_path = "schema-evolving.md"
        parsed = files_mod.read_file(files_mod.memory_path(parsed_path))
        old_id = parsed.entries[-1].id
        new_body = stage.render_schema_body(
            central_proposition="演化后的命题",
            supporting_summary="新证据",
            expected_inferences=["新推论"],
        )
        entries_mod.supersede_entry(
            conn,
            name=parsed_path,
            old_entry_id=old_id,
            new_content=new_body,
            reason="test re-mine",
            tags=["schema", "stable", "confidence:0.8"],
        )
        out = schema_prior.active_schema_inferences(conn)
    assert "新推论" in out
    assert "旧推论" not in out

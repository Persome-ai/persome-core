"""P4 — layered scene/intent background assembly.

recall.assemble_background went from a flat keyword dump to a priority-ordered
pack: scene intents → behavioural priors → durable facts → keyword fallback,
sharing one character budget.
"""

from __future__ import annotations

from datetime import datetime

from persome.intent import recall, sink
from persome.intent.ontology import Intent, IntentEvidence
from persome.store import entries as entries_mod
from persome.store import fts

# A fixed past date would silently age out: meeting_hint rows now carry a 7-day
# TTL (2026-06-12), and the scene layer filters expired intents.
_NOW = datetime.now().isoformat(timespec="seconds")


def _intent(scope: str, text: str, ts: str) -> Intent:
    return Intent(
        kind="meeting_hint",
        scope=scope,
        rationale=text[:200],
        ts=ts,
        payload={"text": text},
        evidence=[IntentEvidence(source="meeting_transcript", ref_id=scope)],
    )


def test_scene_layer_surfaces_scope_intents(ac_root):
    with fts.cursor() as conn:
        sink.persist_intent(conn, _intent("meeting-42", "确认下周预算", _NOW))
        bundle = recall.assemble_background(conn, scope="meeting-42", hints=[])
    # scene context appears even with NO keyword hints — the old flat recall
    # returned "" here.
    assert "场景意图" in bundle
    assert "确认下周预算" in bundle


def test_scope_isolation_in_scene_layer(ac_root):
    with fts.cursor() as conn:
        sink.persist_intent(conn, _intent("meeting-1", "甲场景内容", _NOW))
        bundle = recall.assemble_background(conn, scope="meeting-2", hints=[])
    assert bundle == ""  # different scene, no hints → nothing


def test_scene_layer_excludes_dismissed_and_consumed(ac_root):
    """#533 scene-status contradiction: a dismissed/consumed intent must NOT be
    re-injected as positive "场景意图" — that contradicts the same prompt's
    negative prior ("勿重复 surface 同类")."""
    from persome.intent import store as intent_store

    with fts.cursor() as conn:
        dismissed_id = sink.persist_intent(conn, _intent("meeting-9", "被划掉的预算", _NOW))
        consumed_id = sink.persist_intent(conn, _intent("meeting-9", "已采纳的排期", _NOW))
        sink.persist_intent(conn, _intent("meeting-9", "仍然 open 的项", _NOW))
        intent_store.update_intent_status(conn, intent_id=dismissed_id, new_status="dismissed")
        intent_store.update_intent_status(conn, intent_id=consumed_id, new_status="consumed")
        bundle = recall.assemble_background(conn, scope="meeting-9", hints=[])
    assert "被划掉的预算" not in bundle  # dismissed: gone
    assert "已采纳的排期" not in bundle  # consumed: gone
    assert "仍然 open 的项" in bundle  # open: still surfaced


def test_priors_render_before_facts(ac_root):
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn, name="skill-deploy.md", description="deploy skill", tags=["x"]
        )
        entries_mod.append_entry(
            conn, name="skill-deploy.md", content="run deploy script for ProjectX", tags=["x"]
        )
        entries_mod.create_file(conn, name="project-x.md", description="project x", tags=["x"])
        entries_mod.append_entry(
            conn, name="project-x.md", content="ProjectX uses DeepSeek", tags=["x"]
        )
        bundle = recall.assemble_background(conn, scope="timeline", hints=["ProjectX"])

    assert "行为先验" in bundle and "相关记忆" in bundle
    # behavioural priors come before durable facts in the pack
    assert bundle.index("行为先验") < bundle.index("相关记忆")
    assert "skill-deploy.md" in bundle and "project-x.md" in bundle


def test_backward_compatible_fact_hit_and_empty(ac_root):
    """The P1 contract still holds: fact hits surface; empty hints+scope → ''."""
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-x.md", description="project x", tags=["x"])
        entries_mod.append_entry(
            conn, name="project-x.md", content="uses DeepSeek API for chat", tags=["x"]
        )
        bundle = recall.assemble_background(conn, scope="timeline", hints=["DeepSeek"])
        assert "DeepSeek" in bundle and "project-x.md" in bundle
        assert recall.assemble_background(conn, scope="timeline", hints=[]) == ""


def test_budget_is_respected_across_layers(ac_root):
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-big.md", description="big", tags=["x"])
        for i in range(20):
            entries_mod.append_entry(
                conn, name="project-big.md", content=f"DeepSeek note {i} " + "x" * 100, tags=["x"]
            )
        bundle = recall.assemble_background(
            conn, scope="timeline", hints=["DeepSeek"], per_hint=20, max_chars=300
        )
    # section headers are not counted into the budget, but snippet text is held
    # under it — the bundle stays small rather than dumping all 20 entries.
    assert len(bundle) < 700
    assert "DeepSeek" in bundle


def test_events_excluded_by_default(ac_root):
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="event-2026-06-01.md", description="day", tags=["x"])
        entries_mod.append_entry(
            conn, name="event-2026-06-01.md", content="opened DeepSeek docs", tags=["x"]
        )
        default = recall.assemble_background(conn, scope="timeline", hints=["DeepSeek"])
        with_events = recall.assemble_background(
            conn, scope="timeline", hints=["DeepSeek"], include_events=True
        )
    assert "event-2026-06-01.md" not in default
    assert "event-2026-06-01.md" in with_events

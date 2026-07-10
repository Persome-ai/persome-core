"""ScenePack contract and unified intent-stream behavior.

Covers the §8 mechanism, not the meeting scene's product behaviour:
- ``SceneState`` (③) accumulation / anti-repeat / prompt rendering
- content-hash dedup so distinct hints coexist while identical ones suppress
"""

from __future__ import annotations

from persome.intent import sink
from persome.intent import store as intent_store
from persome.intent.ontology import Intent, IntentEvidence
from persome.intent.pack import SceneState
from persome.store import fts


def test_scene_state_accumulation_and_anti_repeat():
    s = SceneState(scope="meeting-x")
    s.decisions.append("采用方案 A")
    s.action_items.append("张三周五前出稿")
    s.merge_entities(["张三", "张三", "李四"])  # dedupe on merge
    s.note_surfaced("提醒：确认预算")
    s.note_surfaced("提醒：确认预算")  # already surfaced → ignored

    assert s.entities == ["张三", "李四"]
    assert s.surfaced == ["提醒：确认预算"]

    prompt = s.to_prompt()
    assert "采用方案 A" in prompt
    assert "张三" in prompt and "李四" in prompt
    assert "未决问题" not in prompt  # empty section omitted


def test_scene_state_prompt_truncates():
    s = SceneState(scope="x", decisions=["很长的决策" * 500])
    assert len(s.to_prompt(max_chars=120)) <= 120


def _hint(scope: str, text: str, ts: str) -> Intent:
    return Intent(
        kind="meeting_hint",
        scope=scope,
        rationale=text[:200],
        ts=ts,
        payload={"text": text},
        evidence=[IntentEvidence(source="meeting_transcript", ref_id=scope, quote=text[:120])],
    )


def test_distinct_content_intents_coexist_identical_suppressed(ac_root):
    """Hints have no temporal anchor → key on content, not on (when, with)."""
    with fts.cursor() as conn:
        a = sink.persist_intent(conn, _hint("meeting-1", "确认下周预算", "2026-05-31T10:00:00"))
        b = sink.persist_intent(conn, _hint("meeting-1", "张三补充材料", "2026-05-31T10:01:00"))
        dup = sink.persist_intent(conn, _hint("meeting-1", "确认下周预算", "2026-05-31T10:02:00"))

        assert a is not None and b is not None
        assert dup is None  # identical content in same scene is suppressed

        rows = intent_store.intents_for_scope(conn, "meeting-1")
        assert len(rows) == 2
        assert {r.payload["text"] for r in rows} == {"确认下周预算", "张三补充材料"}


def test_intents_for_scope_isolates_by_scope(ac_root):
    with fts.cursor() as conn:
        sink.persist_intent(conn, _hint("meeting-1", "甲", "2026-05-31T10:00:00"))
        sink.persist_intent(conn, _hint("meeting-2", "乙", "2026-05-31T10:00:00"))
        assert len(intent_store.intents_for_scope(conn, "meeting-1")) == 1
        assert len(intent_store.intents_for_scope(conn, "meeting-2")) == 1

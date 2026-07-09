"""P3 — chat write-back converges to the unified store; dream/pattern consume
the unified intent stream.

Mechanism-level coverage:
- chat-extracted memory lands in the structured ``entries`` store + FTS (so
  ``search_memory`` finds it), with type→prefix mapping and content dedup
- pattern_detector + dream render recognized intents as a candidate section
"""

from __future__ import annotations

from datetime import datetime, timedelta

from persome.intent import sink
from persome.intent import store as intent_store
from persome.intent.ontology import Intent, IntentEvidence
from persome.store import fts

# ─── Half A: chat write-back → unified store ────────────────────────────────


def test_chat_memory_writes_through_entries_and_is_searchable(ac_root):
    from datetime import datetime, timedelta

    from persome.chat import memory_extractor as cme

    with fts.cursor() as conn:
        cme._write_memory(
            conn,
            {
                "type": "user",
                "name": "coffee-habit",
                "description": "用户的咖啡偏好",
                "content": "用户每天早上喝手冲耶加雪菲 pourover coffee",
            },
        )
        # landed in the structured entries store (not an orphan markdown file):
        # it shows up in the FTS recency feed the rest of the pipeline reads.
        recent = fts.recent(
            conn,
            since=(datetime.now().astimezone() - timedelta(minutes=5)).isoformat(),
            limit=50,
            prefix_filter=["user"],
            include_superseded=False,
        )
        assert any("pourover coffee" in (r.content or "") for r in recent)
        # and an English token is searchable via FTS
        hits = fts.search(conn, query="pourover", top_k=5)
        assert any("pourover" in (h.content or "") for h in hits)


def test_chat_memory_type_to_prefix_mapping(ac_root):
    from persome.chat import memory_extractor as cme
    from persome.store import files as files_mod

    with fts.cursor() as conn:
        # feedback → user-, reference → topic-, plain name gets prefixed
        cme._write_memory(conn, {"type": "feedback", "name": "be-concise", "content": "回答要简洁"})
        cme._write_memory(
            conn, {"type": "reference", "name": "api-docs", "content": "见 openapi.json"}
        )
        # already-prefixed names are kept as-is (no double prefix)
        cme._write_memory(conn, {"type": "user", "name": "user-goals", "content": "今年学法语"})

    assert files_mod.memory_path("user-be-concise").exists()
    assert files_mod.memory_path("topic-api-docs").exists()
    assert files_mod.memory_path("user-goals").exists()
    assert not files_mod.memory_path("user-user-goals").exists()


def test_chat_memory_content_dedup(ac_root):
    from persome.chat import memory_extractor as cme
    from persome.store import files as files_mod

    mem = {"type": "user", "name": "user-fact", "content": "用户住在上海"}
    with fts.cursor() as conn:
        cme._write_memory(conn, mem)
        cme._write_memory(conn, dict(mem))  # identical → must not append twice

    parsed = files_mod.read_file(files_mod.memory_path("user-fact"))
    assert len(parsed.entries) == 1


# ─── Half B: dream / pattern_detector consume the unified intent stream ──────


def _seed_intent(conn, scope, text, ts):
    sink.persist_intent(
        conn,
        Intent(
            kind="meeting_hint",
            scope=scope,
            rationale=text[:200],
            ts=ts,
            payload={"text": text},
            evidence=[IntentEvidence(source="meeting_transcript", ref_id=scope)],
        ),
    )


def test_pattern_detector_renders_intents(ac_root):
    from persome.writer import pattern_detector as pd

    now = datetime.now().astimezone()
    with fts.cursor() as conn:
        _seed_intent(conn, "meeting-a", "下周三和张三对齐预算", now.isoformat())
        candidates = pd._collect_candidates(
            conn,
            lookback_start=now - timedelta(days=7),
            window_end=now + timedelta(minutes=1),
            min_occurrences=2,
        )
        assert "intents" in candidates
        ctx = pd._assemble_context(
            candidates=candidates, event_daily_path="event-x.md", session_id="s1"
        )
        assert "unified intent stream" in ctx
        assert "下周三和张三对齐预算" in ctx


def test_dream_renders_intents(ac_root):
    from persome.writer import dream

    now = datetime.now().astimezone()
    with fts.cursor() as conn:
        _seed_intent(conn, "meeting-b", "周五前确认场地", now.isoformat())
        intents = intent_store.recent_intents(
            conn,
            start=(now - timedelta(days=3)).isoformat(),
            end=(now + timedelta(minutes=1)).isoformat(),
        )
        ctx = dream._assemble_context(
            conn=conn,
            app_stats={},
            app_sequences=[],
            routines={},
            repeated_titles=[],
            repeated_urls=[],
            chat_pairs=[],
            lookback_days=3,
            intents=intents,
        )
        assert "周五前确认场地" in ctx
        assert "intent" in ctx

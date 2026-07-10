"""Chat write-back and pattern extraction converge on the unified store.

Mechanism-level coverage:
- chat-extracted memory lands in the structured ``entries`` store + FTS (so
  ``search_memory`` finds it), with type→prefix mapping and content dedup
- pattern_detector renders durable event memory with receipts
"""

from __future__ import annotations

from datetime import datetime, timedelta

from persome.store import entries as entries_store
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
                "description": "\u7528\u6237\u7684\u5496\u5561\u504f\u597d",
                "content": "\u7528\u6237\u6bcf\u5929\u65e9\u4e0a\u559d\u624b\u51b2\u8036\u52a0\u96ea\u83f2 pourover coffee",
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
        cme._write_memory(
            conn,
            {"type": "feedback", "name": "be-concise", "content": "\u56de\u7b54\u8981\u7b80\u6d01"},
        )
        cme._write_memory(
            conn, {"type": "reference", "name": "api-docs", "content": "\u89c1 openapi.json"}
        )
        # already-prefixed names are kept as-is (no double prefix)
        cme._write_memory(
            conn,
            {"type": "user", "name": "user-goals", "content": "\u4eca\u5e74\u5b66\u6cd5\u8bed"},
        )

    assert files_mod.memory_path("user-be-concise").exists()
    assert files_mod.memory_path("topic-api-docs").exists()
    assert files_mod.memory_path("user-goals").exists()
    assert not files_mod.memory_path("user-user-goals").exists()


def test_chat_memory_content_dedup(ac_root):
    from persome.chat import memory_extractor as cme
    from persome.store import files as files_mod

    mem = {"type": "user", "name": "user-fact", "content": "\u7528\u6237\u4f4f\u5728\u4e0a\u6d77"}
    with fts.cursor() as conn:
        cme._write_memory(conn, mem)
        cme._write_memory(conn, dict(mem))  # identical → must not append twice

    parsed = files_mod.read_file(files_mod.memory_path("user-fact"))
    assert len(parsed.entries) == 1


# ─── Half B: pattern_detector consumes durable activity sources ─────────────


def test_pattern_detector_renders_durable_event_memory(ac_root):
    from persome.writer import pattern_detector as pd

    now = datetime.now().astimezone()
    with fts.cursor() as conn:
        entries_store.create_file(
            conn,
            name="event-2026-07-10.md",
            description="Synthetic completed activity",
            tags=["event"],
        )
        entry_id = entries_store.append_entry(
            conn,
            name="event-2026-07-10.md",
            content="Reviewed the Persome runtime architecture.",
            tags=["work"],
        )
        candidates = pd._collect_candidates(
            conn,
            lookback_start=now - timedelta(days=7),
            window_end=now + timedelta(minutes=1),
            min_occurrences=2,
        )
        assert "event_memory" in candidates
        ctx = pd._assemble_context(
            candidates=candidates, event_daily_path="event-x.md", session_id="s1"
        )
        assert "Durable event memory" in ctx
        assert "Reviewed the Persome runtime architecture." in ctx
        assert f"⟨{entry_id}:event-2026-07-10.md⟩" in ctx

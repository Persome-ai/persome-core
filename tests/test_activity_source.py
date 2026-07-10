"""Durable ActivityEvent source and legacy namespace contract."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from persome.model.activity_source import (
    ActivityEvent,
    ActivitySource,
    is_activity_identity,
    normalize_activity_identity,
)
from persome.session import store as session_store
from persome.store import entries as entries_store
from persome.store import fts
from persome.timeline import store as timeline_store

TZ = timezone(timedelta(hours=8))


def _ensure_legacy_intents(conn) -> None:
    """Create the minimal old-table shape used by the read-only adapter test."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS intents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            scope TEXT NOT NULL,
            kind TEXT NOT NULL,
            confidence REAL NOT NULL,
            status TEXT NOT NULL,
            rationale TEXT NOT NULL,
            payload TEXT NOT NULL,
            evidence TEXT NOT NULL,
            dedup_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            resolution_outcome TEXT
        )
        """
    )


def _seed_sources(conn) -> str:
    _ensure_legacy_intents(conn)
    entries_store.create_file(
        conn,
        name="event-2026-07-10.md",
        description="Synthetic past activity",
        tags=["event"],
    )
    entry_id = entries_store.append_entry(
        conn,
        name="event-2026-07-10.md",
        content="Reviewed the Persome runtime architecture with Test Contact.",
        tags=["work"],
    )
    start = datetime(2026, 7, 10, 9, 0, tzinfo=TZ)
    end = start + timedelta(minutes=5)
    timeline_store.insert(
        conn,
        timeline_store.TimelineBlock(
            start_time=start,
            end_time=end,
            entries=["[Editor] revised the Runtime documentation with Test Contact"],
            apps_used=["Editor"],
            capture_count=1,
        ),
    )
    session_store.insert(
        conn,
        session_store.SessionRow(
            id="synthetic-session", start_time=start, end_time=end, status="reduced"
        ),
    )
    conn.execute(
        """
        INSERT INTO intents (
            ts, scope, kind, confidence, status, rationale, payload, evidence,
            dedup_key, created_at
        ) VALUES (?, 'synthetic', 'meeting', 0.9, 'consumed', ?, ?, '[]', 'done', ?)
        """,
        (
            end.isoformat(),
            "Reviewed the runtime with Test Contact.",
            json.dumps({"with": ["Test Contact"]}),
            end.isoformat(),
        ),
    )
    conn.execute(
        """
        INSERT INTO intents (
            ts, scope, kind, confidence, status, rationale, payload, evidence,
            dedup_key, created_at
        ) VALUES (?, 'synthetic', 'meeting', 0.8, 'open', 'Future meeting', '{}', '[]',
                  'open', ?)
        """,
        (end.isoformat(), end.isoformat()),
    )
    return entry_id


def test_activity_source_emits_only_canonical_auditable_ids(ac_root) -> None:
    with fts.cursor() as conn:
        entry_id = _seed_sources(conn)
        events = ActivitySource(
            conn,
            participant_resolver=lambda names, summary: [
                "person:test-contact" for _ in [0] if names or "Test Contact" in summary
            ],
        ).events()

    assert {event.stable_id for event in events} == {
        f"event:entry:{entry_id}",
        "event:session:synthetic-session",
        "event:intent:1",
    }
    assert all(event.source_receipt for event in events)
    assert all(event.participant_ids == ["person:test-contact"] for event in events)
    assert all(event.stable_id != "event:1" for event in events)


def test_activity_source_can_exclude_legacy_intents(ac_root) -> None:
    with fts.cursor() as conn:
        _seed_sources(conn)
        events = ActivitySource(conn, include_legacy_intents=False).events()
    assert {event.source_kind for event in events} == {"entry", "session"}
    assert all(not event.stable_id.startswith("event:intent:") for event in events)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("event:42", "event:intent:42"),
        ("event:intent:42", "event:intent:42"),
        ("event:entry:e1", "event:entry:e1"),
        ("event:session:s1", "event:session:s1"),
    ],
)
def test_legacy_namespace_adapter(raw: str, expected: str) -> None:
    assert normalize_activity_identity(raw) == expected
    assert is_activity_identity(raw)


def test_activity_event_rejects_bare_or_mismatched_identity() -> None:
    with pytest.raises(ValueError, match="stable_id"):
        ActivityEvent(
            stable_id="event:1",
            occurred_at=None,
            summary="Synthetic activity",
            participant_ids=[],
            source_kind="intent",
            source_id="1",
            source_receipt="⟨1:intents⟩",
        )

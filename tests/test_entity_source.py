"""Neutral entity/person source derived from durable classifier memory."""

from __future__ import annotations

from datetime import UTC, datetime

from persome.evomem.models import MemoryLayer, MemoryNode
from persome.evomem.store import NodeStore
from persome.model.entity_source import EntitySource, MemoryPersonNameSource
from persome.store import entries as entries_store
from persome.store import fts


def _seed_person_memory() -> str:
    node_id = "point-person-alex"
    NodeStore().save(
        MemoryNode(
            node_id=node_id,
            content="Alex reviews architecture decisions with the user.",
            layer=MemoryLayer.L2_FACT,
            file_name="person-alex.md",
            confidence="high",
            occurred_at="2026-07-10T08:00:00+00:00",
            memory_at=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
        )
    )
    return node_id


def test_entity_source_reads_person_fact_and_event_receipts(ac_root) -> None:
    node_id = _seed_person_memory()
    with fts.cursor() as conn:
        entries_store.create_file(
            conn,
            name="event-2026-07-10.md",
            description="Synthetic activity",
            tags=["event"],
        )
        entry_id = entries_store.append_entry(
            conn,
            name="event-2026-07-10.md",
            content="Reviewed the runtime architecture with Alex.",
            tags=["work"],
        )
        events = EntitySource(conn).events()

    assert {event.source_kind for event in events} == {"point", "entry"}
    assert {event.entity_id for event in events} == {"alex"}
    receipts = {event.source_receipt for event in events}
    assert f"⟨{node_id}:person-alex.md⟩" in receipts
    assert f"⟨{entry_id}:event-2026-07-10.md⟩" in receipts


def test_memory_person_source_implements_person_graph_seam(ac_root) -> None:
    _seed_person_memory()
    events = MemoryPersonNameSource().events()
    assert [event.name for event in events] == ["alex"]
    assert events[0].summary == "Alex reviews architecture decisions with the user."
    assert events[0].confidence == 0.95

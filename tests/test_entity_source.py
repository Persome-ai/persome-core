"""Neutral entity/person source derived from durable classifier memory."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from types import SimpleNamespace

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


def test_event_mentions_keep_only_person_specific_lines(ac_root) -> None:
    NodeStore().save(
        MemoryNode(
            node_id="point-person-kevin",
            content="Kevin is a launch collaborator.",
            layer=MemoryLayer.L2_FACT,
            file_name="person-kevin.md",
            confidence="high",
        )
    )
    with fts.cursor() as conn:
        entries_store.create_file(
            conn,
            name="event-2026-07-10.md",
            description="Synthetic mixed activity",
            tags=["event"],
        )
        entries_store.append_entry(
            conn,
            name="event-2026-07-10.md",
            content=(
                "The user adjusted a private investment portfolio.\n"
                "- [Feishu] Kevin reviewed the launch checklist.\n"
                "- [Chrome] The user opened a banking dashboard."
            ),
            tags=["work"],
        )
        mention = next(
            event for event in EntitySource(conn).events() if event.source_kind == "entry"
        )

    assert mention.summary == "- [Feishu] Kevin reviewed the launch checklist."
    assert "investment" not in mention.summary


def test_memory_person_source_filters_configured_owner_alias(ac_root) -> None:
    node_id = "point-person-owner"
    NodeStore().save(
        MemoryNode(
            node_id=node_id,
            content="Singularity-tian opened a pull request.",
            layer=MemoryLayer.L2_FACT,
            file_name="person-singularity-tian.md",
            confidence="high",
        )
    )
    cfg = SimpleNamespace(memory_delta=SimpleNamespace(owner_aliases=["Singularity-tian"]))

    assert MemoryPersonNameSource(cfg=cfg).events() == []


def test_entity_source_limit_uses_actual_instant_across_offsets(ac_root) -> None:
    for node_id, name, timestamp in (
        ("point-person-older", "older", "2025-11-02T10:00:00+08:00"),
        ("point-person-newer", "newer", "2025-11-02T03:00:00+00:00"),
    ):
        NodeStore().save(
            MemoryNode(
                node_id=node_id,
                content=f"{name} person fact",
                layer=MemoryLayer.L2_FACT,
                file_name=f"person-{name}.md",
                confidence="high",
                occurred_at=timestamp,
                memory_at=datetime.fromisoformat(timestamp),
            )
        )

    with fts.cursor() as conn:
        events = EntitySource(conn, limit=1).events()

    assert [(event.entity_id, event.summary) for event in events] == [
        ("newer", "newer person fact")
    ]


def test_entity_source_compares_legacy_naive_entry_to_aware_point(ac_root, monkeypatch) -> None:
    original_timezone = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    try:
        NodeStore().save(
            MemoryNode(
                node_id="point-person-alex-newer",
                content="newer aware point",
                layer=MemoryLayer.L2_FACT,
                file_name="person-alex.md",
                confidence="high",
                occurred_at="2026-07-11T03:00:00+00:00",
                memory_at=datetime.fromisoformat("2026-07-11T03:00:00+00:00"),
            )
        )
        with fts.cursor() as conn:
            entries_store.create_file(
                conn,
                name="event-legacy.md",
                description="legacy local event",
                tags=["event"],
            )
            entry_id = entries_store.append_entry(
                conn,
                name="event-legacy.md",
                content="older local event with Alex",
                tags=["work"],
            )
            conn.execute(
                "UPDATE entries SET timestamp='2026-07-11T10:00' WHERE id=?",
                (entry_id,),
            )
            events = EntitySource(conn, limit=1).events()

        assert [(event.source_kind, event.summary) for event in events] == [
            ("point", "newer aware point")
        ]
    finally:
        if original_timezone is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_timezone
        time.tzset()

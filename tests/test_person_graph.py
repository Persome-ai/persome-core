"Tests for test person graph."

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from persome.evomem.engine import EvoMemory
from persome.evomem.models import MemoryLayer, MemoryStatus
from persome.evomem.person_graph import PersonEvent, PersonGraph
from persome.evomem.reconciler import Reconciler
from persome.store import fts
from persome.store import owner_aliases as owner_alias_store


def _no_llm(messages):
    raise AssertionError(
        "person_graph \u662f\u786e\u5b9a\u6027\u8def\u5f84\uff0c\u7edd\u4e0d\u8c03 LLM"
    )


def _mem() -> EvoMemory:
    return EvoMemory(user_id="u1", reconciler=Reconciler(llm_call=_no_llm))


def _on() -> SimpleNamespace:
    return SimpleNamespace(person_graph_enabled=True)


class _StaticSource:
    def __init__(self, events: list[PersonEvent]) -> None:
        self._events = events

    def events(self) -> list[PersonEvent]:
        return list(self._events)


def _ts(day: int, hour: int = 10) -> datetime:
    return datetime(2026, 6, day, hour, 0, tzinfo=UTC)


def test_ingest_lists_persons_and_single_timeline(ac_root):
    events = [
        PersonEvent(
            name="\u5f20\u4e09",
            summary="\u5728\u4e00\u6b21 meeting \u573a\u666f",
            occurred_at=_ts(18),
        ),
        PersonEvent(name="\u5f20\u4e09", summary="\u53c8\u4e00\u6b21 review", occurred_at=_ts(20)),
        PersonEvent(name="Alice", summary="\u90ae\u4ef6\u5f80\u6765", occurred_at=_ts(19)),
    ]
    graph = PersonGraph(_mem(), cfg=_on(), name_source=_StaticSource(events))
    touched = graph.ingest()

    assert set(touched) == {"\u5f20\u4e09", "Alice"} or touched.count("\u5f20\u4e09") == 2
    people = graph.list_persons()
    names = {p.canonical for p in people}
    assert names == {"\u5f20\u4e09", "Alice"}

    zhang = next(p for p in people if p.canonical == "\u5f20\u4e09")
    assert zhang.sightings == 2

    timeline = graph.person_timeline("\u5f20\u4e09")
    assert len(timeline) == 2

    contents = [n.content for n in timeline]
    assert contents == ["\u5728\u4e00\u6b21 meeting \u573a\u666f", "\u53c8\u4e00\u6b21 review"]

    assert all("\u90ae\u4ef6" not in n.content for n in timeline)


def test_events_and_entities_are_evo_nodes_via_public_entrance(ac_root):
    mem = _mem()
    graph = PersonGraph(mem, cfg=_on(), name_source=_StaticSource([]))
    graph.record(
        PersonEvent(name="\u5f20\u4e09", summary="\u4e00\u6b21\u4f1a", occurred_at=_ts(18))
    )

    heads = mem.store.all_latest()
    entities = [n for n in heads if "person-entity" in n.tags.split()]
    evcount = [n for n in heads if "person-event" in n.tags.split()]
    assert len(entities) == 1
    assert entities[0].layer is MemoryLayer.L5_KNOWLEDGE
    assert entities[0].status is MemoryStatus.ACTIVE
    assert entities[0].file_name == "person-\u5f20\u4e09.md"
    assert len(evcount) == 1


def test_aliases_merge_into_one_entity(ac_root):
    mem = _mem()
    graph = PersonGraph(mem, cfg=_on(), name_source=_StaticSource([]))

    graph.record(
        PersonEvent(
            name="\u5f20\u4e09", aliases=["Zhang San"], occurred_at=_ts(18), summary="\u4f1a\u8bae"
        )
    )

    graph.record(
        PersonEvent(name="Zhang San", occurred_at=_ts(20), summary="\u4ee3\u7801\u8bc4\u5ba1")
    )

    graph.record(PersonEvent(name="zhang san", occurred_at=_ts(21), summary="\u5348\u9910"))

    people = graph.list_persons()
    assert len(people) == 1, [p.canonical for p in people]
    entity = people[0]
    assert entity.canonical == "\u5f20\u4e09"
    assert entity.sightings == 3

    norm_aliases = {a.casefold() for a in entity.aliases}
    assert "zhang san" in norm_aliases
    assert "\u5f20\u4e09" in {a for a in entity.aliases}

    active_entities = [n for n in mem.store.all_latest() if "person-entity" in n.tags.split()]
    assert len(active_entities) == 1

    assert len(graph.person_timeline("\u5f20\u4e09")) == 3
    assert len(graph.person_timeline("Zhang San")) == 3


def test_distinct_people_stay_separate(ac_root):
    graph = PersonGraph(_mem(), cfg=_on(), name_source=_StaticSource([]))
    graph.record(PersonEvent(name="\u5f20\u4e09", occurred_at=_ts(18), summary="a"))
    graph.record(PersonEvent(name="\u674e\u56db", occurred_at=_ts(19), summary="b"))
    assert len(graph.list_persons()) == 2


def test_same_source_event_is_idempotent_across_enrichment_ticks(ac_root):
    graph = PersonGraph(_mem(), cfg=_on(), name_source=_StaticSource([]))
    event = PersonEvent(
        name="Kevin",
        summary="Kevin reviewed the launch plan.",
        occurred_at=_ts(18),
        source_id="entity:entry:event-1:person:kevin",
    )

    graph.record(event)
    graph.record(event)

    person = graph.list_persons()[0]
    assert person.sightings == 1
    assert len(graph.person_timeline("Kevin")) == 1


def test_pending_owner_alias_never_mints_person(ac_root):
    with fts.cursor() as conn:
        owner_alias_store.record_evidence(
            conn,
            alias="Singularity-tian",
            session_id="owner-session-1",
            source_kind=owner_alias_store.SOURCE_OWNED_ACCOUNT,
            quote="own GitHub account Singularity-tian",
            confidence=0.9,
        )

    graph = PersonGraph(_mem(), cfg=_on(), name_source=_StaticSource([]))
    assert graph.record(PersonEvent(name="Singularity-tian", summary="opened a PR")) is None
    assert graph.list_persons() == []


def test_shared_alias_distinct_people_do_not_merge(ac_root):
    graph = PersonGraph(_mem(), cfg=_on(), name_source=_StaticSource([]))
    graph.record(PersonEvent(name="Alex Chen", aliases=["Alex"], occurred_at=_ts(18), summary="a"))
    graph.record(PersonEvent(name="Alex Wong", aliases=["Alex"], occurred_at=_ts(19), summary="b"))
    assert len(graph.list_persons()) == 2


# ── build_person_context ─────────────────────────────────────────────────────


def test_build_person_context_nonempty_with_recent_interactions(ac_root):
    graph = PersonGraph(_mem(), cfg=_on(), name_source=_StaticSource([]))
    graph.record(
        PersonEvent(
            name="\u5f20\u4e09",
            aliases=["Zhang San"],
            category="colleague",
            occurred_at=_ts(18, 14),
            summary="\u5728\u4e00\u6b21 meeting \u573a\u666f",
        )
    )
    graph.record(
        PersonEvent(name="\u5f20\u4e09", occurred_at=_ts(20, 9), summary="\u4ee3\u7801\u8bc4\u5ba1")
    )

    block = graph.build_person_context("\u5f20\u4e09")
    assert block
    assert "\u5f20\u4e09" in block
    assert "colleague" in block
    assert "Zhang San" in block
    assert "2 interaction(s)" in block

    assert "\u4ee3\u7801\u8bc4\u5ba1" in block
    assert "2026-06-20" in block

    assert block.index("\u4ee3\u7801\u8bc4\u5ba1") < block.index("meeting")


def test_build_person_context_unknown_returns_empty(ac_root):
    graph = PersonGraph(_mem(), cfg=_on(), name_source=_StaticSource([]))
    assert graph.build_person_context("\u67e5\u65e0\u6b64\u4eba") == ""
    assert graph.build_person_context("") == ""


def test_disabled_by_default_is_noop(ac_root):
    mem = _mem()

    graph = PersonGraph(
        mem,
        name_source=_StaticSource([PersonEvent(name="\u5f20\u4e09", occurred_at=_ts(18))]),
    )
    assert graph.enabled is False
    assert graph.ingest() == []
    assert graph.record(PersonEvent(name="\u674e\u56db", occurred_at=_ts(18))) is None
    assert graph.list_persons() == []

    assert [n for n in mem.store.all_latest() if "person" in n.tags] == []


def test_explicit_off_cfg_is_noop(ac_root):
    cfg = SimpleNamespace(person_graph_enabled=False)
    graph = PersonGraph(
        _mem(), cfg=cfg, name_source=_StaticSource([PersonEvent(name="\u5f20\u4e09")])
    )
    assert graph.ingest() == []


def test_low_confidence_first_sighting_skipped(ac_root):
    graph = PersonGraph(_mem(), cfg=_on(), name_source=_StaticSource([]), min_confidence=0.6)
    res = graph.record(PersonEvent(name="\u8def\u4eba\u7532", confidence=0.2, occurred_at=_ts(18)))
    assert res is None
    assert graph.list_persons() == []


def test_low_confidence_but_already_known_is_recorded(ac_root):
    graph = PersonGraph(_mem(), cfg=_on(), name_source=_StaticSource([]), min_confidence=0.6)
    graph.record(PersonEvent(name="\u5f20\u4e09", confidence=0.9, occurred_at=_ts(18)))
    res = graph.record(PersonEvent(name="\u5f20\u4e09", confidence=0.1, occurred_at=_ts(20)))
    assert res == "\u5f20\u4e09"
    assert graph.list_persons()[0].sightings == 2


def test_seen_once_flag_and_min_sightings_filter(ac_root):
    graph = PersonGraph(_mem(), cfg=_on(), name_source=_StaticSource([]))
    graph.record(PersonEvent(name="\u5f20\u4e09", occurred_at=_ts(18)))
    graph.record(PersonEvent(name="\u5f20\u4e09", occurred_at=_ts(20)))
    graph.record(PersonEvent(name="\u674e\u56db", occurred_at=_ts(19)))

    assert {p.canonical for p in graph.list_persons()} == {"\u5f20\u4e09", "\u674e\u56db"}

    filtered = graph.list_persons(min_sightings=2)
    assert {p.canonical for p in filtered} == {"\u5f20\u4e09"}
    lisi = next(p for p in graph.list_persons() if p.canonical == "\u674e\u56db")
    assert lisi.seen_once is True


def test_empty_or_invalid_name_is_ignored(ac_root):
    graph = PersonGraph(_mem(), cfg=_on(), name_source=_StaticSource([]))
    assert graph.record(PersonEvent(name="   ", occurred_at=_ts(18))) is None
    assert graph.record(PersonEvent(name="", occurred_at=_ts(18))) is None
    assert graph.list_persons() == []


def test_person_timeline_survives_mixed_naive_and_aware_timestamps(ac_root):
    graph = PersonGraph(_mem(), cfg=_on())
    graph.record(
        PersonEvent(name="\u5f20\u4e09", summary="aware \u4e8b\u4ef6", occurred_at=_ts(18))
    )
    graph.record(
        PersonEvent(
            name="\u5f20\u4e09",
            summary="naive \u4e8b\u4ef6",
            occurred_at=datetime(2026, 6, 19, 10, 0),
        )
    )
    timeline = graph.person_timeline("\u5f20\u4e09")
    assert len(timeline) == 2

    assert "aware" in (timeline[0].content or "") or "aware" in str(timeline[0].__dict__)

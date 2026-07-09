"""他人关系图（person relationship graph）—— evomem 之上的他人目录。

覆盖验收点（spec E1 / TODO #1）：

- 喂入（经 seam 注入）含人名的事件 → 列出 person 实体 + 单人时间线；
- 同一个人不同别名/写法 → 合并到同一实体（经 evomem SUPERSEDE，不新建重复）；
- ``build_person_context`` 返回非空、含该人最近交互；
- 开关 off（默认）→ 不建图（no-op）；
- 只见一次的低置信人名按保守策略处理（不报错、不上提）。

写入只走 evomem 公共写入口（``add_direct`` / ``commit_*``）；播种 fixture 用注入的
假 name source（不触发 intent recognizer）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from persome.evomem.engine import EvoMemory
from persome.evomem.models import MemoryLayer, MemoryStatus
from persome.evomem.person_graph import (
    IntentPersonNameSource,
    PersonEvent,
    PersonGraph,
)
from persome.evomem.reconciler import Reconciler


def _no_llm(messages):
    raise AssertionError("person_graph 是确定性路径，绝不调 LLM")


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


# ── 列实体 + 单人时间线 ──────────────────────────────────────────────────────


def test_ingest_lists_persons_and_single_timeline(ac_root):
    events = [
        PersonEvent(name="张三", summary="在一次 meeting 场景", occurred_at=_ts(18)),
        PersonEvent(name="张三", summary="又一次 review", occurred_at=_ts(20)),
        PersonEvent(name="Alice", summary="邮件往来", occurred_at=_ts(19)),
    ]
    graph = PersonGraph(_mem(), cfg=_on(), name_source=_StaticSource(events))
    touched = graph.ingest()

    assert set(touched) == {"张三", "Alice"} or touched.count("张三") == 2
    people = graph.list_persons()
    names = {p.canonical for p in people}
    assert names == {"张三", "Alice"}

    zhang = next(p for p in people if p.canonical == "张三")
    assert zhang.sightings == 2

    timeline = graph.person_timeline("张三")
    assert len(timeline) == 2
    # 旧→新排序
    contents = [n.content for n in timeline]
    assert contents == ["在一次 meeting 场景", "又一次 review"]
    # 单人时间线不串入 Alice 的事件
    assert all("邮件" not in n.content for n in timeline)


def test_events_and_entities_are_evo_nodes_via_public_entrance(ac_root):
    """实体 + 事件都作为 evo_nodes 节点存在（走公共写口），且实体唯一活跃头。"""
    mem = _mem()
    graph = PersonGraph(mem, cfg=_on(), name_source=_StaticSource([]))
    graph.record(PersonEvent(name="张三", summary="一次会", occurred_at=_ts(18)))

    heads = mem.store.all_latest()
    entities = [n for n in heads if "person-entity" in n.tags.split()]
    evcount = [n for n in heads if "person-event" in n.tags.split()]
    assert len(entities) == 1
    assert entities[0].layer is MemoryLayer.L5_KNOWLEDGE
    assert entities[0].status is MemoryStatus.ACTIVE
    assert entities[0].file_name == "person-张三.md"
    assert len(evcount) == 1


# ── 别名/写法合并：不新建重复实体 ─────────────────────────────────────────────


def test_aliases_merge_into_one_entity(ac_root):
    """同一个人不同别名/写法 → 合并到同一实体（经 SUPERSEDE，不新建重复）。"""
    mem = _mem()
    graph = PersonGraph(mem, cfg=_on(), name_source=_StaticSource([]))
    # 第一次：张三，带别名 "Zhang San"
    graph.record(
        PersonEvent(name="张三", aliases=["Zhang San"], occurred_at=_ts(18), summary="会议")
    )
    # 第二次：用别名 "Zhang San" 出现 → 必须归到同一实体
    graph.record(PersonEvent(name="Zhang San", occurred_at=_ts(20), summary="代码评审"))
    # 第三次：全角空白/casefold 变体
    graph.record(PersonEvent(name="zhang san", occurred_at=_ts(21), summary="午餐"))

    people = graph.list_persons()
    assert len(people) == 1, [p.canonical for p in people]
    entity = people[0]
    assert entity.canonical == "张三"
    assert entity.sightings == 3
    # 别名并集
    norm_aliases = {a.casefold() for a in entity.aliases}
    assert "zhang san" in norm_aliases
    assert "张三" in {a for a in entity.aliases}

    # 退役的旧实体头确实进了链尾（演化链不分叉：恰好一个活跃实体头）
    active_entities = [n for n in mem.store.all_latest() if "person-entity" in n.tags.split()]
    assert len(active_entities) == 1
    # 时间线 = 3 条事件，都归到同一人
    assert len(graph.person_timeline("张三")) == 3
    assert len(graph.person_timeline("Zhang San")) == 3  # 别名也能查到


def test_distinct_people_stay_separate(ac_root):
    graph = PersonGraph(_mem(), cfg=_on(), name_source=_StaticSource([]))
    graph.record(PersonEvent(name="张三", occurred_at=_ts(18), summary="a"))
    graph.record(PersonEvent(name="李四", occurred_at=_ts(19), summary="b"))
    assert len(graph.list_persons()) == 2


def test_shared_alias_distinct_people_do_not_merge(ac_root):
    """两个不同的人共用一个泛化名字别名（都 "Alex"）→ 必须保持两个实体。

    回归:_find_entity 旧实现用别名集交非空即合并,会把 "Alex Chen" 和 "Alex Wong"
    错并成一人。修复后要求 canonical 一侧吻合,故二者分立;而下方仍验证同一个人的
    真别名照常折叠(test_aliases_merge_into_one_entity)。
    """
    graph = PersonGraph(_mem(), cfg=_on(), name_source=_StaticSource([]))
    graph.record(PersonEvent(name="Alex Chen", aliases=["Alex"], occurred_at=_ts(18), summary="a"))
    graph.record(PersonEvent(name="Alex Wong", aliases=["Alex"], occurred_at=_ts(19), summary="b"))
    assert len(graph.list_persons()) == 2


# ── build_person_context ─────────────────────────────────────────────────────


def test_build_person_context_nonempty_with_recent_interactions(ac_root):
    graph = PersonGraph(_mem(), cfg=_on(), name_source=_StaticSource([]))
    graph.record(
        PersonEvent(
            name="张三",
            aliases=["Zhang San"],
            category="colleague",
            occurred_at=_ts(18, 14),
            summary="在一次 meeting 场景",
        )
    )
    graph.record(PersonEvent(name="张三", occurred_at=_ts(20, 9), summary="代码评审"))

    block = graph.build_person_context("张三")
    assert block
    assert "张三" in block
    assert "colleague" in block
    assert "Zhang San" in block
    assert "共 2 次交互" in block
    # 含最近交互摘要 + 时间戳
    assert "代码评审" in block
    assert "2026-06-20" in block
    # 最近交互排在前（评审在会议之前出现）
    assert block.index("代码评审") < block.index("meeting")


def test_build_person_context_unknown_returns_empty(ac_root):
    graph = PersonGraph(_mem(), cfg=_on(), name_source=_StaticSource([]))
    assert graph.build_person_context("查无此人") == ""
    assert graph.build_person_context("") == ""


# ── 开关 off（默认）→ no-op ──────────────────────────────────────────────────


def test_disabled_by_default_is_noop(ac_root):
    mem = _mem()
    # 默认无 cfg → person_graph_enabled 兜底 False
    graph = PersonGraph(
        mem,
        name_source=_StaticSource([PersonEvent(name="张三", occurred_at=_ts(18))]),
    )
    assert graph.enabled is False
    assert graph.ingest() == []
    assert graph.record(PersonEvent(name="李四", occurred_at=_ts(18))) is None
    assert graph.list_persons() == []
    # 没有任何 person 节点写入
    assert [n for n in mem.store.all_latest() if "person" in n.tags] == []


def test_explicit_off_cfg_is_noop(ac_root):
    cfg = SimpleNamespace(person_graph_enabled=False)
    graph = PersonGraph(_mem(), cfg=cfg, name_source=_StaticSource([PersonEvent(name="张三")]))
    assert graph.ingest() == []


# ── 隐私保守：只见一次的低置信人名 ────────────────────────────────────────────


def test_low_confidence_first_sighting_skipped(ac_root):
    """低置信 + 此前从未见过 → 不上提（不建实体），但不报错。"""
    graph = PersonGraph(_mem(), cfg=_on(), name_source=_StaticSource([]), min_confidence=0.6)
    res = graph.record(PersonEvent(name="路人甲", confidence=0.2, occurred_at=_ts(18)))
    assert res is None
    assert graph.list_persons() == []


def test_low_confidence_but_already_known_is_recorded(ac_root):
    """已知的人即使后续低置信出现，仍记录（重复出现是真实信号，不受隐私门限制）。"""
    graph = PersonGraph(_mem(), cfg=_on(), name_source=_StaticSource([]), min_confidence=0.6)
    graph.record(PersonEvent(name="张三", confidence=0.9, occurred_at=_ts(18)))
    res = graph.record(PersonEvent(name="张三", confidence=0.1, occurred_at=_ts(20)))
    assert res == "张三"
    assert graph.list_persons()[0].sightings == 2


def test_seen_once_flag_and_min_sightings_filter(ac_root):
    graph = PersonGraph(_mem(), cfg=_on(), name_source=_StaticSource([]))
    graph.record(PersonEvent(name="张三", occurred_at=_ts(18)))
    graph.record(PersonEvent(name="张三", occurred_at=_ts(20)))
    graph.record(PersonEvent(name="李四", occurred_at=_ts(19)))  # 只见一次

    assert {p.canonical for p in graph.list_persons()} == {"张三", "李四"}
    # min_sightings=2 → 滤掉只见一次的李四
    filtered = graph.list_persons(min_sightings=2)
    assert {p.canonical for p in filtered} == {"张三"}
    lisi = next(p for p in graph.list_persons() if p.canonical == "李四")
    assert lisi.seen_once is True


def test_empty_or_invalid_name_is_ignored(ac_root):
    graph = PersonGraph(_mem(), cfg=_on(), name_source=_StaticSource([]))
    assert graph.record(PersonEvent(name="   ", occurred_at=_ts(18))) is None
    assert graph.record(PersonEvent(name="", occurred_at=_ts(18))) is None
    assert graph.list_persons() == []


# ── 默认 name source：从 intents 表读 payload.with ───────────────────────────


def test_intent_name_source_reads_payload_with(ac_root):
    """默认 seam 从 intents 表的 payload.with 提取人名事件（只读 recognizer 产出）。"""
    import json

    from persome.intent import store as intent_store
    from persome.store import fts

    with fts.cursor() as conn:
        intent_store.ensure_schema(conn)
        conn.execute(
            "INSERT INTO intents (ts, scope, kind, confidence, status, rationale, "
            "payload, evidence, dedup_key, created_at) "
            "VALUES (?, ?, ?, ?, 'open', ?, ?, '[]', ?, ?)",
            (
                _ts(20).isoformat(),
                "timeline",
                "meeting",
                0.9,
                "周会同步",
                json.dumps({"with": ["王五", "Bob"]}, ensure_ascii=False),
                "k1",
                _ts(20).isoformat(),
            ),
        )

    source = IntentPersonNameSource()
    events = source.events()
    names = {e.name for e in events}
    assert names == {"王五", "Bob"}
    assert all(e.confidence == 0.9 for e in events)
    assert all(e.summary == "周会同步" for e in events)

    # 端到端：用默认 seam 入图
    graph = PersonGraph(_mem(), cfg=_on(), name_source=source)
    graph.ingest()
    assert {p.canonical for p in graph.list_persons()} == {"王五", "Bob"}


def test_intent_name_source_failsafe_on_bad_data(ac_root):
    """来源读不到/坏数据 → 空列表，绝不抛。"""

    def bad_factory():
        raise RuntimeError("db down")

    source = IntentPersonNameSource(conn_factory=bad_factory)
    assert source.events() == []


# ── 混合 naive/aware 时间戳（2026-07-03 生产 bootstrap 实测炸点）────────────────


def test_person_timeline_survives_mixed_naive_and_aware_timestamps(ac_root):
    """真实存量库里 occurred_at 混着 naive 与 aware ISO 串（分钟粒度老行 vs 带时区
    新行）——person_timeline 的排序曾直接 TypeError，被 relation extractor 的
    fail-safe 吞成「0 人、0 边」。_parse_ts 现在统一归一化为 aware（naive 视为
    UTC），排序键的 memory_at/gmt_created 回退同样归一。"""
    graph = PersonGraph(_mem(), cfg=_on())
    graph.record(PersonEvent(name="张三", summary="aware 事件", occurred_at=_ts(18)))
    graph.record(
        PersonEvent(
            name="张三",
            summary="naive 事件",
            occurred_at=datetime(2026, 6, 19, 10, 0),  # 无时区 — 老行形态
        )
    )
    timeline = graph.person_timeline("张三")
    assert len(timeline) == 2
    # 排序稳定：aware(6-18) 在 naive(6-19,按 UTC 解读) 之前
    assert "aware" in (timeline[0].content or "") or "aware" in str(timeline[0].__dict__)

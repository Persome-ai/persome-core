"""Tests for relation-edge extraction (P0-2 / #428) — deterministic + LLM → shadow.

Working object = the PAST layer: the extractor reads consolidated person_graph entities +
interaction timelines (evo_nodes), NOT the live intents table. So these tests seed person_graph
(via its deterministic ingest), then assert the shadow edges.

Covers: SELF↔person + co-occurrence person↔person ``knows``, single-person, disabled=no-op,
idempotent dedup, LLM ``reports_to`` grounded by a quote from the past context, LLM precision
gates (ungrounded / off-roster / off-predicate / low-confidence), fail-open, empty.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from persome.evomem import relation_extractor as rx
from persome.evomem.engine import EvoMemory
from persome.evomem.person_graph import PersonEvent, PersonGraph
from persome.evomem.reconciler import Reconciler
from persome.store import entries as entries_store
from persome.store import fts
from persome.store import relation_edges as edges


def _no_llm(messages):
    raise AssertionError("person_graph ingest is deterministic; must not call LLM")


def _mem() -> EvoMemory:
    return EvoMemory(user_id="u1", reconciler=Reconciler(llm_call=_no_llm))


def _on() -> SimpleNamespace:
    return SimpleNamespace(relation_extraction_enabled=True)


def _ts(day: int, hour: int = 10) -> datetime:
    return datetime(2026, 6, day, hour, 0, tzinfo=UTC)


class _StaticSource:
    def __init__(self, events):
        self._events = events

    def events(self):
        return list(self._events)


def _ingest(mem, events):
    """Consolidate people into person_graph (the past layer) so the extractor can read them."""
    PersonGraph(
        mem, cfg=SimpleNamespace(person_graph_enabled=True), name_source=_StaticSource(events)
    ).ingest()


def _all_edges():
    with fts.cursor() as conn:
        edges.ensure_schema(conn)
        conn.row_factory = None
        return conn.execute(
            "SELECT src_identity, dst_identity, predicate, status, provenance FROM relation_edges"
        ).fetchall()


def _empty_llm(*_a, **_k):
    from persome.writer.llm import _build_response

    return _build_response("")


def _scripted_llm(payload):
    from persome.writer.llm import _build_response

    text = json.dumps(payload, ensure_ascii=False)

    def call(*_a, **_k):
        return _build_response(text)

    return call


# ── deterministic pass (over the consolidated past) ─────────────────────────────


def test_deterministic_self_and_cooccurrence_knows(ac_root):
    mem = _mem()
    # Alice & Bob share an interaction time → co-occurrence
    _ingest(
        mem,
        [
            PersonEvent(name="Alice", summary="周会同步", occurred_at=_ts(20), confidence=0.9),
            PersonEvent(name="Bob", summary="周会同步", occurred_at=_ts(20), confidence=0.9),
        ],
    )
    res = rx.run_relation_extraction(
        _on(), memory=mem, llm_call=_empty_llm, conn_factory=fts.cursor
    )

    assert res.deterministic_count == 3  # self→Alice, self→Bob, Alice↔Bob
    assert res.llm_count == 0
    rows = _all_edges()
    assert all(r[2] == "knows" and r[3] == "shadow" and r[4] == "inferred" for r in rows)
    pairs = {(r[0], r[1]) for r in rows}
    assert ("self", "Alice") in pairs and ("self", "Bob") in pairs and ("Alice", "Bob") in pairs


def test_single_person_only_self_knows(ac_root):
    mem = _mem()
    _ingest(
        mem, [PersonEvent(name="Carol", summary="约了 Carol", occurred_at=_ts(21), confidence=0.9)]
    )
    res = rx.run_relation_extraction(
        _on(), memory=mem, llm_call=_empty_llm, conn_factory=fts.cursor
    )
    assert res.deterministic_count == 1
    assert ("self", "Carol") in {(r[0], r[1]) for r in _all_edges()}


def test_disabled_is_noop(ac_root):
    mem = _mem()
    _ingest(mem, [PersonEvent(name="Alice", summary="x", occurred_at=_ts(20), confidence=0.9)])
    cfg = SimpleNamespace(relation_extraction_enabled=False)
    res = rx.run_relation_extraction(cfg, memory=mem, llm_call=_empty_llm, conn_factory=fts.cursor)
    assert res.written_count == 0
    assert _all_edges() == []


def test_idempotent_across_runs(ac_root):
    mem = _mem()
    _ingest(
        mem,
        [
            PersonEvent(name="Alice", summary="周会", occurred_at=_ts(20), confidence=0.9),
            PersonEvent(name="Bob", summary="周会", occurred_at=_ts(20), confidence=0.9),
        ],
    )
    first = rx.run_relation_extraction(
        _on(), memory=mem, llm_call=_empty_llm, conn_factory=fts.cursor
    )
    second = rx.run_relation_extraction(
        _on(), memory=mem, llm_call=_empty_llm, conn_factory=fts.cursor
    )
    assert first.deterministic_count == 3
    assert second.deterministic_count == 0  # existing open edges not re-added
    assert second.reinforced == 0  # same evidence → MAX no-op, no fake reinforcement
    assert len(_all_edges()) == 3


def test_new_evidence_reinforces_strength(ac_root):
    """边强度=证据数：新交互到来 → observations 单调上涨（而非跳过）。"""
    mem = _mem()
    _ingest(mem, [PersonEvent(name="Alice", summary="第一次", occurred_at=_ts(20), confidence=0.9)])
    rx.run_relation_extraction(_on(), memory=mem, llm_call=_empty_llm, conn_factory=fts.cursor)

    # 第二次真实交互 → sightings 2 → 边应被强化而不是 skip
    _ingest(mem, [PersonEvent(name="Alice", summary="第二次", occurred_at=_ts(21), confidence=0.9)])
    res = rx.run_relation_extraction(
        _on(), memory=mem, llm_call=_empty_llm, conn_factory=fts.cursor
    )
    assert res.deterministic_count == 0  # no new edge
    assert res.reinforced >= 1  # strength grew

    with fts.cursor() as conn:
        conn.row_factory = None
        obs = conn.execute(
            "SELECT observations FROM relation_edges "
            "WHERE src_identity='self' AND dst_identity='Alice' AND predicate='knows'"
        ).fetchone()[0]
    assert obs == 2


def _seed_intent(
    *, status, people=(), rationale="做完的事", kind="reminder", iid=None, resolution_outcome=None
):
    ts = _ts(22).isoformat()
    with fts.cursor() as conn:
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
        conn.execute(
            "INSERT INTO intents (ts, scope, kind, confidence, status, rationale, "
            "payload, evidence, dedup_key, created_at, resolution_outcome) "
            "VALUES (?, 'timeline', ?, 0.9, ?, ?, ?, '[]', ?, ?, ?)",
            (
                ts,
                kind,
                status,
                rationale,
                json.dumps({"with": list(people)}, ensure_ascii=False),
                f"k-{status}-{resolution_outcome or ''}-{rationale}",
                ts,
                resolution_outcome,
            ),
        )


def test_terminal_intents_become_activity_points(ac_root):
    """生命周期切分：done 终态（consumed/resolved/completed）→ Activity(EVENT) 点 +
    participates_in 边；open/armed（将来）绝不进图。"""
    mem = _mem()
    _ingest(mem, [PersonEvent(name="Alice", summary="合作", occurred_at=_ts(20), confidence=0.9)])
    _seed_intent(status="consumed", people=["Alice"], rationale="和 Alice 定稿了方案")
    # resolved is DONE only with resolution_outcome='done' (#461) → this one enters.
    _seed_intent(status="resolved", resolution_outcome="done", rationale="接受了团队邀请")
    _seed_intent(status="open", rationale="下周要做的事")  # future → must NOT enter
    _seed_intent(status="armed", rationale="等触发的事")  # future → must NOT enter

    res = rx.run_relation_extraction(
        _on(), memory=mem, llm_call=_empty_llm, conn_factory=fts.cursor
    )
    rows = _all_edges()
    part = [r for r in rows if r[2] == "participates_in"]
    # consumed: self→event + Alice→event（已巩固的参与者）；resolved: self→event
    assert len(part) == 3
    assert all(r[1].startswith("event:") for r in part)
    srcs = sorted(r[0] for r in part)
    assert srcs == ["Alice", "self", "self"]
    # Legacy intent rows are read through the neutral Activity adapter and no
    # longer carry product status semantics into relation provenance.
    assert {r[4] for r in part} == {"inferred"}
    # open/armed 一条都没进：participates_in 只有 3 条
    assert res.written_count >= 3


def test_durable_event_entry_becomes_sourced_activity_edge(ac_root):
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
            content="Reviewed the Persome runtime architecture.",
            tags=["work"],
        )
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'intents'"
            ).fetchone()
            is None
        )

    result = rx.run_relation_extraction(
        _on(), memory=_mem(), llm_call=_empty_llm, conn_factory=fts.cursor
    )
    with fts.cursor() as conn:
        row = conn.execute(
            "SELECT dst_identity, source_kind, source_id, source_receipt "
            "FROM relation_edges WHERE predicate='participates_in'"
        ).fetchone()
    assert result.deterministic_count == 1
    assert tuple(row) == (
        f"event:entry:{entry_id}",
        "entry",
        entry_id,
        f"⟨{entry_id}:event-2026-07-10.md⟩",
    )


def test_resolved_rejected_is_not_an_activity(ac_root):
    """#461 tense gate：resolved 是证据驱动自动关闭通道，真实结局在 resolution_outcome。
    只有 resolution_outcome='done' 才是「真的发生过」；'rejected'（后来拒绝/没做）与
    'superseded'（被替换）都没有发生，绝不能作为 participates_in 活动入图。"""
    mem = _mem()
    _seed_intent(status="resolved", resolution_outcome="rejected", rationale="本来要去但拒绝了")
    _seed_intent(status="resolved", resolution_outcome="superseded", rationale="被后续替换掉的事")
    res = rx.run_relation_extraction(
        _on(), memory=mem, llm_call=_empty_llm, conn_factory=fts.cursor
    )
    part = [r for r in _all_edges() if r[2] == "participates_in"]
    assert part == [], f"rejected/superseded resolved intents must NOT enter the graph; got {part}"
    assert res.written_count == 0

    # ...but a resolved intent that DID happen (outcome='done') still enters, as before.
    _seed_intent(status="resolved", resolution_outcome="done", rationale="真的做完并接受了")
    rx.run_relation_extraction(_on(), memory=mem, llm_call=_empty_llm, conn_factory=fts.cursor)
    part2 = [r for r in _all_edges() if r[2] == "participates_in"]
    assert [r[0] for r in part2] == ["self"], f"resolved+done must produce self→event; got {part2}"
    assert all(r[1].startswith("event:") for r in part2)


def test_unconsolidated_participant_is_skipped(ac_root):
    """终态意图里的陌生名字（未经过 person_graph 巩固）不铸新点——只连已巩固的人。"""
    mem = _mem()
    _seed_intent(status="consumed", people=["陌生人甲"], rationale="和陌生人做完某事")
    rx.run_relation_extraction(_on(), memory=mem, llm_call=_empty_llm, conn_factory=fts.cursor)
    part = [r for r in _all_edges() if r[2] == "participates_in"]
    assert [r[0] for r in part] == ["self"]  # 只有 self→event，没有陌生人点


def test_no_people_is_empty(ac_root):
    res = rx.run_relation_extraction(
        _on(), memory=_mem(), llm_call=_empty_llm, conn_factory=fts.cursor
    )
    assert res.written_count == 0


# ── LLM pass (over the past interaction context) ────────────────────────────────


def test_llm_pass_adds_reports_to(ac_root):
    mem = _mem()
    _ingest(
        mem,
        [
            PersonEvent(
                name="Alice", summary="Alice 向 Boss 汇报进度", occurred_at=_ts(20), confidence=0.9
            ),
            PersonEvent(name="Boss", summary="听 Alice 汇报", occurred_at=_ts(21), confidence=0.9),
        ],
    )
    llm = _scripted_llm(
        [
            {
                "src": "Alice",
                "dst": "Boss",
                "predicate": "reports_to",
                "label": "老板",
                "quote": "Alice 向 Boss 汇报进度",
                "confidence": 0.9,
            }
        ]
    )
    res = rx.run_relation_extraction(_on(), memory=mem, llm_call=llm, conn_factory=fts.cursor)
    assert res.llm_count == 1
    reports = [r for r in _all_edges() if r[2] == "reports_to"]
    assert (
        reports
        and reports[0][0] == "Alice"
        and reports[0][1] == "Boss"
        and reports[0][3] == "shadow"
    )


def test_llm_drops_bad_relations(ac_root):
    mem = _mem()
    _ingest(
        mem,
        [
            PersonEvent(
                name="Alice", summary="Alice 向 Boss 汇报进度", occurred_at=_ts(20), confidence=0.9
            ),
            PersonEvent(name="Boss", summary="听汇报", occurred_at=_ts(21), confidence=0.9),
        ],
    )
    llm = _scripted_llm(
        [
            {
                "src": "Alice",
                "dst": "Boss",
                "predicate": "reports_to",
                "quote": "",
                "confidence": 0.9,
            },
            {
                "src": "Alice",
                "dst": "Boss",
                "predicate": "reports_to",
                "quote": "查无此句",
                "confidence": 0.9,
            },
            {
                "src": "Zed",
                "dst": "Boss",
                "predicate": "reports_to",
                "quote": "Alice 向 Boss 汇报进度",
                "confidence": 0.9,
            },
            {
                "src": "Alice",
                "dst": "Boss",
                "predicate": "participates_in",
                "quote": "Alice 向 Boss 汇报进度",
                "confidence": 0.9,
            },
            {
                "src": "Alice",
                "dst": "Boss",
                "predicate": "reports_to",
                "quote": "Alice 向 Boss 汇报进度",
                "confidence": 0.3,
            },
        ]
    )
    res = rx.run_relation_extraction(_on(), memory=mem, llm_call=llm, conn_factory=fts.cursor)
    assert res.llm_count == 0
    assert [r for r in _all_edges() if r[2] == "reports_to"] == []


def test_llm_failure_is_fail_open(ac_root):
    mem = _mem()
    _ingest(
        mem,
        [
            PersonEvent(name="Alice", summary="周会", occurred_at=_ts(20), confidence=0.9),
            PersonEvent(name="Bob", summary="周会", occurred_at=_ts(20), confidence=0.9),
        ],
    )

    def boom(*_a, **_k):
        raise RuntimeError("llm down")

    res = rx.run_relation_extraction(_on(), memory=mem, llm_call=boom, conn_factory=fts.cursor)
    assert res.deterministic_count == 3
    assert res.llm_count == 0


def test_knows_dedup_is_direction_insensitive(ac_root):
    """#436: (A,B,knows) 与 (B,A,knows) 是同一条边——反向不重插、只强化。"""
    mem = _mem()
    _ingest(
        mem,
        [
            PersonEvent(name="Bob", summary="周会", occurred_at=_ts(20), confidence=0.9),
            PersonEvent(name="Alice", summary="周会", occurred_at=_ts(20), confidence=0.9),
        ],
    )
    rx.run_relation_extraction(_on(), memory=mem, llm_call=_empty_llm, conn_factory=fts.cursor)
    # 手工塞一条反向 knows 应被识别为已存在（经 _open_edges 的规范键）
    res2 = rx.run_relation_extraction(
        _on(), memory=mem, llm_call=_empty_llm, conn_factory=fts.cursor
    )
    knows_pp = [r for r in _all_edges() if r[2] == "knows" and r[0] != "self" and r[1] != "self"]
    assert len(knows_pp) == 1  # 只有一条 person↔person knows，方向无关
    assert res2.deterministic_count == 0


def test_self_never_in_cooccurrence_pair(ac_root):
    """#435: co-occurrence 对里任一端是 self 都跳过（self↔person 已由第一段循环覆盖）。"""
    mem = _mem()
    # 铸一个 canonical 恰为 "self" 的实体（对抗性）+ 一个正常人，同一时间桶
    _ingest(
        mem,
        [
            PersonEvent(name="self", summary="对抗写法", occurred_at=_ts(20), confidence=0.9),
            PersonEvent(name="Alice", summary="周会", occurred_at=_ts(20), confidence=0.9),
        ],
    )
    rx.run_relation_extraction(_on(), memory=mem, llm_call=_empty_llm, conn_factory=fts.cursor)
    for r in _all_edges():
        # 不存在 self 作为 person↔person 共现对的产物（self→Alice 的 SELF 循环产物除外）
        assert not (r[2] == "knows" and r[0] == "self" and r[1] == "self")


# ── evidence-time valid_from (§1.2 bitemporal: 有效时间可回填，不是事务时间) ──


def _valid_froms():
    with fts.cursor() as conn:
        edges.ensure_schema(conn)
        conn.row_factory = None
        return {
            (r[0], r[1], r[2]): r[3]
            for r in conn.execute(
                "SELECT src_identity, dst_identity, predicate, valid_from FROM relation_edges"
            )
        }


def test_knows_edges_carry_first_evidence_time(ac_root):
    mem = _mem()
    # Alice first seen day 20, Bob day 21, first co-occurrence day 22
    _ingest(
        mem,
        [
            PersonEvent(name="Alice", summary="单独出现", occurred_at=_ts(20), confidence=0.9),
            PersonEvent(name="Bob", summary="单独出现", occurred_at=_ts(21), confidence=0.9),
            PersonEvent(name="Alice", summary="周会", occurred_at=_ts(22), confidence=0.9),
            PersonEvent(name="Bob", summary="周会", occurred_at=_ts(22), confidence=0.9),
        ],
    )
    rx.run_relation_extraction(_on(), memory=mem, llm_call=_empty_llm, conn_factory=fts.cursor)
    vf = _valid_froms()
    # each edge's valid_from = the FIRST evidence moment (minute bucket), never
    # the extraction transaction time — the graph's time axis depends on it
    assert vf[("self", "Alice", "knows")] == str(_ts(20).isoformat())[:16]
    assert vf[("self", "Bob", "knows")] == str(_ts(21).isoformat())[:16]
    assert vf[("Alice", "Bob", "knows")] == str(_ts(22).isoformat())[:16]


def test_activity_edges_carry_source_event_time(ac_root):
    mem = _mem()
    _ingest(
        mem,
        [PersonEvent(name="Alice", summary="做完的事", occurred_at=_ts(20), confidence=0.9)],
    )
    # resolved is DONE only with resolution_outcome='done' (#461) → this one enters.
    _seed_intent(status="resolved", resolution_outcome="done", people=("Alice",))
    rx.run_relation_extraction(_on(), memory=mem, llm_call=_empty_llm, conn_factory=fts.cursor)
    acts = {k: v for k, v in _valid_froms().items() if k[2] == "participates_in"}
    assert acts, "terminal intent should mint Activity edges"
    assert all(v == _ts(22).isoformat() for v in acts.values())


# ── §1.3 about leg: terminal events reconnect adjudicated org/project points ──


def _seed_typed_entity(name, prefix="org-"):
    from datetime import UTC, datetime

    from persome.evomem.models import MemoryLayer, MemoryNode
    from persome.evomem.store import NodeStore

    NodeStore().save(
        MemoryNode(
            node_id=f"n-{prefix}{name}",
            content=name,
            layer=MemoryLayer.L4_IDENTITY,
            file_name=f"{prefix}{name}.md",
            memory_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
    )


def test_event_about_org_reconnects_typed_point(ac_root):
    mem = _mem()
    _ingest(
        mem,
        [PersonEvent(name="Alice", summary="做完的事", occurred_at=_ts(20), confidence=0.9)],
    )
    _seed_typed_entity("研发群")
    _seed_intent(status="consumed", people=("Alice", "研发群"))
    rx.run_relation_extraction(_on(), memory=mem, llm_call=_empty_llm, conn_factory=fts.cursor)
    rows = {(r[0], r[1], r[2]): r for r in _all_edges()}
    about = [k for k in rows if k[2] == "about"]
    assert len(about) == 1
    src, dst, _ = about[0]
    assert src.startswith("event:") and dst == "研发群"
    with fts.cursor() as conn:
        row = conn.execute(
            "SELECT src_kind, dst_kind, provenance FROM relation_edges WHERE predicate='about'"
        ).fetchone()
    assert tuple(row) == ("event", "org", "inferred")


def test_tools_stay_honest_orphans_no_about(ac_root):
    """artifact is NOT in about's dst set (§4.2) — a tool mention mints no edge
    until the matrix grows a `uses` predicate (product decision)."""
    mem = _mem()
    _ingest(
        mem,
        [PersonEvent(name="Alice", summary="做完的事", occurred_at=_ts(20), confidence=0.9)],
    )
    _seed_typed_entity("微信", prefix="tool-")
    _seed_intent(status="consumed", people=("Alice", "微信"))
    rx.run_relation_extraction(_on(), memory=mem, llm_call=_empty_llm, conn_factory=fts.cursor)
    assert not [r for r in _all_edges() if r[2] == "about"]


def test_project_file_evidences_self_participates_in(ac_root):
    """§1.3 SELF→PROJECT works_on: the project memory file IS participation
    evidence by construction; observations = active fact count."""
    mem = _mem()
    _ingest(
        mem,
        [PersonEvent(name="Alice", summary="出现", occurred_at=_ts(20), confidence=0.9)],
    )
    from datetime import UTC, datetime

    from persome.evomem.models import MemoryLayer, MemoryNode
    from persome.evomem.store import NodeStore

    store = NodeStore()
    for i, day in enumerate((5, 3, 9)):
        store.save(
            MemoryNode(
                node_id=f"n-acme-{i}",
                content=f"Acme 事实 {i}",
                layer=MemoryLayer.L2_FACT,
                file_name="project-Acme.md",
                memory_at=datetime(2026, 6, day, tzinfo=UTC),
                occurred_at=datetime(2026, 6, day, tzinfo=UTC),
            )
        )
    rx.run_relation_extraction(_on(), memory=mem, llm_call=_empty_llm, conn_factory=fts.cursor)
    with fts.cursor() as conn:
        row = conn.execute(
            "SELECT src_identity, dst_identity, src_kind, dst_kind, observations, valid_from"
            " FROM relation_edges WHERE predicate='participates_in' AND dst_identity='Acme'"
        ).fetchone()
    assert row is not None
    src, dst, sk, dk, obs, vf = row
    assert (src, sk, dk, obs) == ("self", "self", "project", 3)
    assert str(vf).startswith("2026-06-03")  # earliest evidenced moment


def test_org_has_no_deterministic_self_leg(ac_root):
    """SELF→ORG's only cell is part_of — interaction ≠ membership, so no
    deterministic edge is minted (waits for delta quote evidence)."""
    mem = _mem()
    _ingest(
        mem,
        [PersonEvent(name="Alice", summary="出现", occurred_at=_ts(20), confidence=0.9)],
    )
    _seed_typed_entity("腾讯混元大语言模型部")
    rx.run_relation_extraction(_on(), memory=mem, llm_call=_empty_llm, conn_factory=fts.cursor)
    assert not [
        r for r in _all_edges() if r[1] == "腾讯混元大语言模型部" or r[0] == "腾讯混元大语言模型部"
    ]

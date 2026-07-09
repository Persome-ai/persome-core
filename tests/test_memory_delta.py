"""memory_delta consolidator — Phase 0 shadow channel (spec §4.1/§6.2).

Covers the wiring-acceptance requirements (§6.3 SHADOW + consumer): the channel
fires off a session window, its deterministic gates (quote evidence / roster
multiple-choice / closed predicate set / confidence floor) drop what they must,
malformed LLM output fails open, and the flag-off default is a strict no-op.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from persome import config as config_mod
from persome.store import fts
from persome.store import memory_deltas as deltas_store
from persome.timeline import store as timeline_store
from persome.timeline.store import TimelineBlock
from persome.writer import memory_delta as delta_mod


def _block(start: datetime, entries: list[str], apps: list[str]) -> TimelineBlock:
    return TimelineBlock(
        start_time=start,
        end_time=start + timedelta(minutes=1),
        entries=entries,
        apps_used=apps,
        capture_count=len(entries),
    )


def _seed_session_blocks(entries: list[str]) -> tuple[datetime, datetime]:
    start = datetime(2026, 7, 2, 9, 0).astimezone()
    with fts.cursor() as conn:
        timeline_store.ensure_schema(conn)
        for i, entry in enumerate(entries):
            timeline_store.insert(conn, _block(start + timedelta(minutes=i), [entry], ["Feishu"]))
    return start, start + timedelta(minutes=len(entries) + 1)


def _cfg(enabled: bool = True) -> config_mod.Config:
    cfg = config_mod.Config()
    cfg.memory_delta.enabled = enabled
    return cfg


def _ref(name: str) -> dict:
    return {"new_entity": name}


def _payload(**overrides) -> str:
    base = {
        "entities": [
            {
                "new_entity": "张三",
                "kind": "person",
                "quote": "和张三确认了评审结论",
                "confidence": 0.9,
            }
        ],
        "assertions": [
            {
                "subject": _ref("张三"),
                "text": "张三确认了评审结论",
                "quote": "和张三确认了评审结论",
                "confidence": 0.8,
            }
        ],
        "relations": [],
        "events": [],
    }
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


SESSION_ENTRY = '[Feishu] 聊天: 和张三确认了评审结论。"周五版本可以发"'


def test_flag_off_is_a_strict_noop(ac_root, fake_llm) -> None:
    start, end = _seed_session_blocks([SESSION_ENTRY])
    result = delta_mod.run_after_session(
        _cfg(enabled=False), session_id="s1", start_time=start, end_time=end
    )
    assert result.skipped_reason == "disabled" and not result.written
    assert fake_llm.calls == []  # no LLM call, no row
    with fts.cursor() as conn:
        assert deltas_store.recent(conn) == []


def test_delta_persisted_shadow_with_counts(ac_root, fake_llm) -> None:
    start, end = _seed_session_blocks([SESSION_ENTRY])
    fake_llm.set_default(delta_mod.STAGE, _payload())
    result = delta_mod.run_after_session(_cfg(), session_id="s2", start_time=start, end_time=end)
    assert result.written and result.counts["entities"] == 1 and result.counts["assertions"] == 1
    with fts.cursor() as conn:
        row = deltas_store.latest_for_session(conn, "s2")
    assert row is not None and row["status"] == "shadow"
    delta = json.loads(row["payload"])
    assert delta["entities"][0]["new_entity"] == "张三"
    # the LLM actually received roster + session_events sections (now cache-control blocks)
    blocks = fake_llm.calls[0]["messages"][1]["content"]
    sent = "".join(b["text"] for b in blocks)
    assert "<roster>" in sent and "<session_events>" in sent and "张三" in sent
    # prompt-cache 断点：system + roster 块带 cache_control
    sys_blocks = fake_llm.calls[0]["messages"][0]["content"]
    assert sys_blocks[0].get("cache_control") == {"type": "ephemeral"}
    assert blocks[0].get("cache_control") == {"type": "ephemeral"}  # roster 块


def test_quote_evidence_gate_drops_unquoted_items(ac_root, fake_llm) -> None:
    """No verbatim quote from the session text → the item never lands (§4.1)."""
    start, end = _seed_session_blocks([SESSION_ENTRY])
    fake_llm.set_default(
        delta_mod.STAGE,
        _payload(
            entities=[
                {
                    "new_entity": "张三",
                    "kind": "person",
                    "quote": "这句话不在会话里",
                    "confidence": 0.9,
                }
            ],
            assertions=[],
        ),
    )
    result = delta_mod.run_after_session(_cfg(), session_id="s3", start_time=start, end_time=end)
    assert result.written and result.counts["entities"] == 0 and result.dropped == 1


def test_identity_gate_rejects_bare_store_probing_strings(ac_root, fake_llm) -> None:
    """A new_entity whose name never appears in the session text is rejected —
    the LLM can't invent identities that probe the store (§4.1 选择题)."""
    start, end = _seed_session_blocks([SESSION_ENTRY])
    fake_llm.set_default(
        delta_mod.STAGE,
        _payload(
            entities=[
                {
                    "new_entity": "凭空捏造的人",
                    "kind": "person",
                    "quote": "和张三确认了评审结论",
                    "confidence": 0.9,
                }
            ],
            assertions=[],
        ),
    )
    result = delta_mod.run_after_session(_cfg(), session_id="s4", start_time=start, end_time=end)
    assert result.counts["entities"] == 0 and result.dropped == 1


def test_relation_gate_enforces_closed_predicate_set(ac_root, fake_llm) -> None:
    start, end = _seed_session_blocks([SESSION_ENTRY])
    fake_llm.set_default(
        delta_mod.STAGE,
        _payload(
            entities=[],
            assertions=[],
            relations=[
                {
                    "src": _ref("张三"),
                    "dst": _ref("张三"),
                    "predicate": "loves",  # not in the 6-predicate closed set
                    "label": "",
                    "quote": "和张三确认了评审结论",
                    "confidence": 0.9,
                },
                {
                    "src": _ref("张三"),
                    "dst": _ref("张三"),
                    "predicate": "knows",
                    "label": "同事",
                    "quote": "和张三确认了评审结论",
                    "confidence": 0.9,
                },
            ],
        ),
    )
    result = delta_mod.run_after_session(_cfg(), session_id="s5", start_time=start, end_time=end)
    assert result.counts["relations"] == 1 and result.dropped == 1


def test_confidence_floor_drops_hedges(ac_root, fake_llm) -> None:
    start, end = _seed_session_blocks([SESSION_ENTRY])
    fake_llm.set_default(
        delta_mod.STAGE,
        _payload(
            entities=[
                {
                    "new_entity": "张三",
                    "kind": "person",
                    "quote": "和张三确认了评审结论",
                    "confidence": 0.2,
                }
            ],
            assertions=[],
        ),
    )
    result = delta_mod.run_after_session(_cfg(), session_id="s6", start_time=start, end_time=end)
    assert result.counts["entities"] == 0 and result.dropped == 1


def test_malformed_llm_output_fails_open(ac_root, fake_llm) -> None:
    start, end = _seed_session_blocks([SESSION_ENTRY])
    fake_llm.set_default(delta_mod.STAGE, "not json at all {{{")
    result = delta_mod.run_after_session(_cfg(), session_id="s7", start_time=start, end_time=end)
    assert not result.written and result.skipped_reason == "unparseable"
    with fts.cursor() as conn:
        assert deltas_store.latest_for_session(conn, "s7") is None


def test_no_blocks_skips_without_llm(ac_root, fake_llm) -> None:
    now = datetime.now().astimezone()
    result = delta_mod.run_after_session(
        _cfg(), session_id="s8", start_time=now - timedelta(minutes=5), end_time=now
    )
    assert result.skipped_reason == "no_blocks" and fake_llm.calls == []


def test_stats_aggregates_latest_per_session(ac_root, fake_llm) -> None:
    start, end = _seed_session_blocks([SESSION_ENTRY])
    fake_llm.set_default(delta_mod.STAGE, _payload())
    delta_mod.run_after_session(_cfg(), session_id="s9", start_time=start, end_time=end)
    delta_mod.run_after_session(_cfg(), session_id="s9", start_time=start, end_time=end)
    with fts.cursor() as conn:
        agg = deltas_store.stats(conn)
    assert agg["rows"] == 2 and agg["sessions"] == 1  # latest-per-session, not double-counted
    assert agg["heads"]["entities"] == 1


def test_gate_canonicalizes_honorific_ref_through_the_funnel(ac_root) -> None:
    """§4.3 the ONE funnel at work inside the gate: a "张总" ref resolves to the
    roster canonical and is REWRITTEN in the stored payload — downstream reads
    one identity space."""
    from persome.evomem import identity as identity_mod

    roster = identity_mod.Roster.build([("张伟", ["伟哥"])])
    session_text = "[Feishu] 聊天: 张总确认了对账方案"
    raw = {
        "entities": [
            {"ref": "张总", "kind": "person", "quote": "张总确认了对账方案", "confidence": 0.9}
        ],
        "assertions": [],
        "relations": [],
        "events": [],
    }
    clean, dropped = delta_mod.gate_delta(
        raw, roster=roster, session_text=session_text, min_confidence=0.5
    )
    assert dropped == 0
    assert clean["entities"][0]["ref"] == "张伟"  # canonicalized, not the raw mention
    assert "new_entity" not in clean["entities"][0]


def test_gate_adds_deterministic_cooccurrence_knows(ac_root) -> None:
    """② 确定性共现 knows：同一 session 每对 person 互相 knows（subsume legacy relation_extractor
    的确定性腿）。LLM 没给关系，gate 也补齐 → delta relations ⊇ legacy、退役无召回损失。"""
    from persome.evomem import identity as identity_mod

    roster = identity_mod.Roster.build([("张伟", []), ("李四", []), ("王五", [])])
    session_text = "[Feishu] 群聊: 张伟、李四、王五 三人一起过了方案"
    q = "张伟、李四、王五 三人一起过了方案"
    raw = {
        "entities": [
            {"ref": "张伟", "kind": "person", "quote": q, "confidence": 0.9},
            {"ref": "李四", "kind": "person", "quote": q, "confidence": 0.9},
            {"ref": "王五", "kind": "person", "quote": q, "confidence": 0.9},
        ],
        "relations": [],  # LLM 一条关系没给
        "events": [],
        "assertions": [],
    }
    clean, _ = delta_mod.gate_delta(
        raw, roster=roster, session_text=session_text, min_confidence=0.5
    )
    knows = {
        frozenset((r["src"]["ref"], r["dst"]["ref"]))
        for r in clean["relations"]
        if r["predicate"] == "knows"
    }
    assert knows == {  # 3 人 → C(3,2)=3 条
        frozenset(("张伟", "李四")),
        frozenset(("张伟", "王五")),
        frozenset(("李四", "王五")),
    }


def test_gate_cooccurrence_off_is_noop(ac_root) -> None:
    """cooccurrence=False kill-switch → 不补共现 knows。"""
    from persome.evomem import identity as identity_mod

    roster = identity_mod.Roster.build([("张伟", []), ("李四", [])])
    raw = {
        "entities": [
            {"ref": "张伟", "kind": "person", "quote": "张伟 和 李四", "confidence": 0.9},
            {"ref": "李四", "kind": "person", "quote": "张伟 和 李四", "confidence": 0.9},
        ],
        "relations": [],
        "events": [],
        "assertions": [],
    }
    clean, _ = delta_mod.gate_delta(
        raw, roster=roster, session_text="[Feishu] 张伟 和 李四", min_confidence=0.5, cooccurrence=False
    )
    assert clean["relations"] == []


def test_gate_folds_known_name_posing_as_new_entity(ac_root) -> None:
    """A new_entity whose name resolves to a known identity folds to its ref —
    the LLM cannot re-mint an existing person as a fresh node."""
    from persome.evomem import identity as identity_mod

    roster = identity_mod.Roster.build([("张伟", ["伟哥"])])
    session_text = "[Feishu] 聊天: 伟哥确认了对账方案"
    raw = {
        "entities": [
            {
                "new_entity": "伟哥",
                "kind": "person",
                "quote": "伟哥确认了对账方案",
                "confidence": 0.9,
            }
        ],
        "assertions": [],
        "relations": [],
        "events": [],
    }
    clean, dropped = delta_mod.gate_delta(
        raw, roster=roster, session_text=session_text, min_confidence=0.5
    )
    assert dropped == 0
    assert clean["entities"][0].get("ref") == "张伟" and "new_entity" not in clean["entities"][0]


def test_gate_rejects_unknown_ref_but_keeps_genuine_new_entity(ac_root) -> None:
    """An unknown ref is a store-probing string (rejected); an unknown name
    declared as new_entity and quoted verbatim stays a candidate (候选宁滥毋缺)."""
    from persome.evomem import identity as identity_mod

    roster = identity_mod.Roster.build([("张伟", [])])
    session_text = "[Feishu] 聊天: 王五提交了新的接口文档"
    raw = {
        "entities": [
            {"ref": "王五", "kind": "person", "quote": "王五提交了新的接口文档", "confidence": 0.9},
            {
                "new_entity": "王五",
                "kind": "person",
                "quote": "王五提交了新的接口文档",
                "confidence": 0.9,
            },
        ],
        "assertions": [],
        "relations": [],
        "events": [],
    }
    clean, dropped = delta_mod.gate_delta(
        raw, roster=roster, session_text=session_text, min_confidence=0.5
    )
    assert dropped == 1  # the bare ref probing the store
    assert clean["entities"] == [
        {
            "kind": "person",
            "quote": "王五提交了新的接口文档",
            "confidence": 0.9,
            "new_entity": "王五",
            "ended": False,
        }
    ]


def test_relation_polarity_and_ended_normalize(ac_root, fake_llm) -> None:
    """§4.1 极性/结束轴: polarity coerces to the closed ±0 set (default 0),
    ended coerces to a strict bool — decoration never drops the relation."""
    start, end = _seed_session_blocks([SESSION_ENTRY])
    fake_llm.set_default(
        delta_mod.STAGE,
        _payload(
            entities=[],
            assertions=[],
            relations=[
                {
                    "src": _ref("张三"),
                    "dst": _ref("张三"),
                    "predicate": "knows",
                    "polarity": "positive",  # off-set → coerced to "0"
                    "ended": "yes",  # non-bool → False
                    "quote": "和张三确认了评审结论",
                    "confidence": 0.9,
                },
                {
                    "src": _ref("张三"),
                    "dst": _ref("张三"),
                    "predicate": "knows",
                    "polarity": "-",
                    "ended": True,
                    "quote": "和张三确认了评审结论",
                    "confidence": 0.9,
                },
            ],
        ),
    )
    result = delta_mod.run_after_session(_cfg(), session_id="s9", start_time=start, end_time=end)
    assert result.counts["relations"] == 2
    import json as _json

    from persome.store import fts as fts_store
    from persome.store import memory_deltas as deltas_store

    with fts_store.cursor() as conn:
        row = deltas_store.latest_for_session(conn, "s9")
    rels = _json.loads(row["payload"])["relations"]
    assert (rels[0]["polarity"], rels[0]["ended"]) == ("0", False)
    assert (rels[1]["polarity"], rels[1]["ended"]) == ("-", True)


def test_entity_ended_defaults_false(ac_root, fake_llm) -> None:
    start, end = _seed_session_blocks([SESSION_ENTRY])
    fake_llm.set_default(
        delta_mod.STAGE,
        _payload(
            entities=[
                {
                    "new_entity": "张三",
                    "kind": "person",
                    "quote": "和张三确认了评审结论",
                    "confidence": 0.9,
                }
            ],
            assertions=[],
            relations=[],
        ),
    )
    result = delta_mod.run_after_session(_cfg(), session_id="sa", start_time=start, end_time=end)
    assert result.counts["entities"] == 1
    import json as _json

    from persome.store import fts as fts_store
    from persome.store import memory_deltas as deltas_store

    with fts_store.cursor() as conn:
        row = deltas_store.latest_for_session(conn, "sa")
    assert _json.loads(row["payload"])["entities"][0]["ended"] is False

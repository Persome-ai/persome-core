"""Cross-domain sweeper (Hy-Memory batch 2): collide topic-far/behavior-near schemas.

Pins: deterministic behavior distance (no embedding), topic pre-filter, end-to-end
fuse → write schema-xdomain-*.md → read back through the existing schema消费链,
idempotent re-sweep, and the no-collision / too-few-schemas no-ops.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

from persome.config import Config
from persome.intent import schema_prior
from persome.store import entries as entries_mod
from persome.store import files as files_mod
from persome.timeline import store as timeline_store
from persome.writer import cross_domain_sweeper as sweeper
from persome.writer import schema_miner_stage as stage

# ── fakes / seeds ─────────────────────────────────────────────────────────────


def _fake_llm(payload: dict):
    def call(_messages):
        content = json.dumps(payload, ensure_ascii=False)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    return call


def _seed_schema(conn, name: str, central: str, inferences: list[str]) -> None:
    entries_mod.create_file(conn, name=name, description=central[:120], tags=["schema", "stable"])
    body = stage.render_schema_body(
        central_proposition=central, supporting_summary="s", expected_inferences=inferences
    )
    entries_mod.append_entry(
        conn, name=name, content=body, tags=["schema", "stable", "confidence:0.80"]
    )


# ── deterministic behavior distance (no embedding) ────────────────────────────


def test_signature_distance_near_far_and_ungrounded():
    near_a = sweeper.BehaviorSignature(
        apps=frozenset({"Cursor", "Chrome"}),
        action_dist={"text_input": 0.8, "click": 0.2},
        hours={9: 1.0},
        sample_count=5,
    )
    near_b = sweeper.BehaviorSignature(
        apps=frozenset({"Cursor", "Chrome"}),
        action_dist={"text_input": 0.7, "click": 0.3},
        hours={9: 1.0},
        sample_count=4,
    )
    far_b = sweeper.BehaviorSignature(
        apps=frozenset({"Slack", "Mail"}),
        action_dist={"click": 1.0},
        hours={20: 1.0},
        sample_count=3,
    )
    assert sweeper._signature_distance(near_a, near_b) < 0.2  # behavior-near
    assert sweeper._signature_distance(near_a, far_b) > 0.6  # behavior-far
    # Ungrounded (no occurred_at facts) → 0.0 so the pre-filter passes it to the LLM.
    assert sweeper._signature_distance(near_a, sweeper.BehaviorSignature()) == 0.0


def test_topic_distinct_requires_different_source():
    a = sweeper._StableSchema(
        name="schema-project-a.md",
        source_path="project-a.md",
        central="在 A 项目反复手动重试",
        inferences=[],
        confidence=0.8,
    )
    b = sweeper._StableSchema(
        name="schema-topic-b.md",
        source_path="topic-b.md",
        central="排查网络抖动时频繁刷新",
        inferences=[],
        confidence=0.8,
    )
    same_src = sweeper._StableSchema(
        name="schema-project-a.md",
        source_path="project-a.md",
        central="完全一样的命题",
        inferences=[],
        confidence=0.8,
    )
    assert sweeper._topic_distinct(a, b) is True
    assert sweeper._topic_distinct(a, same_src) is False


# ── behavior signature grounded via occurred_at → timeline_blocks ─────────────


def test_behavior_signature_traces_occurred_at(ac_root):
    from persome.store import fts

    with fts.cursor() as conn:
        t = datetime(2026, 6, 1, 9, 0).astimezone()
        timeline_store.insert(
            conn,
            timeline_store.TimelineBlock(
                start_time=t,
                end_time=t + timedelta(minutes=1),
                apps_used=["Cursor"],
                action_trace=[{"type": "text_input"}, {"type": "click"}],
                capture_count=3,
                id="blk-1",
                created_at=t,
            ),
        )
        entries_mod.create_file(conn, name="project-a.md", description="d", tags=["t"])
        entries_mod.append_entry(
            conn, name="project-a.md", content="did x", tags=["t"], occurred_at=t.isoformat()
        )
        sig = sweeper._schema_behavior_signature(conn, "project-a.md")
        assert sig.grounded
        assert "Cursor" in sig.apps
        assert "text_input" in sig.action_dist


# ── end-to-end fuse + consume ─────────────────────────────────────────────────


def test_sweep_fuses_and_is_consumed(ac_root):
    from persome.store import fts

    with fts.cursor() as conn:
        _seed_schema(
            conn, "schema-project-a.md", "在 A 项目遇阻就反复手动重试", ["遇阻不先找自动化"]
        )
        _seed_schema(conn, "schema-tool-b.md", "用 B 工具卡住时反复手动点", ["不读文档先硬试"])
        fake = _fake_llm(
            {
                "detected": True,
                "central_proposition": "遇阻时倾向硬刚而非寻找自动化方案",
                "supporting_summary": "两个 topic 是同一驱动力的两次显现",
                "expected_inferences": ["会拒绝引入自动化工具", "偏好手动逐步控制"],
                "confidence": 0.82,
            }
        )
        res = sweeper.sweep_cross_domain(Config(), conn, llm_call=fake)
        assert res.written_count == 1
        assert res.collisions == 1
        name = res.written[0].path
        assert name.startswith("schema-xdomain-")
        # fused schema flows through the existing schema消费链 (zero consumer change)
        infs = schema_prior.active_schema_inferences(conn)
        assert any("自动化" in x for x in infs)


def test_resweep_is_idempotent(ac_root):
    from persome.store import fts

    with fts.cursor() as conn:
        _seed_schema(
            conn, "schema-project-a.md", "在 A 项目遇阻就反复手动重试", ["遇阻不先找自动化"]
        )
        _seed_schema(conn, "schema-tool-b.md", "用 B 工具卡住时反复手动点", ["不读文档先硬试"])
        fake = _fake_llm(
            {
                "detected": True,
                "central_proposition": "遇阻时倾向硬刚",
                "supporting_summary": "同构",
                "expected_inferences": ["会拒绝自动化"],
                "confidence": 0.82,
            }
        )
        r1 = sweeper.sweep_cross_domain(Config(), conn, llm_call=fake)
        r2 = sweeper.sweep_cross_domain(Config(), conn, llm_call=fake)
        assert r1.written_count == 1
        assert r2.written_count == 1
        assert r2.written[0].updated_in_place is True
        # the fused file holds exactly one live entry (re-sweep superseded in place)
        name = r1.written[0].path
        parsed = files_mod.read_file(files_mod.memory_path(name))
        live = [e for e in parsed.entries if not e.superseded_by]
        assert len(live) == 1


def test_no_collision_writes_nothing(ac_root):
    from persome.store import fts

    with fts.cursor() as conn:
        _seed_schema(conn, "schema-project-a.md", "话题一的命题", ["推论一"])
        _seed_schema(conn, "schema-tool-b.md", "话题二完全不同", ["推论二"])
        fake = _fake_llm({"detected": False})
        res = sweeper.sweep_cross_domain(Config(), conn, llm_call=fake)
        assert res.written_count == 0
        assert res.collisions == 0
        assert res.pairs_probed == 1  # ungrounded → pre-filter passes; LLM said no


def test_fewer_than_two_schemas_is_noop(ac_root):
    from persome.store import fts

    with fts.cursor() as conn:
        _seed_schema(conn, "schema-project-a.md", "唯一一个", ["推论"])

        def _boom(_messages):  # must never be called
            raise AssertionError("LLM should not run with <2 schemas")

        res = sweeper.sweep_cross_domain(Config(), conn, llm_call=_boom)
        assert res.written_count == 0
        assert res.pairs_considered == 0


def test_xdomain_schemas_are_not_re_fused(ac_root):
    """A fused schema-xdomain-* is excluded from the base set (no recursive collision)."""
    from persome.store import fts

    with fts.cursor() as conn:
        # one normal schema + one already-fused xdomain schema
        _seed_schema(conn, "schema-project-a.md", "普通 schema", ["推论"])
        _seed_schema(conn, "schema-xdomain-x__y.md", "已融合的高层 schema", ["高层推论"])
        bases = sweeper._load_stable_schemas(conn)
        names = {s.name for s in bases}
        assert "schema-project-a.md" in names
        assert "schema-xdomain-x__y.md" not in names

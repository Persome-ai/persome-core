"Tests for test cross domain sweeper."

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

from persome.config import Config
from persome.model import schema_reader
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
        central="\u5728 A \u9879\u76ee\u53cd\u590d\u624b\u52a8\u91cd\u8bd5",
        inferences=[],
        confidence=0.8,
    )
    b = sweeper._StableSchema(
        name="schema-topic-b.md",
        source_path="topic-b.md",
        central="\u6392\u67e5\u7f51\u7edc\u6296\u52a8\u65f6\u9891\u7e41\u5237\u65b0",
        inferences=[],
        confidence=0.8,
    )
    same_src = sweeper._StableSchema(
        name="schema-project-a.md",
        source_path="project-a.md",
        central="\u5b8c\u5168\u4e00\u6837\u7684\u547d\u9898",
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
            conn,
            "schema-project-a.md",
            "\u5728 A \u9879\u76ee\u9047\u963b\u5c31\u53cd\u590d\u624b\u52a8\u91cd\u8bd5",
            ["\u9047\u963b\u4e0d\u5148\u627e\u81ea\u52a8\u5316"],
        )
        _seed_schema(
            conn,
            "schema-tool-b.md",
            "\u7528 B \u5de5\u5177\u5361\u4f4f\u65f6\u53cd\u590d\u624b\u52a8\u70b9",
            ["\u4e0d\u8bfb\u6587\u6863\u5148\u786c\u8bd5"],
        )
        fake = _fake_llm(
            {
                "detected": True,
                "central_proposition": "\u9047\u963b\u65f6\u503e\u5411\u786c\u521a\u800c\u975e\u5bfb\u627e\u81ea\u52a8\u5316\u65b9\u6848",
                "supporting_summary": "\u4e24\u4e2a topic \u662f\u540c\u4e00\u9a71\u52a8\u529b\u7684\u4e24\u6b21\u663e\u73b0",
                "expected_inferences": [
                    "\u4f1a\u62d2\u7edd\u5f15\u5165\u81ea\u52a8\u5316\u5de5\u5177",
                    "\u504f\u597d\u624b\u52a8\u9010\u6b65\u63a7\u5236",
                ],
                "confidence": 0.82,
            }
        )
        res = sweeper.sweep_cross_domain(Config(), conn, llm_call=fake)
        assert res.written_count == 1
        assert res.collisions == 1
        name = res.written[0].path
        assert name.startswith("schema-xdomain-")

        infs = schema_reader.active_schema_inferences(conn)
        assert any("\u81ea\u52a8\u5316" in x for x in infs)


def test_resweep_is_idempotent(ac_root):
    from persome.store import fts

    with fts.cursor() as conn:
        _seed_schema(
            conn,
            "schema-project-a.md",
            "\u5728 A \u9879\u76ee\u9047\u963b\u5c31\u53cd\u590d\u624b\u52a8\u91cd\u8bd5",
            ["\u9047\u963b\u4e0d\u5148\u627e\u81ea\u52a8\u5316"],
        )
        _seed_schema(
            conn,
            "schema-tool-b.md",
            "\u7528 B \u5de5\u5177\u5361\u4f4f\u65f6\u53cd\u590d\u624b\u52a8\u70b9",
            ["\u4e0d\u8bfb\u6587\u6863\u5148\u786c\u8bd5"],
        )
        fake = _fake_llm(
            {
                "detected": True,
                "central_proposition": "\u9047\u963b\u65f6\u503e\u5411\u786c\u521a",
                "supporting_summary": "\u540c\u6784",
                "expected_inferences": ["\u4f1a\u62d2\u7edd\u81ea\u52a8\u5316"],
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
        _seed_schema(
            conn,
            "schema-project-a.md",
            "\u8bdd\u9898\u4e00\u7684\u547d\u9898",
            ["\u63a8\u8bba\u4e00"],
        )
        _seed_schema(
            conn,
            "schema-tool-b.md",
            "\u8bdd\u9898\u4e8c\u5b8c\u5168\u4e0d\u540c",
            ["\u63a8\u8bba\u4e8c"],
        )
        fake = _fake_llm({"detected": False})
        res = sweeper.sweep_cross_domain(Config(), conn, llm_call=fake)
        assert res.written_count == 0
        assert res.collisions == 0
        assert res.pairs_probed == 1  # ungrounded → pre-filter passes; LLM said no


def test_fewer_than_two_schemas_is_noop(ac_root):
    from persome.store import fts

    with fts.cursor() as conn:
        _seed_schema(conn, "schema-project-a.md", "\u552f\u4e00\u4e00\u4e2a", ["\u63a8\u8bba"])

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
        _seed_schema(conn, "schema-project-a.md", "\u666e\u901a schema", ["\u63a8\u8bba"])
        _seed_schema(
            conn,
            "schema-xdomain-x__y.md",
            "\u5df2\u878d\u5408\u7684\u9ad8\u5c42 schema",
            ["\u9ad8\u5c42\u63a8\u8bba"],
        )
        bases = sweeper._load_stable_schemas(conn)
        names = {s.name for s in bases}
        assert "schema-project-a.md" in names
        assert "schema-xdomain-x__y.md" not in names

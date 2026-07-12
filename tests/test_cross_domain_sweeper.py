"Tests for test cross domain sweeper."

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

from persome.config import Config
from persome.model import schema_reader
from persome.store import entries as entries_mod
from persome.store import files as files_mod
from persome.store import schema_faces
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


def test_probe_budget_is_hard_and_pair_order_is_deterministic(ac_root):
    from persome.store import fts

    with fts.cursor() as conn:
        # Seed out of lexical order: scheduling must not inherit insertion or
        # timestamp order from the entries projection.
        for name, central in (
            ("schema-gamma.md", "Repeatedly sample while tuning databases"),
            ("schema-alpha.md", "Collect maps before planning travel"),
            ("schema-delta.md", "Verify experience before hiring interviews"),
            ("schema-beta.md", "Track heart rate throughout training"),
        ):
            _seed_schema(conn, name, central, [f"Inference derived from: {central}"])

        probed: list[str] = []

        def fake(messages):
            probed.append(messages[1]["content"])
            return _fake_llm({"detected": False})(messages)

        result = sweeper.sweep_cross_domain(Config(), conn, max_probes=2, llm_call=fake)

        assert result.pairs_considered == 6
        assert result.eligible_pairs == 6
        assert result.probe_limit == 2
        assert result.pairs_probed == 2
        assert result.pairs_deferred == 4
        assert len(probed) == 2
        assert "## Schema A (topic: alpha.md)" in probed[0]
        assert "## Schema B (topic: beta.md)" in probed[0]
        assert "## Schema A (topic: alpha.md)" in probed[1]
        assert "## Schema B (topic: delta.md)" in probed[1]


def test_deferred_pairs_rotate_into_later_builds(ac_root):
    from persome.store import fts

    with fts.cursor() as conn:
        for name, central in (
            ("schema-alpha.md", "Collect maps before travel"),
            ("schema-beta.md", "Track heart rate during training"),
            ("schema-delta.md", "Verify experience before interviews"),
            ("schema-gamma.md", "Sample repeatedly while tuning"),
        ):
            _seed_schema(conn, name, central, [f"Inference from {central}"])

        first: list[str] = []
        second: list[str] = []

        def record(target):  # type: ignore[no-untyped-def]
            def fake(messages):  # type: ignore[no-untyped-def]
                target.append(messages[1]["content"])
                return _fake_llm({"detected": False})(messages)

            return fake

        one = sweeper.sweep_cross_domain(Config(), conn, max_probes=2, llm_call=record(first))
        two = sweeper.sweep_cross_domain(Config(), conn, max_probes=2, llm_call=record(second))

        assert one.pairs_deferred == 4
        assert two.pairs_deferred == 4
        assert len(first) == len(second) == 2
        assert set(first).isdisjoint(second)


def test_shadow_volume_is_reprobed_first_and_promoted_without_weakening_gate(ac_root):
    from persome.store import fts

    with fts.cursor() as conn:
        _seed_schema(conn, "schema-alpha.md", "Check maps before travel", ["Confirm routes early"])
        _seed_schema(
            conn,
            "schema-beta.md",
            "Track heart rate during training",
            ["Adjust intensity from measurements"],
        )
        _seed_schema(
            conn,
            "schema-gamma.md",
            "Repeat samples while tuning",
            ["Collect measurements before deciding"],
        )

        shadow_id = schema_faces.record_face(
            conn,
            source=schema_faces.PROVENANCE_EMERGENT,
            signature="Measure continuously before adjusting",
            members=["schema-beta.md", "schema-gamma.md"],
            confidence=0.82,
            level=2,
        )
        before = conn.execute(
            "SELECT status, observations FROM schema_faces WHERE face_id = ?", (shadow_id,)
        ).fetchone()
        assert tuple(before) == ("shadow", 1)

        probed: list[str] = []

        def fake(messages):
            probed.append(messages[1]["content"])
            return _fake_llm(
                {
                    "detected": True,
                    "central_proposition": "Measure continuously before adjusting",
                    "supporting_summary": "Both domains use measurement loops to guide changes",
                    "expected_inferences": ["Samples before acting under uncertainty"],
                    "confidence": 0.84,
                }
            )(messages)

        result = sweeper.sweep_cross_domain(Config(), conn, max_probes=1, llm_call=fake)

        assert result.eligible_pairs == 3
        assert result.pairs_probed == 1
        assert result.pairs_deferred == 2
        assert len(probed) == 1
        assert "topic: beta.md" in probed[0] and "topic: gamma.md" in probed[0]
        after = conn.execute(
            "SELECT status, observations, footprints FROM schema_faces WHERE face_id = ?",
            (shadow_id,),
        ).fetchone()
        assert after[0] == "active"
        assert after[1] == 2
        assert len(json.loads(after[2])) == 2


def test_negative_shadow_retry_yields_budget_to_every_unseen_pair(ac_root):
    """A stale shadow gets one priority retry, then joins the rotating queue."""
    from persome.store import fts

    with fts.cursor() as conn:
        for name, central in (
            ("schema-alpha.md", "Check maps before travel"),
            ("schema-beta.md", "Track heart rate during training"),
            ("schema-delta.md", "Verify experience before interviews"),
            ("schema-gamma.md", "Repeat samples while tuning databases"),
        ):
            _seed_schema(conn, name, central, [f"Inference from {central}"])

        shadow_id = schema_faces.record_face(
            conn,
            source=schema_faces.PROVENANCE_EMERGENT,
            signature="Prepare with measurements before committing",
            members=["schema-alpha.md", "schema-beta.md"],
            confidence=0.82,
            level=2,
        )
        probed: list[str] = []

        def reject(messages):  # type: ignore[no-untyped-def]
            probed.append(messages[1]["content"])
            return _fake_llm({"detected": False})(messages)

        # Four schemas produce six eligible pairs. The shadow consumes the
        # first one-probe build, then its negative result must let each of the
        # five unseen pairs run in the next five builds.
        for _ in range(6):
            result = sweeper.sweep_cross_domain(Config(), conn, max_probes=1, llm_call=reject)
            assert result.eligible_pairs == 6
            assert result.pairs_probed == 1

        assert "topic: alpha.md" in probed[0] and "topic: beta.md" in probed[0]
        assert len(set(probed)) == 6
        history = conn.execute(
            "SELECT probe_count, detected FROM cross_domain_probe_state WHERE pair_key = ?",
            (sweeper._pair_key("schema-alpha.md", "schema-beta.md"),),
        ).fetchone()
        assert tuple(history) == (1, 0)
        shadow = conn.execute(
            "SELECT status, observations FROM schema_faces WHERE face_id = ?",
            (shadow_id,),
        ).fetchone()
        assert tuple(shadow) == ("shadow", 1)


def test_probe_history_write_failure_does_not_abort_sweep(ac_root, monkeypatch):
    from persome.store import fts

    with fts.cursor() as conn:
        _seed_schema(conn, "schema-alpha.md", "Check maps before travel", ["Confirm routes"])
        _seed_schema(
            conn,
            "schema-beta.md",
            "Track heart rate during training",
            ["Adjust from measurements"],
        )
        monkeypatch.setattr(
            sweeper,
            "_record_probe",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("history unavailable")),
        )

        result = sweeper.sweep_cross_domain(
            Config(),
            conn,
            max_probes=1,
            llm_call=_fake_llm({"detected": False}),
        )

    assert result.pairs_probed == 1
    assert result.written == []


def test_failed_volume_evidence_is_not_recorded_as_promotable(ac_root, monkeypatch):
    from persome.store import fts

    with fts.cursor() as conn:
        _seed_schema(conn, "schema-alpha.md", "Check maps before travel", ["Confirm routes"])
        _seed_schema(
            conn,
            "schema-beta.md",
            "Track heart rate during training",
            ["Adjust from measurements"],
        )
        monkeypatch.setattr(
            schema_faces,
            "record_face",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("face write failed")),
        )
        collision = _fake_llm(
            {
                "detected": True,
                "central_proposition": "Measure before adjusting",
                "supporting_summary": "Both domains gather evidence first",
                "expected_inferences": ["Samples before acting"],
                "confidence": 0.9,
            }
        )

        result = sweeper.sweep_cross_domain(Config(), conn, max_probes=1, llm_call=collision)
        history = conn.execute("SELECT detected FROM cross_domain_probe_state").fetchone()

    assert result.written == []
    assert history is not None and history[0] == 0


def test_repeated_low_confidence_collision_never_promotes_volume(ac_root):
    from persome.store import fts

    with fts.cursor() as conn:
        _seed_schema(conn, "schema-alpha.md", "Check maps before travel", ["Confirm routes"])
        _seed_schema(
            conn,
            "schema-beta.md",
            "Track heart rate during training",
            ["Adjust from measurements"],
        )
        fake = _fake_llm(
            {
                "detected": True,
                "central_proposition": "Measure before adjusting",
                "supporting_summary": "Both domains gather evidence first",
                "expected_inferences": ["Samples before acting"],
                "confidence": 0.3,
            }
        )

        first = sweeper.sweep_cross_domain(Config(), conn, llm_call=fake)
        second = sweeper.sweep_cross_domain(Config(), conn, llm_call=fake)

        assert [first.written[0].status, second.written[0].status] == ["forming", "forming"]
        assert (
            conn.execute(
                "SELECT count(*) FROM schema_faces WHERE level=2 AND status='active'"
            ).fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT count(*) FROM schema_faces WHERE level=2").fetchone()[0] == 0
        assert conn.execute("SELECT detected FROM cross_domain_probe_state").fetchone()[0] == 0


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


def test_person_schemas_are_not_cross_domain_inputs(ac_root):
    """Collaborator schemas cannot be fused into the owner's project model."""
    from persome.store import fts

    with fts.cursor() as conn:
        _seed_schema(conn, "schema-person-kevin.md", "Kevin iterates with a fixed prompt", ["x"])
        _seed_schema(conn, "schema-project-persome.md", "The owner iterates on Persome", ["y"])

        bases = sweeper._load_stable_schemas(conn)

    assert [schema.name for schema in bases] == ["schema-project-persome.md"]

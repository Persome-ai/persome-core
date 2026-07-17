"""Public Point/Line/Face/Volume/Root model contract tests."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import persome.model.build as model_build_mod
from persome import config as config_mod
from persome import paths
from persome.evomem.models import MemoryLayer, MemoryNode, MemoryStatus
from persome.evomem.store import NodeStore
from persome.model import (
    ModelBuildBusy,
    ModelBuildCoordinator,
    ModelContractError,
    ModelRecoveryIncomplete,
    PipelineOutcome,
    artifact_matches_manifest,
    build_snapshot,
    create_build_manifest,
    export_snapshot,
    is_valid_build_stage_artifact,
    load_last_manifest,
    load_live_manifest,
    model_status,
    run_model_build,
    sync_live_human_markdown,
)
from persome.model.stage_receipt import (
    content_digest,
    create_build_stage_artifact,
)
from persome.store import fts, schema_faces
from persome.store import relation_edges as edges

FIXTURE = Path(__file__).parent / "fixtures" / "runtime_model" / "model_seed.json"
GOLDEN = Path(__file__).parent / "fixtures" / "runtime_model" / "model_snapshot_v1.golden.json"


def _rehash_manifest(manifest: dict) -> None:
    unsigned = {key: value for key, value in manifest.items() if key != "build_id"}
    payload = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    manifest["build_id"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _load_stage_artifact() -> dict:
    return json.loads(paths.model_build_stage_receipt().read_text(encoding="utf-8"))


def _seed_model(monkeypatch: pytest.MonkeyPatch) -> dict:
    seed = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fixed_now = seed["generated_at"]
    monkeypatch.setattr(schema_faces, "_now", lambda: fixed_now)

    store = NodeStore()
    for item in seed["points"]:
        node = MemoryNode(
            node_id=item["id"],
            content=item["content"],
            layer=MemoryLayer.L2_FACT,
            file_name=item["file_name"],
            tags=item["tags"],
            valid_from=item["valid_from"],
            gmt_created=datetime.fromisoformat(item["valid_from"]),
        )
        old_id = item.get("supersedes")
        if old_id:
            store.save_and_supersede(node, old_id=old_id, old_valid_until=item["valid_from"])
        else:
            store.save(node)

    relation = seed["relation"]
    with fts.cursor() as conn:
        edges.add_edge(
            conn,
            edge_id=relation["id"],
            src_identity=relation["source"],
            dst_identity=relation["target"],
            predicate=relation["predicate"],
            src_kind=relation["source_kind"],
            dst_kind=relation["target_kind"],
            provenance="inferred",
            confidence=0.9,
            label=relation["label"],
            quote=relation["quote"],
            valid_from=relation["valid_from"],
            created_at=relation["valid_from"],
            status=MemoryStatus.ACTIVE,
            source_kind=relation["source_event_kind"],
            source_id=relation["source_event_id"],
            source_receipt=relation["source_receipt"],
        )

        model_ids: dict[str, str] = {}
        for face in seed["faces"]:
            face_id = schema_faces.record_face(
                conn,
                source=schema_faces.PROVENANCE_MINED,
                signature=face["signature"],
                members=face["members"],
                confidence=0.8,
                anchors=face["anchors"],
            )
            schema_faces.record_face(
                conn,
                source=schema_faces.PROVENANCE_EMERGENT,
                signature=face["signature"],
                members=face["members"],
                confidence=0.9,
                anchors=face["anchors"],
            )
            assert schema_faces.maybe_promote(conn, face_id)
            model_ids[face["key"]] = face_id

        volume = seed["volume"]
        volume_members = [model_ids[key] for key in volume["members"]]
        volume_id = schema_faces.record_face(
            conn,
            source=schema_faces.PROVENANCE_MINED,
            signature=volume["signature"],
            members=volume_members,
            confidence=0.85,
            level=2,
            anchors=volume["anchors"],
        )
        schema_faces.record_face(
            conn,
            source=schema_faces.PROVENANCE_EMERGENT,
            signature=volume["signature"],
            members=volume_members,
            confidence=0.9,
            level=2,
            anchors=volume["anchors"],
        )
        assert schema_faces.maybe_promote(conn, volume_id)
        model_ids[volume["key"]] = volume_id

        root = seed["root"]
        root_id = schema_faces.upsert_root(
            conn,
            signature=root["signature"],
            members=[model_ids[key] for key in root["members"]],
            anchors=root["anchors"],
        )
        conn.commit()
    return {"seed": seed, "root_id": root_id}


def test_fresh_root_snapshot_has_complete_geometry_and_receipts(ac_root, monkeypatch) -> None:
    seeded = _seed_model(monkeypatch)
    with fts.cursor() as conn:
        snapshot = build_snapshot(
            conn,
            generated_at=seeded["seed"]["generated_at"],
            build_metadata={"build_id": "synthetic-build-1", "trigger": "test-fixture"},
        )

    assert snapshot["schema_version"] == 1
    assert snapshot["stats"] == {
        "points": 4,
        "active_points": 3,
        "evolution_lines": 1,
        "relation_lines": 1,
        "faces": 2,
        "volumes": 1,
        "roots": 1,
        "receipts": 5,
        "redactions": {},
    }
    assert snapshot["root"]["id"] == seeded["root_id"]
    assert snapshot["root"]["provenance"] == "synth"
    assert snapshot["root"]["source_receipts"]
    assert snapshot["volumes"][0]["source_receipts"]
    assert {line["kind"] for line in snapshot["lines"]} == {"evolution", "relation"}
    assert all(face["member_receipts"] for face in snapshot["faces"])
    relation = next(line for line in snapshot["lines"] if line["kind"] == "relation")
    assert relation["source_evidence"] == {
        "kind": "session",
        "id": "event:session:synthetic-1",
        "receipt": "⟨event:session:synthetic-1:fixtures/session-1.json⟩",
    }


def test_snapshot_is_reproducible_with_fixed_build_metadata(ac_root, monkeypatch) -> None:
    seeded = _seed_model(monkeypatch)
    kwargs = {
        "generated_at": seeded["seed"]["generated_at"],
        "build_metadata": {"build_id": "synthetic-build-1", "trigger": "test-fixture"},
    }
    with fts.cursor() as conn:
        first = build_snapshot(conn, **kwargs)
        second = build_snapshot(conn, **kwargs)
    assert first == second


def test_snapshot_schema_matches_v1_golden(ac_root, monkeypatch) -> None:
    seeded = _seed_model(monkeypatch)
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    with fts.cursor() as conn:
        snapshot = build_snapshot(conn, generated_at=seeded["seed"]["generated_at"])

    assert snapshot["schema_version"] == golden["schema_version"]
    assert sorted(snapshot) == sorted(golden["top_level"])
    assert sorted(snapshot["build"]) == sorted(golden["build"])
    assert all(sorted(point) == sorted(golden["point"]) for point in snapshot["points"])
    for line in snapshot["lines"]:
        assert sorted(line) == sorted(golden[f"{line['kind']}_line"])
    for item in [*snapshot["faces"], *snapshot["volumes"], snapshot["root"]]:
        assert sorted(item) == sorted(golden["schema_object"])
    for receipt in snapshot["receipts"]:
        assert set(golden["receipt"]) <= set(receipt)
        if receipt["source_kind"] == "point":
            assert set(golden["point_receipt_extra"]) <= set(receipt)
    assert sorted(snapshot["stats"]) == sorted(golden["stats"])


def test_build_manifest_is_complete_and_mock_reproducible(tmp_path) -> None:
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / "classifier.md").write_text("Classify synthetic facts.\n", encoding="utf-8")
    kwargs = {
        "core_commit": "0123456789abcdef",
        "models": {"classifier": "synthetic-model"},
        "prompt_dir": prompt_dir,
        "config": {"writer": {"batch_size": 8}},
        "input_window": {
            "start": "2026-07-01T00:00:00+00:00",
            "end": "2026-07-10T08:00:00+00:00",
        },
        "started_at": "2026-07-10T08:00:00+00:00",
        "completed_at": "2026-07-10T08:00:02+00:00",
        "duration_ms": 2000,
        "trigger": "test-fixture",
        "mode": "mock",
    }
    first = create_build_manifest(**kwargs)
    second = create_build_manifest(**kwargs)
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert first == second
    assert sorted(first) == sorted(golden["build"])
    assert first["prompt_hashes"].keys() == {"classifier.md"}
    assert first["config_hash"] != ""
    assert first["status"] == "complete"
    assert first["degraded_stages"] == []


def test_point_correction_and_delete_keep_auditable_history(ac_root, monkeypatch) -> None:
    _seed_model(monkeypatch)
    corrected = MemoryNode(
        node_id="point-focus-v3",
        content="The user now reserves mornings for release review.",
        layer=MemoryLayer.L2_FACT,
        file_name="synthetic/work.md",
        valid_from="2026-07-10T08:00:00+00:00",
    )
    store = NodeStore()
    store.save_and_supersede(
        corrected,
        old_id="point-focus-v2",
        old_valid_until="2026-07-10T08:00:00+00:00",
    )
    with fts.cursor() as conn:
        corrected_snapshot = build_snapshot(conn, generated_at="2026-07-10T08:00:00+00:00")

    points = {point["id"]: point for point in corrected_snapshot["points"]}
    assert points["point-focus-v2"]["status"] == "shadow"
    assert points["point-focus-v3"]["status"] == "active"
    assert points["point-focus-v3"]["supersedes"] == ["point-focus-v2"]
    assert any(
        line["source"] == "point-focus-v2" and line["target"] == "point-focus-v3"
        for line in corrected_snapshot["lines"]
    )

    store.shadow("point-focus-v3", valid_until="2026-07-11T08:00:00+00:00")
    with fts.cursor() as conn:
        deleted_snapshot = build_snapshot(conn, generated_at="2026-07-11T08:00:00+00:00")
    deleted_points = {point["id"]: point for point in deleted_snapshot["points"]}
    assert deleted_points["point-focus-v3"]["status"] == "shadow"
    assert deleted_points["point-focus-v2"]["content"] == (
        "The user reserves mornings for focused writing and review."
    )


def test_fresh_root_rebuild_is_structurally_identical(ac_root, monkeypatch, tmp_path) -> None:
    seeded = _seed_model(monkeypatch)
    timestamp = seeded["seed"]["generated_at"]
    manifest = create_build_manifest(
        core_commit="0123456789abcdef",
        prompt_dir=tmp_path / "missing-prompts",
        started_at=timestamp,
        completed_at=timestamp,
        mode="mock",
    )
    with fts.cursor() as conn:
        first = build_snapshot(conn, generated_at=timestamp, build_metadata=manifest)

    rebuilt_root = tmp_path / "rebuilt-persome"
    rebuilt_root.mkdir()
    monkeypatch.setenv("PERSOME_ROOT", str(rebuilt_root))
    _seed_model(monkeypatch)
    with fts.cursor() as conn:
        second = build_snapshot(conn, generated_at=timestamp, build_metadata=manifest)
    assert first == second


def test_upgrade_backfills_human_from_existing_root_without_rebuild(
    ac_root, monkeypatch, tmp_path
) -> None:
    seeded = _seed_model(monkeypatch)
    timestamp = seeded["seed"]["generated_at"]
    manifest = create_build_manifest(
        core_commit="0123456789abcdef",
        prompt_dir=tmp_path / "missing-prompts",
        started_at=timestamp,
        completed_at=timestamp,
        trigger="pre-human-upgrade",
        mode="mock",
    )
    paths.atomic_write_private_text(
        paths.model_build_manifest(),
        json.dumps(manifest, ensure_ascii=False, sort_keys=True),
    )

    assert not paths.human_file().exists()
    human_path = sync_live_human_markdown()
    rendered = human_path.read_text(encoding="utf-8")

    assert f'build_id: "{manifest["build_id"]}"' in rendered
    assert f'root_id: "{seeded["root_id"]}"' in rendered
    assert "Persome has not formed a verified Root yet" not in rendered
    assert human_path.stat().st_mode & 0o777 == 0o600


def test_model_build_persists_complete_owner_only_manifest(ac_root, monkeypatch) -> None:
    seeded = _seed_model(monkeypatch)
    cfg = config_mod.load(ac_root / "config.toml")
    moments = iter(
        [
            datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
            datetime(2026, 7, 10, 8, 0, tzinfo=UTC) + timedelta(seconds=2),
        ]
    )
    pipeline = PipelineOutcome(stages={"synthetic": {"status": "complete"}})
    result = run_model_build(
        cfg,
        pipeline_runner=lambda _cfg: pipeline,
        now=lambda: next(moments),
        trigger="test-fixture",
    )

    assert result.status == "complete"
    assert result.manifest["degraded_stages"] == []
    assert result.stats["roots"] == 1
    assert set(result.manifest) == set(json.loads(GOLDEN.read_text())["build"])
    assert result.stages == pipeline.stages
    assert load_last_manifest() == result.manifest
    artifact = _load_stage_artifact()
    assert artifact["pipeline_kind"] == "override"
    assert [stage["name"] for stage in artifact["stages"]] == [
        "pipeline_override",
        "model_contract",
    ]
    assert artifact_matches_manifest(artifact, result.manifest)
    assert paths.model_build_stage_receipt().stat().st_mode & 0o777 == 0o600
    assert result.manifest_path.stat().st_mode & 0o777 == 0o600
    human_path = paths.human_file()
    assert result.human_path == human_path
    human = human_path.read_text(encoding="utf-8")
    assert human_path.stat().st_mode & 0o777 == 0o600
    assert f'build_id: "{result.manifest["build_id"]}"' in human
    assert f'root_id: "{seeded["root_id"]}"' in human
    assert "# HUMAN.md" in human
    assert "Persome has not formed a verified Root yet" not in human


def test_pipeline_callback_cannot_self_report_stage_receipts(ac_root, monkeypatch) -> None:
    _seed_model(monkeypatch)
    cfg = config_mod.load(ac_root / "config.toml")
    sensitive = "PRIVATE PROMPT: choose the acquisition and email Alice"

    def malicious_report(_cfg):  # type: ignore[no-untyped-def]
        return PipelineOutcome(
            stages={
                "root_synthesis": {
                    "status": "complete",
                    "prompt": sensitive,
                    "response": sensitive,
                }
            },
            degraded_stages=["caller_claimed_failure"],
        )

    result = run_model_build(
        cfg,
        pipeline_runner=malicious_report,
        now=lambda: datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
        trigger="untrusted-callback",
    )

    manifest_text = paths.model_build_manifest().read_text(encoding="utf-8")
    artifact_text = paths.model_build_stage_receipt().read_text(encoding="utf-8")
    assert sensitive not in manifest_text
    assert sensitive not in artifact_text
    assert "caller_claimed_failure" in manifest_text
    assert "caller_claimed_failure" not in artifact_text
    assert result.manifest["degraded_stages"] == ["caller_claimed_failure"]
    assert result.stages == malicious_report(cfg).stages
    artifact = _load_stage_artifact()
    assert [receipt["name"] for receipt in artifact["stages"]] == [
        "pipeline_override",
        "model_contract",
    ]
    assert artifact["pipeline_kind"] == "override"
    assert artifact["status"] == "complete"
    assert artifact_matches_manifest(artifact, result.manifest)


def test_stage_artifact_hashes_unsafe_trigger_text(ac_root, monkeypatch) -> None:
    _seed_model(monkeypatch)
    cfg = config_mod.load(ac_root / "config.toml")
    sensitive_trigger = "PRIVATE: email Alice about the acquisition"
    sensitive_commit = "PRIVATE commit note about Alice"
    monkeypatch.setenv("PERSOME_CORE_COMMIT", sensitive_commit)

    result = run_model_build(
        cfg,
        pipeline_runner=lambda _cfg: PipelineOutcome(),
        now=lambda: datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
        trigger=sensitive_trigger,
    )

    artifact_text = paths.model_build_stage_receipt().read_text(encoding="utf-8")
    artifact = json.loads(artifact_text)
    assert sensitive_trigger not in artifact_text
    assert sensitive_commit not in artifact_text
    assert artifact["trigger_label"] == "other"
    assert artifact["trigger_digest"].startswith("sha256:")
    assert artifact["core_commit"] == "other"
    assert artifact["core_commit_digest"].startswith("sha256:")
    assert artifact_matches_manifest(artifact, result.manifest)


def test_running_stage_is_atomically_visible_before_callback_returns(
    ac_root,
    monkeypatch,
) -> None:
    _seed_model(monkeypatch)
    cfg = config_mod.load(ac_root / "config.toml")
    observed: dict = {}

    def inspect_running_marker(_cfg):  # type: ignore[no-untyped-def]
        observed.update(_load_stage_artifact())
        return PipelineOutcome()

    run_model_build(
        cfg,
        pipeline_runner=inspect_running_marker,
        now=lambda: datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
        trigger="inspect-running",
    )

    assert observed["status"] == "building"
    assert observed["pipeline_kind"] == "override"
    assert observed["stages"][0]["status"] == "running"
    assert observed["stages"][0]["completed_at"] is None
    assert set(load_last_manifest()) == set(json.loads(GOLDEN.read_text())["build"])


def test_stage_artifact_updates_use_atomic_owner_only_writer(
    ac_root,
    monkeypatch,
) -> None:
    _seed_model(monkeypatch)
    cfg = config_mod.load(ac_root / "config.toml")
    real_atomic_write = paths.atomic_write_private_text
    stage_writes: list[dict] = []

    def track_atomic_write(path, content):  # type: ignore[no-untyped-def]
        if path == paths.model_build_stage_receipt():
            stage_writes.append(json.loads(content))
        return real_atomic_write(path, content)

    monkeypatch.setattr(paths, "atomic_write_private_text", track_atomic_write)
    run_model_build(
        cfg,
        pipeline_runner=lambda _cfg: PipelineOutcome(),
        now=lambda: datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
        trigger="atomic-receipt",
    )

    assert len(stage_writes) >= 5
    assert stage_writes[0]["stages"] == []
    assert any(
        write["stages"] and write["stages"][-1]["status"] == "running" for write in stage_writes
    )
    assert stage_writes[-1]["status"] == "complete"
    assert paths.model_build_stage_receipt().stat().st_mode & 0o777 == 0o600


def test_stage_recorder_preserves_legacy_failure_and_skip_result_shape(ac_root) -> None:
    sensitive = "PRIVATE model response about Alice"
    artifact = create_build_stage_artifact(
        trigger="shape-test",
        pipeline_kind="core",
        started_at=datetime.now(UTC).isoformat(),
    )
    recorder = model_build_mod._StageReceiptRecorder(artifact)
    outcome = PipelineOutcome()

    def fail_state_formation() -> dict:
        raise RuntimeError(sensitive)

    model_build_mod._run_stage(
        outcome,
        recorder,
        "state_formation",
        fail_state_formation,
    )
    model_build_mod._run_stage(
        outcome,
        recorder,
        "evomem_baseline",
        lambda: {"backfilled": 0},
        enabled=False,
    )

    assert set(outcome.stages["state_formation"]) == {
        "status",
        "reason",
        "duration_ms",
    }
    assert outcome.stages["state_formation"]["status"] == "failed"
    assert sensitive in outcome.stages["state_formation"]["reason"]
    assert outcome.stages["evomem_baseline"] == {
        "status": "skipped",
        "reason": "disabled",
        "duration_ms": 0,
    }
    persisted = paths.model_build_stage_receipt().read_text(encoding="utf-8")
    assert sensitive not in persisted
    receipts = json.loads(persisted)["stages"]
    assert receipts[0]["error_code"] == "stage_failed"
    assert receipts[1]["error_code"] == "disabled_by_config"


def test_empty_model_build_is_degraded_not_success(ac_root) -> None:
    cfg = config_mod.load(ac_root / "config.toml")
    moment = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    result = run_model_build(
        cfg,
        pipeline_runner=lambda _cfg: PipelineOutcome(),
        now=lambda: moment,
        trigger="test-empty",
    )
    assert result.status == "degraded"
    assert result.manifest["degraded_stages"] == ["model_contract"]
    artifact = _load_stage_artifact()
    contract_receipt = artifact["stages"][-1]
    assert contract_receipt["name"] == "model_contract"
    assert contract_receipt["status"] == "failed"
    assert contract_receipt["degraded"] is True
    assert contract_receipt["error_code"] == "incomplete_geometry"
    assert contract_receipt["outputs"]["roots"] == 0
    assert artifact_matches_manifest(artifact, result.manifest)
    assert result.stats["points"] == 0
    assert result.stats["roots"] == 0
    human_path = paths.human_file()
    human = human_path.read_text(encoding="utf-8")
    assert human_path.stat().st_mode & 0o777 == 0o600
    assert 'build_status: "degraded"' in human
    assert "root_id: null" in human
    assert "Persome has not formed a verified Root yet" in human
    assert "## Stable patterns" not in human
    assert "## Cross-domain patterns" not in human


def test_model_build_propagates_frozen_clock_to_default_state_formation(
    ac_root,
    monkeypatch,
) -> None:
    cfg = config_mod.load(ac_root / "config.toml")
    frozen = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    seen: list[tuple[datetime | None, datetime | None]] = []

    def fake_pipeline(  # type: ignore[no-untyped-def]
        _cfg,
        *,
        recorder,
        stage_clock=None,
        evidence_as_of=None,
    ):
        seen.append((stage_clock, evidence_as_of))
        outcome = PipelineOutcome()
        for name in model_build_mod.CORE_MODEL_BUILD_STAGES[:-1]:
            recorder.skip(name)
            outcome.stages[name] = {
                "status": "skipped",
                "reason": "test",
                "duration_ms": 0,
            }
        return outcome

    monkeypatch.setattr(model_build_mod, "_run_pipeline", fake_pipeline)
    run_model_build(
        cfg,
        now=lambda: frozen,
        trigger="test-frozen-state-formation",
    )

    assert seen == [(frozen, frozen)]
    artifact = _load_stage_artifact()
    assert artifact["started_at"] != frozen.isoformat()
    assert all(stage["started_at"] != frozen.isoformat() for stage in artifact["stages"])


def test_model_build_separates_historical_evidence_from_processing_clock(
    ac_root,
    monkeypatch,
) -> None:
    cfg = config_mod.load(ac_root / "config.toml")
    evidence_as_of = datetime(2026, 7, 13, 13, 46, tzinfo=UTC)
    processing_started = datetime(2026, 7, 17, 9, 0, tzinfo=UTC)
    processing_completed = processing_started + timedelta(seconds=2)
    moments = iter([processing_started, processing_completed])
    seen: list[tuple[datetime | None, datetime | None]] = []

    def fake_pipeline(  # type: ignore[no-untyped-def]
        _cfg,
        *,
        recorder,
        stage_clock=None,
        evidence_as_of=None,
    ):
        seen.append((stage_clock, evidence_as_of))
        outcome = PipelineOutcome()
        for name in model_build_mod.CORE_MODEL_BUILD_STAGES[:-1]:
            recorder.skip(name)
            outcome.stages[name] = {
                "status": "skipped",
                "reason": "test",
                "duration_ms": 0,
            }
        return outcome

    monkeypatch.setattr(model_build_mod, "_run_pipeline", fake_pipeline)
    result = run_model_build(
        cfg,
        evidence_as_of=evidence_as_of,
        processing_clock=lambda: next(moments),
        trigger="test-historical-evidence-clock",
    )

    assert seen == [(processing_started, evidence_as_of)]
    assert result.manifest["started_at"] == processing_started.isoformat()
    assert result.manifest["completed_at"] == processing_completed.isoformat()


def test_default_pipeline_forwards_evidence_clock_to_enrichment(
    ac_root,
    monkeypatch,
) -> None:
    import persome.session.tick as tick_mod
    import persome.vectors_tick as vectors_tick_mod
    import persome.writer.agent as writer_agent_mod

    cfg = config_mod.load(ac_root / "config.toml")
    cfg.reducer.enabled = True
    cfg.schema.enabled = False
    cfg.person_graph_enabled = False
    cfg.case_extraction_enabled = True
    cfg.attention_digest_enabled = False
    cfg.relation_extraction_enabled = False
    evidence_as_of = datetime(2026, 7, 13, 13, 46, tzinfo=UTC)
    processing_clock = datetime(2026, 7, 17, 9, 0, tzinfo=UTC)
    seen: list[tuple[bool, datetime | None]] = []
    writer_clocks: list[datetime | None] = []

    def fake_enrichment(  # type: ignore[no-untyped-def]
        _cfg,
        *,
        raise_on_error=False,
        evidence_as_of=None,
    ):
        seen.append((raise_on_error, evidence_as_of))
        return {
            "person_updates": 0,
            "case_cards": 0,
            "relation_edges": 0,
            "attention_digest": 0,
            "relation_promoted": 0,
        }

    def fake_writer(_cfg, *, stage_clock=None):  # type: ignore[no-untyped-def]
        writer_clocks.append(stage_clock)
        return SimpleNamespace(reduced=0, classified=0, written_ids=[])

    monkeypatch.setattr(tick_mod, "_run_evomem_enrichment_once", fake_enrichment)
    monkeypatch.setattr(writer_agent_mod, "run", fake_writer)
    monkeypatch.setattr(vectors_tick_mod, "backfill", lambda _cfg: 0)

    artifact = create_build_stage_artifact(
        trigger="test-default-pipeline-evidence-clock",
        pipeline_kind="core",
        started_at=datetime.now(UTC).isoformat(),
    )
    recorder = model_build_mod._StageReceiptRecorder(artifact)
    outcome = model_build_mod._run_pipeline(
        cfg,
        recorder=recorder,
        stage_clock=processing_clock,
        evidence_as_of=evidence_as_of,
    )

    assert outcome.stages["entity_relation_enrichment"]["status"] == "complete"
    assert writer_clocks == [processing_clock]
    assert seen == [(True, evidence_as_of)]


def test_model_build_rejects_ambiguous_processing_clock_aliases(ac_root) -> None:
    cfg = config_mod.load(ac_root / "config.toml")
    clock = lambda: datetime(2026, 7, 17, 9, 0, tzinfo=UTC)  # noqa: E731

    with pytest.raises(ValueError, match="processing_clock or now"):
        run_model_build(
            cfg,
            processing_clock=clock,
            now=clock,
        )


def test_model_build_preserves_unknown_human_and_reports_no_projection(ac_root) -> None:
    unknown = "# My HUMAN.md\n\nPersome must not replace this file.\n"
    paths.human_file().write_text(unknown, encoding="utf-8")
    cfg = config_mod.load(ac_root / "config.toml")
    result = run_model_build(
        cfg,
        pipeline_runner=lambda _cfg: PipelineOutcome(),
        now=lambda: datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
        trigger="test-human-conflict",
    )

    assert result.status == "degraded"
    assert result.human_path is None
    assert load_last_manifest() == result.manifest
    assert paths.human_file().read_text(encoding="utf-8") == unknown


def test_interrupted_model_build_invalidates_previous_completed_manifest(ac_root) -> None:
    cfg = config_mod.load(ac_root / "config.toml")
    first = run_model_build(
        cfg,
        pipeline_runner=lambda _cfg: PipelineOutcome(),
        now=lambda: datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
        trigger="first-build",
    )
    assert load_live_manifest()["build_id"] == first.manifest["build_id"]

    def interrupted(_cfg):  # type: ignore[no-untyped-def]
        raise RuntimeError("synthetic interruption")

    with pytest.raises(RuntimeError, match="synthetic interruption"):
        run_model_build(
            cfg,
            pipeline_runner=interrupted,
            now=lambda: datetime(2026, 7, 10, 9, 0, tzinfo=UTC),
            trigger="interrupted-build",
        )

    assert load_last_manifest()["status"] == "building"
    artifact = _load_stage_artifact()
    receipt = artifact["stages"][0]
    assert receipt["name"] == "pipeline_override"
    assert receipt["status"] == "failed"
    assert receipt["error_code"] == "stage_failed"
    assert artifact["status"] == "failed"
    assert artifact["failure_code"] == "build_failed"
    assert "synthetic interruption" not in json.dumps(artifact)
    assert load_live_manifest()["status"] == "not_built"


def test_base_exception_persists_interrupted_stage_receipt(ac_root) -> None:
    cfg = config_mod.load(ac_root / "config.toml")

    def interrupted(_cfg):  # type: ignore[no-untyped-def]
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_model_build(
            cfg,
            pipeline_runner=interrupted,
            now=lambda: datetime(2026, 7, 10, 9, 0, tzinfo=UTC),
            trigger="base-exception",
        )

    assert load_last_manifest()["status"] == "building"
    artifact = _load_stage_artifact()
    receipt = artifact["stages"][0]
    assert receipt["status"] == "interrupted"
    assert receipt["degraded"] is True
    assert receipt["error_code"] == "stage_interrupted"
    assert artifact["status"] == "interrupted"
    assert artifact["failure_code"] == "build_interrupted"


@pytest.mark.parametrize("pending_kind", ["database", "config"])
def test_model_build_is_blocked_while_integrity_recovery_is_pending(
    ac_root,
    pending_kind,
) -> None:
    cfg = config_mod.load(ac_root / "config.toml")
    pending_path = (
        paths.integrity_recovery_pending()
        if pending_kind == "database"
        else paths.integrity_config_recovery_pending()
    )
    paths.atomic_write_private_text(pending_path, '{"version": 1}')

    with pytest.raises(ModelRecoveryIncomplete, match="recovery is incomplete"):
        run_model_build(cfg, pipeline_runner=lambda _cfg: PipelineOutcome())

    assert not paths.model_build_manifest().exists()


def test_active_building_manifest_is_live_only_while_lock_is_held(ac_root) -> None:
    coordinator = ModelBuildCoordinator()
    marker = {
        "build_id": None,
        "status": "building",
        "trigger": "test-active",
        "started_at": "2026-07-12T08:00:00+00:00",
        "completed_at": None,
        "duration_ms": 0,
        "degraded_stages": [],
    }
    with coordinator.acquire(wait_seconds=0):
        paths.atomic_write_private_text(
            paths.model_build_manifest(),
            json.dumps(marker),
        )
        live = load_live_manifest()
        assert all(live[key] == value for key, value in marker.items())
        assert set(live) == set(json.loads(GOLDEN.read_text(encoding="utf-8"))["build"])

    assert load_last_manifest() == marker
    assert load_live_manifest()["status"] == "not_built"


def test_build_lock_hides_previous_manifest_before_building_marker(ac_root, tmp_path) -> None:
    previous = create_build_manifest(
        core_commit="0123456789abcdef",
        prompt_dir=tmp_path / "missing-prompts",
        started_at="2026-07-12T07:00:00+00:00",
        completed_at="2026-07-12T07:01:00+00:00",
        trigger="previous-build",
        mode="mock",
    )
    paths.atomic_write_private_text(paths.model_build_manifest(), json.dumps(previous))

    coordinator = ModelBuildCoordinator()
    with coordinator.acquire(wait_seconds=0):
        # The exclusive lock is acquired before run_model_build writes its
        # provisional marker. Never expose the previous completed manifest in
        # that window.
        live = load_live_manifest()

    assert live["status"] == "building"
    assert live["build_id"] is None
    assert live["trigger"] == "unknown"
    assert live["started_at"] is None


def test_live_manifest_rejects_tampered_build_id(ac_root, tmp_path) -> None:
    manifest = create_build_manifest(
        core_commit="0123456789abcdef",
        prompt_dir=tmp_path / "missing-prompts",
        degraded_stages=["root_synthesis"],
        started_at="2026-07-12T08:00:00+00:00",
        completed_at="2026-07-12T08:01:00+00:00",
        mode="mock",
    )
    manifest["status"] = "complete"
    paths.atomic_write_private_text(paths.model_build_manifest(), json.dumps(manifest))

    assert load_live_manifest()["status"] == "not_built"


@pytest.mark.parametrize(
    ("status", "degraded_stages"),
    [("complete", ["root_synthesis"]), ("degraded", [])],
)
def test_live_manifest_rejects_rehashed_status_stage_contradiction(
    ac_root,
    tmp_path,
    status,
    degraded_stages,
) -> None:
    manifest = create_build_manifest(
        core_commit="0123456789abcdef",
        prompt_dir=tmp_path / "missing-prompts",
        started_at="2026-07-12T08:00:00+00:00",
        completed_at="2026-07-12T08:01:00+00:00",
        mode="mock",
    )
    manifest["status"] = status
    manifest["degraded_stages"] = degraded_stages
    _rehash_manifest(manifest)
    paths.atomic_write_private_text(paths.model_build_manifest(), json.dumps(manifest))

    assert load_live_manifest()["status"] == "not_built"


def test_stage_artifact_rejects_rehashed_personal_text_without_affecting_snapshot(
    ac_root,
    monkeypatch,
) -> None:
    _seed_model(monkeypatch)
    cfg = config_mod.load(ac_root / "config.toml")
    result = run_model_build(
        cfg,
        pipeline_runner=lambda _cfg: PipelineOutcome(),
        now=lambda: datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
    )
    artifact = _load_stage_artifact()
    receipt = artifact["stages"][0]
    receipt["outputs"] = {"prompt": "Email Alice about the private acquisition"}
    receipt["receipt_id"] = content_digest(
        {key: value for key, value in receipt.items() if key != "receipt_id"}
    )
    artifact["artifact_id"] = content_digest(
        {key: value for key, value in artifact.items() if key != "artifact_id"}
    )

    assert is_valid_build_stage_artifact(artifact) is False
    assert artifact_matches_manifest(artifact, result.manifest) is False
    assert load_live_manifest() == result.manifest


def test_model_build_lock_no_wait_reports_busy(ac_root) -> None:
    owner = ModelBuildCoordinator()
    contender = ModelBuildCoordinator()
    with (
        owner.acquire(wait_seconds=0),
        pytest.raises(ModelBuildBusy, match="model build is busy"),
        contender.acquire(wait_seconds=0),
    ):
        pytest.fail("contender unexpectedly acquired the model build lock")
    assert owner.lock_path.stat().st_mode & 0o777 == 0o600


def test_status_reports_model_readiness(ac_root, monkeypatch) -> None:
    seeded = _seed_model(monkeypatch)
    with fts.cursor() as conn:
        status = model_status(conn)
    assert status["ready"] is True
    assert status["issues"] == []
    assert status["root_id"] == seeded["root_id"]


def test_status_requires_complete_point_line_face_volume_root_geometry(ac_root) -> None:
    NodeStore().save(
        MemoryNode(
            node_id="point-only",
            content="One isolated fact is not a complete model.",
            layer=MemoryLayer.L2_FACT,
            file_name="synthetic/partial.md",
        )
    )
    with fts.cursor() as conn:
        status = model_status(conn)
    assert status["ready"] is False
    assert status["issues"] == ["no_lines", "no_faces", "no_volumes", "no_root"]


def test_export_redacts_pii_and_is_owner_only(ac_root, monkeypatch, tmp_path) -> None:
    _seed_model(monkeypatch)
    private_path = "/" + "Users" + "/sample/private.md"
    NodeStore().save(
        MemoryNode(
            node_id="point-private",
            content="Contact person@example.com and review the private path.",
            layer=MemoryLayer.L2_FACT,
            file_name=private_path,
        )
    )
    with fts.cursor() as conn:
        edges.add_edge(
            conn,
            edge_id="edge-private-identity",
            src_identity="self",
            dst_identity="person@example.com",
            predicate="knows",
            src_kind="self",
            dst_kind="person",
            provenance="inferred",
            confidence=0.8,
            quote="synthetic fixture",
            status=MemoryStatus.ACTIVE,
        )
    target = tmp_path / "model.json"
    with fts.cursor() as conn:
        exported = export_snapshot(
            conn,
            out_path=target,
            generated_at="2026-07-10T08:00:00+00:00",
        )
    raw = exported.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert "person@example.com" not in raw
    assert private_path not in raw
    assert "[REDACTED]" in raw
    assert payload["stats"]["redactions"] == {"email": 2, "home_path": 1}
    assert exported.stat().st_mode & 0o777 == 0o600


def test_snapshot_rejects_multiple_live_roots(ac_root, monkeypatch) -> None:
    _seed_model(monkeypatch)
    with fts.cursor() as conn:
        conn.execute(
            """
            INSERT INTO schema_faces (
                face_id, level, parent_face, signature, members, footprints, provenance,
                observations, confidence, status, valid_from, valid_to, created_at, anchors
            )
            SELECT 'root-duplicate', level, parent_face, signature, members, footprints,
                   provenance, observations, confidence, status, valid_from, valid_to,
                   created_at, anchors
            FROM schema_faces WHERE level = 3 AND valid_to IS NULL LIMIT 1
            """
        )
        with pytest.raises(ModelContractError, match="at most one live Root"):
            build_snapshot(conn)

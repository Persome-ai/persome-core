"""One-shot personal-model build orchestration shared by CLI and scheduled callers."""

from __future__ import annotations

import fcntl
import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import paths
from ..evomem.store import NodeStore
from ..logger import get
from ..store import fts
from .manifest import create_build_manifest, is_valid_build_manifest
from .snapshot import build_snapshot
from .stage_receipt import (
    CORE_MODEL_BUILD_STAGES,
    MODEL_BUILD_STAGE_OUTPUT_KEYS,
    bind_completed_artifact,
    create_build_stage_artifact,
    create_stage_receipt,
    fail_build_stage_artifact,
    refresh_artifact_id,
)

DEFAULT_WAIT_SECONDS = 30.0
logger = get("persome.model.build")
_MODEL_STAGES = (
    "reducer",
    "classifier",
    "pattern_detector",
    "case_extractor",
    "evomem_baseline",
    "relation_extraction",
    "schema_miner",
    "cross_domain_sweeper",
    "root_synthesis",
)
_PERSISTED_MANIFEST_KEYS = frozenset(
    {
        "build_id",
        "core_commit",
        "models",
        "prompt_hashes",
        "config_hash",
        "input_window",
        "mode",
        "trigger",
        "status",
        "degraded_stages",
        "started_at",
        "completed_at",
        "duration_ms",
    }
)


class ModelBuildBusy(RuntimeError):
    """Another process still owns the model-build lock after the requested wait."""


class ModelRecoveryIncomplete(RuntimeError):
    """Crash recovery has not established a safe database/write authority."""


@dataclass
class PipelineOutcome:
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)
    degraded_stages: list[str] = field(default_factory=list)


@dataclass
class ModelBuildResult:
    status: str
    manifest: dict[str, Any]
    stats: dict[str, Any]
    stages: dict[str, dict[str, Any]]
    manifest_path: Path
    human_path: Path | None = None


class ModelBuildCoordinator:
    """Cross-process coordinator backed by a kernel-released ``flock`` lock."""

    def __init__(self, lock_path: Path | None = None) -> None:
        self.lock_path = lock_path or paths.model_build_lock()

    @contextmanager
    def acquire(self, *, wait_seconds: float = DEFAULT_WAIT_SECONDS) -> Iterator[None]:
        wait = max(0.0, float(wait_seconds))
        handle = paths.open_private_lock_file(self.lock_path)
        deadline = time.monotonic() + wait
        try:
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise ModelBuildBusy(
                            f"model build is busy (waited {wait:g}s for {self.lock_path})"
                        ) from exc
                    time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


def _processing_timestamp() -> str:
    """Return a wall-clock timestamp for execution receipts, never replay time."""
    return datetime.now(UTC).isoformat()


def _safe_stage_metrics(name: str, details: Any) -> dict[str, int]:
    """Keep only stage-specific non-negative counters."""
    if not isinstance(details, dict):
        return {}
    if name == "root_synthesis":
        return {"roots_written": 1 if details.get("root_id") else 0}
    allowed = MODEL_BUILD_STAGE_OUTPUT_KEYS[name]
    metrics: dict[str, int] = {}
    for key in sorted(allowed):
        value = details.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            continue
        metrics[key] = value
    return metrics


class _StageReceiptRecorder:
    """Core-owned writer for sanitized outcomes of the current build."""

    def __init__(self, artifact: dict[str, Any]) -> None:
        self.artifact = dict(artifact)
        self.receipts: list[dict[str, Any]] = []
        self._active: dict[str, float] = {}

    def _persist(self) -> None:
        self.artifact = refresh_artifact_id(
            {
                **self.artifact,
                "degraded_stages": [
                    receipt["name"] for receipt in self.receipts if receipt["degraded"]
                ],
                "stages": [dict(item) for item in self.receipts],
            }
        )
        _write_json_owner_only(paths.model_build_stage_receipt(), self.artifact)

    def start(self, name: str) -> None:
        if name not in MODEL_BUILD_STAGE_OUTPUT_KEYS or any(
            receipt["name"] == name for receipt in self.receipts
        ):
            raise RuntimeError(f"invalid or duplicate model-build stage: {name}")
        started_at = _processing_timestamp()
        self._active[name] = time.monotonic()
        self.receipts.append(
            create_stage_receipt(
                name=name,
                status="running",
                started_at=started_at,
                completed_at=None,
                duration_ms=0,
                degraded=False,
                error_code=None,
            )
        )
        self._persist()

    def finish(
        self,
        name: str,
        *,
        status: str,
        error_code: str | None = None,
        outputs: Any = None,
    ) -> dict[str, Any]:
        started_monotonic = self._active.pop(name)
        index = next(
            index for index, receipt in enumerate(self.receipts) if receipt["name"] == name
        )
        previous = self.receipts[index]
        receipt = create_stage_receipt(
            name=name,
            status=status,
            started_at=previous["started_at"],
            completed_at=_processing_timestamp(),
            duration_ms=round((time.monotonic() - started_monotonic) * 1000),
            degraded=status in {"failed", "interrupted"},
            error_code=error_code,
            outputs=_safe_stage_metrics(name, outputs),
        )
        self.receipts[index] = receipt
        self._persist()
        return receipt

    def skip(self, name: str) -> dict[str, Any]:
        self.start(name)
        return self.finish(
            name,
            status="skipped",
            error_code="disabled_by_config",
        )

    def bind_manifest(self, manifest: dict[str, Any]) -> None:
        self.artifact = bind_completed_artifact(
            self.artifact,
            manifest=manifest,
            completed_at=_processing_timestamp(),
        )
        _write_json_owner_only(paths.model_build_stage_receipt(), self.artifact)

    def fail_build(self, *, interrupted: bool) -> None:
        self.artifact = fail_build_stage_artifact(
            self.artifact,
            interrupted=interrupted,
            completed_at=_processing_timestamp(),
        )
        _write_json_owner_only(paths.model_build_stage_receipt(), self.artifact)


def _run_stage(
    outcome: PipelineOutcome,
    recorder: _StageReceiptRecorder,
    name: str,
    operation: Callable[[], dict[str, Any]],
    *,
    enabled: bool = True,
    skip_reason: str = "disabled",
) -> None:
    if not enabled:
        recorder.skip(name)
        outcome.stages[name] = {
            "status": "skipped",
            "reason": skip_reason,
            "duration_ms": 0,
        }
        return
    started = time.monotonic()
    recorder.start(name)
    try:
        details = operation()
    except Exception as exc:  # noqa: BLE001 - in-memory compatibility; receipt is sanitized
        recorder.finish(name, status="failed", error_code="stage_failed")
        outcome.degraded_stages.append(name)
        outcome.stages[name] = {
            "status": "failed",
            "reason": f"{type(exc).__name__}: {exc}",
            "duration_ms": round((time.monotonic() - started) * 1000),
        }
        return
    except BaseException:
        recorder.finish(
            name,
            status="interrupted",
            error_code="stage_interrupted",
        )
        raise
    recorder.finish(name, status="complete", outputs=details)
    outcome.stages[name] = {
        "status": "complete",
        "duration_ms": round((time.monotonic() - started) * 1000),
        **details,
    }


def _run_pipeline(
    cfg: Any,
    *,
    recorder: _StageReceiptRecorder,
    stage_clock: datetime | None = None,
) -> PipelineOutcome:
    """Run the one-shot structural model stages in dependency order."""
    from .. import vectors_tick
    from ..session.tick import _run_evomem_enrichment_once
    from ..writer import agent as writer_agent
    from ..writer import cross_domain_sweeper, root_synthesis, schema_miner_stage

    outcome = PipelineOutcome()

    def run_writer() -> dict[str, Any]:
        result = writer_agent.run(cfg, stage_clock=stage_clock)
        return {
            "reduced": result.reduced,
            "classified": result.classified,
            "written": len(result.written_ids),
        }

    _run_stage(
        outcome,
        recorder,
        "state_formation",
        run_writer,
        enabled=cfg.reducer.enabled,
    )

    def run_evomem_baseline() -> dict[str, Any]:
        from ..evomem import backfill

        with fts.cursor() as conn:
            node_count = conn.execute("SELECT COUNT(*) FROM evo_nodes").fetchone()[0]
            fact_count = conn.execute(
                "SELECT COUNT(*) FROM entries WHERE prefix != 'event'"
            ).fetchone()[0]
        if node_count or not fact_count:
            return {
                "reason": "already_initialized" if node_count else "no_durable_facts",
                "backfilled": 0,
            }
        report = backfill.run_backfill()
        if not report.ok:
            raise RuntimeError(
                "evomem baseline verification failed: "
                f"violations={len(report.violations)} heads_only_evo={len(report.heads_only_evo)} "
                f"heads_only_fts={len(report.heads_only_fts)}"
            )
        return {"reason": "initialized", "backfilled": report.backfilled_nodes}

    _run_stage(outcome, recorder, "evomem_baseline", run_evomem_baseline)

    def run_enrichment() -> dict[str, Any]:
        return _run_evomem_enrichment_once(cfg, raise_on_error=True)

    enrichment_enabled = bool(
        getattr(cfg, "person_graph_enabled", False)
        or getattr(cfg, "case_extraction_enabled", False)
        or getattr(cfg, "attention_digest_enabled", False)
        or getattr(cfg, "relation_extraction_enabled", False)
    )
    _run_stage(
        outcome,
        recorder,
        "entity_relation_enrichment",
        run_enrichment,
        enabled=enrichment_enabled,
    )

    def run_schema() -> dict[str, Any]:
        with fts.cursor() as conn:
            result = schema_miner_stage.mine_schemas_for_user(cfg, conn)
        return {
            "written": result.written_count,
            "skipped_small": result.skipped_small,
            "skipped_empty": result.skipped_empty,
        }

    _run_stage(
        outcome,
        recorder,
        "schema_miner",
        run_schema,
        enabled=cfg.schema.enabled,
    )

    def run_cross_domain() -> dict[str, Any]:
        with fts.cursor() as conn:
            result = cross_domain_sweeper.sweep_cross_domain(
                cfg,
                conn,
                behavior_max_distance=cfg.schema.cross_domain_behavior_max_distance,
                min_confidence=cfg.schema.cross_domain_min_confidence,
                max_probes=cfg.schema.cross_domain_max_probes,
            )
        return {
            "written": result.written_count,
            "pairs_considered": result.pairs_considered,
            "eligible_pairs": result.eligible_pairs,
            "pairs_probed": result.pairs_probed,
            "probe_limit": result.probe_limit,
            "pairs_deferred": result.pairs_deferred,
            "collisions": result.collisions,
        }

    _run_stage(
        outcome,
        recorder,
        "cross_domain_sweeper",
        run_cross_domain,
        enabled=cfg.schema.enabled and cfg.schema.cross_domain_enabled,
    )

    def run_root() -> dict[str, Any]:
        with fts.cursor() as conn:
            result = root_synthesis.run_root_synthesis(cfg, conn)
        if result.reason == "error":
            raise RuntimeError("root synthesis returned error")
        return {"reason": result.reason, "root_id": result.face_id}

    _run_stage(
        outcome,
        recorder,
        "root_synthesis",
        run_root,
        enabled=cfg.schema.enabled and cfg.schema.root_synthesis_enabled,
    )

    def run_vectors() -> dict[str, Any]:
        enqueued = vectors_tick.backfill(cfg)
        embedded = 0
        queued = enqueued
        if cfg.search.hybrid_enabled:
            embedded, queued = vectors_tick.run_embed_once(cfg)
        return {"enqueued": enqueued, "embedded": embedded, "queued": queued}

    _run_stage(outcome, recorder, "vector_backfill", run_vectors)

    return outcome


def _input_window() -> dict[str, str | None]:
    with fts.cursor() as conn:
        row = conn.execute(
            """
            WITH input_values(value) AS (
                SELECT timestamp AS value FROM captures
                UNION ALL SELECT start_time AS value FROM sessions
                UNION ALL SELECT end_time AS value FROM sessions WHERE end_time IS NOT NULL
            )
            SELECT
                (SELECT value FROM input_values
                  WHERE persome_epoch(value) IS NOT NULL
                  ORDER BY persome_epoch(value) ASC LIMIT 1),
                (SELECT value FROM input_values
                  WHERE persome_epoch(value) IS NOT NULL
                  ORDER BY persome_epoch(value) DESC LIMIT 1)
            """
        ).fetchone()
    return {"start": row[0] if row else None, "end": row[1] if row else None}


def _models(cfg: Any) -> dict[str, str]:
    return {stage: cfg.model_for(stage).model for stage in _MODEL_STAGES}


def _write_json_owner_only(path: Path, payload: dict[str, Any]) -> None:
    paths.atomic_write_private_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def load_last_manifest() -> dict[str, Any] | None:
    try:
        payload = json.loads(paths.model_build_manifest().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def _recovery_marker_blocks_manifest() -> bool:
    if paths.integrity_recovery_pending().exists():
        return True
    marker = paths.integrity_recovery_marker()
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        database = payload.get("database_recovery") if isinstance(payload, dict) else None
        if not isinstance(database, dict) or not database.get("model_rebuild_required"):
            return False
        return paths.model_build_manifest().stat().st_mtime_ns <= marker.stat().st_mtime_ns
    except FileNotFoundError:
        return False
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        # A malformed old recovery marker cannot prove that the current
        # manifest is stale. The manifest's own strict validation still applies.
        return False


def _not_built_manifest() -> dict[str, Any]:
    return {
        "build_id": None,
        "core_commit": None,
        "models": {},
        "prompt_hashes": {},
        "config_hash": None,
        "input_window": {"start": None, "end": None},
        "mode": None,
        "status": "not_built",
        "trigger": "no_completed_build",
        "started_at": None,
        "completed_at": None,
        "duration_ms": 0,
        "degraded_stages": [],
    }


def _building_manifest(manifest: dict[str, Any] | None) -> dict[str, Any]:
    marker_is_current = manifest is not None and manifest.get("status") == "building"
    return {
        "build_id": None,
        "core_commit": None,
        "models": {},
        "prompt_hashes": {},
        "config_hash": None,
        "input_window": {"start": None, "end": None},
        "mode": None,
        "status": "building",
        "trigger": manifest.get("trigger", "unknown") if marker_is_current else "unknown",
        "started_at": manifest.get("started_at") if marker_is_current else None,
        "completed_at": None,
        "duration_ms": 0,
        "degraded_stages": [],
    }


def _classify_persisted_manifest(manifest: dict[str, Any] | None) -> dict[str, Any]:
    recovery_blocked = _recovery_marker_blocks_manifest()
    if (
        manifest is not None
        and not recovery_blocked
        and manifest.keys() >= _PERSISTED_MANIFEST_KEYS
        and isinstance(manifest.get("started_at"), str)
        and isinstance(manifest.get("completed_at"), str)
        and is_valid_build_manifest(manifest)
    ):
        return manifest
    return _not_built_manifest()


@contextmanager
def _shared_build_lock_if_available() -> Iterator[bool]:
    """Hold a shared build lock when no structural build is already active."""
    try:
        handle = paths.open_private_lock_file(paths.model_build_lock())
    except (OSError, RuntimeError):
        yield False
        return
    acquired = False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            pass
        yield acquired
    finally:
        if acquired:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


@contextmanager
def live_model_generation() -> Iterator[dict[str, Any]]:
    """Hold one stable model generation while a derived projection is published."""
    with _shared_build_lock_if_available() as shared_lock_acquired:
        if shared_lock_acquired:
            yield _classify_persisted_manifest(load_last_manifest())
        else:
            yield _building_manifest(load_last_manifest())


def load_live_manifest() -> dict[str, Any]:
    """Return truthful metadata for a live projection or export.

    Merely reading the current database is not a model build. A missing,
    malformed, or interrupted manifest must therefore never be replaced by the
    snapshot helper's synthetic ``complete`` metadata. The shared lock closes
    the pre-marker race where a builder owns the exclusive lock but has not yet
    replaced the previous completed manifest.
    """
    with live_model_generation() as manifest:
        return manifest


def _build_live_snapshot_from_manifest(
    conn: Any,
    build_metadata: dict[str, Any],
    *,
    redact: bool = True,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Read one SQLite snapshot using metadata from a held model generation."""
    savepoint = "persome_live_model_snapshot"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        snapshot = build_snapshot(
            conn,
            redact=redact,
            generated_at=generated_at,
            build_metadata=build_metadata,
        )
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        return snapshot
    except BaseException:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise


def build_live_snapshot(
    conn: Any,
    *,
    redact: bool = True,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Read one transactionally stable geometry + manifest projection.

    A shared build lock prevents a new explicit structural build from starting
    between manifest and geometry reads. If a build already owns the exclusive
    lock, the public manifest is ``building`` and the SQLite savepoint still
    gives all geometry queries one WAL snapshot.
    """
    with live_model_generation() as build_metadata:
        return _build_live_snapshot_from_manifest(
            conn,
            build_metadata,
            redact=redact,
            generated_at=generated_at,
        )


def run_model_build(
    cfg: Any,
    *,
    wait_seconds: float = DEFAULT_WAIT_SECONDS,
    trigger: str = "cli",
    coordinator: ModelBuildCoordinator | None = None,
    pipeline_runner: Callable[[Any], PipelineOutcome] | None = None,
    now: Callable[[], datetime] | None = None,
) -> ModelBuildResult:
    """Run one idempotent build and persist Core-owned stage receipts."""
    if (
        paths.integrity_recovery_pending().exists()
        or paths.integrity_config_recovery_pending().exists()
    ):
        raise ModelRecoveryIncomplete(
            "database/config recovery is incomplete; repair the reported source and rerun "
            "a stopped-Runtime CLI command before building"
        )
    clock = now or (lambda: datetime.now(UTC))
    coordinator = coordinator or ModelBuildCoordinator()
    with coordinator.acquire(wait_seconds=wait_seconds):
        started_dt = clock()
        started_monotonic = time.monotonic()
        pipeline_kind = "override" if pipeline_runner is not None else "core"
        building_marker = {
            "build_id": None,
            "status": "building",
            "trigger": trigger,
            "started_at": started_dt.isoformat(),
            "completed_at": None,
            "duration_ms": 0,
            "degraded_stages": [],
        }
        _write_json_owner_only(paths.model_build_manifest(), building_marker)
        artifact = create_build_stage_artifact(
            trigger=trigger,
            pipeline_kind=pipeline_kind,
            started_at=_processing_timestamp(),
        )
        _write_json_owner_only(paths.model_build_stage_receipt(), artifact)
        recorder = _StageReceiptRecorder(artifact)
        artifact_bound = False
        try:
            NodeStore()  # ensure the Point store exists even on a completely fresh root
            if pipeline_runner is None:
                outcome = _run_pipeline(
                    cfg,
                    recorder=recorder,
                    stage_clock=started_dt,
                )
            else:
                # This private test seam is visibly marked as an override. The
                # callback's PipelineOutcome (including arbitrary stage names,
                # errors, prompts, or personal text) keeps its legacy in-memory
                # behavior but is never trusted or persisted in the sidecar.
                recorder.start("pipeline_override")
                try:
                    outcome = pipeline_runner(cfg)
                except Exception:
                    recorder.finish(
                        "pipeline_override",
                        status="failed",
                        error_code="stage_failed",
                    )
                    raise
                except BaseException:
                    recorder.finish(
                        "pipeline_override",
                        status="interrupted",
                        error_code="stage_interrupted",
                    )
                    raise
                recorder.finish("pipeline_override", status="complete", outputs={})

            recorder.start("model_contract")
            try:
                with fts.cursor() as conn:
                    provisional = build_snapshot(
                        conn,
                        generated_at=started_dt.isoformat(),
                        build_metadata={"degraded_stages": outcome.degraded_stages},
                    )
            except Exception:
                recorder.finish(
                    "model_contract",
                    status="failed",
                    error_code="model_contract_failed",
                )
                raise
            except BaseException:
                recorder.finish(
                    "model_contract",
                    status="interrupted",
                    error_code="stage_interrupted",
                )
                raise

            stats = provisional["stats"]
            contract_incomplete = (
                not provisional["points"]
                or stats["evolution_lines"] + stats["relation_lines"] == 0
                or not provisional["faces"]
                or not provisional["volumes"]
                or provisional["root"] is None
            )
            recorder.finish(
                "model_contract",
                status="failed" if contract_incomplete else "complete",
                error_code="incomplete_geometry" if contract_incomplete else None,
                outputs=stats,
            )
            if contract_incomplete:
                outcome.degraded_stages.append("model_contract")

            receipt_names = tuple(receipt["name"] for receipt in recorder.receipts)
            expected_names = (
                CORE_MODEL_BUILD_STAGES
                if pipeline_kind == "core"
                else ("pipeline_override", "model_contract")
            )
            if receipt_names != expected_names:
                raise RuntimeError("model-build stage receipt order violated")

            completed_dt = clock()
            manifest = create_build_manifest(
                models=_models(cfg),
                config=asdict(cfg),
                input_window=_input_window(),
                degraded_stages=outcome.degraded_stages,
                started_at=started_dt.isoformat(),
                completed_at=completed_dt.isoformat(),
                duration_ms=round((time.monotonic() - started_monotonic) * 1000),
                trigger=trigger,
            )
            with fts.cursor() as conn:
                snapshot = build_snapshot(
                    conn,
                    redact=False,
                    generated_at=completed_dt.isoformat(),
                    build_metadata=manifest,
                )
            _write_json_owner_only(paths.model_build_manifest(), manifest)
            recorder.bind_manifest(manifest)
            artifact_bound = True
            human_path: Path | None = None
            try:
                from .human import materialize_human_markdown

                human_path = materialize_human_markdown(snapshot)
            except Exception as exc:  # noqa: BLE001 - a derived view never fails the build
                logger.warning("HUMAN.md projection failed after model build: %s", exc)
            return ModelBuildResult(
                status=manifest["status"],
                manifest=manifest,
                stats=snapshot["stats"],
                stages=outcome.stages,
                manifest_path=paths.model_build_manifest(),
                human_path=human_path,
            )
        except Exception:
            if not artifact_bound:
                try:
                    recorder.fail_build(interrupted=False)
                except (OSError, RuntimeError, ValueError) as receipt_exc:
                    logger.error("failed to persist model-build failure receipt: %s", receipt_exc)
            raise
        except BaseException:
            if not artifact_bound:
                try:
                    recorder.fail_build(interrupted=True)
                except (OSError, RuntimeError, ValueError) as receipt_exc:
                    logger.error(
                        "failed to persist model-build interruption receipt: %s",
                        receipt_exc,
                    )
            raise

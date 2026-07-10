"""One-shot personal-model build orchestration shared by CLI and scheduled callers."""

from __future__ import annotations

import fcntl
import json
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import paths
from ..evomem.store import NodeStore
from ..store import fts
from .manifest import create_build_manifest
from .snapshot import build_snapshot

DEFAULT_WAIT_SECONDS = 30.0
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


class ModelBuildBusy(RuntimeError):
    """Another process still owns the model-build lock after the requested wait."""


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


class ModelBuildCoordinator:
    """Cross-process coordinator backed by a kernel-released ``flock`` lock."""

    def __init__(self, lock_path: Path | None = None) -> None:
        self.lock_path = lock_path or paths.model_build_lock()

    @contextmanager
    def acquire(self, *, wait_seconds: float = DEFAULT_WAIT_SECONDS) -> Iterator[None]:
        wait = max(0.0, float(wait_seconds))
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+b")
        os.chmod(self.lock_path, 0o600)
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


def _run_stage(
    outcome: PipelineOutcome,
    name: str,
    operation: Callable[[], dict[str, Any]],
    *,
    enabled: bool = True,
    skip_reason: str = "disabled",
) -> None:
    if not enabled:
        outcome.stages[name] = {"status": "skipped", "reason": skip_reason, "duration_ms": 0}
        return
    started = time.monotonic()
    try:
        details = operation()
    except Exception as exc:  # noqa: BLE001 - a build records degradation and continues
        outcome.degraded_stages.append(name)
        outcome.stages[name] = {
            "status": "failed",
            "reason": f"{type(exc).__name__}: {exc}",
            "duration_ms": round((time.monotonic() - started) * 1000),
        }
        return
    outcome.stages[name] = {
        "status": "complete",
        "duration_ms": round((time.monotonic() - started) * 1000),
        **details,
    }


def _run_pipeline(cfg: Any) -> PipelineOutcome:
    """Run the existing one-shot stage functions in paper-model order."""
    from .. import vectors_tick
    from ..session.tick import _run_evomem_enrichment_once
    from ..viz import sem_layout
    from ..writer import agent as writer_agent
    from ..writer import cross_domain_sweeper, root_synthesis, schema_miner_stage

    outcome = PipelineOutcome()

    def run_writer() -> dict[str, Any]:
        result = writer_agent.run(cfg)
        return {
            "reduced": result.reduced,
            "classified": result.classified,
            "written": len(result.written_ids),
        }

    _run_stage(outcome, "state_formation", run_writer, enabled=cfg.reducer.enabled)

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

    _run_stage(outcome, "evomem_baseline", run_evomem_baseline)

    def run_enrichment() -> dict[str, Any]:
        return _run_evomem_enrichment_once(cfg, raise_on_error=True)

    enrichment_enabled = bool(
        getattr(cfg, "person_graph_enabled", False)
        or getattr(cfg, "case_extraction_enabled", False)
        or getattr(cfg, "relation_extraction_enabled", False)
    )
    _run_stage(outcome, "entity_relation_enrichment", run_enrichment, enabled=enrichment_enabled)

    def run_schema() -> dict[str, Any]:
        with fts.cursor() as conn:
            result = schema_miner_stage.mine_schemas_for_user(cfg, conn)
        return {
            "written": result.written_count,
            "skipped_small": result.skipped_small,
            "skipped_empty": result.skipped_empty,
        }

    _run_stage(outcome, "schema_miner", run_schema, enabled=cfg.schema.enabled)

    def run_cross_domain() -> dict[str, Any]:
        with fts.cursor() as conn:
            result = cross_domain_sweeper.sweep_cross_domain(
                cfg,
                conn,
                behavior_max_distance=cfg.schema.cross_domain_behavior_max_distance,
                min_confidence=cfg.schema.cross_domain_min_confidence,
            )
        return {
            "written": result.written_count,
            "pairs_considered": result.pairs_considered,
            "pairs_probed": result.pairs_probed,
        }

    _run_stage(
        outcome,
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

    _run_stage(outcome, "vector_backfill", run_vectors)

    def run_layout() -> dict[str, Any]:
        return sem_layout.generate(paths.index_db(), paths.root() / "sem_facts.json")

    _run_stage(outcome, "semantic_layout", run_layout)
    return outcome


def _input_window() -> dict[str, str | None]:
    with fts.cursor() as conn:
        row = conn.execute(
            """
            SELECT MIN(value), MAX(value) FROM (
                SELECT timestamp AS value FROM captures
                UNION ALL SELECT start_time AS value FROM sessions
                UNION ALL SELECT end_time AS value FROM sessions WHERE end_time IS NOT NULL
            )
            """
        ).fetchone()
    return {"start": row[0] if row else None, "end": row[1] if row else None}


def _models(cfg: Any) -> dict[str, str]:
    return {stage: cfg.model_for(stage).model for stage in _MODEL_STAGES}


def _write_json_owner_only(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def load_last_manifest() -> dict[str, Any] | None:
    try:
        payload = json.loads(paths.model_build_manifest().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def run_model_build(
    cfg: Any,
    *,
    wait_seconds: float = DEFAULT_WAIT_SECONDS,
    trigger: str = "cli",
    coordinator: ModelBuildCoordinator | None = None,
    pipeline_runner: Callable[[Any], PipelineOutcome] = _run_pipeline,
    now: Callable[[], datetime] | None = None,
) -> ModelBuildResult:
    """Run one idempotent build and persist its reproducibility manifest."""
    clock = now or (lambda: datetime.now(UTC))
    coordinator = coordinator or ModelBuildCoordinator()
    with coordinator.acquire(wait_seconds=wait_seconds):
        started_dt = clock()
        started_monotonic = time.monotonic()
        NodeStore()  # ensure the Point store exists even on a completely fresh root
        outcome = pipeline_runner(cfg)

        with fts.cursor() as conn:
            provisional = build_snapshot(
                conn,
                generated_at=started_dt.isoformat(),
                build_metadata={"degraded_stages": outcome.degraded_stages},
            )
        degraded = list(outcome.degraded_stages)
        stats = provisional["stats"]
        if (
            not provisional["points"]
            or stats["evolution_lines"] + stats["relation_lines"] == 0
            or not provisional["faces"]
            or not provisional["volumes"]
            or provisional["root"] is None
        ):
            degraded.append("model_contract")

        completed_dt = clock()
        manifest = create_build_manifest(
            models=_models(cfg),
            config=asdict(cfg),
            input_window=_input_window(),
            degraded_stages=degraded,
            started_at=started_dt.isoformat(),
            completed_at=completed_dt.isoformat(),
            duration_ms=round((time.monotonic() - started_monotonic) * 1000),
            trigger=trigger,
        )
        with fts.cursor() as conn:
            snapshot = build_snapshot(
                conn,
                generated_at=completed_dt.isoformat(),
                build_metadata=manifest,
            )
        _write_json_owner_only(paths.model_build_manifest(), manifest)
        return ModelBuildResult(
            status=manifest["status"],
            manifest=manifest,
            stats=snapshot["stats"],
            stages=outcome.stages,
            manifest_path=paths.model_build_manifest(),
        )

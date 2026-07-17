"""Content-free, Core-owned execution receipts for explicit model builds."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from .manifest import is_valid_build_manifest

STAGE_RECEIPT_SCHEMA_VERSION = 1
CORE_MODEL_BUILD_STAGES = (
    "state_formation",
    "evomem_baseline",
    "entity_relation_enrichment",
    "schema_miner",
    "cross_domain_sweeper",
    "root_synthesis",
    "vector_backfill",
    "model_contract",
)
MODEL_BUILD_STAGE_OUTPUT_KEYS = {
    "state_formation": frozenset({"reduced", "classified", "written"}),
    "evomem_baseline": frozenset({"backfilled"}),
    "entity_relation_enrichment": frozenset(
        {
            "person_updates",
            "case_cards",
            "relation_edges",
            "attention_digest",
            "relation_promoted",
        }
    ),
    "schema_miner": frozenset({"written", "skipped_small", "skipped_empty"}),
    "cross_domain_sweeper": frozenset(
        {
            "written",
            "pairs_considered",
            "eligible_pairs",
            "pairs_probed",
            "probe_limit",
            "pairs_deferred",
            "collisions",
        }
    ),
    "root_synthesis": frozenset({"roots_written"}),
    "vector_backfill": frozenset({"enqueued", "embedded", "queued"}),
    "model_contract": frozenset(
        {
            "points",
            "active_points",
            "evolution_lines",
            "relation_lines",
            "faces",
            "volumes",
            "roots",
            "receipts",
        }
    ),
    "pipeline_override": frozenset(),
}

_ARTIFACT_KEYS = frozenset(
    {
        "schema_version",
        "artifact_id",
        "status",
        "pipeline_kind",
        "build_id",
        "core_commit",
        "core_commit_digest",
        "config_hash",
        "manifest_digest",
        "trigger_label",
        "trigger_digest",
        "started_at",
        "completed_at",
        "failure_code",
        "degraded_stages",
        "stages",
    }
)
_STAGE_KEYS = frozenset(
    {
        "receipt_id",
        "name",
        "status",
        "started_at",
        "completed_at",
        "duration_ms",
        "degraded",
        "error_code",
        "inputs",
        "outputs",
    }
)
_FINAL_STAGE_STATUSES = frozenset({"complete", "skipped", "failed", "interrupted"})
_PIPELINE_KINDS = frozenset({"core", "override"})
_ARTIFACT_FAILURE_CODES = frozenset({"build_failed", "build_interrupted"})
_STAGE_ERROR_CODES = {
    "complete": frozenset({None}),
    "skipped": frozenset({"disabled_by_config"}),
    "failed": frozenset({"stage_failed", "model_contract_failed", "incomplete_geometry"}),
    "interrupted": frozenset({"stage_interrupted"}),
    "running": frozenset({None}),
}
_SAFE_TRIGGER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SAFE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_CONFIG_HASH = re.compile(r"^[0-9a-f]{64}$")
_SAFE_BUILD_ID = re.compile(r"^[0-9a-f]{20}$")
_SAFE_CORE_COMMIT = re.compile(r"^(?:[0-9a-f]{7,64}|unknown)$")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def content_digest(value: Any) -> str:
    """Return a full canonical SHA-256 digest for independent recomputation."""
    return f"sha256:{hashlib.sha256(_canonical_bytes(value)).hexdigest()}"


def trigger_binding(trigger: str) -> tuple[str, str]:
    """Bind an arbitrary trigger without copying unsafe caller text."""
    label = trigger if _SAFE_TRIGGER.fullmatch(trigger) is not None else "other"
    return label, content_digest(trigger)


def core_commit_binding(core_commit: str) -> tuple[str, str]:
    """Bind a configured commit without copying an unsafe override value."""
    label = core_commit if _SAFE_CORE_COMMIT.fullmatch(core_commit) is not None else "other"
    return label, content_digest(core_commit)


def _processing_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _safe_metrics(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    return all(
        isinstance(key, str)
        and (
            isinstance(metric, int)
            and not isinstance(metric, bool)
            and metric >= 0
            or isinstance(metric, str)
            and _SAFE_DIGEST.fullmatch(metric) is not None
        )
        for key, metric in value.items()
    )


def create_stage_receipt(
    *,
    name: str,
    status: str,
    started_at: str,
    completed_at: str | None,
    duration_ms: int,
    degraded: bool,
    error_code: str | None,
    outputs: Mapping[str, int | str] | None = None,
) -> dict[str, Any]:
    """Create one fixed-shape receipt with no arbitrary text fields."""
    receipt: dict[str, Any] = {
        "name": name,
        "status": status,
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_ms": max(0, int(duration_ms)),
        "degraded": bool(degraded),
        "error_code": error_code,
        "inputs": {},
        "outputs": dict(sorted((outputs or {}).items())),
    }
    receipt["receipt_id"] = content_digest(receipt)
    if not is_valid_stage_receipt(receipt, allow_running=status == "running"):
        raise ValueError("invalid model-build stage receipt")
    return receipt


def is_valid_stage_receipt(
    receipt: Mapping[str, Any],
    *,
    allow_running: bool = False,
) -> bool:
    if set(receipt) != _STAGE_KEYS:
        return False
    name = receipt.get("name")
    status = receipt.get("status")
    error_code = receipt.get("error_code")
    started_at = _processing_datetime(receipt.get("started_at"))
    completed_at = _processing_datetime(receipt.get("completed_at"))
    allowed_statuses = _FINAL_STAGE_STATUSES | ({"running"} if allow_running else set())
    if (
        not isinstance(name, str)
        or name not in MODEL_BUILD_STAGE_OUTPUT_KEYS
        or status not in allowed_statuses
        or started_at is None
        or not isinstance(receipt.get("duration_ms"), int)
        or isinstance(receipt.get("duration_ms"), bool)
        or receipt["duration_ms"] < 0
        or not isinstance(receipt.get("degraded"), bool)
        or receipt.get("inputs") != {}
        or not _safe_metrics(receipt.get("outputs"))
        or error_code not in _STAGE_ERROR_CODES.get(str(status), frozenset())
    ):
        return False
    outputs = receipt["outputs"]
    if status == "running":
        if completed_at is not None or receipt["degraded"] or outputs:
            return False
    else:
        if completed_at is None or completed_at < started_at:
            return False
        should_degrade = status in {"failed", "interrupted"}
        if receipt["degraded"] is not should_degrade:
            return False
        if status == "complete" and set(outputs) != set(MODEL_BUILD_STAGE_OUTPUT_KEYS[name]):
            return False
        if status == "skipped" and outputs:
            return False
        if should_degrade and not set(outputs).issubset(MODEL_BUILD_STAGE_OUTPUT_KEYS[name]):
            return False
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_id"}
    return receipt.get("receipt_id") == content_digest(unsigned)


def create_build_stage_artifact(
    *,
    trigger: str,
    pipeline_kind: str,
    started_at: str,
) -> dict[str, Any]:
    """Create the initial unbound artifact written before any stage enters."""
    if pipeline_kind not in _PIPELINE_KINDS:
        raise ValueError(f"unsupported stage-receipt pipeline kind: {pipeline_kind!r}")
    if _processing_datetime(started_at) is None:
        raise ValueError("stage-receipt processing timestamp must be timezone-aware")
    trigger_label, trigger_digest = trigger_binding(trigger)
    artifact: dict[str, Any] = {
        "schema_version": STAGE_RECEIPT_SCHEMA_VERSION,
        "status": "building",
        "pipeline_kind": pipeline_kind,
        "build_id": None,
        "core_commit": None,
        "core_commit_digest": None,
        "config_hash": None,
        "manifest_digest": None,
        "trigger_label": trigger_label,
        "trigger_digest": trigger_digest,
        "started_at": started_at,
        "completed_at": None,
        "failure_code": None,
        "degraded_stages": [],
        "stages": [],
    }
    artifact["artifact_id"] = content_digest(artifact)
    if not is_valid_build_stage_artifact(artifact, allow_incomplete=True):
        raise ValueError("invalid initial model-build stage artifact")
    return artifact


def refresh_artifact_id(artifact: Mapping[str, Any]) -> dict[str, Any]:
    refreshed = dict(artifact)
    refreshed["stages"] = [dict(stage) for stage in artifact.get("stages", [])]
    unsigned = {key: value for key, value in refreshed.items() if key != "artifact_id"}
    refreshed["artifact_id"] = content_digest(unsigned)
    return refreshed


def bind_completed_artifact(
    artifact: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any],
    completed_at: str,
) -> dict[str, Any]:
    """Bind final Core outcomes to the exact persisted v1 build manifest."""
    if not is_valid_build_manifest(manifest):
        raise ValueError("cannot bind stage receipts to an invalid build manifest")
    bound = dict(artifact)
    receipt_degraded = [stage["name"] for stage in bound["stages"] if stage["degraded"]]
    core_pipeline = bound.get("pipeline_kind") == "core"
    core_commit, core_commit_digest = core_commit_binding(str(manifest["core_commit"]))
    bound.update(
        {
            "status": (
                manifest["status"]
                if core_pipeline
                else ("degraded" if receipt_degraded else "complete")
            ),
            "build_id": manifest["build_id"],
            "core_commit": core_commit,
            "core_commit_digest": core_commit_digest,
            "config_hash": manifest["config_hash"],
            "manifest_digest": content_digest(manifest),
            "completed_at": completed_at,
            "failure_code": None,
            "degraded_stages": (
                list(manifest["degraded_stages"]) if core_pipeline else receipt_degraded
            ),
        }
    )
    bound = refresh_artifact_id(bound)
    if not is_valid_build_stage_artifact(bound):
        raise ValueError("completed model-build stage artifact is inconsistent")
    return bound


def fail_build_stage_artifact(
    artifact: Mapping[str, Any],
    *,
    interrupted: bool,
    completed_at: str,
) -> dict[str, Any]:
    """Close a caught build failure without persisting its exception text."""
    failed = dict(artifact)
    failed.update(
        {
            "status": "interrupted" if interrupted else "failed",
            "completed_at": completed_at,
            "failure_code": "build_interrupted" if interrupted else "build_failed",
            "degraded_stages": [stage["name"] for stage in failed["stages"] if stage["degraded"]],
        }
    )
    failed = refresh_artifact_id(failed)
    if not is_valid_build_stage_artifact(failed, allow_incomplete=True):
        raise ValueError("failed model-build stage artifact is inconsistent")
    return failed


def is_valid_build_stage_artifact(
    artifact: Mapping[str, Any],
    *,
    allow_incomplete: bool = False,
) -> bool:
    """Validate one artifact using only its public, content-free contract."""
    if set(artifact) != _ARTIFACT_KEYS:
        return False
    status = artifact.get("status")
    pipeline_kind = artifact.get("pipeline_kind")
    stages = artifact.get("stages")
    started_at = _processing_datetime(artifact.get("started_at"))
    completed_at = _processing_datetime(artifact.get("completed_at"))
    if (
        artifact.get("schema_version") != STAGE_RECEIPT_SCHEMA_VERSION
        or pipeline_kind not in _PIPELINE_KINDS
        or status not in {"building", "complete", "degraded", "failed", "interrupted"}
        or started_at is None
        or not isinstance(stages, list)
        or not all(
            isinstance(stage, Mapping)
            and is_valid_stage_receipt(
                stage,
                allow_running=allow_incomplete and status == "building",
            )
            for stage in stages
        )
        or artifact.get("trigger_label") is None
        or _SAFE_TRIGGER.fullmatch(str(artifact["trigger_label"])) is None
        or _SAFE_DIGEST.fullmatch(str(artifact.get("trigger_digest"))) is None
        or not isinstance(artifact.get("degraded_stages"), list)
    ):
        return False
    expected_names = (
        CORE_MODEL_BUILD_STAGES
        if pipeline_kind == "core"
        else ("pipeline_override", "model_contract")
    )
    stage_names = tuple(stage["name"] for stage in stages)
    running_indexes = [index for index, stage in enumerate(stages) if stage["status"] == "running"]
    if (
        len(set(stage_names)) != len(stage_names)
        or stage_names != expected_names[: len(stage_names)]
        or len(running_indexes) > 1
        or running_indexes
        and running_indexes[0] != len(stages) - 1
        or not allow_incomplete
        and stage_names != expected_names
    ):
        return False
    last_completed = started_at
    for stage in stages:
        stage_started = _processing_datetime(stage["started_at"])
        if stage_started is None or stage_started < last_completed:
            return False
        stage_completed = _processing_datetime(stage.get("completed_at"))
        if stage_completed is not None:
            last_completed = stage_completed
    degraded = [stage["name"] for stage in stages if stage["degraded"]]
    recorded_degraded = list(artifact["degraded_stages"])
    if len(set(recorded_degraded)) != len(recorded_degraded) or set(recorded_degraded) != set(
        degraded
    ):
        return False
    if status in {"complete", "degraded"}:
        if (
            completed_at is None
            or completed_at < last_completed
            or artifact.get("failure_code") is not None
            or _SAFE_BUILD_ID.fullmatch(str(artifact.get("build_id"))) is None
            or artifact.get("core_commit") is None
            or _SAFE_CORE_COMMIT.fullmatch(str(artifact.get("core_commit"))) is None
            and artifact.get("core_commit") != "other"
            or _SAFE_DIGEST.fullmatch(str(artifact.get("core_commit_digest"))) is None
            or _SAFE_CONFIG_HASH.fullmatch(str(artifact.get("config_hash"))) is None
            or _SAFE_DIGEST.fullmatch(str(artifact.get("manifest_digest"))) is None
            or (status == "complete") != (not degraded)
        ):
            return False
    elif status == "building":
        if (
            not allow_incomplete
            or completed_at is not None
            or artifact.get("failure_code") is not None
            or any(
                artifact.get(key) is not None
                for key in (
                    "build_id",
                    "core_commit",
                    "core_commit_digest",
                    "config_hash",
                    "manifest_digest",
                )
            )
        ):
            return False
    else:
        if (
            not allow_incomplete
            or completed_at is None
            or completed_at < last_completed
            or artifact.get("failure_code") not in _ARTIFACT_FAILURE_CODES
            or any(
                artifact.get(key) is not None
                for key in (
                    "build_id",
                    "core_commit",
                    "core_commit_digest",
                    "config_hash",
                    "manifest_digest",
                )
            )
        ):
            return False
    unsigned = {key: value for key, value in artifact.items() if key != "artifact_id"}
    return artifact.get("artifact_id") == content_digest(unsigned)


def artifact_matches_manifest(
    artifact: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> bool:
    """Return whether a final artifact is bound to this exact v1 manifest."""
    core_commit, core_commit_digest = core_commit_binding(str(manifest.get("core_commit")))
    if not (
        is_valid_build_stage_artifact(artifact)
        and is_valid_build_manifest(manifest)
        and artifact.get("build_id") == manifest.get("build_id")
        and artifact.get("core_commit") == core_commit
        and artifact.get("core_commit_digest") == core_commit_digest
        and artifact.get("config_hash") == manifest.get("config_hash")
        and artifact.get("manifest_digest") == content_digest(manifest)
        and artifact.get("trigger_digest") == trigger_binding(str(manifest.get("trigger")))[1]
    ):
        return False
    if artifact.get("pipeline_kind") == "core":
        return bool(
            artifact.get("status") == manifest.get("status")
            and artifact.get("degraded_stages") == manifest.get("degraded_stages")
        )
    return True

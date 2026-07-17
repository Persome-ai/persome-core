"""Public model-contract surface for Personome runtime consumers."""

from .activity_source import (
    ACTIVITY_PREFIX,
    SOURCE_KINDS,
    ActivityEvent,
    ActivitySource,
    is_activity_identity,
    normalize_activity_identity,
)
from .build import (
    DEFAULT_WAIT_SECONDS,
    ModelBuildBusy,
    ModelBuildCoordinator,
    ModelBuildResult,
    ModelRecoveryIncomplete,
    PipelineOutcome,
    build_live_snapshot,
    load_last_manifest,
    load_live_manifest,
    run_model_build,
)
from .entity_source import EntityEvent, EntitySource, MemoryPersonNameSource
from .human import (
    materialize_human_markdown,
    render_human_markdown,
    sync_live_human_markdown,
)
from .manifest import (
    create_build_manifest,
    detect_core_commit,
    is_valid_build_manifest,
    prompt_hashes,
)
from .schema_reader import active_schema_inferences, active_schema_inferences_with_sources
from .snapshot import (
    SCHEMA_VERSION,
    ModelContractError,
    build_snapshot,
    export_snapshot,
    model_status,
    validate_snapshot,
)
from .stage_receipt import (
    CORE_MODEL_BUILD_STAGES,
    MODEL_BUILD_STAGE_OUTPUT_KEYS,
    STAGE_RECEIPT_SCHEMA_VERSION,
    artifact_matches_manifest,
    is_valid_build_stage_artifact,
    is_valid_stage_receipt,
)

__all__ = [
    "SCHEMA_VERSION",
    "STAGE_RECEIPT_SCHEMA_VERSION",
    "CORE_MODEL_BUILD_STAGES",
    "MODEL_BUILD_STAGE_OUTPUT_KEYS",
    "ACTIVITY_PREFIX",
    "DEFAULT_WAIT_SECONDS",
    "ModelBuildBusy",
    "ModelBuildCoordinator",
    "ModelBuildResult",
    "ModelRecoveryIncomplete",
    "ModelContractError",
    "PipelineOutcome",
    "SOURCE_KINDS",
    "ActivityEvent",
    "ActivitySource",
    "EntityEvent",
    "EntitySource",
    "MemoryPersonNameSource",
    "active_schema_inferences",
    "active_schema_inferences_with_sources",
    "build_snapshot",
    "build_live_snapshot",
    "create_build_manifest",
    "detect_core_commit",
    "export_snapshot",
    "load_last_manifest",
    "load_live_manifest",
    "is_activity_identity",
    "is_valid_build_manifest",
    "is_valid_build_stage_artifact",
    "is_valid_stage_receipt",
    "artifact_matches_manifest",
    "materialize_human_markdown",
    "model_status",
    "normalize_activity_identity",
    "prompt_hashes",
    "render_human_markdown",
    "run_model_build",
    "sync_live_human_markdown",
    "validate_snapshot",
]

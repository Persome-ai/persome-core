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

__all__ = [
    "SCHEMA_VERSION",
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
    "materialize_human_markdown",
    "model_status",
    "normalize_activity_identity",
    "prompt_hashes",
    "render_human_markdown",
    "run_model_build",
    "sync_live_human_markdown",
    "validate_snapshot",
]

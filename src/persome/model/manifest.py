"""Reproducibility metadata for model builds and snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def prompt_hashes(prompt_dir: Path | None = None) -> dict[str, str]:
    """Hash every packaged prompt by relative name, in deterministic order."""
    base = prompt_dir or Path(__file__).resolve().parents[1] / "prompts"
    if not base.exists():
        return {}
    return {
        str(path.relative_to(base)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(base.rglob("*.md"))
    }


def detect_core_commit(repo_root: Path | None = None) -> str:
    """Resolve the core commit without making it a hard runtime dependency on git."""
    if configured := os.environ.get("PERSOME_CORE_COMMIT"):
        return configured.strip()
    root = repo_root or Path(__file__).resolve().parents[3]
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


def create_build_manifest(
    *,
    core_commit: str | None = None,
    models: dict[str, str] | None = None,
    prompt_dir: Path | None = None,
    config: dict[str, Any] | None = None,
    input_window: dict[str, str | None] | None = None,
    degraded_stages: list[str] | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
    duration_ms: int = 0,
    trigger: str = "snapshot",
    mode: str | None = None,
) -> dict[str, Any]:
    """Create complete, deterministic build metadata.

    Secrets are not copied into the manifest. Configuration is represented only by a stable hash.
    Callers may pass fixed timestamps to make mock builds byte-reproducible.
    """
    started = started_at or datetime.now(UTC).isoformat()
    completed = completed_at or started
    degraded = sorted(set(degraded_stages or []))
    manifest: dict[str, Any] = {
        "core_commit": core_commit or detect_core_commit(),
        "models": dict(sorted((models or {}).items())),
        "prompt_hashes": prompt_hashes(prompt_dir),
        "config_hash": _stable_hash(config or {}),
        "input_window": input_window or {"start": None, "end": None},
        "mode": mode or ("mock" if os.environ.get("PERSOME_LLM_MOCK") == "1" else "real"),
        "trigger": trigger,
        "status": "degraded" if degraded else "complete",
        "degraded_stages": degraded,
        "started_at": started,
        "completed_at": completed,
        "duration_ms": max(0, int(duration_ms)),
    }
    manifest["build_id"] = _stable_hash(manifest)[:20]
    return manifest

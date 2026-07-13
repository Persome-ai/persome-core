"""CLI coverage for one-shot personal-model operations."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from persome import cli, paths
from persome import model as model_mod
from persome.writer import correct as correct_mod
from persome.writer import root_synthesis as root_synthesis_mod


@pytest.mark.parametrize(
    ("shell_value", "expected"),
    [
        (None, "key-from-owner-env"),
        ("key-from-shell", "key-from-shell"),
    ],
    ids=["owner-env", "shell-precedence"],
)
def test_model_build_loads_runtime_env_before_llm_work(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    shell_value: str | None,
    expected: str,
) -> None:
    """A standalone model build sees the saved key without a daemon process."""
    env_path = paths.env_file()
    env_path.write_text("PERSOME_LLM_API_KEY=key-from-owner-env\n", encoding="utf-8")
    if shell_value is None:
        # Track the initially absent variable so pytest restores it after the
        # loader mutates os.environ directly.
        monkeypatch.setenv("PERSOME_LLM_API_KEY", "temporary")
        monkeypatch.delenv("PERSOME_LLM_API_KEY")
    else:
        monkeypatch.setenv("PERSOME_LLM_API_KEY", shell_value)

    seen: list[str | None] = []

    def fake_build(_cfg, **_kwargs):  # type: ignore[no-untyped-def]
        seen.append(os.environ.get("PERSOME_LLM_API_KEY"))
        return SimpleNamespace(
            status="complete",
            stages={
                "cross_domain_sweeper": {
                    "status": "complete",
                    "pairs_probed": 8,
                    "pairs_deferred": 3,
                    "probe_limit": 8,
                }
            },
            stats={
                "points": 1,
                "evolution_lines": 1,
                "relation_lines": 0,
                "faces": 1,
                "volumes": 1,
                "roots": 1,
            },
            manifest_path=paths.model_build_manifest(),
            human_path=None,
        )

    monkeypatch.setattr(model_mod, "run_model_build", fake_build)

    result = CliRunner().invoke(cli.app, ["model", "build"])

    assert result.exit_code == 0, result.output
    assert seen == [expected]
    assert "cross-domain: probed=8 deferred=3 limit=8" in result.output
    assert "Deferred pairs stay queued" in result.output
    assert "HUMAN.md:" not in result.output


def test_model_build_cli_reports_integrity_recovery_block(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_init", lambda: SimpleNamespace())
    paths.atomic_write_private_text(
        paths.integrity_config_recovery_pending(),
        '{"version": 1, "phase": "authority_unresolved"}',
    )

    result = CliRunner().invoke(cli.app, ["model", "build"])

    assert result.exit_code == 2
    assert "model build blocked" in result.output
    assert "recovery is incomplete" in result.output


def test_model_export_without_build_manifest_is_truthfully_not_built(ac_root: Path) -> None:
    output = ac_root / "model-export.json"

    result = CliRunner().invoke(cli.app, ["model", "export", "--out", str(output)])

    assert result.exit_code == 0, result.output
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["build"]["status"] == "not_built"
    assert payload["build"]["trigger"] == "no_completed_build"
    assert payload["build"]["build_id"] is None


def test_model_export_uses_one_transactionally_stable_snapshot(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = ac_root / "stable-model-export.json"
    sentinel = {"schema_version": 1, "source": "live-reader"}
    calls: list[tuple[str, object]] = []

    def fake_live_snapshot(_conn, *, redact=True):  # type: ignore[no-untyped-def]
        calls.append(("read", redact))
        return sentinel

    def fake_export(_conn, *, out_path, snapshot_data, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(("write", snapshot_data))
        out_path.write_text("{}\n", encoding="utf-8")
        return out_path

    monkeypatch.setattr(model_mod, "build_live_snapshot", fake_live_snapshot)
    monkeypatch.setattr(model_mod, "export_snapshot", fake_export)

    result = CliRunner().invoke(cli.app, ["model", "export", "--out", str(output)])

    assert result.exit_code == 0, result.output
    assert calls == [("read", True), ("write", sentinel)]


def test_root_synth_loads_runtime_env(ac_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths.env_file().write_text("PERSOME_LLM_API_KEY=key-for-root\n", encoding="utf-8")
    monkeypatch.setenv("PERSOME_LLM_API_KEY", "temporary")
    monkeypatch.delenv("PERSOME_LLM_API_KEY")
    seen: list[str | None] = []

    def fake_synthesis(_cfg, _conn):  # type: ignore[no-untyped-def]
        seen.append(os.environ.get("PERSOME_LLM_API_KEY"))
        return SimpleNamespace(reason="written", face_id="root-test")

    monkeypatch.setattr(root_synthesis_mod, "synthesize_root", fake_synthesis)

    result = CliRunner().invoke(cli.app, ["root-synth"])

    assert result.exit_code == 0, result.output
    assert seen == ["key-for-root"]


def test_correct_loads_runtime_env(ac_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths.env_file().write_text("PERSOME_LLM_API_KEY=key-for-correction\n", encoding="utf-8")
    monkeypatch.setenv("PERSOME_LLM_API_KEY", "temporary")
    monkeypatch.delenv("PERSOME_LLM_API_KEY")
    seen: list[str | None] = []

    def fake_update(_cfg, _conn, _correction, **_kwargs):  # type: ignore[no-untyped-def]
        seen.append(os.environ.get("PERSOME_LLM_API_KEY"))
        return SimpleNamespace(kind="updated", ok=True, applied=[], reason="")

    monkeypatch.setattr(correct_mod, "update_memory", fake_update)

    result = CliRunner().invoke(cli.app, ["correct", "This is a test correction."])

    assert result.exit_code == 0, result.output
    assert seen == ["key-for-correction"]


def test_model_status_rejects_incomplete_completed_manifest(ac_root: Path) -> None:
    paths.atomic_write_private_text(
        paths.model_build_manifest(),
        json.dumps({"status": "complete", "build_id": "incomplete"}),
    )

    result = CliRunner().invoke(cli.app, ["model", "status"])

    assert result.exit_code == 0, result.output
    assert "model: not built" in result.output
    assert "no_completed_build" in result.output
    assert "build status: not_built" in result.output
    assert "last build: none" in result.output


def test_model_status_reports_active_build(ac_root: Path) -> None:
    coordinator = model_mod.ModelBuildCoordinator()
    with coordinator.acquire(wait_seconds=0):
        paths.atomic_write_private_text(
            paths.model_build_manifest(),
            json.dumps(
                {
                    "build_id": None,
                    "status": "building",
                    "trigger": "test-cli",
                    "started_at": "2026-07-12T08:00:00+00:00",
                    "completed_at": None,
                    "duration_ms": 0,
                    "degraded_stages": [],
                }
            ),
        )

        result = CliRunner().invoke(cli.app, ["model", "status"])

    assert result.exit_code == 0, result.output
    assert "model: building" in result.output
    assert "build_in_progress" in result.output
    assert "build status: building" in result.output
    assert "last build: none" in result.output

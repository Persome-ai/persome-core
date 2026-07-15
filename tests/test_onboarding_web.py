from __future__ import annotations

import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from persome import source_import
from persome.api import routes
from persome.api.onboarding_view import render_onboarding_view


@pytest.fixture(autouse=True)
def _reset_task() -> None:
    with routes._onboarding_task_lock:
        routes._onboarding_task.update(
            stage="idle",
            message="",
            imported=0,
            unchanged=0,
            skipped=0,
            error="",
        )
        routes._onboarding_selected_paths.clear()


def test_shell_keeps_every_step_in_one_local_page() -> None:
    body = render_onboarding_view("/model/" + "x" * 43 + "/")
    assert "Set up Persome" in body
    assert 'id="steps"' in body
    assert 'id="screen"' in body
    assert 'href="assets/onboarding.css"' in body
    assert 'src="assets/onboarding.js"' in body
    assert "http://" not in body and "https://" not in body


def test_state_only_shows_detected_product_sources(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "Personal"
    vault.mkdir()
    monkeypatch.setattr(source_import, "discover_obsidian_vaults", lambda: [vault])
    monkeypatch.setattr(source_import, "count_documents", lambda root: 12)
    monkeypatch.setattr(source_import, "notion_is_installed", lambda: False)
    monkeypatch.setattr(
        routes,
        "_onboarding_permissions",
        lambda: {"accessibility": "granted", "screen_recording": "granted"},
    )

    data = routes.onboarding_state().data

    assert [item["type"] for item in data["sources"]] == ["obsidian", "folder"]
    assert data["sources"][0]["label"] == "Obsidian — Personal"
    assert "12 notes" in data["sources"][0]["detail"]
    assert data["sources"][0]["available"] is True


def test_state_disables_source_that_exceeds_import_bounds(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "Personal"
    vault.mkdir()
    monkeypatch.setattr(source_import, "discover_obsidian_vaults", lambda: [vault])
    monkeypatch.setattr(
        source_import,
        "count_documents",
        lambda root: (_ for _ in ()).throw(source_import.ImportLimitError("too large")),
    )
    monkeypatch.setattr(source_import, "notion_is_installed", lambda: False)

    data = routes.onboarding_state().data

    assert data["sources"][0]["available"] is False
    assert "too large" in data["sources"][0]["detail"]


def test_browser_cannot_inject_an_unselected_local_path(tmp_path: Path) -> None:
    with pytest.raises(HTTPException, match="choose this folder") as caught:
        routes.onboarding_import({"sources": [{"type": "folder", "path": str(tmp_path)}]})
    assert caught.value.status_code == 409


def test_active_task_receipt_recovers_as_resumable_after_restart(ac_root: Path) -> None:
    routes._set_onboarding_task(stage="building", message="Building…")
    receipt = ac_root / ".onboarding-state.json"
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600

    with routes._onboarding_task_lock:
        routes._onboarding_task.update(stage="idle", message="", error="")
    restored = routes._load_onboarding_task()

    assert restored["stage"] == "failed"
    assert "resume" in restored["error"]


def test_onboarding_assets_are_bundled() -> None:
    script = routes.model_asset("onboarding.js")
    css = routes.model_asset("onboarding.css")
    assert b"Bring your history" in script.body
    assert b"Your original files will not be changed" in script.body
    assert b"onboarding/state" not in script.body  # relative capability-scoped URL
    assert b"selectionInitialized" in script.body
    assert b"selected=new Set(sources" not in script.body
    assert b".screen" in css.body
    assert script.media_type == "text/javascript"
    assert css.media_type == "text/css"


def test_unchanged_import_still_retries_structural_build(
    ac_root: Path, tmp_path: Path, monkeypatch
) -> None:
    builds: list[object] = []
    monkeypatch.setattr(
        source_import,
        "import_folder",
        lambda root, *, source_type: source_import.ImportResult(
            source_type=source_type,
            root=root,
            unchanged=1,
        ),
    )
    monkeypatch.setattr(
        source_import,
        "build_imported_model",
        lambda cfg: (
            builds.append(cfg),
            SimpleNamespace(status="complete", manifest={"degraded_stages": []}),
        )[1],
    )

    routes._run_onboarding_import([("folder", tmp_path)])

    assert len(builds) == 1
    assert routes._onboarding_task["stage"] == "complete"


def test_degraded_build_is_not_presented_as_ready(
    ac_root: Path, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        source_import,
        "import_folder",
        lambda root, *, source_type: source_import.ImportResult(
            source_type=source_type,
            root=root,
            imported=1,
            session_ids=["pending"],
        ),
    )
    monkeypatch.setattr(
        source_import,
        "build_imported_model",
        lambda cfg: SimpleNamespace(
            status="degraded",
            manifest={"degraded_stages": ["model_contract"]},
        ),
    )

    routes._run_onboarding_import([("folder", tmp_path)])

    assert routes._onboarding_task["stage"] == "failed"
    assert "model_contract" in routes._onboarding_task["error"]


def test_import_reservation_is_atomic_across_concurrent_requests(
    ac_root: Path, tmp_path: Path, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    callers_ready = threading.Barrier(2)
    release_background = threading.Event()

    def discover() -> list[Path]:
        callers_ready.wait(timeout=5)
        return [vault]

    monkeypatch.setattr(source_import, "discover_obsidian_vaults", discover)
    monkeypatch.setattr(
        routes,
        "_run_onboarding_import",
        lambda sources: release_background.wait(timeout=5),
    )

    def request() -> int:
        try:
            routes.onboarding_import({"sources": [{"type": "obsidian"}]})
        except HTTPException as exc:
            return exc.status_code
        return 200

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(request), pool.submit(request)]
            statuses = sorted(future.result() for future in futures)
    finally:
        release_background.set()

    assert statuses == [200, 409]

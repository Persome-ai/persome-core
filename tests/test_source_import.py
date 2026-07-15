from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from persome import source_import
from persome.session import store as session_store
from persome.store import fts
from persome.timeline import store as timeline_store


def test_discovers_open_obsidian_vault_first(tmp_path: Path) -> None:
    closed = tmp_path / "closed"
    active = tmp_path / "active"
    (closed / ".obsidian").mkdir(parents=True)
    (active / ".obsidian").mkdir(parents=True)
    registry = tmp_path / "Library" / "Application Support" / "obsidian" / "obsidian.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(
        json.dumps(
            {
                "vaults": {
                    "a": {"path": str(closed), "ts": 20},
                    "b": {"path": str(active), "ts": 10, "open": True},
                }
            }
        )
    )

    assert source_import.discover_obsidian_vaults(tmp_path) == [active, closed]


def test_notion_option_requires_installed_desktop_app(tmp_path: Path) -> None:
    (tmp_path / "Applications" / "Notion.app").mkdir(parents=True)
    assert source_import.notion_is_installed(tmp_path) is True


def test_onboarding_shows_only_detected_sources_and_builds_once(
    tmp_path: Path, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    local = tmp_path / "local"
    vault.mkdir()
    local.mkdir()
    seen_choices: list[str] = []
    imported: list[tuple[str, Path]] = []
    builds: list[object] = []

    class UI:
        def status(self, message: str) -> None:
            pass

        def choose_import_sources(self, choices: list[str]) -> list[str]:
            seen_choices.extend(choices)
            return choices

        def choose_folder(self, prompt: str) -> Path | None:
            assert "Notion" not in prompt
            return local

    monkeypatch.setattr(source_import, "discover_obsidian_vaults", lambda: [vault])
    monkeypatch.setattr(source_import, "notion_is_installed", lambda: False)

    def run_import(path: Path, *, source_type: str) -> source_import.ImportResult:
        imported.append((source_type, path))
        return source_import.ImportResult(
            source_type=source_type,
            root=path,
            imported=1,
            session_ids=[source_type],
        )

    monkeypatch.setattr(source_import, "import_folder", run_import)
    monkeypatch.setattr(
        source_import,
        "build_imported_model",
        lambda cfg: (builds.append(cfg), _complete_build())[1],
    )

    results = source_import.offer_data_import(UI(), object())

    assert seen_choices == ["Local folder", "Obsidian — vault"]
    assert imported == [("obsidian", vault), ("folder", local)]
    assert len(results) == 2
    assert len(builds) == 1


def test_onboarding_shows_notion_only_when_installed(tmp_path: Path, monkeypatch) -> None:
    choices_seen: list[str] = []

    class UI:
        def status(self, message: str) -> None:
            pass

        def choose_import_sources(self, choices: list[str]) -> list[str]:
            choices_seen.extend(choices)
            return []

        def choose_folder(self, prompt: str) -> Path | None:
            raise AssertionError("no source was selected")

    monkeypatch.setattr(source_import, "discover_obsidian_vaults", lambda: [])
    monkeypatch.setattr(source_import, "notion_is_installed", lambda: True)

    assert source_import.offer_data_import(UI(), object()) == []
    assert choices_seen == ["Local folder", "Notion export"]


def test_import_folder_is_read_only_private_and_idempotent(ac_root: Path, tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    note = vault / "Work" / "plan.md"
    note.parent.mkdir()
    original = "# Plan\n\nI am building a local-first personal model."
    note.write_text(original)
    (vault / ".obsidian" / "workspace.md").write_text("private config")
    (vault / "binary.pdf").write_bytes(b"not imported")

    first = source_import.import_folder(vault, source_type="obsidian")
    assert first.discovered == 1
    assert first.imported == 1
    assert len(first.session_ids) == 1
    assert note.read_text() == original

    with fts.cursor() as conn:
        session = session_store.get_by_id(conn, first.session_ids[0])
        assert session is not None
        assert session.status == "ended"
        blocks = timeline_store.query_range(conn, session.start_time, session.end_time, limit=10)
    assert len(blocks) == 1
    assert "Work/plan.md" in blocks[0].focus_excerpt
    assert original in blocks[0].focus_excerpt
    assert "private config" not in blocks[0].focus_excerpt

    second = source_import.import_folder(vault, source_type="obsidian")
    assert second.imported == 0
    assert second.unchanged == 1
    # The pending session is returned so a failed/interrupted build is resumed.
    assert second.session_ids == first.session_ids


def test_changed_note_gets_a_new_provenance_session(ac_root: Path, tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    note.write_text("first version")
    first = source_import.import_folder(vault, source_type="folder")

    note.write_text("second version")
    second = source_import.import_folder(vault, source_type="folder")

    assert second.imported == 1
    assert second.session_ids != first.session_ids


def test_refuses_to_import_persome_generated_state(ac_root: Path) -> None:
    with pytest.raises(ValueError, match="Persome data directory"):
        source_import.import_folder(ac_root, source_type="folder")


def _complete_build() -> SimpleNamespace:
    return SimpleNamespace(status="complete", manifest={"degraded_stages": []})


def test_refuses_source_that_contains_persome_state(ac_root: Path) -> None:
    with pytest.raises(ValueError, match="Persome data directory"):
        source_import.import_folder(ac_root.parent, source_type="folder")


def test_document_walk_prunes_hidden_directories(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    hidden = vault / ".git" / "objects"
    hidden.mkdir(parents=True)
    (vault / "visible.md").write_text("visible")
    (hidden / "private.md").write_text("hidden")
    real_walk = os.walk
    pruned: list[bool] = []

    def checked_walk(*args, **kwargs):
        for directory, names, files in real_walk(*args, **kwargs):
            is_root = Path(directory) == vault
            yield directory, names, files
            if is_root:
                pruned.append(".git" not in names)

    monkeypatch.setattr(source_import.os, "walk", checked_walk)

    assert source_import.count_documents(vault) == 1
    assert pruned == [True]


def test_document_count_is_cached_for_onboarding_polling(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    calls: list[Path] = []

    def documents(root: Path) -> list[Path]:
        calls.append(root)
        return [root / "one.md"]

    monkeypatch.setattr(source_import, "_documents", documents)

    assert source_import.count_documents(vault) == 1
    assert source_import.count_documents(vault) == 1
    assert calls == [vault]


@pytest.mark.parametrize(
    ("limit_name", "limit", "files", "message"),
    [
        ("_MAX_DOCUMENTS", 1, {"one.md": "1", "two.md": "2"}, "documents"),
        ("_MAX_TOTAL_BYTES", 3, {"one.md": "four"}, "MiB import limit"),
    ],
)
def test_import_enforces_aggregate_source_limits(
    tmp_path: Path,
    monkeypatch,
    limit_name: str,
    limit: int,
    files: dict[str, str],
    message: str,
) -> None:
    vault = tmp_path / limit_name
    vault.mkdir()
    for name, content in files.items():
        (vault / name).write_text(content)
    monkeypatch.setattr(source_import, limit_name, limit)

    with pytest.raises(source_import.ImportLimitError, match=message):
        source_import.count_documents(vault)


def test_import_enforces_aggregate_session_limit(
    ac_root: Path, tmp_path: Path, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "long.md").write_text("one two three four five")
    monkeypatch.setattr(source_import, "_CHUNK_CHARS", 5)
    monkeypatch.setattr(source_import, "_MAX_IMPORT_SESSIONS", 1)

    with pytest.raises(source_import.ImportLimitError, match="session import limit"):
        source_import.import_folder(vault)


def test_imported_session_windows_never_drift_into_the_future(
    ac_root: Path, tmp_path: Path, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "future.md"
    note.write_text("one two three four five")
    future = time.time() + 86_400
    os.utime(note, (future, future))
    monkeypatch.setattr(source_import, "_CHUNK_CHARS", 5)

    result = source_import.import_folder(vault)

    with fts.cursor() as conn:
        starts = [
            session_store.get_by_id(conn, session_id).start_time
            for session_id in result.session_ids
        ]
    assert starts == sorted(starts)
    assert starts[-1] <= source_import.datetime.now().astimezone()

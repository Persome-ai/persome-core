"""Personal-data deletion must cover canonical, projected, and exported state."""

from __future__ import annotations

from datetime import UTC, datetime

from persome import cli, paths
from persome.evomem.models import MemoryLayer, MemoryNode
from persome.evomem.store import NodeStore
from persome.store import entries as entries_mod
from persome.store import fts, schema_faces


def _seed_capture() -> None:
    with fts.cursor() as conn:
        fts.insert_capture(
            conn,
            id="synthetic-capture",
            timestamp="2026-07-10T08:00:00+00:00",
            app_name="TestApp",
            bundle_id="test.app",
            window_title="Synthetic",
            focused_role="AXTextArea",
            focused_value="synthetic text",
            visible_text="synthetic text",
            url="",
        )


def _seed_model() -> None:
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn,
            name="project-synthetic.md",
            description="Synthetic memory",
            tags=["synthetic"],
        )
        entry_id = entries_mod.append_entry(
            conn,
            name="project-synthetic.md",
            content="Synthetic personal-model fact.",
            tags=["synthetic"],
        )
        schema_faces.upsert_root(
            conn,
            signature="Synthetic root.",
            members=[entry_id],
            anchors=["self"],
        )
    NodeStore().save(
        MemoryNode(
            node_id=entry_id,
            content="Synthetic personal-model fact.",
            layer=MemoryLayer.L2_FACT,
            file_name="project-synthetic.md",
            valid_from="2026-07-10T08:00:00+00:00",
            gmt_created=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
        )
    )


def test_clean_captures_removes_files_and_index_rows(ac_root) -> None:
    _seed_capture()
    capture_file = paths.capture_buffer_dir() / "synthetic.json"
    capture_file.write_text("{}")

    assert cli._clean_captures() == (1, 1)
    assert not capture_file.exists()
    with fts.cursor() as conn:
        assert conn.execute("SELECT COUNT(*) FROM captures").fetchone()[0] == 0


def test_clean_memory_removes_canonical_model_exports_and_backups(ac_root) -> None:
    _seed_capture()
    _seed_model()
    paths.exports_dir().mkdir()
    (paths.exports_dir() / "model.json").write_text("{}")
    paths.backup_dir().mkdir()
    (paths.backup_dir() / "evo.db").write_text("synthetic")
    paths.model_build_manifest().write_text("{}")

    files, entries, model_rows, artifacts = cli._clean_memory()

    assert files == 1
    assert entries == 1
    assert model_rows >= 2
    assert artifacts == 3
    assert not paths.exports_dir().exists()
    assert not paths.backup_dir().exists()
    assert not paths.model_build_manifest().exists()
    with fts.cursor() as conn:
        assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM evo_nodes").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM schema_faces").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM captures").fetchone()[0] == 1


def test_clean_all_keeps_only_install_configuration_and_custom_skills(ac_root) -> None:
    _seed_capture()
    _seed_model()
    paths.config_file().write_text("[capture]\n")
    paths.env_file().write_text("ANTHROPIC_API_KEY=synthetic\n")
    (paths.root() / "venv").mkdir()
    paths.skills_dir().mkdir()
    (paths.skills_dir() / "custom.md").write_text("Synthetic skill.")
    (paths.root() / "chat-history").mkdir()
    (paths.root() / "chat-history" / "active.json").write_text("[]")
    paths.logs_dir().mkdir(exist_ok=True)
    (paths.logs_dir() / "daemon.log").write_text("synthetic")

    cli.clean_all(yes=True)

    assert paths.config_file().exists()
    assert paths.env_file().exists()
    assert (paths.root() / "venv").is_dir()
    assert (paths.skills_dir() / "custom.md").exists()
    for deleted in (
        paths.capture_buffer_dir(),
        paths.memory_dir(),
        paths.logs_dir(),
        paths.root() / "chat-history",
        paths.index_db(),
    ):
        assert not deleted.exists()

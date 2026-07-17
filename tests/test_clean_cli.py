"""Personal-data deletion must cover canonical, projected, and exported state."""

from __future__ import annotations

from datetime import UTC, datetime

from typer.testing import CliRunner

from persome import cli, paths
from persome.evomem.models import MemoryLayer, MemoryNode
from persome.evomem.store import NodeStore
from persome.store import entries as entries_mod
from persome.store import fts, memory_deltas, schema_faces
from persome.timeline import store as timeline_store


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


def test_clean_timeline_removes_block_claims_before_timeline_rows(ac_root) -> None:
    start = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    block = timeline_store.TimelineBlock(
        start_time=start,
        end_time=start.replace(minute=1),
        entries=["private timeline evidence"],
        apps_used=["PrivateApp"],
        capture_count=1,
    )
    with fts.cursor() as conn:
        timeline_store.insert(
            conn,
            block,
            source_capture_ids=["capture:2026-07-10T08-00-00p00-00"],
        )
        delta_id = memory_deltas.insert(
            conn,
            session_id="private-session",
            payload={},
            window_start=block.start_time,
            window_end=block.end_time,
            evidence_ids=[block.id],
        )

    assert cli._clean_timeline() == 1
    with fts.cursor() as conn:
        assert conn.execute("SELECT COUNT(*) FROM timeline_blocks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM timeline_block_sources").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM memory_delta_evidence_claims").fetchone()[0] == 0
        assert (
            conn.execute("SELECT COUNT(*) FROM memory_deltas WHERE id=?", (delta_id,)).fetchone()[0]
            == 1
        )


def test_clean_memory_removes_delta_claim_children(ac_root) -> None:
    with fts.cursor() as conn:
        memory_deltas.insert(
            conn,
            session_id="private-session",
            payload={},
            evidence_ids=["tlb-20260710-0800-private"],
        )

    cli._clean_memory()

    with fts.cursor() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_delta_evidence_claims").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM memory_deltas").fetchone()[0] == 0


def test_clean_memory_removes_canonical_model_exports_and_backups(ac_root, monkeypatch) -> None:
    _seed_capture()
    _seed_model()
    paths.exports_dir().mkdir()
    (paths.exports_dir() / "model.json").write_text("{}")
    paths.backup_dir().mkdir()
    (paths.backup_dir() / "evo.db").write_text("synthetic")
    paths.human_file().write_text("# HUMAN.md\n")
    paths.model_build_manifest().write_text("{}")
    paths.model_build_stage_receipt().write_text("{}")

    real_remove = cli._remove_path
    removed_inside_gate: list[bool] = []

    def guarded_remove(path):  # noqa: ANN001, ANN202
        removed_inside_gate.append(fts._in_exclusive_maintenance())  # noqa: SLF001
        return real_remove(path)

    monkeypatch.setattr(cli, "_remove_path", guarded_remove)
    files, entries, model_rows, artifacts = cli._clean_memory()

    assert files == 1
    assert entries == 1
    assert model_rows >= 2
    assert artifacts == 5
    assert removed_inside_gate and all(removed_inside_gate)
    assert not paths.exports_dir().exists()
    assert not paths.backup_dir().exists()
    assert not paths.human_file().exists()
    assert not paths.model_build_manifest().exists()
    assert not paths.model_build_stage_receipt().exists()
    with fts.cursor() as conn:
        assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM evo_nodes").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM schema_faces").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM captures").fetchone()[0] == 1


def test_clean_all_keeps_only_install_configuration(ac_root, monkeypatch) -> None:
    _seed_capture()
    _seed_model()
    paths.config_file().write_text("[capture]\n")
    paths.env_file().write_text("PERSOME_LLM_API_KEY=synthetic\n")
    paths.human_file().write_text("# HUMAN.md\n")
    paths.model_build_stage_receipt().write_text("{}")
    (paths.root() / "venv").mkdir()
    # Legacy Chat-era data from an older install: a full wipe must still purge it.
    (paths.root() / "chat-history").mkdir()
    (paths.root() / "chat-history" / "active.json").write_text("[]")
    (paths.root() / "skills").mkdir()
    (paths.root() / "skills" / "custom.md").write_text("Synthetic legacy skill.")
    paths.logs_dir().mkdir(exist_ok=True)
    (paths.logs_dir() / "daemon.log").write_text("synthetic")

    real_remove = cli._remove_path
    removed_inside_gate: list[bool] = []

    def guarded_remove(path):  # noqa: ANN001, ANN202
        removed_inside_gate.append(fts._in_exclusive_maintenance())  # noqa: SLF001
        return real_remove(path)

    monkeypatch.setattr(cli, "_remove_path", guarded_remove)
    cli.clean_all(yes=True)

    assert paths.config_file().exists()
    assert paths.env_file().exists()
    assert (paths.root() / "venv").is_dir()
    assert removed_inside_gate and all(removed_inside_gate)
    for deleted in (
        paths.capture_buffer_dir(),
        paths.memory_dir(),
        paths.logs_dir(),
        paths.human_file(),
        paths.model_build_stage_receipt(),
        paths.root() / "chat-history",
        paths.root() / "skills",
        paths.index_db(),
    ):
        assert not deleted.exists()


def test_clean_refuses_to_race_a_running_daemon(ac_root, monkeypatch) -> None:
    capture_file = paths.capture_buffer_dir() / "must-survive.json"
    capture_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cli, "_read_pid", lambda: 4242)

    result = CliRunner().invoke(cli.app, ["clean", "all", "--yes"])

    assert result.exit_code == 1
    assert "Refusing to clean" in result.output
    assert capture_file.exists()


def test_clean_running_guard_precedes_database_or_integrity_work(ac_root, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_read_pid", lambda: 4242)
    monkeypatch.setattr(
        cli.fts,
        "cursor",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("DB was opened")),
    )
    monkeypatch.setattr(
        cli.integrity,
        "check_and_recover",
        lambda: (_ for _ in ()).throw(AssertionError("integrity work ran")),
    )

    result = CliRunner().invoke(cli.app, ["clean", "memory", "--yes"])

    assert result.exit_code == 1
    assert "Refusing to clean" in result.output

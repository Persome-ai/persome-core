from __future__ import annotations

import errno
import fcntl
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from persome import paths
from persome.model import build as model_build
from persome.model import human


def _manifest(*, build_id: str | None = "build-1", status: str = "complete") -> dict[str, Any]:
    return {
        "build_id": build_id,
        "status": status,
        "started_at": "2026-07-13T12:00:00Z" if build_id else None,
        "completed_at": "2026-07-13T12:00:01Z" if build_id else None,
        "degraded_stages": [],
    }


def _schema_item(
    item_id: str,
    signature: str,
    *,
    observations: int,
    confidence: float = 0.8,
) -> dict[str, Any]:
    return {
        "id": item_id,
        "signature": signature,
        "observations": observations,
        "confidence": confidence,
    }


def _snapshot(
    *,
    build_id: str = "build-1",
    generated_at: str = "2026-07-13T12:00:01Z",
    root: dict[str, Any] | None = None,
    faces: list[dict[str, Any]] | None = None,
    volumes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    face_items = faces or []
    volume_items = volumes or []
    root_item = root
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "build": _manifest(build_id=build_id),
        "points": [
            {
                "id": "point-private-id",
                "content": "POINT_PRIVATE_CONTENT_DO_NOT_RENDER",
                "path": "/Users/alice/private/point.md",
            }
        ],
        "lines": [],
        "faces": face_items,
        "volumes": volume_items,
        "root": root_item,
        "receipts": [
            {
                "id": "receipt-private-id",
                "quote": "RECEIPT_PRIVATE_QUOTE_DO_NOT_RENDER",
                "path": "/Users/alice/private/evidence.jsonl",
            }
        ],
        "stats": {
            "points": 1,
            "active_points": 1,
            "evolution_lines": 2,
            "relation_lines": 3,
            "faces": len(face_items),
            "volumes": len(volume_items),
            "roots": int(root_item is not None),
            "receipts": 1,
            "redactions": {},
        },
    }


def test_render_is_deterministic_safe_and_keeps_only_unknown_handles() -> None:
    known_volume = _schema_item(
        "volume-known",
        "Known Domain",
        observations=12,
        confidence=0.91,
    )
    snapshot = _snapshot(
        root={
            "id": "root-1",
            "signature": (
                "Builds ![tracking](https://invalid.example/pixel) and "
                "[click me](https://invalid.example) <script>alert(1)</script> "
                "across ⟨Known Domain⟩ while retaining ⟨Unrecognized Lens⟩."
            ),
            "members": ["volume-known"],
        },
        faces=[_schema_item("face-1", "Careful execution", observations=7)],
        volumes=[known_volume],
    )

    first = human.render_human_markdown(snapshot)
    second = human.render_human_markdown(snapshot)

    assert first == second
    assert first.startswith(
        "---\n"
        f"human_schema_version: {human.HUMAN_SCHEMA_VERSION}\n"
        f"renderer_version: {human.HUMAN_RENDERER_VERSION}\n"
        "model_schema_version: 1\n"
    )
    assert 'build_id: "build-1"' in first
    assert 'build_status: "complete"' in first
    assert 'root_id: "root-1"' in first
    assert 'visibility: "owner-only"' in first
    assert "redacted: false" in first
    assert 'projection: "persome-model"' in first

    portrait = first.split("## Persome's portrait of me", 1)[1].split("## Stable patterns", 1)[0]
    assert "⟨Known Domain⟩" not in portrait
    assert "⟨Unrecognized Lens⟩" in portrait
    assert "![tracking](" not in portrait
    assert "[click me](" not in portrait
    assert "<script>" not in portrait
    assert r"!\[tracking\](https://invalid.example/pixel)" in portrait
    assert r"\[click me\](https://invalid.example)" in portrait
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in portrait
    assert "- Known Domain _(evidence 12; confidence 0.91)_" in first

    assert "POINT_PRIVATE_CONTENT_DO_NOT_RENDER" not in first
    assert "RECEIPT_PRIVATE_QUOTE_DO_NOT_RENDER" not in first
    assert "point-private-id" not in first
    assert "receipt-private-id" not in first
    assert "/Users/alice/private" not in first


def test_render_sorts_and_caps_faces_and_root_member_volumes() -> None:
    faces = [
        _schema_item(f"face-{index:02d}", f"Face rank {index:02d}", observations=index)
        for index in range(10)
    ]
    volumes = [
        _schema_item(
            f"volume-{index:02d}",
            f"Volume rank {index:02d}",
            observations=index,
        )
        for index in range(10)
    ]
    snapshot = _snapshot(
        root={
            "id": "root-1",
            "signature": "A compact portrait without embedded handles.",
            "members": [item["id"] for item in volumes],
        },
        faces=list(reversed(faces)),
        volumes=list(reversed(volumes)),
    )

    rendered = human.render_human_markdown(snapshot)
    face_section = rendered.split("## Stable patterns", 1)[1].split("## Cross-domain patterns", 1)[
        0
    ]
    volume_section = rendered.split("## Cross-domain patterns", 1)[1].split(
        "## Model provenance", 1
    )[0]

    assert face_section.index("Face rank 09") < face_section.index("Face rank 08")
    assert volume_section.index("Volume rank 09") < volume_section.index("Volume rank 08")
    assert "Face rank 02" in face_section
    assert "Face rank 01" not in face_section
    assert "Volume rank 02" in volume_section
    assert "Volume rank 01" not in volume_section
    assert "> Showing 8 of 10 Faces." in face_section
    assert "> Showing 8 of 10 Volumes." in volume_section


def test_materialize_is_owner_only_and_atomically_replaces_managed_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "HUMAN.md"
    initial = _snapshot(generated_at="2026-07-13T12:00:01Z")
    updated = _snapshot(generated_at="2026-07-13T12:00:02Z")
    human.materialize_human_markdown(initial, out_path=target)

    with target.open("r", encoding="utf-8") as previous_handle:
        previous_inode = os.fstat(previous_handle.fileno()).st_ino
        original_exchange = human.paths.atomic_exchange
        observations: list[tuple[bool, bool]] = []

        def observed_exchange(first: Path, second: Path) -> None:
            before = second.exists()
            original_exchange(first, second)
            observations.append((before, second.exists()))

        monkeypatch.setattr(human.paths, "atomic_exchange", observed_exchange)
        human.materialize_human_markdown(updated, out_path=target)

        assert target.stat().st_ino != previous_inode
        assert "2026-07-13T12:00:01Z" in previous_handle.read()

    assert observations == [(True, True)]
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert "2026-07-13T12:00:02Z" in target.read_text(encoding="utf-8")
    assert list(tmp_path.glob(".HUMAN.md.*")) == [tmp_path / ".HUMAN.md.lock"]
    assert stat.S_IMODE((tmp_path / ".HUMAN.md.lock").stat().st_mode) == 0o600


def test_materialize_preserves_unrecognized_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "HUMAN.md"
    unknown = '# My hand-written HUMAN.md\n\n---\nprojection: "persome-model"\n---\n'
    target.write_text(unknown, encoding="utf-8")
    target.chmod(0o640)
    before = target.stat()

    with pytest.raises(human.HumanMarkdownConflict, match="refusing to replace"):
        human.materialize_human_markdown(_snapshot(), out_path=target)

    after = target.stat()
    assert target.read_text(encoding="utf-8") == unknown
    assert after.st_ino == before.st_ino
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode)


def test_materialize_preserves_symlink_and_its_target(tmp_path: Path) -> None:
    victim = tmp_path / "user-notes.md"
    victim.write_text("do not replace me\n", encoding="utf-8")
    target = tmp_path / "HUMAN.md"
    target.symlink_to(victim)

    with pytest.raises(human.HumanMarkdownConflict, match="non-regular"):
        human.materialize_human_markdown(_snapshot(), out_path=target)

    assert target.is_symlink()
    assert target.readlink() == victim
    assert victim.read_text(encoding="utf-8") == "do not replace me\n"


def test_materialize_preserves_hard_link_and_its_target(tmp_path: Path) -> None:
    victim = tmp_path / "user-notes.md"
    victim.write_text("do not replace me\n", encoding="utf-8")
    target = tmp_path / "HUMAN.md"
    os.link(victim, target)
    before = victim.stat()

    with pytest.raises(human.HumanMarkdownConflict, match="non-regular"):
        human.materialize_human_markdown(_snapshot(), out_path=target)

    assert target.stat().st_ino == before.st_ino
    assert victim.stat().st_nlink == 2
    assert victim.read_text(encoding="utf-8") == "do not replace me\n"


@pytest.mark.parametrize("operation", ["refresh", "remove"])
def test_managed_operation_preserves_unknown_file_swapped_in_after_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    target = tmp_path / "HUMAN.md"
    human.materialize_human_markdown(_snapshot(), out_path=target)
    replacement = "# Edited concurrently\n\nThis file belongs to the user.\n"
    swapped = False

    def swap_target() -> None:
        nonlocal swapped
        editor_temp = target.with_name("editor-HUMAN.md")
        editor_temp.write_text(replacement, encoding="utf-8")
        editor_temp.chmod(0o640)
        os.replace(editor_temp, target)
        swapped = True

    if operation == "refresh":
        original_exchange = human.paths.atomic_exchange

        def swap_then_exchange(first: Path, second: Path) -> None:
            if not swapped and second == target:
                swap_target()
            original_exchange(first, second)

        monkeypatch.setattr(human.paths, "atomic_exchange", swap_then_exchange)
    else:
        original_rename = human.paths.atomic_rename_noreplace

        def swap_then_rename(first: Path, second: Path) -> None:
            if not swapped and first == target:
                swap_target()
            original_rename(first, second)

        monkeypatch.setattr(human.paths, "atomic_rename_noreplace", swap_then_rename)

    with pytest.raises(human.HumanMarkdownConflict, match="refusing to replace|changed"):
        if operation == "refresh":
            human.materialize_human_markdown(
                _snapshot(generated_at="2026-07-13T12:00:02Z"),
                out_path=target,
            )
        else:
            human.remove_managed_human_markdown(target)

    assert target.read_text(encoding="utf-8") == replacement
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_materialize_fails_open_when_another_projection_holds_the_lock(tmp_path: Path) -> None:
    target = tmp_path / "HUMAN.md"
    lock_path = tmp_path / ".HUMAN.md.lock"
    lock_path.touch(mode=0o600)

    with lock_path.open("a+b") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(human.HumanMarkdownConflict, match="already active"):
            human.materialize_human_markdown(_snapshot(), out_path=target)

    assert not target.exists()


def test_materialize_preserves_managed_human_hardlink_backup(tmp_path: Path) -> None:
    target = tmp_path / "HUMAN.md"
    human.materialize_human_markdown(_snapshot(), out_path=target)
    before = target.read_bytes()
    backup = tmp_path / ".HUMAN.md.my-backup"
    os.link(target, backup)
    assert target.stat().st_nlink == 2

    with pytest.raises(human.HumanMarkdownConflict, match="non-regular"):
        human.materialize_human_markdown(
            _snapshot(generated_at="2026-07-13T12:00:02Z"),
            out_path=target,
        )

    assert backup.exists()
    assert target.stat().st_ino == backup.stat().st_ino
    assert target.stat().st_nlink == 2
    assert target.read_bytes() == before


def test_materialize_preserves_unknown_human_hardlink_backup(
    tmp_path: Path,
) -> None:
    target = tmp_path / "HUMAN.md"
    unknown = "# Concurrent user file\n"
    target.write_text(unknown, encoding="utf-8")
    target.chmod(0o640)
    backup = tmp_path / ".HUMAN.md.my-backup"
    os.link(target, backup)

    with pytest.raises(human.HumanMarkdownConflict, match="refusing to replace"):
        human.materialize_human_markdown(_snapshot(), out_path=target)

    assert backup.exists()
    assert target.read_text(encoding="utf-8") == unknown
    assert target.stat().st_ino == backup.stat().st_ino
    assert target.stat().st_nlink == 2
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_materialize_recovers_journaled_initial_link_crash(tmp_path: Path) -> None:
    target = tmp_path / "HUMAN.md"
    initial = _snapshot(generated_at="2026-07-13T12:00:01Z")
    with human._human_lock(target) as handle:
        stage, _transaction = human._stage_publish_transaction(
            target,
            human.render_human_markdown(initial),
            None,
            handle,
        )
        os.link(stage, target, follow_symlinks=False)

    assert target.stat().st_nlink == 2
    assert stage.exists()

    human.materialize_human_markdown(
        _snapshot(generated_at="2026-07-13T12:00:02Z"),
        out_path=target,
    )

    assert not stage.exists()
    assert target.stat().st_nlink == 1
    assert "2026-07-13T12:00:02Z" in target.read_text(encoding="utf-8")
    assert (tmp_path / ".HUMAN.md.lock").read_bytes() == b""


def test_materialize_recovers_journaled_post_exchange_crash(tmp_path: Path) -> None:
    target = tmp_path / "HUMAN.md"
    human.materialize_human_markdown(_snapshot(), out_path=target)
    crashed = _snapshot(generated_at="2026-07-13T12:00:02Z")
    with human._human_lock(target) as handle:
        expected = human._managed_metadata(target)
        assert expected is not None
        stage, _transaction = human._stage_publish_transaction(
            target,
            human.render_human_markdown(crashed),
            expected,
            handle,
        )
        paths.atomic_exchange(stage, target)

    assert stage.exists()
    assert "2026-07-13T12:00:02Z" in target.read_text(encoding="utf-8")

    human.materialize_human_markdown(
        _snapshot(generated_at="2026-07-13T12:00:03Z"),
        out_path=target,
    )

    assert not stage.exists()
    assert "2026-07-13T12:00:03Z" in target.read_text(encoding="utf-8")
    assert list(tmp_path.glob(".HUMAN.md.persome-stage.*")) == []


def test_materialize_ignores_truncated_transaction_tail(tmp_path: Path) -> None:
    target = tmp_path / "HUMAN.md"
    stage = human._new_stage_path(target)
    transaction = human._HumanTransaction("publish", stage.name, None, None)

    with human._human_lock(target) as handle:
        human._write_transaction(handle, transaction)
        handle.seek(0, os.SEEK_END)
        handle.write(b'{"candidate": [1,')
        handle.flush()
        os.fsync(handle.fileno())

    human.materialize_human_markdown(_snapshot(), out_path=target)

    assert target.exists()
    assert not stage.exists()
    assert (tmp_path / ".HUMAN.md.lock").read_bytes() == b""


def test_materialize_fails_closed_when_atomic_exchange_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "HUMAN.md"
    human.materialize_human_markdown(_snapshot(), out_path=target)
    before = target.stat()
    before_content = target.read_bytes()

    def unsupported(_first: Path, _second: Path) -> None:
        raise OSError(errno.ENOTSUP, "unsupported")

    monkeypatch.setattr(human.paths, "atomic_exchange", unsupported)
    with pytest.raises(human.HumanMarkdownConflict, match="cannot atomically replace"):
        human.materialize_human_markdown(
            _snapshot(generated_at="2026-07-13T12:00:02Z"),
            out_path=target,
        )

    after = target.stat()
    assert after.st_ino == before.st_ino
    assert target.read_bytes() == before_content
    assert list(tmp_path.glob(".HUMAN.md.persome-stage.*")) == []
    assert (tmp_path / ".HUMAN.md.lock").read_bytes() == b""


def test_sync_writes_truthful_cold_start_placeholder(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest(build_id=None, status="not_built")

    @contextmanager
    def generation():
        yield manifest

    monkeypatch.setattr("persome.model.build.live_model_generation", generation)

    target = human.sync_live_human_markdown()
    rendered = target.read_text(encoding="utf-8")

    assert target == ac_root / "HUMAN.md"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert "build_id: null" in rendered
    assert 'build_status: "not_built"' in rendered
    assert "root_id: null" in rendered
    assert "has not formed a verified Root yet" in rendered
    assert "No identity portrait is being" in rendered
    assert "## Stable patterns" not in rendered
    assert "## Cross-domain patterns" not in rendered


def test_sync_is_noop_when_build_schema_and_renderer_match(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot(root={"id": "root-1", "signature": "Stable portrait", "members": []})
    target = human.materialize_human_markdown(snapshot)
    before = target.stat()
    before_content = target.read_bytes()

    @contextmanager
    def generation():
        yield snapshot["build"]

    monkeypatch.setattr("persome.model.build.live_model_generation", generation)

    def fail_if_rebuilt(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("matching HUMAN.md must not rebuild the live snapshot")

    monkeypatch.setattr("persome.model.build._build_live_snapshot_from_manifest", fail_if_rebuilt)

    assert human.sync_live_human_markdown() == ac_root / "HUMAN.md"
    after = target.stat()
    assert after.st_ino == before.st_ino
    assert after.st_mtime_ns == before.st_mtime_ns
    assert target.read_bytes() == before_content


def test_sync_holds_shared_generation_lock_through_publication(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot(root={"id": "root-1", "signature": "Stable portrait", "members": []})
    original_materialize = human.materialize_human_markdown

    @contextmanager
    def locked_generation():
        with model_build._shared_build_lock_if_available() as acquired:
            assert acquired is True
            yield snapshot["build"]

    monkeypatch.setattr(model_build, "live_model_generation", locked_generation)
    monkeypatch.setattr(
        model_build,
        "_build_live_snapshot_from_manifest",
        lambda _conn, _manifest, *, redact: snapshot,
    )

    def checked_materialize(*args: Any, **kwargs: Any) -> Path:
        with (
            pytest.raises(model_build.ModelBuildBusy),
            model_build.ModelBuildCoordinator().acquire(wait_seconds=0),
        ):
            pass
        return original_materialize(*args, **kwargs)

    monkeypatch.setattr(human, "materialize_human_markdown", checked_materialize)

    assert human.sync_live_human_markdown() == ac_root / "HUMAN.md"
    assert 'build_id: "build-1"' in paths.human_file().read_text(encoding="utf-8")


def test_sync_does_not_publish_placeholder_during_active_build(ac_root: Path) -> None:
    with (
        model_build.ModelBuildCoordinator().acquire(wait_seconds=0),
        pytest.raises(human.HumanMarkdownConflict, match="active model build"),
    ):
        human.sync_live_human_markdown()

    assert not paths.human_file().exists()


def test_update_receipt_defers_all_canonical_publication_without_artifacts(
    ac_root: Path,
) -> None:
    paths.update_state_file().write_text("{}\n", encoding="utf-8")

    with pytest.raises(human.HumanMarkdownDeferred, match="update to commit"):
        human.materialize_human_markdown(_snapshot())
    with pytest.raises(human.HumanMarkdownDeferred, match="update to commit"):
        human.sync_live_human_markdown()

    assert not paths.human_file().exists()
    assert list(ac_root.glob(".HUMAN.md.*")) == []

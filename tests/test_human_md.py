from __future__ import annotations

import fcntl
import os
import stat
from pathlib import Path
from typing import Any

import pytest

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
) -> None:
    target = tmp_path / "HUMAN.md"
    initial = _snapshot(generated_at="2026-07-13T12:00:01Z")
    updated = _snapshot(generated_at="2026-07-13T12:00:02Z")
    human.materialize_human_markdown(initial, out_path=target)

    with target.open("r", encoding="utf-8") as previous_handle:
        previous_inode = os.fstat(previous_handle.fileno()).st_ino
        human.materialize_human_markdown(updated, out_path=target)

        assert target.stat().st_ino != previous_inode
        assert "2026-07-13T12:00:01Z" in previous_handle.read()

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
    original_move = human._move_existing_target
    replacement = "# Edited concurrently\n\nThis file belongs to the user.\n"

    def swap_then_move(path: Path) -> Path:
        editor_temp = path.with_name("editor-HUMAN.md")
        editor_temp.write_text(replacement, encoding="utf-8")
        editor_temp.chmod(0o640)
        os.replace(editor_temp, path)
        return original_move(path)

    monkeypatch.setattr(human, "_move_existing_target", swap_then_move)

    with pytest.raises(human.HumanMarkdownConflict, match="changed during refresh"):
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


def test_materialize_recovers_same_inode_staged_link_after_crash(tmp_path: Path) -> None:
    target = tmp_path / "HUMAN.md"
    human.materialize_human_markdown(_snapshot(), out_path=target)
    stranded = tmp_path / ".HUMAN.md.stranded"
    os.link(target, stranded)
    assert target.stat().st_nlink == 2

    human.materialize_human_markdown(
        _snapshot(generated_at="2026-07-13T12:00:02Z"),
        out_path=target,
    )

    assert not stranded.exists()
    assert target.stat().st_nlink == 1
    assert "2026-07-13T12:00:02Z" in target.read_text(encoding="utf-8")


def test_materialize_recovers_unknown_restore_link_but_still_preserves_it(
    tmp_path: Path,
) -> None:
    target = tmp_path / "HUMAN.md"
    unknown = "# Concurrent user file\n"
    target.write_text(unknown, encoding="utf-8")
    target.chmod(0o640)
    stranded = tmp_path / ".HUMAN.md.replaced.stranded"
    os.link(target, stranded)

    with pytest.raises(human.HumanMarkdownConflict, match="refusing to replace"):
        human.materialize_human_markdown(_snapshot(), out_path=target)

    assert not stranded.exists()
    assert target.read_text(encoding="utf-8") == unknown
    assert target.stat().st_nlink == 1
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_sync_writes_truthful_cold_start_placeholder(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest(build_id=None, status="not_built")
    monkeypatch.setattr("persome.model.build.load_live_manifest", lambda: manifest)

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
    monkeypatch.setattr(
        "persome.model.build.load_live_manifest",
        lambda: snapshot["build"],
    )

    def fail_if_rebuilt(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("matching HUMAN.md must not rebuild the live snapshot")

    monkeypatch.setattr("persome.model.build.build_live_snapshot", fail_if_rebuilt)

    assert human.sync_live_human_markdown() == ac_root / "HUMAN.md"
    after = target.stat()
    assert after.st_ino == before.st_ino
    assert after.st_mtime_ns == before.st_mtime_ns
    assert target.read_bytes() == before_content

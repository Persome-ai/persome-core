"Tests for test projector."

from __future__ import annotations

import dataclasses
from datetime import datetime
from pathlib import Path

import pytest

from persome import paths
from persome.evomem import backfill
from persome.evomem.models import MemoryLayer, MemoryNode, MemoryStatus
from persome.evomem.store import NodeStore
from persome.store import entries, fts, projector
from persome.store import files as files_mod
from persome.writer.schema_miner_stage import render_schema_body


def _node(node_id: str, content: str, *, ts: str, **kw) -> MemoryNode:
    d = datetime.fromisoformat(ts)
    kw.setdefault("file_name", "project-rt.md")
    kw.setdefault("layer", MemoryLayer.L2_FACT)
    kw.setdefault("valid_from", ts)
    return MemoryNode(node_id=node_id, content=content, memory_at=d, gmt_created=d, **kw)


def _all_op_shape_nodes() -> list[MemoryNode]:
    schema_body = render_schema_body(
        central_proposition="\u504f\u597d\u6781\u7b80\u5de5\u5177",
        supporting_summary="\u591a\u6b21\u9009\u62e9 uv/ruff",
        expected_inferences=[
            "\u4f1a\u62d2\u7edd\u91cd\u578b\u6846\u67b6",
            "\u8bc4\u4f30\u5de5\u5177\u5148\u770b\u4f9d\u8d56\u4f53\u79ef",
        ],
    )
    return [
        _node("20260601-1000-add001", "plain fact", ts="2026-06-01T10:00", tags="alpha beta"),
        _node(
            "20260601-1001-sup0ld",
            "v1 fact",
            ts="2026-06-01T10:01",
            superseded_by=["20260601-1002-sup4ew"],
            is_latest=False,
            status=MemoryStatus.SHADOW,
            valid_until="2026-06-01T10:02",
        ),
        _node(
            "20260601-1002-sup4ew",
            "v2 fact\n<!-- supersedes: 20260601-1001-sup0ld; reason: updated -->",
            ts="2026-06-01T10:02",
            supersedes=["20260601-1001-sup0ld"],
        ),
        _node(
            "20260601-1003-upd0ld",
            "rough fact",
            ts="2026-06-01T10:03",
            is_latest=False,
            status=MemoryStatus.SHADOW,
        ),
        _node(
            "20260601-1004-upd4ew",
            "sharpened fact",
            ts="2026-06-01T10:04",
            refined_from="20260601-1003-upd0ld",
        ),
        _node(
            "20260601-1005-del0rp",
            "stale fact",
            ts="2026-06-01T10:05",
            is_latest=False,
            status=MemoryStatus.SHADOW,
            valid_until="2026-06-01T12:34",
        ),
        _node(
            "20260601-1006-abss01",
            "part a",
            ts="2026-06-01T10:06",
            is_latest=False,
            status=MemoryStatus.SHADOW,
        ),
        _node(
            "20260601-1007-abss02",
            "part b",
            ts="2026-06-01T10:07",
            is_latest=False,
            status=MemoryStatus.SHADOW,
        ),
        _node(
            "20260601-1008-abssyn",
            "synthesis",
            ts="2026-06-01T10:08",
            abstracted_from=["20260601-1006-abss01", "20260601-1007-abss02"],
        ),
        _node(
            "20260601-1009-met001",
            "meta fact",
            ts="2026-06-01T10:09",
            confidence="high",
            conflicted=True,
            occurred_at="2026-06-01T08:00",
        ),
        _node(
            "20260601-1010-lay001",
            "knowledge in a project file",
            ts="2026-06-01T10:10",
            layer=MemoryLayer.L5_KNOWLEDGE,
        ),
        _node(
            "20260601-1011-ref5hd",
            "refined then deleted",
            ts="2026-06-01T10:11",
            refined_from="20260601-1004-upd4ew",
            is_latest=False,
            status=MemoryStatus.SHADOW,
        ),
        _node(
            "20260601-1012-arc001",
            "archived fact",
            ts="2026-06-01T10:12",
            is_latest=False,
            status=MemoryStatus.ARCHIVED,
        ),
        _node(
            "20260601-1013-sch001",
            schema_body,
            ts="2026-06-01T10:13",
            file_name="schema-rt.md",
            layer=MemoryLayer.L6_SCHEMA,
            tags="schema stable",
            schema_summary="\u591a\u6b21\u9009\u62e9 uv/ruff",
            schema_inferences=[
                "\u4f1a\u62d2\u7edd\u91cd\u578b\u6846\u67b6",
                "\u8bc4\u4f30\u5de5\u5177\u5148\u770b\u4f9d\u8d56\u4f53\u79ef",
            ],
            schema_confidence=0.72,
        ),
    ]


def _parse_out_dir(out: Path) -> list[tuple[str, list[files_mod.ParsedEntry]]]:
    return [(p.name, files_mod.read_file(p).entries) for p in sorted(out.iterdir())]


def test_round_trip_all_op_shapes(ac_root: Path, tmp_path: Path) -> None:
    originals = _all_op_shape_nodes()
    store = NodeStore()
    for n in originals:
        store.save(n)

    out = tmp_path / "proj"
    with fts.cursor() as conn:
        report = projector.project_all(conn, out_dir=out)
    assert sorted(report.files) == ["project-rt.md", "schema-rt.md"]
    assert report.nodes == len(originals)

    rebuilt = {n.node_id: n for n in projector.rebuild_nodes_from_projection(_parse_out_dir(out))}
    assert set(rebuilt) == {n.node_id for n in originals}
    for orig in originals:
        got = rebuilt[orig.node_id]
        assert dataclasses.asdict(got) == dataclasses.asdict(orig), orig.node_id


def test_round_trip_non_default_scope(ac_root: Path, tmp_path: Path) -> None:
    node = _node(
        "20260601-1100-scp001",
        "scoped fact",
        ts="2026-06-01T11:00",
        user_id="u1",
        agent_id="a1",
    )
    text = projector.render_projection("project-rt.md", [node])
    assert "#scope:u1/a1" in text
    f = tmp_path / "project-rt.md"
    f.write_text(text)
    (got,) = projector.rebuild_nodes_from_projection(
        [("project-rt.md", files_mod.read_file(f).entries)]
    )
    assert dataclasses.asdict(got) == dataclasses.asdict(node)


def test_project_all_refuses_live_memory_dir(ac_root: Path) -> None:
    with fts.cursor() as conn, pytest.raises(ValueError, match="memory"):
        projector.project_all(conn, out_dir=paths.memory_dir())


def test_projection_idempotent(ac_root: Path, tmp_path: Path) -> None:
    store = NodeStore()
    for n in _all_op_shape_nodes():
        store.save(n)
    out = tmp_path / "proj"
    with fts.cursor() as conn:
        projector.project_all(conn, out_dir=out)
        first = {p.name: p.read_text() for p in out.iterdir()}
        projector.project_all(conn, out_dir=out)
        second = {p.name: p.read_text() for p in out.iterdir()}
    assert first == second


def test_unrouted_nodes_skipped_and_counted(ac_root: Path, tmp_path: Path) -> None:
    store = NodeStore()
    store.save(_node("20260601-1200-unr001", "unrouted", ts="2026-06-01T12:00", file_name=""))
    with fts.cursor() as conn:
        report = projector.project_all(conn, out_dir=tmp_path / "proj")
    assert report.files == [] and report.skipped_unrouted == 1


@pytest.fixture
def _deterministic_ids(monkeypatch: pytest.MonkeyPatch):
    counter = iter(range(1, 10_000))

    def fake_make_id(timestamp: str) -> str:
        compact = timestamp.replace("-", "").replace(":", "").replace("T", "-")[:13]
        return f"{compact}-{next(counter):06x}"

    monkeypatch.setattr(entries, "make_id", fake_make_id)


def test_projection_byte_identical_to_real_markdown(
    ac_root: Path, tmp_path: Path, _deterministic_ids
) -> None:
    with fts.cursor() as conn:
        entries.create_file(conn, name="project-demo.md", description="demo", tags=["proj"])
        e1 = entries.append_entry(conn, name="project-demo.md", content="v1 fact", tags=["alpha"])
        entries.supersede_entry(
            conn,
            name="project-demo.md",
            old_entry_id=e1,
            new_content="v2 fact",
            reason="updated",
            tags=["alpha"],
        )
        e3 = entries.append_entry(conn, name="project-demo.md", content="rough", tags=[])
        entries.supersede_entry(
            conn,
            name="project-demo.md",
            old_entry_id=e3,
            new_content="sharpened",
            reason="refined",
            tags=["alpha"],
            refined_from=e3,
            confidence="high",
            conflicted=True,
            occurred_at="2026-06-01T10:00",
        )
        entries.create_file(conn, name="schema-demo.md", description="schema", tags=["schema"])
        entries.append_entry(
            conn,
            name="schema-demo.md",
            content=render_schema_body(
                central_proposition="\u504f\u597d\u6781\u7b80",
                supporting_summary="\u591a\u6b21\u9009\u62e9 uv",
                expected_inferences=["\u62d2\u7edd\u91cd\u6846\u67b6"],
            ),
            tags=["schema", "stable", "confidence:0.72"],
        )

    report = backfill.run_backfill()
    assert report.ok

    out = tmp_path / "proj"
    with fts.cursor() as conn:
        projector.project_all(conn, out_dir=out)

    for name in ("project-demo.md", "schema-demo.md"):
        original = (paths.memory_dir() / name).read_text()
        projected = (out / name).read_text()
        assert projected == original, name


def test_projection_orphan_retire_round_trips_valid_until_tag(
    ac_root: Path, tmp_path: Path, _deterministic_ids
) -> None:
    with fts.cursor() as conn:
        entries.create_file(conn, name="person-bob.md", description="d", tags=[])
        eid = entries.append_entry(conn, name="person-bob.md", content="stale", tags=[])
        entries.mark_entry_deleted(conn, name="person-bob.md", entry_id=eid)

    report = backfill.run_backfill()
    assert report.ok
    out = tmp_path / "proj"
    with fts.cursor() as conn:
        projector.project_all(conn, out_dir=out)

    original = (paths.memory_dir() / "person-bob.md").read_text()
    projected = (out / "person-bob.md").read_text()
    assert projected == original
    assert "#valid-until:" in projected

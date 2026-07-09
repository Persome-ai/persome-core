"""markdown 投影生成器测试（SSOT 切换设计 §1.5/§3.4，PR-6a）。

两条主线：

1. **round-trip CI（设计稿风险 3 的机器守护）**：构造覆盖全 op 形态的 evo_nodes
   → projector 渲染 → 用 backfill 的 markdown 解析逻辑逆向重建 → 逐字段全等断言。
2. **逐字兼容性**：真实写口（append/supersede）铺库 → backfill → project，投影
   产物与原 markdown 文件逐字节相等；孤儿退役（mark_entry_deleted）的已知差异
   （#valid-until 附加 tag，损失趋零编码）单独钉死其形态。
"""

from __future__ import annotations

import dataclasses
import re
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


@pytest.fixture(autouse=True)
def _quiet_alerts(monkeypatch: pytest.MonkeyPatch) -> None:
    """backfill 的变更前快照验证走报警通路；测试里只静音 SSE 侧。"""
    monkeypatch.setattr("persome.events.publish", lambda *a, **k: None)


def _node(node_id: str, content: str, *, ts: str, **kw) -> MemoryNode:
    d = datetime.fromisoformat(ts)
    kw.setdefault("file_name", "project-rt.md")
    kw.setdefault("layer", MemoryLayer.L2_FACT)
    kw.setdefault("valid_from", ts)
    return MemoryNode(node_id=node_id, content=content, memory_at=d, gmt_created=d, **kw)


def _all_op_shape_nodes() -> list[MemoryNode]:
    """覆盖 ADD/UPDATE(两形态)/SUPERSEDE/DELETE/ABSTRACT/schema 四元组/temporal/
    shadow 态/元认知/layer 偏离/archived 的节点集（writer 可产出的全部形态）。"""
    schema_body = render_schema_body(
        central_proposition="偏好极简工具",
        supporting_summary="多次选择 uv/ruff",
        expected_inferences=["会拒绝重型框架", "评估工具先看依赖体积"],
    )
    return [
        # ADD：普通活跃链头 + 语义 tag
        _node("20260601-1000-add001", "plain fact", ts="2026-06-01T10:00", tags="alpha beta"),
        # SUPERSEDE 对：双向指针，旧节点 valid_until = 后继 heading ts（无附加 tag）
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
        # UPDATE（engine 形态）：旧节点孤儿 shadow 无指针，新头记 refined_from
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
        # DELETE：孤儿退役，valid_until 与任何 heading ts 无关 → 落 #valid-until tag
        _node(
            "20260601-1005-del0rp",
            "stale fact",
            ts="2026-06-01T10:05",
            is_latest=False,
            status=MemoryStatus.SHADOW,
            valid_until="2026-06-01T12:34",
        ),
        # ABSTRACT 链语义②：合成头带 abstracted_from 正交边，源孤儿 shadow 无指针
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
        # 元认知三件套（entry 级 confidence 词表 + conflicted + occurred）
        _node(
            "20260601-1009-met001",
            "meta fact",
            ts="2026-06-01T10:09",
            confidence="high",
            conflicted=True,
            occurred_at="2026-06-01T08:00",
        ),
        # layer 偏离前缀默认映射 → #layer tag
        _node(
            "20260601-1010-lay001",
            "knowledge in a project file",
            ts="2026-06-01T10:10",
            layer=MemoryLayer.L5_KNOWLEDGE,
        ),
        # refined-from 头被退役（三态判定会强制其活跃）→ #status:shadow tag、不打 strike
        _node(
            "20260601-1011-ref5hd",
            "refined then deleted",
            ts="2026-06-01T10:11",
            refined_from="20260601-1004-upd4ew",
            is_latest=False,
            status=MemoryStatus.SHADOW,
        ),
        # archived 态（现行格式无法区分）→ strike + #status:archived tag
        _node(
            "20260601-1012-arc001",
            "archived fact",
            ts="2026-06-01T10:12",
            is_latest=False,
            status=MemoryStatus.ARCHIVED,
        ),
        # schema 四元组全字段（L6，schema-*.md，confidence 浮点 tag）
        _node(
            "20260601-1013-sch001",
            schema_body,
            ts="2026-06-01T10:13",
            file_name="schema-rt.md",
            layer=MemoryLayer.L6_SCHEMA,
            tags="schema stable",
            schema_summary="多次选择 uv/ruff",
            schema_inferences=["会拒绝重型框架", "评估工具先看依赖体积"],
            schema_confidence=0.72,
        ),
    ]


def _parse_out_dir(out: Path) -> list[tuple[str, list[files_mod.ParsedEntry]]]:
    return [(p.name, files_mod.read_file(p).entries) for p in sorted(out.iterdir())]


def test_round_trip_all_op_shapes(ac_root: Path, tmp_path: Path) -> None:
    """§3.4 round-trip：evo_nodes → 投影渲染 → 逆向重建 → 逐字段全等。"""
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
    """非 default scope 以 #scope tag 编码并还原（§1.5 损失趋零）。"""
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
    """幂等：同一真相态重复投影逐字节相同。"""
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
    """file_name='' 的节点（如 run_system2 直写）不投影，计入 skipped_unrouted。"""
    store = NodeStore()
    store.save(_node("20260601-1200-unr001", "unrouted", ts="2026-06-01T12:00", file_name=""))
    with fts.cursor() as conn:
        report = projector.project_all(conn, out_dir=tmp_path / "proj")
    assert report.files == [] and report.skipped_unrouted == 1


# ── 逐字兼容性（加分项）：真实写口 → backfill → project 对比原文件 ───────────


@pytest.fixture
def _deterministic_ids(monkeypatch: pytest.MonkeyPatch):
    """make_id 改为递增计数器：同分钟多条 entry 的 (ts, id) 排序 == 追加顺序，
    使「投影稳定排序」与「原文件追加序」可比（真实库中同分钟乱序属已知重排面）。"""
    counter = iter(range(1, 10_000))

    def fake_make_id(timestamp: str) -> str:
        compact = timestamp.replace("-", "").replace(":", "").replace("T", "-")[:13]
        return f"{compact}-{next(counter):06x}"

    monkeypatch.setattr(entries, "make_id", fake_make_id)


def test_projection_byte_identical_to_real_markdown(
    ac_root: Path, tmp_path: Path, _deterministic_ids
) -> None:
    """append + supersede +（refined+元认知）supersede + schema 形态：
    backfill → project 的产物与真实写口写出的原文件**逐字节相等**。"""
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
                central_proposition="偏好极简",
                supporting_summary="多次选择 uv",
                expected_inferences=["拒绝重框架"],
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


def test_projection_orphan_retire_diff_is_only_valid_until_tag(
    ac_root: Path, tmp_path: Path, _deterministic_ids
) -> None:
    """mark_entry_deleted（DELETE/ABSTRACT 源）的孤儿退役：唯一差异是损失趋零
    编码的 #valid-until 附加 tag（退役时刻在现行格式中本不可表达）。"""
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
    assert projected != original
    stripped = re.sub(r" #valid-until:\S+", "", projected)
    assert stripped == original
    assert re.search(r"#valid-until:\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", projected)

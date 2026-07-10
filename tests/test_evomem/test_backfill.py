"""evo_nodes backfill 测试（SSOT 切换设计稿 §4.1，PR-2）。

构造一座真实写口写出的 markdown 库（SUPERSEDE 链 / refined-from / abstracted-from /
strike 孤儿 / event- 豁免 / schema 四元组 / 元认知 / temporal），跑 backfill 后逐字段
断言；另覆盖幂等重跑、变更前快照、dry-run 零写入、收尾断言失败路径。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from persome import paths
from persome.evomem import backfill, integrity
from persome.evomem.models import MemoryLayer, MemoryNode, MemoryStatus
from persome.evomem.store import NodeStore
from persome.store import entries, fts
from persome.writer.schema_miner_stage import render_schema_body


@pytest.fixture
def memory_fixture(ac_root: Path) -> dict[str, str]:
    """真实写口（append/supersede/mark_deleted）铺一座覆盖全部映射形态的库。"""
    ids: dict[str, str] = {}
    with fts.cursor() as conn:
        # SUPERSEDE 链：v1 → v2（v2 是链头）
        entries.create_file(conn, name="project-alpha.md", description="d", tags=["proj"])
        ids["chain_old"] = entries.append_entry(
            conn, name="project-alpha.md", content="v1 fact", tags=["alpha"]
        )
        ids["chain_new"] = entries.supersede_entry(
            conn,
            name="project-alpha.md",
            old_entry_id=ids["chain_old"],
            new_content="v2 fact",
            reason="updated",
            tags=["alpha"],
        )
        # refined-from（UPDATE 双标签法）+ 元认知三件套
        entries.create_file(conn, name="user-test.md", description="d", tags=[])
        ids["refine_old"] = entries.append_entry(
            conn, name="user-test.md", content="rough", tags=[]
        )
        ids["refine_new"] = entries.supersede_entry(
            conn,
            name="user-test.md",
            old_entry_id=ids["refine_old"],
            new_content="sharpened",
            reason="refined",
            refined_from=ids["refine_old"],
            confidence="high",
            conflicted=True,
            occurred_at="2026-06-01T10:00",
        )
        # ABSTRACT 形态：合成条目带多值出处 tag，源逐个 strike（孤儿退役）
        entries.create_file(conn, name="topic-merge.md", description="d", tags=[])
        ids["src_a"] = entries.append_entry(conn, name="topic-merge.md", content="part a", tags=[])
        ids["src_b"] = entries.append_entry(conn, name="topic-merge.md", content="part b", tags=[])
        ids["synth"] = entries.append_entry(
            conn,
            name="topic-merge.md",
            content="synthesis",
            tags=[f"abstracted-from:{ids['src_a']},{ids['src_b']}"],
        )
        entries.mark_entry_deleted(conn, name="topic-merge.md", entry_id=ids["src_a"])
        entries.mark_entry_deleted(conn, name="topic-merge.md", entry_id=ids["src_b"])
        # strike 孤儿（DELETE 无后继）
        entries.create_file(conn, name="person-bob.md", description="d", tags=[])
        ids["orphan"] = entries.append_entry(conn, name="person-bob.md", content="stale", tags=[])
        entries.mark_entry_deleted(conn, name="person-bob.md", entry_id=ids["orphan"])
        # event- 豁免（Q2）
        entries.create_file(conn, name="event-2026-06-10.md", description="d", tags=[])
        ids["event_1"] = entries.append_entry(
            conn, name="event-2026-06-10.md", content="did x", tags=[]
        )
        ids["event_2"] = entries.append_entry(
            conn, name="event-2026-06-10.md", content="did y", tags=[]
        )
        # schema 四元组（miner 的真实落盘形态：render_schema_body + confidence float tag）
        entries.create_file(conn, name="schema-project-alpha.md", description="d", tags=["schema"])
        ids["schema"] = entries.append_entry(
            conn,
            name="schema-project-alpha.md",
            content=render_schema_body(
                central_proposition="偏好极简工具",
                supporting_summary="多次选择 uv/ruff",
                expected_inferences=["拒绝重框架", "优先看依赖体积"],
            ),
            tags=["schema", "stable", "confidence:0.72"],
        )
        # intent- 投影（L7）
        entries.create_file(conn, name="intent-meeting.md", description="d", tags=[])
        ids["intent"] = entries.append_entry(
            conn, name="intent-meeting.md", content="周五开会", tags=[]
        )
    return ids


def _node(node_id: str) -> MemoryNode:
    got = NodeStore().get(node_id)
    assert got is not None, f"node {node_id} missing from evo_nodes"
    return got


def test_backfill_end_to_end(memory_fixture: dict[str, str]) -> None:
    ids = memory_fixture
    report = backfill.run_backfill()
    assert report.ok, (report.violations, report.heads_only_evo, report.heads_only_fts)
    assert report.files == 7
    assert report.scanned_entries == 12
    assert report.skipped_event == 2
    assert report.backfilled_nodes == 10
    assert report.dangling_edges == []

    # SUPERSEDE 链：双向指针 + is_latest（tag 三态判定派生）+ status
    old = _node(ids["chain_old"])
    new = _node(ids["chain_new"])
    assert old.superseded_by == [ids["chain_new"]]
    assert old.status is MemoryStatus.SHADOW
    assert not old.is_latest
    assert old.content == "v1 fact"  # strike 已剥，prose 干净
    assert new.supersedes == [ids["chain_old"]]
    assert new.superseded_by == []
    assert new.status is MemoryStatus.ACTIVE
    assert new.is_latest
    assert new.layer is MemoryLayer.L2_FACT
    assert new.file_name == "project-alpha.md"
    assert new.tags == "alpha"  # 链 tag 不进 tags 列
    # Q8：temporal 收编
    assert old.valid_from and old.valid_until
    assert new.valid_from and new.valid_until is None

    # refined-from + 元认知（entry_metadata 收编）
    refined = _node(ids["refine_new"])
    assert refined.refined_from == ids["refine_old"]
    assert refined.confidence == "high"
    assert refined.conflicted is True
    assert refined.occurred_at == "2026-06-01T10:00"
    assert refined.layer is MemoryLayer.L4_IDENTITY
    assert refined.is_latest and refined.status is MemoryStatus.ACTIVE

    # ABSTRACT：多源出处列 + 源 strike 孤儿 → shadow
    synth = _node(ids["synth"])
    assert synth.abstracted_from == [ids["src_a"], ids["src_b"]]
    assert synth.supersedes == []  # 出处是正交 provenance 边，不进链指针
    assert synth.is_latest and synth.status is MemoryStatus.ACTIVE
    for src in (ids["src_a"], ids["src_b"]):
        n = _node(src)
        assert n.status is MemoryStatus.SHADOW and not n.is_latest

    # strike 孤儿
    orphan = _node(ids["orphan"])
    assert orphan.status is MemoryStatus.SHADOW and not orphan.is_latest
    assert orphan.content == "stale"

    # event- 豁免：不进表
    assert NodeStore().get(ids["event_1"]) is None
    assert NodeStore().get(ids["event_2"]) is None

    # schema 四元组
    schema = _node(ids["schema"])
    assert schema.layer is MemoryLayer.L6_SCHEMA
    assert schema.schema_summary == "多次选择 uv/ruff"
    assert schema.schema_inferences == ["拒绝重框架", "优先看依赖体积"]
    assert schema.schema_confidence == pytest.approx(0.72)
    assert "central: 偏好极简工具" in schema.content
    assert schema.tags == "schema stable"  # confidence: colon-tag 编码进列

    # intent- → L7
    assert _node(ids["intent"]).layer is MemoryLayer.L7_INTENTION


def test_backfill_takes_pre_change_snapshot(memory_fixture: dict[str, str]) -> None:
    backup_dir = paths.backup_dir()
    assert not (backup_dir.exists() and list(backup_dir.glob("evo-*.db")))
    report = backfill.run_backfill()
    assert report.ok
    assert list(paths.backup_dir().glob("evo-*.db")), "§3.2 变更前快照未落盘"


def test_backfill_aborts_when_snapshot_fails(
    memory_fixture: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """坏快照 = 不动真相：evo_nodes 保持未写入。"""
    monkeypatch.setattr("persome.evomem.backup.create_snapshot", lambda **kw: None)
    with pytest.raises(backfill.BackfillError):
        backfill.run_backfill()
    with fts.cursor() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='evo_nodes'"
        ).fetchone()
        if row[0]:
            assert conn.execute("SELECT COUNT(*) FROM evo_nodes").fetchone()[0] == 0


def test_backfill_is_idempotent(memory_fixture: dict[str, str]) -> None:
    """重跑产出 byte-identical 行（INSERT OR REPLACE 幂等纪律）。"""

    def dump() -> list[tuple]:
        with fts.cursor() as conn:
            return [tuple(r) for r in conn.execute("SELECT * FROM evo_nodes ORDER BY node_id")]

    r1 = backfill.run_backfill()
    first = dump()
    r2 = backfill.run_backfill()
    second = dump()
    assert r1.ok and r2.ok
    assert r1.backfilled_nodes == r2.backfilled_nodes
    assert first == second


def test_dry_run_writes_nothing(memory_fixture: dict[str, str]) -> None:
    report = backfill.run_backfill(dry_run=True)
    assert report.dry_run
    assert report.ok  # 计划链头集合与 entries 检索投影活跃集合全等
    assert report.backfilled_nodes == 10
    assert report.skipped_event == 2
    with fts.cursor() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='evo_nodes'"
        ).fetchone()
        if row[0]:
            assert conn.execute("SELECT COUNT(*) FROM evo_nodes").fetchone()[0] == 0
    backup = paths.backup_dir()
    assert not (backup.exists() and list(backup.glob("evo-*.db"))), "dry-run 不应打快照"


def test_closing_assertion_fails_on_rogue_head(memory_fixture: dict[str, str]) -> None:
    """收尾断言失败路径：evo_nodes 里有检索投影没有的活跃头 → ok=False 报 diff。"""
    rogue = MemoryNode(node_id="rogue-head", content="x", layer=MemoryLayer.L2_FACT)
    NodeStore().save(rogue)
    report = backfill.run_backfill()
    assert not report.ok
    assert "rogue-head" in report.heads_only_evo
    # 投影对账（check 6）也应同时报数目不齐（alert-only 类）
    assert any(v.check == "projection_reconciliation" for v in report.violations)


def test_dangling_supersede_edge_dropped(memory_fixture: dict[str, str]) -> None:
    """指向未知 id 的 #superseded-by 按悬空丢弃记录，不写进指针列（自检无悬空铁律）。"""
    path = paths.memory_dir() / "project-alpha.md"
    text = path.read_text()
    # 给链头追加一个指向不存在 id 的 superseded-by tag（模拟坏数据）
    needle = f"{{id: {memory_fixture['chain_new']}}}"
    assert needle in text
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if needle in line:
            lines[i] = line + " #superseded-by:00000000-0000-ffffff"
            break
    path.write_text("\n".join(lines) + "\n")
    # entries 检索投影同步退役该头，保持投影一致（只考悬空边处理，不考漂移检测）
    with fts.cursor() as conn:
        fts.mark_superseded(conn, memory_fixture["chain_new"])

    report = backfill.run_backfill()
    assert report.dangling_edges == [f"{memory_fixture['chain_new']}→00000000-0000-ffffff"]
    node = _node(memory_fixture["chain_new"])
    assert node.superseded_by == []  # 悬空边没有落进指针列
    assert node.status is MemoryStatus.SHADOW  # strike/tag 判定照常退役
    assert report.ok, (report.violations, report.heads_only_evo, report.heads_only_fts)


def test_event_exemption_in_projection_check(memory_fixture: dict[str, str]) -> None:
    """Q2：integrity check 6 的对账侧排除 event-，backfill 后全套自检零违例。"""
    report = backfill.run_backfill()
    assert report.ok
    with fts.cursor() as conn:
        assert integrity.run_checks(conn) == []


def test_abstracted_from_round_trips_as_json(memory_fixture: dict[str, str]) -> None:
    backfill.run_backfill()
    with fts.cursor() as conn:
        row = conn.execute(
            "SELECT abstracted_from FROM evo_nodes WHERE node_id=?", (memory_fixture["synth"],)
        ).fetchone()
    assert json.loads(row["abstracted_from"]) == [
        memory_fixture["src_a"],
        memory_fixture["src_b"],
    ]

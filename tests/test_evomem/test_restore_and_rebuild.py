"""PR-7 — rebuild_index 重定义（检索投影重建器）+ import_from_markdown 灾难恢复。

契约（设计稿 §2 表「rebuild_index」行 + §3.4）：

- write_authority="markdown"（默认）：rebuild 行为与历史一致（from-markdown
  全量重放）——由既有测试（test_evo02_refined_from / test_compact_* 等）钉死。
- write_authority="evomem"：rebuild 变为混合重建——entries/entry_metadata 从
  evo_nodes（真相）投影，event-*（Q2 豁免）与不在 evo_nodes 的文件从 markdown
  直读；files 行不被清空（本模式下它是文件级元数据真相）。
- import_from_markdown（evomem/restore.py）：从投影 markdown 整库重建
  evo_nodes（§3.4 有损灾难恢复）——colon-tag 还原、指针重建、变更前快照、
  收尾自检；dry-run 零写入。
"""

from __future__ import annotations

from persome import paths
from persome.evomem import backfill, restore
from persome.store import entries as entries_mod
from persome.store import fts


def _set_authority(root, value: str) -> None:
    (root / "config.toml").write_text(f'[evomem]\nwrite_authority = "{value}"\n')


def _seed_and_backfill() -> dict[str, str]:
    """markdown 模式下种一条 supersede 链 + 孤立条目 + event 文件，再 backfill。"""
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-x.md", description="x", tags=["t"])
        v1 = entries_mod.append_entry(conn, name="project-x.md", content="v1 fact", tags=["t"])
        v2 = entries_mod.supersede_entry(
            conn, name="project-x.md", old_entry_id=v1, new_content="v2 fact", reason="r"
        )
        iso = entries_mod.append_entry(conn, name="project-x.md", content="isolated", tags=["t"])
        entries_mod.create_file(conn, name="event-2026-06-11.md", description="e", tags=[])
        ev = entries_mod.append_entry(
            conn, name="event-2026-06-11.md", content="event row", tags=[]
        )
    report = backfill.run_backfill()
    assert report.ok
    return {"v1": v1, "v2": v2, "iso": iso, "ev": ev}


def _entries_state(conn) -> dict[str, tuple[str, int]]:
    return {
        r["id"]: (r["path"], int(r["superseded"]))
        for r in conn.execute("SELECT id, path, superseded FROM entries").fetchall()
    }


# ── evomem 主写模式：混合重建 ────────────────────────────────────────────────


def test_evo_rebuild_projects_from_evo_nodes_and_markdown_for_events(ac_root) -> None:
    """检索投影炸了（DELETE entries）→ rebuild 从 evo_nodes 重放非 event 行、
    从 markdown 直读 event 行；files 行不被清空。"""
    ids = _seed_and_backfill()
    _set_authority(ac_root, "evomem")
    with fts.cursor() as conn:
        before_files = {r["path"] for r in conn.execute("SELECT path FROM files").fetchall()}
        conn.execute("DELETE FROM entries")
        conn.execute("DELETE FROM entry_metadata")
        files_n, entries_n = entries_mod.rebuild_index(conn)
        state = _entries_state(conn)
        after_files = {r["path"] for r in conn.execute("SELECT path FROM files").fetchall()}
    assert files_n == 2 and entries_n == 4
    assert state[ids["v1"]] == ("project-x.md", 1)  # 退役判定从 evo_nodes 派生
    assert state[ids["v2"]] == ("project-x.md", 0)
    assert state[ids["iso"]] == ("project-x.md", 0)
    assert state[ids["ev"]] == ("event-2026-06-11.md", 0)  # Q2：markdown 直读
    assert after_files == before_files  # files 行是真相，不被 DELETE


def test_evo_rebuild_follows_truth_not_markdown(ac_root) -> None:
    """行为证明：只篡改 evo_nodes（markdown 不动）→ rebuild 后 superseded 跟着
    真相走，证明数据源确实是 evo_nodes 而非 markdown 重放。"""
    ids = _seed_and_backfill()
    _set_authority(ac_root, "evomem")
    with fts.cursor() as conn:
        conn.execute(
            "UPDATE evo_nodes SET is_latest=0, status='shadow' WHERE node_id=?", (ids["iso"],)
        )
        entries_mod.rebuild_index(conn)
        state = _entries_state(conn)
    assert state[ids["iso"]][1] == 1  # markdown 里它仍是活条目；真相说退役 → 投影跟真相


def test_markdown_rebuild_unchanged_under_default_authority(ac_root) -> None:
    """默认（markdown 主写）：rebuild 仍是 from-markdown 重放（历史行为）——
    篡改 evo_nodes 不影响输出。"""
    ids = _seed_and_backfill()
    with fts.cursor() as conn:
        conn.execute(
            "UPDATE evo_nodes SET is_latest=0, status='shadow' WHERE node_id=?", (ids["iso"],)
        )
        entries_mod.rebuild_index(conn)
        state = _entries_state(conn)
    assert state[ids["iso"]][1] == 0  # markdown 是真相，evo 篡改无效


# ── import_from_markdown 灾难恢复 ────────────────────────────────────────────


def _evo_dump(conn) -> list[tuple]:
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT node_id, file_name, supersedes, superseded_by, is_latest, status,"
            " refined_from, abstracted_from, tags, content"
            " FROM evo_nodes ORDER BY node_id"
        ).fetchall()
    ]


def test_restore_round_trips_chain_fields_after_disaster(ac_root) -> None:
    """灾难（evo_nodes 整表清空）→ import_from_markdown 还原：链字段
    （指针/is_latest/status/出处/语义 tag/content）逐节点全等。"""
    _seed_and_backfill()
    with fts.cursor() as conn:
        before = _evo_dump(conn)
        conn.execute("DELETE FROM evo_nodes")  # the disaster
    report = restore.import_from_markdown()
    assert report.ok, report.violations
    assert report.nodes == len(before)
    assert report.skipped_event_files == 1  # Q2 豁免
    with fts.cursor() as conn:
        after = _evo_dump(conn)
        live = {
            r["id"] for r in conn.execute("SELECT id FROM entries WHERE superseded=0").fetchall()
        }
    assert after == before
    assert live  # 检索投影同步重放（非空）
    # 恢复前强制快照（§3.2）落了盘
    assert list(paths.backup_dir().glob("evo-*.db"))


def test_restore_dry_run_writes_nothing(ac_root) -> None:
    _seed_and_backfill()
    with fts.cursor() as conn:
        before = _evo_dump(conn)
    report = restore.import_from_markdown(dry_run=True)
    assert report.dry_run
    assert report.nodes == len(before)
    with fts.cursor() as conn:
        assert _evo_dump(conn) == before  # untouched

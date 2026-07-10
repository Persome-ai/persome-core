"""逐站「输出等价」断言的共享 harness（PR-6b，设计稿 §4.4）。

核心承诺：**同输入下，反转写路（write_authority="evomem"）产出的 markdown 投影
与 legacy 直写产物 byte-identical**（仅允许两处已知良性差异，见
:func:`normalize_projection`），且 FTS 检索投影（entries / entry_metadata /
entry_temporal / files 四表）逐行全等，且 evo_nodes 真相态与
「legacy 直写后跑全量 backfill」的态逐行全等。

用法（每个写站点一个测试，独立 commit）::

    def _script():
        ...  # 以该站点的真实入口驱动一组写
    snap_md, snap_evo = run_in_both_modes(monkeypatch, tmp_path, _script)
    assert_equivalent(snap_md, snap_evo)

确定性：两轮分别在隔离的 PERSOME_ROOT 跑，``make_id`` 换成重置计数器、
``_now_iso_minute`` 冻结到同一分钟——两轮的 entry id / 时间戳逐字相同，快照
可以直接全等比较。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from persome import paths
from persome.evomem import inversion as evo_inversion
from persome.evomem import shadow as evo_shadow
from persome.store import entries, fts

FROZEN_MINUTE = "2026-06-11T10:00"

# 反转模式投影的两处已知良性差异（其余一个字节都不许差）：
# 1. frontmatter 的 `projected:` 注记行（Q1 (b) 手改警示，仅反转模式落）。
# 2. 孤儿退役（DELETE/ABSTRACT 源）的 #valid-until 损失趋零附加 tag——退役时刻
#    在现行格式中本不可表达，6a 的 test_projection_orphan_retire_diff_is_only_
#    valid_until_tag 已钉死其形态。
_MARKER_LINE_RE = re.compile(r"^projected: .*\n", re.MULTILINE)
_VALID_UNTIL_TAG_RE = re.compile(r" #valid-until:\S+")


def normalize_projection(text: str) -> str:
    return _VALID_UNTIL_TAG_RE.sub("", _MARKER_LINE_RE.sub("", text))


@dataclass
class Snapshot:
    memory: dict[str, str]
    entries: list[tuple]
    metadata: list[tuple]
    temporal: list[tuple]
    files: list[tuple]
    evo_nodes: list[tuple]


def take_snapshot() -> Snapshot:
    import sqlite3

    memory = {
        p.name: p.read_text()
        for p in sorted(paths.memory_dir().glob("*.md"))
        if p.name != "index.md"
    }

    def _evo_rows(conn) -> list[tuple]:
        try:
            return [
                tuple(r)
                for r in conn.execute(
                    "SELECT node_id, user_id, agent_id, content, layer, supersedes,"
                    " superseded_by, is_latest, status, memory_at, gmt_created, file_name,"
                    " tags, refined_from, abstracted_from, confidence, conflicted,"
                    " occurred_at, schema_summary, schema_inferences, schema_confidence,"
                    " valid_from, valid_until FROM evo_nodes ORDER BY node_id"
                )
            ]
        except sqlite3.OperationalError:  # markdown 轮 backfill 前表还不存在
            return []

    with fts.cursor() as conn:
        snap = Snapshot(
            memory=memory,
            entries=[
                tuple(r)
                for r in conn.execute(
                    "SELECT id, path, prefix, timestamp, tags, content, superseded"
                    " FROM entries ORDER BY id"
                )
            ],
            metadata=[
                tuple(r)
                for r in conn.execute(
                    "SELECT entry_id, confidence, conflicted, occurred_at"
                    " FROM entry_metadata ORDER BY entry_id"
                )
            ],
            temporal=[
                tuple(r)
                for r in conn.execute(
                    "SELECT entry_id, valid_from, valid_until FROM entry_temporal ORDER BY entry_id"
                )
            ],
            files=[
                tuple(r)
                for r in conn.execute(
                    "SELECT path, prefix, description, tags, status, entry_count,"
                    " created, updated, needs_compact FROM files ORDER BY path"
                )
            ],
            evo_nodes=_evo_rows(conn),
        )
    return snap


def _patch_deterministic(mp: pytest.MonkeyPatch) -> None:
    counter = iter(range(1, 10_000))

    def fake_make_id(timestamp: str) -> str:
        compact = timestamp.replace("-", "").replace(":", "").replace("T", "-")[:13]
        return f"{compact}-{next(counter):06x}"

    mp.setattr(entries, "make_id", fake_make_id)
    mp.setattr(entries, "_now_iso_minute", lambda: FROZEN_MINUTE)


def run_in_both_modes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    script: Callable[[], None],
) -> tuple[Snapshot, Snapshot]:
    """在两个隔离 root 里以两种写权跑同一 ``script``，返回 (markdown 态, evomem 态)。

    markdown 轮的快照分两段取：

    - ``memory``/``entries``/``chain``/``temporal``/``files`` 取**增量态**——recall
      在两次 rebuild 之间看到的就是它，是逐站等价的主比较面。
    - ``metadata``/``evo_nodes`` 取 **rebuild 后的 fixpoint 态**（先
      ``rebuild_index`` 再全量 backfill）。系统自己的声明不变量是「增量
      entry_metadata ≡ fresh rebuild」，但 legacy 在一个角落违反它（supersede
      ``tags=None`` 回退把旧 heading 的元认知 colon-tag 抄上新 heading、却不写
      metadata 行，直到下次 rebuild 才补上）；反转写路把承继落在 canonical 家，
      **直接落在 fixpoint 上**——所以这两面以 fixpoint 为比较目标才是对的尺。
      evo_nodes 面同时承接「反转写路增量真相 == legacy 直写 + 全量 backfill」
      （影子写不变式在反转后的延续）。
    """
    snaps: dict[str, Snapshot] = {}
    for mode in ("markdown", "evomem"):
        with monkeypatch.context() as mp:
            root = tmp_path / f"root-{mode}"
            root.mkdir()
            mp.setenv("PERSOME_ROOT", str(root))
            paths.ensure_dirs()
            (root / "config.toml").write_text(f'[evomem]\nwrite_authority = "{mode}"\n')
            _patch_deterministic(mp)
            evo_shadow.reset_misses()
            evo_inversion.reset_misses()
            script()
            snap = take_snapshot()
            if mode == "markdown":
                from persome.evomem import backfill

                with fts.cursor() as conn:
                    entries.rebuild_index(conn)
                report = backfill.run_backfill()
                assert report.ok, f"markdown-mode backfill failed: {report}"
                healed = take_snapshot()
                snap.metadata = healed.metadata
                snap.evo_nodes = healed.evo_nodes
            snaps[mode] = snap
    return snaps["markdown"], snaps["evomem"]


def assert_equivalent(md: Snapshot, evo: Snapshot) -> None:
    """逐面全等断言：markdown 投影（normalize 后逐字节）、FTS 五表、evo_nodes。"""
    assert set(evo.memory) == set(md.memory), (set(evo.memory), set(md.memory))
    for name in sorted(md.memory):
        assert normalize_projection(evo.memory[name]) == md.memory[name], (
            f"projection of {name} not byte-identical\n--- legacy ---\n{md.memory[name]}"
            f"\n--- inverted (normalized) ---\n{normalize_projection(evo.memory[name])}"
        )
        # 反转模式的投影必须带手改警示注记（Q1 (b)）。
        assert _MARKER_LINE_RE.search(evo.memory[name]), f"{name} missing projected: marker"
    assert evo.entries == md.entries
    assert evo.metadata == md.metadata
    assert evo.temporal == md.temporal
    assert evo.files == md.files
    assert evo.evo_nodes == md.evo_nodes

"Tests for inversion harness."

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
        except sqlite3.OperationalError:
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
    assert set(evo.memory) == set(md.memory), (set(evo.memory), set(md.memory))
    for name in sorted(md.memory):
        assert normalize_projection(evo.memory[name]) == md.memory[name], (
            f"projection of {name} not byte-identical\n--- legacy ---\n{md.memory[name]}"
            f"\n--- inverted (normalized) ---\n{normalize_projection(evo.memory[name])}"
        )

        assert _MARKER_LINE_RE.search(evo.memory[name]), f"{name} missing projected: marker"
    assert evo.entries == md.entries
    assert evo.metadata == md.metadata
    assert evo.temporal == md.temporal
    assert evo.files == md.files
    assert evo.evo_nodes == md.evo_nodes

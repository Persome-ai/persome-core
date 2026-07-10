"Tests for test inversion."

from __future__ import annotations

from pathlib import Path

import pytest

from persome.evomem import integrity as evo_integrity
from persome.evomem import inversion as evo_inversion
from persome.store import entries, fts
from persome.store import files as files_mod

from .inversion_harness import (
    assert_equivalent,
    normalize_projection,
    run_in_both_modes,
    take_snapshot,
)


def _set_authority(root: Path, value: str) -> None:
    (root / "config.toml").write_text(f'[evomem]\nwrite_authority = "{value}"\n')


@pytest.fixture(autouse=True)
def _reset_inversion_misses() -> None:
    evo_inversion.reset_misses()


def test_default_authority_is_markdown_and_routes_nothing(ac_root: Path) -> None:
    assert evo_inversion.authority() == "markdown"
    assert not evo_inversion.routes_to_engine("project-x.md")


def test_unknown_authority_falls_back_to_markdown(ac_root: Path) -> None:
    _set_authority(ac_root, "everything-to-the-moon")
    assert evo_inversion.authority() == "markdown"
    assert not evo_inversion.routes_to_engine("project-x.md")


def test_routes_to_engine_exemptions(ac_root: Path) -> None:
    _set_authority(ac_root, "evomem")
    assert evo_inversion.routes_to_engine("project-x.md")
    assert evo_inversion.routes_to_engine("user-profile")
    assert not evo_inversion.routes_to_engine("event-2026-06-11.md")  # Q2
    assert not evo_inversion.routes_to_engine("skills/skill-foo.md")
    assert not evo_inversion.routes_to_engine("bogus.md")


def test_core_write_verbs_equivalent_across_authorities(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:

    def _script() -> None:
        with fts.cursor() as conn:
            entries.create_file(conn, name="project-demo.md", description="demo", tags=["proj"])
            e1 = entries.append_entry(conn, name="project-demo.md", content="v1", tags=["alpha"])
            entries.supersede_entry(
                conn,
                name="project-demo.md",
                old_entry_id=e1,
                new_content="v2",
                reason="updated",
                tags=["alpha"],
            )
            e3 = entries.append_entry(
                conn,
                name="project-demo.md",
                content="meta fact",
                tags=["beta"],
                confidence="high",
                conflicted=True,
                occurred_at="2026-06-01 08:00",
            )

            entries.supersede_entry(
                conn,
                name="project-demo.md",
                old_entry_id=e3,
                new_content="sharpened",
                reason="refined",
                tags=["beta"],
                refined_from=e3,
                confidence="medium",
                occurred_at="2026-06-02T09:00",
            )

            e5 = entries.append_entry(conn, name="project-demo.md", content="rough", tags=["g1"])
            entries.supersede_entry(
                conn,
                name="project-demo.md",
                old_entry_id=e5,
                new_content="polished",
                reason="rewrite",
                tags=None,
            )
            e7 = entries.append_entry(conn, name="project-demo.md", content="stale", tags=[])
            entries.mark_entry_deleted(conn, name="project-demo.md", entry_id=e7)
            entries.set_file_status(conn, name="project-demo.md", status="dormant")

    snap_md, snap_evo = run_in_both_modes(monkeypatch, tmp_path, _script)
    assert_equivalent(snap_md, snap_evo)


def test_fallback_meta_inheritance_pins_healing_divergence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:

    def _script() -> None:
        with fts.cursor() as conn:
            entries.create_file(conn, name="person-m.md", description="d", tags=[])
            old = entries.append_entry(
                conn,
                name="person-m.md",
                content="v1",
                tags=["who"],
                confidence="low",
                occurred_at="2026-06-01T07:00",
            )
            entries.supersede_entry(
                conn,
                name="person-m.md",
                old_entry_id=old,
                new_content="v2",
                reason="update",
                tags=None,
            )

    snap_md, snap_evo = run_in_both_modes(monkeypatch, tmp_path, _script)

    assert_equivalent(snap_md, snap_evo)

    evo_meta = {r[0]: r for r in snap_evo.metadata}
    (new_id,) = [r[0] for r in snap_evo.entries if r[6] == 0]
    assert evo_meta[new_id][1] == "low" and evo_meta[new_id][3] == "2026-06-01T07:00"


def test_append_raises_filenotfound_before_create(ac_root: Path) -> None:
    _set_authority(ac_root, "evomem")
    with fts.cursor() as conn, pytest.raises(FileNotFoundError, match="call create_file first"):
        entries.append_entry(conn, name="project-nope.md", content="x", tags=[])


def test_supersede_unknown_entry_raises_valueerror(ac_root: Path) -> None:
    _set_authority(ac_root, "evomem")
    with fts.cursor() as conn:
        entries.create_file(conn, name="project-a.md", description="d", tags=[])
        with pytest.raises(ValueError, match="not found in project-a.md"):
            entries.supersede_entry(
                conn,
                name="project-a.md",
                old_entry_id="20990101-0000-ffffff",
                new_content="x",
                reason="r",
            )


def test_create_duplicate_raises_fileexists(ac_root: Path) -> None:
    _set_authority(ac_root, "evomem")
    with fts.cursor() as conn:
        entries.create_file(conn, name="project-a.md", description="d", tags=[])
        with pytest.raises(FileExistsError):
            entries.create_file(conn, name="project-a.md", description="d", tags=[])


def test_soft_limit_flags_needs_compact_in_inversion(ac_root: Path) -> None:
    _set_authority(ac_root, "evomem")
    with fts.cursor() as conn:
        entries.create_file(conn, name="project-big.md", description="d", tags=[])
        entries.append_entry(
            conn, name="project-big.md", content="x" * 4000, tags=[], soft_limit_tokens=100
        )
        row = conn.execute("SELECT needs_compact FROM files WHERE path='project-big.md'").fetchone()
    assert row["needs_compact"] == 1
    parsed = files_mod.read_file(files_mod.memory_path("project-big.md"))
    assert parsed.needs_compact


def test_projection_failure_keeps_truth_write_and_counts_misses(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_authority(ac_root, "evomem")
    with fts.cursor() as conn:
        entries.create_file(conn, name="project-p.md", description="d", tags=[])

    alerts: list[tuple] = []
    monkeypatch.setattr(evo_integrity, "emit_alert", lambda *a, **k: alerts.append((a, k)))
    monkeypatch.setattr(evo_inversion, "_ALERT_EVERY", 1)
    monkeypatch.setattr(
        files_mod, "atomic_write_text", lambda *a, **k: (_ for _ in ()).throw(OSError("disk"))
    )
    before = evo_inversion.miss_count()
    with fts.cursor() as conn:
        eid = entries.append_entry(conn, name="project-p.md", content="fact", tags=[])

        node_row = conn.execute("SELECT * FROM evo_nodes WHERE node_id=?", (eid,)).fetchone()
        entry_row = conn.execute("SELECT * FROM entries WHERE id=?", (eid,)).fetchone()
    assert node_row is not None and entry_row is not None
    assert evo_inversion.miss_count() == before + 1
    assert alerts and alerts[0][0][0] == "markdown_projection_lag"

    with fts.cursor() as conn:
        state = conn.execute(
            "SELECT content_hash FROM projection_state WHERE file_name='project-p.md'"
        ).fetchone()

    assert state is not None
    projected = files_mod.memory_path("project-p.md").read_text()
    assert evo_inversion.content_hash(projected) == state["content_hash"]


def test_event_files_stay_on_legacy_path_in_evomem_mode(ac_root: Path) -> None:
    _set_authority(ac_root, "evomem")
    name = "event-2026-06-11.md"
    from persome.evomem.store import NodeStore

    NodeStore()
    with fts.cursor() as conn:
        entries.create_file(conn, name=name, description="day log", tags=["event"])
        entries.append_entry(conn, name=name, content="[10:00-10:05] did things", tags=[])
        n_nodes = conn.execute("SELECT COUNT(*) c FROM evo_nodes").fetchone()["c"]
    assert n_nodes == 0
    text = files_mod.memory_path(name).read_text()
    assert "projected:" not in text


def test_shadow_write_disabled_under_evomem_authority(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    with fts.cursor() as conn:
        entries.create_file(conn, name="project-s.md", description="d", tags=[])
        entries.append_entry(conn, name="project-s.md", content="seed", tags=[])
    from persome.evomem import backfill

    assert backfill.run_backfill().ok
    _set_authority(ac_root, "evomem")
    with fts.cursor() as conn:
        entries.create_file(conn, name="skills/skill-t.md", description="d", tags=[])
        entries.append_entry(conn, name="skills/skill-t.md", content="echo", tags=[])
        rows = conn.execute(
            "SELECT COUNT(*) c FROM evo_nodes WHERE file_name LIKE 'skill%'"
        ).fetchone()["c"]
    assert rows == 0


def test_rollback_flip_back_restores_legacy_path_and_shadow(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_authority(ac_root, "evomem")
    with fts.cursor() as conn:
        entries.create_file(conn, name="project-r.md", description="d", tags=[])
        e1 = entries.append_entry(conn, name="project-r.md", content="inverted era", tags=[])

    _set_authority(ac_root, "markdown")
    assert not evo_inversion.routes_to_engine("project-r.md")
    with fts.cursor() as conn:
        e2 = entries.append_entry(conn, name="project-r.md", content="markdown era", tags=[])
        shadowed = conn.execute(
            "SELECT COUNT(*) c FROM evo_nodes WHERE node_id=?", (e2,)
        ).fetchone()["c"]
        first = conn.execute("SELECT COUNT(*) c FROM evo_nodes WHERE node_id=?", (e1,)).fetchone()[
            "c"
        ]
    assert first == 1
    assert shadowed == 1
    text = files_mod.memory_path("project-r.md").read_text()
    assert "inverted era" in text and "markdown era" in text


def test_reproject_idempotent(ac_root: Path) -> None:
    _set_authority(ac_root, "evomem")
    with fts.cursor() as conn:
        entries.create_file(conn, name="project-i.md", description="d", tags=[])
        entries.append_entry(conn, name="project-i.md", content="fact", tags=["a"])
        first = files_mod.memory_path("project-i.md").read_text()
        evo_inversion.reproject_file(conn, "project-i.md")
        second = files_mod.memory_path("project-i.md").read_text()
    assert first == second
    assert normalize_projection(first) != first
    snap = take_snapshot()
    assert "project-i.md" in snap.memory


def test_compact_deferred_under_evomem_authority(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from persome import config as config_mod
    from persome.writer import compact as compact_mod
    from persome.writer import llm as llm_mod

    _set_authority(ac_root, "evomem")
    with fts.cursor() as conn:
        entries.create_file(conn, name="project-c.md", description="d", tags=[])
        entries.append_entry(conn, name="project-c.md", content="fact", tags=[])
        fts.set_needs_compact(conn, "project-c.md", True)

    monkeypatch.setattr(
        llm_mod, "call_llm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no LLM"))
    )
    before = files_mod.memory_path("project-c.md").read_text()
    cfg = config_mod.load()
    with fts.cursor() as conn:
        results = compact_mod.run_pending(cfg, conn)

        assert results and all(not r.accepted for r in results)
        assert all("deferred" in r.note for r in results)
        flag = conn.execute("SELECT needs_compact FROM files WHERE path='project-c.md'").fetchone()[
            "needs_compact"
        ]
    assert flag == 1
    assert files_mod.memory_path("project-c.md").read_text() == before


def test_no_reconcile_apply_residue() -> None:
    src = Path(__file__).resolve().parents[2] / "src" / "persome"
    assert not (src / "writer" / "reconcile_apply.py").exists()
    offenders = []
    for p in src.rglob("*.py"):
        text = p.read_text()
        if "import reconcile_apply" in text or "reconcile_apply.apply" in text:
            offenders.append(str(p))
    assert offenders == []

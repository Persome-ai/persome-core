"Tests for test restore and rebuild."

from __future__ import annotations

from persome import paths
from persome.evomem import backfill, restore
from persome.store import entries as entries_mod
from persome.store import fts


def _set_authority(root, value: str) -> None:
    (root / "config.toml").write_text(f'[evomem]\nwrite_authority = "{value}"\n')


def _seed_and_backfill() -> dict[str, str]:
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


def test_evo_rebuild_projects_from_evo_nodes_and_markdown_for_events(ac_root) -> None:
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
    assert state[ids["v1"]] == ("project-x.md", 1)
    assert state[ids["v2"]] == ("project-x.md", 0)
    assert state[ids["iso"]] == ("project-x.md", 0)
    assert state[ids["ev"]] == ("event-2026-06-11.md", 0)
    assert after_files == before_files


def test_evo_rebuild_follows_truth_not_markdown(ac_root) -> None:
    ids = _seed_and_backfill()
    _set_authority(ac_root, "evomem")
    with fts.cursor() as conn:
        conn.execute(
            "UPDATE evo_nodes SET is_latest=0, status='shadow' WHERE node_id=?", (ids["iso"],)
        )
        entries_mod.rebuild_index(conn)
        state = _entries_state(conn)
    assert state[ids["iso"]][1] == 1


def test_markdown_rebuild_unchanged_under_default_authority(ac_root) -> None:
    ids = _seed_and_backfill()
    with fts.cursor() as conn:
        conn.execute(
            "UPDATE evo_nodes SET is_latest=0, status='shadow' WHERE node_id=?", (ids["iso"],)
        )
        entries_mod.rebuild_index(conn)
        state = _entries_state(conn)
    assert state[ids["iso"]][1] == 0


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
    _seed_and_backfill()
    with fts.cursor() as conn:
        before = _evo_dump(conn)
        conn.execute("DELETE FROM evo_nodes")  # the disaster
    report = restore.import_from_markdown()
    assert report.ok, report.violations
    assert report.nodes == len(before)
    assert report.skipped_event_files == 1
    with fts.cursor() as conn:
        after = _evo_dump(conn)
        live = {
            r["id"] for r in conn.execute("SELECT id FROM entries WHERE superseded=0").fetchall()
        }
    assert after == before
    assert live

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

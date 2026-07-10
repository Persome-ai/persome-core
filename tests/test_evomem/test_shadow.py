"Tests for test shadow."

from __future__ import annotations

import json
from pathlib import Path

import pytest

from persome.evomem import backfill, integrity, shadow
from persome.evomem import store as evo_store
from persome.store import entries, fts


def _dump_evo(conn) -> list[tuple]:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(evo_nodes)")]
    rows = conn.execute(
        f"SELECT {', '.join(cols)} FROM evo_nodes ORDER BY node_id, user_id, agent_id"
    ).fetchall()
    return [tuple(r) for r in rows]


def _assert_incremental_equals_full_backfill() -> None:
    with fts.cursor() as conn:
        before = _dump_evo(conn)
    report = backfill.run_backfill()
    assert report.ok, (report.violations, report.heads_only_evo, report.heads_only_fts)
    with fts.cursor() as conn:
        after = _dump_evo(conn)
    assert before == after


def _node(conn, node_id: str):
    row = conn.execute(
        "SELECT * FROM evo_nodes WHERE node_id=? AND user_id='default' AND agent_id='default'",
        (node_id,),
    ).fetchone()
    return dict(row) if row else None


def _seed_baseline(name: str = "project-base.md") -> str:
    with fts.cursor() as conn:
        entries.create_file(conn, name=name, description="d", tags=["seed"])
        eid = entries.append_entry(conn, name=name, content="baseline fact", tags=["base"])
    report = backfill.run_backfill()
    assert report.ok
    shadow.reset_misses()
    return eid


def test_append_shadow_writes_node(ac_root: Path) -> None:
    _seed_baseline()
    with fts.cursor() as conn:
        eid = entries.append_entry(
            conn,
            name="project-base.md",
            content="new fact",
            tags=["alpha"],
            confidence="high",
            conflicted=True,
            occurred_at="2026-06-01 10:00",
        )
    with fts.cursor() as conn:
        node = _node(conn, eid)
    assert node is not None
    assert node["content"] == "new fact"
    assert node["status"] == "active"
    assert node["is_latest"] == 1
    assert json.loads(node["supersedes"]) == []
    assert json.loads(node["superseded_by"]) == []
    assert node["file_name"] == "project-base.md"
    assert node["tags"] == "alpha"
    assert node["confidence"] == "high"
    assert node["conflicted"] == 1
    assert node["occurred_at"] == "2026-06-01T10:00"
    assert node["valid_from"] is not None
    assert shadow.miss_count() == 0
    _assert_incremental_equals_full_backfill()


def test_supersede_shadow_links_chain(ac_root: Path) -> None:
    old = _seed_baseline()
    with fts.cursor() as conn:
        new = entries.supersede_entry(
            conn,
            name="project-base.md",
            old_entry_id=old,
            new_content="v2 fact",
            reason="updated",
            tags=["base"],
        )
    with fts.cursor() as conn:
        old_node, new_node = _node(conn, old), _node(conn, new)
    assert old_node["status"] == "shadow"
    assert old_node["is_latest"] == 0
    assert json.loads(old_node["superseded_by"]) == [new]
    assert new_node["status"] == "active"
    assert new_node["is_latest"] == 1
    assert json.loads(new_node["supersedes"]) == [old]
    assert shadow.miss_count() == 0
    _assert_incremental_equals_full_backfill()


def test_refined_from_update_shape(ac_root: Path) -> None:
    old = _seed_baseline(name="user-pref.md")
    with fts.cursor() as conn:
        new = entries.supersede_entry(
            conn,
            name="user-pref.md",
            old_entry_id=old,
            new_content="sharpened",
            reason="refined",
            refined_from=old,
        )
    with fts.cursor() as conn:
        new_node = _node(conn, new)
        old_node = _node(conn, old)
    assert new_node["refined_from"] == old
    assert json.loads(new_node["supersedes"]) == [old]
    assert old_node["status"] == "shadow"
    _assert_incremental_equals_full_backfill()


def test_delete_shadow_retires_node(ac_root: Path) -> None:
    eid = _seed_baseline(name="person-bob.md")
    with fts.cursor() as conn:
        entries.mark_entry_deleted(conn, name="person-bob.md", entry_id=eid)
    with fts.cursor() as conn:
        node = _node(conn, eid)
    assert node["status"] == "shadow"
    assert node["is_latest"] == 0
    assert json.loads(node["superseded_by"]) == []
    assert node["content"] == "baseline fact"
    assert node["valid_until"] is not None
    _assert_incremental_equals_full_backfill()


def test_abstract_shape(ac_root: Path) -> None:
    _seed_baseline(name="topic-merge.md")
    with fts.cursor() as conn:
        a = entries.append_entry(conn, name="topic-merge.md", content="part a", tags=[])
        b = entries.append_entry(conn, name="topic-merge.md", content="part b", tags=[])
        synth = entries.append_entry(
            conn,
            name="topic-merge.md",
            content="synthesis",
            tags=[f"abstracted-from:{a},{b}"],
        )
        entries.mark_entry_deleted(conn, name="topic-merge.md", entry_id=a)
        entries.mark_entry_deleted(conn, name="topic-merge.md", entry_id=b)
    with fts.cursor() as conn:
        synth_node = _node(conn, synth)
        a_node, b_node = _node(conn, a), _node(conn, b)
    assert json.loads(synth_node["abstracted_from"]) == [a, b]
    assert synth_node["status"] == "active" and synth_node["is_latest"] == 1
    assert "abstracted-from" not in synth_node["tags"]
    for src in (a_node, b_node):
        assert src["status"] == "shadow" and src["is_latest"] == 0
        assert json.loads(src["superseded_by"]) == []
    assert shadow.miss_count() == 0
    _assert_incremental_equals_full_backfill()


def test_mixed_sequence_invariant(ac_root: Path) -> None:
    v1 = _seed_baseline()
    with fts.cursor() as conn:
        v2 = entries.supersede_entry(
            conn, name="project-base.md", old_entry_id=v1, new_content="v2", reason="r"
        )
        v3 = entries.supersede_entry(
            conn,
            name="project-base.md",
            old_entry_id=v2,
            new_content="v3",
            reason="r",
            refined_from=v2,
        )
        extra = entries.append_entry(conn, name="project-base.md", content="extra", tags=[])
        entries.mark_entry_deleted(conn, name="project-base.md", entry_id=extra)
    with fts.cursor() as conn:
        assert _node(conn, v3)["is_latest"] == 1
        assert _node(conn, v2)["status"] == "shadow"
    assert shadow.miss_count() == 0
    _assert_incremental_equals_full_backfill()


def test_shadow_failure_never_rolls_back_main_write(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_baseline()

    def boom(*a, **k):  # noqa: ANN002, ANN003
        raise RuntimeError("shadow exploded")

    monkeypatch.setattr(evo_store, "upsert_node", boom)
    with fts.cursor() as conn:
        eid = entries.append_entry(conn, name="project-base.md", content="survives", tags=[])

        row = conn.execute("SELECT superseded FROM entries WHERE id=?", (eid,)).fetchone()
    assert row is not None and row["superseded"] == 0
    with fts.cursor() as conn:
        assert _node(conn, eid) is None
    assert shadow.miss_count() == 1


def test_miss_counter_alerts_at_threshold(ac_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_baseline()
    alerts: list[tuple] = []
    monkeypatch.setattr(integrity, "emit_alert", lambda *a, **k: alerts.append((a, k)))
    monkeypatch.setattr(evo_store, "upsert_node", lambda *a, **k: 1 / 0)
    with fts.cursor() as conn:
        for i in range(shadow._ALERT_EVERY):
            entries.append_entry(conn, name="project-base.md", content=f"m{i}", tags=[])
    assert shadow.miss_count() == shadow._ALERT_EVERY
    assert len(alerts) == 1
    (check, _detail), kwargs = alerts[0]
    assert check == "shadow_write_lag"
    assert kwargs["source"] == "shadow_write"
    assert kwargs["structural"] is False


def test_disabled_flag_means_no_shadow_and_no_misses(ac_root: Path) -> None:
    (ac_root / "config.toml").write_text("[evomem]\nshadow_write_enabled = false\n")
    with fts.cursor() as conn:
        entries.create_file(conn, name="project-off.md", description="d", tags=[])
        eid = entries.append_entry(conn, name="project-off.md", content="fact", tags=[])
        new = entries.supersede_entry(
            conn, name="project-off.md", old_entry_id=eid, new_content="v2", reason="r"
        )
        entries.mark_entry_deleted(conn, name="project-off.md", entry_id=new)

        assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 2
        evo_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='evo_nodes'"
        ).fetchone()
        if evo_exists:
            assert conn.execute("SELECT COUNT(*) FROM evo_nodes").fetchone()[0] == 0
    assert shadow.miss_count() == 0


def test_default_flag_is_on() -> None:
    from persome.config import EvomemConfig

    assert EvomemConfig().shadow_write_enabled is True


def test_event_prefix_exempt(ac_root: Path) -> None:
    _seed_baseline()
    with fts.cursor() as conn:
        entries.create_file(conn, name="event-2026-06-10.md", description="d", tags=[])
        eid = entries.append_entry(conn, name="event-2026-06-10.md", content="log", tags=[])
    with fts.cursor() as conn:
        assert _node(conn, eid) is None
    assert shadow.miss_count() == 0
    _assert_incremental_equals_full_backfill()


def test_cold_start_skip_when_backfill_never_ran(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alerts: list = []
    monkeypatch.setattr(integrity, "emit_alert", lambda *a, **k: alerts.append(a))
    monkeypatch.setattr(shadow, "_ALERT_EVERY", 1)
    with fts.cursor() as conn:
        entries.create_file(conn, name="project-cold.md", description="d", tags=[])
        eid = entries.append_entry(conn, name="project-cold.md", content="fact", tags=[])

        assert conn.execute("SELECT 1 FROM entries WHERE id=?", (eid,)).fetchone()
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='evo_nodes'"
        ).fetchone()
    assert shadow.miss_count() == 1
    assert alerts == []


def test_missing_predecessor_skips_whole_batch(ac_root: Path) -> None:
    _seed_baseline()
    real_upsert = evo_store.upsert_node
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(evo_store, "upsert_node", lambda *a, **k: 1 / 0)
        with fts.cursor() as conn:
            v1 = entries.append_entry(conn, name="project-base.md", content="v1", tags=[])
            v2 = entries.supersede_entry(
                conn, name="project-base.md", old_entry_id=v1, new_content="v2", reason="r"
            )
    assert evo_store.upsert_node is real_upsert
    shadow.reset_misses()
    with fts.cursor() as conn:
        v3 = entries.supersede_entry(
            conn, name="project-base.md", old_entry_id=v2, new_content="v3", reason="r"
        )
    with fts.cursor() as conn:
        assert _node(conn, v3) is None
        assert _node(conn, v2) is None

        assert conn.execute("SELECT 1 FROM entries WHERE id=?", (v3,)).fetchone()
        violations = [v for v in integrity.run_checks(conn) if v.structural]
    assert violations == []
    assert shadow.miss_count() == 1

    report = backfill.run_backfill()
    assert report.ok
    with fts.cursor() as conn:
        assert _node(conn, v3)["is_latest"] == 1
    _assert_incremental_equals_full_backfill()


def test_stale_mirror_pointer_skips_whole_batch(ac_root: Path) -> None:
    v1 = _seed_baseline()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(evo_store, "upsert_node", lambda *a, **k: 1 / 0)
        with fts.cursor() as conn:
            v2 = entries.supersede_entry(
                conn, name="project-base.md", old_entry_id=v1, new_content="v2", reason="r"
            )
    shadow.reset_misses()
    with fts.cursor() as conn:
        v3 = entries.supersede_entry(
            conn, name="project-base.md", old_entry_id=v2, new_content="v3", reason="r"
        )
    with fts.cursor() as conn:
        assert _node(conn, v3) is None
        assert json.loads(_node(conn, v1)["superseded_by"]) == []
        violations = [v for v in integrity.run_checks(conn) if v.structural]
    assert violations == []
    assert shadow.miss_count() == 1

    assert backfill.run_backfill().ok
    _assert_incremental_equals_full_backfill()


def test_compact_out_of_band_rewrite_records_visible_miss(ac_root: Path) -> None:
    _seed_baseline()
    shadow.note_out_of_band_rewrite(["project-base.md", "event-2026-06-10.md"])
    assert shadow.miss_count() == 1


def test_self_heal_when_old_node_absent_but_in_batch(ac_root: Path) -> None:
    _seed_baseline()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(evo_store, "upsert_node", lambda *a, **k: 1 / 0)
        with fts.cursor() as conn:
            v1 = entries.append_entry(conn, name="project-base.md", content="v1", tags=[])
    shadow.reset_misses()
    with fts.cursor() as conn:
        v2 = entries.supersede_entry(
            conn, name="project-base.md", old_entry_id=v1, new_content="v2", reason="r"
        )
    with fts.cursor() as conn:
        assert _node(conn, v1)["status"] == "shadow"
        assert json.loads(_node(conn, v2)["supersedes"]) == [v1]
    assert shadow.miss_count() == 0
    _assert_incremental_equals_full_backfill()

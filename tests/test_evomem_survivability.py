"Tests for test evomem survivability."

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import suppress
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from persome import paths
from persome.config import Config
from persome.evomem import backup, integrity
from persome.evomem.models import MemoryLayer, MemoryNode
from persome.evomem.store import NodeStore
from persome.store import entries, fts


@pytest.fixture(autouse=True)
def _reset_freeze():
    """The freeze flag is process-global — never leak it across tests."""
    integrity.unfreeze_writes()
    yield
    integrity.unfreeze_writes()


@pytest.fixture
def alerts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict]]:
    """Capture structured integrity alerts while retaining the real log call."""
    captured: list[tuple[str, str, dict]] = []
    original = integrity.emit_alert

    def _capture(check, detail, *, source, structural=False, frozen=False):
        captured.append(
            (
                "integrity",
                "integrity_alert",
                {
                    "check": check,
                    "detail": detail,
                    "source": source,
                    "structural": structural,
                    "frozen": frozen,
                },
            )
        )
        original(
            check,
            detail,
            source=source,
            structural=structural,
            frozen=frozen,
        )

    monkeypatch.setattr(integrity, "emit_alert", _capture)
    return captured


def _insert_evo(
    conn: sqlite3.Connection,
    node_id: str,
    *,
    supersedes: tuple[str, ...] = (),
    superseded_by: tuple[str, ...] = (),
    is_latest: int = 1,
    status: str = "active",
) -> None:
    conn.execute(
        "INSERT INTO evo_nodes (node_id, user_id, agent_id, content, layer, supersedes,"
        " superseded_by, is_latest, status, memory_at, gmt_created)"
        " VALUES (?, 'default', 'default', 'c', 'l2_fact', ?, ?, ?, ?, NULL, NULL)",
        (
            node_id,
            json.dumps(list(supersedes)),
            json.dumps(list(superseded_by)),
            is_latest,
            status,
        ),
    )


@pytest.fixture
def evo_table(ac_root: Path) -> Path:
    """Create the evo_nodes table (NodeStore DDL) inside the tmp root."""
    NodeStore()
    return ac_root


def _checks_named(violations: list[integrity.Violation], name: str) -> list[integrity.Violation]:
    return [v for v in violations if v.check == name]


# ─── self-check: happy paths ─────────────────────────────────────────────────


def test_checks_pass_on_fresh_root(ac_root: Path) -> None:
    with fts.cursor() as conn:
        assert integrity.run_checks(conn) == []


def test_checks_pass_on_real_writes(evo_table: Path) -> None:
    with fts.cursor() as conn:
        entries.create_file(conn, name="project-x.md", description="d", tags=[])
        e1 = entries.append_entry(conn, name="project-x.md", content="v1", tags=[])
        entries.supersede_entry(
            conn, name="project-x.md", old_entry_id=e1, new_content="v2", reason="r"
        )
        entries.append_entry(conn, name="project-x.md", content="other", tags=[])

    store = NodeStore()
    a = MemoryNode(node_id="n-a", content="a", layer=MemoryLayer.L2_FACT)
    store.save(a)
    b = MemoryNode(node_id="n-b", content="b", layer=MemoryLayer.L2_FACT)
    store.save_and_supersede(b, old_id="n-a")

    with fts.cursor() as conn:
        violations = integrity.run_checks(conn)

    assert [v for v in violations if v.structural] == []


# ─── self-check: violations (checks 2–5, evo edition) ───────────────────────


def test_pointer_asymmetry_detected(evo_table: Path) -> None:
    with fts.cursor() as conn:
        _insert_evo(conn, "old", superseded_by=("new",), is_latest=0, status="shadow")
        _insert_evo(conn, "new", supersedes=())  # forgot the back-pointer
        violations = integrity.run_checks(conn)
    found = _checks_named(violations, "pointer_symmetry")
    assert found and all(v.structural for v in found)


def test_dangling_pointer_detected(evo_table: Path) -> None:
    with fts.cursor() as conn:
        _insert_evo(conn, "a", superseded_by=("ghost",), is_latest=0, status="shadow")
        violations = integrity.run_checks(conn)
    found = _checks_named(violations, "pointer_symmetry")
    assert found and "dangling" in found[0].detail


def test_anti_fork_detected(evo_table: Path) -> None:
    with fts.cursor() as conn:
        _insert_evo(conn, "a", superseded_by=("b", "c"), is_latest=0, status="shadow")
        _insert_evo(conn, "b", supersedes=("a",))
        _insert_evo(conn, "c", supersedes=("a",))
        violations = integrity.run_checks(conn)
    assert _checks_named(violations, "anti_fork")


def test_head_with_successor_detected(evo_table: Path) -> None:
    with fts.cursor() as conn:
        # "old" still claims is_latest=1 although it has a successor.
        _insert_evo(conn, "old", superseded_by=("new",), is_latest=1, status="active")
        _insert_evo(conn, "new", supersedes=("old",), is_latest=1, status="active")
        violations = integrity.run_checks(conn)
    found = _checks_named(violations, "head_consistency")
    assert found
    # Both faces of check 4 fire: bad head AND two heads on one chain.
    details = " | ".join(v.detail for v in found)
    assert "successor" in details and ">1 head" in details


def test_shadow_head_detected(evo_table: Path) -> None:
    with fts.cursor() as conn:
        _insert_evo(conn, "z", is_latest=1, status="shadow")
        violations = integrity.run_checks(conn)
    assert _checks_named(violations, "head_consistency")


def test_cycle_detected(evo_table: Path) -> None:
    with fts.cursor() as conn:
        _insert_evo(
            conn, "a", supersedes=("b",), superseded_by=("b",), is_latest=0, status="shadow"
        )
        _insert_evo(
            conn, "b", supersedes=("a",), superseded_by=("a",), is_latest=0, status="shadow"
        )
        violations = integrity.run_checks(conn)
    assert _checks_named(violations, "acyclicity")


def test_malformed_pointer_json_detected(evo_table: Path) -> None:
    with fts.cursor() as conn:
        conn.execute(
            "INSERT INTO evo_nodes (node_id, user_id, agent_id, content, layer, supersedes,"
            " superseded_by, is_latest, status)"
            " VALUES ('bad', 'default', 'default', 'c', 'l2_fact', 'NOT-JSON', '[]', 1, 'active')"
        )
        violations = integrity.run_checks(conn)
    assert _checks_named(violations, "pointer_parse")


def test_evo_projection_skipped_while_evo_empty(ac_root: Path) -> None:
    with fts.cursor() as conn:
        entries.create_file(conn, name="project-z.md", description="d", tags=[])
        entries.append_entry(conn, name="project-z.md", content="x", tags=[])
        violations = integrity.run_checks(conn)
    assert violations == []


def test_evo_projection_mismatch_detected_when_nonempty(evo_table: Path) -> None:
    with fts.cursor() as conn:
        entries.create_file(conn, name="project-w.md", description="d", tags=[])
        entries.append_entry(conn, name="project-w.md", content="x", tags=[])
        entries.append_entry(conn, name="project-w.md", content="y", tags=[])
        _insert_evo(conn, "lonely-head")  # 1 active head vs 2 live entries
        violations = integrity.run_checks(conn)
    found = _checks_named(violations, "projection_reconciliation")
    assert found and not found[0].structural


# ─── check 1 (physical) + orchestration ──────────────────────────────────────


def test_quick_check_on_garbage_db(ac_root: Path, alerts: list) -> None:
    garbage = ac_root / "garbage.db"
    garbage.write_bytes(b"this is not a sqlite database at all" * 64)
    violations = integrity.check_and_handle(source="test", db_path=garbage)
    assert _checks_named(violations, "quick_check")
    assert any(p["check"] == "quick_check" for _, _, p in alerts)
    # No freeze: freeze_on_failure defaulted to False.
    assert integrity.write_frozen() is None


def test_structural_failure_alerts_but_does_not_freeze_by_default(
    evo_table: Path, alerts: list
) -> None:
    with fts.cursor() as conn:
        # head-consistency violation: a node with a successor still claims head.
        _insert_evo(conn, "old", superseded_by=("new",), is_latest=1, status="active")
        _insert_evo(conn, "new", supersedes=("old",), is_latest=1, status="active")
    violations = integrity.check_and_handle(source="test", freeze_on_failure=False)
    assert any(v.structural for v in violations)
    assert integrity.write_frozen() is None
    published = [(s, t, p) for s, t, p in alerts if t == "integrity_alert"]
    assert published and all(s == "integrity" for s, _, _ in published)
    assert all(p["frozen"] is False for _, _, p in published)


def test_structural_failure_freezes_when_configured(evo_table: Path, alerts: list) -> None:
    with fts.cursor() as conn:
        _insert_evo(conn, "old", superseded_by=("new",), is_latest=1, status="active")
        _insert_evo(conn, "new", supersedes=("old",), is_latest=1, status="active")
        entries.create_file(conn, name="project-f.md", description="d", tags=[])

    integrity.check_and_handle(source="test", freeze_on_failure=True)
    assert integrity.write_frozen() is not None
    assert any(p["frozen"] is True for _, _, p in alerts)

    # The freeze seam rejects every write entry point; reads stay available.
    with fts.cursor() as conn:
        with pytest.raises(integrity.WriteFrozenError):
            entries.append_entry(conn, name="project-f.md", content="x", tags=[])
        with pytest.raises(integrity.WriteFrozenError):
            entries.create_file(conn, name="project-g.md", description="d", tags=[])
        assert fts.search(conn, query="anything") == []  # read path untouched

    store = NodeStore()
    with pytest.raises(integrity.WriteFrozenError):
        store.save(MemoryNode(node_id="n", content="c", layer=MemoryLayer.L2_FACT))

    # No auto-recovery — only the explicit human button clears it.
    integrity.unfreeze_writes()
    with fts.cursor() as conn:
        entries.append_entry(conn, name="project-f.md", content="after thaw", tags=[])


def test_projection_violation_never_freezes(evo_table: Path, alerts: list) -> None:
    with fts.cursor() as conn:
        entries.create_file(conn, name="project-p.md", description="d", tags=[])
        entries.append_entry(conn, name="project-p.md", content="x", tags=[])
        entries.append_entry(conn, name="project-p.md", content="y", tags=[])
        _insert_evo(conn, "lonely-head")  # 1 active head vs 2 live entries → check 6
    violations = integrity.check_and_handle(source="test", freeze_on_failure=True)
    assert violations and not any(v.structural for v in violations)
    assert integrity.write_frozen() is None


def test_inject_violation_exercises_alert_pipeline(ac_root: Path, alerts: list) -> None:
    fake = integrity.Violation("drill", "manual alert drill", structural=False)
    violations = integrity.check_and_handle(source="drill", inject_violation=fake)
    assert fake in violations
    assert any(
        t == "integrity_alert" and p["check"] == "drill" and p["source"] == "drill"
        for _, t, p in alerts
    )


def test_startup_check_gating(ac_root: Path) -> None:
    cfg = Config()
    cfg.evomem.integrity_check_enabled = False
    assert integrity.startup_check(cfg) is None
    cfg.evomem.integrity_check_enabled = True
    assert integrity.startup_check(cfg) == []


def test_freeze_default_off_in_config() -> None:
    assert Config().evomem.freeze_writes_on_failure is False


# ─── snapshots (§3.2) ────────────────────────────────────────────────────────


def test_snapshot_roundtrip(ac_root: Path) -> None:
    with fts.cursor() as conn:
        entries.create_file(conn, name="project-s.md", description="d", tags=[])
        entries.append_entry(conn, name="project-s.md", content="snapshot-marker", tags=[])
    dest = backup.run_daily_backup(Config())
    assert dest is not None and dest.exists()
    assert dest.parent == paths.backup_dir()
    # The snapshot is a self-contained, openable DB carrying the data.
    snap = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
    try:
        rows = snap.execute("SELECT content FROM entries").fetchall()
    finally:
        snap.close()
    assert any("snapshot-marker" in r[0] for r in rows)
    # No tmp residue.
    assert list(paths.backup_dir().glob("*.tmp")) == []


def test_snapshot_same_day_rerun_replaces_atomically(ac_root: Path) -> None:
    cfg = Config()
    first = backup.run_daily_backup(cfg)
    with fts.cursor() as conn:
        entries.create_file(conn, name="project-t.md", description="d", tags=[])
        entries.append_entry(conn, name="project-t.md", content="second-pass", tags=[])
    second = backup.run_daily_backup(cfg)
    assert first == second
    assert len(list(paths.backup_dir().glob("evo-*.db"))) == 1
    snap = sqlite3.connect(f"file:{second}?mode=ro", uri=True)
    try:
        rows = snap.execute("SELECT content FROM entries").fetchall()
    finally:
        snap.close()
    assert any("second-pass" in r[0] for r in rows)


def test_bad_snapshot_alerts_and_never_overwrites_good_one(
    ac_root: Path, alerts: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = Config()
    good = backup.run_daily_backup(cfg)
    assert good is not None
    good_bytes = good.read_bytes()

    # Next snapshot attempt fails verification — must alert and keep the old one.
    monkeypatch.setattr(
        integrity,
        "verify_snapshot",
        lambda path: [integrity.Violation("quick_check", "simulated corruption", True)],
    )
    with fts.cursor() as conn:
        entries.create_file(conn, name="project-u.md", description="d", tags=[])
        entries.append_entry(conn, name="project-u.md", content="newer data", tags=[])
    assert backup.create_snapshot() is None
    assert good.read_bytes() == good_bytes  # good snapshot untouched
    assert list(paths.backup_dir().glob("*.tmp")) == []  # bad tmp discarded
    assert any(
        t == "integrity_alert" and p["check"] == "snapshot_verification" for _, t, p in alerts
    )


def test_structural_only_snapshot_tolerates_alert_only_findings(
    ac_root: Path, alerts: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    alert_only = [integrity.Violation("projection_reconciliation", "count drift", False)]
    monkeypatch.setattr(integrity, "verify_snapshot", lambda path: list(alert_only))
    # Default (daily-tick) semantics: any violation rejects the snapshot.
    assert backup.create_snapshot() is None
    # Pre-change (backfill) semantics: alert-only findings alert but don't reject.
    dest = backup.create_snapshot(structural_only=True)
    assert dest is not None and dest.exists()
    assert any(
        t == "integrity_alert" and p["check"] == "snapshot_verification" for _, t, p in alerts
    )
    # Structural violations always reject, structural_only or not.
    monkeypatch.setattr(
        integrity,
        "verify_snapshot",
        lambda path: [integrity.Violation("quick_check", "corrupt", True), *alert_only],
    )
    before = dest.read_bytes()
    assert backup.create_snapshot(structural_only=True) is None
    assert dest.read_bytes() == before  # good snapshot preserved


def test_retention_policy(ac_root: Path) -> None:
    now = datetime(2026, 6, 10, 23, 55)
    today = now.date()
    bdir = paths.backup_dir()
    bdir.mkdir(parents=True, exist_ok=True)

    def mk(d: date) -> Path:
        p = bdir / f"evo-{d.strftime('%Y%m%d')}.db"
        p.write_bytes(b"x")
        return p

    candidates = [today - timedelta(days=i) for i in range(40)]
    keep_expected: list[Path] = []
    drop_expected: list[Path] = []
    for d in candidates:
        p = mk(d)
        age = (today - d).days
        if age < 7 or (d.weekday() == 0 and age < 28):
            keep_expected.append(p)
        else:
            drop_expected.append(p)
    future = mk(today + timedelta(days=1))
    unrelated = bdir / "evo-junk.db"
    unrelated.write_bytes(b"x")

    # Sanity: the synthetic window really contains both weekly keepers and drops.
    assert any(d.weekday() == 0 and 7 <= (today - d).days < 28 for d in candidates)
    assert drop_expected

    removed = backup.apply_retention(keep_daily=7, keep_weekly=4, now=now)
    assert set(removed) == set(drop_expected)
    for p in [*keep_expected, future]:
        assert p.exists(), p.name
    assert unrelated.exists()


class _FakeManager:
    def force_end(self, reason: str) -> None:  # noqa: ARG002
        return None


async def _run_one_tick(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, calls: dict[str, int]
) -> None:
    from persome.session import tick as tick_mod

    monkeypatch.setattr(tick_mod, "_seconds_until_next_local", lambda h, m: 0.001)

    def fake_checkpoint(mode: str = "TRUNCATE") -> tuple[int, int, int]:
        calls["checkpoint"] += 1
        return (0, 0, 0)

    def fake_backup(cfg_, **kw) -> None:  # noqa: ANN003, ARG001
        calls["backup"] += 1

    def fake_check(**kw) -> list:  # noqa: ANN003, ARG001
        calls["integrity"] += 1
        return []

    monkeypatch.setattr(tick_mod.fts, "checkpoint", fake_checkpoint)
    monkeypatch.setattr(tick_mod.evo_backup, "run_daily_backup", fake_backup)
    monkeypatch.setattr(tick_mod.evo_integrity, "check_and_handle", fake_check)

    task = asyncio.create_task(tick_mod.run_daily_safety_net(cfg, _FakeManager()))
    for _ in range(300):
        await asyncio.sleep(0.01)
        if calls["checkpoint"] >= 2:
            break
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def test_daily_tick_runs_checkpoint_snapshot_and_check(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = Config()
    cfg.reducer.enabled = False  # skip the reducer catch-up sleep
    calls = {"checkpoint": 0, "backup": 0, "integrity": 0}
    await _run_one_tick(cfg, monkeypatch, calls)
    assert calls["checkpoint"] >= 1
    assert calls["backup"] >= 1
    assert calls["integrity"] >= 1


async def test_daily_tick_p0_byte_equivalent_when_disabled(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = Config()
    cfg.reducer.enabled = False
    cfg.evomem.snapshot_enabled = False
    cfg.evomem.integrity_check_enabled = False
    calls = {"checkpoint": 0, "backup": 0, "integrity": 0}
    await _run_one_tick(cfg, monkeypatch, calls)
    assert calls["checkpoint"] >= 1
    assert calls["backup"] == 0
    assert calls["integrity"] == 0


def test_wal_checkpoint_truncate_works(ac_root: Path) -> None:
    with fts.cursor() as conn:
        entries.create_file(conn, name="project-c.md", description="d", tags=[])
        entries.append_entry(conn, name="project-c.md", content="x", tags=[])
    busy, log_pages, ckpt = fts.checkpoint("TRUNCATE")
    assert busy == 0
    assert log_pages >= 0 and ckpt >= 0

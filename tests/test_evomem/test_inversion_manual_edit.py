"""Tests for manual-edit detection, import, and live projection."""

from __future__ import annotations

from pathlib import Path

import pytest

from persome.evomem import integrity as evo_integrity
from persome.evomem import inversion as evo_inversion
from persome.store import entries, fts
from persome.store import files as files_mod


@pytest.fixture(autouse=True)
def _quiet() -> None:
    evo_inversion.reset_misses()


def _evomem(root: Path) -> None:
    (root / "config.toml").write_text('[evomem]\nwrite_authority = "evomem"\n')


def _seed(conn, name: str = "project-e.md") -> str:
    entries.create_file(conn, name=name, description="d", tags=[])
    return entries.append_entry(conn, name=name, content="truth fact", tags=["a"])


def test_manual_edit_detected_and_alerted(ac_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _evomem(ac_root)
    alerts: list[tuple] = []
    monkeypatch.setattr(evo_integrity, "emit_alert", lambda *a, **k: alerts.append(a))
    with fts.cursor() as conn:
        _seed(conn)
        assert evo_inversion.check_manual_edits(conn) == []
        path = files_mod.memory_path("project-e.md")
        path.write_text(path.read_text() + "\n\u624b\u5199\u7684\u4e00\u884c\n")
        findings = evo_inversion.check_manual_edits(conn)
    assert findings == [{"file": "project-e.md", "kind": "modified"}]
    assert alerts and alerts[0][0] == "manual_edit_detected"


def test_manual_edit_check_flags_deleted_projection(ac_root: Path) -> None:
    _evomem(ac_root)
    with fts.cursor() as conn:
        _seed(conn)
        files_mod.memory_path("project-e.md").unlink()
        findings = evo_inversion.check_manual_edits(conn)
    assert findings == [{"file": "project-e.md", "kind": "missing"}]


def test_daily_check_noop_under_markdown_authority(ac_root: Path) -> None:
    assert evo_inversion.run_daily_manual_edit_check() == []


def test_projection_lag_is_not_a_manual_edit(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _evomem(ac_root)
    with fts.cursor() as conn:
        _seed(conn)

        with monkeypatch.context() as mp:
            mp.setattr(
                files_mod,
                "atomic_write_text",
                lambda *a, **k: (_ for _ in ()).throw(OSError("x")),
            )
            entries.append_entry(conn, name="project-e.md", content="lagging", tags=[])
        assert evo_inversion.check_manual_edits(conn) == []


def test_import_markdown_plain_addition(ac_root: Path) -> None:
    _evomem(ac_root)
    with fts.cursor() as conn:
        _seed(conn)
        path = files_mod.memory_path("project-e.md")
        path.write_text(
            path.read_text().rstrip()
            + "\n\n## [2026-06-11T11:00] {id: 20260611-1100-abcdef} #hand\n\u624b\u5199\u7684\u65b0\u4e8b\u5b9e\n"
        )
        report = evo_inversion.import_markdown_file(conn, "project-e.md")
        assert report.imported == ["20260611-1100-abcdef"]
        assert report.conflicts == [] and report.reprojected
        node = conn.execute(
            "SELECT * FROM evo_nodes WHERE node_id='20260611-1100-abcdef'"
        ).fetchone()
        row = conn.execute("SELECT * FROM entries WHERE id='20260611-1100-abcdef'").fetchone()
        assert node is not None and node["tags"] == "hand"
        assert row is not None and row["superseded"] == 0

        assert evo_inversion.check_manual_edits(conn) == []


def test_import_markdown_modified_existing_entry_reports_conflict(ac_root: Path) -> None:
    _evomem(ac_root)
    with fts.cursor() as conn:
        eid = _seed(conn)
        path = files_mod.memory_path("project-e.md")
        path.write_text(
            path.read_text().replace("truth fact", "\u7528\u6237\u6539\u5199\u4e86\u771f\u76f8")
        )
        before = path.read_text()
        report = evo_inversion.import_markdown_file(conn, "project-e.md")
        assert report.imported == []
        assert report.conflicts and not report.reprojected
        assert path.read_text() == before

        node = conn.execute("SELECT content FROM evo_nodes WHERE node_id=?", (eid,)).fetchone()
        assert node["content"] == "truth fact"

        assert evo_inversion.check_manual_edits(conn)


def test_import_markdown_refuses_under_markdown_authority(ac_root: Path) -> None:
    with fts.cursor() as conn:
        _seed(conn)
        with pytest.raises(RuntimeError, match="rebuild-index"):
            evo_inversion.import_markdown_file(conn, "project-e.md")


def test_project_live_all_repairs_lag_and_skips_event(ac_root: Path) -> None:
    _evomem(ac_root)
    with fts.cursor() as conn:
        _seed(conn)
        entries.create_file(conn, name="event-2026-06-11.md", description="day", tags=[])
        entries.append_entry(conn, name="event-2026-06-11.md", content="[10:00] x", tags=[])

        files_mod.memory_path("project-e.md").write_text("damaged")
        event_before = files_mod.memory_path("event-2026-06-11.md").read_text()
        names = evo_inversion.project_live_all(conn)
    assert "project-e.md" in names and "event-2026-06-11.md" not in names
    text = files_mod.memory_path("project-e.md").read_text()
    assert "truth fact" in text and "projected:" in text
    assert files_mod.memory_path("event-2026-06-11.md").read_text() == event_before

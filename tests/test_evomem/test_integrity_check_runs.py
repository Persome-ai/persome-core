"Tests for test integrity check runs."

from __future__ import annotations

from persome.evomem import integrity
from persome.store import fts


def test_check_and_handle_records_a_run(ac_root) -> None:
    integrity.check_and_handle(source="test")
    with fts.cursor() as conn:
        row = integrity.last_check_run(conn)
    assert row is not None
    assert row["source"] == "test"
    assert row["violation_count"] == 0


def test_injected_drill_violation_not_counted(ac_root) -> None:
    fake = integrity.Violation("drill", "injected for alert-channel drill", structural=False)
    violations = integrity.check_and_handle(source="drill", inject_violation=fake)
    assert any(v.check == "drill" for v in violations)
    with fts.cursor() as conn:
        row = integrity.last_check_run(conn)
    assert row is not None
    assert row["violation_count"] == 0

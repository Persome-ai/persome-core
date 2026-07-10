"""自检审计账（integrity_check_runs）——原 §4.3 判据 2 数据源，PR-7 起为纯审计。

覆盖：每次 check_and_handle 落一行真实发现数；注入的报警演练 violation 不入账
（演练不许污染审计账）。判据聚合/streak/仪表盘已随双读对账在 PR-7 退役。
"""

from __future__ import annotations

from persome.evomem import integrity
from persome.store import fts


def test_check_and_handle_records_a_run(ac_root) -> None:
    """每次自检在 integrity_check_runs 落一行（干净库 → 0 发现）。"""
    integrity.check_and_handle(source="test")
    with fts.cursor() as conn:
        row = integrity.last_check_run(conn)
    assert row is not None
    assert row["source"] == "test"
    assert row["violation_count"] == 0


def test_injected_drill_violation_not_counted(ac_root) -> None:
    """报警通路演练注入走完整报警 pipeline，但**不**计入审计账。"""
    fake = integrity.Violation("drill", "injected for alert-channel drill", structural=False)
    violations = integrity.check_and_handle(source="drill", inject_violation=fake)
    assert any(v.check == "drill" for v in violations)  # 注入确实走完了通路
    with fts.cursor() as conn:
        row = integrity.last_check_run(conn)
    assert row is not None
    assert row["violation_count"] == 0  # 审计账只记真实发现

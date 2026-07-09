"""Reverse-loop P2 (spec 2026-06-26 G4): the content-free ``outcomes`` ledger +
the ≥N-gated per-kind execution success rate.

The forward path knows what was *recognized* and *accepted*; it never learns
whether the proactive follow-up / supervised run actually LANDED. ``outcomes`` is
that missing channel — one content-free row per finished execution — and
``kind_success_rate`` turns it into a per-KIND success prior, gated on ≥N samples
so a data-starved kind can never drive a decision (the data-hunger red line:
UNDECIDABLE beats a fake rate).

These pin the load-bearing contracts:
  * round-trip + success-rate math + worst-first ordering,
  * the ≥``min_samples`` gate drops a starved kind entirely,
  * the lookback window excludes stale rows (anchored on ``ts``),
  * read-only safety: a missing table fails open to ``[]`` (no write, no raise),
  * the **content-free red line** at the API boundary — ``POST /outcomes`` lands
    only the fixed enum/bool/count/duration columns and 422s any extra field,
  * ``feedback-report`` stays UNDECIDABLE until a kind reaches the floor.

Deterministic, zero-LLM, zero-network.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from persome import paths
from persome.api import build_api_app
from persome.config import load as load_config
from persome.feedback import report as fb_report
from persome.store import fts
from persome.store import outcomes as outcomes_store


def _since(days: int) -> str:
    return (datetime.now().astimezone() - timedelta(days=days)).isoformat(timespec="seconds")


def _ts(days_ago: float) -> str:
    return (datetime.now().astimezone() - timedelta(days=days_ago)).isoformat(timespec="seconds")


# ── 1. round-trip + success-rate math + worst-first ordering
def test_success_rate_math_and_ordering(ac_root):
    with fts.cursor() as conn:
        # meeting: 6 rows, 3 success → 0.50
        for i in range(6):
            outcomes_store.insert_outcome(
                conn, kind="meeting", status="followup", success=(i % 2 == 0)
            )
        # calendar: 5 rows, 1 success → 0.20 (worse → must sort first)
        for i in range(5):
            outcomes_store.insert_outcome(
                conn, kind="calendar", status="supervised", success=(i == 0)
            )
        rows = outcomes_store.kind_success_rate(conn, since=_since(7), min_samples=5)

    assert [r[0] for r in rows] == ["calendar", "meeting"]  # worst-first
    by_kind = {r[0]: r for r in rows}
    assert by_kind["meeting"][1:] == (6, 3, 0.5)
    assert by_kind["calendar"][1:] == (5, 1, 0.2)


# ── 2. the ≥N gate drops a starved kind ENTIRELY (UNDECIDABLE, never noise)
def test_min_samples_gate_drops_starved_kind(ac_root):
    with fts.cursor() as conn:
        for _ in range(5):
            outcomes_store.insert_outcome(conn, kind="meeting", status="followup", success=True)
        for _ in range(4):  # below the floor
            outcomes_store.insert_outcome(conn, kind="reminder", status="followup", success=True)
        rows = outcomes_store.kind_success_rate(conn, since=_since(7), min_samples=5)

    assert {r[0] for r in rows} == {"meeting"}  # reminder gated out wholesale


# ── 3. lookback window excludes stale rows (anchored on ts)
def test_lookback_excludes_stale_rows(ac_root):
    with fts.cursor() as conn:
        for _ in range(3):  # fresh
            outcomes_store.insert_outcome(
                conn, kind="meeting", status="followup", success=True, ts=_ts(1)
            )
        for _ in range(3):  # old — outside a 7-day window
            outcomes_store.insert_outcome(
                conn, kind="meeting", status="followup", success=False, ts=_ts(30)
            )
        # min_samples=3 so the window is the only thing that can gate it
        rows = outcomes_store.kind_success_rate(conn, since=_since(7), min_samples=3)

    assert len(rows) == 1
    assert rows[0][1:] == (3, 3, 1.0)  # only the 3 fresh successes counted


# ── 4. read-only safe: missing table fails open to [] (no write, no raise)
def test_missing_table_fails_open_readonly(tmp_path):
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()  # exists, but no `outcomes` table
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        assert outcomes_store.kind_success_rate(conn, since=_since(7)) == []
    finally:
        conn.close()


# ── 5. CONTENT-FREE RED LINE: POST /outcomes lands only the fixed columns
def test_post_outcomes_lands_content_free_row(ac_root):
    client = TestClient(build_api_app(load_config()))
    resp = client.post(
        "/outcomes",
        json={
            "kind": "meeting",
            "status": "followup",
            "success": True,
            "intent_id": 42,
            "executor_tier": "tier2",
            "artifact_verified": True,
            "placed": False,
            "elapsed_ms": 1234,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["success"] is True and body["outcome_id"] > 0

    # The row is exactly what we sent — and the COLUMN SET is the content-free
    # fixed schema (zero free-text/body columns can exist to smuggle text).
    conn = sqlite3.connect(f"file:{paths.index_db()}?mode=ro", uri=True)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(outcomes)").fetchall()}
        row = conn.execute(
            "SELECT kind, status, success, intent_id, executor_tier, artifact_verified,"
            " placed, elapsed_ms FROM outcomes WHERE id = ?",
            (body["outcome_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert cols == {
        "id",
        "ts",
        "intent_id",
        "kind",
        "status",
        "success",
        "executor_tier",
        "artifact_verified",
        "placed",
        "awaited_confirm",
        "reschedule_suggested",
        "elapsed_ms",
        "created_at",
    }
    assert row == ("meeting", "followup", 1, 42, "tier2", 1, 0, 1234)


# ── 6. content-free guard at the boundary: an extra (text-bearing) field 422s
def test_post_outcomes_rejects_extra_field(ac_root):
    client = TestClient(build_api_app(load_config()))
    resp = client.post(
        "/outcomes",
        json={
            "kind": "meeting",
            "status": "followup",
            "success": True,
            "artifact_text": "the meeting notes body that must never ride here",
        },
    )
    assert resp.status_code == 422  # extra="forbid" — text can't be smuggled


# ── 7. feedback-report: UNDECIDABLE until a kind reaches the floor, then surfaces
def test_feedback_report_followup_section(ac_root):
    logs = paths.logs_dir()

    # No outcomes yet → UNDECIDABLE, and the render must say so (never a fake rate).
    fs = fb_report.followup_success(logs)
    assert fs["decidable"] is False
    assert "G4 每 kind 执行成功率" in fb_report.render_text(logs)
    assert "UNDECIDABLE" in fb_report.render_text(logs)

    with fts.cursor() as conn:
        for i in range(5):
            outcomes_store.insert_outcome(conn, kind="calendar", status="followup", success=(i < 4))

    fs = fb_report.followup_success(logs)
    assert fs["decidable"] is True
    assert fs["rows"] == [{"kind": "calendar", "n": 5, "successes": 4, "rate": 0.8}]
    assert "success-rate 0.80" in fb_report.render_text(logs)


# ── 8. bounded telemetry: the daily prune hook keeps `outcomes` from growing forever
def test_prune_telemetry_bounds_outcomes(ac_root):
    """``_prune_telemetry_tables`` must cap ``outcomes`` at ``prune``'s keep count.

    Regression for the #378 #508-style无界增长 bug: ``outcomes`` shipped no
    ``prune`` and was not wired into the daily retention hook, unlike its sibling
    per-event ledger ``intent_fold_ticks`` — so on a long-running install every
    accepted follow-up / supervised finish accumulated forever even though
    ``kind_success_rate`` only ever reads a ``since`` window. This seeds more rows
    than the keep count and asserts the daily hook actually trims it.
    """
    from persome.session import tick as session_tick

    keep = 50000
    overflow = 5  # rows beyond `keep` that must be pruned away
    total = keep + overflow
    with fts.cursor() as conn:
        outcomes_store.ensure_schema(conn)
        # Bulk-insert directly (the per-row insert path is exercised above); we
        # only need the table over-full so the prune has something to cut.
        conn.executemany(
            "INSERT INTO outcomes (ts, kind, status, success, created_at) "
            "VALUES (?, 'meeting', 'followup', 1, ?)",
            [(f"2026-01-01T00:{i % 60:02d}:00", "2026-01-01T00:00:00") for i in range(total)],
        )
        conn.commit()
        before = conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]
    assert before == total

    deleted = session_tick._prune_telemetry_tables()

    # The hook reported a non-zero prune for outcomes (it was wired in)…
    assert deleted.get("outcomes") == overflow
    # …and the table is now bounded at `keep`.
    with fts.cursor() as conn:
        after = conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]
    assert after == keep

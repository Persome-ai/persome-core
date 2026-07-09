"""Regression tests for issue #626.

``intent.store.same_fact_cross_form`` (the #549 cross-activation-form dedup
lookup, armed↔immediate) had **no liveness guard**. #525 gave the same-form
dedup choke point a windowed, liveness-aware match (``find_live_duplicate`` /
``_same_occurrence``: a stale ``expired`` / overdue ``open``/``armed`` prior row
stops suppressing a fresh occurrence), but the cross-form leg queried the whole
table for ANY-status match and returned it unconditionally.

The consequence is the same systematic silent-miss the #525 fix closed for the
same-form path, but across forms: a recurring commitment ("每周三 standup")
recognized once as an ``armed`` L7 row keeps the SAME base ``dedup_key`` every
week (the relative-weekday token carries no anchored date), so once a stale
armed/immediate row exists, every later same-fact occurrence recognized in the
OTHER form is folded → skipped → never inserted, forever.

The fix reuses the established lifecycle judgment ``store.is_expired``: a
candidate cross-form row that is ``expired`` or an overdue ``open``/``armed`` row
(its grounded deadline came and went, or its recognition aged out) is the
PREVIOUS occurrence wrapping up — it no longer suppresses a fresh recognition,
so the new occurrence inserts. ``consumed`` / ``dismissed`` are final user
feedback (not a stale-lifecycle state, so ``is_expired`` reports them live) and
KEEP suppressing — "dismissed 永不复活" is preserved.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from persome.intent import sink
from persome.intent import store as intent_store
from persome.intent.ontology import Intent
from persome.store import fts


def _now() -> datetime:
    return datetime.now()


def _meeting(
    *,
    armed: bool,
    ts: str,
    valid_until: str | None = None,
    resolved_at: str | None = None,
    confidence: float = 0.7,
) -> Intent:
    """同一事实（周三下午3点 standup）的两种激活形式。

    The relative-weekday ``when_text`` keeps the SAME base ``dedup_key`` across
    weeks, so a stale prior occurrence must not permanently suppress a fresh one.
    """
    return Intent(
        kind="meeting",
        scope="session-x",
        confidence=confidence,
        rationale="用户确认每周三下午3点 standup",
        ts=ts,
        payload={"when_text": "周三下午3点", "with": ["团队"], "provenance": "user_committed"},
        fire_on="app_opened" if armed else "",
        fire_config={"app": "飞书"} if armed else {},
        resolved_at=resolved_at,
        valid_until=valid_until,
    )


def _count(conn) -> int:  # noqa: ANN001
    return conn.execute("SELECT COUNT(*) FROM intents").fetchone()[0]


# ── unit: same_fact_cross_form skips stale rows ──────────────────────────────


def test_cross_form_skips_overdue_armed_row(ac_root):  # noqa: ANN001
    """An overdue ``armed`` row (grounded deadline elapsed) must NOT be returned
    as a cross-form match for a fresh immediate recognition — it is the previous
    occurrence wrapping up."""
    now = _now()
    last_week = (now - timedelta(days=7)).isoformat(timespec="minutes")
    past_deadline = (now - timedelta(days=6, hours=20)).isoformat(timespec="minutes")
    with fts.cursor() as conn:
        stale_armed = _meeting(
            armed=True, ts=last_week, resolved_at=past_deadline, valid_until=past_deadline
        )
        stale_armed.status = "armed"
        intent_store.insert_intent(conn, stale_armed)

        fresh_immediate = _meeting(armed=False, ts=now.isoformat(timespec="minutes"))
        # The stale armed row must not be returned as a cross-form match.
        assert intent_store.same_fact_cross_form(conn, fresh_immediate) is None


def test_cross_form_skips_overdue_open_immediate_row(ac_root):  # noqa: ANN001
    """An overdue ``open`` immediate row must NOT suppress a fresh event-based
    recognition of the same fact."""
    now = _now()
    last_week = (now - timedelta(days=7)).isoformat(timespec="minutes")
    past_deadline = (now - timedelta(days=6, hours=20)).isoformat(timespec="minutes")
    with fts.cursor() as conn:
        stale_open = _meeting(
            armed=False, ts=last_week, resolved_at=past_deadline, valid_until=past_deadline
        )
        intent_store.insert_intent(conn, stale_open)  # default status=open

        fresh_armed = _meeting(armed=True, ts=now.isoformat(timespec="minutes"))
        assert intent_store.same_fact_cross_form(conn, fresh_armed) is None


def test_cross_form_skips_expired_status_row(ac_root):  # noqa: ANN001
    """A harvested ``expired`` row never suppresses a fresh occurrence."""
    now = _now()
    last_week = (now - timedelta(days=7)).isoformat(timespec="minutes")
    with fts.cursor() as conn:
        stale = _meeting(armed=True, ts=last_week)
        stale.status = "armed"
        rid = intent_store.insert_intent(conn, stale)
        assert intent_store.update_intent_status(conn, intent_id=rid, new_status="expired")

        fresh_immediate = _meeting(armed=False, ts=now.isoformat(timespec="minutes"))
        assert intent_store.same_fact_cross_form(conn, fresh_immediate) is None


# ── unit: live rows STILL match (no regression) ──────────────────────────────


def test_cross_form_still_matches_live_armed_row(ac_root):  # noqa: ANN001
    """A live ``armed`` row (no elapsed deadline, recent) STILL folds the
    immediate re-recognition — the #549 rule is preserved for live rows."""
    now = _now()
    with fts.cursor() as conn:
        live_armed = _meeting(armed=True, ts=now.isoformat(timespec="minutes"))
        live_armed.status = "armed"
        intent_store.insert_intent(conn, live_armed)

        immediate = _meeting(
            armed=False, ts=(now + timedelta(minutes=5)).isoformat(timespec="minutes")
        )
        matched = intent_store.same_fact_cross_form(conn, immediate)
        assert matched is not None and matched.status == "armed"


def test_cross_form_still_matches_dismissed_row(ac_root):  # noqa: ANN001
    """``dismissed`` is final user feedback, NOT a stale-lifecycle state, so it
    keeps suppressing — "dismissed 永不复活" is preserved."""
    now = _now()
    with fts.cursor() as conn:
        first = _meeting(armed=False, ts=now.isoformat(timespec="minutes"))
        rid = intent_store.insert_intent(conn, first)
        assert intent_store.update_intent_status(conn, intent_id=rid, new_status="dismissed")

        armed = _meeting(armed=True, ts=(now + timedelta(minutes=5)).isoformat(timespec="minutes"))
        matched = intent_store.same_fact_cross_form(conn, armed)
        assert matched is not None and matched.status == "dismissed"


# ── integration: the sink inserts the fresh occurrence past a stale cross-form ─


def test_sink_inserts_fresh_immediate_past_stale_armed(ac_root):  # noqa: ANN001
    """End-to-end: a fresh immediate occurrence is INSERTED, not silently
    skipped, when only a stale (overdue) armed cross-form row exists."""
    now = _now()
    last_week = (now - timedelta(days=7)).isoformat(timespec="minutes")
    past_deadline = (now - timedelta(days=6, hours=20)).isoformat(timespec="minutes")
    with fts.cursor() as conn:
        stale_armed = _meeting(
            armed=True, ts=last_week, resolved_at=past_deadline, valid_until=past_deadline
        )
        stale_armed.status = "armed"
        intent_store.insert_intent(conn, stale_armed)
        assert _count(conn) == 1

        fresh = _meeting(armed=False, ts=now.isoformat(timespec="minutes"))
        res = sink.persist_intent_result(conn, fresh)
        assert res.outcome == "inserted"
        assert _count(conn) == 2


def test_sink_still_skips_immediate_past_live_armed(ac_root):  # noqa: ANN001
    """No regression: a live armed row still folds the immediate re-recognition
    (skip), keeping the time-gate intact."""
    now = _now()
    with fts.cursor() as conn:
        live_armed = _meeting(armed=True, ts=now.isoformat(timespec="minutes"))
        live_armed.status = "armed"
        sink.persist_intent(conn, live_armed)
        assert _count(conn) == 1

        immediate = _meeting(
            armed=False, ts=(now + timedelta(minutes=5)).isoformat(timespec="minutes")
        )
        res = sink.persist_intent_result(conn, immediate)
        assert res.outcome == "skipped"
        assert _count(conn) == 1

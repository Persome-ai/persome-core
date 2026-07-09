"""Regression tests for issue #629.

A grounded ``armed`` intent (one carrying a ``valid_until``) whose waited-for
event never fires and whose deadline has passed used to fall through every
lifecycle gate:

- :func:`store.expire_overdue` only harvested ``status='open'`` rows, so the
  overdue armed row was never flipped to ``expired``.
- :func:`store.expire_stale_armed` (#532) reaps by ``created_at`` age (14d), so
  a row only days old but already past its ``valid_until`` survives the TTL.
- :func:`store.is_expired` returned ``False`` for armed rows, so the overdue
  armed row leaked into the recall scene layer (recall.py filters on
  ``is_expired``) presented as live "未过期素材".

The fix: an ``armed`` row whose ``valid_until`` has passed is genuinely expired
(its grounded deadline came and went) and is treated identically to an overdue
``open`` row — ``is_expired`` reports it, ``expire_overdue`` harvests it to
``expired``. The ``created_at``-based TTL reap (#532, terminal ``expired`` per the §9
audit) stays the path for ungrounded never-fire reminders and is untouched.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from persome.intent import store as intent_store
from persome.intent.ontology import Intent
from persome.store import fts


def _armed_grounded(*, valid_until: str | None, app: str = "Figma") -> Intent:
    """A dormant ``armed`` L7 reminder that ALSO carries a grounded deadline."""
    it = Intent(
        kind="reminder",
        scope="session-x",
        confidence=0.8,
        rationale=f"下次打开 {app} 时改图标",
        payload={"text": "改图标"},
        fire_on="app_opened",
        fire_config={"app": app},
        resolved_at=valid_until,
        valid_until=valid_until,
    )
    it.status = "armed"
    return it


def test_is_expired_flags_overdue_armed_row():
    """RED→GREEN: an armed row past its valid_until reads as expired."""
    now = datetime.now()
    past = (now - timedelta(hours=2)).isoformat(timespec="seconds")
    it = _armed_grounded(valid_until=past)
    assert intent_store.is_expired(it, now=now.isoformat(timespec="seconds")) is True


def test_is_expired_keeps_armed_with_future_valid_until():
    """An armed row whose deadline is still ahead is NOT expired."""
    now = datetime.now()
    future = (now + timedelta(days=3)).isoformat(timespec="seconds")
    it = _armed_grounded(valid_until=future)
    assert intent_store.is_expired(it, now=now.isoformat(timespec="seconds")) is False


def test_is_expired_keeps_ungrounded_armed_row():
    """An armed row with no valid_until (the normal L7 shape) never expires by
    deadline — only the #532 created_at TTL reaps it."""
    it = _armed_grounded(valid_until=None)
    now = datetime.now().isoformat(timespec="seconds")
    assert intent_store.is_expired(it, now=now) is False


def test_expire_overdue_harvests_overdue_armed_row(ac_root):
    """RED→GREEN: the daily harvest flips an overdue armed row to expired."""
    now = datetime.now()
    past = (now - timedelta(hours=2)).isoformat(timespec="seconds")
    with fts.cursor() as conn:
        intent_store.insert_intent(conn, _armed_grounded(valid_until=past))
        harvested = len(intent_store.expire_overdue(conn, now=now.isoformat(timespec="seconds")))
        assert harvested == 1
        # no longer dormant, no longer leaking as armed
        assert intent_store.intents_armed(conn) == []
        got = intent_store.get_by_dedup_key(
            conn, intent_store.dedup_key(_armed_grounded(valid_until=past))
        )
        assert got is not None and got.status == "expired"


def test_expire_overdue_keeps_armed_with_future_valid_until(ac_root):
    """A still-pending armed row (deadline ahead) survives the harvest — its
    event may yet fire."""
    now = datetime.now()
    future = (now + timedelta(days=3)).isoformat(timespec="seconds")
    with fts.cursor() as conn:
        intent_store.insert_intent(conn, _armed_grounded(valid_until=future))
        harvested = len(intent_store.expire_overdue(conn, now=now.isoformat(timespec="seconds")))
        assert harvested == 0
        assert len(intent_store.intents_armed(conn)) == 1


def test_expire_overdue_skips_ungrounded_armed_row(ac_root):
    """An ungrounded armed row (valid_until NULL) is not the harvest's job — it
    waits for the #532 created_at TTL."""
    now = datetime.now()
    with fts.cursor() as conn:
        intent_store.insert_intent(conn, _armed_grounded(valid_until=None))
        harvested = len(intent_store.expire_overdue(conn, now=now.isoformat(timespec="seconds")))
        assert harvested == 0
        assert len(intent_store.intents_armed(conn)) == 1


def test_overdue_armed_does_not_leak_into_scene(ac_root):
    """End-to-end: an overdue armed row is filtered out of the recall scene
    layer's lifecycle gate (the leak the issue describes)."""
    now = datetime.now()
    past = (now - timedelta(hours=2)).isoformat(timespec="seconds")
    with fts.cursor() as conn:
        intent_store.insert_intent(conn, _armed_grounded(valid_until=past))
        scoped = intent_store.intents_for_scope(conn, "session-x")
        now_iso = now.isoformat(timespec="seconds")
        live = [it for it in scoped if not intent_store.is_expired(it, now=now_iso)]
        assert live == []  # overdue armed row excluded as stale material

"""Regression for #525: intent dedup had no time window → recurring commitments
were permanently suppressed (systematic, silent misses).

Two distinct failure modes the issue describes:

1. **Cross-week relative-weekday collision** — "每周五15:00 和 Alice 开会" produces
   the same ``dedup_key`` (``meeting|{wk5}15:00|alice``) every Friday because the
   ``{wk5}`` token carries no anchored date. The first row, once the user has
   acted on it (consumed/dismissed) or it expired, blocks NEXT Friday's genuinely
   new standup forever — ``exists()`` queried the whole table with no time bound.

2. **Cross-day same-token collision** — "明天15:00" recognized Monday and "明天15:00"
   recognized next Thursday share the key ``meeting|{tomorrow}15:00|`` even though
   they denote different calendar days.

The fix bounds the exact-key dedup to a recent window AND lets a stale (non-open,
or expired-open) prior row stop suppressing a fresh recognition. A still-live row
(open, not yet expired, recent) still folds so the same commitment recognized
twice in one trajectory is not double-surfaced.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from persome.intent import sink
from persome.intent import store as intent_store
from persome.intent.ontology import Intent, IntentEvidence
from persome.store import fts


def _mk(*, kind: str, scope: str, when: str, ts: str, people: list[str] | None = None) -> Intent:
    return Intent(
        kind=kind,
        scope=scope,
        confidence=0.8,
        rationale="",
        ts=ts,
        payload={"when_text": when, "with": people or []},
        evidence=[IntentEvidence(source="timeline_block", ref_id="blk-1", quote=when)],
    )


def _count(conn) -> int:  # noqa: ANN001
    return conn.execute("SELECT COUNT(*) FROM intents").fetchone()[0]


def test_weekly_standup_not_permanently_suppressed(ac_root) -> None:  # noqa: ANN001
    """每周五15:00 和 Alice 开会: the SAME relative-weekday token recognized one
    week apart must produce TWO rows, not be silently folded forever."""
    # 2026-06-12 is a Friday.
    first = _mk(
        kind="meeting",
        scope="session-w1",
        when="周五15:00",
        ts="2026-06-12T09:00",
        people=["Alice"],
    )
    # Next Friday, 2026-06-19, same recurring standup.
    second = _mk(
        kind="meeting",
        scope="session-w2",
        when="周五15:00",
        ts="2026-06-19T09:00",
        people=["Alice"],
    )
    with fts.cursor() as conn:
        assert sink.persist_intent(conn, first) is not None
        # The user acts on the first occurrence — final feedback.
        first_id = intent_store.id_for_intent(conn, first)
        assert first_id is not None
        intent_store.update_intent_status(conn, intent_id=first_id, new_status="consumed")

        # Next week's standup is a genuinely NEW commitment, not a duplicate.
        res = sink.persist_intent_result(conn, second)
        assert res.outcome == "inserted", "next week's recurring standup was suppressed (#525)"
        assert _count(conn) == 2


def test_tomorrow_token_collides_across_days(ac_root) -> None:  # noqa: ANN001
    """明天15:00 said Monday and 明天15:00 said next Thursday are different days —
    they must not collapse onto one row across a week."""
    monday = _mk(kind="meeting", scope="session-mon", when="明天15:00", ts="2026-06-15T10:00")
    next_thu = _mk(kind="meeting", scope="session-thu", when="明天15:00", ts="2026-06-25T10:00")
    with fts.cursor() as conn:
        assert sink.persist_intent(conn, monday) is not None
        res = sink.persist_intent_result(conn, next_thu)
        assert res.outcome == "inserted", "cross-day {tomorrow} collision suppressed a new row"
        assert _count(conn) == 2


def test_same_trajectory_recent_duplicate_still_folds(ac_root) -> None:  # noqa: ANN001
    """The fix must NOT regress the within-trajectory dedup: the same commitment
    recognized minutes apart (fast-K1 then slow path) still folds to one row."""
    early = _mk(kind="meeting", scope="fast-K1", when="周五15:00", ts="2026-06-12T09:00")
    late = _mk(kind="meeting", scope="session-a", when="周五15:00", ts="2026-06-12T09:03")
    with fts.cursor() as conn:
        assert sink.persist_intent(conn, early) is not None
        assert sink.persist_intent(conn, late) is None  # still folded, no double-surface
        assert _count(conn) == 1


def test_open_but_not_expired_recent_row_still_dedups(ac_root) -> None:  # noqa: ANN001
    """A recent, still-open, not-yet-expired prior row keeps suppressing an exact
    re-recognition (same day) — only STALE priors stop suppressing."""
    now = datetime.now()
    ts1 = now.isoformat(timespec="minutes")
    ts2 = (now + timedelta(minutes=5)).isoformat(timespec="minutes")
    a = _mk(kind="meeting", scope="s1", when="周五15:00", ts=ts1)
    b = _mk(kind="meeting", scope="s2", when="周五15:00", ts=ts2)
    with fts.cursor() as conn:
        assert sink.persist_intent(conn, a) is not None
        assert sink.persist_intent(conn, b) is None
        assert _count(conn) == 1


def test_expired_prior_does_not_suppress_even_in_window(ac_root) -> None:  # noqa: ANN001
    """An already-expired prior occurrence (the meeting happened) must NOT block a
    fresh recognition even if it is recent — the is_expired branch of the fix."""
    now = datetime.now()
    # First occurrence recognized ~5h ago for a meeting "1小时内": resolved = past+1h,
    # valid_until = +1h grace → elapsed ~3h ago, unambiguously expired.
    past = (now - timedelta(hours=5)).isoformat(timespec="minutes")
    a = _mk(kind="meeting", scope="s1", when="1小时内", ts=past)
    # Same euphemism recognized now — a genuinely new "within the hour" meeting.
    b = _mk(kind="meeting", scope="s2", when="1小时内", ts=now.isoformat(timespec="minutes"))
    with fts.cursor() as conn:
        assert sink.persist_intent(conn, a) is not None
        # The prior row's valid_until (past + 1h + 1h grace) is already elapsed.
        res = sink.persist_intent_result(conn, b)
        assert res.outcome == "inserted", "expired prior occurrence wrongly suppressed a new one"
        assert _count(conn) == 2

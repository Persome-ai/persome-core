"""(kind, scope)-level closed-set hard cooldown for the negative-feedback loop (#533).

Before #533 the negative feedback was prompt-soft only: a kind dismissed many
times still re-surfaced under a new wording (fresh ``dedup_key``). These tests
pin the deterministic hard gate at the unified sink:

- a (kind, scope) dismissed ≥ threshold within the window → its intents are dropped;
- the cooldown is TIME-BOUNDED — it expires ``cooldown_hours`` after the latest
  dismissal (never a lifetime ban);
- below threshold → unaffected; the gate is config-killable;
- the cooldown clock anchors on the DISMISS instant (``dismissed_at``), produced
  by the PRODUCTION dismiss path (``update_intent_status``), NOT on ``ts``
  (recognition time);
- user_committed / high-confidence intents bypass the gate (宪法 §5);
- a suppression is recorded as observable telemetry (拒绝是金矿).

CRITICAL (#533 review): the dismissals here are produced via the SAME path
production uses — ``insert_intent(status='open')`` then
``update_intent_status(id, 'dismissed')`` — so the test exercises the real
``dismissed_at`` stamping. A synthetic "born dismissed" row (status='dismissed'
inserted directly with a fabricated ts) does NOT exist in production and would
mask the very bug this PR fixes.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from persome.config import load as load_config
from persome.intent import cooldown, sink
from persome.intent import store as intent_store
from persome.intent.ontology import Intent
from persome.store import cooldown_suppressions, fts


def _dismiss_via_production_path(
    conn, kind: str, text: str, *, scope: str = "session-x", dismissed_minutes_ago: int = 0
) -> int:
    """Dismiss a kind THE WAY PRODUCTION DOES: insert an open intent, then flip it
    to dismissed via ``update_intent_status`` (which stamps ``dismissed_at``).

    ``dismissed_minutes_ago`` back-dates the stamped ``dismissed_at`` after the
    flip so a test can place the dismiss instant in the past — production stamps
    ``datetime.now()``, but tests need to age dismissals to probe the time bound.
    """
    intent_id = intent_store.insert_intent(
        conn,
        Intent(
            kind=kind,
            scope=scope,
            confidence=0.5,
            rationale=text,
            status="open",
            ts=datetime.now().isoformat(timespec="seconds"),
            payload={"text": text},
        ),
    )
    intent_store.update_intent_status(conn, intent_id=intent_id, new_status="dismissed")
    if dismissed_minutes_ago:
        ts = (datetime.now().astimezone() - timedelta(minutes=dismissed_minutes_ago)).isoformat(
            timespec="seconds"
        )
        conn.execute("UPDATE intents SET dismissed_at = ? WHERE id = ?", (ts, intent_id))
        conn.commit()
    return intent_id


def _fresh(kind: str, text: str, scope: str = "session-x", confidence: float = 0.5) -> Intent:
    return Intent(
        kind=kind,
        scope=scope,
        confidence=confidence,
        rationale=text,
        ts=datetime.now().isoformat(timespec="seconds"),
        payload={"text": text},
    )


# --- THE blocking regression: the clock anchors on the dismiss instant ---------
# This test MUST fail against the pre-fix code (which anchored on MAX(ts) of a
# 'dismissed' row, an instant the production dismiss path never writes) and pass
# after it (anchoring on dismissed_at written by update_intent_status).


def test_cooldown_anchors_on_dismissed_at_not_recognition_ts(ac_root):
    """Intents recognized long ago, then dismissed JUST NOW (the production
    pattern), must still trigger + keep the kind in cooldown — proving the clock
    reads ``dismissed_at`` (now), not ``ts`` (the old recognition time).

    Pre-fix: ``dismissed_kind_window`` used ``MAX(ts) WHERE status='dismissed'``;
    here every ts is ~2 days old, so a 24h window would count 0 and never cool
    down — that漏挡 is the blocking bug. Post-fix: dismissed_at = now → cooled."""
    old_ts = (datetime.now() - timedelta(days=2)).isoformat(timespec="seconds")
    with fts.cursor() as conn:
        for i in range(3):
            iid = intent_store.insert_intent(
                conn,
                Intent(
                    kind="reminder",
                    scope="session-x",
                    confidence=0.5,
                    rationale=f"提醒{i}",
                    status="open",
                    ts=old_ts,  # recognized 2 days ago
                    payload={"text": f"提醒{i}"},
                ),
            )
            # ...but dismissed NOW (production path stamps dismissed_at=now).
            intent_store.update_intent_status(conn, intent_id=iid, new_status="dismissed")
        # The kind is in cooldown for THIS scope, anchored on the just-now dismisses.
        assert cooldown.kind_in_cooldown(conn, "reminder", scope="session-x")
        # And the sink drops a fresh, differently-worded reminder in that scope.
        res = sink.persist_intent_result(conn, _fresh("reminder", "全新措辞的提醒"))
    assert res.outcome == "skipped"
    assert res.row_id is None


def test_recognition_ts_alone_does_not_cool_down(ac_root):
    """A row that is born 'dismissed' WITHOUT a dismissed_at (a status flipped directly
    by some code path, not a real user dismiss) must NOT feed the cooldown — only a real
    user dismiss action (which stamps dismissed_at) counts. (The #532 armed-TTL reaper
    that used to produce such rows now terminates at ``expired`` per the §9 audit; this
    test still guards the general dismissed-without-dismissed_at invariant.)"""
    with fts.cursor() as conn:
        for i in range(5):
            # Reaper-style: status flipped directly, dismissed_at stays NULL.
            iid = intent_store.insert_intent(
                conn,
                Intent(
                    kind="reminder",
                    scope="session-x",
                    confidence=0.5,
                    rationale=f"armed-reaped-{i}",
                    status="open",
                    ts=datetime.now().isoformat(timespec="seconds"),
                    payload={"text": f"x{i}"},
                ),
            )
            conn.execute("UPDATE intents SET status = 'dismissed' WHERE id = ?", (iid,))
        conn.commit()
        # NULL dismissed_at → not counted → no cooldown (fail-open).
        assert not cooldown.kind_in_cooldown(conn, "reminder", scope="session-x")


# --- the gate at the sink (the choke point every producer funnels through) ----


def test_dismissed_kind_is_hard_blocked_at_sink(ac_root):
    """3 dismissals of a (kind, scope) → a freshly-worded intent of that kind is dropped."""
    with fts.cursor() as conn:
        for i in range(3):
            _dismiss_via_production_path(conn, "reminder", f"提醒{i}")
        # New text → new dedup_key, so dedup never fires; only the cooldown can.
        res = sink.persist_intent_result(conn, _fresh("reminder", "全新措辞的提醒"))
    assert res.outcome == "skipped"
    assert res.row_id is None


def test_below_threshold_kind_still_persists(ac_root):
    """2 dismissals (threshold 3) → the kind is NOT in cooldown, intent inserts."""
    with fts.cursor() as conn:
        for i in range(2):
            _dismiss_via_production_path(conn, "reminder", f"提醒{i}")
        res = sink.persist_intent_result(conn, _fresh("reminder", "应该正常入库"))
    assert res.outcome == "inserted"
    assert res.row_id is not None


def test_cooldown_is_kind_scoped_not_global(ac_root):
    """Dismissing reminders cools down ONLY reminder — meeting is unaffected."""
    with fts.cursor() as conn:
        for i in range(5):
            _dismiss_via_production_path(conn, "reminder", f"提醒{i}")
        blocked = sink.persist_intent_result(conn, _fresh("reminder", "另一个提醒"))
        other = sink.persist_intent_result(conn, _fresh("meeting", "周五例会"))
    assert blocked.outcome == "skipped"
    assert other.outcome == "inserted"


def test_cooldown_is_scope_scoped_not_cross_scope(ac_root):
    """#533 #4: dismissing reminders in ONE scene must NOT mute reminders in a
    genuinely DIFFERENT scene — the cooldown is (kind, scope), not global by-kind.

    Uses two distinct STABLE scenes (fast-K1 vs a meeting scope). Per-session
    ``session-<uuid>`` scopes are deliberately NOT used here: they belong to the
    same slow-trajectory scene and fold into one cooldown domain (see
    ``test_slow_trajectory_session_uuid_scopes_still_cool_down``)."""
    with fts.cursor() as conn:
        for i in range(3):
            _dismiss_via_production_path(conn, "reminder", f"提醒{i}", scope="fast-K1")
        # fast-K1 reminder is cooled…
        cooled = sink.persist_intent_result(conn, _fresh("reminder", "K1 新提醒", scope="fast-K1"))
        # …but a meeting-scene reminder is untouched (different scene).
        other_scope = sink.persist_intent_result(
            conn, _fresh("reminder", "会议场景新提醒", scope="meeting-abc123")
        )
    assert cooled.outcome == "skipped"
    assert other_scope.outcome == "inserted"


def test_slow_trajectory_session_uuid_scopes_still_cool_down(ac_root):
    """慢路修复 (#533): the slow trajectory recognizer stamps a fresh
    ``session-<uuid>`` scope every session, so dismissals of one kind spread across
    several different sessions must STILL accumulate into one cooldown.

    Pre-fix: ``dismissed_kind_window`` exact-matched ``scope``, so 5 dismisses in 5
    different ``session-*`` scopes counted as 1-each → never reached the threshold →
    the hard cooldown NEVER fired on the slow path (the欠抑 bug). A test that fixed
    the scope (like the others) masked this exact production behavior. Post-fix:
    all ``session-*`` scopes fold into one cross-session domain → the count reaches
    threshold → a fresh re-worded reminder in yet ANOTHER new session is dropped."""
    with fts.cursor() as conn:
        # Three dismisses, each in a DIFFERENT per-session scope (production: every
        # session is a new uuid). Threshold is 3 — exact-match would see 1+1+1.
        _dismiss_via_production_path(conn, "reminder", "提醒0", scope="session-sess_aaaaaaaaaaaa")
        _dismiss_via_production_path(conn, "reminder", "提醒1", scope="session-sess_bbbbbbbbbbbb")
        _dismiss_via_production_path(conn, "reminder", "提醒2", scope="session-sess_cccccccccccc")
        # A brand-new session re-recognizes a re-worded reminder → must be cooled.
        res = sink.persist_intent_result(
            conn, _fresh("reminder", "又一个会话的全新措辞提醒", scope="session-sess_dddddddddddd")
        )
        # And the deterministic gate agrees for that fresh-uuid scope.
        cooled = cooldown.kind_in_cooldown(conn, "reminder", scope="session-sess_eeeeeeeeeeee")
    assert res.outcome == "skipped"
    assert res.row_id is None
    assert cooled is True


# --- confidence / provenance bypass (宪法 §5 零熵猎场不该被否决) ----------------


def test_user_committed_bypasses_cooldown(ac_root):
    """A verbatim user_committed promise is EXEMPT from the hard cooldown even when
    the (kind, scope) is fully cooled down."""
    with fts.cursor() as conn:
        for i in range(3):
            _dismiss_via_production_path(conn, "reminder", f"提醒{i}")
        committed = _fresh("reminder", "用户亲口说的提醒", confidence=0.7)
        committed.payload["provenance"] = "user_committed"
        res = sink.persist_intent_result(conn, committed)
    assert res.outcome == "inserted"
    assert res.row_id is not None


def test_high_confidence_bypasses_cooldown(ac_root):
    """A high-confidence (>=0.9) user_committed intent bypasses the cooldown; the
    same intent without that provenance is clamped to 0.9 and still bypasses (the
    threshold is inclusive)."""
    with fts.cursor() as conn:
        for i in range(3):
            _dismiss_via_production_path(conn, "reminder", f"提醒{i}")
        hi = _fresh("reminder", "高置信承诺", confidence=1.0)
        hi.payload["provenance"] = "user_committed"  # keeps confidence uncapped
        res = sink.persist_intent_result(conn, hi)
    assert res.outcome == "inserted"


def test_inferred_high_confidence_is_clamped_then_still_blocked(ac_root):
    """An INFERRED intent claiming confidence 1.0 is clamped to 0.9 — which IS the
    bypass threshold, so it bypasses. (The exemption is intentionally generous:
    anything the clamp leaves at ≥0.9 is treated as a strong signal.)"""
    with fts.cursor() as conn:
        for i in range(3):
            _dismiss_via_production_path(conn, "reminder", f"提醒{i}")
        inferred = _fresh("reminder", "模型高置信猜测", confidence=1.0)  # no provenance
        res = sink.persist_intent_result(conn, inferred)
    # Clamped to 0.9 == threshold → bypass (inclusive). Document the boundary.
    assert res.outcome == "inserted"


def test_mid_confidence_inferred_is_blocked(ac_root):
    """A mid-confidence model GUESS (the gate's intended target) IS suppressed."""
    with fts.cursor() as conn:
        for i in range(3):
            _dismiss_via_production_path(conn, "reminder", f"提醒{i}")
        res = sink.persist_intent_result(conn, _fresh("reminder", "中置信猜测", confidence=0.5))
    assert res.outcome == "skipped"


# --- observability: 拒绝是金矿, the suppression is recorded ----------------------


def test_suppression_is_recorded_as_telemetry(ac_root):
    """A cooled-down drop leaves no ``intents`` row, but it MUST leave a structured
    trace in ``cooldown_suppressions`` (the #534 recalibration's data source)."""
    with fts.cursor() as conn:
        for i in range(3):
            _dismiss_via_production_path(conn, "reminder", f"提醒{i}")
        sink.persist_intent_result(conn, _fresh("reminder", "被吞掉的提醒"))
        stats = cooldown_suppressions.stats(conn)
    assert stats["total"] == 1
    assert stats["by_kind"].get("reminder") == 1


# --- the time bound: it always expires (no lifetime ban) ----------------------


def test_cooldown_expires_after_window(ac_root):
    """Dismissals older than cooldown_hours from `now` no longer suppress."""
    with fts.cursor() as conn:
        # 3 dismissals, but each dismissed_at is 48h ago (> default 24h cooldown).
        for i in range(3):
            _dismiss_via_production_path(
                conn, "reminder", f"提醒{i}", dismissed_minutes_ago=60 * 48 + i
            )
        # In cooldown right after the dismissals (anchor at the latest one)…
        latest = datetime.now().astimezone() - timedelta(hours=48)
        assert cooldown.kind_in_cooldown(
            conn, "reminder", scope="session-x", now=latest + timedelta(minutes=1)
        )
        # …but NOW (48h later) it has healed.
        assert not cooldown.kind_in_cooldown(
            conn, "reminder", scope="session-x", now=datetime.now().astimezone()
        )


def test_cooldown_anchors_on_most_recent_dismissal(ac_root):
    """The cooldown clock starts at the LATEST dismissal, not the first."""
    with fts.cursor() as conn:
        # Two old + one recent — the recent one keeps the kind hot.
        _dismiss_via_production_path(conn, "reminder", "old1", dismissed_minutes_ago=60 * 23)
        _dismiss_via_production_path(conn, "reminder", "old2", dismissed_minutes_ago=60 * 22)
        _dismiss_via_production_path(conn, "reminder", "recent", dismissed_minutes_ago=30)
        assert cooldown.kind_in_cooldown(
            conn, "reminder", scope="session-x", now=datetime.now().astimezone()
        )


# --- knobs: relaxable + killable ---------------------------------------------


def test_cooldown_disabled_restores_soft_behavior(ac_root):
    """cooldown_enabled=false → the gate never fires (prompt-soft-only)."""
    cfg = load_config()
    cfg.intent_recognizer.cooldown_enabled = False
    with fts.cursor() as conn:
        for i in range(9):
            _dismiss_via_production_path(conn, "reminder", f"提醒{i}")
        assert cooldown.suppression_for(conn, "reminder", scope="session-x", cfg=cfg) is None


def test_zero_cooldown_hours_never_bans(ac_root):
    """A misconfig (cooldown_hours<=0) must NOT become a lifetime ban."""
    with fts.cursor() as conn:
        for i in range(9):
            _dismiss_via_production_path(conn, "reminder", f"提醒{i}", dismissed_minutes_ago=1 + i)
        assert not cooldown.kind_in_cooldown(conn, "reminder", scope="session-x", cooldown_hours=0)


def test_empty_kind_is_never_in_cooldown(ac_root):
    with fts.cursor() as conn:
        assert not cooldown.kind_in_cooldown(conn, "")

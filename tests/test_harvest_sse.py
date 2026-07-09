"""The daily/boot intent-expiry harvest publishes a live `intent/resolved` SSE per
reaped row (#intent-evidence-autoclose follow-up), so the app drops a now-stale
suggestion card the instant it's harvested instead of waiting for the next
reconcile poll. The app's already-merged `handleStatusChange` removes the card on
any terminal `new_status` (expired), so this is daemon-only.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from persome.intent import store as intent_store
from persome.intent.ontology import Intent
from persome.session import tick as tick_mod
from persome.store import fts


def _insert(conn, **over) -> int:
    it = Intent(
        ts="2026-06-30T10:00:00+08:00",
        scope="s1",
        kind="reminder",
        confidence=0.9,
        rationale="x",
        payload={"text": over.pop("text", "t")},
        evidence=[],
    )
    for k, v in over.items():
        setattr(it, k, v)
    return intent_store.insert_intent(conn, it)


def test_harvest_publishes_status_change_per_reaped_row(ac_root, monkeypatch):
    sent: list[tuple] = []
    monkeypatch.setattr(
        intent_store,
        "publish_intent_status_change",
        lambda iid, **kw: sent.append((iid, kw["new_status"], kw["reason"])),
    )
    old = (datetime.now() - timedelta(days=45)).isoformat(timespec="seconds")
    with fts.cursor() as conn:
        # grounded + long-overdue open → expire_overdue → expired
        overdue = _insert(conn, valid_until="2020-01-01T00:00:00+08:00", text="过期承诺")
        # ungrounded + aged-out open → expire_stale_open → expired
        stale = _insert(conn, text="无锚陈旧待办")
        # dormant + aged-out armed → expire_stale_armed → EXPIRED (system/staleness close;
        # §9 audit moved it dismissed→expired — the leg most likely to regress, cover it).
        armed = _insert(conn, status="armed", text="永不触发的提醒")
        conn.execute("UPDATE intents SET created_at = ? WHERE id IN (?, ?)", (old, stale, armed))
        conn.commit()

    tick_mod.expire_overdue_intents()

    assert (overdue, "expired", "harvest_overdue") in sent
    assert (stale, "expired", "harvest_ungrounded_ttl") in sent
    assert (armed, "expired", "harvest_armed_ttl") in sent
    # and the rows really flipped to their terminal status
    with fts.cursor() as conn:
        statuses = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT id, status FROM intents WHERE id IN (?, ?, ?)", (overdue, stale, armed)
            )
        }
    assert statuses[overdue] == "expired"
    assert statuses[stale] == "expired"
    assert statuses[armed] == "expired"


def test_harvest_no_rows_publishes_nothing(ac_root, monkeypatch):
    sent: list = []
    monkeypatch.setattr(
        intent_store, "publish_intent_status_change", lambda iid, **kw: sent.append(iid)
    )
    with fts.cursor() as conn:
        _insert(conn, valid_until="2099-01-01T00:00:00+08:00", text="远期未过期")  # not overdue
    tick_mod.expire_overdue_intents()
    assert sent == []  # nothing harvested → no SSE

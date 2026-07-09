"""E2E: the merged over-fire confidence clamps fire through the REAL persist path.

The pure clamp functions (`_clamp_confidence` / `_soft_validate_payload`) are unit-
tested in isolation elsewhere. This suite proves they are actually WIRED INTO the
production persist pipeline — `sink.persist_intent` → `config.load()` (real default
config, flags on) → clamp → sqlite write → read-back — so a future refactor that
bypasses the clamp at the persist seam is caught even though the pure-function tests
still pass. It is the integration counterpart of the per-fix unit tests:

  #380 counterpart_proposed meeting  → capped to 0.6 (below the 0.7 surface bar)
  #383 info_need (even user_committed) → capped to 0.4 (unconditional kind ceiling)
  #390 reminder with empty load-bearing text → capped to 0.4 (incomplete-actionable)
  control: a complete user_committed meeting → stays ≥0.7 (clamps are targeted, not blanket)

Deterministic, zero-LLM (the model's job — does it emit such an intent — is the
golden firing eval's concern; here the intent is constructed, then persisted).
"""

from __future__ import annotations

import pytest

from persome.intent import sink
from persome.intent.ontology import Intent


@pytest.fixture
def conn(ac_root):
    from persome.store import fts

    with fts.cursor() as c:
        yield c


def _persist_and_read_confidence(conn, intent: Intent) -> float:
    rid = sink.persist_intent(conn, intent)
    assert rid is not None, (
        "intent was skipped at persist (dedup/cooldown?) — fresh DB expected insert"
    )
    row = conn.execute("SELECT confidence FROM intents WHERE id=?", (rid,)).fetchone()
    return float(row[0])


def test_counterpart_meeting_capped_below_bar_through_persist(conn):
    """#380: a counterpart_proposed meeting (complete payload, so soft-validate is a
    no-op) at high raw confidence is clamped to the counterpart cap 0.6 at persist."""
    it = Intent(
        kind="meeting",
        scope="timeline",
        confidence=0.95,
        payload={"when_text": "明天下午3点", "provenance": "counterpart_proposed"},
    )
    assert _persist_and_read_confidence(conn, it) == pytest.approx(0.6)


def test_info_need_kind_ceiling_through_persist_even_user_committed(conn):
    """#383: the per-kind zero-nag ceiling for info_need is UNCONDITIONAL — even a
    user_committed info_need is capped to 0.4, never a high-confidence surface."""
    it = Intent(
        kind="info_need",
        scope="timeline",
        confidence=0.95,
        payload={"provenance": "user_committed"},
    )
    assert _persist_and_read_confidence(conn, it) == pytest.approx(0.4)


def test_empty_reminder_incomplete_actionable_capped_through_persist(conn):
    """#390: a reminder whose load-bearing `text` is empty is not actionable — capped
    to 0.4 below the surface bar, even though user_committed exempts it from _clamp."""
    it = Intent(
        kind="reminder",
        scope="timeline",
        confidence=0.95,
        payload={"text": "", "provenance": "user_committed"},
    )
    assert _persist_and_read_confidence(conn, it) == pytest.approx(0.4)


def test_complete_user_committed_meeting_stays_surfaceable(conn):
    """Control: the clamps are TARGETED, not a blanket suppressor. A complete,
    user_committed meeting keeps its high confidence and stays above the 0.7 bar."""
    it = Intent(
        kind="meeting",
        scope="timeline",
        confidence=0.95,
        payload={"when_text": "明天下午3点", "provenance": "user_committed"},
    )
    conf = _persist_and_read_confidence(conn, it)
    assert conf >= 0.7, f"a legit user_committed meeting must still surface, got {conf}"

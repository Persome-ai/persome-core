"""Tests for #619: ungrounded / content-only / cross-kind commitment folding.

The #546 semantic fold collapses on ``resolved_at``, but ~94% of real ``open``
rows carry NULL ``resolved_at`` (the LLM never emitted a parseable ``when_text``),
so the fold path is dead for them. Three independent mechanisms each fan ONE
fact into 2-4 rows — the exact "同一承诺多行重复呈现" compounding-cost failure
the asymmetric-cost constitution names. The fixtures replay the real DB rows
cited in the issue:

- B1 cross-kind fan-out: {meeting,calendar} same `with`, same 「这周」, no
  resolved_at → two rows (real ids 75/76).
- B2 content field drift: info_need / reminder whose only difference is a
  volatile `channel` (System vs cmux) → fresh content digest each round (real
  info_need 47/48/49, reminder 78/79).
- B3 key-shape flip: a reminder where one recognition carries when_text/with
  and the other does not → temporal vs content key, never folds (real
  calendar 40/41).
"""

from __future__ import annotations

import pytest

from persome.intent import sink
from persome.intent import store as intent_store
from persome.intent.ontology import Intent, IntentEvidence
from persome.store import fts

_DAY_START = "2026-06-11T00:00"
_DAY_END = "2026-06-12T00:00"


def _mk(
    *,
    kind: str,
    scope: str,
    ts: str,
    when: str = "",
    people: list[str] | None = None,
    text: str = "",
    task_text: str = "",
    topic: str = "",
    channel: str = "",
    confidence: float = 0.8,
    rationale: str = "",
) -> Intent:
    payload: dict = {}
    if when:
        payload["when_text"] = when
    if people is not None:
        payload["with"] = people
    if text:
        payload["text"] = text
    if task_text:
        payload["task_text"] = task_text
    if topic:
        payload["topic"] = topic
    if channel:
        payload["channel"] = channel
    return Intent(
        kind=kind,
        scope=scope,
        confidence=confidence,
        rationale=rationale,
        ts=ts,
        payload=payload,
        evidence=[IntentEvidence(source="timeline_block", ref_id="blk-1", quote=text or when)],
    )


def _persist_all(rows: list[Intent]) -> list[Intent]:
    with fts.cursor() as conn:
        for it in rows:
            sink.persist_intent(conn, it)
        return intent_store.recent_intents(conn, start=_DAY_START, end=_DAY_END)


# --- B1: ungrounded cross-kind fan-out (real ids 75/76) -------------------------


def test_b1_ungrounded_cross_kind_meeting_calendar_folds(ac_root) -> None:  # noqa: ANN001
    """{meeting, calendar} same `with`, same vague 「这周」 anchor that does NOT
    resolve → NULL resolved_at. Old fold bails on `not resolved_at`. They are
    the same commitment → must fold to ONE row."""
    got = _persist_all(
        [
            _mk(
                kind="meeting",
                scope="fast-K1",
                ts="2026-06-11T13:05",
                when="这周",
                people=["Vanessa"],
            ),
            _mk(
                kind="calendar",
                scope="session-a1",
                ts="2026-06-11T13:30",
                when="这周",
                people=["Vanessa"],
            ),
        ]
    )
    assert len(got) == 1


def test_b1_ungrounded_meeting_only_when_text_no_people_folds(ac_root) -> None:  # noqa: ANN001
    """Same vague phrase, no `with` at all (识别抖动丢了对象) → still one fact."""
    got = _persist_all(
        [
            _mk(
                kind="meeting", scope="fast-K1", ts="2026-06-11T13:05", when="等忙完这阵", people=[]
            ),
            _mk(
                kind="meeting",
                scope="session-a1",
                ts="2026-06-11T13:20",
                when="等忙完这阵",
                people=[],
            ),
        ]
    )
    assert len(got) == 1


# --- B2: content field drift (real info_need 47/48/49, reminder 78/79) ----------


def test_b2_info_need_channel_drift_folds(ac_root) -> None:  # noqa: ANN001
    """Same info_need text, only `channel` differs (System vs cmux) → the whole
    payload hashed into the content digest mints a fresh key each round. Channel
    is volatile metadata, not identity → must fold to ONE row."""
    got = _persist_all(
        [
            _mk(
                kind="info_need",
                scope="s1",
                ts="2026-06-11T14:00",
                text="查一下 manus 新闻",
                channel="System",
            ),
            _mk(
                kind="info_need",
                scope="s1",
                ts="2026-06-11T14:01",
                text="查一下 manus 新闻",
                channel="cmux",
            ),
            _mk(
                kind="info_need",
                scope="s1",
                ts="2026-06-11T14:02",
                text="查一下 manus 新闻",
                channel="System",
            ),
        ]
    )
    assert len(got) == 1


def test_b2_reminder_channel_drift_folds(ac_root) -> None:  # noqa: ANN001
    """reminder 78/79: identical text, channel System vs cmux → one fact."""
    got = _persist_all(
        [
            _mk(
                kind="reminder",
                scope="s1",
                ts="2026-06-11T16:00",
                text="调整事件摘要的设计",
                channel="System",
            ),
            _mk(
                kind="reminder",
                scope="s1",
                ts="2026-06-11T16:01",
                text="调整事件摘要的设计",
                channel="cmux",
            ),
        ]
    )
    assert len(got) == 1


def test_b2_facet_provenance_flip_keeps_dedup_key_stable() -> None:
    """#269: the fast-path facetizer (#262) stashes a `facets` sub-dict on every
    payload, and `facets["provenance"]` (committed/proposed/inferred) is the same
    volatile direction marker the top-level `provenance` field already strips for
    the content digest. If `facets` is hashed verbatim, the volatile provenance
    rides back in through the sub-dict → the content `dedup_key` changes when the
    SAME anchorless fact's provenance flips → fold fails → duplicate push (the
    #619 B2 failure mode). The derived facets must not perturb the identity key."""
    base = {"text": "查一下 manus 新闻"}
    committed = _mk(kind="info_need", scope="s1", ts="2026-06-11T14:00", text="查一下 manus 新闻")
    committed.payload["facets"] = {**base, "object": "manus", "provenance": "committed"}
    proposed = _mk(kind="info_need", scope="s1", ts="2026-06-11T14:01", text="查一下 manus 新闻")
    proposed.payload["facets"] = {**base, "object": "manus", "provenance": "proposed"}

    # Anchorless info_need → content-branch key; the facet provenance flip must
    # not change the digest.
    assert intent_store.dedup_key(committed) == intent_store.dedup_key(proposed)
    # And the projection itself must carry no provenance (top-level or nested).
    norm = intent_store.normalize_content_payload(committed.payload)
    assert "facets" not in norm
    assert "provenance" not in norm


def test_b2_facet_provenance_flip_folds(ac_root) -> None:  # noqa: ANN001
    """End-to-end of #269: same anchorless info_need, only `facets["provenance"]`
    flips committed→proposed→committed → must fold to ONE row, not three."""
    rows = []
    for ts, prov in (
        ("2026-06-11T14:00", "committed"),
        ("2026-06-11T14:01", "proposed"),
        ("2026-06-11T14:02", "committed"),
    ):
        it = _mk(kind="info_need", scope="s1", ts=ts, text="查一下 manus 新闻")
        it.payload["facets"] = {"object": "manus", "provenance": prov}
        rows.append(it)
    got = _persist_all(rows)
    assert len(got) == 1


def test_b2_genuinely_different_info_need_stays_separate(ac_root) -> None:  # noqa: ANN001
    """Folding must not over-collapse: two DIFFERENT hints in one scene coexist."""
    got = _persist_all(
        [
            _mk(kind="info_need", scope="s1", ts="2026-06-11T14:00", text="查一下 manus 新闻"),
            _mk(kind="info_need", scope="s1", ts="2026-06-11T14:01", text="查一下机票价格"),
        ]
    )
    assert len(got) == 2


# --- B3: key-shape flip (real calendar 40/41) -----------------------------------


def test_b3_when_text_jitter_does_not_split(ac_root) -> None:  # noqa: ANN001
    """calendar 40/41: same commitment, one recognition carries when_text the
    next drops it (LLM jitter). Temporal-branch key vs content-branch key → two
    rows today. Same fact → must fold to ONE."""
    got = _persist_all(
        [
            _mk(
                kind="reminder",
                scope="s1",
                ts="2026-06-11T16:00",
                text="给 mina 写反馈",
                when="尽快",
            ),
            _mk(kind="reminder", scope="s1", ts="2026-06-11T16:05", text="给 mina 写反馈"),
        ]
    )
    assert len(got) == 1


# --- guards: do not over-fold across genuine boundaries -------------------------


def test_no_fold_reminder_vs_meeting_content_only(ac_root) -> None:  # noqa: ANN001
    """Different kinds (reminder vs meeting) with the same text are different
    fold groups — never fold."""
    got = _persist_all(
        [
            _mk(kind="reminder", scope="s1", ts="2026-06-11T16:00", text="聊聊设计"),
            _mk(kind="meeting", scope="s1", ts="2026-06-11T16:05", text="聊聊设计"),
        ]
    )
    assert len(got) == 2


def test_no_fold_content_outside_window(ac_root) -> None:  # noqa: ANN001
    """A re-statement >48h later is a fresh occurrence — never folds."""
    with fts.cursor() as conn:
        sink.persist_intent(
            conn, _mk(kind="reminder", scope="s1", ts="2026-06-08T16:00", text="给 mina 写反馈")
        )
        sink.persist_intent(
            conn, _mk(kind="reminder", scope="s1", ts="2026-06-11T16:00", text="给 mina 写反馈")
        )
        got = intent_store.recent_intents(conn, start="2026-06-01T00:00", end="2026-06-30T00:00")
    assert len(got) == 2


# --- people-entity whitespace drift (real meeting ids 133/137) ------------------
#
# Production replay of the "明天晚上 / Dev群 / Onboarding 一期 PRD" commitment that
# the daemon re-recognized into a long chain of distinct OPEN rows (133/137/…)
# because the fold never collapsed them. The meeting payload carries NO text body
# (only ``when_text``/``with``/``channel``), so the ungrounded content fold falls
# back to ``with`` overlap — and ``_norm_people`` only ``.strip()``ed the ends, so
# the SAME entity surfaced as ``"Dev群"`` one recognition and ``"Dev 群"`` the next
# never overlapped, minting a fresh row each session. ``with`` is volatile surface
# wording, not identity (same asymmetric-cost rule as the channel-drift fold).


def test_meeting_people_internal_whitespace_drift_folds(ac_root) -> None:  # noqa: ANN001
    """meeting 133/137: 「明天晚上」 never resolves a clock → ungrounded fold; the
    only identity signal is ``with``, which drifts ``"Dev 群"`` vs ``"Dev群"``
    (internal space). Same shared counterpart → must fold to ONE row."""
    got = _persist_all(
        [
            _mk(
                kind="meeting",
                scope="session-c49a6a0e761d",
                ts="2026-06-11T23:40",
                when="明天晚上",
                people=["桃子", "Dev 群"],
            ),
            _mk(
                kind="meeting",
                scope="session-77f055f0f1c5",
                ts="2026-06-11T23:52",
                when="明天晚上",
                people=["Dev群"],
            ),
        ]
    )
    assert len(got) == 1


def test_meeting_hint_people_internal_whitespace_drift_folds(ac_root) -> None:  # noqa: ANN001
    """The hint-fold path (``_find_hint_fold_target``) shares ``_norm_people``: a
    ``meeting_hint`` re-stated with the same euphemism phrase but ``"Dev 群"`` vs
    ``"Dev群"`` must fold, not fan into two conf≤0.4 rows that each push."""
    got = _persist_all(
        [
            _mk(
                kind="meeting_hint",
                scope="session-a",
                ts="2026-06-11T16:00",
                when="明天晚上",
                people=["Dev 群"],
                confidence=0.4,
            ),
            _mk(
                kind="meeting_hint",
                scope="session-b",
                ts="2026-06-11T16:20",
                when="明天晚上",
                people=["Dev群"],
                confidence=0.4,
            ),
        ]
    )
    assert len(got) == 1


def test_meeting_distinct_people_stay_separate(ac_root) -> None:  # noqa: ANN001
    """Guard: whitespace-normalizing ``with`` must NOT over-fold two genuinely
    different counterparts at the same vague hour into one row."""
    got = _persist_all(
        [
            _mk(
                kind="meeting", scope="s1", ts="2026-06-11T23:40", when="明天晚上", people=["张三"]
            ),
            _mk(
                kind="meeting", scope="s2", ts="2026-06-11T23:52", when="明天晚上", people=["李四"]
            ),
        ]
    )
    assert len(got) == 2


# --- meeting agenda (``topic``) is the cross-recognition identity --------------
#
# Meeting payloads used to carry NO text body (only when_text/with/channel), so
# the ungrounded content fold could only lean on the volatile ``with`` list. The
# recognizer now emits a short ``topic`` (the agenda), which ``_content_body``
# already reads — so the SAME meeting re-recognized folds by its stable agenda
# even when the people list drifts to empty on one side (识别抖动丢了对象). The
# disjoint-people guard is deliberately NOT relaxed: two DIFFERENT agendas, or
# the same agenda with genuinely-disjoint non-empty attendees, still stay apart.


def test_meeting_same_topic_one_side_no_people_folds(ac_root) -> None:  # noqa: ANN001
    """Same agenda, but one recognition dropped ``with`` entirely → the topic body
    + one-empty-people path folds them (what ``with``-only could not)."""
    got = _persist_all(
        [
            _mk(
                kind="meeting",
                scope="session-a",
                ts="2026-06-11T23:40",
                when="明天晚上",
                people=["桃子"],
                topic="Onboarding 一期 PRD",
            ),
            _mk(
                kind="meeting",
                scope="session-b",
                ts="2026-06-11T23:52",
                when="明天晚上",
                people=[],
                topic="Onboarding 一期 PRD",
            ),
        ]
    )
    assert len(got) == 1


@pytest.mark.xfail(
    reason="pre-existing publish frame-shape drift (Phase-0-cut recognizer domain) — #520",
    strict=False,
)
def test_meeting_different_topic_stays_separate(ac_root) -> None:  # noqa: ANN001
    """Guard: in the content-fold path (distinct ``with`` → distinct dedup_key, so
    the exact key can't fold them), two DIFFERENT agendas must stay apart — the
    topic body discriminates and must NOT over-fold on the vague hour alone. (Same
    when_text + same ``with`` is a separate, pre-existing exact-key fold that
    ignores ``topic`` — not what this guards.)"""
    got = _persist_all(
        [
            _mk(
                kind="meeting",
                scope="s1",
                ts="2026-06-11T23:40",
                when="明天晚上",
                people=["桃子"],
                topic="Onboarding 一期 PRD",
            ),
            _mk(
                kind="meeting",
                scope="s2",
                ts="2026-06-11T23:52",
                when="明天晚上",
                people=[],
                topic="季度预算评审",
            ),
        ]
    )
    assert len(got) == 2

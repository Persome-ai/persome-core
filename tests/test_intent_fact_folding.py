"""Tests for #546: intent fact-layer dedup folding + lifecycle.

Three pieces share one deterministic base (``intent/normalize.py``):

1. when_text temporal resolution (zero LLM, anchored at intent.ts);
2. sink-level semantic fact folding (cross-scope / cross-surface / cross-kind
   within the {meeting, calendar} group);
3. expiry lifecycle (valid_until at persist; daily harvest open → expired;
   read-side filters in recall ① and the active tick).

The folding fixtures replay the REAL 2026-06-11 duplicates from the issue:
4 「产研对齐 22:00」 rows and 3 「给 mina 写反馈 1h内」 rows that each describe
one fact.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from persome.config import load as load_config
from persome.intent import normalize, recall, sink
from persome.intent import store as intent_store
from persome.intent.ontology import Intent, IntentEvidence
from persome.store import fts

_ANCHOR = datetime.fromisoformat("2026-06-11T13:00")
_DAY_START = "2026-06-11T00:00"
_DAY_END = "2026-06-12T00:00"


# --- normalize: deterministic when_text resolution ------------------------------


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        ("22:00", datetime(2026, 6, 11, 22, 0)),
        ("今天22:00", datetime(2026, 6, 11, 22, 0)),
        ("6月11日 (今天) 22:00", datetime(2026, 6, 11, 22, 0)),
        ("明天上午10点", datetime(2026, 6, 12, 10, 0)),
        ("明早8点", datetime(2026, 6, 12, 8, 0)),
        ("后天15:30", datetime(2026, 6, 13, 15, 30)),
        # 2026-06-11 is a Thursday → 周五 = next day, 周三 = next Wednesday.
        ("周五下午3点", datetime(2026, 6, 12, 15, 0)),
        ("周三上午9点", datetime(2026, 6, 17, 9, 0)),
        ("晚上8点", datetime(2026, 6, 11, 20, 0)),
        ("今晚十点", datetime(2026, 6, 11, 22, 0)),  # Chinese-numeral hour
        ("下午两点半", datetime(2026, 6, 11, 14, 30)),
        # Bare period-of-day with NO clock → the conventional default hour for that period
        # (anchor is Thu 2026-06-11 13:00), so a reminder/todo still gets a fire time.
        ("今晚", datetime(2026, 6, 11, 20, 0)),
        ("今天晚上", datetime(2026, 6, 11, 20, 0)),
        ("下午", datetime(2026, 6, 11, 15, 0)),
        ("明天上午", datetime(2026, 6, 12, 10, 0)),
        ("明早", datetime(2026, 6, 12, 8, 0)),
        ("1小时内", datetime(2026, 6, 11, 14, 0)),
        ("2小时后", datetime(2026, 6, 11, 15, 0)),
        ("30分钟后", datetime(2026, 6, 11, 13, 30)),
        ("15min", datetime(2026, 6, 11, 13, 15)),
    ],
)
def test_resolve_when_text_forms(when: str, expected: datetime) -> None:
    resolved, _end = normalize.resolve_when_text(when, anchor=_ANCHOR)
    assert resolved == expected


def test_resolve_when_text_range_sets_end() -> None:
    resolved, end = normalize.resolve_when_text("14:00 - 15:30", anchor=_ANCHOR)
    assert resolved == datetime(2026, 6, 11, 14, 0)
    assert end == datetime(2026, 6, 11, 15, 30)


def test_resolve_when_text_unparseable_is_none_not_error() -> None:
    for raw in ("尽快", "", "回头", "下次见面时", None):
        assert normalize.resolve_when_text(raw, anchor=_ANCHOR) == (None, None)  # type: ignore[arg-type]


def test_compute_valid_until_grace_by_kind() -> None:
    at = datetime(2026, 6, 11, 22, 0)
    assert normalize.compute_valid_until("meeting", at) == at + timedelta(hours=1)
    assert normalize.compute_valid_until("calendar", at) == at + timedelta(hours=1)
    assert normalize.compute_valid_until("reminder", at) == at + timedelta(hours=24)
    # Explicit range end pushes the grace anchor to the end.
    end = datetime(2026, 6, 11, 23, 30)
    assert normalize.compute_valid_until("meeting", at, end) == end + timedelta(hours=1)
    assert normalize.compute_valid_until("meeting", None) is None


def test_stamp_temporal_sets_fields_and_tolerates_failure() -> None:
    it = _mk(kind="meeting", scope="s", when="今天22:00", ts="2026-06-11T13:00")
    normalize.stamp_temporal(it)
    assert it.resolved_at == "2026-06-11T22:00:00"
    assert it.valid_until == "2026-06-11T23:00:00"
    vague = _mk(kind="meeting", scope="s", when="尽快", ts="2026-06-11T13:00")
    normalize.stamp_temporal(vague)
    assert vague.resolved_at is None and vague.valid_until is None


# --- fixtures -------------------------------------------------------------------


def _mk(
    *,
    kind: str,
    scope: str,
    when: str,
    ts: str,
    people: list[str] | None = None,
    confidence: float = 0.8,
    rationale: str = "",
) -> Intent:
    return Intent(
        kind=kind,
        scope=scope,
        confidence=confidence,
        rationale=rationale,
        ts=ts,
        payload={"when_text": when, "with": people or []},
        evidence=[IntentEvidence(source="timeline_block", ref_id="blk-1", quote=when)],
    )


# --- 面1: semantic fact folding (real 2026-06-11 duplicates) ---------------------


def test_chanyan_alignment_four_rows_fold_to_one(ac_root) -> None:  # noqa: ANN001
    """「产研对齐 22:00」: kind drifts calendar↔meeting, when_text comes in three
    surface forms, scopes span fast-K1 + two sessions → ONE canonical row."""
    rows = [
        _mk(kind="calendar", scope="fast-K1", when="22:00", ts="2026-06-11T13:05"),
        _mk(kind="meeting", scope="session-a1", when="今天22:00", ts="2026-06-11T13:07"),
        _mk(
            kind="calendar", scope="session-a1", when="6月11日 (今天) 22:00", ts="2026-06-11T14:02"
        ),
        _mk(kind="meeting", scope="session-b2", when="今天22:00", ts="2026-06-11T15:30"),
    ]
    with fts.cursor() as conn:
        assert sink.persist_intent(conn, rows[0]) is not None
        for late in rows[1:]:
            assert sink.persist_intent(conn, late) is None  # folded, never inserted
        got = intent_store.recent_intents(conn, start=_DAY_START, end=_DAY_END)
    assert len(got) == 1
    assert got[0].resolved_at == "2026-06-11T22:00:00"


def test_mina_feedback_three_rows_fold_to_one(ac_root) -> None:  # noqa: ANN001
    """「给 mina 写反馈 1h内」: `with` drifts ([mina,沈砚舟] / [沈砚舟] / [mina])
    but every later row's entities intersect the stored row's → ONE row."""
    rows = [
        _mk(
            kind="reminder",
            scope="fast-K1",
            when="1小时内",
            ts="2026-06-11T16:00",
            people=["mina", "沈砚舟"],
        ),
        _mk(
            kind="reminder",
            scope="session-a1",
            when="1小时内",
            ts="2026-06-11T16:05",
            people=["沈砚舟"],
        ),
        _mk(
            kind="reminder",
            scope="session-b2",
            when="1小时内",
            ts="2026-06-11T16:20",
            people=["mina"],
        ),
    ]
    with fts.cursor() as conn:
        assert sink.persist_intent(conn, rows[0]) is not None
        for late in rows[1:]:
            assert sink.persist_intent(conn, late) is None
        got = intent_store.recent_intents(conn, start=_DAY_START, end=_DAY_END)
    assert len(got) == 1
    assert got[0].kind == "reminder"


def test_fold_preserves_id_and_status_via_material_update(ac_root) -> None:  # noqa: ANN001
    """A fold hit with material confidence gain goes through the existing
    material_change UPDATE path: same row id, status untouched."""
    with fts.cursor() as conn:
        first = _mk(
            kind="calendar", scope="fast-K1", when="22:00", ts="2026-06-11T13:05", confidence=0.5
        )
        row_id = sink.persist_intent(conn, first)
        assert row_id is not None
        better = _mk(
            kind="meeting",
            scope="session-a1",
            when="今天22:00",
            ts="2026-06-11T13:30",
            confidence=0.9,
        )
        res = sink.persist_intent_result(conn, better, allow_material_update=True)
        assert res.outcome == "updated"
        assert res.row_id == row_id
        got = intent_store.recent_intents(conn, start=_DAY_START, end=_DAY_END)
    assert len(got) == 1
    assert got[0].id == row_id
    assert got[0].status == "open"
    assert got[0].confidence == 0.9


def test_no_fold_across_different_time_buckets(ac_root) -> None:  # noqa: ANN001
    """Same kind group + same day but 22:00 vs 16:00 are different facts."""
    with fts.cursor() as conn:
        assert (
            sink.persist_intent(
                conn, _mk(kind="meeting", scope="s1", when="22:00", ts="2026-06-11T13:00")
            )
            is not None
        )
        assert (
            sink.persist_intent(
                conn, _mk(kind="meeting", scope="s2", when="16:00", ts="2026-06-11T13:10")
            )
            is not None
        )
        got = intent_store.recent_intents(conn, start=_DAY_START, end=_DAY_END)
    assert len(got) == 2


def test_no_fold_when_people_disjoint(ac_root) -> None:  # noqa: ANN001
    """Same time bucket but disjoint non-empty `with` = two different meetings."""
    with fts.cursor() as conn:
        assert (
            sink.persist_intent(
                conn,
                _mk(
                    kind="meeting",
                    scope="s1",
                    when="22:00",
                    ts="2026-06-11T13:00",
                    people=["Alice"],
                ),
            )
            is not None
        )
        assert (
            sink.persist_intent(
                conn,
                _mk(
                    kind="meeting",
                    scope="s2",
                    when="今天22:00",
                    ts="2026-06-11T13:10",
                    people=["Bob"],
                ),
            )
            is not None
        )
        got = intent_store.recent_intents(conn, start=_DAY_START, end=_DAY_END)
    assert len(got) == 2


def test_no_fold_between_reminder_and_meeting(ac_root) -> None:  # noqa: ANN001
    """reminder is its own fold group — never folds with meeting/calendar."""
    with fts.cursor() as conn:
        assert (
            sink.persist_intent(
                conn, _mk(kind="meeting", scope="s1", when="22:00", ts="2026-06-11T13:00")
            )
            is not None
        )
        assert (
            sink.persist_intent(
                conn, _mk(kind="reminder", scope="s2", when="今天22:00", ts="2026-06-11T13:10")
            )
            is not None
        )
        got = intent_store.recent_intents(conn, start=_DAY_START, end=_DAY_END)
    assert len(got) == 2


def test_no_fold_outside_48h_window(ac_root) -> None:  # noqa: ANN001
    """A row recognized >48h earlier (e.g. an earlier week's standup) never
    folds — different surface forms keep distinct keys, and both the 48h
    candidate window and the ±30min bucket reject the old row."""
    with fts.cursor() as conn:
        assert (
            sink.persist_intent(
                conn, _mk(kind="meeting", scope="s1", when="22:00", ts="2026-06-08T13:00")
            )
            is not None
        )
        assert (
            sink.persist_intent(
                conn, _mk(kind="meeting", scope="s2", when="今天22:00", ts="2026-06-11T13:00")
            )
            is not None
        )
        got = intent_store.recent_intents(conn, start="2026-06-01T00:00", end="2026-06-30T00:00")
    assert len(got) == 2


def test_unparseable_when_text_never_folds_semantically(ac_root) -> None:  # noqa: ANN001
    """No temporal grounding → pre-#546 behavior (insert; exact dedup only)."""
    with fts.cursor() as conn:
        assert (
            sink.persist_intent(
                conn, _mk(kind="meeting", scope="s1", when="尽快", ts="2026-06-11T13:00")
            )
            is not None
        )
        assert (
            sink.persist_intent(
                conn, _mk(kind="meeting", scope="s2", when="回头", ts="2026-06-11T13:10")
            )
            is not None
        )
        got = intent_store.recent_intents(conn, start=_DAY_START, end=_DAY_END)
    assert len(got) == 2
    assert all(it.resolved_at is None and it.valid_until is None for it in got)


# --- fuzzy content fold (重复推送相同语义修复) -----------------------------------
#
# The exact-equality ungrounded content fold missed the COMMON real case — the
# SAME to-do re-recognized with drifted wording every session. The char-bigram
# Jaccard fold (sink._body_similarity ≥ content_fold_similarity, default 0.72)
# collapses near-identical restatements deterministically. It is deliberately
# CONSERVATIVE: only high-overlap rephrasings fold; the semantic-but-lexically-
# distant mid-band (e.g. "查明…相关的产品" vs "查明…本地…产品信息") is the
# app-side LLM deduper's job at push time, not this zero-LLM source fold.


def _mk_content(*, kind: str, scope: str, text: str, ts: str, confidence: float = 0.8) -> Intent:
    """An UNGROUNDED content intent (no when_text → resolved_at None → content-fold
    path), carrying its fact body in ``payload.text`` like a real info_need/reminder."""
    return Intent(
        kind=kind,
        scope=scope,
        confidence=confidence,
        rationale="",
        ts=ts,
        payload={"text": text},
        evidence=[IntentEvidence(source="timeline_block", ref_id="blk-1", quote=text)],
    )


def test_body_similarity_is_jaccard_with_exact_and_distinct_poles() -> None:
    """Identical → 1.0; near-identical drift ≥ 0.72; genuinely distinct ≪ 0.72."""
    assert sink._body_similarity("abc", "abc") == 1.0
    assert sink._body_similarity("", "") == 1.0
    # Real production duplicates (normalized) — near-identical rephrasings.
    near = sink._body_similarity(
        sink._content_body({"text": "随这次合并添加 GitHub Actions 所需的 secret 和 labels"}),
        sink._content_body({"text": "随PR #102合并添加GitHub Actions所需的secret和labels"}),
    )
    assert near >= 0.72
    # Genuinely distinct same-topic to-dos must score far below the threshold.
    distinct = sink._body_similarity(
        sink._content_body({"text": "验证几何v2 OCR在改变侧边栏宽度和窗口大小后是否仍能正常识别"}),
        sink._content_body({"text": "跑AB测试对比几何置信度方案与远程视觉方案的OCR结构化效果"}),
    )
    assert distinct < 0.72


def test_fuzzy_fold_collapses_near_identical_reminder_rephrasings(ac_root) -> None:  # noqa: ANN001
    """A pair of near-identical (≥0.72) reminder rephrasings folds to ONE open row;
    the full 6-phrasing PR#102 family collapses to strictly fewer than 6 rows."""
    pair = [
        _mk_content(
            kind="reminder",
            scope="session-a",
            text="随这次合并添加 GitHub Actions 所需的 secret 和 labels",
            ts="2026-06-11T13:00",
        ),
        _mk_content(
            kind="reminder",
            scope="session-b",
            text="随PR #102合并添加GitHub Actions所需的secret和labels",
            ts="2026-06-11T13:05",
        ),
    ]
    with fts.cursor() as conn:
        assert sink.persist_intent(conn, pair[0]) is not None
        assert sink.persist_intent(conn, pair[1]) is None  # folded, never inserted
        got = intent_store.recent_intents(conn, start=_DAY_START, end=_DAY_END)
    assert len(got) == 1

    family = [
        "随这次合并添加 GitHub Actions 所需的 secret 和 labels",
        "随PR #102合并添加GitHub Actions所需的secret和labels",
        "随 PR #102 合并后，需要添加 GitHub Actions 所需的 secret 和 labels",
        "随 PR #102 合并添加 GitHub Actions 的 secret 和 labels（PR #102 已合并但该操作未执行）",
        "为 PR #102 合并到的仓库添加 GitHub Actions 的 secret 和 labels（PR #102 已合并但该操作未执行）",
        "为 PR #102 合并到的仓库添加 GitHub Actions 的 secret 和 labels",
    ]
    with fts.cursor() as conn:
        for i, text in enumerate(family):
            sink.persist_intent(
                conn,
                _mk_content(kind="reminder", scope=f"s{i}", text=text, ts=f"2026-06-12T13:{i:02d}"),
            )
        fam = intent_store.recent_intents(conn, start="2026-06-12T00:00", end="2026-06-13T00:00")
    assert 1 <= len(fam) < len(family)  # real reduction; conservative threshold keeps a few


def test_fuzzy_fold_keeps_distinct_same_topic_apart(ac_root) -> None:  # noqa: ANN001
    """Two genuinely-distinct same-topic info_needs (Jaccard ≪ threshold) stay TWO
    rows — the conservative threshold must not over-fold different tasks."""
    with fts.cursor() as conn:
        assert (
            sink.persist_intent(
                conn,
                _mk_content(
                    kind="info_need",
                    scope="s1",
                    text="验证几何v2 OCR在改变侧边栏宽度和窗口大小后是否仍能正常识别",
                    ts="2026-06-11T13:00",
                ),
            )
            is not None
        )
        assert (
            sink.persist_intent(
                conn,
                _mk_content(
                    kind="info_need",
                    scope="s2",
                    text="跑AB测试对比几何置信度方案与远程视觉方案的OCR结构化效果",
                    ts="2026-06-11T13:10",
                ),
            )
            is not None
        )
        got = intent_store.recent_intents(conn, start=_DAY_START, end=_DAY_END)
    assert len(got) == 2


def test_fuzzy_fold_killswitch_off_is_exact_only(ac_root, monkeypatch) -> None:  # noqa: ANN001
    """content_fold_fuzzy_enabled=false → the near-identical pair does NOT fold
    (exact-only, pre-fuzzy behavior): two separate rows."""
    cfg = load_config()
    cfg.intent_recognizer.content_fold_fuzzy_enabled = False
    monkeypatch.setattr(sink.config_mod, "load", lambda *a, **k: cfg)
    with fts.cursor() as conn:
        assert (
            sink.persist_intent(
                conn,
                _mk_content(
                    kind="reminder",
                    scope="s1",
                    text="随这次合并添加 GitHub Actions 所需的 secret 和 labels",
                    ts="2026-06-11T13:00",
                ),
            )
            is not None
        )
        assert (
            sink.persist_intent(
                conn,
                _mk_content(
                    kind="reminder",
                    scope="s2",
                    text="随PR #102合并添加GitHub Actions所需的secret和labels",
                    ts="2026-06-11T13:05",
                ),
            )
            is not None  # NOT folded under exact-only
        )
        got = intent_store.recent_intents(conn, start=_DAY_START, end=_DAY_END)
    assert len(got) == 2


# --- 面2: lifecycle (valid_until at persist + daily harvest + read filters) ------


def test_persist_writes_valid_until_with_kind_grace(ac_root) -> None:  # noqa: ANN001
    with fts.cursor() as conn:
        sink.persist_intent(
            conn, _mk(kind="meeting", scope="s1", when="今天22:00", ts="2026-06-11T13:00")
        )
        sink.persist_intent(
            conn, _mk(kind="reminder", scope="s2", when="1小时内", ts="2026-06-11T16:00")
        )
        got = intent_store.recent_intents(conn, start=_DAY_START, end=_DAY_END)
    by_kind = {it.kind: it for it in got}
    assert by_kind["meeting"].valid_until == "2026-06-11T23:00:00"  # 22:00 + 1h
    assert by_kind["reminder"].valid_until == "2026-06-12T17:00:00"  # 17:00 deadline + 24h


def test_expire_overdue_harvests_only_stale_open(ac_root) -> None:  # noqa: ANN001
    with fts.cursor() as conn:
        stale = _mk(kind="meeting", scope="s1", when="今天22:00", ts="2026-06-11T13:00")
        fresh = _mk(kind="meeting", scope="s2", when="明天上午10点", ts="2026-06-11T13:00")
        ungrounded = _mk(kind="meeting", scope="s3", when="尽快", ts="2026-06-11T13:00")
        dismissed = _mk(kind="meeting", scope="s4", when="今天20:00", ts="2026-06-11T13:00")
        dismissed.status = "dismissed"
        for it in (stale, fresh, ungrounded, dismissed):
            sink.persist_intent(conn, it)
        n = len(intent_store.expire_overdue(conn, now="2026-06-11T23:55:00"))
        assert n == 1  # only the stale open row
        got = {
            it.scope: it.status
            for it in intent_store.recent_intents(conn, start=_DAY_START, end=_DAY_END)
        }
    assert got == {"s1": "expired", "s2": "open", "s3": "open", "s4": "dismissed"}


def test_expire_overdue_is_idempotent(ac_root) -> None:  # noqa: ANN001
    with fts.cursor() as conn:
        sink.persist_intent(
            conn, _mk(kind="meeting", scope="s1", when="今天22:00", ts="2026-06-11T13:00")
        )
        assert len(intent_store.expire_overdue(conn, now="2026-06-11T23:55:00")) == 1
        assert len(intent_store.expire_overdue(conn, now="2026-06-11T23:56:00")) == 0


def test_is_expired_read_side_filter() -> None:
    now = "2026-06-11T23:30:00"
    harvested = Intent(kind="meeting", scope="s", status="expired")
    stale_open = Intent(kind="meeting", scope="s", status="open", valid_until="2026-06-11T23:00:00")
    fresh_open = Intent(kind="meeting", scope="s", status="open", valid_until="2026-06-12T11:00:00")
    ungrounded = Intent(kind="meeting", scope="s", status="open")
    consumed = Intent(
        kind="meeting", scope="s", status="consumed", valid_until="2026-06-11T23:00:00"
    )
    assert intent_store.is_expired(harvested, now=now)
    assert intent_store.is_expired(stale_open, now=now)  # harvest hasn't run yet
    assert not intent_store.is_expired(fresh_open, now=now)
    assert not intent_store.is_expired(ungrounded, now=now)
    assert not intent_store.is_expired(consumed, now=now)


def test_material_update_never_wipes_temporal_grounding(ac_root) -> None:  # noqa: ANN001
    """A material re-recognition WITHOUT temporal grounding (resolved_at None)
    must keep the stored resolved_at/valid_until (COALESCE ratchet) — wiping
    valid_until would pull the row back out of the expiry lifecycle."""
    with fts.cursor() as conn:
        first = _mk(
            kind="meeting",
            scope="s1",
            when="今天22:00",
            ts="2026-06-11T13:00",
            confidence=0.5,
            people=["Alice"],
        )
        row_id = sink.persist_intent(conn, first)
        assert row_id is not None
        regrounded = _mk(
            kind="meeting",
            scope="s1",
            when="今天22:00",
            ts="2026-06-11T14:00",
            confidence=0.9,
            people=["Alice"],
        )
        assert regrounded.resolved_at is None  # caller did not stamp
        intent_store.update_intent_recognition(
            conn,
            intent_id=row_id,
            intent=regrounded,
            canonical_key=intent_store.dedup_key(regrounded),
        )
        got = intent_store.recent_intents(conn, start=_DAY_START, end=_DAY_END)
    assert got[0].confidence == 0.9
    assert got[0].resolved_at == "2026-06-11T22:00:00"
    assert got[0].valid_until == "2026-06-11T23:00:00"


def test_scene_layer_excludes_expired(ac_root) -> None:  # noqa: ANN001
    """recall ① 场景意图层 must not inject stale commitments.

    ``_scene_layer`` filters against wall-clock now, so the fixture anchors on
    real time: the stale meeting happened yesterday (harvested → expired), the
    fresh one is tomorrow.
    """
    scope = "session-x"
    yesterday = (datetime.now() - timedelta(days=1)).replace(hour=13, minute=0)
    today = datetime.now()
    with fts.cursor() as conn:
        stale = _mk(
            kind="meeting",
            scope=scope,
            when="今天22:00",
            ts=yesterday.isoformat(timespec="minutes"),
            rationale="产研对齐",
        )
        fresh = _mk(
            kind="meeting",
            scope=scope,
            when="明天上午10点",
            ts=today.isoformat(timespec="minutes"),
            rationale="和 Bob 过方案",
        )
        sink.persist_intent(conn, stale)
        sink.persist_intent(conn, fresh)
        n = len(intent_store.expire_overdue(conn, now=datetime.now().isoformat(timespec="seconds")))
        assert n == 1
        budget = recall._Budget(2000)
        lines = recall._scene_layer(conn, scope, budget)
    assert any("和 Bob 过方案" in line for line in lines)
    assert not any("产研对齐" in line for line in lines)


# --- 2026-06-12 生产实测修复：people 单侧为空放行 + meeting_hint 折叠/TTL ----------


def test_fold_one_side_empty_people_is_compatible(ac_root) -> None:  # noqa: ANN001
    """生产 id 10-13 形态：同一 22:00，一行带 with=[Vanessa] 其余三行 with 为空——
    单侧为空 = 兼容（识别抖动丢 with），必须折成一行。"""
    rows = [
        _mk(kind="calendar", scope="s1", when="6月11日 (今天) 22:00", ts="2026-06-11T21:59"),
        _mk(kind="calendar", scope="s1", when="22:00", ts="2026-06-11T22:00"),
        _mk(kind="meeting", scope="s1", when="22:00", ts="2026-06-11T22:01", people=["Vanessa"]),
        _mk(kind="calendar", scope="s1", when="今天22:00", ts="2026-06-11T22:04"),
    ]
    with fts.cursor() as conn:
        assert sink.persist_intent(conn, rows[0]) is not None
        for late in rows[1:]:
            assert sink.persist_intent(conn, late) is None
        got = intent_store.recent_intents(conn, start=_DAY_START, end=_DAY_END)
    assert len(got) == 1


def _hint(
    *, scope: str, ts: str, when: str = "", people: list[str] | None = None, rationale: str = ""
) -> Intent:
    payload: dict = {"with": people or []}
    if when:
        payload["when_text"] = when
    return Intent(
        kind="meeting_hint",
        scope=scope,
        confidence=0.35,
        rationale=rationale or "婉转见面意愿",
        ts=ts,
        payload=payload,
        evidence=[IntentEvidence(source="timeline_block", ref_id="blk-1", quote=rationale)],
    )


def test_hint_same_phrase_overlapping_people_folds(ac_root) -> None:  # noqa: ANN001
    """生产 id 14-17 形态：「下次周会」同对象 hint 连发 → 折成一行（跨 scope 也折）。"""
    with fts.cursor() as conn:
        assert (
            sink.persist_intent(
                conn, _hint(scope="s1", ts="2026-06-12T01:30", when="下次周会", people=["Vanessa"])
            )
            is not None
        )
        assert (
            sink.persist_intent(
                conn,
                _hint(
                    scope="s2",
                    ts="2026-06-12T01:42",
                    when="下次周会",
                    people=["Vanessa", "James"],
                ),
            )
            is None
        )
        got = intent_store.recent_intents(conn, start=_DAY_START, end="2026-06-13T00:00")
    assert len(got) == 1


def test_hint_blank_phrase_folds_only_within_same_scope(ac_root) -> None:  # noqa: ANN001
    """无短语 hint 的折叠边界。注意 people 集合完全相等的情形早被 exact
    dedup_key（kind|when|people，无 scope 前缀）全局折掉——本规则管的是
    people 交集但集合不等的剩余形态，那时 scope 是仅存的身份信号。"""
    with fts.cursor() as conn:
        first = _hint(scope="s1", ts="2026-06-12T01:30", people=["Vanessa"], rationale="约着聊聊")
        assert sink.persist_intent(conn, first) is not None
        # 同 scope、对象交集非空（集合不等，避开 exact key）、双方无短语 → 折。
        again = _hint(
            scope="s1", ts="2026-06-12T01:40", people=["Vanessa", "James"], rationale="提议聊聊"
        )
        assert sink.persist_intent(conn, again) is None
        # 不同 scope 的无短语 hint（同样交集非空、集合不等）——身份信号不足，不折。
        other = _hint(
            scope="s2", ts="2026-06-12T02:00", people=["Vanessa", "Bob"], rationale="测一波bug"
        )
        assert sink.persist_intent(conn, other) is not None


def test_hint_different_phrase_or_disjoint_people_never_folds(ac_root) -> None:  # noqa: ANN001
    with fts.cursor() as conn:
        assert (
            sink.persist_intent(
                conn, _hint(scope="s1", ts="2026-06-12T01:30", when="下次周会", people=["Vanessa"])
            )
            is not None
        )
        # 同对象但短语不同（不同议题/不同惯例锚）→ 不折。
        assert (
            sink.persist_intent(
                conn, _hint(scope="s1", ts="2026-06-12T01:35", when="等发布完", people=["Vanessa"])
            )
            is not None
        )
        # 同短语但对象不相交 → 不折。
        assert (
            sink.persist_intent(
                conn, _hint(scope="s1", ts="2026-06-12T01:50", when="下次周会", people=["Bob"])
            )
            is not None
        )
        # 无对象的 hint 没有身份信号 → 不参与折叠。
        assert sink.persist_intent(conn, _hint(scope="s1", ts="2026-06-12T01:55")) is not None


def test_hint_gets_seven_day_ttl(ac_root) -> None:  # noqa: ANN001
    """hint 无可解析锚 → valid_until 锚在识别时间 + 7d（不再永不过期）。"""
    it = _hint(scope="s1", ts="2026-06-12T01:30", when="改天", people=["Vanessa"])
    with fts.cursor() as conn:
        row_id = sink.persist_intent(conn, it)
        assert row_id is not None
        got = intent_store.recent_intents(conn, start=_DAY_START, end="2026-06-20T00:00")
    assert got[0].resolved_at is None
    assert got[0].valid_until == "2026-06-19T01:30:00"


def test_restamp_overdue_grounding_backfills_and_expires(ac_root) -> None:  # noqa: ANN001
    """stale-daemon 修复工具：直插的无章行被补章；过期者随即可收割；幂等。"""
    with fts.cursor() as conn:
        # 模拟旧 daemon 写法：绕过 sink 直插（无 resolved_at/valid_until）。
        legacy = _mk(kind="calendar", scope="s1", when="22:00", ts="2026-06-11T21:59")
        legacy_id = intent_store.insert_intent(conn, legacy)
        hint = _hint(scope="s1", ts="2026-06-11T20:00", when="改天", people=["Vanessa"])
        intent_store.insert_intent(conn, hint)
        rescanned, restamped = intent_store.restamp_overdue_grounding(conn)
        assert rescanned == 2 and restamped == 2
        # 22:00+1h 宽限早已过 → 收割。
        expired = len(intent_store.expire_overdue(conn, now="2026-06-12T05:00:00"))
        assert expired == 1
        got = {i.id: i for i in intent_store.recent_intents(conn, start=_DAY_START, end=_DAY_END)}
        assert legacy_id not in got or got[legacy_id].status == "expired"
        # 幂等：再跑不再改任何行。
        assert intent_store.restamp_overdue_grounding(conn)[1] == 0


# --- regressions: 新一轮 auto-review (#564 / #586 / #587) --------------------


@pytest.mark.parametrize("raw", ["晚上12点", "今晚12点", "夜里12点"])
def test_evening_midnight_resolves_to_next_day(raw: str) -> None:
    """#564: 晚上/今晚/夜里12点 = 今天结束的那个午夜 = 次日 00:00，而不是 anchor 当天
    00:00（一个已过去的时间戳，会让 intent 一落库即过期被静默丢弃）。"""
    resolved, _end = normalize.resolve_when_text(raw, anchor=_ANCHOR)
    assert resolved == datetime.fromisoformat("2026-06-12T00:00"), raw


def test_evening_ten_pm_unchanged_regression_guard() -> None:
    """只有午夜 12 点推进；晚上十点仍是当天 22:00（防过度推进）。"""
    resolved, _end = normalize.resolve_when_text("晚上十点", anchor=_ANCHOR)
    assert resolved == datetime.fromisoformat("2026-06-11T22:00")


# --- #618: 相对周序前缀 + 同日已过时刻必须滚到下一个未来实例 -----------------------
# anchor = 2026-06-11 周四 13:00。weekday 分支若不识别 下/下下 前缀、且 ahead==0
# 时不向前滚一周，会把未来承诺解析到过去 → valid_until 落在过去 → 一落库即被收割。


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        # 下周X：anchor 周四 → 下周五 = 06-19（不是本周 06-12）；下周四 = 06-18。
        ("下周五下午3点", datetime(2026, 6, 19, 15, 0)),
        ("下周四上午9点", datetime(2026, 6, 18, 9, 0)),
        # 下周一/三 同理滚到下一周（本周一/三虽已过，但 下 前缀也要 +7）。
        ("下周一10点", datetime(2026, 6, 15, 10, 0)),
        ("下周三14:00", datetime(2026, 6, 17, 14, 0)),
        # 下下周X = +14 天再对齐到该 weekday：下下周五 = 06-26。
        ("下下周五15:00", datetime(2026, 6, 26, 15, 0)),
    ],
)
def test_relative_week_prefix_rolls_forward(when: str, expected: datetime) -> None:
    """#618: 「下周X / 下下周X」前缀必须把 weekday 滚到下一周 / 下下周，
    而不是被正则丢弃后落在本周（一个可能已过去的日期）。"""
    resolved, _end = normalize.resolve_when_text(when, anchor=_ANCHOR)
    assert resolved == expected, when


def test_same_weekday_passed_time_rolls_to_next_week() -> None:
    """#618: 当天就是周X、说「周X <已过时刻>」时 ahead==0，若不滚一周就会解析到
    今天已过去的时刻 → 落库即过期。anchor 周四 13:00 说「周四上午9点」(9<13 已过)
    应指下周四 06-18 09:00。"""
    resolved, _end = normalize.resolve_when_text("周四上午9点", anchor=_ANCHOR)
    assert resolved == datetime(2026, 6, 18, 9, 0)


def test_same_weekday_future_time_stays_today() -> None:
    """#618 防过度滚动：当天周X 说「周X <未来时刻>」(晚于 anchor) 仍指今天。
    anchor 周四 13:00 说「周四晚上8点」(20:00>13:00) 应是今天 06-11 20:00。"""
    resolved, _end = normalize.resolve_when_text("周四晚上8点", anchor=_ANCHOR)
    assert resolved == datetime(2026, 6, 11, 20, 0)


def test_future_weekday_unchanged_regression_guard() -> None:
    """#618 回归守护：不带相对前缀、ahead>0 的 weekday 行为不变（周五=次日 06-12）。"""
    resolved, _end = normalize.resolve_when_text("周五下午3点", anchor=_ANCHOR)
    assert resolved == datetime(2026, 6, 12, 15, 0)


def test_valid_until_passed_compares_instants_not_strings() -> None:
    """#586: offset-aware valid_until vs now 必须按瞬时比较，而非字典序——offset 不同
    时字典序会把判定反转。"""
    vu = "2026-06-14T01:00:00+08:00"  # = 2026-06-13T17:00Z
    now = "2026-06-14T00:00:00+00:00"  # = 2026-06-14T00:00Z（更晚的瞬时）
    assert intent_store._valid_until_passed(vu, now) is True  # 已过期
    assert (vu < now) is False  # 字典序会误判为未过期
    assert intent_store._valid_until_passed(now, vu) is False  # 反向仍正确


def test_cross_kind_fold_refreshes_kind(ac_root) -> None:  # noqa: ANN001
    """#587: material 跨 kind 折叠（calendar 重述折到 meeting 行）把 kind 与
    dedup_key/payload 一并迁到 incoming，行保持自洽（kind 不与路由键脱节）。"""
    with fts.cursor() as conn:
        first = _mk(
            kind="meeting", scope="fast-K1", when="今天22:00", ts="2026-06-11T13:05", confidence=0.6
        )
        assert sink.persist_intent(conn, first) is not None
        later = _mk(
            kind="calendar",
            scope="session-a1",
            when="今天22:00",
            ts="2026-06-11T13:30",
            confidence=0.9,  # 置信度上升 → material UPDATE 折叠路径
        )
        # material UPDATE 折叠只在 allow_material_update=True 入口触发（生产识别器用此口）。
        res = sink.persist_intent_result(conn, later, allow_material_update=True)
        assert res.outcome == "updated"  # folded onto the meeting row, not inserted
        got = intent_store.recent_intents(conn, start=_DAY_START, end=_DAY_END)
    assert len(got) == 1
    assert got[0].kind == "calendar"  # kind 随 incoming 迁移，与 dedup_key 自洽

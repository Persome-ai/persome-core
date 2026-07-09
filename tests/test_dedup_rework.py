"""Intent dedup — L1 deterministic golden (default gate, zero LLM, offline).

The daemon-side source fold is deliberately **precision-first**: for proactive push,
folding two genuinely-different intents into one row (over-fold / 误吞) silently drops
a real to-do the user should see — strictly worse than leaving a duplicate. So this
layer stays conservative (`content_fold_similarity` 0.72) and this file's PRIMARY job
is the over-fold guards: distinct intents MUST stay separate.

Adversarial review (spec docs/superpowers/specs/2026-06-18-intent-dedup-rework) proved
char-bigram Jaccard CANNOT separate a "reworded same-intent" from a "one-keyword-apart
DISTINCT to-do": measured on the production sink, distinct pairs ("密钥"/"标签" 0.714,
"PR#102"/"PR#103" 0.733) sit ABOVE the legit same-intent paraphrase ("两个项目" added,
0.700). No single threshold folds the latter without over-folding the former. Lowering
the threshold to chase that recall therefore trades away precision — rejected. The
lexically-drifted same-intent mid-band (modes A/B/D below) is left to the **app-side LLM
deduper** at push time (the semantic layer). Those cases are kept here as strict-xfail
oracles: they document the boundary and flip to xpass (tripping the gate) if a future
semantic fold lands daemon-side.
"""

from __future__ import annotations

import sqlite3

import pytest

from persome.intent import sink
from persome.intent import store as intent_store
from persome.intent.ontology import Intent

# All ts within the 48h fold candidate window so "did not fold" can only mean the
# discriminator rejected it — never that the candidate fell out of the window.
_T = "2026-06-18T"


def _mk(
    kind: str,
    scope: str,
    hhmm: str,
    *,
    text: str | None = None,
    when_text: str | None = None,
    people: list[str] | None = None,
    rationale: str = "",
) -> Intent:
    payload: dict = {}
    if text is not None:
        payload["text"] = text
    if when_text is not None:
        payload["when_text"] = when_text
    if people is not None:
        payload["with"] = list(people)
    return Intent(kind=kind, scope=scope, ts=f"{_T}{hhmm}", rationale=rationale, payload=payload)


def _persist_count(intents: list[Intent]) -> int:
    """Persist intents in order through the real sink; return surviving row count."""
    conn = sqlite3.connect(":memory:")
    try:
        intent_store.ensure_schema(conn)
        for it in intents:
            sink.persist_intent(conn, it)
        return conn.execute("SELECT COUNT(*) FROM intents").fetchone()[0]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# OVER-FOLD GUARDS — distinct intents MUST stay separate (the precision core)  #
# GREEN at the shipped 0.72; go RED if anyone lowers the fuzzy threshold into   #
# the danger band — that RED is the honest "you just traded away precision".    #
# --------------------------------------------------------------------------- #


def test_guard_distinct_todos_stay_separate() -> None:
    """Two semantically distinct info_needs in one scene never fold (sim ~0.0)."""
    intents = [
        _mk("info_need", "s", "10:00", text="调研某工具的连接方式"),
        _mk("info_need", "s", "10:05", text="比较两个项目的意图识别差异"),
    ]
    assert _persist_count(intents) == 2


def test_guard_one_keyword_apart_todos_stay_separate() -> None:
    """Adversarial case: "添加密钥" vs "添加标签" (separately-actionable subtasks, body sim
    0.714) MUST stay 2 rows. GREEN at 0.72; folding them (a threshold ≤0.71 would) is the
    exact 误吞 this layer refuses for proactive push."""
    intents = [
        _mk("reminder", "s", "10:00", text="合并后给仓库添加所需的密钥"),
        _mk("reminder", "s", "10:05", text="合并后给仓库添加所需的标签"),
    ]
    assert _persist_count(intents) == 2


def test_guard_one_word_swap_todos_stay_separate() -> None:
    """Second adversarial case: "调研…连接方式" vs "…部署方式" (distinct research, sim 0.667)
    MUST stay 2 rows — guards the threshold against the 0.55–0.70 band where distinct pairs
    sit alongside legit same-intent paraphrases (provably inseparable by bigram Jaccard)."""
    intents = [
        _mk("info_need", "s", "10:00", text="调研某框架的连接方式"),
        _mk("info_need", "s", "10:05", text="调研某框架的部署方式"),
    ]
    assert _persist_count(intents) == 2


def test_guard_distinct_meetings_same_time_different_people_stay_separate() -> None:
    """Two different grounded meetings at the SAME clock time but DIFFERENT people — the
    grounded fold's people-block is the ONLY thing keeping them apart, so this pins that
    block: it must NOT be loosened to fold a real schedule clash into one row."""
    intents = [
        _mk(
            "meeting",
            "s",
            "09:00",
            when_text="今天14:30",
            people=["客户甲"],
            rationale="和客户甲评审设计稿",
        ),
        _mk(
            "meeting",
            "s",
            "09:05",
            when_text="今天14:30",
            people=["团队乙"],
            rationale="和团队乙产研对齐",
        ),
    ]
    assert _persist_count(intents) == 2


def test_guard_different_kind_reminder_vs_info_need_stay_separate() -> None:
    """reminder and info_need are NOT one fold family — a dated promise is not a query."""
    intents = [
        _mk("reminder", "s", "11:00", text="合并后给仓库添加所需的密钥和标签"),
        _mk("info_need", "s", "11:05", text="查看后端契约文档了解所需字段"),
    ]
    assert _persist_count(intents) == 2


# --------------------------------------------------------------------------- #
# Sanity: cross-scope IDENTICAL body folds even at the conservative threshold  #
# --------------------------------------------------------------------------- #


def test_sanity_identical_body_cross_scope_folds() -> None:
    """A verbatim re-recognition (sim 1.0) of the same to-do across two sessions folds."""
    intents = [
        _mk("info_need", "session-1", "01:29", text="调研某工具的连接方式"),
        _mk("info_need", "session-2", "01:31", text="调研某工具的连接方式"),
    ]
    assert _persist_count(intents) == 1


# --------------------------------------------------------------------------- #
# DAEMON-SIDE LIMITS — same-intent the source fold deliberately does NOT fold  #
# (deferred to the app-side LLM deduper). strict-xfail = documented boundary + #
# a ratchet that trips if a future daemon-side semantic fold lands.            #
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(
    reason="Same-intent reworded across sessions (body sim ~0.70). Folding it daemon-side "
    "would require a threshold ≤0.70, which adversarial review proved also folds "
    "one-keyword-apart DISTINCT to-dos (sim 0.71–0.73 ≥ this 0.70) — an unacceptable 误吞 "
    "for proactive push. Deferred to the app-side LLM deduper. strict-xfail.",
    strict=True,
)
def test_mode_ab_same_intent_paraphrase_deferred_to_app_llm() -> None:
    intents = [
        _mk("info_need", "session-1", "13:50", text="比较甲和乙在意图识别上的版本差异"),
        _mk("info_need", "session-2", "14:12", text="比较甲和乙两个项目在意图识别上的版本差异"),
    ]
    assert _persist_count(intents) == 1


@pytest.mark.xfail(
    reason="Same to-do tagged info_need one tick then assignment the next. Merging the two "
    "kinds into one content-fold family only folds them via the SAME fuzzy threshold, which "
    "would then also fuzzily fold a cross-kind one-keyword-apart pair — the same precision "
    "risk, now across kinds. Left to the app-side LLM deduper. strict-xfail.",
    strict=True,
)
def test_mode_d_info_need_assignment_kind_drift_deferred_to_app_llm() -> None:
    body = "跑 AB 测试对比方案甲与方案乙的结构化效果"
    intents = [
        _mk("info_need", "session-x", "14:42", text=body),
        _mk("assignment", "session-x", "14:44", text=body),
    ]
    assert _persist_count(intents) == 1


@pytest.mark.xfail(
    reason="meeting_hint's topic lives in `rationale` (untrusted LLM prose), NOT in any payload "
    "field the fold trusts — and meeting_hint is a conf<=0.4 NON-proposable kind (never surfaced), "
    "so its over-split is low-harm. Folding it safely needs the recognizer to extract topic into "
    "payload first. strict-xfail.",
    strict=True,
)
def test_mode_c_meeting_hint_with_when_drift_folds() -> None:
    intents = [
        _mk(
            "meeting_hint",
            "session-a",
            "16:00",
            when_text="明天晚上",
            people=["某同事"],
            rationale="明天晚上聊 Onboarding 一期 PRD 的问题",
        ),
        _mk(
            "meeting_hint",
            "session-b",
            "16:20",
            when_text="明天 晚上",
            people=["某群成员"],
            rationale="明天晚上讨论 Onboarding 一期 PRD",
        ),
        _mk(
            "meeting_hint",
            "session-c",
            "16:44",
            when_text="明天...晚上",
            people=["某群"],
            rationale="明天晚上 Onboarding 一期 PRD 批注讨论",
        ),
    ]
    assert _persist_count(intents) == 1


@pytest.mark.xfail(
    reason="Mirror of the meeting_hint limit: two hints with IDENTICAL trusted payload (same "
    "when_text, empty with) but different rationale share a grounded dedup_key and fold today. "
    "meeting_hint is non-proposable (never surfaced) so this over-fold is low-harm; discriminating "
    "it would require trusting `rationale`. strict-xfail.",
    strict=True,
)
def test_meeting_hint_same_when_different_topic_overfold_known() -> None:
    intents = [
        _mk(
            "meeting_hint",
            "s",
            "16:00",
            when_text="明天晚上",
            rationale="明天晚上聊 Onboarding 一期 PRD",
        ),
        _mk(
            "meeting_hint", "s", "16:05", when_text="明天晚上", rationale="明天晚上聊新版本发布排期"
        ),
    ]
    assert _persist_count(intents) == 2

"""Semantic (embedding) content fold — the paraphrase layer the char-bigram Jaccard
misses (intent.sink._find_content_fold_target second-chance via intent.embeddings).

Production failure this guards: the same Feishu assignment ("沈砚舟 交办: 去掉动画 + 修
Home/Calendar→Task 导航回退 + dev-only") was recognized in 3 sessions as 3 separate
`assignment` rows (140/141/143) with the task worded differently each time. Pairwise
char-bigram Jaccard = 0.27/0.35/0.52 (all < 0.72), so the lexical fold never collapsed
them; bge cosine = ~0.88, so the semantic fold does.

These exercise the REAL bge model, so the fold-positive cases skip when it is absent
(``embeddings.available()`` is False — onnxruntime is a hard dep so it should be present
locally / in CI). The negative + kill-switch + people-guard cases hold regardless.
"""

from __future__ import annotations

import pytest

from persome.config import load as load_config
from persome.intent import embeddings, sink
from persome.intent import store as intent_store
from persome.intent.ontology import Intent, IntentEvidence
from persome.store import fts

_DAY_START = "2026-06-11T00:00"
_DAY_END = "2026-06-12T00:00"


@pytest.fixture
def semantic_on(monkeypatch):  # noqa: ANN001, ANN201
    """Force the OPT-IN semantic fold on (it is default-OFF — adversarial review showed
    a cosine threshold over-folds one-keyword-apart distinct to-dos, so the production
    default leaves the semantic mid-band to the LLM; these tests exercise the opt-in)."""
    cfg = load_config()
    cfg.intent_recognizer.semantic_fold_enabled = True
    cfg.intent_recognizer.semantic_fold_similarity = 0.82
    cfg.intent_recognizer.content_fold_fuzzy_enabled = True
    monkeypatch.setattr(sink.config_mod, "load", lambda *a, **k: cfg)
    return cfg


# Real production task_texts of rows 140/141/143 (same commitment, drifted wording).
_T140 = "修复Acme bug：把动画去掉，并修复从Home/Calendar点击Task后无法返回的导航问题"
_T141 = (
    "去掉动画，修复从Home和Calendar点到Task后无法回退的导航bug，并将那个奇怪的UI元素改为仅dev可见"
)
_T143 = "修Acme的动画去掉、从Home/Calendar点击Task后无法返回的导航问题，以及将某些功能只开放给dev"

needs_model = pytest.mark.skipif(
    not embeddings.available(), reason="bge embedding model not available"
)


def _mk(
    *,
    kind: str,
    scope: str,
    ts: str,
    task_text: str = "",
    text: str = "",
    people: list[str] | None = None,
    confidence: float = 0.8,
) -> Intent:
    payload: dict = {}
    if task_text:
        payload["task_text"] = task_text
    if text:
        payload["text"] = text
    if people is not None:
        payload["with"] = people
    return Intent(
        kind=kind,
        scope=scope,
        confidence=confidence,
        rationale="",
        ts=ts,
        payload=payload,
        evidence=[IntentEvidence(source="timeline_block", ref_id="blk-1", quote=task_text or text)],
    )


def _persist_all(rows: list[Intent]) -> list[Intent]:
    with fts.cursor() as conn:
        for it in rows:
            sink.persist_intent(conn, it)
        return intent_store.recent_intents(conn, start=_DAY_START, end=_DAY_END)


# --- the production failure: paraphrased assignment must fold to ONE row -----------


@needs_model
def test_paraphrased_assignment_folds(ac_root, semantic_on) -> None:  # noqa: ANN001
    """Real 140/141/143: one commitment recognized in 3 sessions, worded differently
    → the semantic fold collapses them to ONE row (the lexical fold never could)."""
    got = _persist_all(
        [
            _mk(
                kind="assignment",
                scope="session-6a9b7d8f4c0f",
                ts="2026-06-11T16:19",
                task_text=_T140,
            ),
            _mk(
                kind="assignment",
                scope="session-f7c2bb321f0c",
                ts="2026-06-11T16:40",
                task_text=_T141,
            ),
            _mk(
                kind="assignment",
                scope="session-a7a18f950696",
                ts="2026-06-11T16:51",
                task_text=_T143,
            ),
        ]
    )
    assert len(got) == 1


def test_paraphrases_are_lexically_distant(ac_root) -> None:  # noqa: ANN001
    """Companion proof that it is the EMBEDDING path doing the fold, not Jaccard: the
    three bodies' pairwise char-bigram Jaccard is all < the 0.72 lexical threshold."""
    norm = sink._content_body  # NFKC+casefold+strip-whitespace, the fold's body
    bodies = [norm({"task_text": t}) for t in (_T140, _T141, _T143)]
    for i in range(len(bodies)):
        for j in range(i + 1, len(bodies)):
            assert sink._body_similarity(bodies[i], bodies[j]) < 0.72


@needs_model
def test_reminder_paraphrase_folds(ac_root, semantic_on) -> None:  # noqa: ANN001
    """A reminder re-stated with different wording (real "整理 acme-context 为飞书 Wiki"
    family, cosine ~0.88) folds on meaning, not surface wording."""
    got = _persist_all(
        [
            _mk(
                kind="reminder",
                scope="s1",
                ts="2026-06-11T14:00",
                text="将 acme-context 整理成飞书 Wiki",
            ),
            _mk(
                kind="reminder",
                scope="s2",
                ts="2026-06-11T14:30",
                text="把 acme-context 这个包按 wiki 结构整理上传到飞书",
            ),
        ]
    )
    assert len(got) == 1


# --- guards: must NOT over-fold ----------------------------------------------------


@needs_model
def test_distinct_same_topic_assignments_stay_separate(ac_root, semantic_on) -> None:  # noqa: ANN001
    """Two GENUINELY different bug-fix tasks (cosine ~0.6–0.7, below 0.82) must NOT
    fold — the threshold sits above the same-topic band."""
    got = _persist_all(
        [
            _mk(
                kind="assignment",
                scope="s1",
                ts="2026-06-11T16:00",
                task_text="修复登录页面崩溃的 bug",
            ),
            _mk(
                kind="assignment",
                scope="s2",
                ts="2026-06-11T16:10",
                task_text="修复用户头像上传失败的 bug",
            ),
        ]
    )
    assert len(got) == 2


@needs_model
def test_semantic_fold_respects_people_guard(ac_root) -> None:  # noqa: ANN001
    """The people-compat guard applies to the semantic match too: same paraphrased
    body but two disjoint non-empty `with` sets = two different facts → stay 2."""
    with fts.cursor() as conn:
        a = _mk(
            kind="assignment", scope="s1", ts="2026-06-11T16:00", task_text=_T140, people=["张三"]
        )
        sink.persist_intent(conn, a)
        b = _mk(
            kind="assignment", scope="s2", ts="2026-06-11T16:10", task_text=_T141, people=["李四"]
        )
        # Search BEFORE persisting b (else b would find itself).
        tgt = sink._find_content_fold_target(conn, b, semantic_threshold=0.82)
        sink.persist_intent(conn, b)
        rows = [
            r
            for r in intent_store.recent_intents(conn, start=_DAY_START, end=_DAY_END)
            if r.kind == "assignment"
        ]
    assert tgt is None  # disjoint people block the fold even on a high-cosine body
    assert len(rows) == 2


def test_semantic_fold_killswitch(ac_root) -> None:  # noqa: ANN001
    """semantic_threshold=1.0 (off) ⇒ the paraphrases do NOT fold via embeddings —
    byte-identical to pre-semantic behaviour. (No model needed: the path is gated off
    before any embedding call.)"""
    with fts.cursor() as conn:
        a = _mk(kind="assignment", scope="s1", ts="2026-06-11T16:00", task_text=_T140)
        b = _mk(kind="assignment", scope="s2", ts="2026-06-11T16:10", task_text=_T141)
        sink.persist_intent(conn, a)
        # Direct call with the kill-switch: no semantic second chance, lexical < 0.72.
        tgt = sink._find_content_fold_target(
            conn, b, similarity_threshold=0.72, semantic_threshold=1.0
        )
    assert tgt is None

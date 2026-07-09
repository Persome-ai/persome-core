"""The single write-back entry point for recognized intents.

Every recognizer (passive timeline today; meeting/chat packs later) persists
through ``persist_intent`` — never by writing its own bespoke store. This closes
the "病1 不写回主记忆" gap uniformly: one call lands the intent in both

1. the structured ``intents`` table-of-record (for programmatic consumers like
   the active layer), and
2. a compact projection into the ``entries`` FTS index under ``intent-*.md`` so
   it is immediately retrievable by ``search_memory`` / chat / the MCP server —
   exactly like the reducer keeps ``timeline_blocks`` alongside ``event-*.md``.

Idempotent: a duplicate intent (same scope|kind|when|with) is skipped — unless
the caller opts into material-change updates (R3, see
:func:`persist_intent_result` + :func:`material_change`), in which case a
re-recognition that *materially* improves on the stored row UPDATEs it in place
(id + status preserved) instead of being silently dropped.

写权反转（PR-6b，SSOT 切换设计 §1.3/§5）：``write_authority="evomem"`` 时本站点
的 FTS 投影写（``intent-*.md``，append-only 永不入链 = 确定性 ADD）经
``store/entries.py`` 的 choke-point dispatch 走 evomem engine 落 evo_nodes
（L7_INTENTION），markdown 由投影器再生成；``intents`` table-of-record 照旧不动。
逐站输出等价由 ``tests/test_evomem/test_inversion_stations.py`` 钉死。
"""

from __future__ import annotations

import re
import sqlite3
import threading
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta

from .. import config as config_mod
from .. import events as events_mod
from ..logger import get
from ..store import cooldown_suppressions, intent_fold_ticks
from ..store import entries as entries_mod
from ..store import files as files_mod
from . import cooldown, embeddings
from . import normalize as normalize_mod
from . import store as intent_store
from .ontology import Intent

logger = get("persome.intent.sink")

# TOCTOU guard (#625): the dedup decision is a SELECT (find_live_duplicate /
# cross-form / the three fold lookups) FOLLOWED BY an INSERT, with the
# autocommit connection issuing each statement independently — nothing wraps the
# check and the insert into one atomic step. The fast path (capture pool thread)
# and the slow path (timeline task thread) each open their OWN ``fts.cursor()``
# connection, so when both recognize the SAME commitment their dedup SELECTs can
# both miss before either INSERTs → twin rows (issue's exact window; currently
# latent because fast-K1 is rarely concurrent, will bite once #610 activates the
# fast path against the 60s slow tick).
#
# The two recognizers run in ONE daemon process on DIFFERENT threads, so the
# correct, complete serialization is an in-process lock that makes the
# resolve-duplicate-then-insert critical section mutually exclusive — the loser
# re-runs its dedup AFTER the winner committed and folds instead of inserting a
# twin. A ``threading.RLock`` (reentrant) is used so a nested persist on the SAME
# thread — e.g. a recognizer that persists a derived intent while already inside
# one — never self-deadlocks. NOT a UNIQUE constraint: #525 deliberately lets a
# recurring commitment carry the SAME dedup_key across occurrences (multiple
# legitimate rows), which a UNIQUE index would forbid. The fix serializes the
# decision; it does not forbid same-key rows.
_PERSIST_LOCK = threading.RLock()

# Material-change threshold: a re-recognition must raise confidence by at least
# this much over the STORED row to count as material. Because the update writes
# the new confidence back (a ratchet), this can fire at most ~⌈1/Δ⌉ times per
# intent — bounded republish by construction.
MATERIAL_CONFIDENCE_DELTA = 0.15

# Only rows still awaiting the user may be updated. consumed/dismissed is final
# user feedback: a higher-confidence re-recognition must NEVER resurrect a
# dismissed intent (re-surfacing something the user swatted away is the exact
# compounding-cost failure the asymmetric-cost constitution forbids).
_UPDATABLE_STATUSES = ("open", "armed")

# --- intent hardening (#550): confidence calibration + payload soft validation.
# Both run at THIS single write entrance so every producer (fast K1 / slow
# trajectory / meeting pack) gets identical treatment. They mutate ONLY
# ``intent.confidence`` — payload is never touched, so ``dedup_key`` (which
# hashes the payload for content-only intents) is never perturbed.

# confidence=1.0 is reserved for verbatim user-committed promises. Anything the
# model merely inferred (counterpart_proposed, hints, missing provenance) can
# never claim full certainty — cap it here regardless of which prompt produced it.
CONFIDENCE_CAP_INFERRED = 0.9

# Soft payload validation for the seed kinds: the fields downstream consumers
# (active proposals / folding / display) expect per kind. A missing field NEVER
# rejects the intent (漏报代价有限，照单全收) — it down-weights confidence and
# emits a structured log line so the quality gap stays visible.
PAYLOAD_MISSING_FIELD_PENALTY = 0.8
_EXPECTED_PAYLOAD_FIELDS: dict[str, tuple[str, ...]] = {
    "meeting": ("when_text",),
    "calendar": ("when_text",),
    "reminder": ("text",),
    # WorkThread S0: an assignment without the task text is uncitable downstream
    # (the tracker quotes it verbatim) — keep it, but down-weight.
    "assignment": ("task_text",),
}


def _cooldown_exempt(intent: Intent) -> bool:
    """Is this intent EXEMPT from the kind-level hard cooldown (#533)?

    The hard cooldown is a precision闸 on the model's mid/low-confidence GUESSES
    at a kind the user keeps rejecting. It must never swallow a zero-entropy fact
    the user stated verbatim (宪法 §5 零熵猎场不该被否决) — that would contradict
    the same PR's ``user_committed`` confidence-cap exemption. Exempt when either:

    - ``payload.provenance == "user_committed"`` — a verbatim promise, OR
    - ``confidence >= CONFIDENCE_CAP_INFERRED`` (0.9) — only a user_committed
      intent survives :func:`_clamp_confidence` this high, so this is the same
      "the user said it" signal read off the calibrated confidence (this helper
      is called AFTER the clamp).

    A bypassed intent still flows through the normal dedup/fold path — the闸 may
    downstream-deprioritize it, but it is never silently dropped here.
    """
    if str(intent.payload.get("provenance") or "") == "user_committed":
        return True
    return intent.confidence >= CONFIDENCE_CAP_INFERRED


# Per-kind zero-nag confidence ceilings: the prompt assigns these kinds an explicit
# ≤0.4 ceiling (low-cost memory/recall signals, never a proactive surface). Enforced
# deterministically (the model honours it only for some kinds — info_need violates it
# 97% of the time). UNCONDITIONAL by kind (applies even to user_committed: these kinds
# are signals, not high-confidence surfaces), so it composes with the provenance caps.
_KIND_CONFIDENCE_CEILING: dict[str, float] = {
    "info_need": 0.4,
    "meeting_hint": 0.4,
    "backlog": 0.4,
}


def _clamp_confidence(intent: Intent, cfg: config_mod.Config | None = None) -> None:
    """Deterministically cap confidence at this single sink entrance.

    The effective cap is the MIN of three composing tiers (the most specific wins):

    1. **Per-kind zero-nag ceiling** (``_KIND_CONFIDENCE_CEILING``, gated by
       ``enforce_kind_confidence_ceilings``): info_need / meeting_hint / backlog →
       0.4, the prompt's stated ceiling. Applies UNCONDITIONALLY (even to a
       ``user_committed`` info_need — these kinds are low-cost memory signals, never
       a high-confidence proactive surface).
    2. **Generic inferred cap** (``CONFIDENCE_CAP_INFERRED`` = 0.9): any
       non-``user_committed`` intent (mere inference can't claim full certainty).
       ``user_committed`` is exempt from THIS tier only.
    3. **Provenance cap** for ``counterpart_proposed`` →
       ``counterpart_confidence_cap`` (default 0.6, below the 0.7 surface bar): the
       prompt says counterpart proposals are ≤0.6, but the model honours it ~23% of
       the time; enforcing it stops the production over-fire (~10% accept).

    A capped intent still persists unchanged otherwise (payload untouched, so
    ``dedup_key`` is never perturbed) — only its surfacing eligibility changes.
    """
    provenance = str(intent.payload.get("provenance") or "")
    cap = 1.0
    # Tier 1 — per-kind zero-nag ceiling (applies regardless of provenance).
    if cfg is None or cfg.intent_recognizer.enforce_kind_confidence_ceilings:
        cap = min(cap, _KIND_CONFIDENCE_CEILING.get(intent.kind, 1.0))
    # Tiers 2 & 3 — generic inferred + counterpart caps (user_committed exempt).
    if provenance != "user_committed":
        cap = min(cap, CONFIDENCE_CAP_INFERRED)
        if provenance == "counterpart_proposed":
            counterpart_cap = (
                cfg.intent_recognizer.counterpart_confidence_cap if cfg is not None else 0.6
            )
            cap = min(cap, counterpart_cap)
    if intent.confidence > cap:
        logger.info(
            "intent confidence clamped: kind=%s scope=%s provenance=%s %.2f -> %.2f",
            intent.kind,
            intent.scope,
            provenance or "(unset)",
            intent.confidence,
            cap,
        )
        intent.confidence = cap


# Load-bearing fields: a kind is NOT ACTIONABLE without these, so an intent missing
# one is not a usable proactive surface (a reminder with no WHAT, a meeting/calendar
# with no WHEN) — cap it BELOW the 0.7 surface bar rather than merely down-weighting.
# assignment is DELIBERATELY ABSENT — production shows missing task_text is fine
# (assigned_by/channel carry it; 9/16 accepted assignments lack it), so it stays soft.
_ACTIONABILITY_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "reminder": ("text",),
    "meeting": ("when_text",),
    "calendar": ("when_text",),
}
_INCOMPLETE_ACTIONABLE_CAP = 0.4  # zero-nag level, below the app sentinel's 0.7 bar


def _soft_validate_payload(intent: Intent, cfg: config_mod.Config | None = None) -> None:
    """Validate seed-kind payloads. Never rejects — only adjusts confidence.

    - A LOAD-BEARING field missing (reminder.text / meeting.when_text /
      calendar.when_text) → the intent is not actionable; cap below the surface bar
      (``_INCOMPLETE_ACTIONABLE_CAP``), gated by ``suppress_incomplete_actionable_intents``.
    - Any other expected field missing → soft down-weight ×``PAYLOAD_MISSING_FIELD_PENALTY``
      (assignment.task_text, or any load-bearing case when the kill-switch is off).
    """
    expected = _EXPECTED_PAYLOAD_FIELDS.get(intent.kind)
    if not expected:
        return
    missing = [f for f in expected if intent.payload.get(f) in (None, "", [])]
    if not missing:
        return
    before = intent.confidence
    hard = _ACTIONABILITY_REQUIRED_FIELDS.get(intent.kind)
    enforce_hard = (
        hard is not None
        and any(f in missing for f in hard)
        and (cfg is None or cfg.intent_recognizer.suppress_incomplete_actionable_intents)
    )
    if enforce_hard:
        intent.confidence = min(before, _INCOMPLETE_ACTIONABLE_CAP)
    else:
        intent.confidence = round(before * PAYLOAD_MISSING_FIELD_PENALTY, 4)
    logger.info(
        "intent payload validation: kind=%s scope=%s missing=%s confidence %.2f -> %.2f "
        "(%s, kept, never rejected)",
        intent.kind,
        intent.scope,
        missing,
        before,
        intent.confidence,
        "actionability-capped" if enforce_hard else "soft-downweighted",
    )


@dataclass(frozen=True)
class PersistResult:
    """Outcome of one persist call.

    ``outcome`` is one of:

    - ``"inserted"`` — a new row was created (``row_id`` set).
    - ``"updated"``  — dedup hit + material change → the existing row was
      updated in place (``row_id`` is the EXISTING row's id).
    - ``"skipped"``  — dedup hit without material change (or invalid intent);
      ``row_id`` is ``None``.
    """

    row_id: int | None
    outcome: str


def material_change(old: Intent, new: Intent) -> bool:
    """Deterministic test: does the re-recognition *materially* improve on the
    stored row? (R3 material-change-republish.)

    克制优先 — 宁可漏 republish，不可重复打扰（重复弹 HUD 是复利损失）。Material
    means strictly one of:

    1. ``new.confidence - old.confidence >= MATERIAL_CONFIDENCE_DELTA`` — the
       update ratchets the stored confidence upward, so per intent this fires a
       bounded number of times, and a wobbling-DOWN re-recognition never
       triggers anything (no downgrade-update, no republish).
    2. provenance upgraded ``counterpart_proposed → user_committed`` — the user
       has since accepted; downstream proposal wording flips ("要不要回复" →
       "加日历"), so the stale row would actively mislead consumers. Fires at
       most once (after the update the row IS ``user_committed``).

    Deliberately NOT material:

    - ``old.status`` consumed/dismissed — final user feedback, never reopened.
    - ``when_text`` 模糊→具体 ("周五" → "周五15:00"): the normalized
      ``when_text`` participates in :func:`store.dedup_key`, so a specificity
      change produces a *different* key — it inserts a new row and never reaches
      this dedup-hit comparison. Surface variants that DO fold onto the same key
      ("周五下午3点" vs "星期五15:00") are semantically identical by
      construction → non-material.
    """
    if old.status not in _UPDATABLE_STATUSES:
        return False
    if new.confidence - old.confidence >= MATERIAL_CONFIDENCE_DELTA:
        return True
    return (
        str(old.payload.get("provenance") or "") == "counterpart_proposed"
        and str(new.payload.get("provenance") or "") == "user_committed"
    )


def _projection_file() -> str:
    return f"intent-{files_mod.today()}.md"


# --- semantic fact folding (#546 面1) ------------------------------------------
#
# The exact ``dedup_key`` only folds SURFACE variants of the same ``when_text``.
# Real-world drift ("22:00" / "今天22:00" / "6月11日 (今天) 22:00", or kind
# wobbling between calendar↔meeting, or `with` lists overlapping but unequal)
# produces distinct keys for the SAME underlying fact — 2026-06-11 实测 13 条
# open intents 实际只有 4 个事实。This layer folds those onto the canonical row
# by the DETERMINISTIC temporal grounding instead of the LLM's free text.
# Cross-scope folding is the core value here (fast-K1 first, the session slow
# path seconds later → one row).

# Kinds that fold together: calendar↔meeting drift is the same commitment;
# reminders only fold with reminders. Other kinds (info_need, …) never fold
# semantically — they have no reliable temporal grounding.
_FOLD_KIND_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"meeting", "calendar"}),
    frozenset({"reminder"}),
)
# Candidate rows must have been recognized within this window of the new
# intent's ts — re-recognitions of one fact cluster within hours, while "every
# Wednesday 15:00" standups a week apart must stay separate rows.
_FOLD_WINDOW = timedelta(hours=48)
# Two resolved_at within this bucket are the same moment ("22:00" vs a slightly
# different parse of the same commitment).
_FOLD_BUCKET = timedelta(minutes=30)


# Ungrounded commitment folding (#619 B1/B3) -----------------------------------
#
# The grounded fold above only fires when BOTH rows resolved a ``when_text`` into
# a ``resolved_at`` — but ~94% of real ``open`` rows never resolve one (the LLM
# emitted "这周"/"等忙完这阵"/no when_text at all), so for them the fold path was
# dead and the SAME commitment fanned into 2-4 rows (B1 cross-kind: a {meeting,
# calendar} pair on the same vague 「这周」; B3 key-shape flip: one recognition
# carries when_text, the twin drops it → temporal vs content key, never folds).
# This layer generalizes the proven ``_find_hint_fold_target`` strategy (the
# issue's checklist: "把无锚折叠统一扩展到 ungrounded 的 meeting/calendar/
# info_need/assignment/reminder") to every commitment kind: fold on the SAME
# normalized content identity within the recognition window, when the grounded
# layer could not. Mis-fold cost = drop one duplicate candidate (bounded);
# not-folding cost = the same commitment repeats across recall (compounding) —
# the asymmetric-cost constitution says fold.
#
# The fold groups for the ungrounded path mirror the grounded ``_FOLD_KIND_GROUPS``
# ({meeting, calendar} are the same commitment even when neither resolved a clock
# — that is B1's exact case) plus the content-only kinds as their own groups.
# Without a resolved instant the identity is the group + normalized text body +
# compatible people, so a cross-kind {meeting, calendar} match still needs a
# matching body or overlapping people — never folds on the group membership alone.
_CONTENT_FOLD_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"meeting", "calendar"}),
    frozenset({"reminder"}),
    frozenset({"info_need"}),
    frozenset({"assignment"}),
)
# The payload fields that carry the fact's text body, in priority order — the
# first non-empty one is the identity string folded on.
_CONTENT_TEXT_KEYS: tuple[str, ...] = ("text", "task_text", "title", "topic")


def _norm_people(payload: dict) -> set[str]:
    """Casefolded NFKC entity set from ``payload.with`` for overlap matching.

    ALL whitespace is collapsed (not just the ends), mirroring
    :func:`_content_body`: the LLM re-emits the same counterpart as ``"Dev群"``
    one recognition and ``"Dev 群"`` the next, and an internal-space mismatch
    blocked the people-overlap fold → the SAME commitment fanned into a fresh
    OPEN row every session (production meeting 133/137: 「明天晚上 / Dev群 /
    Onboarding 一期 PRD」 re-pushed N times). ``with`` is volatile surface
    wording, not identity — normalize it like every other fold body.
    """
    return {
        norm
        for p in (payload.get("with") or [])
        if (norm := re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(p)).casefold()))
    }


def _content_body(payload: dict) -> str:
    """NFKC+casefold-normalized fact-body string for ungrounded fold matching.

    The first non-empty of ``text``/``task_text``/``title``/``topic`` (the kind's
    primary content field), whitespace-stripped — mirrors
    :func:`intent.store.normalize_content_payload` so two paraphrase-stable
    re-statements of the same hint compare equal (#619 B2/B3)."""
    for k in _CONTENT_TEXT_KEYS:
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return re.sub(r"\s+", "", unicodedata.normalize("NFKC", v).casefold())
    return ""


# Fuzzy content-fold similarity (重复推送相同语义修复) ----------------------------
#
# The exact-equality content fold misses the COMMON real case — the SAME to-do
# re-recognized with drifted wording every session, which the deterministic key
# can never collapse. 生产实测：同一件「为 PR #102 加 GitHub Actions secret/labels」
# 被记成 6 条 open reminder（id 103/104/105/112/113/115），措辞各异。A deterministic
# char-bigram (shingle) Jaccard over the already-NFKC+casefold-normalized bodies
# folds those near-identical restatements while keeping genuinely-distinct
# same-topic facts apart — zero LLM, stays inside the deterministic fold path so
# the daemon's golden-set gate still governs it. 误折的代价 = 丢一条重复候选（有限）；
# 不折的代价 = 同一意图反复弹窗（复利）—— 按不对称损失偏向折叠，阈值取保守值并以
# golden 负例钉死下界。Identical bodies → 1.0, so the exact path is just the sim==1 case.
_CONTENT_FOLD_MIN_LEN = (
    6  # below this BOTH bodies stay exact-only (short strings over-fold under shingles)
)


def _char_bigrams(s: str) -> set[str]:
    """Character 2-shingles of ``s`` (the whole string when it is a single char)."""
    return {s[i : i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else {s}


def _body_similarity(a: str, b: str) -> float:
    """Deterministic char-bigram Jaccard over two normalized fact bodies (0..1).

    Identical strings → 1.0. Language-agnostic (works for the mixed CN/EN intent
    bodies) and dependency-free — deliberately NOT jieba, so the fold path stays
    light. Callers guard non-empty inputs; an empty shingle set yields 0.0.
    """
    if a == b:
        return 1.0
    ga, gb = _char_bigrams(a), _char_bigrams(b)
    if not ga or not gb:
        return 0.0
    union = len(ga | gb)
    return len(ga & gb) / union if union else 0.0


def _find_fold_target(conn: sqlite3.Connection, intent: Intent) -> Intent | None:
    """The canonical row ``intent`` is a semantic re-statement of, or ``None``.

    Match = ALL of: kind in the same fold group · candidate recognized within
    48h · ``resolved_at`` within ±30min · ``with`` entities compatible. People
    compatibility (2026-06-12 生产实测放宽): only two NON-EMPTY ``with`` sets
    that are disjoint block the fold — that genuinely reads as two different
    meetings at the same hour. One side empty = compatible: the same 22:00
    commitment was recognized once with the counterpart attached ("22:00 跟
    Vanessa") and once without (识别抖动丢了 with)，blocking on that asymmetry
    left 4 rows for 1 fact. 误折的代价 = 丢一条重复候选（有限）；不折的代价 =
    同一承诺多行重复呈现（复利）——按不对称损失放行。Event-based intents
    (``fire_on``) never fold semantically: their identity is the trigger, not
    the clock.
    """
    if not intent.resolved_at or intent.fire_on:
        return None
    group = next((g for g in _FOLD_KIND_GROUPS if intent.kind in g), None)
    if group is None:
        return None
    try:
        anchor = datetime.fromisoformat(intent.ts)
        new_resolved = datetime.fromisoformat(intent.resolved_at)
    except ValueError:
        return None
    # A naive side is assumed local (the daemon's — i.e. the user's — current tz)
    # before comparing instants, exactly like store._abs_delta_within (#586). A
    # fast-path intent carries an offset-aware resolved_at (anchored at the capture
    # ts) while a slow/sink-fallback one can be naive; without normalising, the old
    # `except TypeError: continue` silently skipped the fold and let a duplicate of
    # the SAME occurrence surface.
    if new_resolved.tzinfo is None:
        new_resolved = new_resolved.astimezone()
    since = (anchor - _FOLD_WINDOW).isoformat(timespec="minutes")
    new_people = _norm_people(intent.payload)
    for cand in intent_store.fold_candidates(conn, kinds=tuple(group), since=since):
        if cand.fire_on or not cand.resolved_at:
            continue
        try:
            cand_resolved = datetime.fromisoformat(cand.resolved_at)
        except ValueError:
            continue
        if cand_resolved.tzinfo is None:
            cand_resolved = cand_resolved.astimezone()
        if abs(cand_resolved - new_resolved) > _FOLD_BUCKET:
            continue
        cand_people = _norm_people(cand.payload)
        if new_people and cand_people and not (new_people & cand_people):
            continue
        return cand
    return None


# --- meeting_hint folding (2026-06-12 生产实测：同一"下次周会"hint 连发 4 行) ----
#
# Hints have no reliable temporal grounding, so they are deliberately OUT of
# the resolved_at-bucket fold above. Their identity proxy is conservative:
# the SAME verbatim-normalized euphemism phrase ("下次周会") with overlapping
# counterparts, or — when both sides carry no phrase at all — the same scope
# (the same chat session re-recognizing the same anchorless willingness).
# 错折丢的是一条 confidence≤0.4 的提示信号（有限）；不折则同一意愿在 recall
# 场景层复读（复利）。


def _find_hint_fold_target(conn: sqlite3.Connection, intent: Intent) -> Intent | None:
    if intent.kind != "meeting_hint" or intent.fire_on:
        return None
    try:
        anchor = datetime.fromisoformat(intent.ts)
    except ValueError:
        return None
    since = (anchor - _FOLD_WINDOW).isoformat(timespec="minutes")
    new_people = _norm_people(intent.payload)
    if not new_people:
        # 无对象的 hint 没有任何身份信号可比，宁可留行也不瞎折。
        return None
    new_when = intent_store.normalize_when_text(str(intent.payload.get("when_text") or ""))
    for cand in intent_store.fold_candidates(
        conn, kinds=("meeting_hint",), since=since, require_grounding=False
    ):
        if cand.fire_on:
            continue
        if not (new_people & _norm_people(cand.payload)):
            continue
        cand_when = intent_store.normalize_when_text(str(cand.payload.get("when_text") or ""))
        same_phrase = bool(new_when) and new_when == cand_when
        both_blank_same_scope = not new_when and not cand_when and cand.scope == intent.scope
        if same_phrase or both_blank_same_scope:
            return cand
    return None


def _find_content_fold_target(
    conn: sqlite3.Connection,
    intent: Intent,
    *,
    similarity_threshold: float = 1.0,
    semantic_threshold: float = 1.0,
) -> Intent | None:
    """Ungrounded commitment fold (#619 B1/B3): the canonical row ``intent``
    re-states by CONTENT identity (not the clock), or ``None``.

    Runs ONLY when the grounded fold could not (``intent.resolved_at`` is None) —
    the 94% of real rows the LLM never resolved a ``when_text`` for. Match = ALL
    of: kind in the same ``_CONTENT_FOLD_GROUPS`` group · candidate recognized
    within the 48h window · still ungrounded (a grounded candidate is the
    grounded layer's job)
    · text body matching when both carry one — EXACT, or char-bigram Jaccard
    ≥ ``similarity_threshold`` once both bodies clear ``_CONTENT_FOLD_MIN_LEN``,
    or (on a Jaccard miss) sentence-embedding cosine ≥ ``semantic_threshold`` —
    the latter folds PARAPHRASES the lexical layer misses (生产实测: 同一交办换三种
    措辞记成 3 行, Jaccard 0.3 / cosine 0.88); distinct bodies stay apart —
    else compatible people (overlap, with one-side-empty allowed mirroring the
    grounded fold). When neither side carries a text body NOR any people there is
    no identity signal — fall back to same-scope (the same scene re-recognizing
    one anchorless hint), never cross-scope (would collapse unrelated rows).
    Event-based intents (``fire_on``) never fold here — their identity is the
    trigger. Both ``similarity_threshold`` and ``semantic_threshold`` default to
    1.0 (kill-switch) — exact-only, byte-identical to the pre-fuzzy behavior.
    """
    if intent.resolved_at or intent.fire_on:
        return None
    group = next((g for g in _CONTENT_FOLD_GROUPS if intent.kind in g), None)
    if group is None:
        return None
    try:
        anchor = datetime.fromisoformat(intent.ts)
    except ValueError:
        return None
    since = (anchor - _FOLD_WINDOW).isoformat(timespec="minutes")
    new_body = _content_body(intent.payload)
    new_people = _norm_people(intent.payload)
    for cand in intent_store.fold_candidates(
        conn, kinds=tuple(group), since=since, require_grounding=False
    ):
        if cand.fire_on or cand.resolved_at:
            continue
        cand_body = _content_body(cand.payload)
        cand_people = _norm_people(cand.payload)
        # Text body is the strongest identity signal: when BOTH carry one, they
        # must match — EXACTLY, or fuzzily (char-bigram Jaccard ≥ threshold) once
        # both bodies are long enough that shingle overlap is meaningful. A short
        # body stays exact-only (min-len guard) so two short distinct to-dos that
        # happen to share characters never over-fold.
        if new_body and cand_body:
            both_long = (
                len(new_body) >= _CONTENT_FOLD_MIN_LEN and len(cand_body) >= _CONTENT_FOLD_MIN_LEN
            )
            threshold = similarity_threshold if both_long else 1.0
            lexical_ok = _body_similarity(new_body, cand_body) >= threshold
            # Second chance on a lexical miss: a PARAPHRASE of the same commitment
            # ("修复Mens bug：把动画去掉…" vs "去掉动画，修复…导航bug") scores ~0.3 on
            # char-bigram overlap yet ~0.88 on sentence-embedding cosine, so the SAME
            # fact otherwise fans into N open rows across sessions. Only when both
            # bodies are long enough for the encoder to mean something, behind the
            # ``semantic_threshold`` kill-switch (1.0 = off). Fail-open: no model →
            # ``embeddings.cosine`` returns 0.0 and this never fires.
            semantic_ok = (
                not lexical_ok
                and both_long
                and semantic_threshold < 1.0
                and embeddings.cosine(embeddings.embed(new_body), embeddings.embed(cand_body))
                >= semantic_threshold
            )
            if not lexical_ok and not semantic_ok:
                continue
            # Same / near-same body — people only BLOCK on two disjoint non-empty
            # sets (the same asymmetric rule as the grounded fold: one side empty
            # = recognition drift, not a different fact). Applies to BOTH the
            # lexical and the semantic match.
            if new_people and cand_people and not (new_people & cand_people):
                continue
            return cand
        # One side has no text body: fold on people overlap alone (识别抖动 dropped
        # the body on one row). Requires a people signal on the side that has one.
        if new_people and cand_people:
            if new_people & cand_people:
                return cand
            continue
        # Neither body nor people on at least one side — only identity left is the
        # scene. Same scope = the same session re-stating one anchorless intent.
        if (
            not new_body
            and not cand_body
            and not new_people
            and not cand_people
            and cand.scope == intent.scope
        ):
            return cand
    return None


def persist_intent(conn: sqlite3.Connection, intent: Intent) -> int | None:
    """Persist one intent. Returns the structured row id, or ``None`` if skipped.

    Legacy surface-once contract (unchanged): a dedup hit always returns
    ``None`` — callers that want material-change updates use
    :func:`persist_intent_result` with ``allow_material_update=True``.
    """
    res = persist_intent_result(conn, intent)
    return res.row_id if res.outcome == "inserted" else None


def _record_fold_telemetry(
    conn: sqlite3.Connection,
    intent: Intent,
    fold_target: Intent | None,
    *,
    outcome: str,
    cfg: config_mod.Config,
) -> None:
    """G5.1: record one content-free ``intent_fold_ticks`` row for a fold/dedup hit.
    Gated + fully best-effort (the DAO swallows its own errors too) so it can NEVER
    perturb the dedup decision — the canonical write already returned."""
    if not cfg.intent_recognizer.intent_fold_telemetry_enabled:
        return
    target_id = fold_target.id if fold_target is not None else None
    intent_fold_ticks.record_fold(
        conn, scope=intent.scope, kind=intent.kind, target_id=target_id, outcome=outcome
    )


def persist_intent_result(
    conn: sqlite3.Connection, intent: Intent, *, allow_material_update: bool = False
) -> PersistResult:
    """Persist one intent, reporting what happened (inserted/updated/skipped).

    With ``allow_material_update=False`` (default) this is behavior-identical to
    the historical ``persist_intent``: dedup hits are skipped outright. With
    ``True``, a dedup hit is compared against the stored row via
    :func:`material_change`; a material re-recognition UPDATEs the row in place
    (id + status preserved, ``dedup_key`` migrated to the canonical key) so the
    caller can republish it as ``updated`` rather than dropping it silently.

    Failures in the FTS projection are logged but do not fail the structured
    write — the table-of-record is the source of truth.
    """
    if not intent.kind or not intent.scope:
        logger.debug("intent skipped: missing kind/scope (%r)", intent)
        return PersistResult(None, "skipped")

    # Load config ONCE for this persist (the cooldown gate needs it). Passed down
    # so the sink's hot write path doesn't re-read + re-parse config.toml per
    # intent (#533 perf): :func:`cooldown.suppression_for` reuses this cfg instead
    # of calling ``config.load()`` itself.
    cfg = config_mod.load()

    # Intent hardening (#550): calibrate confidence then soft-validate payload —
    # in that order, so an inferred intent missing a field gets cap × penalty.
    # Mutates confidence only; never rejects. Run BEFORE the cooldown gate so the
    # high-confidence bypass below reads the CALIBRATED confidence (an inferred
    # intent is capped at CONFIDENCE_CAP_INFERRED first; only a verbatim
    # user_committed promise keeps >0.9).
    _clamp_confidence(intent, cfg)
    _soft_validate_payload(intent, cfg)

    # (kind, scope)-level closed-set hard cooldown (#533): when the user has
    # dismissed this KIND IN THIS SCOPE enough times in the recent window, the
    # (kind, scope) is in a hard cooldown and its intents are dropped HERE — at the
    # single write entrance every producer (fast K1 / slow trajectory / meeting
    # pack) funnels through — so the gate bypasses the prompt entirely. Before
    # #533 the negative-feedback loop was prompt-soft only (the model was *asked*
    # not to re-surface dismissed kinds; the only hard block was an exactly-equal
    # dedup_key), so a re-worded re-statement of a rejected kind slipped through on
    # a fresh key. Time-bounded by construction (always expires — see
    # :mod:`intent.cooldown`): 弹错一个被反复拒绝的 kind = 复利损失，漏一个 = 有限
    # 损失，so suppressing is net-positive. Config-gated kill-switch + best-effort
    # (a lookup failure fails open).
    #
    # Confidence/provenance bypass (宪法 §5 零熵猎场不该被否决): a verbatim
    # ``user_committed`` promise — or any intent whose CALIBRATED confidence is
    # still ≥ CONFIDENCE_CAP_INFERRED (only user_committed survives the clamp that
    # high) — is EXEMPT. The hard闸 exists to suppress the model's mid/low
    # confidence GUESSES at a kind the user keeps rejecting; it must never swallow
    # a thing the user said in so many words, which would contradict the same PR's
    # user_committed confidence-cap exemption. A bypassed intent is logged so the
    # bypass stays visible.
    if not _cooldown_exempt(intent):
        until = cooldown.suppression_for(conn, intent.kind, scope=intent.scope, cfg=cfg)
        if until is not None:
            logger.info(
                "intent suppressed (kind in hard cooldown): kind=%s scope=%s conf=%.2f until=%s",
                intent.kind,
                intent.scope,
                intent.confidence,
                until.isoformat(timespec="minutes"),
            )
            # 拒绝是金矿 (#534 再校准的训练集): the suppressed intent never reaches
            # the ``intents`` table, so record a structured, additive trace
            # (presentation gated, observability never) — read by /intents/stats.
            cooldown_suppressions.record(
                conn,
                ts=intent.ts or datetime.now().isoformat(timespec="minutes"),
                kind=intent.kind,
                scope=intent.scope,
                confidence=intent.confidence,
                cooldown_until=until.isoformat(timespec="seconds"),
            )
            return PersistResult(None, "skipped")
    elif cfg.intent_recognizer.cooldown_enabled:
        logger.info(
            "intent BYPASSED cooldown (high-confidence/user_committed): "
            "kind=%s scope=%s conf=%.2f provenance=%s",
            intent.kind,
            intent.scope,
            intent.confidence,
            str(intent.payload.get("provenance") or "(unset)"),
        )

    if not intent.ts:
        intent.ts = datetime.now().isoformat(timespec="minutes")

    # Event-based prospective intent (L7): a fire_on means "wait for the event,
    # don't surface now" → store dormant as ``armed`` so the active layer (which
    # reads status=open) never proposes it before the activator fires it. An
    # already-set non-open status (e.g. a replay) is respected.
    if intent.fire_on and intent.status == "open":
        intent.status = "armed"

    # Deterministic temporal grounding (#546): resolve when_text anchored at ts
    # into resolved_at/valid_until. Best-effort — unparseable leaves both None
    # (no semantic folding, no expiry; pre-#546 behavior for that row).
    normalize_mod.stamp_temporal(intent)

    key = intent_store.dedup_key(intent)

    # TOCTOU guard (#625): resolve-duplicate-then-insert is ONE atomic critical
    # section. The dedup decision (find_live_duplicate / cross-form / the three
    # fold lookups) and the INSERT must not be interleaved by another producer —
    # the fast and slow recognizers run on different daemon threads, each on its
    # own autocommit connection, so without this lock both can dedup-miss then
    # both INSERT a twin. The lock is reentrant so a nested persist on the same
    # thread does not self-deadlock; the loser thread re-runs the resolution
    # AFTER the winner committed (the duplicate is now visible to its connection)
    # and folds instead of inserting.
    with _PERSIST_LOCK:
        terminal = _resolve_duplicate(
            conn, intent, key=key, allow_material_update=allow_material_update, cfg=cfg
        )
        if terminal is not None:
            return terminal

        row_id = intent_store.insert_intent(conn, intent)

        # FTS projection — best-effort; canonical write already succeeded.
        # Material updates above deliberately do NOT re-append a projection
        # entry: the markdown projection is an append-only recognition log, and
        # duplicating the same intent there would re-pollute exactly the search
        # surface dedup keeps clean.
        try:
            name = _projection_file()
            content = intent.to_text()
            tags = ["#intent", f"#kind:{intent.kind}", f"#scope:{intent.scope}"]
            try:
                entries_mod.append_entry(conn, name=name, content=content, tags=tags)
            except FileNotFoundError:
                entries_mod.create_file(
                    conn,
                    name=name,
                    description="Recognized intents (unified intent stream projection).",
                    tags=["intent"],
                )
                entries_mod.append_entry(conn, name=name, content=content, tags=tags)
        except Exception as exc:  # noqa: BLE001 — projection is best-effort
            logger.warning("intent FTS projection failed (row %s kept): %s", row_id, exc)

    # 识别即推 (#intent-auto-enqueue): a brand-new OPEN intent is published on the
    # SSE bus the instant it lands, so the app auto-enqueues it without waiting on
    # the 5min reconcile poll. ARMED inserts are deliberately NOT published here —
    # the activator's `event_fired` covers the armed→open surfacing moment, and
    # pushing an armed row would fire before its L7 时机门. UPDATED rows are skipped
    # too: the app keys dedup on intent id, so a material update of a row it
    # already enqueued is a no-op seen-id hit, and a row it has NOT seen only
    # arises via armed→open (covered by event_fired). Best-effort: a publish
    # failure never perturbs the canonical write.
    if intent.status == "open":
        _publish_persisted(row_id, intent)
    return PersistResult(row_id, "inserted")


def _publish_persisted(row_id: int, intent: Intent) -> None:
    """Publish one ``stage=intent type=persisted`` SSE event for a freshly-inserted
    OPEN intent. The payload is the intent's full ``to_dict()`` (same shape as the
    ``GET /intents`` item, so the app decodes it straight into its ``ChronicleIntent``)
    with ``id`` forced to the committed row id. Best-effort, never raises."""
    try:
        payload = intent.to_dict()
        payload["id"] = row_id
        events_mod.publish("intent", "persisted", payload)
    except Exception as exc:  # noqa: BLE001 — SSE publish is best-effort
        logger.debug("intent persisted-event publish failed (row %s): %s", row_id, exc)


def _resolve_duplicate(
    conn: sqlite3.Connection,
    intent: Intent,
    *,
    key: str,
    allow_material_update: bool,
    cfg: config_mod.Config | None = None,
) -> PersistResult | None:
    """Resolve whether ``intent`` duplicates an existing row, returning the
    terminal :class:`PersistResult` (``skipped`` / ``updated``) when it does, or
    ``None`` when it is genuinely new and the caller should INSERT.

    This is the SELECT half of the SELECT-then-INSERT critical section. It is
    deliberately a self-contained function so the sink can run it INSIDE the
    in-process persist lock (#625) immediately before the insert: a row committed
    by another producer between the recognizer's earlier reads and now becomes
    visible here, so the fold/skip decision is made against the freshest state
    and a twin is never inserted.

    ``cfg`` carries the content-fold fuzzy threshold to ``_find_content_fold_target``
    (None / fuzzy-disabled ⇒ exact-only, the pre-fuzzy behavior).
    """
    # Windowed, liveness-aware dedup (#525): the temporal dedup_key uses a
    # relative token ("周五" → {wk5}) with no anchored date, so a recurring
    # commitment repeats the SAME key every week. A whole-history existence check
    # let the first row suppress every later occurrence forever — systematic
    # silent misses concentrated in periodic promises. ``find_live_duplicate``
    # only treats a prior row as a duplicate when it is a LIVE duplicate of the
    # SAME occurrence (recent ts / coinciding resolved_at, not stale-expired).
    matched_key: str | None = (
        key if intent_store.find_live_duplicate(conn, key, intent) is not None else None
    )
    if matched_key is None:
        # Migration shim (#467): rows persisted before when_text normalization
        # carry the raw-text key — fold onto them too, never double-surface. New
        # rows below are always written with the canonical (normalized) key.
        legacy_key = intent_store.legacy_dedup_key(intent)
        if legacy_key and intent_store.find_live_duplicate(conn, legacy_key, intent) is not None:
            matched_key = legacy_key

    if matched_key is None:
        # Cross-form fold (#549 去重规则): the same fact stored under the OTHER
        # activation form (the trigger suffix makes armed/immediate keys differ).
        # Rule — 首存形式获胜，重识别永不翻转 status：
        # - stored=armed, incoming=immediate → skip. 不得把休眠意图翻回 open，也
        #   不得插一条立即弹出的孪生行（那会架空时机门）。
        # - stored=open/consumed/dismissed, incoming=event-based → skip. 事实已经
        #   呈现/处理过，再 arm 一份 = 安排第二次打扰；dismissed 永不复活。
        # 跨形式命中一律 skip、不走 material_change UPDATE：material update 会把
        # dedup_key 迁到 incoming 的 canonical key，从而改变存量行的激活形式
        # （armed 行丢触发后缀 / open 行凭空带上后缀），与被刻意保留的
        # status/fire_* 自相矛盾。
        # 与 #546 语义折叠互补且互斥：本检查专管 armed↔immediate 孪生形式，
        # 语义折叠在双侧都排除 fire_on 意图——没有同一 case 被两条规则处理。
        other = intent_store.same_fact_cross_form(conn, intent)
        if other is not None:
            logger.debug(
                "intent skipped (cross-form duplicate of row %s, status=%s)",
                other.id,
                other.status,
            )
            # G5.1: a cross-form fold IS a fold — the same fact re-recognized under
            # the OTHER activation form (#549 armed↔immediate) and dropped. Record it
            # so `fold_heat` counts re-recognition frequency consistently across ALL
            # three fold paths (exact-key / cross-form / semantic), matching the
            # intent_fold_ticks docstring; without this the cross-form kinds were
            # systematically undercounted in the content-fold tuning signal.
            _record_fold_telemetry(conn, intent, other, outcome="skipped", cfg=cfg)
            return PersistResult(None, "skipped")

    # Semantic fact folding (#546 面1): when no exact key matches, the SAME
    # underlying fact may still exist under a drifted surface (when_text
    # wording, calendar↔meeting kind wobble, partial `with`, different scope).
    # A fold hit is handled exactly like a dedup hit — material change UPDATEs
    # the existing row (id/status preserved), otherwise skip; never INSERT.
    fold_target: Intent | None = None
    if matched_key is None:
        # Fuzzy content-fold threshold: enabled ⇒ the config similarity, disabled
        # (kill-switch) or no cfg ⇒ 1.0 = exact-only (byte-identical to pre-fuzzy).
        content_threshold = 1.0
        if cfg is not None and cfg.intent_recognizer.content_fold_fuzzy_enabled:
            content_threshold = cfg.intent_recognizer.content_fold_similarity
        # Semantic (embedding cosine) second-chance threshold: catches paraphrases
        # the char-bigram layer misses. 1.0 = off (no cfg / kill-switch).
        semantic_threshold = 1.0
        if cfg is not None and cfg.intent_recognizer.semantic_fold_enabled:
            semantic_threshold = cfg.intent_recognizer.semantic_fold_similarity
        fold_target = (
            _find_fold_target(conn, intent)
            or _find_hint_fold_target(conn, intent)
            or _find_content_fold_target(
                conn,
                intent,
                similarity_threshold=content_threshold,
                semantic_threshold=semantic_threshold,
            )
        )
        if fold_target is not None:
            logger.info(
                "intent folded (semantic): row_id=%s outcome=fold "
                "new_scope=%s old_scope=%s new_kind=%s old_kind=%s "
                "new_when=%r old_when=%r resolved_at=%s",
                fold_target.id,
                intent.scope,
                fold_target.scope,
                intent.kind,
                fold_target.kind,
                str(intent.payload.get("when_text", "")),
                str(fold_target.payload.get("when_text", "")),
                intent.resolved_at,
            )

    if matched_key is not None or fold_target is not None:
        if allow_material_update:
            old = (
                fold_target
                if fold_target is not None
                else intent_store.get_by_dedup_key(conn, matched_key or "")
            )
            if old is not None and old.id is not None and material_change(old, intent):
                intent_store.update_intent_recognition(
                    conn, intent_id=old.id, intent=intent, canonical_key=key
                )
                logger.debug("intent updated (material change): row %s", old.id)
                # G5.1: a material re-recognition is still a fold (same fact, drifted).
                _record_fold_telemetry(conn, intent, fold_target, outcome="updated", cfg=cfg)
                return PersistResult(old.id, "updated")
        dup_label = (
            matched_key
            if matched_key is not None
            else f"fold:{fold_target.id if fold_target else None}"
        )
        logger.debug("intent skipped (duplicate): %s", dup_label)
        # G5.1: the same thing was recognized again and dropped — count it so the
        # re-recognition frequency (content-fold tuning signal) is measurable.
        _record_fold_telemetry(conn, intent, fold_target, outcome="skipped", cfg=cfg)
        return PersistResult(None, "skipped")

    return None

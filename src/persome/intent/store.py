"""DAO for the ``intents`` table — the unified intent stream's table-of-record.

This is the canonical structured store every recognizer writes through (via
:mod:`persome.intent.sink`) and every programmatic consumer reads from
(e.g. the active layer). A human/FTS-searchable projection is mirrored into the
``entries`` table separately (see ``sink.persist_intent``), mirroring how the
reducer keeps structured ``timeline_blocks`` alongside markdown ``event-*.md``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta

from ..logger import get
from .ontology import Intent

logger = get("persome.intent.store")

# Exact-key dedup window (#525). The temporal ``dedup_key`` deliberately uses a
# RELATIVE surface token ("周五" → ``{wk5}``, "明天" → ``{tomorrow}``) that carries
# NO anchored date, so a recurring commitment ("每周五15:00 和 Alice 开会")
# produces a byte-identical key every week. With a whole-history existence check
# the first row ever stored suppressed every later occurrence forever — a
# systematic, silent miss concentrated in periodic promises (the most valuable
# kind). The fix bounds the lookup: a prior row only suppresses a fresh
# recognition when it is still a LIVE duplicate of the SAME occurrence —
# - recognized within ``_DEDUP_WINDOW`` of the new intent's ``ts`` (covers one
#   trajectory split across the 5-min session gap / the timeline↔session
#   shadow-coexistence period, far short of a weekly recurrence), AND
# - not stale: ``expired`` rows, and ``open`` rows whose ``valid_until`` has
#   already passed, are the PREVIOUS occurrence finishing — they must not block
#   the next one. ``consumed``/``dismissed`` rows are this occurrence's final
#   user feedback and still suppress within the window (re-surfacing something
#   the user just acted on is the compounding-cost failure the constitution
#   forbids), but only within the window — next week is a new occurrence.
# When BOTH rows carry deterministic temporal grounding (``resolved_at``), the
# occurrence boundary is decided on the resolved instant (±``_DEDUP_GROUND_BUCKET``)
# instead of the recognition window, so "明天15:00" said on two different days
# (same key, different resolved date) never collides.
_DEDUP_WINDOW = timedelta(hours=36)
_DEDUP_GROUND_BUCKET = timedelta(hours=12)

SCHEMA = """
CREATE TABLE IF NOT EXISTS intents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,                 -- ISO8601 recognition time
    scope TEXT NOT NULL,              -- scene id: 'timeline' | <meeting-id> | ...
    kind TEXT NOT NULL,               -- open string; seed: meeting|calendar|reminder
    confidence REAL NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL DEFAULT 'open',  -- open | armed | consumed | dismissed | expired | resolved | completed | failed
    rationale TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}',   -- JSON: kind-specific fields
    evidence TEXT NOT NULL DEFAULT '[]',  -- JSON: provenance list
    dedup_key TEXT NOT NULL DEFAULT '',   -- scope|kind|when_text|with — for idempotent persist
    created_at TEXT NOT NULL,
    dismissed_at TEXT,                    -- ISO8601 when the row transitioned to status='dismissed' (NULL otherwise); the kind-cooldown clock (#533) anchors on this, NOT ts (recognition time)
    completed_at TEXT,                    -- ISO8601 when the row transitioned to status IN (completed,failed) (NULL otherwise); reverse-loop: the accept→completed positive prior (_completed_prior) anchors on this, NOT ts

    fire_on TEXT NOT NULL DEFAULT '',     -- event-based intent: trigger event key ('' == immediate)
    fire_config TEXT NOT NULL DEFAULT '{}',  -- JSON: trigger params (bundle_id/app/...)
    fired_at TEXT,                        -- ISO8601 when the trigger fired (NULL until then)
    schema_sources TEXT NOT NULL DEFAULT '[]',  -- JSON: schema-*.md files injected when recognized (R4 provenance)
    resolved_at TEXT,                     -- ISO8601 deterministic parse of payload.when_text anchored at ts (NULL = unparsed)
    valid_until TEXT,                     -- ISO8601 expiry: resolved_at(+end) + kind grace (NULL = never expires)
    source_capture TEXT,                  -- capture-buffer file stem this intent was recognized FROM (#7 provenance; NULL = none/legacy → time-window join fallback)
    resolution_outcome TEXT,              -- evidence-driven auto-close outcome (done|rejected|superseded; NULL unless status='resolved')
    resolution_quote TEXT                 -- ≤120-char supporting quote for the evidence-driven close (NULL unless status='resolved')
);
CREATE INDEX IF NOT EXISTS idx_intents_scope_ts ON intents(scope, ts DESC);
CREATE INDEX IF NOT EXISTS idx_intents_status_ts ON intents(status, ts DESC);
CREATE INDEX IF NOT EXISTS idx_intents_dedup ON intents(dedup_key);
"""

# The #7 provenance reverse-lookup index ("which intents came from this capture
# stem?") is created in :func:`_migrate`, NOT in SCHEMA: on an OLD DB the
# ``CREATE TABLE IF NOT EXISTS`` above no-ops, so an index referencing
# ``source_capture`` here would run before the ALTER that adds the column and
# raise "no such column". ``_migrate`` runs AFTER the column exists (fresh:
# created by SCHEMA's CREATE TABLE; old: added by the ALTER) and creates it
# idempotently for both paths.
_SOURCE_CAPTURE_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_intents_source_capture ON intents(source_capture)"
)

# The full column list every row-shaped SELECT uses, so a new column lands in
# all read paths at once (``_row_to_intent`` tolerates absent columns anyway).
_SELECT_COLS = (
    "id, ts, scope, kind, confidence, status, rationale, payload, evidence, "
    "fire_on, fire_config, fired_at, schema_sources, resolved_at, valid_until"
)


def _migrate(conn: sqlite3.Connection) -> None:
    """Backfill event-based-intent columns added after the initial schema."""
    # Positional column access (``PRAGMA table_info`` col 1 = name) so this works
    # regardless of the connection's ``row_factory`` — ``ensure_schema`` no longer
    # forces ``sqlite3.Row`` before calling here (#532).
    cols = {row[1] for row in conn.execute("PRAGMA table_info(intents)")}
    if "fire_on" not in cols:
        conn.execute("ALTER TABLE intents ADD COLUMN fire_on TEXT NOT NULL DEFAULT ''")
    if "fire_config" not in cols:
        conn.execute("ALTER TABLE intents ADD COLUMN fire_config TEXT NOT NULL DEFAULT '{}'")
    if "fired_at" not in cols:
        conn.execute("ALTER TABLE intents ADD COLUMN fired_at TEXT")
    if "schema_sources" not in cols:
        conn.execute("ALTER TABLE intents ADD COLUMN schema_sources TEXT NOT NULL DEFAULT '[]'")
    if "resolved_at" not in cols:
        conn.execute("ALTER TABLE intents ADD COLUMN resolved_at TEXT")
    if "valid_until" not in cols:
        conn.execute("ALTER TABLE intents ADD COLUMN valid_until TEXT")
    if "dismissed_at" not in cols:
        # #533: the kind-cooldown clock must anchor on WHEN the dismiss action
        # happened, not on ``ts`` (recognition time). The production dismiss path
        # (``update_intent_status``) only flips ``status`` and never touches
        # ``ts``, so a kind dismissed long after recognition (or a row recognized
        # long ago then dismissed now) would otherwise be timed off a wrong
        # instant. This separate column records the dismiss instant.
        conn.execute("ALTER TABLE intents ADD COLUMN dismissed_at TEXT")
    if "completed_at" not in cols:
        # Reverse-loop (spec 2026-06-26 G2/G3): the instant a `.context` task for
        # this intent finished executing (status → completed|failed). The
        # accept→completed positive prior anchors on THIS, not on ``ts``
        # (recognition time), for the same reason ``dismissed_at`` does — the
        # write-back happens long after recognition. Legacy rows carry NULL and
        # are simply not counted (fail-open).
        conn.execute("ALTER TABLE intents ADD COLUMN completed_at TEXT")
    if "source_capture" not in cols:
        # #7 provenance固化: the capture-buffer file stem this intent was
        # recognized FROM, so "intent → that screenshot" is a direct reverse
        # query instead of a fuzzy time-window join (and the retention scanner
        # can ask the inverse — "is this stem actionable?"). Backfilled NULL on
        # old DBs; consumers fall back to the time-window join for NULL rows.
        conn.execute("ALTER TABLE intents ADD COLUMN source_capture TEXT")
    if "resolution_outcome" not in cols:
        # Evidence-driven auto-close (`resolved` terminal status): when later
        # context shows an open intent is已做/已拒, the recognizer's resolution
        # channel flips it to ``status='resolved'`` (a status DELIBERATELY
        # distinct from the user-feedback ``consumed``/``dismissed`` so it never
        # feeds the kind-cooldown / R3 priors — those select on the literal
        # status). These two additive columns are the audit trail for WHY: the
        # outcome (done|rejected|superseded) and the ≤120-char supporting quote.
        conn.execute("ALTER TABLE intents ADD COLUMN resolution_outcome TEXT")
    if "resolution_quote" not in cols:
        conn.execute("ALTER TABLE intents ADD COLUMN resolution_quote TEXT")
    # Idempotent, and now safe for BOTH paths (fresh DB: column from SCHEMA's
    # CREATE TABLE; old DB: column just ALTERed in above) — see _SOURCE_CAPTURE_INDEX.
    conn.execute(_SOURCE_CAPTURE_INDEX)


# The columns the live ``intents`` schema must carry once SCHEMA + every
# :func:`_migrate` ALTER has run. Used as a cheap "already up to date?" probe so
# ``ensure_schema`` can short-circuit WITHOUT an ``executescript`` (which issues
# an implicit COMMIT) on the steady-state path.
_EXPECTED_COLS = frozenset(
    {
        "id", "ts", "scope", "kind", "confidence", "status", "rationale",
        "payload", "evidence", "dedup_key", "created_at", "fire_on",
        "fire_config", "fired_at", "schema_sources", "resolved_at", "valid_until",
        "dismissed_at", "completed_at", "source_capture",
        "resolution_outcome", "resolution_quote",
    }
)  # fmt: skip


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the ``intents`` schema + run column migrations, idempotently.

    ``ensure_schema`` is called at the top of nearly every read/write helper
    (``exists`` / ``get_by_dedup_key`` / ``recent_intents`` / …). The old body
    ran ``executescript(SCHEMA)`` unconditionally, which issues an **implicit
    COMMIT** — a footgun on a shared/long-lived connection: a read helper would
    silently commit a caller's open write transaction out from under them (#532).

    The fix: a cheap read-only probe (``PRAGMA table_info``) first; when the table
    already carries every expected column, return immediately — no DDL, no COMMIT,
    no transaction side effect on the caller. Only a missing/stale table falls
    through to the (legitimately committing) ``executescript`` + ``_migrate``.

    ``row_factory`` is also no longer force-set here — ``fts.connect`` sets
    ``sqlite3.Row`` for the shared daemon path and the read helpers that need
    named columns set it locally — so this no longer rewrites a caller's
    ``row_factory`` as a hidden side effect (#532).
    """
    # Positional access (PRAGMA col 1 = name) so the probe is row_factory-agnostic.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(intents)")}
    if cols >= _EXPECTED_COLS:
        return  # up to date — pure read, no implicit COMMIT
    conn.executescript(SCHEMA)
    _migrate(conn)


# --- when_text surface normalization (dedup only) ------------------------------
#
# ``when_text`` is free text the LLM emitted ("周五3点" / "周五下午3点" /
# "星期五15:00" …). Used verbatim it fans the SAME commitment out into several
# dedup keys, so one meeting surfaces multiple times — a compounding-cost bug
# under the asymmetric-cost constitution. ``normalize_when_text`` folds the
# *surface form* deterministically; it does NOT do semantic date resolution
# (no calendar math, no "which Friday"). The normalized form is used ONLY for
# dedup-key computation — storage and display keep the original text.

# Date/period compound words rewritten before tokenizing (今晚 = 今天 + 晚上).
_WHEN_COMPOUND = (
    ("tonight", "今天晚上"),
    ("今晚", "今天晚上"),
    ("今早", "今天早上"),
    ("明晚", "明天晚上"),
    ("明早", "明天早上"),
    ("明晨", "明天早上"),
)
# Common date words → stable tokens (brace-delimited so a following bare digit
# can't glue onto the token). English words FIRST: a Chinese-produced token like
# "{tomorrow}" contains the English word, so replacing English afterwards would
# double-wrap it.
_WHEN_DATES = (
    ("tomorrow", "{tomorrow}"),
    ("today", "{today}"),
    ("今天", "{today}"),
    ("明天", "{tomorrow}"),
    ("后天", "{day_after}"),
)

_AM_WORDS = ("凌晨", "清晨", "早上", "早晨", "上午")
_PM_WORDS = ("中午", "下午")
_EVE_WORDS = ("傍晚", "晚上", "夜里", "夜间")
_PERIOD_ALT = "|".join(_AM_WORDS + _PM_WORDS + _EVE_WORDS)

# A time RANGE whose LEFT endpoint carries a period word but whose RIGHT endpoint
# does not ("下午2点到3点半") — the period must propagate to the right clock or it
# resolves to the AM hour (03:30), landing ``end_at`` ~12h off and polluting
# ``valid_until`` (#631 nit CC). Capture: (period)(left-clock)(separator)(right
# clock with NO period prefix). The right clock is required to start with a bare
# digit (no period of its own) so we never overwrite an explicit "下午2点到晚上8点".
_RANGE_SEP = r"到|至|-|~|—|―|－"
_PERIOD_RANGE_RE = re.compile(
    rf"(?P<period>{_PERIOD_ALT})"
    rf"(?P<left>\d{{1,2}}(?:[点时](?:半|一刻|三刻|\d{{1,2}}分?)?|:\d{{2}}))"
    rf"(?P<sep>{_RANGE_SEP})"
    rf"(?P<right>\d{{1,2}}(?:[点时]|:\d{{2}}))"
)


def _propagate_period_in_range(s: str) -> str:
    """Re-inject the left endpoint's period word before a period-less right
    endpoint in a time range, so both halves resolve to the same daypart."""

    def repl(m: re.Match[str]) -> str:
        return f"{m['period']}{m['left']}{m['sep']}{m['period']}{m['right']}"

    return _PERIOD_RANGE_RE.sub(repl, s)


# English clock: "3pm" / "3:30pm" / "12am" (whitespace already stripped).
_EN_TIME_RE = re.compile(r"(?<!\d)(\d{1,2})(?::(\d{2}))?(a|p)\.?m\.?")
# Chinese clock with optional period prefix: "下午3点" / "晚上8点半" / "15时" /
# "3点15分" — or an already-digital "HH:MM" with optional prefix ("晚上20:00").
_CN_TIME_RE = re.compile(
    rf"({_PERIOD_ALT})?(\d{{1,2}})(?:[点时](半|一刻|三刻|(\d{{1,2}})分?)?|:(\d{{2}}))"
)
# A relative-week prefix (下/下下/下个/下一个 周X) shifts the resolved date by
# whole weeks (#618). The prefix is captured so it survives into the dedup
# token (``{wk5+7}``) instead of being silently dropped — dropping it resolved
# 下周五 to *this* Friday, landing a future commitment on a past date.
_CN_WEEKDAY_RE = re.compile(r"(下下|下个?|下一个?)?(?:周|星期|礼拜|週)([一二三四五六日天])")
_CN_WEEKDAY_MAP = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}
# Chinese relative-week prefix → whole-week offset added to the weekday.
_CN_WEEK_OFFSET = {"下": 7, "下个": 7, "下一": 7, "下一个": 7, "下下": 14}
_EN_WEEKDAY_MAP = {
    "monday": 1,
    "mon": 1,
    "tuesday": 2,
    "tues": 2,
    "tue": 2,
    "wednesday": 3,
    "wed": 3,
    "thursday": 4,
    "thurs": 4,
    "thur": 4,
    "thu": 4,
    "friday": 5,
    "fri": 5,
    "saturday": 6,
    "sat": 6,
    "sunday": 7,
    "sun": 7,
}
# Longest-first alternation; lookarounds instead of \b because whitespace is
# already stripped ("friday3pm" — y→3 is \w→\w, so \b would never match).
_EN_WEEKDAY_RE = re.compile(
    r"(?<![a-z])(" + "|".join(sorted(_EN_WEEKDAY_MAP, key=len, reverse=True)) + r")(?![a-z])"
)
_PUNCT_RE = re.compile(r"[，。、,.!！?？;；:：~～]+$|[，。、,!！?？;；~～]")
_HHMM_RE = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)")


def _period_hour(period: str | None, hour: int) -> int:
    """Resolve an explicit period word + hour to a 24h hour (surface rule only)."""
    if hour >= 13:  # already 24h ("下午15点" is odd but unambiguous)
        return hour
    if period in _AM_WORDS:
        return 0 if hour == 12 else hour
    if period in _PM_WORDS:
        return 12 if hour == 12 else hour + 12
    if period in _EVE_WORDS:
        return 0 if hour == 12 else hour + 12  # 晚上8点=20:00; 晚上12点=00:00
    return hour  # no period marker: keep as given (no am/pm guessing)


def _en_time_repl(m: re.Match[str]) -> str:
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    if m.group(3) == "p":
        hour = 12 if hour == 12 else (hour + 12 if hour < 12 else hour)
    else:
        hour = 0 if hour == 12 else hour
    return f"{hour:02d}:{minute:02d}"


def _cn_time_repl(m: re.Match[str]) -> str:
    period, hour_s, cn_min, cn_min_digits, colon_min = m.groups()
    hour = _period_hour(period, int(hour_s))
    if colon_min is not None:
        minute = int(colon_min)
    elif cn_min == "半":
        minute = 30
    elif cn_min == "一刻":
        minute = 15
    elif cn_min == "三刻":
        minute = 45
    elif cn_min_digits is not None:
        minute = int(cn_min_digits)
    else:
        minute = 0
    return f"{hour:02d}:{minute % 60:02d}"


# Payload fields that are VOLATILE channel/provenance metadata, not the fact's
# identity. The LLM re-emits an info_need / reminder every ~60s as the user keeps
# typing, and the only thing that wobbles is which surface it saw the signal on
# (``channel`` = "System" one tick, "cmux" the next — same fact). Hashing the
# whole payload into the content dedup key (#529 only stripped ``rationale``)
# minted a fresh key per channel flip → a new row each round (#619 B2). The
# content digest must hash ONLY the fact's identity fields, so these are dropped
# before hashing.
#
# ``facets`` is the fast-path facetizer's derived sub-dict (#262) — and it carries
# its OWN ``facets["provenance"]`` (committed/proposed/inferred), the same volatile
# direction marker the top-level ``provenance`` is stripped for. Hashing ``facets``
# verbatim smuggled that volatile provenance back into the digest through the
# sub-dict, so an anchorless intent's content key flipped when only its provenance
# changed → fold failure → duplicate push (#269, the #619 B2 failure mode it was
# meant to kill). Facets are a derived projection of the same fact, never an
# identity input, so the whole sub-dict is dropped before hashing.
_VOLATILE_PAYLOAD_FIELDS = ("channel", "provenance", "facets")

# Free-text identity fields whose surface form the LLM re-phrases between
# block-flushes ("给 mina 写反馈" / "给 Yuna 写一下反馈"). NFKC+casefold-normalized
# and whitespace-stripped before hashing so paraphrase-stable variants of the
# SAME hint fold instead of fanning out (#619 B2).
_CONTENT_TEXT_FIELDS = ("text", "task_text", "title", "topic")


def normalize_content_payload(payload: dict) -> dict:
    """Identity-only projection of ``payload`` for the content dedup digest.

    Drops volatile channel/provenance/facets metadata (:data:`_VOLATILE_PAYLOAD_FIELDS`)
    and NFKC+casefold-normalizes the free-text identity fields
    (:data:`_CONTENT_TEXT_FIELDS`) so the digest tracks the fact, not the surface
    wording or the channel it was seen on. All other fields pass through verbatim
    (sorted at hash time). #619 B2.
    """
    out: dict = {}
    for k, v in payload.items():
        if k in _VOLATILE_PAYLOAD_FIELDS:
            continue
        if k in _CONTENT_TEXT_FIELDS and isinstance(v, str):
            v = re.sub(r"\s+", "", unicodedata.normalize("NFKC", v).casefold())
        out[k] = v
    return out


def normalize_when_text(text: str) -> str:
    """Deterministic *surface-form* normalization of a free-text time anchor.

    Folds spelling variants of the same expression — full/half width, spacing,
    weekday words (周五/星期五/礼拜五/Fri/Friday → ``{wk5}``), clock forms
    (下午3点/3pm/15:00/15点 → ``15:00``; 上午X点 → ``X:00``; 下午12点 → 12:00;
    晚上8点 → 20:00), and common date words (今天/今晚 → ``{today}``, 明天/明早 →
    ``{tomorrow}``, 后天 → ``{day_after}``) — WITHOUT semantic date parsing.
    A bare hour with no am/pm marker ("3点") keeps its literal hour (03:00):
    resolving it would require context this function deliberately does not have.

    Used only for :func:`dedup_key`; never for storage or display.
    """
    s = unicodedata.normalize("NFKC", str(text)).casefold()
    s = re.sub(r"\s+", "", s)
    s = _PUNCT_RE.sub("", s)
    for old, new in _WHEN_COMPOUND:
        s = s.replace(old, new)
    for old, new in _WHEN_DATES:
        s = s.replace(old, new)
    s = _EN_TIME_RE.sub(_en_time_repl, s)
    s = _propagate_period_in_range(s)  # #631 nit CC: "下午2点到3点半" → both 下午
    s = _CN_TIME_RE.sub(_cn_time_repl, s)
    s = _HHMM_RE.sub(lambda m: f"{int(m.group(1)):02d}:{m.group(2)}", s)
    s = _CN_WEEKDAY_RE.sub(_cn_weekday_repl, s)
    s = _EN_WEEKDAY_RE.sub(lambda m: "{wk" + str(_EN_WEEKDAY_MAP[m.group(1)]) + "}", s)
    return s


def _cn_weekday_repl(m: re.Match[str]) -> str:
    """``周五`` → ``{wk5}``; ``下周五`` → ``{wk5+7}``; ``下下周五`` → ``{wk5+14}``.

    The whole-week offset is encoded INTO the token so it reaches the resolver
    (``intent/normalize.py``) and the dedup key keeps relative-week variants
    distinct (本周五 ≠ 下周五)."""
    wk = _CN_WEEKDAY_MAP[m.group(2)]
    offset = _CN_WEEK_OFFSET.get(m.group(1), 0) if m.group(1) else 0
    return "{wk" + str(wk) + (f"+{offset}" if offset else "") + "}"


def dedup_key(intent: Intent) -> str:
    """Stable key so the same intent recognized twice isn't double-stored.

    Temporal scheduling intents (``meeting``/``calendar``/``reminder`` with a
    ``when_text`` and/or ``with``) collapse on those anchors **across scopes** —
    the key intentionally omits ``scope``. Two reasons this must be
    scope-agnostic:

    1. The session cutter (5-min idle gap, etc.) can split one trajectory across
       two sessions (``"周五行吗?"`` lands in session A, ``"行"`` in session B);
       the trajectory recognizer re-reads cross-session timeline history, so the
       same meeting can surface under ``session-A`` and ``session-B``.
    2. During the timeline/trajectory shadow-coexistence period the same meeting
       is recognized once as ``scope="timeline"`` and once as
       ``scope="session-<id>"``.

    In both cases it is the *same* commitment and must fold into one row.

    Content-only intents (e.g. an ``info_need`` hint with no temporal anchor)
    keep the ``scope`` prefix + a content hash, so two genuinely different hints
    in the same scene coexist while an identical hint pushed twice is suppressed.
    """
    # ``when_text`` enters the key through ``normalize_when_text`` so surface
    # variants of the same anchor ("周五下午3点" vs "星期五15:00") fold into one
    # row instead of re-surfacing the same commitment (storage keeps the raw
    # text). Pre-normalization rows are reached via :func:`legacy_dedup_key`.
    when_raw = str(intent.payload.get("when_text", ""))
    return _compose_key(intent, when_raw=when_raw, when_for_key=normalize_when_text(when_raw))


def legacy_dedup_key(intent: Intent) -> str | None:
    """The pre-normalization dedup key (raw ``when_text``), or ``None`` when it
    is identical to :func:`dedup_key`.

    Migration shim: rows persisted before ``when_text`` normalization carry the
    raw-text key. Dedup lookups check this key *in addition to* the canonical
    one so a re-recognition still folds onto its legacy row instead of
    double-surfacing. New rows are always written with the canonical key.
    """
    when_raw = str(intent.payload.get("when_text", ""))
    legacy = _compose_key(intent, when_raw=when_raw, when_for_key=when_raw)
    return legacy if legacy != dedup_key(intent) else None


def _compose_key(intent: Intent, *, when_raw: str, when_for_key: str) -> str:
    """Assemble the key. The temporal-vs-content branch is decided on the RAW
    ``when_text`` so normalization can never flip the key shape."""
    # Event-based intents fold per (kind, trigger): the SAME prospective intent
    # ("下次打开 Figma 提醒改图标") recognized twice must collapse, but two armed
    # intents waiting on DIFFERENT apps must NOT — so the trigger is part of the
    # key. Appended (not prefixed) so existing immediate intents — fire_on="" —
    # produce a byte-identical key (zero regression).
    trigger_suffix = ""
    if intent.fire_on:
        cfg_digest = hashlib.sha1(
            json.dumps(intent.fire_config, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:8]
        trigger_suffix = f"|@{intent.fire_on}:{cfg_digest}"

    people = ",".join(sorted(str(p) for p in (intent.payload.get("with") or [])))
    if when_raw or people:
        return f"{intent.kind}|{when_for_key}|{people}{trigger_suffix}"
    # Content-only intents (e.g. an ``info_need`` hint with no temporal anchor)
    # hash ONLY the fact's IDENTITY fields — NOT ``rationale`` (#529) and NOT the
    # volatile channel/provenance metadata or the un-normalized surface wording
    # (#619 B2). ``rationale`` is LLM free-text re-phrased every ~60s block-flush;
    # ``channel`` flips System↔cmux for the same fact; ``text`` gets re-paraphrased
    # — each previously minted a fresh key → a new row each round → HUD republish.
    # :func:`normalize_content_payload` projects the payload onto its stable
    # identity before hashing so the digest tracks the hint, not its surface.
    content = json.dumps(
        normalize_content_payload(intent.payload), sort_keys=True, ensure_ascii=False
    )
    digest = hashlib.sha1(content.encode("utf-8")).hexdigest()[:12]
    return f"{intent.scope}|{intent.kind}||{digest}{trigger_suffix}"


def exists(conn: sqlite3.Connection, key: str) -> bool:
    if not key:
        return False
    ensure_schema(conn)
    row = conn.execute("SELECT 1 FROM intents WHERE dedup_key = ? LIMIT 1", (key,)).fetchone()
    return row is not None


def id_for_dedup_key(conn: sqlite3.Connection, key: str) -> int | None:
    """Return the row id for ``key`` (most recent if several), or None.

    Lets a recognizer resolve the canonical row id of an intent it just
    persisted — including the case where the intent folded onto an existing row
    via dedup (``persist_intent`` returns None then) — so consumers like the
    debug HUD can address it by id for status write-back.
    """
    if not key:
        return None
    ensure_schema(conn)
    row = conn.execute(
        "SELECT id FROM intents WHERE dedup_key = ? ORDER BY id DESC LIMIT 1", (key,)
    ).fetchone()
    return int(row[0]) if row else None


def get_by_dedup_key(conn: sqlite3.Connection, key: str) -> Intent | None:
    """Full row for ``key`` (most recent if several), or ``None``.

    The material-change comparison (R3, ``sink.material_change``) needs the
    stored confidence / payload / status — not just the id that
    :func:`id_for_dedup_key` returns.
    """
    if not key:
        return None
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        f"SELECT {_SELECT_COLS} FROM intents WHERE dedup_key = ? ORDER BY id DESC LIMIT 1",
        (key,),
    ).fetchone()
    return _row_to_intent(row) if row else None


def _abs_delta_within(a: str, b: str, window: timedelta) -> bool | None:
    """``|a - b| <= window`` for two ISO timestamps, tz-safe (#586 pattern).

    ``resolved_at`` carries an explicit offset (anchored at the capture ts) while
    a recognition ``ts`` is usually a naive local string; a naive side is assumed
    local (``astimezone()``) before the instants are compared, so an
    offset-vs-naive pair never raises. Returns ``None`` when either side is
    unparseable so the caller can fall back to a coarser rule.
    """
    try:
        da = datetime.fromisoformat(a)
        db = datetime.fromisoformat(b)
    except (ValueError, TypeError):
        return None
    if da.tzinfo is None:
        da = da.astimezone()
    if db.tzinfo is None:
        db = db.astimezone()
    return abs(da - db) <= window


def _same_occurrence(stored: Intent, incoming: Intent) -> bool:
    """Does ``stored`` describe the SAME occurrence as ``incoming`` (#525)?

    Decided on the deterministic temporal grounding when BOTH rows carry it
    (``resolved_at`` within ``_DEDUP_GROUND_BUCKET`` = the same calendar
    instant), else on a recognition-time window (``ts`` within ``_DEDUP_WINDOW``
    = one trajectory, not a weekly recurrence). A stored row past its useful life
    (``expired``, or ``open`` with an elapsed ``valid_until``) is the PREVIOUS
    occurrence wrapping up and is never the same occurrence as a fresh
    recognition — so it stops suppressing.
    """
    if is_expired(stored, now=incoming.ts or datetime.now().isoformat(timespec="minutes")):
        return False
    if stored.resolved_at and incoming.resolved_at:
        grounded = _abs_delta_within(stored.resolved_at, incoming.resolved_at, _DEDUP_GROUND_BUCKET)
        if grounded is not None:
            return grounded  # fall through only when a resolved_at was unparseable
    if not stored.ts or not incoming.ts:
        return True  # missing recognition ts — degrade to legacy "any match suppresses"
    windowed = _abs_delta_within(stored.ts, incoming.ts, _DEDUP_WINDOW)
    return True if windowed is None else windowed


def find_live_duplicate(conn: sqlite3.Connection, key: str, incoming: Intent) -> Intent | None:
    """The stored row ``key`` matches that is a LIVE duplicate of ``incoming``'s
    occurrence (#525), or ``None``.

    Replaces the whole-history :func:`exists` boolean at the dedup choke point:
    a relative-token key ("周五15:00" → ``meeting|{wk5}15:00|...``) repeats every
    week, so an unbounded match permanently suppressed recurring commitments.
    This scans the most-recent rows for ``key`` and returns the first whose
    occurrence coincides with ``incoming`` (see :func:`_same_occurrence`); a
    stale prior occurrence yields ``None`` so the new occurrence is inserted.
    """
    if not key:
        return None
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT {_SELECT_COLS} FROM intents WHERE dedup_key = ? ORDER BY id DESC LIMIT 20",
        (key,),
    ).fetchall()
    for row in rows:
        stored = _row_to_intent(row)
        if _same_occurrence(stored, incoming):
            return stored
    return None


def same_fact_cross_form(conn: sqlite3.Connection, intent: Intent) -> Intent | None:
    """Cross-activation-form dedup lookup (#549 去重交互).

    The trigger suffix in :func:`dedup_key` keeps two armed intents waiting on
    DIFFERENT apps distinct — but it also means the SAME fact recognized once
    event-based (``fire_on`` set) and once immediate (``fire_on=""``) yields two
    different keys, so the plain exact-key dedup in the sink would let a twin
    row in. This helper finds the stored row holding the same fact under the
    *other* activation form (any status):

    - incoming event-based → look for the immediate-form row (exact base key,
      i.e. the key with the trigger stripped; plus the pre-normalization legacy
      key, mirroring the #467 shim);
    - incoming immediate → look for any event-form row (base key + the
      ``|@<fire_on>:<digest>`` suffix), most recent first.

    Same-form duplicates are NOT this function's job (the sink's exact-key
    match folds them), and two event-form intents with different triggers stay
    distinct by design. Content-only intents hash the normalized ``payload``
    into the base key (#529), so their cross-form fold fires on the same
    structured hint regardless of rationale wording.

    **Liveness guard (#626).** The match is liveness-aware, mirroring the
    same-form choke point (#525, :func:`find_live_duplicate`): a candidate row
    that is past its useful life (:func:`is_expired` — ``expired``, or an overdue
    ``open``/``armed`` row whose grounded ``valid_until`` elapsed) is the PREVIOUS
    occurrence wrapping up and no longer suppresses a fresh recognition, so the
    new occurrence inserts instead of being permanently swallowed. Without this
    guard a stale ``armed`` L7 row keeps the SAME base key every week (the
    relative-weekday token carries no anchored date) and silently suppresses
    every later same-fact occurrence forever (the issue's exact failure).
    ``consumed`` / ``dismissed`` are final user feedback — NOT a stale-lifecycle
    state, so :func:`is_expired` reports them live and they keep suppressing
    ("dismissed 永不复活" is preserved).
    """
    base_intent = dataclasses.replace(intent, fire_on="", fire_config={})
    base = dedup_key(base_intent)
    ensure_schema(conn)
    now = intent.ts or datetime.now().isoformat(timespec="minutes")
    if intent.fire_on:
        row = get_by_dedup_key(conn, base)
        if row is not None and not is_expired(row, now=now):
            return row
        legacy = legacy_dedup_key(base_intent)
        if legacy:
            legacy_row = get_by_dedup_key(conn, legacy)
            if legacy_row is not None and not is_expired(legacy_row, now=now):
                return legacy_row
        return None
    escaped = base.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT {_SELECT_COLS} FROM intents WHERE dedup_key LIKE ? ESCAPE '\\' "
        "ORDER BY id DESC LIMIT 20",
        (escaped + "|@%",),
    ).fetchall()
    for row in rows:
        stored = _row_to_intent(row)
        if not is_expired(stored, now=now):
            return stored
    return None


def update_intent_recognition(
    conn: sqlite3.Connection, *, intent_id: int, intent: Intent, canonical_key: str
) -> bool:
    """Material-change UPDATE (R3): refresh one row's recognition fields in place.

    Preserves the row's identity and user-feedback state — ``id`` / ``status`` /
    ``created_at`` / ``fire_*`` are deliberately NOT touched — and refreshes
    ``ts`` / ``confidence`` / ``rationale`` / ``payload`` / ``evidence`` to the
    re-recognition. ``dedup_key`` is migrated to the canonical (normalized) key,
    healing pre-normalization legacy rows on their first material update — safe
    because the caller only reaches here when no canonical-key row exists (the
    canonical key is checked first in ``sink``), so no twin can collide.

    ``schema_sources`` (R4 provenance) follows the same "latest recognition
    wins" rule **only when the re-recognition actually carries sources**: a
    material update from a round that injected schemas refreshes the column
    (the user's eventual feedback reacts to that latest round), while a
    sourceless re-recognition (e.g. the fast path, which never stamps
    ``schema_sources``) leaves the stored provenance untouched so the R4
    feedback loop is never wiped to a no-op.

    ``resolved_at`` / ``valid_until`` (#546 temporal grounding) ratchet the
    same way via COALESCE: a re-recognition whose ``when_text`` resolved
    refreshes them; an unresolvable one never wipes existing grounding (losing
    ``valid_until`` would resurrect a row from the expiry lifecycle).
    """
    ensure_schema(conn)
    # ``kind`` migrates with ``dedup_key`` + ``payload`` to the incoming
    # recognition (#587): a cross-kind semantic fold (calendar re-statement folds
    # onto a meeting row, _FOLD_KIND_GROUPS) otherwise leaves the row with
    # kind='meeting' but dedup_key='calendar|...' + calendar payload — kind列与
    # 路由键/载荷脱节, breaking any kind-filtered read (active _PROPOSABLE_KINDS,
    # fold-group归属). All three now come from ``intent``, keeping the row自洽.
    if intent.schema_sources:
        cur = conn.execute(
            "UPDATE intents SET ts = ?, kind = ?, confidence = ?, rationale = ?, payload = ?, "
            "evidence = ?, dedup_key = ?, schema_sources = ?, "
            "resolved_at = COALESCE(?, resolved_at), "
            "valid_until = COALESCE(?, valid_until) WHERE id = ?",
            (
                intent.ts,
                intent.kind,
                intent.confidence,
                intent.rationale,
                intent.payload_json(),
                intent.evidence_json(),
                canonical_key,
                json.dumps(intent.schema_sources, ensure_ascii=False),
                intent.resolved_at,
                intent.valid_until,
                intent_id,
            ),
        )
    else:
        cur = conn.execute(
            "UPDATE intents SET ts = ?, kind = ?, confidence = ?, rationale = ?, payload = ?, "
            "evidence = ?, dedup_key = ?, "
            "resolved_at = COALESCE(?, resolved_at), "
            "valid_until = COALESCE(?, valid_until) WHERE id = ?",
            (
                intent.ts,
                intent.kind,
                intent.confidence,
                intent.rationale,
                intent.payload_json(),
                intent.evidence_json(),
                canonical_key,
                intent.resolved_at,
                intent.valid_until,
                intent_id,
            ),
        )
    conn.commit()
    return cur.rowcount > 0


def id_for_intent(conn: sqlite3.Connection, intent: Intent) -> int | None:
    """Resolve ``intent``'s canonical row id, trying the canonical dedup key
    first and falling back to the legacy (pre-normalization) key, so HUD
    status write-back keeps working for rows stored before normalization."""
    row_id = id_for_dedup_key(conn, dedup_key(intent))
    if row_id is None:
        legacy = legacy_dedup_key(intent)
        if legacy:
            row_id = id_for_dedup_key(conn, legacy)
    return row_id


def source_capture_stem(intent: Intent) -> str | None:
    """The capture-buffer file stem this intent was recognized FROM, or ``None``.

    #7 provenance固化: read off the intent's first ``capture``-sourced evidence
    entry (``IntentEvidence.source == "capture"``, ``ref_id`` = the buffer file
    stem the fast K1 path stamps). Persisted into the dedicated ``source_capture``
    column at insert so "intent → that exact screenshot" is a direct indexed
    reverse query — no fuzzy time-window join, no re-parsing the evidence JSON.

    Returns ``None`` when the intent has no capture-sourced evidence (e.g. the
    slow trajectory path, whose evidence refs ``timeline_block`` ids); such rows
    keep ``source_capture`` NULL and fall back to the time-window join.
    """
    for ev in intent.evidence:
        source = ev.source if hasattr(ev, "source") else (ev or {}).get("source", "")  # type: ignore[union-attr]
        ref = ev.ref_id if hasattr(ev, "ref_id") else (ev or {}).get("ref_id", "")  # type: ignore[union-attr]
        if str(source) == "capture" and str(ref):
            return str(ref)
    return None


def insert_intent(conn: sqlite3.Connection, intent: Intent) -> int:
    """Insert one intent row, returning its id."""
    ensure_schema(conn)
    key = dedup_key(intent)
    cur = conn.execute(
        """
        INSERT INTO intents
            (ts, scope, kind, confidence, status, rationale, payload, evidence,
             dedup_key, created_at, fire_on, fire_config, fired_at, schema_sources,
             resolved_at, valid_until, source_capture)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            intent.ts,
            intent.scope,
            intent.kind,
            intent.confidence,
            intent.status,
            intent.rationale,
            intent.payload_json(),
            intent.evidence_json(),
            key,
            datetime.now().isoformat(timespec="seconds"),
            intent.fire_on,
            json.dumps(intent.fire_config, ensure_ascii=False),
            intent.fired_at,
            json.dumps(intent.schema_sources, ensure_ascii=False),
            intent.resolved_at,
            intent.valid_until,
            source_capture_stem(intent),
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def actionable_capture_stems(conn: sqlite3.Connection) -> set[str]:
    """Every capture stem referenced as the ``source_capture`` of any intent (#7).

    The provenance ground-truth the retention scanner consults: a capture that
    produced an intent is "actionable" and earns extended screenshot retention.
    Index-served (``idx_intents_source_capture``); NULLs (legacy / slow-path
    rows) are naturally excluded. Returns an empty set when the table is absent
    or empty, so a fresh / un-migrated DB degrades to "nothing extended".
    """
    try:
        ensure_schema(conn)
        rows = conn.execute(
            "SELECT DISTINCT source_capture FROM intents WHERE source_capture IS NOT NULL"
        ).fetchall()
    except sqlite3.Error:
        return set()
    return {str(r[0]) for r in rows if r[0]}


def intent_ids_for_capture(conn: sqlite3.Connection, stem: str) -> list[int]:
    """Row ids of intents recognized FROM capture ``stem`` (#7 reverse query).

    The direct "intent → that screenshot" inverse: given a capture stem, the
    intents it produced. Empty for a stem no intent references (or a legacy NULL
    row) — the caller then falls back to the time-window join
    (``aggregator.captures_in_window``)."""
    if not stem:
        return []
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT id FROM intents WHERE source_capture = ? ORDER BY id ASC", (stem,)
    ).fetchall()
    return [int(r[0]) for r in rows]


def _row_to_intent(row: sqlite3.Row) -> Intent:
    keys = row.keys()
    return Intent.from_dict(
        {
            "id": row["id"],
            "kind": row["kind"],
            "scope": row["scope"],
            "confidence": row["confidence"],
            "status": row["status"],
            "rationale": row["rationale"],
            "ts": row["ts"],
            "payload": json.loads(row["payload"] or "{}"),
            "evidence": json.loads(row["evidence"] or "[]"),
            # New columns are tolerated-absent so a SELECT that didn't request
            # them (or a pre-migration row) still maps cleanly.
            "fire_on": row["fire_on"] if "fire_on" in keys else "",
            "fire_config": json.loads(
                (row["fire_config"] if "fire_config" in keys else None) or "{}"
            ),
            "fired_at": row["fired_at"] if "fired_at" in keys else None,
            "schema_sources": json.loads(
                (row["schema_sources"] if "schema_sources" in keys else None) or "[]"
            ),
            "resolved_at": row["resolved_at"] if "resolved_at" in keys else None,
            "valid_until": row["valid_until"] if "valid_until" in keys else None,
        }
    )


def recent_intents(
    conn: sqlite3.Connection,
    *,
    start: str,
    end: str,
    scope: str | None = None,
    status: str | None = None,
) -> list[Intent]:
    """Structured query over ``[start, end)`` by ISO ts, optional scope/status."""
    ensure_schema(conn)
    sql = f"SELECT {_SELECT_COLS} FROM intents WHERE ts >= ? AND ts < ?"
    params: list[object] = [start, end]
    if scope is not None:
        sql += " AND scope = ?"
        params.append(scope)
    if status is not None:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY ts ASC"
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_intent(r) for r in rows]


def get_intent(conn: sqlite3.Connection, intent_id: int) -> Intent | None:
    """One intent by row id (the recall-pack endpoint resolves scope/hints/raw handles
    from the row). Returns None when no such row exists."""
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        f"SELECT {_SELECT_COLS} FROM intents WHERE id = ? LIMIT 1", (intent_id,)
    ).fetchone()
    return _row_to_intent(row) if row is not None else None


def intents_for_scope(
    conn: sqlite3.Connection, scope: str, *, status: str | None = None
) -> list[Intent]:
    """All intents recognized within one scene, ts-ascending.

    The scene's slice of the unified stream — used by a :class:`ScenePack` to read
    back what it has recognized so far, independent of any time window.
    """
    # "" sorts before, and "￿" after, any real ISO ts, so this spans all rows.
    return recent_intents(conn, start="", end="￿", scope=scope, status=status)


def dismissed_kind_window(
    conn: sqlite3.Connection,
    *,
    kind: str,
    since: str,
    scope: str | None = None,
    scope_like: str | None = None,
) -> tuple[int, str | None]:
    """How many times ``kind`` was dismissed with a ``dismissed_at`` at/after
    ``since`` (ISO), and the MOST RECENT dismissal's ``dismissed_at`` — the inputs
    to the kind-level hard cooldown (#533, :mod:`intent.cooldown`).

    The window and the anchor are both keyed on ``dismissed_at`` (the instant the
    dismiss ACTION happened), NOT on ``ts`` (recognition time). The production
    dismiss path (``update_intent_status``) never touches ``ts``, so a row
    recognized days ago and dismissed just now carries an old ``ts`` but a fresh
    ``dismissed_at`` — anchoring the cooldown on ``ts`` would both under-count
    (recent dismisses of old intents fall outside the ``ts`` window) and mistime
    (the clock would start at recognition, not rejection). ``dismissed_at`` fixes
    both. Legacy rows dismissed before this column existed carry
    ``dismissed_at IS NULL`` and are simply not counted — fail-open, never a
    lifetime ban from un-timestamped history.

    Scope filtering has two shapes (mutually exclusive — ``scope_like`` wins):

    - ``scope`` (#533 #4): exact-match — only dismissals in that scope count, so
      the cooldown is ``(kind, scope)``-level and dismissing reminders in one
      session cannot silently mute reminders system-wide. ``None`` = global
      by-kind (kept for callers that genuinely want cross-scope).
    - ``scope_like`` (#533 慢路修复): a SQL ``LIKE`` pattern — dismissals whose
      scope matches the pattern count as ONE logical cooldown domain. The slow
      trajectory recognizer stamps a fresh ``session-<uuid>`` scope every session,
      so an exact ``scope`` match would reset the count to 0 every session and the
      cooldown would never trigger (欠抑). Passing ``scope_like='session-%'``
      collapses all per-session scopes into one cross-session domain for the
      cooldown count while the intents keep their true per-session identity scope.

    A single aggregate query (COUNT + MAX) over the indexed ``status`` / ``kind``
    columns — cheap enough to run on the sink's write path. Returns ``(0, None)``
    when nothing matches.
    """
    ensure_schema(conn)
    sql = (
        "SELECT COUNT(*), MAX(dismissed_at) FROM intents "
        "WHERE kind = ? AND status = 'dismissed' "
        "AND dismissed_at IS NOT NULL AND dismissed_at >= ?"
    )
    params: list[object] = [kind, since]
    if scope_like is not None:
        sql += " AND scope LIKE ?"
        params.append(scope_like)
    elif scope is not None:
        sql += " AND scope = ?"
        params.append(scope)
    row = conn.execute(sql, params).fetchone()
    if not row:
        return (0, None)
    return (int(row[0] or 0), row[1])


def completed_kind_counts(
    conn: sqlite3.Connection, *, since: str, min_count: int = 1, limit: int | None = None
) -> list[tuple[str, int, str]]:
    """``(kind, count, last_completed_at)`` for kinds the user accepted AND a
    `.context` task finished (``status='completed'``) with ``completed_at`` at/after
    ``since`` — the input to the reverse-loop POSITIVE prior
    (``recognizer._completed_prior``, spec 2026-06-26 G2/G3).

    The positive twin of :func:`dismissed_kind_window`, but aggregated across all
    kinds in ONE GROUP BY. Keyed on ``completed_at`` (the execution-finished
    instant, NOT ``ts``) for the same reason the dismiss window keys on
    ``dismissed_at`` — the write-back lands long after recognition, so a ``ts``
    window would drop a row recognized days ago but completed today. ``failed``
    rows are deliberately excluded: a positive prior must reflect *useful*
    completions only. Legacy rows carry ``completed_at IS NULL`` and are not
    counted (fail-open).

    ``min_count`` is the damping gate (a single completion is not a pattern);
    ``limit`` caps how many kinds are returned. Ranked by count desc, then kind
    asc (deterministic).
    """
    ensure_schema(conn)
    sql = (
        "SELECT kind, COUNT(*) AS n, MAX(completed_at) AS last FROM intents "
        "WHERE status = 'completed' AND completed_at IS NOT NULL AND completed_at >= ? "
        "GROUP BY kind HAVING n >= ? ORDER BY n DESC, kind ASC"
    )
    params: list[object] = [since, max(1, min_count)]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [(str(r[0]), int(r[1]), str(r[2])) for r in conn.execute(sql, params).fetchall()]


VALID_STATUSES = (
    "open", "armed", "consumed", "dismissed", "expired", "resolved", "completed", "failed",
)  # fmt: skip

# The subset a CLIENT may set through the R3 feedback口 (MCP `set_intent_status`
# + HTTP PATCH /intents/{id}). ``armed``/``expired`` are engine-owned lifecycle
# states — letting a client set them lets it craft an ``armed`` zombie with no
# ``fire_config`` or back-date a row to ``expired`` (#631 nit T).
#
# ``completed``/``failed`` (reverse-loop spec 2026-06-26 G2): the app's
# ``TaskRunner.maybeFinalize`` write-backs these for a `.context` task that was
# accepted (``consumed``) AND finished — ``completed`` = done, ``failed`` = ran
# but failed. They are distinct from ``consumed`` (accepted/enqueued) precisely
# so the recognizer can tell "accepted" from "actually done" (the strongest
# positive signal). Like ``consumed``/``dismissed`` they are terminal user-outcome
# states, so a client may set them.
FEEDBACK_STATUSES = ("open", "consumed", "dismissed", "completed", "failed")


def fold_candidates(
    conn: sqlite3.Connection,
    *,
    kinds: tuple[str, ...],
    since: str,
    statuses: tuple[str, ...] = ("open", "armed"),
    require_grounding: bool = True,
) -> list[Intent]:
    """Rows eligible as semantic-fold targets (#546): temporally grounded
    (``resolved_at`` set), kind in the same fold group, recognized since
    ``since``, still awaiting the user. Most-recent first so the sink folds
    onto the freshest matching row.

    ``require_grounding=False`` drops the ``resolved_at`` filter — the
    meeting_hint fold (2026-06-12) matches on phrase+people instead of the
    clock, and a hint's NORMAL shape is ungrounded.
    """
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    kind_q = ",".join("?" for _ in kinds)
    status_q = ",".join("?" for _ in statuses)
    grounding = " AND resolved_at IS NOT NULL" if require_grounding else ""
    rows = conn.execute(
        f"SELECT {_SELECT_COLS} FROM intents "
        f"WHERE kind IN ({kind_q}) AND status IN ({status_q}) "
        f"AND ts >= ?{grounding} ORDER BY id DESC",
        (*kinds, *statuses, since),
    ).fetchall()
    return [_row_to_intent(r) for r in rows]


def _valid_until_passed(valid_until: str, now: str) -> bool:
    """True when ``valid_until`` is strictly before ``now``, compared as
    tz-aware datetimes (#586).

    ``valid_until`` carries an explicit offset (stamp_temporal anchors it at the
    capture ts, e.g. ``...T23:00:00+08:00``) while ``now`` is a naive local
    ``datetime.now().isoformat()``. A plain string compare treats the trailing
    ``+08:00`` as ordinary characters, so when the capture tz ≠ the daemon's
    local tz the wall-clock numbers misalign by a full offset and the verdict
    inverts. Parse both (a naive side is assumed local) and compare instants;
    fall back to the string compare only if parsing fails — never raises.
    """
    try:
        vu = datetime.fromisoformat(valid_until)
        n = datetime.fromisoformat(now)
        if vu.tzinfo is None:
            vu = vu.astimezone()
        if n.tzinfo is None:
            n = n.astimezone()
        return vu < n
    except (ValueError, TypeError):
        return valid_until < now


def is_expired(intent: Intent, *, now: str) -> bool:
    """Lifecycle read-side filter (#546): is this row past its useful life?

    True for harvested rows (``status='expired'`` OR the evidence-auto-closed
    ``status='resolved'``) AND for still-pending rows (``open`` OR ``armed``)
    whose ``valid_until`` has passed but the daily harvest hasn't run yet —
    consumers (recall scene layer, active tick) must not act on either.

    ``resolved`` is load-bearing: the dedup suppression choke
    (``find_live_duplicate`` / ``same_fact_cross_form`` / ``_same_occurrence``)
    gates on ``not is_expired(...)``. If a ``resolved`` row read as live, an
    evidence-closed commitment with the same ``dedup_key`` would suppress the
    user's NEXT genuine same-fact intent forever (the "永不复活" bug #525/#626
    guard against). Done-by-evidence is past-useful-life exactly like ``expired``.

    The ``armed`` case is #629: a grounded ``armed`` L7 intent ("下次打开 X 时
    提醒…") whose waited-for event never fired and whose deadline elapsed used to
    read as live here (only ``open``/``expired`` were checked), so it leaked into
    the recall scene layer presented as "未过期素材". A grounded deadline coming
    and going is genuine expiry regardless of whether the row was still waiting on
    its event. An ungrounded ``armed`` row (``valid_until`` NULL — the normal L7
    shape) still never expires by deadline; it waits for the #532 ``created_at``
    TTL reap instead.
    """
    if intent.status in ("expired", "resolved"):
        return True
    return (
        intent.status in ("open", "armed")
        and bool(intent.valid_until)
        and _valid_until_passed(str(intent.valid_until), now)
    )


def debug_reset_open(conn: sqlite3.Connection) -> int:
    """DEBUG/operator only — flip EVERY ``open``/``armed`` intent to ``expired``.

    Not part of any pipeline. The recognition pipeline dedups/folds a new intent onto
    an existing live row (``find_live_duplicate`` / content-fold), so the SAME message
    re-sent during testing never produces a fresh intent — it folds onto the earlier
    one. Expiring the live rows removes those fold/dedup targets, so the next
    recognition INSERTs fresh. Pairs with ``event_source.reset_state()`` (clears the
    K1 seen-set so the capture isn't dropped as ``no_unseen``) — both are driven by the
    ``POST /intents/debug/reset-recognition`` endpoint. ``consumed``/``dismissed``
    (terminal user feedback) are left untouched. Returns the row count flipped.
    """
    cur = conn.execute("UPDATE intents SET status = 'expired' WHERE status IN ('open', 'armed')")
    return cur.rowcount


def expire_overdue(conn: sqlite3.Connection, *, now: str) -> list[int]:
    """Daily expiry harvest (#546 面2): flip stale grounded rows to ``expired``.

    Touches ``open`` AND ``armed`` rows with a non-NULL ``valid_until`` strictly
    before ``now`` — ``consumed`` / ``dismissed`` (final user feedback) and
    ungrounded rows (``valid_until`` NULL) never expire. Returns the list of
    harvested ids (its length is the count; the ids feed the live SSE flip).

    ``armed`` was added in #629: a grounded ``armed`` L7 intent whose waited-for
    event never fired and whose deadline elapsed previously fell through every
    gate — :func:`expire_overdue` skipped it (``open``-only) and the #532
    ``created_at`` TTL (:func:`expire_stale_armed`) only reaps by row age, so a
    row days young but already past its ``valid_until`` survived both and leaked
    into recall as live material. A grounded deadline coming and going is genuine
    expiry (``expired``), same as for an ``open`` row; the ``created_at`` TTL
    stays the path for the ungrounded never-fire reminder (terminal
    ``dismissed``, a soft-rejection semantic).

    The overdue判定 is done in Python via :func:`_valid_until_passed` (tz-aware),
    not a SQL ``valid_until < ?`` lexicographic compare — the offset-vs-naive
    skew (#586) would otherwise flip the verdict across timezones.
    """
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT id, valid_until FROM intents "
        "WHERE status IN ('open', 'armed') AND valid_until IS NOT NULL"
    ).fetchall()
    overdue = [int(row[0]) for row in rows if _valid_until_passed(str(row[1]), now)]
    if not overdue:
        return []
    placeholders = ",".join("?" * len(overdue))
    conn.execute(
        f"UPDATE intents SET status = 'expired' WHERE id IN ({placeholders})",
        overdue,
    )
    conn.commit()
    return overdue


# Armed (event-based, L7) intents wait dormant for a future trigger. They are
# deliberately exempt from the valid_until expiry harvest (the trigger may fire
# weeks later), but with NO ceiling they accumulate forever — and an armed
# intent whose ``fire_config.app`` the LLM wrote loosely ("Figma 设计稿") never
# matches a real frontmost app name, so it can NEVER fire and stays armed for
# good, re-scanned on every capture (#532). A generous TTL caps the dead ones
# without clipping legitimate "next time I open X" prospections: 14 days is far
# longer than the days-scale horizon of an L7 app-open reminder, so a real one
# fires well within it, while a never-matching one is reaped.
_ARMED_MAX_AGE = timedelta(days=14)


def expire_stale_armed(
    conn: sqlite3.Connection, *, now: str, max_age: timedelta = _ARMED_MAX_AGE, cfg=None
) -> list[int]:
    """TTL harvest for dormant ``armed`` intents (#532): flip ones older than
    ``max_age`` (by ``created_at``) to ``expired``. Returns the harvested ids
    (length = count).

    ``expired`` — a SYSTEM/staleness close, NOT ``dismissed`` (a USER/rejection
    close). Completeness audit (design-philosophy §9, 2026-06-30): the lifecycle
    terminal states partition by (who-closed × valence) — user/done=``consumed``,
    user/dropped=``dismissed``, system/done=``resolved``, system/dropped=``expired``.
    A TTL reap is system/dropped → ``expired`` (the same cell as the #612 ungrounded-
    open reaper). Crucially an ``armed`` row is DORMANT — never surfaced to the user —
    so labelling it ``dismissed`` ("the user rejected it") is dishonest: the user never
    saw it. ``expired`` is the honest label. It is also unconditionally clear of R3's
    ``_dismissed_prior`` window (``expired`` ∉ ``FEEDBACK_STATUSES``), so a reaped
    reminder never trains the recognizer against that kind — previously that held only
    by the age coincidence (``ts`` ≥ ``max_age`` > R3's 7-day window) and left a muddy
    ``dismissed``-with-``dismissed_at``-NULL row. The age compare is tz-aware
    (:func:`_abs_delta_within`) so a capture-tz ``created_at`` vs daemon-local ``now``
    never skews the cutoff.

    **Reverse-loop G5.2** (spec 2026-06-26 §3.3): a reaped armed event-intent never
    fired — its trigger prediction was a false positive — so BEFORE the flip each
    reaped row's SOURCE SCHEMAS get a negative confidence signal (the same
    ``apply_intent_feedback`` seam a HUD dismiss uses, called with
    ``new_status="dismissed"``). This is the SCHEMA-level channel ONLY and is
    ORTHOGONAL to the intent-lifecycle label above: the row itself still closes
    ``expired`` (it is never written as a ``dismissed`` intent row), so R3's kind-level
    ``_dismissed_prior`` — which reads ``status='dismissed'`` intent rows within a 7-day
    ``ts`` window — never sees it and the KIND is not penalised; only a SCHEMA that keeps
    predicting triggers that never happen loses confidence (recoverable — a later
    ``consumed`` raises it back). Gated (``armed_reap_schema_feedback_enabled``) + fully
    best-effort: schema feedback can NEVER block the reap, and only acts on rows that
    carry ``schema_sources``.
    """
    ensure_schema(conn)
    rows = conn.execute("SELECT id, created_at FROM intents WHERE status = 'armed'").fetchall()
    stale: list[int] = []
    for row in rows:
        created = str(row[1] or "")
        within = _abs_delta_within(created, now, max_age) if created else None
        # within is True when |created - now| <= max_age (still fresh); False =
        # older than the TTL → reap. Unparseable created_at (None) → leave it
        # (don't reap a row we can't age-check).
        if within is False:
            stale.append(int(row[0]))
    if not stale:
        return []
    # G5.2: flow source-schema false-positive feedback for each reaped row BEFORE the
    # status flip (apply_intent_feedback reads the row's OLD status = 'armed'). Whole
    # block is best-effort — the lifecycle reap below must run regardless.
    try:
        from .. import config as _config_mod

        _cfg = cfg if cfg is not None else _config_mod.load()
        if _cfg.intent_recognizer.armed_reap_schema_feedback_enabled:
            from . import schema_feedback

            for iid in stale:
                try:
                    schema_feedback.apply_intent_feedback(
                        conn, intent_id=iid, new_status="dismissed", cfg=_cfg
                    )
                except Exception:  # noqa: BLE001 — one bad schema never blocks the reap
                    logger.debug("armed-reap schema feedback failed for %s", iid, exc_info=True)
    except Exception:  # noqa: BLE001 — feedback unavailable must not block the reap
        logger.debug("armed-reap schema feedback skipped", exc_info=True)
    placeholders = ",".join("?" * len(stale))
    conn.execute(
        f"UPDATE intents SET status = 'expired' WHERE id IN ({placeholders})",
        stale,
    )
    conn.commit()
    return stale


# Ungrounded ``open`` intents (``valid_until`` NULL) are the open-side twin of
# the armed dead-row problem (#612): ``stamp_temporal`` could not resolve a
# ``when_text`` (info_need / assignment / meeting_hint / a vague reminder with no
# clock), so the row carries no deadline → ``expire_overdue`` (valid_until-only)
# never harvests it AND ``is_expired`` reads it live forever. In the field 66/70
# ``open`` rows were this shape, accumulating unbounded and polluting recall's
# scene layer with stale commitments. A generous age-based TTL caps them by
# ``created_at`` — the same lever the #532 armed reaper uses — without clipping
# the legitimately-recent ones the recognizer just minted. 30 days is far longer
# than the days-scale relevance horizon of an actionable info_need / assignment,
# so a real one is acted on (consumed) or grounded (folds into the valid_until
# lifecycle) well within it, while a never-resolved backlog row is reaped.
_OPEN_UNGROUNDED_MAX_AGE = timedelta(days=30)


def expire_stale_open(
    conn: sqlite3.Connection, *, now: str, max_age: timedelta = _OPEN_UNGROUNDED_MAX_AGE
) -> list[int]:
    """TTL harvest for UNGROUNDED ``open`` intents (#612): flip ``open`` rows with
    NO temporal grounding (``valid_until`` NULL) older than ``max_age`` (by
    ``created_at``) to ``expired``. Returns the harvested ids (length = count).

    This is the open-side equivalent of :func:`expire_stale_armed`. Two harvests
    cover the ``open`` stream, by row shape, never overlapping:

    - GROUNDED ``open`` (``valid_until`` set) → :func:`expire_overdue` by deadline.
      An old-but-far-future grounded commitment is genuinely still live, so this
      age TTL must skip it — hence the ``valid_until IS NULL`` guard.
    - UNGROUNDED ``open`` (``valid_until`` NULL) → here, by age. Without a deadline
      there is nothing for :func:`expire_overdue` to compare against, so age is the
      only bound available.

    Terminal state is ``expired`` (NOT ``dismissed``): semantically the row is a
    commitment whose relevance ran out, parallel to an overdue grounded ``open``
    that :func:`expire_overdue` flips to ``expired`` — staleness is code's job, not
    a user soft-rejection. ``expired`` is also clear of the R3 negative prior,
    which only reads ``status='dismissed'`` (``recognizer._dismissed_prior``), so a
    reaped backlog row never durably trains the recognizer against that kind. (The
    #532 armed reaper chose ``dismissed`` because a never-fired *prospective*
    reminder is a soft rejection; an ungrounded open commitment that simply aged
    out is not.)

    ``armed`` rows are NOT touched here — they have their own #532 ``created_at``
    TTL with a longer horizon and a ``dismissed`` terminal — and only ``open`` rows
    with ``valid_until IS NULL`` are candidates, so ``consumed`` / ``dismissed`` /
    ``expired`` (terminal) and grounded ``open`` rows are excluded by the SQL guard.
    The age compare is tz-aware (:func:`_abs_delta_within` pattern) so a capture-tz
    ``created_at`` vs a daemon-local ``now`` never skews the cutoff.
    """
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT id, created_at FROM intents WHERE status = 'open' AND valid_until IS NULL"
    ).fetchall()
    stale: list[int] = []
    for row in rows:
        created = str(row[1] or "")
        within = _abs_delta_within(created, now, max_age) if created else None
        # within is True when |created - now| <= max_age (still fresh); False =
        # older than the TTL → reap. Unparseable/empty created_at → leave it
        # (don't reap a row we can't age-check).
        if within is False:
            stale.append(int(row[0]))
    if not stale:
        return []
    placeholders = ",".join("?" * len(stale))
    conn.execute(
        f"UPDATE intents SET status = 'expired' WHERE id IN ({placeholders})",
        stale,
    )
    conn.commit()
    return stale


def restamp_overdue_grounding(conn: sqlite3.Connection) -> tuple[int, int]:
    """存量回填（stale-daemon 修复工具）：给缺 temporal grounding 的活跃行补章.

    Rows written by a daemon predating #546（或 hint TTL）carry NULL
    ``resolved_at``/``valid_until`` forever — they never fold and never expire.
    Re-run :func:`intent.normalize.stamp_temporal` over every ``open``/``armed``
    row still missing grounding and persist what it derives. Idempotent: rows
    that already carry grounding (or remain unparseable) are untouched.

    Returns ``(rescanned, restamped)``.
    """
    from . import normalize as normalize_mod  # local import — store stays base layer

    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT {_SELECT_COLS} FROM intents "
        "WHERE status IN ('open', 'armed') AND (resolved_at IS NULL OR valid_until IS NULL)"
    ).fetchall()
    restamped = 0
    for row in rows:
        intent = _row_to_intent(row)
        before = (intent.resolved_at, intent.valid_until)
        normalize_mod.stamp_temporal(intent)
        if (intent.resolved_at, intent.valid_until) == before:
            continue
        conn.execute(
            "UPDATE intents SET resolved_at = COALESCE(?, resolved_at), "
            "valid_until = COALESCE(?, valid_until) WHERE id = ?",
            (intent.resolved_at, intent.valid_until, intent.id),
        )
        restamped += 1
    conn.commit()
    return (len(rows), restamped)


def intents_armed(conn: sqlite3.Connection) -> list[Intent]:
    """All dormant event-based intents (``status='armed'``) awaiting their trigger.

    The activator scans these on each capture; they are deliberately invisible to
    ``recent_intents(status='open')`` so the active layer never proposes them
    before their event fires.
    """
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT {_SELECT_COLS} FROM intents WHERE status = 'armed' ORDER BY ts ASC"
    ).fetchall()
    return [_row_to_intent(r) for r in rows]


def arm_to_open(conn: sqlite3.Connection, *, intent_id: int, fired_at: str) -> bool:
    """Fire a dormant intent: ``armed → open`` + stamp ``fired_at``.

    Guarded on ``status='armed'`` so a double-fire (two captures of the same app
    in quick succession) is a no-op on the second. Returns True if it fired.

    ``ts`` is refreshed to ``fired_at`` too (#524): an L7 prospective intent
    ("下次打开 Figma 提醒改图标") typically fires hours/days after recognition,
    but the active tick selects open intents by ``ts`` within a ~15-min window
    (``recent_intents``); leaving ``ts`` at recognition time means the just-fired
    intent lands outside that window and never reaches ``pending_actions`` — the
    时机门 silently severed at the last mile. An event-fired intent becomes
    actionable *at fire time*, so ``ts`` rightfully tracks the fire moment;
    ``fired_at`` still preserves the recognition-vs-fire distinction.
    """
    ensure_schema(conn)
    cur = conn.execute(
        "UPDATE intents SET status = 'open', fired_at = ?, ts = ? "
        "WHERE id = ? AND status = 'armed'",
        (fired_at, fired_at, intent_id),
    )
    conn.commit()
    return cur.rowcount > 0


def update_intent_status(conn: sqlite3.Connection, *, intent_id: int, new_status: str) -> bool:
    """Set one intent's status (the R3 feedback write-back). Returns False if the
    status is invalid or no row matched.

    This is the SINGLE seam every status write-back funnels through (both HUD
    entry points — ``mcp/server.py::_set_intent_status`` and
    ``api/routes.py::set_intent_status`` — call here), so the R4 schema-level
    feedback hook lives inside it: before the row is updated (it must observe
    the OLD status to detect a real transition — the idempotency guard against
    double HUD clicks), the dismiss/accept is flowed back onto the intent's
    ``schema_sources`` confidence via
    :func:`intent.schema_feedback.apply_intent_feedback`. Best-effort: the hook
    is config-gated, lazily imported, and exception-swallowed — it can never
    block or change the status write-back itself (return contract unchanged).
    """
    if new_status not in VALID_STATUSES:
        return False
    ensure_schema(conn)
    # Read the OLD status BEFORE the UPDATE so the SSE status-change event (below)
    # carries an accurate ``previous_status`` and so we only publish a real flip.
    prev_row = conn.execute("SELECT status FROM intents WHERE id = ?", (intent_id,)).fetchone()
    prev_status = prev_row[0] if prev_row else None
    try:
        # Lazy import keeps this module light (schema_feedback pulls in the
        # markdown write seam) and avoids import cycles at module load.
        from . import schema_feedback

        schema_feedback.apply_intent_feedback(conn, intent_id=intent_id, new_status=new_status)
    except Exception:  # noqa: BLE001 — feedback is a side channel, never fatal
        logger.warning("schema feedback failed for intent %s", intent_id, exc_info=True)
    if new_status == "dismissed":
        # Stamp the dismiss INSTANT (#533): this — not ``ts`` (recognition time,
        # which this path leaves untouched) — is the kind-cooldown clock anchor.
        # COALESCE keeps the FIRST dismiss instant if the row is re-dismissed
        # (idempotent against a double HUD click), so the count/anchor reflect
        # distinct dismiss actions, not redundant clicks on one row.
        cur = conn.execute(
            "UPDATE intents SET status = ?, dismissed_at = COALESCE(dismissed_at, ?) WHERE id = ?",
            (new_status, datetime.now().astimezone().isoformat(timespec="seconds"), intent_id),
        )
    elif new_status in ("completed", "failed"):
        # Reverse-loop (spec 2026-06-26 G2): stamp the execution-finished INSTANT
        # server-side (mirrors ``dismissed_at``; NOT ``ts``). COALESCE keeps the
        # first stamp so a re-sent write-back (the app fires fire-and-forget) is
        # idempotent — distinct completions, not redundant retries.
        cur = conn.execute(
            "UPDATE intents SET status = ?, completed_at = COALESCE(completed_at, ?) WHERE id = ?",
            (new_status, datetime.now().astimezone().isoformat(timespec="seconds"), intent_id),
        )
    else:
        cur = conn.execute(
            "UPDATE intents SET status = ? WHERE id = ?",
            (new_status, intent_id),
        )
    conn.commit()
    ok = cur.rowcount > 0
    # Make every terminal status flip live on the SSE bus (the app removes the
    # stale suggestion card the instant it learns, instead of waiting on the
    # reconnect reconcile poll). Best-effort; a publish failure never changes the
    # return contract.
    if ok and new_status != prev_status and new_status in ("consumed", "dismissed", "expired"):
        publish_intent_status_change(
            intent_id, new_status=new_status, previous_status=prev_status, reason="user_feedback"
        )
    return ok


def publish_intent_status_change(
    intent_id: int,
    *,
    new_status: str,
    previous_status: str | None,
    reason: str,
    quote: str | None = None,
) -> None:
    """Publish one ``stage=intent type=resolved`` SSE event for a status flip
    (evidence auto-close, user feedback, or the daily expiry harvest), so the app
    can drop the stale card event-driven. The payload is lean (id + status only) —
    distinct from the full-intent ``persisted`` payload. Best-effort, never raises.
    """
    try:
        from .. import events as events_mod

        events_mod.publish(
            "intent",
            "resolved",
            {
                "id": int(intent_id),
                "new_status": new_status,
                "previous_status": previous_status,
                "reason": reason,
                "quote": quote,
            },
        )
    except Exception as exc:  # noqa: BLE001 — SSE publish is best-effort
        logger.debug("intent status-change publish failed (id %s): %s", intent_id, exc)


def resolve_intent(
    conn: sqlite3.Connection, *, intent_id: int, outcome: str, quote: str
) -> str | None:
    """Evidence-driven auto-close: flip an OPEN/ARMED intent to the terminal
    ``resolved`` status (later context showed it is已做/已拒/被取代).

    Returns the PREVIOUS status on success (for the SSE event), or ``None`` if the
    row was missing or already terminal — only ``open``/``armed`` rows are
    closable, mirroring ``sink._UPDATABLE_STATUSES`` so a row the user已 consumed/
    dismissed is never reopened-then-resolved.

    Deliberately writes ONLY ``status`` + the two audit columns: it does NOT stamp
    ``dismissed_at``/``consumed_at`` and does NOT call ``apply_intent_feedback``, so
    ``resolved`` stays inert to the kind-cooldown clock and the R3 dismissed-prior
    (both select on the literal ``dismissed``/``consumed`` status). An auto-close is
    not the user rejecting a recognition — it must not train the recognizer.
    """
    ensure_schema(conn)
    row = conn.execute("SELECT status FROM intents WHERE id = ?", (intent_id,)).fetchone()
    if not row:
        return None
    prev_status = row[0]
    if prev_status not in ("open", "armed"):
        return None
    cur = conn.execute(
        "UPDATE intents SET status = 'resolved', resolution_outcome = ?, resolution_quote = ? "
        "WHERE id = ? AND status IN ('open', 'armed')",
        (outcome, (quote or "")[:120], intent_id),
    )
    conn.commit()
    if cur.rowcount <= 0:
        return None
    return prev_status

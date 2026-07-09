"""Deterministic ``when_text`` temporal resolution — zero LLM (#546 的共享地基).

``when_text`` is free text the recognizer LLM emitted ("今天22:00" / "1小时内" /
"周五下午3点" …). :mod:`intent.store` already folds the *surface form* for
dedup-key purposes; this module goes one step further and resolves the text to
an actual point in time, anchored at the intent's recognition timestamp
(``intent.ts``). Two consumers share this base:

1. **Fact folding (sink 层语义折叠)** — "22:00" / "今天22:00" /
   "6月11日 (今天) 22:00" produce *different* dedup keys but the *same*
   ``resolved_at``; the sink folds them into one canonical row by ±30min
   bucket instead of inserting near-duplicates.
2. **Lifecycle expiry (过期收割)** — ``resolved_at`` + a per-kind grace period
   yields ``valid_until``; a daily harvest flips stale ``open`` rows to
   ``expired`` so the 22:00 meeting stops polluting recall / the active layer
   after it has happened.

Design constraints (mirrors the asymmetric-cost constitution):

- **Deterministic only.** No LLM, no fuzzy guessing. A form we cannot resolve
  with certainty returns ``(None, None)`` and everything behaves exactly as
  before — best-effort, never blocking, never wrong-with-confidence.
- **Anchored.** All relative forms ("明天", "1小时内", "周五") resolve against
  the *recognition* timestamp, not wall-clock at read time.

Supported forms (via :func:`persome.intent.store.normalize_when_text`'s
surface canonicalization + a small Chinese-numeral pre-pass):

- ``今天/明天/后天 + HH:MM`` (and bare ``HH:MM`` → anchor's date)
- ``周X 上午/下午X点`` (weekday → next matching future date; a same-day weekday
  whose time-of-day already passed rolls to next week, #618)
- ``下周X / 下下周X`` (relative-week prefix → the weekday of next / next-next
  ISO week, #618)
- ``N小时内 / N分钟后 / Nmin / Nh`` (relative to anchor)
- ``HH:MM - HH:MM`` (range → start resolves, end feeds ``valid_until``)
- ``晚上X点 / 下午X点 / 今晚十点`` (period words + Chinese-numeral hours)
- ``M月D日 + HH:MM`` (explicit date, same/next year)
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from ..logger import get
from .ontology import Intent
from .store import normalize_when_text

logger = get("persome.intent.normalize")

# Per-kind grace period appended after the event's (end) time before an open
# intent is considered expired. meeting/calendar: the commitment is moot ~1h
# after it started/ended; reminder: the deadline still matters for a day.
GRACE_BY_KIND: dict[str, timedelta] = {
    "meeting": timedelta(hours=1),
    "calendar": timedelta(hours=1),
    "reminder": timedelta(hours=24),
    # #631 nit BB: a meeting_hint that DOES carry a parseable when_text ("下周三
    # 下午3点碰一下") would otherwise fall through to the 1h default grace — but a
    # hint's willingness stays warm for the same week as an ungrounded one. Use
    # the same 7-day window the ungrounded HINT_TTL branch (below) gives, so a
    # grounded hint is not prematurely expired ~1h after its tentative slot.
    "meeting_hint": timedelta(days=7),
}
_DEFAULT_GRACE = timedelta(hours=1)

# meeting_hint TTL (2026-06-12 生产实测): hints carry no resolvable anchor
# ("改天"/"下次周会"), so the resolved_at-based expiry above never reaches them
# — they stayed ``open`` forever, polluting recall's scene layer long after the
# willingness went stale. A euphemism older than a week is dead either way
# (双方早就另约了或不了了之)；漏掉一条过期 hint = 有限损失，复读 = 复利。
HINT_TTL = timedelta(days=7)

# --- Chinese-numeral hours (今晚十点 / 下午两点) --------------------------------
# ``store.normalize_when_text`` only understands ASCII digits; convert numeral
# hours/counts to digits FIRST so "十点" becomes "10点". Only numerals directly
# followed by a time/duration unit are touched, so weekday words (周五) and
# minute words (一刻) survive untouched.
_CN_DIGIT = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CN_NUM_RE = re.compile(r"([零一二两三四五六七八九十]{1,3})(?=[点时]|小时|分钟)")


def _cn_num_to_int(s: str) -> int | None:
    """'十'→10, '十一'→11, '二十'→20, '二十三'→23, '两'→2; None if malformed."""
    if "十" in s:
        tens_s, _, units_s = s.partition("十")
        tens = _CN_DIGIT.get(tens_s) if tens_s else 1
        units = _CN_DIGIT.get(units_s) if units_s else 0
        if tens is None or units is None:
            return None
        return tens * 10 + units
    if len(s) == 1:
        return _CN_DIGIT.get(s)
    return None


def _cn_numerals_to_digits(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        n = _cn_num_to_int(m.group(1))
        return str(n) if n is not None else m.group(1)

    return _CN_NUM_RE.sub(repl, text)


# --- patterns over the canonical surface form ----------------------------------
# These run on the OUTPUT of ``normalize_when_text`` ("{today}22:00",
# "{wk5}15:00", "1小时内", "6月11日({today})22:00", …): whitespace stripped,
# clock forms already 24h ``HH:MM``, date words already brace tokens.
_TIME_RE = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)")
# ``{wk5}`` (this week's matching weekday) or ``{wk5+7}`` / ``{wk5+14}`` carrying
# a relative-week offset emitted by ``store._cn_weekday_repl`` for 下/下下 周X.
_WK_RE = re.compile(r"\{wk([1-7])(?:\+(\d+))?\}")
_MONTH_DAY_RE = re.compile(r"(?<!\d)(\d{1,2})月(\d{1,2})日")
_REL_RE = re.compile(
    r"(?<!\d)(\d{1,3})(?:个)?"
    r"(小时|hours|hour|hrs|hr|h|分钟|minutes|mins|min|分)"
    r"(?:之内|以内|内|之后|以后|后)?(?![\d:])"
)
_HOUR_UNITS = ("小时", "hours", "hour", "hrs", "hr", "h")

# Bare period-of-day → a conventional default clock time, used ONLY when the text carries NO
# explicit clock ("今晚" → 20:00, "明天上午" → 10:00). Ordered most-specific-first so a broad word
# never shadows the intended period. Best-effort by design: an approximate fire time beats NULL
# (no grounding at all) for a reminder/todo — the alternative left "今晚发 X" ungrounded forever.
_PERIOD_DEFAULT: list[tuple[re.Pattern[str], time]] = [
    (re.compile(r"凌晨"), time(1, 0)),
    (re.compile(r"清晨|一早|大早"), time(7, 0)),
    (re.compile(r"早上|早晨|今早|明早|早间"), time(8, 0)),
    (re.compile(r"上午"), time(10, 0)),
    (re.compile(r"中午|正午|晌午"), time(12, 0)),
    (re.compile(r"下午"), time(15, 0)),
    (re.compile(r"傍晚"), time(18, 0)),
    (re.compile(r"晚上|今晚|明晚|晚间|夜里|夜晚|夜间"), time(20, 0)),
]


def _period_default_time(raw: str) -> time | None:
    """Conventional default clock for a bare period-of-day word (no explicit clock). None when the
    text names no period — caller then keeps it ungrounded."""
    for rx, t in _PERIOD_DEFAULT:
        if rx.search(raw):
            return t
    return None


def resolve_when_text(
    when_text: str, *, anchor: datetime
) -> tuple[datetime | None, datetime | None]:
    """Resolve a free-text time anchor to ``(resolved_at, end_at)``.

    ``resolved_at`` is the commitment's point in time (meeting start / reminder
    deadline); ``end_at`` is only set when the text carries an explicit range
    (``14:00 - 15:30``). Anything unresolvable returns ``(None, None)`` —
    callers must treat that as "no temporal grounding", never as an error.
    """
    if not when_text or not str(when_text).strip():
        return (None, None)
    try:
        s = normalize_when_text(_cn_numerals_to_digits(str(when_text)))
    except Exception:  # noqa: BLE001 — resolution is best-effort by contract
        return (None, None)

    times = [
        time(int(m.group(1)), int(m.group(2)))
        for m in _TIME_RE.finditer(s)
        if int(m.group(1)) < 24 and int(m.group(2)) < 60
    ]

    if not times:
        # Relative forms ("1小时内" / "30分钟后" / "15min") — only meaningful
        # when no absolute clock time is present.
        m = _REL_RE.search(s)
        if m:
            n = int(m.group(1))
            delta = timedelta(hours=n) if m.group(2) in _HOUR_UNITS else timedelta(minutes=n)
            return (anchor + delta, None)
        # Bare period-of-day with no clock ("今晚" / "明天上午" / "下午") → the conventional default
        # hour for that period, so a reminder/todo still gets a fire time instead of NULL. The date
        # binds via the SAME _resolve_date ({tomorrow}/weekday markers) as a clocked time.
        pt = _period_default_time(str(when_text))
        if pt is not None:
            day = _resolve_date(s, anchor, time_of_day=pt)
            if day is not None:
                return (datetime.combine(day, pt, tzinfo=anchor.tzinfo), None)
        return (None, None)

    day = _resolve_date(s, anchor, time_of_day=times[0])
    if day is None:
        return (None, None)

    resolved = datetime.combine(day, times[0], tzinfo=anchor.tzinfo)
    end: datetime | None = None
    if len(times) >= 2:
        end = datetime.combine(day, times[1], tzinfo=anchor.tzinfo)
        if end <= resolved:  # "23:00 - 00:30" crosses midnight
            end += timedelta(days=1)
    # "晚上/今晚/夜里12点" 经 _period_hour 归成 00:00、_resolve_date 落到 anchor 当天
    # = 一个已过去十几小时的时间戳,intent 一落库即过期被静默丢弃(#564)。傍晚/夜间语境
    # 下的"12点"指的是今天结束的那个午夜 = 次日 00:00,推进一天(端点同步推进)。
    if times[0] == time(0, 0) and _is_evening_midnight(when_text):
        resolved += timedelta(days=1)
        if end is not None:
            end += timedelta(days=1)
    return (resolved, end)


# 傍晚/夜间语境（晚/夜）+ 12 点引用 → 午夜归 00:00 时应推进到次日（#564）。
# 凌晨/早上/中午/下午 等不含"晚/夜",故不会误推进（凌晨12点 ≈ 当天 00:00,语义正确）。
_EVENING_CUE_RE = re.compile(r"[晚夜]")
_TWELVE_RE = re.compile(r"12|十二")


def _is_evening_midnight(when_text: str) -> bool:
    """True when the raw text is an evening/night reference to 12 o'clock
    ("晚上/今晚/夜里12点") — meaning the UPCOMING midnight (next calendar day
    00:00), not anchor-day 00:00."""
    t = str(when_text)
    return bool(_EVENING_CUE_RE.search(t)) and bool(_TWELVE_RE.search(t))


def _resolve_date(s: str, anchor: datetime, *, time_of_day: time | None = None) -> date | None:
    """Pick the date the time-of-day binds to. No date marker → anchor's date
    (the dominant real-world case: "22:00" said at 13:00 means today 22:00).

    ``time_of_day`` (the clock time the text resolved to) lets the weekday branch
    avoid landing a future commitment on a *past* instant: when the matching
    weekday is the anchor's own day and that time-of-day has already passed, the
    intent means NEXT week's occurrence, not today's already-gone slot (#618)."""
    if "{tomorrow}" in s:
        return anchor.date() + timedelta(days=1)
    if "{day_after}" in s:
        return anchor.date() + timedelta(days=2)
    md = _MONTH_DAY_RE.search(s)
    if md:
        try:
            d = date(anchor.year, int(md.group(1)), int(md.group(2)))
        except ValueError:
            return None
        # "1月5日" said in December refers to NEXT January; tolerate a little
        # backward slack for same-day-but-earlier phrasing.
        if (anchor.date() - d).days > 30:
            try:
                d = d.replace(year=anchor.year + 1)
            except ValueError:
                return None
        return d
    wk = _WK_RE.search(s)
    if wk:
        target = int(wk.group(1))
        week_offset_days = int(wk.group(2)) if wk.group(2) else 0
        if week_offset_days:
            # Explicit 下/下下 周X: the weekday of the next (or next-next) ISO
            # week, counted from the Monday of anchor's own week — NOT
            # "next matching weekday + N days" (that would double-count).
            monday = anchor.date() - timedelta(days=anchor.isoweekday() - 1)
            return monday + timedelta(days=week_offset_days + (target - 1))
        ahead = (target - anchor.isoweekday()) % 7
        # No 下/下下 prefix but the weekday is anchor's own day and the clock
        # time has already passed → the speaker means next week's slot, not
        # today's gone time (#618).
        if ahead == 0 and time_of_day is not None and time_of_day <= anchor.time():
            ahead = 7
        return anchor.date() + timedelta(days=ahead)
    return anchor.date()  # "{today}" or no marker


def compute_valid_until(
    kind: str, resolved_at: datetime | None, end_at: datetime | None = None
) -> datetime | None:
    """Expiry timestamp: (explicit end | resolved_at) + per-kind grace period."""
    if resolved_at is None:
        return None
    return (end_at or resolved_at) + GRACE_BY_KIND.get(kind, _DEFAULT_GRACE)


def stamp_temporal(intent: Intent) -> None:
    """Best-effort: parse ``payload.when_text`` anchored at ``intent.ts`` and
    stamp ``intent.resolved_at`` / ``intent.valid_until`` (ISO8601, seconds).

    Unparseable input leaves both fields untouched (``None`` → row behaves
    exactly as before #546: never folds semantically, never expires). Never
    raises — temporal grounding must not block the canonical write.
    """
    try:
        anchor = datetime.fromisoformat(intent.ts) if intent.ts else datetime.now()
    except ValueError:
        return
    try:
        resolved, end = resolve_when_text(str(intent.payload.get("when_text") or ""), anchor=anchor)
    except Exception:  # noqa: BLE001 — defensive; resolve already best-effort
        logger.warning("when_text temporal stamp failed (ignored)", exc_info=True)
        return
    if resolved is None:
        # meeting_hint TTL: no resolvable anchor is the hint's NORMAL shape, so
        # expiry anchors at the recognition time instead — without this a hint
        # never expires (resolved_at-based valid_until stays None forever).
        if intent.kind == "meeting_hint" and not intent.valid_until:
            intent.valid_until = (anchor + HINT_TTL).isoformat(timespec="seconds")
        return
    intent.resolved_at = resolved.isoformat(timespec="seconds")
    valid_until = compute_valid_until(intent.kind, resolved, end)
    if valid_until is not None:
        intent.valid_until = valid_until.isoformat(timespec="seconds")

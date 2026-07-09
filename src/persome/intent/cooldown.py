"""Kind-level closed-set hard cooldown for the negative-feedback loop (#533).

设计宪法（``agent-docs/design-philosophy-intent.md``）：弹错一个意图 = **复利损失**
（侵蚀未来每一次介入的信任）；漏一个 = 有限损失。一个被反复 dismiss 的 kind 仍能换个
措辞（新 ``dedup_key``）立刻再弹，是这条不对称里最贵的失败模式。

在 #533 之前，唯一的负反馈是 **prompt-soft**：``recognizer._dismissed_prior`` 把"最近被
忽略 N 次"渲染成提示文字塞进 prompt，靠模型自觉别再 surface 同类——只有完全相等的
``dedup_key`` 才是硬闸。本模块把它升级成 **(kind, scope) 级闭集硬冷却**：当某个 kind 在
最近一个窗口内**在某个 scope 里**被 dismiss 达到阈值，该 (kind, scope) 进入硬冷却期——
冷却期内该意图**绕过 prompt 直接被闸掉**（不写库、不呈现），这正是哲学里 "Cursor 在线
策略"（拒绝即降低出手率）的确定性等价物。

时间维度是硬约束：冷却**必有到期时间**（窗口内最后一次 dismiss + 冷却时长），绝不终身
抑制——终身抑制把"软拒绝"误当成"永久禁令"，与 #534（kind 级冷却的再校准/解除）相关但
不在本批；本模块只负责"窗口内 ≥阈值 → 限时硬冷却"这一条最小闭环。

两个刻意的收紧（#533 review 反馈，避免"惩罚高反馈用户"陷阱）：

1. **(kind, scope) 而非全局 by-kind**：在一个会话里划掉 3 个 reminder，只冷却该 scope 的
   reminder，不波及全系统所有 scope。``cooldown.py`` 把 ``intent.scope`` 透传给
   ``dismissed_kind_window``。
2. **计数窗口 = 冷却同量级**：默认 ``cooldown_window_days=1``（24h）与 ``cooldown_hours``
   （24h）同量级。否则一个 7 天宽窗 + 每次 dismiss 滑动重置 24h，会让一个一周内零星划掉
   3 次的活跃用户近乎常驻冷却。同量级窗口要求 3 次 dismiss 集中在 ~24h 内才触发，复发
   ``_dismissed_prior`` 当年"惩罚高反馈用户"陷阱的概率大降。

锚点是 ``dismissed_at``（dismiss 动作发生的时刻）而非 ``ts``（识别时刻）：生产 dismiss
路径 ``update_intent_status`` 只改 status、绝不动 ts，所以一条很久前识别、刚刚被划掉的
意图带着旧 ts 但新 ``dismissed_at``——锚在 ts 上会既漏挡（旧意图刚被划，MAX(ts) 已在窗
外）又误计时。``dismissed_at`` 两头都修。

闭集语义：动作仍是那几个 + 一个"啥也不做"。硬冷却就是把"啥也不做"在 (kind, scope) 维度
上确定性地选出来——信号开集（用户千变万化），动作闭集（冷却 = 确定性地收敛到 no-op）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from .. import config as config_mod
from ..config import Config
from ..logger import get
from . import store as intent_store

logger = get("persome.intent.cooldown")

# The slow trajectory recognizer (``intent/recognizer.py:scope_for_session``)
# stamps a brand-new ``session-<uuid>`` scope every session — it is the most
# prolific cross-session intent producer AND the path most prone to re-recognizing
# a dismissed kind under a new wording. Keying the cooldown on that ephemeral
# per-session scope reset the dismiss count to 0 every session, so the hard
# cooldown NEVER fired on the slow path (欠抑 — the feature was名存实亡 on the very
# path #533 most needs to protect). We collapse every per-session scope into ONE
# stable cross-session cooldown domain so ``(kind, session-A) + (kind, session-B)
# + …`` count together. The intents keep their true per-session identity scope in
# the ``intents`` table — only the COOLDOWN count is folded.
#
# fast-K1 (constant ``"fast-K1"`` scope) and meeting packs (per-meeting stable
# ``"meeting-<id>"`` scope) are unaffected: neither matches ``session-`` so both
# stay on exact-scope matching, exactly as before.
_SESSION_SCOPE_PREFIX = "session-"
_SESSION_SCOPE_LIKE = "session-%"


def _scope_filter(scope: str | None) -> tuple[str | None, str | None]:
    """Map an intent's identity ``scope`` to the cooldown count's scope filter.

    Returns ``(exact_scope, scope_like)`` — at most one is non-None — for
    :func:`store.dismissed_kind_window`:

    - a per-session slow-trajectory scope (``session-<uuid>``) → ``(None,
      "session-%")``: count across ALL sessions as one stable domain;
    - any other scope (fast-K1, meeting, explicit test scope) → ``(scope, None)``:
      unchanged exact-match;
    - ``None`` (legacy global by-kind) → ``(None, None)``.
    """
    if scope is None:
        return (None, None)
    if scope.startswith(_SESSION_SCOPE_PREFIX):
        return (None, _SESSION_SCOPE_LIKE)
    return (scope, None)


def cooldown_until(
    conn: sqlite3.Connection,
    kind: str,
    *,
    now: datetime | None = None,
    window_days: float = 1.0,
    dismiss_threshold: int = 3,
    cooldown_hours: float = 24.0,
    scope: str | None = None,
) -> datetime | None:
    """The instant the current hard cooldown for ``kind`` (optionally scoped)
    expires, or ``None`` when the kind is NOT in cooldown.

    Deterministic, zero-LLM. A kind enters cooldown when it has been dismissed
    at least ``dismiss_threshold`` times within the last ``window_days`` days
    (R3's negative金矿 read as a confidence vote), counting only dismissals whose
    ``dismissed_at`` (the dismiss action instant — NOT ``ts``, recognition time)
    falls in the window and, when ``scope`` is given, only in that scope. The
    cooldown then runs for ``cooldown_hours`` measured from the **most recent**
    dismissal in that window — so it always expires (never a lifetime ban) and a
    kind the user stops dismissing naturally heals out of cooldown once the
    latest rejection ages past ``cooldown_hours``.

    Tuning knobs (all from config, all relaxable, the whole gate is killable):
    raising ``dismiss_threshold`` makes cooldown rarer, shortening
    ``cooldown_hours`` makes it heal faster, widening ``window_days`` makes more
    sparse dismissals trigger it (the default keeps it同量级 with the cooldown so
    active feedback-givers are not near-permanently cooled). ``cooldown_hours <=
    0`` is treated as "no cooldown" defensively (a misconfig must never become a
    lifetime ban).

    Best-effort: any DB error returns ``None`` (fail-open — 漏挡一次冷却 = 有限
    损失，硬挡一个真意图才是复利损失，所以宁可放行)。
    """
    if not kind or dismiss_threshold <= 0 or cooldown_hours <= 0:
        return None
    now = now or datetime.now().astimezone()
    window_start = (now - timedelta(days=window_days)).isoformat()
    # Per-session slow-trajectory scopes fold into one cross-session domain
    # (see _scope_filter); fast-K1 / meeting / explicit scopes stay exact-match.
    exact_scope, scope_like = _scope_filter(scope)
    try:
        count, last_ts = intent_store.dismissed_kind_window(
            conn, kind=kind, since=window_start, scope=exact_scope, scope_like=scope_like
        )
    except Exception as exc:  # noqa: BLE001 — gate must never break a write
        logger.debug("cooldown lookup failed for kind=%s scope=%s: %s", kind, scope, exc)
        return None
    if count < dismiss_threshold or not last_ts:
        return None
    # Cooldown expires cooldown_hours after the most recent dismissal — a hard
    # TIME bound, never a lifetime suppression.
    try:
        last = datetime.fromisoformat(last_ts)
    except ValueError:
        return None
    if last.tzinfo is None:
        last = last.astimezone()
    n = now if now.tzinfo is not None else now.astimezone()
    until = last + timedelta(hours=cooldown_hours)
    if n >= until:
        return None
    logger.info(
        "intent kind in hard cooldown: kind=%s scope=%s dismissed=%d in %.2gd, last=%s, until≈%s",
        kind,
        scope or "(global)",
        count,
        window_days,
        last_ts,
        until.isoformat(timespec="minutes"),
    )
    return until


def kind_in_cooldown(
    conn: sqlite3.Connection,
    kind: str,
    *,
    now: datetime | None = None,
    window_days: float = 1.0,
    dismiss_threshold: int = 3,
    cooldown_hours: float = 24.0,
    scope: str | None = None,
) -> bool:
    """True when ``kind`` (optionally scoped) is currently under a hard cooldown.

    Thin boolean wrapper over :func:`cooldown_until` — kept for callers/tests that
    only need the yes/no verdict.
    """
    return (
        cooldown_until(
            conn,
            kind,
            now=now,
            window_days=window_days,
            dismiss_threshold=dismiss_threshold,
            cooldown_hours=cooldown_hours,
            scope=scope,
        )
        is not None
    )


def suppression_for(
    conn: sqlite3.Connection,
    kind: str,
    *,
    scope: str,
    cfg: Config | None = None,
    now: datetime | None = None,
) -> datetime | None:
    """Config-gated, (kind, scope)-scoped cooldown lookup used at the sink choke
    point. Returns the cooldown's expiry instant when the kind is suppressed in
    this scope, else ``None``.

    Returns ``None`` (never suppress) when the cooldown is disabled — the
    kill-switch — so a flipped flag fully restores the pre-#533 prompt-soft-only
    behavior. Otherwise delegates to :func:`cooldown_until` with the configured
    window/threshold/duration, scoped to ``scope``.
    """
    cfg = cfg if cfg is not None else config_mod.load()
    ir = cfg.intent_recognizer
    if not ir.cooldown_enabled:
        return None
    return cooldown_until(
        conn,
        kind,
        now=now,
        window_days=ir.cooldown_window_days,
        dismiss_threshold=ir.cooldown_dismiss_threshold,
        cooldown_hours=ir.cooldown_hours,
        scope=scope,
    )


def is_suppressed(
    conn: sqlite3.Connection,
    kind: str,
    *,
    scope: str | None = None,
    cfg: Config | None = None,
    now: datetime | None = None,
) -> bool:
    """Boolean form of :func:`suppression_for` (back-compat for existing tests).

    When ``scope`` is omitted the cooldown is computed globally by-kind (legacy
    behavior); the sink passes the intent's scope so production is (kind, scope).
    """
    cfg = cfg if cfg is not None else config_mod.load()
    ir = cfg.intent_recognizer
    if not ir.cooldown_enabled:
        return False
    return kind_in_cooldown(
        conn,
        kind,
        now=now,
        window_days=ir.cooldown_window_days,
        dismiss_threshold=ir.cooldown_dismiss_threshold,
        cooldown_hours=ir.cooldown_hours,
        scope=scope,
    )

"""R4 schema-level feedback loop — user accept/dismiss 回灌 schema confidence.

设计哲学第七条（``agent-docs/design-philosophy-intent.md``）：「拒绝是金矿」——
用户在 HUD 上的 dismiss/accept 是免费、高保真的监督信号。意图级负反馈已经存在
（``recognizer._dismissed_prior``，按 kind 聚合的 7 天负先验），本模块补上更慢的
一层：当某个 ``schema-*.md`` 先验反复参与了被 dismiss 的识别，它的 confidence 被
确定性地拖低，跌穿 stable 阈值后翻成 ``forming`` —— 从而自动退出
``schema_prior.active_schema_inferences`` 的 stable-only 注入闸，从根上停止注入
错误惯性。consumed 则温和回升，可把一个 forming schema 重新抬回 stable。

确定性最小版本（不引入在线学习框架）：

- **归因**：粗粒度出处线。识别那一轮注入了哪些 schema（``Intent.schema_sources``，
  由 recognizer 记录），反馈就回灌到哪些 schema —— 诚实记「当时在场」，不假装知道
  单条因果。无 sources 的 intent 零行为变化。
- **幂等**：仅在 status 真正变迁时施加（open→dismissed / open→consumed）。HUD
  重复点击写同值 → 旧值==新值 → no-op，绝不双重衰减。
- **落地缝**：confidence 骑在 schema 条目的 heading tag 上，所以每次变更通过
  schema miner 同款写口 ``store.entries.supersede_entry`` 原地超越（reason 写
  ``intent feedback: dismissed/consumed``），正文不变、tags 更新。**前向兼容**：
  evomem SSOT 切换设计稿（``2026-06-10-evomem-ssot-switch-design.md`` §5）会把
  ``schema_miner_stage._persist_schema`` 的这条缝整体重定向到 evomem engine——
  本模块走同一条缝，迁移时一起被收编，不直接拼 SQL、不绕过写口。
  （PR-6b 已兑现：``write_authority="evomem"`` 时本站点的确定性原地 supersede 经
  ``store/entries.py`` 的 choke-point dispatch 走 evomem engine 落 evo_nodes；
  逐站输出等价由 ``tests/test_evomem/test_inversion_stations.py`` 钉死。）
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass

from .. import config as config_mod
from ..config import Config
from ..logger import get
from ..store import entries as entries_mod
from ..store import files as files_mod
from ..writer import schema_miner_stage as stage
from . import schema_prior

logger = get("persome.intent.schema_feedback")

# Confidence deltas — 非对称设计的温和版本：罚（dismiss −0.05）> 奖（consume
# +0.03）。这是设计宪法「弹错=复利损失 > 漏判=有限损失」在数值上的体现；但真正的
# 非对称在 0.6 阈值穿越上 —— 单次 dismiss 只温和衰减，绝不立刻杀死一个多源证据
# 支撑的 schema（一次误判≠惯性翻转），要累计净 dismiss 才把 stable 拖成 forming、
# 退出先验注入。回升步长更小，意味着重新赢回 stable 地位比失去它更难。
DISMISS_DELTA = -0.05
CONSUME_DELTA = +0.03

# stable↔forming 翻转阈值 — 与 miner 首次裁定共用同一常量
# (``schema_miner_stage._DEFAULT_STABLE_THRESHOLD`` = 0.6)，保证 mine 与 feedback
# 两条路径对「什么算 stable」永远一致。
STABLE_THRESHOLD = stage._DEFAULT_STABLE_THRESHOLD

# Statuses that carry feedback signal. 其它状态（open/armed 回退等）零行为。
# ``completed`` (reverse-loop G5.3, spec 2026-06-26 §3.3) is the EXECUTION-completion
# backlink: an intent whose accepted follow-up actually got DONE is the strongest
# positive evidence its source schemas helped, so it flows the same positive delta as
# ``consumed``. ``failed`` is deliberately NOT here — an execution that was attempted
# and failed is not a schema misprediction (the schema correctly predicted the intent;
# the doing failed), the same 偶然熵-vs-认知熵 carve-out the ``expired`` guard makes.
_FEEDBACK_STATUSES = ("dismissed", "consumed", "completed")

# Schema 状态 tag 全集（本模块只翻转 stable/forming；行内若有别的状态 token 不动）。
_STATUS_TAGS = ("stable", "forming")

# supersede_entry 在 markdown 里给新条目尾部追加的机器注释
# (``<!-- supersedes: id; reason: ... -->``)。重读条目 body 时会带上它；重写「正文
# 不变」前剥掉，避免反复 supersede 时注释逐次累积（与 recall._clean_belief 同款）。
_SUPERSEDES_COMMENT_RE = re.compile(r"<!--\s*supersedes:.*?-->", re.DOTALL)


@dataclass
class FeedbackAdjustment:
    """One schema's confidence adjustment applied by a feedback event."""

    schema: str  # memory filename, e.g. "schema-project-x.md"
    old_confidence: float
    new_confidence: float
    old_status: str  # "stable" | "forming"
    new_status: str


def apply_intent_feedback(
    conn: sqlite3.Connection,
    *,
    intent_id: int,
    new_status: str,
    cfg: Config | None = None,
) -> list[FeedbackAdjustment]:
    """Flow one intent's status transition back onto its source schemas.

    Called by :func:`intent.store.update_intent_status` **before** the row is
    updated, so the row still holds the OLD status — the transition guard reads
    it directly:

    - ``new_status`` not in (dismissed, consumed, completed) → no-op (so ``failed``
      and any non-terminal status flow nothing — execution failure ≠ schema error);
    - row missing, or old status == new status (repeated HUD click) → no-op
      (idempotent: no double decay);
    - ``schema_sources`` empty → no-op (zero behaviour change for intents
      recognized without schema priors in context).

    Otherwise each source schema's confidence is shifted by the deterministic
    delta (clamped to [0.0, 1.0]) and its entry is superseded in place with
    refreshed tags (status flips stable↔forming when the 0.6 threshold is
    crossed; body unchanged). Returns the adjustments actually applied.
    """
    if new_status not in _FEEDBACK_STATUSES:
        return []
    cfg = cfg if cfg is not None else config_mod.load()
    if not cfg.intent_recognizer.schema_feedback_enabled:
        return []

    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status, schema_sources FROM intents WHERE id = ?", (intent_id,)
    ).fetchone()
    if row is None:
        return []
    if row["status"] == new_status:
        return []  # idempotent: same value re-written, no double decay
    if new_status == "completed" and row["status"] == "consumed":
        # G5.3 over-reward guard: ``consumed`` (accept) and ``completed`` (it got
        # DONE) are the SAME positive event class — the schema "helped". An intent
        # that goes open→consumed→completed already took +CONSUME_DELTA at the
        # consume step; rewarding AGAIN at completed would make one accepted-and-done
        # intent (+0.06) outweigh a single dismiss (−0.05), inverting the deliberate
        # 罚>奖 asymmetry above. Reward the positive lifecycle ONCE: a direct
        # open→completed (auto-run .context task never separately consumed) still
        # gets its single +delta; a consumed→completed transition adds nothing.
        return []
    if row["status"] == "expired":
        # #631 nit S (constitution point): time expiry is偶然熵, not a judgment
        # error — a row the daily harvest flipped to ``expired`` (or one that
        # aged out) being subsequently dismissed must NOT decay its source
        # schemas. Misattributing 偶然熵 as 认知熵 is the most expensive error in
        # this domain; the schema's predictive value is unchanged by the clock.
        return []

    try:
        sources = json.loads(row["schema_sources"] or "[]")
    except (ValueError, TypeError):
        sources = []
    if not sources:
        return []

    # dismiss → penalty; consumed/completed → reward (completed is G5.3's execution
    # -completion positive backlink, same gentle +delta as an accept).
    delta = DISMISS_DELTA if new_status == "dismissed" else CONSUME_DELTA
    out: list[FeedbackAdjustment] = []
    for name in sources:
        try:
            adj = _adjust_schema(conn, str(name), delta=delta, feedback=new_status)
        except Exception:  # noqa: BLE001 — one bad schema must not block the rest
            logger.warning("schema feedback adjust failed for %s", name, exc_info=True)
            continue
        if adj is not None:
            out.append(adj)
            logger.info(
                "schema feedback (%s, intent %s): %s conf %.2f→%.2f status %s→%s",
                new_status,
                intent_id,
                adj.schema,
                adj.old_confidence,
                adj.new_confidence,
                adj.old_status,
                adj.new_status,
            )
    return out


def _adjust_schema(
    conn: sqlite3.Connection, name: str, *, delta: float, feedback: str
) -> FeedbackAdjustment | None:
    """Apply one confidence delta to schema file ``name``'s live entry.

    Reads the live (non-superseded) entry from the markdown SSOT, computes the
    clamped new confidence, and — when the rendered value actually changes —
    supersedes the entry in place through the same seam the miner's re-mine
    uses (``supersede_entry``), with the body unchanged and only the
    status/confidence tags rewritten. Returns ``None`` when the schema file is
    gone, has no live entry, or the delta is a no-op at the clamp boundary
    (skipping the no-op avoids growing the evolution chain for nothing).
    """
    path = files_mod.memory_path(name)
    if not path.exists():
        return None
    parsed = files_mod.read_file(path)
    live = [e for e in parsed.entries if not e.superseded_by]
    if not live:
        return None
    entry = live[-1]

    old_conf = schema_prior._confidence_of(" ".join(entry.tags))
    new_conf = min(1.0, max(0.0, old_conf + delta))
    if f"{new_conf:.2f}" == f"{old_conf:.2f}":
        return None  # clamped at floor/ceiling — nothing to persist

    old_status = "stable" if "stable" in entry.tags else "forming"
    new_status = stage._status_for(new_conf, STABLE_THRESHOLD)
    new_tags = _rewrite_tags(entry.tags, new_status=new_status, new_conf=new_conf)
    body = _SUPERSEDES_COMMENT_RE.sub("", entry.body).strip()

    entries_mod.supersede_entry(
        conn,
        name=name,
        old_entry_id=entry.id,
        new_content=body,
        reason=f"intent feedback: {feedback}",
        tags=new_tags,
    )
    # File visibility must track the maturity flip (#631 nit Q): a stable schema
    # demoted to forming goes dormant (out of default list_memories + the prior),
    # and the inverse — same rule as the miner's re-mine (#440). Pre-fix only the
    # entry tags flipped, so file status drifted until the next re-mine.
    if new_status != old_status:
        file_status = "dormant" if new_status == "forming" else "active"
        entries_mod.set_file_status(conn, name=name, status=file_status)
    return FeedbackAdjustment(
        schema=name,
        old_confidence=old_conf,
        new_confidence=new_conf,
        old_status=old_status,
        new_status=new_status,
    )


def _rewrite_tags(tags: list[str], *, new_status: str, new_conf: float) -> list[str]:
    """Return ``tags`` with the status token and ``confidence:<float>`` replaced.

    Order-preserving and additive-safe: any other tag (``schema``, a cross-domain
    marker, provenance tags…) passes through untouched; a non-float
    ``confidence:`` token (the meta-cognition ``confidence:low`` level) is NOT
    mistaken for the schema confidence. Missing tokens are appended so a legacy
    entry without them still ends up well-formed.
    """
    conf_tag = f"confidence:{new_conf:.2f}"
    out: list[str] = []
    replaced_status = replaced_conf = False
    for t in tags:
        if t in _STATUS_TAGS:
            if not replaced_status:
                out.append(new_status)
                replaced_status = True
            continue  # drop duplicate status tokens
        if t.startswith("confidence:") and _is_float(t.split(":", 1)[1]):
            if not replaced_conf:
                out.append(conf_tag)
                replaced_conf = True
            continue
        out.append(t)
    if not replaced_status:
        out.append(new_status)
    if not replaced_conf:
        out.append(conf_tag)
    return out


def _is_float(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True

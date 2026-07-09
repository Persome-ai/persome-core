"""Feedback-loop report — turns the real accept/dismiss/ignore/completed/failed
telemetry (``context-feedback.jsonl``) into the accept↔importance correlation, the
usefulness gap, and the gate funnel: the numbers that stay UNDECIDABLE until enough
real feedback accrues.

HONEST about sparsity: when a bucket has too few samples it reports UNDECIDABLE
rather than a fake ratio (don't manufacture signal). The accrual line makes the
distance-to-decidable visible so a single user knows when the report is worth
reading. This is the L3 data-pipeline entry the methodology
(``agent-docs/data-driven-iteration.md``) calls for.

The JSONL schema is the contract with the app's ``ContextFeedbackLog`` (Swift) —
keys ``t/outcome/intentID/kind/importance/urgency/confidence/headline/taskTitle/
taskPrompt``; the smoke test ``tests/test_feedback_report.py`` pins it so the
cross-language loop can't silently break.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..store import outcomes as outcomes_store

# Goal-1 acceptance bar: high-importance accept-rate must be ≥ 2× low-importance.
CORRELATION_TARGET = 2.0
# Minimum samples per bucket before a ratio is trustworthy (else UNDECIDABLE).
MIN_BUCKET_N = 5
IMPORTANCE_SPLIT = 0.6  # high ≥ 0.6 (the INU importance floor), else low

ACCEPTED = {"accepted"}
# "engaged" = the user acted on it (accepted). dismissed/ignored = did not.
DECLINED = {"dismissed", "ignored"}
# Every outcome the app's ContextFeedbackLog can write (the display order). Keep in
# lockstep with the Swift `Outcome` enum — `failed` was missing from the old report.
ALL_OUTCOMES = ("accepted", "dismissed", "ignored", "completed", "failed", "manual_baseline")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Tolerant JSONL load (read-only): skip blank / malformed lines."""
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def accept_by_importance(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Accept-rate in the high- vs low-importance bucket, over surfaced proposals
    (accepted/dismissed/ignored — the verdicts on a push). completed/failed/
    manual_baseline are not 'a verdict on a surfaced proposal' so they're excluded."""
    buckets = {"high": {"accepted": 0, "declined": 0}, "low": {"accepted": 0, "declined": 0}}
    for e in events:
        oc = e.get("outcome")
        if oc not in ACCEPTED and oc not in DECLINED:
            continue
        imp = float(e.get("importance", 0) or 0)
        b = "high" if imp >= IMPORTANCE_SPLIT else "low"
        buckets[b]["accepted" if oc in ACCEPTED else "declined"] += 1

    def rate(b: str) -> tuple[float | None, int]:
        n = buckets[b]["accepted"] + buckets[b]["declined"]
        return (buckets[b]["accepted"] / n if n else None), n

    hi_rate, hi_n = rate("high")
    lo_rate, lo_n = rate("low")
    result: dict[str, Any] = {
        "high": {"rate": hi_rate, "n": hi_n},
        "low": {"rate": lo_rate, "n": lo_n},
    }
    if hi_n < MIN_BUCKET_N or lo_n < MIN_BUCKET_N:
        result["verdict"] = "UNDECIDABLE"
        result["reason"] = (
            f"样本不足 (high n={hi_n}, low n={lo_n}; 每桶需 ≥{MIN_BUCKET_N})"
            " — 继续使用以积累真实反馈"
        )
    elif lo_rate in (None, 0):
        result["ratio"] = None
        result["verdict"] = "high-importance accepts, low-importance never does (ratio = ∞)"
    else:
        ratio = hi_rate / lo_rate  # type: ignore[operator] — guarded non-None/non-zero above
        result["ratio"] = round(ratio, 2)
        result["verdict"] = (
            f"PASS (ratio {ratio:.2f} ≥ {CORRELATION_TARGET})"
            if ratio >= CORRELATION_TARGET
            else f"BELOW TARGET (ratio {ratio:.2f} < {CORRELATION_TARGET})"
        )
    return result


def accrual_status(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Distance-to-decidable — the visibility line so a user knows WHEN the
    correlation becomes readable: per-bucket surfaced-proposal counts vs the
    ≥MIN_BUCKET_N floor, and how many more each bucket needs."""
    hi = lo = 0
    for e in events:
        oc = e.get("outcome")
        if oc not in ACCEPTED and oc not in DECLINED:
            continue
        if float(e.get("importance", 0) or 0) >= IMPORTANCE_SPLIT:
            hi += 1
        else:
            lo += 1
    return {
        "high_n": hi,
        "low_n": lo,
        "need_high": max(0, MIN_BUCKET_N - hi),
        "need_low": max(0, MIN_BUCKET_N - lo),
        "decidable": hi >= MIN_BUCKET_N and lo >= MIN_BUCKET_N,
    }


CONF_BUCKET_WIDTH = 0.1


def confidence_calibration(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Is the recognizer's confidence CALIBRATED? Bucket surfaced verdicts
    (accepted/dismissed/ignored) by confidence into 0.1-wide bins → accept-rate per
    bin. A calibrated recognizer accepts MORE as confidence rises (accept-rate
    monotone non-decreasing across the bins that have enough samples); if it doesn't,
    the ≥0.7 gate is sorting on a number that doesn't track real acceptance and should
    be re-calibrated (A2). NOTE: the sentinel only surfaces confidence ≥0.7, so the
    <0.7 bins stay empty until active-learning (A3) feeds them — this calibrates the
    *surfaced band*, NOT the 0.7 floor itself (that question needs A3 data)."""
    bins: dict[int, dict[str, int]] = {i: {"accepted": 0, "declined": 0} for i in range(10)}
    for e in events:
        oc = e.get("outcome")
        if oc not in ACCEPTED and oc not in DECLINED:
            continue
        conf = float(e.get("confidence", 0) or 0)
        # +1e-9 so an exact decile (0.7/0.1 == 6.999…9 in float) lands in its own bin.
        idx = min(9, max(0, int(conf / CONF_BUCKET_WIDTH + 1e-9)))
        bins[idx]["accepted" if oc in ACCEPTED else "declined"] += 1

    rows: list[dict[str, Any]] = []
    decidable_rates: list[float] = []
    for i in range(10):
        acc = bins[i]["accepted"]
        n = acc + bins[i]["declined"]
        if not n:
            continue  # only surface bins that actually have samples
        rate = acc / n
        dec = n >= MIN_BUCKET_N
        rows.append(
            {
                "range": f"{i / 10:.1f}-{(i + 1) / 10:.1f}",
                "n": n,
                "accepted": acc,
                "rate": round(rate, 3),
                "decidable": dec,
            }
        )
        if dec:
            decidable_rates.append(rate)

    result: dict[str, Any] = {"buckets": rows}
    if len(decidable_rates) < 2:
        result["verdict"] = "UNDECIDABLE"
        result["reason"] = (
            f"可判定的置信桶 <2 (每桶需 ≥{MIN_BUCKET_N} 样本) — 继续积累；"
            "哨兵只 surface ≥0.7,<0.7 段要靠 A3 主动学习才有数据"
        )
    else:
        monotone = all(
            decidable_rates[i] <= decidable_rates[i + 1] + 1e-9
            for i in range(len(decidable_rates) - 1)
        )
        result["verdict"] = (
            "CALIBRATED (accept 率随置信单调上升)"
            if monotone
            else "MISCALIBRATED (accept 率不随置信上升 — 该重标定 ≥0.7 阈值)"
        )
    return result


def per_kind_accept(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Accept-rate per intent kind among surfaced verdicts — which kinds the user
    actually values, the signal for per-kind surface thresholds (A2). UNDECIDABLE per
    kind until ≥MIN_BUCKET_N (so a kind seen twice can't masquerade as 'never wanted')."""
    by_kind: dict[str, dict[str, int]] = {}
    for e in events:
        oc = e.get("outcome")
        if oc not in ACCEPTED and oc not in DECLINED:
            continue
        kind = str(e.get("kind") or "?")
        d = by_kind.setdefault(kind, {"accepted": 0, "declined": 0})
        d["accepted" if oc in ACCEPTED else "declined"] += 1

    kinds: dict[str, Any] = {}
    any_decidable = False
    for kind, d in sorted(by_kind.items()):
        n = d["accepted"] + d["declined"]
        dec = n >= MIN_BUCKET_N
        any_decidable = any_decidable or dec
        kinds[kind] = {
            "n": n,
            "accepted": d["accepted"],
            "rate": round(d["accepted"] / n, 3) if n else None,
            "decidable": dec,
        }
    return {
        "kinds": kinds,
        "decidable": any_decidable,
        "reason": None if any_decidable else f"每 kind 需 ≥{MIN_BUCKET_N} 样本才可判定",
    }


def usefulness(events: list[dict[str, Any]]) -> dict[str, Any]:
    """The 'looked important' vs 'actually useful' gap: of accepted proposals, how
    many finished (completed) vs failed."""
    accepted = sum(1 for e in events if e.get("outcome") == "accepted")
    completed = sum(1 for e in events if e.get("outcome") == "completed")
    failed = sum(1 for e in events if e.get("outcome") == "failed")
    return {
        "accepted": accepted,
        "completed": completed,
        "failed": failed,
        "completion_rate": round(completed / accepted, 3) if accepted else None,
    }


def gate_funnel(decisions: list[dict[str, Any]]) -> Counter:
    """Where intents are dropped, from the sentinel decision log (the L3 funnel)."""
    return Counter(d.get("gate", "?") for d in decisions)


_FOLLOWUP_LOOKBACK_DAYS = 7


def followup_success(
    dirpath: Path, *, lookback_days: int = _FOLLOWUP_LOOKBACK_DAYS, min_samples: int = MIN_BUCKET_N
) -> dict[str, Any]:
    """Per-kind execution success rate from the ``outcomes`` table (reverse-loop G4).

    The app writes one content-free ``outcomes`` row when a proactive follow-up /
    supervised run finishes (did the accepted thing actually land). This reads
    ``<dir>/../index.db`` and surfaces the per-kind success rate for kinds with
    **≥``min_samples``** rows — gated exactly like the accept/calibration buckets:
    a data-starved kind is UNDECIDABLE, never a fake rate.

    Read-only + fail-open: a missing DB / missing table / any sqlite error → empty
    (``decidable=False``), never raises — so ``feedback-report`` works on a fresh
    install with zero outcomes.
    """
    db = dirpath.parent / "index.db"
    if not db.exists():
        return {"decidable": False, "rows": [], "reason": "无 index.db（outcomes 尚未落库）"}
    since = (datetime.now().astimezone() - timedelta(days=lookback_days)).isoformat(
        timespec="seconds"
    )
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
        try:
            rows = outcomes_store.kind_success_rate(conn, since=since, min_samples=min_samples)
        finally:
            conn.close()
    except sqlite3.Error:  # pragma: no cover - depends on FS/DB state
        return {"decidable": False, "rows": [], "reason": "outcomes 读取失败（fail-open）"}
    return {
        "decidable": bool(rows),
        "lookback_days": lookback_days,
        "min_samples": min_samples,
        "rows": [
            {"kind": k, "n": n, "successes": s, "rate": round(r, 3)} for (k, n, s, r) in rows
        ],
    }


def build_report(dirpath: Path) -> dict[str, Any]:
    """Structured report over ``<dir>/context-feedback.jsonl`` + ``context-sentinel.jsonl``."""
    fb = load_jsonl(dirpath / "context-feedback.jsonl")
    dec = load_jsonl(dirpath / "context-sentinel.jsonl")
    return {
        "n_feedback": len(fb),
        "n_decisions": len(dec),
        "outcomes": dict(Counter(e.get("outcome") for e in fb)),
        "accrual": accrual_status(fb),
        "accept_by_importance": accept_by_importance(fb),
        "confidence_calibration": confidence_calibration(fb),
        "per_kind_accept": per_kind_accept(fb),
        "usefulness": usefulness(fb),
        "followup_success": followup_success(dirpath),
        "gate_funnel": dict(gate_funnel(dec)),
    }


def render_text(dirpath: Path) -> str:
    """Human-readable report for ``persome feedback-report``."""
    fb = load_jsonl(dirpath / "context-feedback.jsonl")
    dec = load_jsonl(dirpath / "context-sentinel.jsonl")
    out = [
        "=" * 64,
        "意图识别真实反馈回路 — 报告",
        "=" * 64,
        f"feedback events: {len(fb)}   sentinel decisions: {len(dec)}",
        f"(读自 {dirpath}/context-feedback.jsonl)",
        "",
    ]
    oc = Counter(e.get("outcome") for e in fb)
    out.append("结果分布 (outcomes):")
    for k in ALL_OUTCOMES:
        out.append(f"  {k:16} {oc.get(k, 0)}")
    out.append("")

    acc = accrual_status(fb)
    if acc["decidable"]:
        out.append(
            f"累计进度: 高/低重要桶各 {acc['high_n']}/{acc['low_n']} (≥{MIN_BUCKET_N}) — 可判定 ✓"
        )
    else:
        out.append(
            f"累计进度: 高桶 {acc['high_n']}/{MIN_BUCKET_N} (还差 {acc['need_high']}), "
            f"低桶 {acc['low_n']}/{MIN_BUCKET_N} (还差 {acc['need_low']}) — 继续使用以攒够样本"
        )
    out.append("")

    out.append("Goal-1 accept↔importance 相关性 (高重要桶 accept 率 ÷ 低重要桶):")
    corr = accept_by_importance(fb)
    hi, lo = corr["high"], corr["low"]
    hi_r = f"{hi['rate']:.2f}" if hi["rate"] is not None else "—"
    lo_r = f"{lo['rate']:.2f}" if lo["rate"] is not None else "—"
    out.append(f"  high-importance (≥{IMPORTANCE_SPLIT}): accept-rate {hi_r} (n={hi['n']})")
    out.append(f"  low-importance  (<{IMPORTANCE_SPLIT}): accept-rate {lo_r} (n={lo['n']})")
    out.append(f"  → {corr.get('verdict')}")
    if corr.get("reason"):
        out.append(f"    {corr['reason']}")
    out.append("")

    cal = confidence_calibration(fb)
    out.append("A1 置信度校准 (surfaced 裁决按 confidence 分桶的 accept 率):")
    if cal["buckets"]:
        for row in cal["buckets"]:
            flag = "" if row["decidable"] else f"  (n<{MIN_BUCKET_N}, 未达判定)"
            out.append(
                f"  conf {row['range']}: accept-rate {row['rate']:.2f} "
                f"(accepted {row['accepted']}/{row['n']}){flag}"
            )
    else:
        out.append("  (无 surfaced 裁决样本)")
    out.append(f"  → {cal.get('verdict')}")
    if cal.get("reason"):
        out.append(f"    {cal['reason']}")
    out.append("")

    pk = per_kind_accept(fb)
    out.append("A1 每 kind accept 率 (→ A2 的 per-kind 阈值信号):")
    if pk["kinds"]:
        for kind, row in pk["kinds"].items():
            rate_s = f"{row['rate']:.2f}" if row["rate"] is not None else "—"
            flag = "" if row["decidable"] else f"  (n<{MIN_BUCKET_N}, 未达判定)"
            out.append(
                f"  {kind:16} accept-rate {rate_s} (accepted {row['accepted']}/{row['n']}){flag}"
            )
    else:
        out.append("  (无 surfaced 裁决样本)")
    if pk.get("reason"):
        out.append(f"    {pk['reason']}")
    out.append("")

    use = usefulness(fb)
    out.append("'看着重要' vs '真有用' (accepted → completed):")
    rate_s = use["completion_rate"] if use["completion_rate"] is not None else "—"
    out.append(
        f"  accepted={use['accepted']}  completed={use['completed']}  "
        f"failed={use['failed']}  completion-rate={rate_s}"
    )
    out.append("")

    fs = followup_success(dirpath)
    out.append(
        f"G4 每 kind 执行成功率 (follow-up/supervised 真落地, 近 {_FOLLOWUP_LOOKBACK_DAYS} 天):"
    )
    if fs["decidable"]:
        for row in fs["rows"]:
            out.append(
                f"  {row['kind']:16} success-rate {row['rate']:.2f} "
                f"(成功 {row['successes']}/{row['n']})"
            )
        out.append(f"  → 每 kind ≥{MIN_BUCKET_N} 样本方判定; 仅供 A2 成功率先验, 不降识别门槛")
    else:
        reason = fs.get("reason") or f"暂无任一 kind 达 ≥{MIN_BUCKET_N} 样本"
        out.append(f"  ({reason}) — UNDECIDABLE, 继续使用以攒够 outcomes 样本")
    out.append("")

    if dec:
        out.append("gate 漏斗 (意图在哪一道被挡掉):")
        for gate, n in gate_funnel(dec).most_common():
            out.append(f"  {gate:22} {n}")
    else:
        out.append("gate 漏斗: (无 sentinel decision log)")
    out.append("=" * 64)
    return "\n".join(out)

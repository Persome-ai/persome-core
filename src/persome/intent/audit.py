"""Intent precision · lifecycle audit oracle (read-only, deterministic, zero-LLM).

Turns the recognizer's `intents` table from a black box into measured aggregates,
so precision/lifecycle tuning is data-driven instead of anecdote-driven (the
harness-loop / data-driven-iteration methodology: build the measurable oracle
first, then let the data pick the lever).

Surfaced via `persome intent-audit` (+ `--json-out`). **Read-only** — never writes
the DB, never changes recognition; **fail-open** — a missing/locked/corrupt DB
yields an empty report rather than raising; **PII-free** — only counts / rates /
buckets / cluster sizes leave this module, never any body text.

The aggregates (each a pure function over the row list):
  - status distribution + kind×status crosstab
  - stale-open: open rows by age bucket + the ungrounded rate (valid_until NULL →
    no clock anchor → never time-expires)
  - duplicate clusters: same normalized body across ≥2 OPEN rows (the "×3" heat)
  - re-mention: a body that is BOTH handled (consumed/dismissed/resolved) AND
    later open again (already-handled, yet a re-mention surfaced a new card)
  - accept:dismiss, splitting USER dismissals (dismissed_at set) from engine
    HARVEST dismissals (dismissed_at NULL) — the over-fire signal
  - fold gap: same body across an open row WITH resolved_at and one WITHOUT (the
    grounded/content-fold no-man's-land where two same-body opens can't fold)
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..logger import get
from ..store import intent_fold_ticks
from . import sink

logger = get("persome.intent.audit")

_AGE_BUCKETS = ("<1d", "1-7d", "7-30d", ">30d")
_HANDLED = ("consumed", "dismissed", "resolved")  # "user/agent dealt with it" terminals

# App rich-proposal surface bar — mirrors ContextSentinel.richProposalMinConfidence
# (Sources/Persome/Engine/Context/ContextSentinel.swift). An intent ≥ this DID surface
# to the user; below it never surfaced. Hard-coded (not imported across the
# Swift/Python boundary); keep in lockstep if the Swift constant moves.
_SURFACE_BAR = 0.7


@dataclass
class AuditReport:
    total: int = 0
    status_dist: dict[str, int] = field(default_factory=dict)
    kind_status: dict[str, dict[str, int]] = field(default_factory=dict)
    open_total: int = 0
    open_by_age: dict[str, int] = field(default_factory=dict)
    open_ungrounded: int = 0  # valid_until NULL → never time-expires
    open_ungrounded_rate: float = 0.0
    dup_clusters: int = 0  # normalized bodies with ≥2 OPEN rows
    dup_max_cluster: int = 0
    dup_cluster_sizes: dict[str, int] = field(default_factory=dict)  # "size" → count
    re_mention: int = 0  # handled-then-open-again bodies
    consumed: int = 0
    dismissed_user: int = 0  # dismissed_at set → real user reject
    dismissed_harvest: int = 0  # dismissed_at NULL → engine TTL reap
    accept_rate: float | None = None  # consumed / (consumed + user-dismissed)
    # Surfacing soft-nag (lifecycle, the analog of firing precision): an intent
    # ≥ the surface bar that the user NEITHER consumed NOR explicitly dismissed —
    # it surfaced and was ignored until the engine reaped it (status `expired`, or
    # `dismissed` with NULL dismissed_at = TTL harvest). These are INVISIBLE to
    # accept_rate (it counts only explicit consumed/dismissed) yet are real soft
    # over-fire that took a surface slot. true_surface_accept_rate folds them in
    # as implicit rejections: surfaced-consumed / (surfaced-consumed +
    # surfaced-user-dismissed + soft-nag) — the honest surfacing denominator.
    surface_bar: float = _SURFACE_BAR
    surfaced_consumed: int = 0
    surfaced_dismissed_user: int = 0
    softnag: int = 0
    softnag_by_kind: dict[str, int] = field(default_factory=dict)
    true_surface_accept_rate: float | None = None
    fold_gap: int = 0  # same body across a grounded + an ungrounded OPEN row
    # Per-kind threshold separability: for each kind with enough verdicts, can a
    # confidence / importance×urgency threshold cleanly separate accepted from
    # rejected? {kind: {verdict: "SEPARABLE|NOT_SEPARABLE|INSUFFICIENT", score, j,
    # threshold, kept_accept, dropped_reject, n_accept, n_reject}} — n_accept/
    # n_reject are the WINNING score's eval-sample sizes (same source as
    # kept_accept/dropped_reject; the iu subset when iu wins), not the full count.
    kind_separability: dict[str, dict[str, Any]] = field(default_factory=dict)


def build_report(db_path: str, *, now_iso: str | None = None) -> dict[str, Any]:
    """Compute the audit aggregates for the intents table at ``db_path``.
    Read-only + fail-open: returns an empty report dict if the DB is absent /
    locked / unreadable. ``now_iso`` defaults to now() (injectable for tests)."""
    now = now_iso or datetime.now().astimezone().isoformat()
    rows = _read_rows(db_path)
    d = _to_dict(_audit(rows, now=now))
    # G5.1: actual fold counts per kind (from intent_fold_ticks) — "how often the
    # SAME thing gets re-recognized", the content-fold tuning signal.
    d["fold_heat"] = [
        {"kind": k, "folds": n, "distinct_targets": t}
        for (k, n, t) in _read_fold_heat(db_path, now)
    ]
    return d


def render_text(db_path: str, *, now_iso: str | None = None) -> str:
    now = now_iso or datetime.now().astimezone().isoformat()
    base = _render(_audit(_read_rows(db_path), now=now))
    return base + _render_fold_heat(db_path, now)


# --- G5.1 fold heat (intent_fold_ticks, read-only) --------------------------

_FOLD_HEAT_WINDOW = timedelta(days=14)


def _read_fold_heat(db_path: str, now: str) -> list[tuple[str, int, int]]:
    """Per-kind fold counts over the last 14 days. Read-only + fail-open ([])."""
    try:
        since = (datetime.fromisoformat(now) - _FOLD_HEAT_WINDOW).isoformat()
    except (TypeError, ValueError):
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:  # pragma: no cover - depends on FS state
        return []
    try:
        return intent_fold_ticks.fold_heat(conn, since=since, limit=20)
    finally:
        conn.close()


def _render_fold_heat(db_path: str, now: str) -> str:
    heat = _read_fold_heat(db_path, now)
    if not heat:
        return ""
    lines = ["", "fold heat (G5.1 — 同一件事每会话重复识别次数, 近 14 天):"]
    for kind, folds, targets in heat:
        avg = folds / targets if targets else float(folds)
        lines.append(f"  {kind:16} folds={folds} onto {targets} distinct (≈{avg:.1f}×/fact)")
    return "\n".join(lines)


# --- DB read (read-only, fail-open) -----------------------------------------


def _read_rows(db_path: str) -> list[dict[str, Any]]:
    """Read the columns the audit needs, read-only. Any error → []."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error as exc:  # pragma: no cover - depends on FS state
        logger.debug("intent-audit: cannot open %s read-only: %s", db_path, exc)
        return []
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT id, status, ts, valid_until, resolved_at, dismissed_at, kind, confidence, payload "
            "FROM intents"
        )
        out: list[dict[str, Any]] = []
        for r in cur:
            try:
                payload = json.loads(r["payload"] or "{}")
            except (TypeError, ValueError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            out.append(
                {
                    "status": r["status"] or "",
                    "ts": r["ts"] or "",
                    "valid_until": r["valid_until"],
                    "resolved_at": r["resolved_at"],
                    "dismissed_at": r["dismissed_at"],
                    "kind": r["kind"] or "",
                    "body": sink._content_body(payload),
                    "confidence": _num(r["confidence"]),
                    "iu": _iu_or_none(payload),
                }
            )
        return out
    except sqlite3.Error as exc:  # pragma: no cover - missing table / corrupt
        logger.debug("intent-audit: read failed: %s", exc)
        return []
    finally:
        conn.close()


# --- pure aggregation --------------------------------------------------------


def _audit(rows: list[dict[str, Any]], *, now: str) -> AuditReport:
    rep = AuditReport(total=len(rows))
    if not rows:
        return rep
    rep.status_dist = dict(sorted(Counter(r["status"] for r in rows).items()))
    rep.kind_status = _kind_status(rows)

    opens = [r for r in rows if r["status"] == "open"]
    rep.open_total = len(opens)
    rep.open_by_age = _age_histogram(opens, now=now)
    rep.open_ungrounded = sum(1 for r in opens if not r["valid_until"])
    rep.open_ungrounded_rate = round(rep.open_ungrounded / len(opens), 3) if opens else 0.0

    rep.dup_clusters, rep.dup_max_cluster, rep.dup_cluster_sizes = _dup_clusters(opens)
    rep.re_mention = _re_mention(rows)
    rep.fold_gap = _fold_gap(opens)
    rep.kind_separability = _kind_separability(rows)

    rep.consumed = rep.status_dist.get("consumed", 0)
    rep.dismissed_user = sum(1 for r in rows if r["status"] == "dismissed" and r["dismissed_at"])
    rep.dismissed_harvest = sum(
        1 for r in rows if r["status"] == "dismissed" and not r["dismissed_at"]
    )
    # Accept rate is consumed / (consumed + USER-dismissed) only — harvest
    # dismissals (engine TTL, no user verdict) and `resolved` (evidence auto-close,
    # also no user verdict) are deliberately excluded so the rate reflects real
    # user accept-vs-reject. None when there's no user verdict yet (UNDECIDABLE).
    denom = rep.consumed + rep.dismissed_user
    rep.accept_rate = round(rep.consumed / denom, 3) if denom else None

    # Surfacing soft-nag: restrict to rows that DID surface (≥ surface bar), then
    # split by terminal. consumed = accepted; dismissed+dismissed_at = explicit
    # reject; expired OR harvest-dismissed (NULL dismissed_at) = surfaced-then-
    # ignored = soft-nag (the over-fire accept_rate hides).
    surfaced = [r for r in rows if r["confidence"] >= _SURFACE_BAR]
    rep.surfaced_consumed = sum(1 for r in surfaced if r["status"] == "consumed")
    rep.surfaced_dismissed_user = sum(
        1 for r in surfaced if r["status"] == "dismissed" and r["dismissed_at"]
    )
    softnag_rows = [
        r
        for r in surfaced
        if r["status"] == "expired" or (r["status"] == "dismissed" and not r["dismissed_at"])
    ]
    rep.softnag = len(softnag_rows)
    rep.softnag_by_kind = dict(
        sorted(Counter(r["kind"] for r in softnag_rows).items(), key=lambda kv: (-kv[1], kv[0]))
    )
    sdenom = rep.surfaced_consumed + rep.surfaced_dismissed_user + rep.softnag
    rep.true_surface_accept_rate = round(rep.surfaced_consumed / sdenom, 3) if sdenom else None
    return rep


def _kind_status(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        out.setdefault(r["kind"], {})
        out[r["kind"]][r["status"]] = out[r["kind"]].get(r["status"], 0) + 1
    return {k: dict(sorted(v.items())) for k, v in sorted(out.items())}


def _age_histogram(opens: list[dict[str, Any]], *, now: str) -> dict[str, int]:
    hist = dict.fromkeys(_AGE_BUCKETS, 0)
    n = _parse(now)
    for r in opens:
        t = _parse(r["ts"])
        if n is None or t is None:
            continue
        days = (n - t).total_seconds() / 86400.0
        if days < 1:
            hist["<1d"] += 1
        elif days < 7:
            hist["1-7d"] += 1
        elif days < 30:
            hist["7-30d"] += 1
        else:
            hist[">30d"] += 1
    return hist


def _dup_clusters(opens: list[dict[str, Any]]) -> tuple[int, int, dict[str, int]]:
    bodies = Counter(r["body"] for r in opens if r["body"])
    multi = {b: c for b, c in bodies.items() if c >= 2}
    sizes = Counter(str(c) for c in multi.values())
    return len(multi), (max(multi.values()) if multi else 0), dict(sorted(sizes.items()))


def _re_mention(rows: list[dict[str, Any]]) -> int:
    """Count bodies that are BOTH handled (consumed/dismissed/resolved) AND open
    again at-or-after the handling — already dealt with, yet a re-mention surfaced
    a fresh open card. Compares PARSED datetimes (not raw ISO strings) so mixed
    offsets/precision can't misorder, and uses ``>=`` so a handle + reopen landing
    in the SAME tick (common when both transitions fall in one finalize pass, ts
    truncated to seconds) still counts — that simultaneous reopen IS the over-fire
    signal this metric exists to measure."""
    handled_latest: dict[str, datetime] = {}  # body → latest handled time
    open_latest: dict[str, datetime] = {}  # body → latest open time
    for r in rows:
        b = r["body"]
        if not b:
            continue
        t = _parse(r["ts"])
        if t is None:
            continue
        if r["status"] in _HANDLED:
            if b not in handled_latest or t > handled_latest[b]:
                handled_latest[b] = t
        elif r["status"] == "open" and (b not in open_latest or t > open_latest[b]):
            open_latest[b] = t
    return sum(
        1 for b, ots in open_latest.items() if b in handled_latest and ots >= handled_latest[b]
    )


_SEP_MIN_PER_CLASS = 3  # need ≥ this many accepted AND rejected to even attempt
_SEP_J_BAR = 0.5  # best Youden's J ≥ this → SEPARABLE (a threshold cleanly splits)
_SEP_LOW_CONF_N = 8  # accepted sample < this → low_confidence flag (J over-fits small N)
_SEP_SCORES = ("confidence", "iu")


def _kind_separability(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """For each kind, ask the data: would a per-kind surfacing threshold reduce
    over-fire WITHOUT killing accepted items? Uses ONLY past USER verdicts —
    accepted (`consumed`) vs real rejections (`dismissed` with `dismissed_at`) —
    and for each candidate score (confidence, importance×urgency) finds the
    threshold maximizing Youden's J (= sensitivity + specificity − 1). A high J
    means a clean split exists (→ a data-justified gated floor); a low J means the
    score can't separate, so the over-fire is a recognition-quality problem, NOT a
    threshold one (don't blindly tune — it would hurt recall). Pure, deterministic,
    no key. Kinds with too few verdicts return INSUFFICIENT (never a faked verdict)."""
    by_kind: dict[str, dict[str, list[float]]] = {}
    for r in rows:
        accepted = r["status"] == "consumed"
        rejected = r["status"] == "dismissed" and bool(r["dismissed_at"])  # real user reject
        if not (accepted or rejected):
            continue
        slot = by_kind.setdefault(r["kind"], {"acc": [], "rej": [], "acc_iu": [], "rej_iu": []})
        slot["acc" if accepted else "rej"].append(r["confidence"])
        # iu is only collected when BOTH importance + urgency were genuinely present
        # (r["iu"] is None otherwise). A missing key must NOT collapse to 0 — that
        # would let an asymmetric-missing-data artifact fabricate a clean split.
        if r["iu"] is not None:
            slot["acc_iu" if accepted else "rej_iu"].append(r["iu"])

    out: dict[str, dict[str, Any]] = {}
    for kind, slot in sorted(by_kind.items()):
        n_acc, n_rej = len(slot["acc"]), len(slot["rej"])
        if n_acc < _SEP_MIN_PER_CLASS or n_rej < _SEP_MIN_PER_CLASS:
            out[kind] = {"verdict": "INSUFFICIENT", "n_accept": n_acc, "n_reject": n_rej}
            continue
        best: dict[str, Any] | None = None
        for score in _SEP_SCORES:
            if score == "confidence":
                acc, rej = slot["acc"], slot["rej"]
            else:
                acc, rej = slot["acc_iu"], slot["rej_iu"]
                # Only consider the iu axis when BOTH classes have enough rows that
                # actually CARRY importance×urgency — else skip it (don't let a
                # missing-data artifact win the max-J selection).
                if len(acc) < _SEP_MIN_PER_CLASS or len(rej) < _SEP_MIN_PER_CLASS:
                    continue
            cand = _best_threshold(acc, rej)
            if best is None or cand["j"] > best["j"]:
                # n_eval_* = the sample this candidate's threshold was ACTUALLY
                # evaluated over (full verdicts for confidence; the
                # importance×urgency SUBSET for iu, which excludes rows missing
                # either key). kept_accept/dropped_reject are counted over exactly
                # this sample, so it MUST be their denominator — else an iu winner
                # renders an iu-subset numerator over a confidence-full denominator
                # (e.g. "keeps 4/10 accepted" where 6 of those 10 carry no iu and
                # were never in the iu threshold's scope). Issue #368.
                best = {**cand, "score": score, "n_eval_acc": len(acc), "n_eval_rej": len(rej)}
        assert best is not None  # confidence always evaluates (both classes ≥ min)
        out[kind] = {
            # SEPARABLE means a clean split EXISTS; `low_confidence` warns the
            # accepted sample is tiny (J over-fits) — a downstream consumer must
            # not read a small-N SEPARABLE as a green light to apply a floor. Key
            # it on the EVAL sample (the iu subset when iu won), the sample J was
            # actually fit on — not the full confidence count.
            "verdict": "SEPARABLE" if best["j"] >= _SEP_J_BAR else "NOT_SEPARABLE",
            "low_confidence": best["n_eval_acc"] < _SEP_LOW_CONF_N,
            "score": best["score"],
            "j": round(best["j"], 3),
            "threshold": round(best["threshold"], 3),
            "kept_accept": best["kept_accept"],  # accepted kept if surface ≥ threshold
            "dropped_reject": best["dropped_reject"],  # rejected suppressed below threshold
            # Denominators are SAME-SOURCE as the numerators above (the winning
            # score's eval sample), so the rendered "kept X/Y" / "drops X/Y" pairs
            # are coherent. For a confidence winner these equal the full counts
            # (n_acc/n_rej); for an iu winner they are the iu-subset sizes.
            "n_accept": best["n_eval_acc"],
            "n_reject": best["n_eval_rej"],
        }
    return out


def _best_threshold(accept: list[float], reject: list[float]) -> dict[str, Any]:
    """Sweep candidate thresholds; return the one maximizing Youden's J for the
    rule "surface iff score ≥ T". J = (accepted kept / n_accept) + (rejected
    dropped / n_reject) − 1, in [−1, 1]; ~0 = no separation, →1 = clean split."""
    n_acc, n_rej = len(accept), len(reject)
    best = {"j": -1.0, "threshold": 0.0, "kept_accept": n_acc, "dropped_reject": 0}
    for t in sorted({*accept, *reject}):
        kept_acc = sum(1 for s in accept if s >= t)
        dropped_rej = sum(1 for s in reject if s < t)
        j = (kept_acc / n_acc) + (dropped_rej / n_rej) - 1.0
        if j > best["j"]:
            best = {"j": j, "threshold": t, "kept_accept": kept_acc, "dropped_reject": dropped_rej}
    return best


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _iu_or_none(payload: dict[str, Any]) -> float | None:
    """importance×urgency, or None when EITHER key is absent/non-numeric. None (not
    0) so a missing-data row is EXCLUDED from the iu separability axis rather than
    fabricating a low-score point that could create a spurious clean split."""
    imp, urg = payload.get("importance"), payload.get("urgency")
    if imp is None or urg is None:
        return None
    try:
        return float(imp) * float(urg)
    except (TypeError, ValueError):
        return None


def _fold_gap(opens: list[dict[str, Any]]) -> int:
    """Bodies with ≥1 OPEN row WITH resolved_at AND ≥1 OPEN row WITHOUT — the
    grounded/content-fold no-man's-land where two same-body opens won't fold."""
    grounded: set[str] = set()
    ungrounded: set[str] = set()
    for r in opens:
        if not r["body"]:
            continue
        (grounded if r["resolved_at"] else ungrounded).add(r["body"])
    return len(grounded & ungrounded)


def _parse(iso: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(iso)
        return d if d.tzinfo else d.astimezone()
    except (ValueError, TypeError):
        return None


# --- rendering ---------------------------------------------------------------


def _to_dict(rep: AuditReport) -> dict[str, Any]:
    return {
        "total": rep.total,
        "status_dist": rep.status_dist,
        "kind_status": rep.kind_status,
        "open": {
            "total": rep.open_total,
            "by_age": rep.open_by_age,
            "ungrounded": rep.open_ungrounded,
            "ungrounded_rate": rep.open_ungrounded_rate,
        },
        "duplicates": {
            "clusters": rep.dup_clusters,
            "max_cluster": rep.dup_max_cluster,
            "cluster_sizes": rep.dup_cluster_sizes,
        },
        "re_mention": rep.re_mention,
        "accept_dismiss": {
            "consumed": rep.consumed,
            "dismissed_user": rep.dismissed_user,
            "dismissed_harvest": rep.dismissed_harvest,
            "accept_rate": rep.accept_rate,
        },
        "surfacing_softnag": {
            "surface_bar": rep.surface_bar,
            "surfaced_consumed": rep.surfaced_consumed,
            "surfaced_dismissed_user": rep.surfaced_dismissed_user,
            "softnag": rep.softnag,
            "softnag_by_kind": rep.softnag_by_kind,
            "true_surface_accept_rate": rep.true_surface_accept_rate,
        },
        "fold_gap": rep.fold_gap,
        "kind_separability": rep.kind_separability,
    }


def _render(rep: AuditReport) -> str:
    if rep.total == 0:
        return "intent-audit: no intents found (empty/absent DB)."
    lines = [
        "── intent precision · lifecycle audit ──",
        f"total intents: {rep.total}",
        f"status: {_fmt(rep.status_dist)}",
        "",
        f"OPEN ({rep.open_total}): age {_fmt(rep.open_by_age)}",
        f"  ungrounded (no clock → never time-expires): {rep.open_ungrounded}"
        f" ({rep.open_ungrounded_rate:.0%})",
        f"DUPLICATES among open: {rep.dup_clusters} clusters"
        f" (max size {rep.dup_max_cluster}; sizes {_fmt(rep.dup_cluster_sizes) or '—'})",
        f"FOLD-GAP (grounded+ungrounded same body): {rep.fold_gap}",
        f"RE-MENTION (handled then open again): {rep.re_mention}",
        "",
        "ACCEPT vs DISMISS:",
        f"  consumed(accepted): {rep.consumed}",
        f"  dismissed — user(real reject): {rep.dismissed_user}"
        f" · harvest(engine TTL): {rep.dismissed_harvest}",
        "  accept rate (consumed / consumed+user-dismiss): "
        + (f"{rep.accept_rate:.0%}" if rep.accept_rate is not None else "UNDECIDABLE (no signal)"),
        "",
        f"SURFACING SOFT-NAG (≥{rep.surface_bar:g} bar — surfaced then ignored to engine-reap):",
        f"  soft-nag (surfaced→expired/harvest, no user verdict): {rep.softnag}"
        + (f"  [{_fmt(rep.softnag_by_kind)}]" if rep.softnag_by_kind else ""),
        "  true surfacing accept (surfaced-consumed / +user-dismiss +soft-nag): "
        + (
            f"{rep.true_surface_accept_rate:.0%}"
            if rep.true_surface_accept_rate is not None
            else "UNDECIDABLE (nothing surfaced)"
        )
        + (
            f"  ⚠ headline accept-rate {rep.accept_rate:.0%} hides the {rep.softnag} soft-nags"
            if rep.accept_rate is not None and rep.softnag
            else ""
        ),
        "",
        "kind × status:",
    ]
    for kind, sd in rep.kind_status.items():
        lines.append(f"  {kind}: {_fmt(sd)}")
    if rep.kind_separability:
        lines += ["", "per-kind threshold separability (is over-fire threshold-fixable?):"]
        for kind, s in rep.kind_separability.items():
            if s["verdict"] == "INSUFFICIENT":
                lines.append(
                    f"  {kind}: INSUFFICIENT (accept={s['n_accept']} reject={s['n_reject']},"
                    f" need ≥{_SEP_MIN_PER_CLASS} each)"
                )
            elif s["verdict"] == "SEPARABLE":
                warn = " ⚠️small-N(noisy)" if s.get("low_confidence") else ""
                lines.append(
                    f"  {kind}: SEPARABLE via {s['score']}≥{s['threshold']} (J={s['j']}){warn} →"
                    f" keeps {s['kept_accept']}/{s['n_accept']} accepted,"
                    f" drops {s['dropped_reject']}/{s['n_reject']} rejected"
                )
            else:
                lines.append(
                    f"  {kind}: NOT separable by score (best {s['score']} J={s['j']}) →"
                    f" recognition-quality problem, don't threshold-tune"
                )
    return "\n".join(lines)


def _fmt(d: dict[str, int]) -> str:
    return "  ".join(f"{k}={v}" for k, v in d.items())

"""Text-axis graded forgetting — 细节链 → 粗摘要 → 一行事实.

Spec ``docs/superpowers/specs/2026-07-03-text-axis-graded-forgetting-design.md``
(the text half of memory-rebuild §1.5-5; the pixel half is cleanup_buffer's
thumbnail tier). Old durable-memory entries that were NEVER read-reinforced
(``entry_retrieval_stats.retrieval_count`` — the testing-effect signal: a
retrieved memory is load-bearing and immune) are nightly distilled into a
coarser summary — precision degrades in tiers, nothing is ever binary-deleted.

Eligibility is the MECE cell table (spec §2): only **old ∧ weak ∧
unprotected** entries decay. Protections: conflicted (⚠ pending human
adjudication — never destroy evidence), non-fact prefixes (event-* / schema-*
/ intent-* have their own lifecycles), and ``decayed:2`` (the
one-line floor — coarser than one line is deletion, which §1.5-4 forbids).

The decay op is a COMPOSITION of existing choke-point verbs (spec §4 — zero
new write verbs, so PR-3 shadow dual-write, PR-6b write-authority inversion,
FTS projection and rebuild-index are all consistent for free):

1. ``append_entry(summary, tags=[…, "decayed:N", "abstracted-from:{ids}"])``
   — the existing ABSTRACT provenance vocabulary (parser/backfill/projector
   already speak it);
2. ``mark_entry_deleted`` per source — whose own docstring names it "the
   ABSTRACT source-retire landing": markdown strike (bytes stay on disk =
   the receipt), FTS retire, evo orphan-shadow.

Anti-hallucination gates (spec §5, all zero-LLM, any failure ⇒ the cluster is
kept as-is and retried another night — loss is the point, fabrication is not):
mention-subset (roster mentions in the summary ⊆ union of source mentions,
via the same ``identity.scan_mentions`` knife), shrink ceiling (a "summary"
longer than half its sources is not decay), non-empty.

Bounded: ≤ ``max_clusters_per_night`` LLM calls, oldest cluster first.
Idempotent by construction: decayed sources leave the live scan; a summary
must age past ``after_days`` again before the L1→L2 pass can touch it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import prompts
from ..config import Config
from ..evomem import identity as identity_mod
from ..logger import get
from ..store import entries as entries_mod
from . import llm as llm_mod
from .schema_miner_stage import _FACT_PREFIXES

logger = get("persome.writer")

_STAGE = "memory_decay"
_DECAYED_FLOOR_TAG = "decayed:2"  # the one-line tier — never decays further


@dataclass
class DecayCluster:
    path: str
    entry_ids: list[str]
    bodies: list[str]
    oldest_ts: str
    tier: int  # 1 = L0 details → L1 summary; 2 = L1 summary → L2 one-liner


@dataclass
class DecayRunResult:
    clusters_considered: int = 0
    clusters_decayed: int = 0
    entries_retired: int = 0
    gated: list[str] = field(default_factory=list)  # gate-name per rejected cluster


def _tag_set(tags: str | None) -> set[str]:
    return set((tags or "").split())


def _tier_of(tags: set[str]) -> int:
    """0 = plain detail entry, 1 = decayed:1 summary, 2 = one-line floor."""
    if _DECAYED_FLOOR_TAG in tags:
        return 2
    if "decayed:1" in tags:
        return 1
    return 0


def find_decay_clusters(
    conn: sqlite3.Connection,
    *,
    after_days: int,
    cluster_min: int,
    cluster_max: int,
    max_clusters: int,
    now: datetime | None = None,
) -> list[DecayCluster]:
    """The zero-LLM eligibility scan → bounded cluster list, oldest first.

    Implements the spec §2 cell table: live entries in fact files, older than
    ``after_days``, never retrieved, not conflicted, not the one-line floor.
    L0 details cluster per file (≥ cluster_min); an old weak L1 summary forms
    its own singleton tier-2 cluster.
    """
    now = now or datetime.now(UTC)
    cutoff = (now - timedelta(days=after_days)).isoformat()
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" * len(_FACT_PREFIXES))
    rows = conn.execute(
        f"""
        SELECT e.id, e.path, e.content, e.timestamp, e.tags
        FROM entries e
        LEFT JOIN entry_retrieval_stats r ON r.entry_id = e.id
        LEFT JOIN entry_metadata m ON m.entry_id = e.id
        WHERE e.prefix IN ({placeholders})
          AND e.superseded = 0
          AND e.timestamp <= ?
          AND COALESCE(r.retrieval_count, 0) = 0
          AND COALESCE(m.conflicted, 0) = 0
        ORDER BY e.path, e.timestamp
        """,
        (*_FACT_PREFIXES, cutoff),
    ).fetchall()

    details_by_file: dict[str, list[sqlite3.Row]] = {}
    singles: list[DecayCluster] = []
    for row in rows:
        if not (row["content"] or "").strip():
            continue
        tier = _tier_of(_tag_set(row["tags"]))
        if tier == 2:
            continue  # the floor — protected
        if tier == 1:
            singles.append(
                DecayCluster(
                    path=row["path"],
                    entry_ids=[row["id"]],
                    bodies=[row["content"].strip()],
                    oldest_ts=row["timestamp"],
                    tier=2,
                )
            )
        else:
            details_by_file.setdefault(row["path"], []).append(row)

    clusters: list[DecayCluster] = list(singles)
    for path, entries in details_by_file.items():
        if len(entries) < cluster_min:
            continue  # too few details to distill — noise, not compression
        chunk = entries[:cluster_max]
        clusters.append(
            DecayCluster(
                path=path,
                entry_ids=[r["id"] for r in chunk],
                bodies=[r["content"].strip() for r in chunk],
                oldest_ts=chunk[0]["timestamp"],
                tier=1,
            )
        )
    clusters.sort(key=lambda c: (c.oldest_ts, c.path))
    return clusters[:max_clusters]


def _build_llm_call(cfg: Config) -> Callable[[list[dict]], Any]:
    def _call(messages: list[dict]) -> Any:
        return llm_mod.call_llm(cfg, _STAGE, cfg.model_for(_STAGE), messages)

    return _call


def _distill(call: Callable[[list[dict]], Any], cluster: DecayCluster) -> str:
    template = prompts.load("memory_decay.md")
    numbered = "\n".join(f"{i + 1}. {b}" for i, b in enumerate(cluster.bodies))
    prompt = (
        template.replace("{mode}", "一行事实（不换行）" if cluster.tier == 2 else "一段粗摘要")
        .replace("{path}", cluster.path)
        .replace("{facts}", numbered)
    )
    resp = call([{"role": "user", "content": prompt}])
    return (resp.choices[0].message.content or "").strip()


def _passes_gates(
    cluster: DecayCluster,
    summary: str,
    *,
    roster: identity_mod.Roster,
    shrink_ceiling: float,
    line_max_chars: int,
) -> str | None:
    """Return the failing gate's name, or None when the summary is admissible."""
    if not summary:
        return "empty"
    if cluster.tier == 2 and ("\n" in summary or len(summary) > line_max_chars):
        return "not_one_line"
    total = sum(len(b) for b in cluster.bodies)
    # L1 must genuinely compress; L2 distills ONE already-short summary into a
    # line, so the honest bar there is "strictly shorter" — halving a
    # one-sentence L1 is often impossible without fabricating brevity.
    if cluster.tier == 2:
        if len(summary) >= total:
            return "no_shrink"
    elif len(summary) >= total * shrink_ceiling:
        return "no_shrink"
    allowed = set()
    for body in cluster.bodies:
        allowed.update(identity_mod.scan_mentions(body, roster))
    introduced = [m for m in identity_mod.scan_mentions(summary, roster) if m not in allowed]
    if introduced:
        return "new_mentions"
    return None


def run_memory_decay(
    cfg: Config,
    conn: sqlite3.Connection,
    *,
    llm_call: Callable[[list[dict]], Any] | None = None,
    roster: identity_mod.Roster | None = None,
    now: datetime | None = None,
) -> DecayRunResult:
    """One nightly pass. Self-gated on ``[memory_decay] enabled``."""
    result = DecayRunResult()
    dc = cfg.memory_decay
    if not dc.enabled:
        return result
    clusters = find_decay_clusters(
        conn,
        after_days=dc.after_days,
        cluster_min=dc.cluster_min,
        cluster_max=dc.cluster_max,
        max_clusters=dc.max_clusters_per_night,
        now=now,
    )
    result.clusters_considered = len(clusters)
    if not clusters:
        return result
    call = llm_call if llm_call is not None else _build_llm_call(cfg)
    if roster is None:
        roster = identity_mod.load_roster(cfg)

    for cluster in clusters:
        try:
            summary = _distill(call, cluster)
        except Exception:  # noqa: BLE001 — one bad distill never kills the pass
            logger.exception("memory decay distill failed on %s", cluster.path)
            continue
        gate = _passes_gates(
            cluster,
            summary,
            roster=roster,
            shrink_ceiling=dc.shrink_ceiling,
            line_max_chars=dc.line_max_chars,
        )
        if gate is not None:
            result.gated.append(gate)
            logger.info("memory decay gated (%s) on %s — cluster kept", gate, cluster.path)
            continue
        # the op (spec §4): summary in, sources struck — receipts survive
        entries_mod.append_entry(
            conn,
            name=cluster.path,
            content=summary,
            tags=[
                "fact",
                f"decayed:{cluster.tier}",
                "abstracted-from:" + ",".join(cluster.entry_ids),
            ],
        )
        for entry_id in cluster.entry_ids:
            entries_mod.mark_entry_deleted(conn, name=cluster.path, entry_id=entry_id)
        result.clusters_decayed += 1
        result.entries_retired += len(cluster.entry_ids)
        logger.info(
            "memory decay: %s — %d entr%s → 1 tier-%d summary",
            cluster.path,
            len(cluster.entry_ids),
            "y" if len(cluster.entry_ids) == 1 else "ies",
            cluster.tier,
        )
    return result

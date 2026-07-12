"Structural and optional semantic audit of relation edges."

from __future__ import annotations

import json
import random
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from ..logger import get
from ..store import relation_edges as edges_store

logger = get("persome.evomem.edge_audit")

_ENTITY_PREFIXES = ("person-", "org-", "project-", "tool-")

# The extractor's honest synthetic fallbacks (constructions, not excerpts).
# Edges written by pre-0.3.0 releases carry the same constructions in their
# original Chinese wording; preserved data must keep auditing as synthetic, so
# both generations are recognized (escaped to satisfy the language gate).
_SYNTHETIC_PATTERNS = (
    re.compile(r"^Interaction history with .+$"),
    re.compile(r"^.+ and .+ appeared in the same context$"),
    re.compile(r"^Completed .+$"),
    re.compile(r"^.+ project memory contains \d+ durable facts$"),
    re.compile("^\u4e0e .+ \u7684\u4ea4\u4e92\u8bb0\u5f55$"),
    re.compile("^.+ \u4e0e .+ \u66fe\u5728\u540c\u4e00\u573a\u666f\u51fa\u73b0$"),
    re.compile("^\u5df2\u5b8c\u6210\u7684 .+ \u4e8b\u9879$"),
    re.compile("^.+ \u9879\u76ee\u8bb0\u5fc6 \\d+ \u6761\u6301\u4e45\u4e8b\u5b9e$"),
)

# Markers identifying the co-occurrence construction across both generations.
_COOCCUR_MARKERS = (
    "appeared in the same context",
    "\u66fe\u5728\u540c\u4e00\u573a\u666f\u51fa\u73b0",
)


def _norm_ws(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text or "").split())


def _is_synthetic(quote: str) -> bool:
    return any(p.match(quote or "") for p in _SYNTHETIC_PATTERNS)


def _is_cooccur_quote(quote: str) -> bool:
    return _is_synthetic(quote) and any(marker in quote for marker in _COOCCUR_MARKERS)


def _activity_source(identity: str) -> tuple[str, str] | None:
    from ..model.activity_source import normalize_activity_identity

    normalized = normalize_activity_identity(identity)
    parts = normalized.split(":", 2)
    if len(parts) != 3 or parts[0] != "event" or parts[1] not in {"intent", "entry", "session"}:
        return None
    return parts[1], parts[2]


def _activity_evidence(conn, identity: str, row: Any | None = None) -> tuple[bool, list[str], str]:
    """Resolve one Activity identity/source triplet to live evidence text."""
    source = _activity_source(identity)
    if row is not None:
        try:
            kind = str(row["source_kind"] or "")
            source_id = str(row["source_id"] or "")
            if kind and source_id:
                source = (kind, source_id)
        except (IndexError, KeyError, TypeError):
            pass
    if source is None:
        return False, [], "unknown"
    kind, source_id = source
    try:
        if kind == "intent":
            record = conn.execute(
                "SELECT status, rationale, resolution_outcome FROM intents WHERE id = ?",
                (source_id,),
            ).fetchone()
            if record is None:
                return False, [], kind
            done = str(record[0]) in {"consumed", "completed"} or (
                str(record[0]) == "resolved" and str(record[2] or "") == "done"
            )
            return done, [str(record[1] or "")], kind
        if kind == "entry":
            record = conn.execute(
                "SELECT content FROM entries WHERE id = ? AND prefix = 'event' "
                "AND superseded = 0 LIMIT 1",
                (source_id,),
            ).fetchone()
            return record is not None, [str(record[0] or "")] if record else [], kind
        if kind == "session":
            from ..model.activity_source import ActivitySource

            event = next(
                (
                    item
                    for item in ActivitySource(conn, include_legacy_intents=False).events()
                    if item.stable_id == f"event:session:{source_id}"
                ),
                None,
            )
            return event is not None, [event.summary] if event else [], kind
    except Exception:  # noqa: BLE001 — old/missing stores are an honest unavailable source
        return False, [], kind
    return False, [], kind


@dataclass
class EdgeVerdict:
    edge_id: str
    src: str
    dst: str
    predicate: str
    observations: int
    verdict: str = "valid"  # valid | structural_hallucination | semantic_hallucination
    checks: dict[str, bool] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _file_stems(identity: str) -> list[str]:
    from .person_graph import _slug

    stems = [identity]
    slug = _slug(identity)
    if slug and slug != identity:
        stems.append(slug)
    return stems


def _derived_kind(conn, identity: str) -> str | None:
    """The kind the identity ITSELF derives — the audit's ground for
    kind_consistent and endpoint existence. None = unresolvable (dangling)."""
    if identity == "self":
        return "self"
    if identity.startswith("event:"):
        exists, _texts, _kind = _activity_evidence(conn, identity)
        return "event" if exists else None
    for prefix, kind in (
        ("person-", "person"),
        ("org-", "org"),
        ("project-", "project"),
        ("tool-", "artifact"),
    ):
        for stem in _file_stems(identity):
            try:
                hit = conn.execute(
                    "SELECT 1 FROM evo_nodes WHERE file_name = ? AND is_latest = 1"
                    " AND status = 'active' LIMIT 1",
                    (f"{prefix}{stem}.md",),
                ).fetchone()
            except Exception:  # noqa: BLE001 — table may predate this build
                return None
            if hit is not None:
                return kind
    return None


def _entity_texts(conn, identity: str) -> list[str]:
    texts: list[str] = []
    try:
        for prefix in _ENTITY_PREFIXES:
            for stem in _file_stems(identity):
                for row in conn.execute(
                    "SELECT content FROM evo_nodes WHERE file_name = ? AND status = 'active'",
                    (f"{prefix}{stem}.md",),
                ):
                    if row[0]:
                        texts.append(str(row[0]))
    except Exception:  # noqa: BLE001 — table may predate this build
        return []
    return texts


def _shared_bucket(conn, a: str, b: str) -> bool:
    """Re-derive the co-occurrence claim: do a and b share an occurred_at
    minute bucket in their entity-file event nodes?"""

    def buckets(name: str) -> set[str]:
        out = set()
        try:
            for prefix in _ENTITY_PREFIXES:
                for stem in _file_stems(name):
                    for row in conn.execute(
                        "SELECT occurred_at FROM evo_nodes WHERE file_name = ?"
                        " AND occurred_at IS NOT NULL",
                        (f"{prefix}{stem}.md",),
                    ):
                        out.add(str(row[0])[:16])
        except Exception:  # noqa: BLE001 — table may predate this build
            return set()
        return out

    return bool(buckets(a) & buckets(b))


def audit_edge(conn, row: Any, *, llm_call: Any | None = None) -> EdgeVerdict:
    """One edge through the deterministic checks (+ optional LLM entailment)."""
    src, dst = str(row["src_identity"]), str(row["dst_identity"])
    quote = str(row["quote"] or "")
    v = EdgeVerdict(
        edge_id=str(row["edge_id"]),
        src=src,
        dst=dst,
        predicate=str(row["predicate"]),
        observations=int(row["observations"] or 1),
    )

    src_kind_derived = _derived_kind(conn, src)
    dst_kind_derived = _derived_kind(conn, dst)
    v.checks["src_exists"] = src_kind_derived is not None
    v.checks["dst_exists"] = dst_kind_derived is not None

    # matrix legality on the STORED kinds (add_edge validated at mint; a miss = drift)
    try:
        pred = edges_store.Predicate(str(row["predicate"]))
        sk = edges_store.EntityKind(str(row["src_kind"] or ""))
        dk = edges_store.EntityKind(str(row["dst_kind"] or ""))
        v.checks["matrix_legal"] = (sk, dk) in edges_store._LEGAL_ENDPOINTS[pred]  # noqa: SLF001
    except ValueError:
        v.checks["matrix_legal"] = False

    v.checks["kind_consistent"] = (
        src_kind_derived is None or str(row["src_kind"] or "") == src_kind_derived
    ) and (dst_kind_derived is None or str(row["dst_kind"] or "") == dst_kind_derived)

    # source existence + quote traceability, per edge family
    source_ok = True
    trace_ok = True
    event_identity = None
    for ident in (src, dst):
        if ident.startswith("event:"):
            event_identity = ident
    if event_identity is not None:
        source_ok, source_texts, source_kind = _activity_evidence(conn, event_identity, row)
        if not source_ok and source_kind == "intent":
            v.notes.append("legacy_source_unavailable")
        if source_ok and quote and not _is_synthetic(quote):
            trace_ok = any(_norm_ws(quote) in _norm_ws(text) for text in source_texts)
    elif _is_cooccur_quote(quote):
        # co-occurrence: re-derive the shared minute bucket instead of text trace
        trace_ok = _shared_bucket(conn, src, dst)
        source_ok = trace_ok
        v.notes.append("synthetic_quote:cooccur_rederived")
    else:
        texts = _entity_texts(conn, src) + _entity_texts(conn, dst)
        source_ok = bool(texts)
        if quote and not _is_synthetic(quote):
            nq = _norm_ws(quote)
            trace_ok = any(nq in _norm_ws(t) for t in texts)
        elif _is_synthetic(quote):
            v.notes.append("synthetic_quote")
    v.checks["source_exists"] = source_ok
    v.checks["quote_traceable"] = trace_ok

    if not all(v.checks.values()):
        v.verdict = "structural_hallucination"
        v.notes.append("failed:" + ",".join(k for k, ok in v.checks.items() if not ok))
        return v

    if llm_call is not None:
        entailed = _llm_entails(conn, row, quote, llm_call)
        if entailed is False:
            v.verdict = "semantic_hallucination"
            v.notes.append("llm:not_entailed")
            return v
        if entailed is None:
            v.notes.append("llm:unparseable_fail_open")
    v.verdict = "valid"
    return v


def _llm_entails(conn, row: Any, quote: str, llm_call: Any) -> bool | None:
    """Semantic tier: does the evidence text entail the relation? None = judge
    failed (fail-open — never counts as hallucination on a broken judge)."""
    try:
        prompt = (
            f"Relation: {row['src_identity']} —{row['predicate']}"
            f"{('(' + str(row['label']) + ')') if row['label'] else ''}→ {row['dst_identity']}\n"
            f"Evidence: {quote}\n\n"
            'Does the evidence entail this relation? Return JSON only: {"entailed": true|false}'
        )
        resp = llm_call([{"role": "user", "content": prompt}])
        data = json.loads(resp.choices[0].message.content or "{}")
        val = data.get("entailed")
        return val if isinstance(val, bool) else None
    except Exception:  # noqa: BLE001 — judge failure is fail-open
        logger.debug("edge_audit: LLM judge failed", exc_info=True)
        return None


# ── sampling + report ─────────────────────────────────────────────────────────

_STRATA = (  # (predicate on observations, share of the budget)
    (lambda o: o <= 1, 0.5),
    (lambda o: 2 <= o <= 3, 0.3),
    (lambda o: o >= 4, 0.2),
)


def stratified_sample(conn, n: int, *, seed: int | None = None) -> list[Any]:
    """N shadow edges stratified by observations (low-evidence overweighted);
    a short stratum donates its slack downstream. Seeded = reproducible."""
    conn.row_factory = __import__("sqlite3").Row
    rows = conn.execute("SELECT * FROM relation_edges WHERE status = 'shadow'").fetchall()
    rng = random.Random(seed)
    picked: list[Any] = []
    remaining = list(rows)
    budget = n
    for pred, share in _STRATA:
        stratum = [r for r in remaining if pred(int(r["observations"] or 1))]
        take = min(len(stratum), max(0, round(n * share)))
        take = min(take, budget)
        chosen = rng.sample(stratum, take) if take else []
        picked.extend(chosen)
        chosen_ids = {c["edge_id"] for c in chosen}
        remaining = [r for r in remaining if r["edge_id"] not in chosen_ids]
        budget = n - len(picked)
    if budget > 0 and remaining:
        # slack redistribution prefers LOW-evidence edges (the stratification's
        # whole point); rng tie-breaks within equal observations
        remaining.sort(key=lambda r: (int(r["observations"] or 1), rng.random()))
        picked.extend(remaining[: min(budget, len(remaining))])
    return picked


def run_audit(
    conn, *, n: int = 20, seed: int | None = None, llm_call: Any | None = None
) -> dict[str, Any]:
    sample = stratified_sample(conn, n, seed=seed)
    verdicts = [audit_edge(conn, r, llm_call=llm_call) for r in sample]
    halluc = [v for v in verdicts if v.verdict != "valid"]
    by_pred: dict[str, dict[str, int]] = {}
    for v in verdicts:
        b = by_pred.setdefault(v.predicate, {"sampled": 0, "hallucinated": 0})
        b["sampled"] += 1
        if v.verdict != "valid":
            b["hallucinated"] += 1
    return {
        "sample_size": len(verdicts),
        "hallucination_count": len(halluc),
        "hallucination_rate": (len(halluc) / len(verdicts)) if verdicts else 0.0,
        "semantic_tier": llm_call is not None,
        "by_predicate": by_pred,
        "edges": [
            {
                "edge_id": v.edge_id,
                "src": v.src,
                "dst": v.dst,
                "predicate": v.predicate,
                "observations": v.observations,
                "verdict": v.verdict,
                "checks": v.checks,
                "notes": v.notes,
            }
            for v in verdicts
        ],
    }

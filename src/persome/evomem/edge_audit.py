"""Edge hallucination-rate sampler — §7-7 row 5's oracle (the §7-4 promise).

Answers "how many of these edges are WRONG" on a reproducible sample, so
extraction quality gets a number instead of a vibe. Two tiers:

**Deterministic tier (zero LLM, always on)** — structural hallucination:
every minted edge is traceable by construction (quote ≤120 chars excerpted
from its source; ``event:<intents.id>`` identities point at the minting
intent; person/project edges point at their entity files), so the checks
re-derive what ``add_edge``/the extractor claimed:

- ``src_exists`` / ``dst_exists`` — the endpoint identity still resolves
  (self · a done-terminal ``intents`` row · an entity file with active
  nodes). A dangling endpoint (e.g. an entity later shadowed by
  adjudication) is a structural hallucination.
- ``matrix_legal`` — predicate × (src_kind, dst_kind) ∈ §4.2
  ``_LEGAL_ENDPOINTS``. ``add_edge`` validates at mint time, so a failure
  here is legacy drift / hand-written rows.
- ``kind_consistent`` — the STORED kinds match what the identity itself
  derives (event: prefix → event, file prefix → org/project/artifact,
  else person).
- ``source_exists`` — the minting source is still there (the intent row is
  done-terminal; the entity file has ≥1 active node).
- ``quote_traceable`` — the evidence excerpt is found in its claimed source
  text (intent rationale / the endpoint files' node contents). The
  extractor's SYNTHETIC fallback quotes (与 X 的交互记录 / A 与 B 曾在同一
  场景出现 / …) are honest constructions, not excerpts — they pass with a
  ``synthetic_quote`` note; the co-occurrence one is instead re-derived
  from the shared-minute-bucket computation.

**LLM tier (``--llm``, default OFF)** — semantic hallucination: does the
source text actually ENTAIL the relation? Expensive but catches what
structure cannot (a traceable quote that doesn't support the predicate).

Verdicts (MECE): ``valid`` · ``structural_hallucination`` ·
``semantic_hallucination``. Sampling is stratified by ``observations``
(low-evidence edges hallucinate more): obs==1 gets half the budget, 2–3 gets
30%, ≥4 the rest; a short stratum donates its slack to the next.
"""

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

_DONE_STATUSES = ("consumed", "resolved", "completed")
_ENTITY_PREFIXES = ("person-", "org-", "project-", "tool-")

# the extractor's honest synthetic fallbacks (constructions, not excerpts)
_SYNTHETIC_PATTERNS = (
    re.compile(r"^与 .+ 的交互记录$"),
    re.compile(r"^.+ 与 .+ 曾在同一场景出现$"),
    re.compile(r"^已完成的 .+ 事项$"),
    re.compile(r"^.+ 项目记忆 \d+ 条持久事实$"),
)


def _norm_ws(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text or "").split())


def _is_synthetic(quote: str) -> bool:
    return any(p.match(quote or "") for p in _SYNTHETIC_PATTERNS)


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
    """identity → candidate file stems: exact AND slugged (person_graph mints
    files via ``_slug``, so special-char canonicals — 「金辰Vincent 天壹资本」 →
    ``金辰vincent-天壹资本`` — are not exact-invertible)."""
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
        try:
            row = conn.execute(
                "SELECT status FROM intents WHERE id = ?", (identity.removeprefix("event:"),)
            ).fetchone()
        except Exception:  # noqa: BLE001 — table may predate this build
            row = None
        return "event" if row is not None else None
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
    event_id = None
    for ident in (src, dst):
        if ident.startswith("event:"):
            event_id = ident.removeprefix("event:")
    if event_id is not None:
        srow = conn.execute(
            "SELECT status, rationale FROM intents WHERE id = ?", (event_id,)
        ).fetchone()
        source_ok = srow is not None and str(srow[0]) in _DONE_STATUSES
        if source_ok and quote and not _is_synthetic(quote):
            trace_ok = _norm_ws(quote) in _norm_ws(str(srow[1] or ""))
    elif _is_synthetic(quote) and "曾在同一场景出现" in quote:
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
            f"关系断言：{row['src_identity']} —{row['predicate']}"
            f"{('(' + str(row['label']) + ')') if row['label'] else ''}→ {row['dst_identity']}\n"
            f"证据文本：{quote}\n\n"
            '证据文本是否足以支撑这条关系断言？只输出 JSON：{"entailed": true|false}'
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

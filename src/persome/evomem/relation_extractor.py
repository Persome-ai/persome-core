"Evidence-gated extraction and persistence of relation edges."

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any

from ..logger import get
from ..store import relation_edges as edges_store
from ..store.relation_edges import EntityKind, Predicate

logger = get("persome.evomem.relation_extractor")

# The user (graph centre). A sentinel identity distinct from any person canonical.
SELF_IDENTITY = "self"
# Activity (EVENT) identities use the model contract namespace:
# event:intent:<legacy-id> | event:entry:<entry-id> | event:session:<session-id>.
EVENT_PREFIX = "event:"
_MAX_PEOPLE = 200  # cap persons scanned from the past layer
_MAX_ACTIVITIES = 200  # cap sourced past activities scanned
_LLM_MAX_PEOPLE = 40  # cap the LLM evidence roster
_MAX_PEOPLE_PER_GROUP = 6  # skip combinatorial blowup on huge co-occurrence groups
_LLM_MIN_CONFIDENCE = 0.7  # precision gate for LLM-proposed edges
_QUOTE_MAX = 120

LlmCallFn = Callable[..., Any]


@dataclass
class ExtractionResult:
    deterministic_count: int = 0
    llm_count: int = 0
    reinforced: int = 0  # open edges whose evidence-count strength grew this run

    @property
    def written_count(self) -> int:
        return self.deterministic_count + self.llm_count


# ── reading the consolidated PAST layer (person_graph over evo_nodes) ──────────


@dataclass
class _People:
    persons: list[Any]  # PersonEntity
    cooccur: dict[tuple[str, str], int]  # unordered person-person pair → shared-bucket count
    summaries: dict[str, str]  # canonical → latest interaction summary
    evidence: str  # concatenated build_person_context for the LLM pass
    aliases: dict[str, str]  # _norm(alias) → canonical (participant resolution)
    # evidence-time grounding (memory-graph fidelity fix): an edge's valid_from
    # must be the FIRST EVIDENCE moment, not the extraction transaction moment —
    # a one-shot backlog run otherwise collapses the whole graph onto one day
    # and the as-of axis degenerates to two states.
    first_seen: dict[str, str] = field(default_factory=dict)  # canonical → earliest occurred_at
    cooccur_first: dict[tuple[str, str], str] = field(
        default_factory=dict
    )  # pair → earliest bucket


@dataclass
class _Activity:
    """One versioned past Activity (EVENT) point (§4.0 residue ①)."""

    identity: str
    label: str
    quote: str
    participants: list[str]  # canonical person identities (consolidated only)
    ts: str | None = None  # source-event time — the activity edge's valid_from
    source_kind: str = ""
    source_id: str = ""
    source_receipt: str = ""
    # typed non-person mentions (identity, kind∈{org,project}) — the §1.3 about
    # leg: EVENT→ORG/PROJECT is the one legal cell that reconnects retyped
    # entities to USER (self→event→org) without inventing a predicate.
    typed_mentions: list[tuple[str, str]] = dataclasses.field(default_factory=list)


def _load_people(memory: Any, cfg: Any) -> _People:
    """Read consolidated persons + interaction timelines from person_graph (past layer).

    Fail-safe: any read error → empty (never raises into the tick).
    """
    empty = _People([], {}, {}, "", {})
    try:
        from .person_graph import PersonGraph, _norm

        pg = PersonGraph(memory, cfg=cfg)
        persons = pg.list_persons()[:_MAX_PEOPLE]
        if not persons:
            return empty

        buckets: dict[str, set[str]] = {}
        summaries: dict[str, str] = {}
        aliases: dict[str, str] = {}
        first_seen: dict[str, str] = {}
        for p in persons:
            for a in [p.canonical, *getattr(p, "aliases", [])]:
                key = _norm(a)
                if key:
                    aliases.setdefault(key, p.canonical)
            timeline = pg.person_timeline(p.canonical)
            if timeline:
                summaries[p.canonical] = (timeline[-1].content or "").strip()
            for ev in timeline:
                occurred = getattr(ev, "occurred_at", None)
                if not occurred:
                    continue
                bucket = str(occurred)[:16]
                buckets.setdefault(bucket, set()).add(p.canonical)
                cur = first_seen.get(p.canonical)
                if cur is None or bucket < cur:
                    first_seen[p.canonical] = bucket

        cooccur: dict[tuple[str, str], int] = {}
        cooccur_first: dict[tuple[str, str], str] = {}
        for bucket in sorted(buckets):
            names = buckets[bucket]
            if 2 <= len(names) <= _MAX_PEOPLE_PER_GROUP:
                for a, b in combinations(sorted(names), 2):
                    cooccur[(a, b)] = cooccur.get((a, b), 0) + 1
                    cooccur_first.setdefault((a, b), bucket)

        evidence = "\n\n".join(
            block
            for p in persons[:_LLM_MAX_PEOPLE]
            if (block := pg.build_person_context(p.canonical))
        ).strip()
        return _People(persons, cooccur, summaries, evidence, aliases, first_seen, cooccur_first)
    except Exception:  # noqa: BLE001 — past-layer read is best-effort
        logger.debug("relation_extractor: person_graph read failed, empty", exc_info=True)
        return empty


def _load_typed_entities(conn) -> dict[str, tuple[str, str]]:
    """norm(name) → (identity, kind) for org-/project- entity files — the
    adjudicated non-person points (§1.2 dimension criterion). Read directly
    off evo_nodes; fail-safe empty. artifact (tool-) is deliberately absent:
    ``about``'s dst set excludes ARTIFACT (§4.2) and no other cell covers
    SELF/EVENT→tool usage — tools stay honest orphans until the matrix grows
    a ``uses`` predicate (product decision) or delta evidence lands.
    """
    from .person_graph import _norm

    out: dict[str, tuple[str, str]] = {}
    try:
        for row in conn.execute(
            "SELECT DISTINCT file_name FROM evo_nodes"
            " WHERE (file_name LIKE 'org-%' OR file_name LIKE 'project-%')"
            " AND is_latest = 1 AND status = 'active'"
        ).fetchall():
            fn = str(row[0] or "")
            for prefix, kind in (("org-", "org"), ("project-", "project")):
                if fn.startswith(prefix):
                    ident = fn.removeprefix(prefix).removesuffix(".md")
                    if ident:
                        out[_norm(ident)] = (ident, kind)
    except Exception:  # noqa: BLE001 — typed roster is best-effort
        logger.debug("relation_extractor: typed-entity read failed", exc_info=True)
    return out


def _alias_mentioned(alias: str, text: str) -> bool:
    """Evidence-grade mention check for a normalized alias in normalized text.

    Plain substring containment fabricates participants — "amy" is inside
    "family" and a one-character given name is inside almost any sentence —
    and a fabricated participant becomes a relation edge. An ASCII alias must
    therefore match on word boundaries, and every alias must be at least two
    characters long.
    """
    if not alias or len(alias) < 2 or not text or alias not in text:
        return False
    if alias.isascii():
        pattern = rf"(?<![0-9a-z]){re.escape(alias)}(?![0-9a-z])"
        return re.search(pattern, text) is not None
    return True


def _load_terminal_activities(conn, people: _People) -> list[_Activity]:
    """Load durable activities plus the read-only legacy terminal-intent fallback.

    Participants are linked only when already consolidated in person_graph (unknown names are
    skipped, not minted). Typed non-person mentions are matched against the adjudicated typed
    roster for the about leg. A missing legacy intents table must not disable durable activities.
    """
    typed_roster = _load_typed_entities(conn)
    from ..model.activity_source import ActivitySource
    from .person_graph import _norm

    out: list[_Activity] = []

    def resolve_participants(raw_names: list[str], summary: str) -> list[str]:
        candidates = {_norm(name) for name in raw_names if _norm(name)}
        normalized_summary = _norm(summary)
        candidates.update(
            alias for alias in people.aliases if _alias_mentioned(alias, normalized_summary)
        )
        resolved: list[str] = []
        for alias in candidates:
            if canonical := people.aliases.get(alias):
                resolved.append(canonical)
            elif typed := typed_roster.get(alias):
                identity, kind = typed
                resolved.append(f"{kind}:{identity}")
        return list(dict.fromkeys(resolved))

    for event in ActivitySource(
        conn,
        participant_resolver=resolve_participants,
        include_legacy_intents=True,
        limit=_MAX_ACTIVITIES,
    ).events():
        summary_norm = _norm(event.summary)
        typed_mentions = [
            typed for alias, typed in typed_roster.items() if _alias_mentioned(alias, summary_norm)
        ]
        participants: list[str] = []
        for participant in event.participant_ids:
            kind, separator, identity = participant.partition(":")
            if separator and kind in {"org", "project"}:
                typed_mentions.append((identity, kind))
            else:
                participants.append(participant)
        out.append(
            _Activity(
                identity=event.stable_id,
                label="activity",
                quote=event.summary,
                participants=participants,
                ts=event.occurred_at,
                source_kind=event.source_kind,
                source_id=event.source_id,
                source_receipt=event.source_receipt,
                typed_mentions=list(dict.fromkeys(typed_mentions)),
            )
        )
    return out


# ── shared write helper ─────────────────────────────────────────────────────────


def _kind_of(identity: str) -> EntityKind:
    if identity == SELF_IDENTITY:
        return EntityKind.SELF
    if identity.startswith(EVENT_PREFIX):
        return EntityKind.EVENT
    return EntityKind.PERSON


def _edge_key(src: str, dst: str, predicate_value: str) -> tuple[str, str, str]:
    """Dedup key. Undirected predicates (``knows``) canonicalize endpoint order so
    (A,B,knows) and (B,A,knows) are the same edge (#436)."""
    from ..model.activity_source import normalize_activity_identity

    src = normalize_activity_identity(src)
    dst = normalize_activity_identity(dst)
    if predicate_value == Predicate.KNOWS.value:
        a, b = sorted((src, dst))
        return (a, b, predicate_value)
    return (src, dst, predicate_value)


def _open_edges(conn) -> dict[tuple[str, str, str], str]:
    """Existing open (valid_to IS NULL), non-retired edges: key → edge_id (for reinforcement)."""
    edges_store.ensure_schema(conn)
    conn.row_factory = None
    rows = conn.execute(
        "SELECT src_identity, dst_identity, predicate, edge_id FROM relation_edges "
        "WHERE valid_to IS NULL AND status IN ('shadow','active')"
    ).fetchall()
    return {_edge_key(r[0], r[1], r[2]): r[3] for r in rows}


@dataclass
class _Tally:
    new: int = 0
    reinforced: int = 0


def _upsert_shadow(
    conn,
    seen: dict[tuple[str, str, str], str],
    tally: _Tally,
    *,
    src: str,
    dst: str,
    predicate: Predicate,
    confidence: float,
    quote: str,
    label: str | None,
    observations: int = 1,
    provenance: str = "inferred",
    valid_from: str | None = None,
    src_kind: str | None = None,
    dst_kind: str | None = None,
    polarity: str = "0",
    additive: bool = False,
    source_kind: str | None = None,
    source_id: str | None = None,
    source_receipt: str | None = None,
    status: str = "shadow",
    commit: bool = True,
) -> None:
    """New evidence adds an edge; existing open edge ratchets its strength.

    ``observations`` is the caller-computed count of distinct supporting evidence
    (idempotent MAX semantics — re-running over the same data changes nothing).
    Endpoint legality is enforced by ``add_edge`` (raises for an illegal pair) — the
    caller treats a raise as "not a P0 relation" and drops it. Extractors use the
    default shadow status; deterministic observed floor edges may request active.
    """
    key = _edge_key(src, dst, predicate.value)
    eid = seen.get(key)
    if eid is not None:
        if edges_store.reinforce_edge(
            conn,
            edge_id=eid,
            observations=observations,
            confidence=confidence,
            additive=additive,
            commit=commit,
        ):
            tally.reinforced += 1
        if status == "active":
            conn.execute(
                "UPDATE relation_edges SET status='active' WHERE edge_id=? AND status='shadow'",
                (eid,),
            )
        return
    new_id = edges_store.add_edge(
        conn,
        src_identity=src,
        dst_identity=dst,
        predicate=predicate,
        src_kind=src_kind or _kind_of(src),
        dst_kind=dst_kind or _kind_of(dst),
        provenance=provenance,
        confidence=confidence,
        label=label,
        quote=(quote or "")[:_QUOTE_MAX] or None,
        observations=observations,
        valid_from=valid_from,
        polarity=polarity,
        source_kind=source_kind,
        source_id=source_id,
        source_receipt=source_receipt,
        status=status,
        commit=commit,
    )
    seen[key] = new_id
    tally.new += 1


# ── the passes ────────────────────────────────────────────────────────────────


def _deterministic_pass(
    conn, people: _People, seen: dict[tuple[str, str, str], str], tally: _Tally
) -> None:
    """SELF↔person + co-occurring person↔person ``knows`` from consolidated entities.

    strength(observations) comes FROM the evidence: sightings for SELF↔person, shared
    time-bucket count for person↔person — so reinforcement is idempotent per evidence.
    """
    for p in people.persons:
        canonical = p.canonical
        if not canonical or canonical == SELF_IDENTITY:
            continue
        sightings = max(1, int(getattr(p, "sightings", 1) or 1))
        conf = min(1.0, 0.55 + 0.1 * min(sightings, 5))
        quote = people.summaries.get(canonical) or f"Interaction history with {canonical}"
        try:
            _upsert_shadow(
                conn,
                seen,
                tally,
                src=SELF_IDENTITY,
                dst=canonical,
                predicate=Predicate.KNOWS,
                confidence=conf,
                quote=quote,
                label=(getattr(p, "category", None) or None),
                observations=sightings,
                valid_from=people.first_seen.get(canonical),
            )
        except ValueError:
            continue  # illegal endpoints for P0 → drop (mirrors the LLM pass, #435)
    for (a, b), shared in people.cooccur.items():
        if SELF_IDENTITY in (a, b):
            continue  # self co-occurrence is already the SELF↔person loop above (#435)
        try:
            _upsert_shadow(
                conn,
                seen,
                tally,
                src=a,
                dst=b,
                predicate=Predicate.KNOWS,
                confidence=0.6,
                quote=f"{a} and {b} appeared in the same context",
                label=None,
                observations=shared,
                valid_from=people.cooccur_first.get((a, b)),
            )
        except ValueError:
            continue  # illegal endpoints for P0 → drop (#435)


def _project_pass(conn, seen: dict[tuple[str, str, str], str], tally: _Tally) -> None:
    """SELF→PROJECT ``participates_in`` (works_on) — the §1.3 legal cell whose
    evidence is the project memory file itself: every durable fact under
    ``project-X.md`` exists because the classifier attributed the USER's own
    session facts to that project, so the file IS participation evidence by
    construction. observations = active fact count (evidence ratchet),
    valid_from = the earliest evidenced moment. Deterministic, idempotent.
    ORG deliberately has no deterministic leg: SELF→ORG's only cell is
    part_of, and mere interaction cannot honestly assert membership (an
    interview counterexample) — that waits for delta quote evidence.
    """
    try:
        rows = conn.execute(
            "SELECT file_name, COUNT(*), MIN(COALESCE(occurred_at, memory_at)),"
            " MAX(content)"
            " FROM evo_nodes WHERE file_name LIKE 'project-%'"
            " AND is_latest = 1 AND status = 'active' GROUP BY file_name"
        ).fetchall()
    except Exception:  # noqa: BLE001 — best-effort
        logger.debug("relation_extractor: project read failed", exc_info=True)
        return
    for fn, count, first_ts, sample in rows:
        ident = str(fn or "").removeprefix("project-").removesuffix(".md")
        if not ident or int(count or 0) < 1:
            continue
        try:
            _upsert_shadow(
                conn,
                seen,
                tally,
                src=SELF_IDENTITY,
                dst=ident,
                predicate=Predicate.PARTICIPATES_IN,
                confidence=0.9,
                quote=(
                    str(sample or "").strip()
                    or f"{ident} project memory contains {count} durable facts"
                ),
                label="works_on",
                observations=int(count),
                provenance="inferred",
                valid_from=str(first_ts) if first_ts else None,
                src_kind="self",
                dst_kind="project",
            )
        except ValueError:
            continue


def _activity_pass(
    conn, activities: list[_Activity], seen: dict[tuple[str, str, str], str], tally: _Tally
) -> None:
    """Sourced past activities → EVENT points via ``participates_in`` edges.

    Each activity is one evidence item; a re-scan is a no-op because its source
    identity is stable. Legacy intent activities remain inferred because the
    neutral Activity contract intentionally carries no product status semantics.
    """
    for act in activities:
        prov = "inferred"
        conf = 0.7
        try:
            _upsert_shadow(
                conn,
                seen,
                tally,
                src=SELF_IDENTITY,
                dst=act.identity,
                predicate=Predicate.PARTICIPATES_IN,
                confidence=conf,
                quote=act.quote,
                label=act.label,
                provenance=prov,
                valid_from=act.ts,
                source_kind=act.source_kind,
                source_id=act.source_id,
                source_receipt=act.source_receipt,
            )
        except ValueError:
            continue
        for person in act.participants:
            try:
                _upsert_shadow(
                    conn,
                    seen,
                    tally,
                    src=person,
                    dst=act.identity,
                    predicate=Predicate.PARTICIPATES_IN,
                    confidence=conf,
                    quote=act.quote,
                    label=act.label,
                    provenance=prov,
                    valid_from=act.ts,
                    source_kind=act.source_kind,
                    source_id=act.source_id,
                    source_receipt=act.source_receipt,
                )
            except ValueError:
                continue
        # §1.3 about leg: the event mentions an adjudicated org/project point —
        # EVENT→ORG/PROJECT is the legal cell that reconnects typed entities to
        # USER (self→event→org), no invented predicate, evidence = the activity.
        for ident, tkind in act.typed_mentions:
            try:
                _upsert_shadow(
                    conn,
                    seen,
                    tally,
                    src=act.identity,
                    dst=ident,
                    predicate=Predicate.ABOUT,
                    confidence=conf,
                    quote=act.quote,
                    label=act.label,
                    provenance=prov,
                    valid_from=act.ts,
                    src_kind="event",
                    dst_kind=tkind,
                    source_kind=act.source_kind,
                    source_id=act.source_id,
                    source_receipt=act.source_receipt,
                )
            except ValueError:
                continue


_LLM_SYSTEM = (
    "Extract only clearly evidenced person-to-person relations from the supplied "
    "interaction records. Omit anything uncertain or unsupported by a direct quote."
)


def _llm_prompt(evidence: str, identities: list[str]) -> list[dict[str, Any]]:
    roster = ", ".join(["self=the memory owner", *identities])
    instruction = (
        f"Entities (src and dst must be copied exactly from this list): {roster}\n\n"
        "Extract relations from the interaction records below. Return a JSON array; "
        "each item is:\n"
        '{"src": "...", "dst": "...", "predicate": "...", "label": "...", '
        '"quote": "...", "confidence": 0.0}\n'
        "- predicate must be reports_to (src reports to dst) or knows "
        "(colleague, client, friend, or general acquaintance; direction is not meaningful).\n"
        "- quote must copy at most 120 characters of direct supporting evidence.\n"
        "- label is an optional one-sentence description such as manager or client.\n"
        "- Omit unsupported or differently typed relations. Return only the JSON array.\n\n"
        f"Interaction records:\n{evidence}"
    )
    return [{"role": "system", "content": _LLM_SYSTEM}, {"role": "user", "content": instruction}]


def _llm_pass(
    cfg: Any,
    conn,
    people: _People,
    seen: dict[tuple[str, str, str], str],
    tally: _Tally,
    llm_call: LlmCallFn,
) -> int:
    """One bounded LLM call → directed person-person edges (reports_to). Fail-open."""
    from ..writer.llm import call_llm as _live_call
    from ..writer.llm import extract_text

    identities = [p.canonical for p in people.persons[:_LLM_MAX_PEOPLE] if p.canonical]
    evidence = people.evidence
    if not identities or not evidence:
        return 0
    allowed = {SELF_IDENTITY, *identities}

    call = llm_call or _live_call
    try:
        resp = call(
            cfg, "relation_extractor", messages=_llm_prompt(evidence, identities), json_mode=True
        )
        raw = extract_text(resp).strip()
        data = json.loads(raw) if raw else []
    except Exception:  # noqa: BLE001 — LLM/parse failure never blocks the tick
        logger.debug("relation_extractor: LLM pass failed (ignored)", exc_info=True)
        return 0
    if not isinstance(data, list):
        return 0

    written = 0
    for rel in data:
        if not isinstance(rel, dict):
            continue
        src = str(rel.get("src", "")).strip()
        dst = str(rel.get("dst", "")).strip()
        pred_raw = str(rel.get("predicate", "")).strip()
        quote = str(rel.get("quote", "")).strip()
        try:
            conf = float(rel.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        if src not in allowed or dst not in allowed or src == dst:
            continue
        if not quote or quote[:_QUOTE_MAX] not in evidence:
            continue
        if conf < _LLM_MIN_CONFIDENCE:
            continue
        try:
            pred = Predicate(pred_raw)
        except ValueError:
            continue
        label = str(rel.get("label", "")).strip() or None
        try:
            before = tally.new
            _upsert_shadow(
                conn,
                seen,
                tally,
                src=src,
                dst=dst,
                predicate=pred,
                confidence=conf,
                quote=quote,
                label=label,
            )
            if tally.new > before:
                written += 1
        except ValueError:
            continue  # illegal endpoints for P0 → drop
    return written


# ── public entry (mirrors writer.case_extractor.run_case_extraction) ───────────


def run_relation_extraction(
    cfg: Any,
    *,
    memory: Any | None = None,
    llm_call: LlmCallFn | None = None,
    conn_factory: Callable[[], Any] | None = None,
) -> ExtractionResult:
    """Run both passes over the consolidated PAST layer, writing shadow edges. No-op disabled.

    ``memory`` / ``llm_call`` / ``conn_factory`` are injectable seams for tests; live defaults
    use a fresh ``EvoMemory``, provider-aware ``llm.call_llm``, and ``fts.cursor``.
    """
    if not getattr(cfg, "relation_extraction_enabled", False):
        return ExtractionResult()

    from contextlib import nullcontext

    if memory is None:
        from .engine import EvoMemory

        memory = EvoMemory()

    if conn_factory is not None:
        ctx = conn_factory()
        cm = ctx if hasattr(ctx, "__enter__") else nullcontext(ctx)
    else:
        from ..store import fts

        cm = fts.cursor()

    try:
        people = _load_people(memory, cfg)
        with cm as conn:
            activities = _load_terminal_activities(conn, people)
            has_projects = bool(
                conn.execute(
                    "SELECT 1 FROM evo_nodes WHERE file_name LIKE 'project-%'"
                    " AND is_latest = 1 AND status = 'active' LIMIT 1"
                ).fetchone()
            )
            if not people.persons and not activities and not has_projects:
                return ExtractionResult()
            seen = _open_edges(conn)
            tally = _Tally()
            _deterministic_pass(conn, people, seen, tally)
            _activity_pass(conn, activities, seen, tally)
            _project_pass(conn, seen, tally)
            det = tally.new
            llm = _llm_pass(cfg, conn, people, seen, tally, llm_call)  # type: ignore[arg-type]
        return ExtractionResult(deterministic_count=det, llm_count=llm, reinforced=tally.reinforced)
    except Exception:  # noqa: BLE001 — extraction is best-effort enrichment; never crash the tick
        logger.error("relation_extraction failed (ignored)", exc_info=True)
        return ExtractionResult()

"""Relation-edge extraction — deterministic + LLM, writes SHADOW edges (P0-2 / #428).

Spec ``docs/superpowers/specs/2026-07-01-user-centric-relation-graph-memory-design.md`` §4.3.

**Working object = the PAST layer, cut by LIFECYCLE (not by table).** The memory graph is the
user's *weights* (过去式). The tense rule is a lifecycle cut, not a table blacklist:

- ``person_graph`` entities + interaction timelines (evo_nodes) — consolidated past → read.
- ``intents`` rows in a DONE terminal status (``consumed``/``resolved``/``completed``) — the
  commitment/task **happened and finished**; that is a past fact → read, becomes an Activity
  (EVENT) point with ``participates_in`` edges (§4.0 residue ①).
- ``intents`` rows still ``open``/``armed`` — pending future (Prediction) → NEVER read.
- transient runtime state → never read here.

Edge **strength = observations = count of distinct supporting evidence** (event 蒸馏计数),
computed FROM the evidence each run and ratcheted monotonically (``reinforce_edge`` MAX
semantics) — so re-running over the same data is a no-op, while new evidence strengthens.

Hangs beside :mod:`persome.evomem.person_graph` in the daily evomem enrichment tick. Writes edges
through :mod:`persome.store.relation_edges` with ``status='shadow'`` so nothing reaches retrieval or
the digest until proven (§4.3). Gated on ``cfg.relation_extraction_enabled`` (default False) and
**fully fail-open**: any read/LLM/write error is swallowed, never bubbling into the tick.

Two passes, both confined to the **person subgraph** (P0 has PERSON/SELF identities only;
PROJECT/EVENT minimal, ARTIFACT Phase 2 — relations needing those are dropped by the endpoint
validator):

- **Deterministic (no LLM)** — from the consolidated person entities: SELF ↔ each known person
  ``knows`` (strength from sightings); person ↔ person ``knows`` where their interaction events
  co-occur (same time bucket). Identities ARE person_graph canonical names (single resolver).
- **LLM (mockable seam, fail-open)** — one bounded call over the persons' consolidated interaction
  context (`build_person_context`) upgrades the flat ``knows`` set with the directed relations
  co-occurrence cannot infer (``reports_to``), over the SAME roster, each requiring a grounding
  ``quote`` copied from that past context. Off-roster / unquoted / low-confidence → dropped.
"""

from __future__ import annotations

import dataclasses
import json
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
# Done terminals only — the thing actually happened (§4.0 tense gate). dropped terminals
# (dismissed/expired/failed) are past facts too but carry no accomplished activity; P0 skips them.
# `consumed`/`completed` are unconditionally DONE. `resolved` is the evidence-driven auto-close
# channel and is DONE ONLY when resolution_outcome='done' — a resolved intent auto-closed as
# 'rejected' (user later declined / didn't do it) or 'superseded' (replaced) did NOT happen and must
# never enter the graph as an accomplished activity (#461). So `resolved` is gated separately in the
# query, not listed here.
_DONE_STATUSES = ("consumed", "completed")

_MAX_PEOPLE = 200  # cap persons scanned from the past layer
_MAX_ACTIVITIES = 200  # cap done-terminal intents scanned
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
    skipped: int = 0

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
    status: str
    participants: list[str]  # canonical person identities (consolidated only)
    ts: str | None = None  # recognition time — the activity edge's valid_from
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


def _load_terminal_activities(conn, people: _People) -> list[_Activity]:
    """Load durable activities plus the read-only legacy terminal-intent fallback.

    Participants are linked only when already consolidated in person_graph (unknown names are
    skipped, not minted). Typed non-person mentions are matched against the adjudicated typed
    roster for the about leg. A missing legacy intents table must not disable durable activities.
    """
    typed_roster = _load_typed_entities(conn)
    try:
        placeholders = ",".join("?" * len(_DONE_STATUSES))
        conn.row_factory = None
        # `resolved` is DONE only when resolution_outcome='done' (#461): rejected/superseded closes
        # are past facts that never happened, so they must not become participates_in activities.
        rows = conn.execute(
            f"SELECT id, kind, rationale, payload, status, ts FROM intents "
            f"WHERE status IN ({placeholders}) "
            f"OR (status = 'resolved' AND resolution_outcome = 'done') "
            f"ORDER BY ts DESC LIMIT ?",
            (*_DONE_STATUSES, _MAX_ACTIVITIES),
        ).fetchall()
    except Exception:  # noqa: BLE001 — intents table may not exist in a bare install
        logger.debug("relation_extractor: terminal-intent read failed, empty", exc_info=True)
        rows = []

    from .person_graph import _norm

    out: list[_Activity] = []
    for iid, kind, rationale, payload_text, status, ts in rows:
        try:
            payload = json.loads(payload_text or "{}")
        except (TypeError, ValueError):
            payload = {}
        raw_people = payload.get("with") or payload.get("participants") or []
        participants: list[str] = []
        typed_mentions: list[tuple[str, str]] = []
        if isinstance(raw_people, list):
            for name in raw_people:
                norm_name = _norm(str(name))
                canonical = people.aliases.get(norm_name)
                if canonical and canonical not in participants:
                    participants.append(canonical)
                    continue
                typed = typed_roster.get(norm_name)
                if typed and typed not in typed_mentions:
                    typed_mentions.append(typed)
        out.append(
            _Activity(
                identity=f"event:intent:{iid}",
                label=str(kind or "") or "activity",
                quote=(rationale or "").strip() or f"已完成的 {kind} 事项",
                status=str(status),
                participants=participants,
                ts=str(ts) if ts else None,
                source_kind="intent",
                source_id=str(iid),
                source_receipt=f"⟨{iid}:intents⟩",
                typed_mentions=typed_mentions,
            )
        )

    # New installs derive activities from durable event entries and ended sessions,
    # independent of the intent product lifecycle. Legacy intents above remain read-only.
    from ..model.activity_source import ActivitySource

    def resolve_participants(raw_names: list[str], summary: str) -> list[str]:
        from .person_graph import _norm

        candidates = {_norm(name) for name in raw_names if _norm(name)}
        normalized_summary = _norm(summary)
        candidates.update(
            alias for alias in people.aliases if alias and alias in normalized_summary
        )
        return list(
            dict.fromkeys(
                canonical
                for alias in candidates
                if (canonical := people.aliases.get(alias)) is not None
            )
        )

    for event in ActivitySource(
        conn,
        participant_resolver=resolve_participants,
        include_legacy_intents=False,
        limit=_MAX_ACTIVITIES,
    ).events():
        summary_norm = _norm(event.summary)
        typed_mentions = [
            typed for alias, typed in typed_roster.items() if alias and alias in summary_norm
        ]
        out.append(
            _Activity(
                identity=event.stable_id,
                label="activity",
                quote=event.summary,
                status="completed",
                participants=event.participant_ids,
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
) -> None:
    """New evidence → add a shadow edge; existing open edge → ratchet its strength.

    ``observations`` is the caller-computed count of distinct supporting evidence
    (idempotent MAX semantics — re-running over the same data changes nothing).
    Endpoint legality is enforced by ``add_edge`` (raises for an illegal pair) — the
    caller treats a raise as "not a P0 relation" and drops it.
    """
    key = _edge_key(src, dst, predicate.value)
    eid = seen.get(key)
    if eid is not None:
        if edges_store.reinforce_edge(
            conn, edge_id=eid, observations=observations, confidence=confidence, additive=additive
        ):
            tally.reinforced += 1
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
        quote = people.summaries.get(canonical) or f"与 {canonical} 的交互记录"
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
                quote=f"{a} 与 {b} 曾在同一场景出现",
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
                quote=(str(sample or "").strip() or f"{ident} 项目记忆 {count} 条持久事实"),
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
    """DONE-terminal intents → Activity(EVENT) points via ``participates_in`` edges (§4.0 ①).

    provenance: ``consumed`` was the USER acting on it → user_committed; ``resolved``/
    ``completed`` are system-detected → inferred. Each terminal intent is ONE evidence item
    (observations=1); a re-scan is a no-op because the event identity is stable.
    """
    for act in activities:
        prov = "user_committed" if act.status == "consumed" else "inferred"
        conf = 0.9 if prov == "user_committed" else 0.7
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
        # USER (self→event→org), no invented predicate, evidence = the intent.
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
    "你从某人已沉淀的人际交互记录里，只抽取【人与人】之间**明确有据**的关系。"
    "宁缺毋滥：拿不准、或没有原文能佐证的，一律不要输出。"
)


def _llm_prompt(evidence: str, identities: list[str]) -> list[dict[str, Any]]:
    roster = ", ".join(["self=用户本人", *identities])
    instruction = (
        f"实体（只能在这些之间建立关系，src/dst 必须原样取自此列表）：{roster}\n\n"
        "从下面这些人的交互记录中，抽取实体之间的关系，输出一个 JSON 数组，每条形如：\n"
        '{"src": "...", "dst": "...", "predicate": "...", "label": "...", '
        '"quote": "...", "confidence": 0.0}\n'
        "- predicate 只能是：reports_to（src 向 dst 汇报 / dst 是 src 的上级），"
        "knows（一般认识 / 同事 / 客户 / 朋友，方向不敏感）。\n"
        "- quote：从记录里**原文摘录**一句 ≤120 字的支撑证据；摘不出就不要输出这条。\n"
        "- label：一句话自由描述（如「老板」「客户」），可空。\n"
        "- 关系不属于上面两种、或证据不足 → 不输出该条。只输出 JSON 数组，无其他文字。\n\n"
        f"交互记录：\n{evidence}"
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
    use a fresh ``EvoMemory``, ``llm.call_llm`` (Anthropic, mock-aware), and ``fts.cursor``.
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

"Cross-domain synthesis of stable predictive schemas."

from __future__ import annotations

import contextlib
import json
import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import Config
from ..evomem._json import parse_json_object
from ..logger import get
from ..store import entries as entries_mod
from ..store import files as files_mod
from ..store import schema_faces
from ..timeline import store as timeline_store
from . import llm as llm_mod
from . import schema_miner_stage as stage

logger = get("persome.writer")

# Behavior-signature trace window: a fact's ``occurred_at`` ± this many minutes of
# timeline_blocks form its behavior context. Wide enough to catch the surrounding
# activity, narrow enough not to bleed into an unrelated later task.
_TRACE_WINDOW_MINUTES = 15

# Pre-filter thresholds (overridable via SchemaConfig).
_DEFAULT_BEHAVIOR_MAX_DISTANCE = 0.5  # ≤ this behavior distance == "behavior-near"
_DEFAULT_MIN_CONFIDENCE = 0.6  # fused schema below this is born ``forming``
_DEFAULT_MAX_PROBES = 8  # hard per-build LLM-call budget
# Topic token-overlap ceiling: above this the two schemas are too on-topic to be a
# cross-domain pair (the LLM would just dedup them). Generous — source-distinctness
# already does most of the work; this only drops near-identical propositions.
_TOPIC_OVERLAP_MAX = 0.6

# Distance weights: behavior is mostly "which apps + what kind of actions"; time of
# day is a weak tertiary signal.
_W_APPS = 0.4
_W_ACTIONS = 0.4
_W_HOURS = 0.2

_CENTRAL_MARKER = "central:"


@dataclass
class BehaviorSignature:
    """A schema's deterministic behavior fingerprint (no embedding)."""

    apps: frozenset[str] = frozenset()
    action_dist: dict[str, float] = field(default_factory=dict)  # type → normalized freq
    hours: dict[int, float] = field(default_factory=dict)  # hour → normalized freq
    sample_count: int = 0  # timeline_blocks backing this; 0 == ungrounded (don't filter)

    @property
    def grounded(self) -> bool:
        return self.sample_count > 0


@dataclass
class _StableSchema:
    name: str  # e.g. schema-project-x.md
    source_path: str  # e.g. project-x.md (None-ish for non-derivable)
    central: str
    inferences: list[str]
    confidence: float


@dataclass
class CrossSweepResult:
    """Outcome of one :func:`sweep_cross_domain` call."""

    written: list[stage.WrittenSchema] = field(default_factory=list)
    pairs_considered: int = 0
    eligible_pairs: int = 0  # survived deterministic topic/behavior filters
    pairs_probed: int = 0  # survived the behavior pre-filter, sent to the LLM
    probe_limit: int = _DEFAULT_MAX_PROBES
    pairs_deferred: int = 0  # eligible but left for a later build by the hard budget
    collisions: int = 0  # LLM said detected=true

    @property
    def written_count(self) -> int:
        return len(self.written)


# ── behavior signature (deterministic, occurred_at-grounded) ──────────────────


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _normalize(counter: Counter) -> dict[Any, float]:
    total = sum(counter.values())
    if total == 0:
        return {}
    return {k: v / total for k, v in counter.items()}


def _schema_behavior_signature(
    conn: sqlite3.Connection,
    source_path: str,
    *,
    window_minutes: int = _TRACE_WINDOW_MINUTES,
) -> BehaviorSignature:
    """Trace a schema's source facts (via ``occurred_at``) to their timeline behavior.

    Only facts carrying an ``occurred_at`` are traced — a write-time ``timestamp``
    is offset from the actual activity, so filtering on it would manufacture false
    behavior similarity/difference. Ungrounded schemas return an empty signature
    (``sample_count==0``), which the distance function treats as "do not filter".
    """
    try:
        parsed = files_mod.read_file(files_mod.memory_path(source_path))
    except (FileNotFoundError, ValueError):
        return BehaviorSignature()

    apps: Counter = Counter()
    actions: Counter = Counter()
    hours: Counter = Counter()
    n_blocks = 0
    seen_block_ids: set[str] = set()
    for e in parsed.entries:
        if e.superseded_by:
            continue
        dt = _parse_iso(e.occurred_at)
        if dt is None:
            continue
        blocks = timeline_store.query_range(
            conn,
            dt - timedelta(minutes=window_minutes),
            dt + timedelta(minutes=window_minutes),
            limit=200,
        )
        for b in blocks:
            if b.id in seen_block_ids:
                continue
            seen_block_ids.add(b.id)
            n_blocks += 1
            for a in b.apps_used:
                apps[a] += 1
            for act in b.action_trace:
                t = act.get("type") if isinstance(act, dict) else None
                if t:
                    actions[str(t)] += 1
            if b.start_time is not None:
                hours[b.start_time.hour] += 1

    return BehaviorSignature(
        apps=frozenset(apps),
        action_dist=_normalize(actions),
        hours=_normalize(hours),
        sample_count=n_blocks,
    )


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


def _tv_distance(p: dict[Any, float], q: dict[Any, float]) -> float:
    """Total-variation distance between two normalized distributions, in [0, 1]."""
    keys = set(p) | set(q)
    if not keys:
        return 0.0
    return 0.5 * sum(abs(p.get(k, 0.0) - q.get(k, 0.0)) for k in keys)


def _signature_distance(a: BehaviorSignature, b: BehaviorSignature) -> float:
    """Deterministic behavior distance in [0, 1]; 0.0 when either side is ungrounded.

    An ungrounded signature (no ``occurred_at`` facts) yields 0.0 so the caller's
    ``<= max_distance`` pre-filter passes the pair through to the LLM rather than
    filtering on absent data.
    """
    if not a.grounded or not b.grounded:
        return 0.0
    apps_dist = 1.0 - _jaccard(a.apps, b.apps)
    action_dist = _tv_distance(a.action_dist, b.action_dist)
    hours_dist = _tv_distance(a.hours, b.hours)
    return _W_APPS * apps_dist + _W_ACTIONS * action_dist + _W_HOURS * hours_dist


# ── topic distinctness (cheap deterministic guard before the LLM) ─────────────


def _tokens(text: str) -> set[str]:
    out: set[str] = set()
    for raw in text.replace("\n", " ").split():
        tok = "".join(c for c in raw.lower() if c.isalnum())
        if len(tok) > 1:
            out.add(tok)
    return out


def _topic_distinct(a: _StableSchema, b: _StableSchema) -> bool:
    """True when the two schemas are different enough in topic to be a cross pair.

    Source-distinctness is required (the classifier groups by topic into files, so a
    different source file is already a different topic). A token-overlap ceiling on
    the propositions drops the rare near-identical pair that would just dedup.
    """
    if a.source_path == b.source_path:
        return False
    ta = _tokens(a.central + " " + " ".join(a.inferences))
    tb = _tokens(b.central + " " + " ".join(b.inferences))
    if not ta or not tb:
        return True  # nothing to compare → let the LLM decide
    overlap = len(ta & tb) / len(ta | tb)
    return overlap <= _TOPIC_OVERLAP_MAX


# ── load stable base schemas ──────────────────────────────────────────────────


def _parse_field(body: str, marker: str) -> str:
    for raw in body.splitlines():
        line = raw.strip()
        if line.lower().startswith(marker):
            return line[len(marker) :].strip()
    return ""


def _source_of(schema_name: str) -> str:
    """``schema-project-x.md`` → ``project-x.md`` (the file it was mined from)."""
    stem = schema_name.split("/")[-1].removesuffix(".md")
    return stem.removeprefix("schema-") + ".md"


def _load_stable_schemas(conn: sqlite3.Connection) -> list[_StableSchema]:
    """Live, stable, **non-xdomain** schemas — the base material the sweeper pairs.

    Excludes ``schema-xdomain-*`` so a fused schema is never itself re-fused
    (no recursive collisions), and keeps only ``stable`` ones (the authority bar
    used by active model reads).
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT path, tags, content FROM entries "
        "WHERE prefix = 'schema' AND superseded = 0 "
        "ORDER BY timestamp DESC"
    ).fetchall()
    out: list[_StableSchema] = []
    for r in rows:
        name = r["path"]
        if name.startswith("schema-xdomain-"):
            continue
        source_path = _source_of(name)
        # person-* schemas describe collaborators. Cross-domain synthesis is
        # owner-scoped; fusing across subjects turns another person's behavior
        # into an owner belief.
        if source_path.startswith("person-"):
            continue
        tags = (r["tags"] or "").split()
        if "stable" not in tags:
            continue
        body = r["content"] or ""
        central = _parse_field(body, _CENTRAL_MARKER)
        if not central:
            continue
        out.append(
            _StableSchema(
                name=name,
                source_path=source_path,
                central=central,
                inferences=stage.parse_expected_inferences(body),
                confidence=_confidence_of(tags),
            )
        )
    return out


def _confidence_of(tags: list[str]) -> float:
    for t in tags:
        if t.startswith("confidence:"):
            try:
                return float(t.split(":", 1)[1])
            except ValueError:
                return 0.0
    return 0.0


# ── LLM collision judge ───────────────────────────────────────────────────────


@dataclass
class _Collision:
    detected: bool
    central_proposition: str = ""
    supporting_summary: str = ""
    expected_inferences: list[str] = field(default_factory=list)
    confidence: float = 0.0


def _sig_summary(sig: BehaviorSignature) -> str:
    if not sig.grounded:
        return "(no behavior samples)"
    apps = ", ".join(sorted(sig.apps)[:6]) or "—"
    acts = ", ".join(f"{k}:{v:.2f}" for k, v in sorted(sig.action_dist.items())) or "—"
    return f"apps=[{apps}]; actions=[{acts}]; blocks={sig.sample_count}"


def _probe_collision(
    cfg: Config,
    a: _StableSchema,
    b: _StableSchema,
    sig_a: BehaviorSignature,
    sig_b: BehaviorSignature,
    llm_call: Callable[[list[dict]], Any],
) -> _Collision:
    """Ask the LLM whether two topic-distinct, behavior-near schemas collide."""
    prompt = _load_prompt()
    user = (
        f"## Schema A (topic: {a.source_path})\n"
        f"central: {a.central}\n"
        f"inferences:\n" + "\n".join(f"- {x}" for x in a.inferences) + "\n"
        f"behavior: {_sig_summary(sig_a)}\n\n"
        f"## Schema B (topic: {b.source_path})\n"
        f"central: {b.central}\n"
        f"inferences:\n" + "\n".join(f"- {x}" for x in b.inferences) + "\n"
        f"behavior: {_sig_summary(sig_b)}\n\n"
        "Decide whether these schemas share one higher-level mental pattern and return JSON."
    )
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": user},
    ]
    parsed = parse_json_object(_content_of(llm_call(messages)))
    if parsed is None:
        return _Collision(detected=False)
    inferences = parsed.get("expected_inferences") or []
    if not isinstance(inferences, list):
        inferences = []
    return _Collision(
        detected=bool(parsed.get("detected")),
        central_proposition=str(parsed.get("central_proposition", "")),
        supporting_summary=str(parsed.get("supporting_summary", "")),
        expected_inferences=[str(x) for x in inferences],
        confidence=_as_float(parsed.get("confidence")),
    )


def _content_of(resp: Any) -> str:
    try:
        return resp.choices[0].message.content or ""
    except (AttributeError, IndexError):
        return ""


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "cross_domain_sweeper.md"


def _load_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _build_llm_call(cfg: Config) -> Callable[[list[dict]], Any]:
    def _call(messages: list[dict]) -> Any:
        return llm_mod.call_llm(cfg, "cross_domain_sweeper", messages=messages, json_mode=True)

    return _call


def _xdomain_name(a: _StableSchema, b: _StableSchema) -> str:
    """Stable, order-independent fused schema filename so a re-sweep is idempotent."""
    sa = a.name.removeprefix("schema-").removesuffix(".md")
    sb = b.name.removeprefix("schema-").removesuffix(".md")
    lo, hi = sorted((sa, sb))
    slug = f"xdomain-{lo}__{hi}"
    return f"schema-{slug}.md"


def _persist_cross_schema(
    conn: sqlite3.Connection,
    a: _StableSchema,
    b: _StableSchema,
    collision: _Collision,
    *,
    stable_threshold: float,
) -> stage.WrittenSchema | None:
    central = collision.central_proposition.strip()
    if not central:
        return None
    status = stage._status_for(collision.confidence, stable_threshold)
    name = _xdomain_name(a, b)
    body = stage.render_schema_body(
        central_proposition=central,
        supporting_summary=collision.supporting_summary,
        expected_inferences=collision.expected_inferences,
    )
    tags = ["schema", "xdomain", status, f"confidence:{collision.confidence:.2f}"]
    # A still-``forming`` fusion is born/kept ``dormant`` so it stays out of
    # default ``list_memories`` and active model reads until it matures — mirrors
    # the miner's #440 rule. Pre-fix the
    # sweeper dropped the ``status=`` kwarg on ``create_file`` (defaulting to
    # ``active``) and never re-set it on re-sweep, so low-quality fusions born
    # ``forming`` leaked into the default surface (#631 nit P).
    file_status = "dormant" if status == "forming" else "active"

    path = files_mod.memory_path(name)
    updated_in_place = False
    if path.exists():
        old_id = stage._latest_entry_id(name)
        if old_id is not None:
            entries_mod.supersede_entry(
                conn,
                name=name,
                old_entry_id=old_id,
                new_content=body,
                reason="re-swept cross-domain collision",
                tags=tags,
            )
            updated_in_place = True
        else:
            entries_mod.append_entry(conn, name=name, content=body, tags=tags)
        entries_mod.set_file_status(conn, name=name, status=file_status)
    else:
        entries_mod.create_file(
            conn,
            name=name,
            description=central[:120],
            tags=["schema", status],
            status=file_status,
        )
        entries_mod.append_entry(conn, name=name, content=body, tags=tags)

    logger.info(
        "xdomain schema: %s status=%s conf=%.2f%s",
        name,
        status,
        collision.confidence,
        " (updated)" if updated_in_place else "",
    )
    # §4.5 unified schema object, emergent route (footprint first): the fused

    # faces; and the collision itself is an independent behavioral signal on
    # EACH parent — a signal-only contribution (empty members, so the parents'
    # mined footprint history stays untouched) that can escalate the parent's
    # provenance to ``both``. Shadow-only SQLite write, fail-open.
    if status == "stable":
        try:
            # The normal sweep preloads scheduling state, which creates this
            # table as a side effect. Keep the persistence station independently
            # safe as well: direct/recovery callers may land the first stable
            # collision before any scheduling read has run.
            schema_faces.ensure_schema(conn)
            parent_anchors: set[str] = set()
            for parent in (a, b):
                row = schema_faces._find_match(  # noqa: SLF001 — same-module family
                    conn, signature=parent.central, members=set(), level=1
                )
                if row is not None:
                    with contextlib.suppress(TypeError, ValueError):
                        parent_anchors.update(json.loads(row["anchors"] or "[]"))
            body_id = schema_faces.record_face(
                conn,
                source=schema_faces.PROVENANCE_EMERGENT,
                signature=central,
                members=[a.name, b.name],
                confidence=collision.confidence,
                level=2,
                anchors=sorted(parent_anchors),
            )
            schema_faces.maybe_promote(conn, body_id)
            for parent in (a, b):
                pid = schema_faces.record_face(
                    conn,
                    source=schema_faces.PROVENANCE_EMERGENT,
                    signature=parent.central,
                    members=[],
                    confidence=collision.confidence,
                )
                schema_faces.maybe_promote(conn, pid)
        except Exception:
            logger.exception("schema_faces record failed for %s", name)
            raise
    return stage.WrittenSchema(
        path=name,
        status=status,
        confidence=collision.confidence,
        expected_inferences=list(collision.expected_inferences),
        updated_in_place=updated_in_place,
    )


@dataclass(frozen=True)
class _CandidatePair:
    """One deterministic, pre-filtered cross-domain probe candidate."""

    a: _StableSchema
    b: _StableSchema
    sig_a: BehaviorSignature
    sig_b: BehaviorSignature
    priority: int
    last_probed_at: str | None = None

    @property
    def sort_key(self) -> tuple[int, str, str, str]:
        lo, hi = sorted((self.a.name, self.b.name))
        return (self.priority, self.last_probed_at or "", lo, hi)

    @property
    def pair_key(self) -> str:
        return _pair_key(self.a.name, self.b.name)


def _pair_key(a_name: str, b_name: str) -> str:
    return json.dumps(sorted((a_name, b_name)), ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class _ProbeHistory:
    """Last persisted scheduling result for one pair.

    ``promotable`` means the last probe produced stable evidence that was
    eligible for the Volume promotion path. A negative, failed, or forming
    result is false and must not leave an old shadow at highest priority.
    """

    last_probed_at: str
    promotable: bool


def _probe_history(conn: sqlite3.Connection) -> dict[str, _ProbeHistory]:
    schema_faces.ensure_schema(conn)
    return {
        str(row[0]): _ProbeHistory(last_probed_at=str(row[1]), promotable=bool(row[2]))
        for row in conn.execute(
            "SELECT pair_key, last_probed_at, detected FROM cross_domain_probe_state"
        ).fetchall()
    }


def _record_probe(candidate: _CandidatePair, conn: sqlite3.Connection, *, detected: bool) -> None:
    conn.execute(
        "INSERT INTO cross_domain_probe_state"
        " (pair_key, last_probed_at, probe_count, detected) VALUES (?, ?, 1, ?)"
        " ON CONFLICT(pair_key) DO UPDATE SET"
        " last_probed_at=excluded.last_probed_at,"
        " probe_count=cross_domain_probe_state.probe_count + 1,"
        " detected=excluded.detected",
        (
            candidate.pair_key,
            datetime.now(UTC).isoformat(),
            1 if detected else 0,
        ),
    )


def _record_probe_fail_open(
    candidate: _CandidatePair,
    conn: sqlite3.Connection,
    *,
    detected: bool,
) -> None:
    try:
        _record_probe(candidate, conn, detected=detected)
    except Exception:  # pragma: no cover - scheduling metadata must not abort the stage
        logger.exception("cross-domain probe history write failed for %s", candidate.pair_key)


def _live_volume_pair_statuses(conn: sqlite3.Connection) -> dict[frozenset[str], str]:
    """Map live two-schema Volume footprints to ``shadow`` or ``active``.

    The cross-domain sweeper is the honest producer for level-2 objects. A live
    shadow pair needs another independent sweep observation before promotion, so
    it is the most valuable use of a bounded probe budget.
    """
    schema_faces.ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT members, status FROM schema_faces "
        "WHERE level = 2 AND valid_to IS NULL AND status IN ('shadow', 'active')"
    ).fetchall()
    statuses: dict[frozenset[str], str] = {}
    for row in rows:
        try:
            members = frozenset(str(member) for member in json.loads(row["members"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if len(members) != 2:
            continue
        # A shadow row always wins if a damaged/legacy store happens to contain
        # duplicate live footprints: it is the one that still needs evidence.
        status = str(row["status"])
        if status == "shadow" or members not in statuses:
            statuses[members] = status
    return statuses


def _candidate_priority(
    a: _StableSchema,
    b: _StableSchema,
    volume_statuses: dict[frozenset[str], str],
    probe_history: dict[str, _ProbeHistory],
) -> int:
    pair_key = _pair_key(a.name, b.name)
    history = probe_history.get(pair_key)
    status = volume_statuses.get(frozenset((a.name, b.name)))
    if status == "shadow" and (history is None or history.promotable):
        return 0
    if status is None and history is None:
        return 1
    if status != "active":
        return 2
    return 3  # active pairs refresh after corroborating, unseen, and rejected pairs


# ── main entry ────────────────────────────────────────────────────────────────


def sweep_cross_domain(
    cfg: Config,
    conn: sqlite3.Connection,
    *,
    behavior_max_distance: float = _DEFAULT_BEHAVIOR_MAX_DISTANCE,
    min_confidence: float = _DEFAULT_MIN_CONFIDENCE,
    max_probes: int = _DEFAULT_MAX_PROBES,
    llm_call: Callable[[list[dict]], Any] | None = None,
) -> CrossSweepResult:
    """Pair stable schemas, behavior-prefilter, LLM-judge collisions, land fusions.

    Behavior signatures pre-filter pairs cheaply (behavior-near + topic-distinct);
    only survivors hit the LLM, up to ``max_probes`` per call. Shadows whose
    last observation was still promotable are retried first, then unseen pairs,
    then negative/forming/failed retries oldest-first, and finally active pairs.
    This prevents a stale shadow from permanently consuming a bounded budget.
    Ties use schema filenames for deterministic ordering. ``llm_call`` is
    injectable for tests. A per-pair failure is logged and skipped — the sweep
    never aborts (schema is a decoration, must not cascade).
    """
    probe_limit = max(0, int(max_probes))
    schemas = _load_stable_schemas(conn)
    result = CrossSweepResult(probe_limit=probe_limit)
    if len(schemas) < 2:
        return result

    call = llm_call if llm_call is not None else _build_llm_call(cfg)
    # Cache signatures so each schema is traced once, not once per pair.
    sigs: dict[str, BehaviorSignature] = {
        s.name: _schema_behavior_signature(conn, s.source_path) for s in schemas
    }
    volume_statuses = _live_volume_pair_statuses(conn)
    probe_history = _probe_history(conn)
    candidates: list[_CandidatePair] = []

    for i in range(len(schemas)):
        for j in range(i + 1, len(schemas)):
            # Normalize A/B as well as queue order. The entries query is newest
            # first, so insertion timestamps must not change prompt orientation.
            a, b = sorted((schemas[i], schemas[j]), key=lambda schema: schema.name)
            result.pairs_considered += 1
            if not _topic_distinct(a, b):
                continue
            sig_a, sig_b = sigs[a.name], sigs[b.name]
            if _signature_distance(sig_a, sig_b) > behavior_max_distance:
                continue  # behavior too different — not a cross-domain twin
            history = probe_history.get(_pair_key(a.name, b.name))
            candidates.append(
                # The history table is intentionally consulted once per build:
                # each result affects the next scheduling pass, never the order
                # of candidates already selected in this one.
                _CandidatePair(
                    a=a,
                    b=b,
                    sig_a=sig_a,
                    sig_b=sig_b,
                    priority=_candidate_priority(a, b, volume_statuses, probe_history),
                    last_probed_at=history.last_probed_at if history is not None else None,
                )
            )

    candidates.sort(key=lambda candidate: candidate.sort_key)
    result.eligible_pairs = len(candidates)
    result.pairs_deferred = max(0, len(candidates) - probe_limit)

    for candidate in candidates[:probe_limit]:
        result.pairs_probed += 1
        a, b = candidate.a, candidate.b
        try:
            collision = _probe_collision(cfg, a, b, candidate.sig_a, candidate.sig_b, call)
        except Exception:  # pragma: no cover - defensive; one bad pair can't kill the sweep
            logger.exception("cross-domain probe failed on %s × %s", a.name, b.name)
            _record_probe_fail_open(candidate, conn, detected=False)
            continue
        if not collision.detected:
            _record_probe_fail_open(candidate, conn, detected=False)
            continue
        result.collisions += 1
        try:
            written = _persist_cross_schema(
                conn,
                a,
                b,
                collision,
                stable_threshold=min_confidence,
            )
        except Exception:  # pragma: no cover - one failed projection must not monopolize the queue
            logger.exception("cross-domain persistence failed on %s × %s", a.name, b.name)
            _record_probe_fail_open(candidate, conn, detected=False)
            continue
        # The historical column is named ``detected`` for compatibility. Its
        # scheduling meaning is stricter: only stable output is promotable.
        _record_probe_fail_open(
            candidate,
            conn,
            detected=written is not None and written.status == "stable",
        )
        if written is not None:
            result.written.append(written)

    if result.pairs_deferred:
        logger.info(
            "cross-domain probe budget reached: probed=%d eligible=%d deferred=%d limit=%d",
            result.pairs_probed,
            result.eligible_pairs,
            result.pairs_deferred,
            result.probe_limit,
        )
    return result

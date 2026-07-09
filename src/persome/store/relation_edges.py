"""DAO for ``relation_edges`` — the横轴 relation layer of the user-centric graph memory.

Spec: ``docs/superpowers/specs/2026-07-01-user-centric-relation-graph-memory-design.md``
§4.2 (predicate closed set + ``src×dst`` completeness table) and §4.6 (DDL + as-of-T
traversal). This is P0-1 (#427): schema + write entrances + an as-of-T read helper.

**Why a separate table (§2.5/§2.6).** evomem (``evo_nodes`` + SUPERSEDE chains) is the
*vertical / temporal* axis — each node's own version history. This table is the ORTHOGONAL
*horizontal / relational* axis: first-class **directed relation edges BETWEEN entities**
(person / org / project / event / artifact). An edge addresses stable *identities*
(person_graph canonical name / project slug), NEVER a specific version node — evomem resolves
an identity to its as-of-T state (that is the §2.5 interface; not this module's job).

**Persistence discipline (§2.6, §5) — this table is a persistent (bitemporal) graph.**

- *append-only*: rows are never physically deleted. A relationship ending is
  :func:`close_edge` (stamps ``valid_to``), and ``created_at`` (transaction time) is immutable.
- *two time dimensions*: ``created_at`` = when Persome learned the edge (the persistence /
  version axis, monotonic); ``valid_from`` / ``valid_to`` = when the fact holds in the world
  (valid-time). :func:`edges_as_of` filters on **valid-time**.

**Default is inert.** :func:`add_edge` writes ``status='shadow'`` by default, so extracted
edges reach neither retrieval nor the digest until proven (§4.3). This module wires no
retrieval; ``edges_as_of`` is the read primitive P0-3 / P1 build on.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum

from ..evomem.models import MemoryStatus
from ..logger import get

logger = get("persome.store.relation_edges")


class EntityKind(StrEnum):
    """节点种类闭集（§4.1）——覆盖「人事物」，用于端点合法性校验。"""

    SELF = "self"
    PERSON = "person"
    ORG = "org"
    PROJECT = "project"
    EVENT = "event"
    ARTIFACT = "artifact"


class Predicate(StrEnum):
    """关系谓词闭集（§4.2）。**两层**（transformer 类比：原始 attention + 多头类型）：

    - **① 关联地板** ``engaged_with`` —— 均匀·无类型·稠密的「碰过/关注过」关系（= 原始
      attention）。共现即建、确定性零 LLM、**kind 无关**（self 关联任何东西）。连通性只靠这层，
      永远完备（共现普适）；kind 判错只错②层标签，永不掉孤儿（attention 不看词性）。
    - **② 语义结构** 其余 6 谓词 —— LLM 给强关联贴的类型标签（= 多头学到的关系）。

    具体语义走开集 ``label``，不进谓词。"""

    ENGAGED_WITH = "engaged_with"  # ① 关联地板（attention 基底）
    PARTICIPATES_IN = "participates_in"
    PART_OF = "part_of"
    REPORTS_TO = "reports_to"
    KNOWS = "knows"
    ABOUT = "about"
    DEPENDS_ON = "depends_on"


PROVENANCE: frozenset[str] = frozenset({"user_committed", "inferred"})

_K = EntityKind


def _pairs(
    srcs: set[EntityKind], dsts: set[EntityKind]
) -> frozenset[tuple[EntityKind, EntityKind]]:
    return frozenset((s, d) for s in srcs for d in dsts)


# §4.2 完备表的规范方向合法端点：predicate -> 允许的 (src_kind, dst_kind) 集合。
# 反向（``↩``）不在这里 —— 每条 tie 只存一个规范方向，反向靠 dst 索引查（§4.6）。
# 全部 dst kind（关联地板 ``engaged_with`` 的规范 dst——kind 无关，故列全）。
_ALL_DST = {_K.PERSON, _K.ORG, _K.PROJECT, _K.EVENT, _K.ARTIFACT}
_LEGAL_ENDPOINTS: dict[Predicate, frozenset[tuple[EntityKind, EntityKind]]] = {
    # ① 关联地板：agent（含 self）→ 任何事物。均匀无类型；连通性靠它，永远完备。
    Predicate.ENGAGED_WITH: _pairs({_K.SELF, _K.PERSON, _K.ORG}, _ALL_DST),
    Predicate.PARTICIPATES_IN: _pairs({_K.SELF, _K.PERSON, _K.ORG}, {_K.PROJECT, _K.EVENT}),
    Predicate.PART_OF: (
        _pairs({_K.SELF, _K.PERSON, _K.ORG}, {_K.ORG})  # O→O 组织嵌套（部门 part_of 公司）
        | _pairs({_K.PROJECT}, {_K.ORG})
        | _pairs({_K.ARTIFACT}, {_K.PROJECT})
    ),
    Predicate.REPORTS_TO: _pairs({_K.SELF, _K.PERSON}, {_K.PERSON}),
    Predicate.KNOWS: _pairs({_K.SELF, _K.PERSON}, {_K.PERSON}),
    Predicate.ABOUT: _pairs({_K.EVENT, _K.ARTIFACT}, {_K.PROJECT, _K.PERSON, _K.ORG, _K.EVENT}),
    Predicate.DEPENDS_ON: _pairs(
        {_K.PROJECT, _K.EVENT, _K.ARTIFACT}, {_K.PROJECT, _K.EVENT, _K.ARTIFACT}
    ),
}


SCHEMA = """
CREATE TABLE IF NOT EXISTS relation_edges (
    edge_id      TEXT PRIMARY KEY,
    src_identity TEXT NOT NULL,          -- 稳定 identity（person_graph 规范名 / PROJECT slug），非版本 node_id
    dst_identity TEXT NOT NULL,
    predicate    TEXT NOT NULL,          -- 6 谓词闭集之一
    label        TEXT,                   -- 自由文本关系（开集语义）
    valid_from   TEXT NOT NULL,          -- 有效时间起（ISO8601）
    valid_to     TEXT,                   -- NULL = 当前有效
    provenance   TEXT NOT NULL,          -- 'user_committed' | 'inferred'
    confidence   REAL NOT NULL,
    quote        TEXT,                   -- ≤120 字支撑原文（证据）
    status       TEXT NOT NULL,          -- MemoryStatus: 'shadow'|'active'|'superseded'|'archived'
    created_at   TEXT NOT NULL,          -- 事务时间（版本轴，不可变）
    observations INTEGER NOT NULL DEFAULT 1,  -- 强度=支撑该关系的证据数（event 蒸馏计数，单调不减）
    last_observed_at TEXT,              -- 最近一次证据强化时刻（ISO8601）
    recall_count INTEGER NOT NULL DEFAULT 0  -- 读也是强化（§3.3 testing effect）：树链走过这条边即 +1
);
CREATE INDEX IF NOT EXISTS ix_edges_src ON relation_edges(src_identity, valid_from);
CREATE INDEX IF NOT EXISTS ix_edges_dst ON relation_edges(dst_identity, valid_from);
"""

# Edge polarity closed set (§1.2 边状态轴): deterministic extractors always
# stamp neutral '0'; the LLM relation pass may stamp ± when the quote carries
# sentiment. Rendered by the §7-6 graph view's 极性 lens.
POLARITIES = frozenset({"+", "-", "0"})

# Columns added after the first shipped schema — ensure_schema back-fills them on old DBs.
_EXTRA_COLUMNS: tuple[tuple[str, str], ...] = (
    ("observations", "INTEGER NOT NULL DEFAULT 1"),
    ("last_observed_at", "TEXT"),
    ("recall_count", "INTEGER NOT NULL DEFAULT 0"),
    # §7-6 graph-projection axes: kinds were validated at add_edge but never
    # persisted, so the node 种类 axis (PERSON/ORG/PROJECT/EVENT/…) could not
    # be recovered from the table; polarity had no storage at all.
    ("src_kind", "TEXT"),
    ("dst_kind", "TEXT"),
    ("polarity", "TEXT NOT NULL DEFAULT '0'"),
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    have = {row[1] for row in conn.execute("PRAGMA table_info(relation_edges)").fetchall()}
    for name, decl in _EXTRA_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE relation_edges ADD COLUMN {name} {decl}")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def add_edge(
    conn: sqlite3.Connection,
    *,
    src_identity: str,
    dst_identity: str,
    predicate: str | Predicate,
    src_kind: str | EntityKind,
    dst_kind: str | EntityKind,
    provenance: str,
    confidence: float,
    label: str | None = None,
    quote: str | None = None,
    valid_from: str | None = None,
    status: str | MemoryStatus = MemoryStatus.SHADOW,
    created_at: str | None = None,
    edge_id: str | None = None,
    observations: int = 1,
    polarity: str = "0",
) -> str:
    """Append one relation edge. Returns its ``edge_id``.

    Deterministic, no LLM. Every input is validated against the §4.2 closed sets — an
    illegal predicate, an illegal ``(src_kind, dst_kind)`` for that predicate, an unknown
    provenance, an out-of-range confidence, or an empty identity all raise ``ValueError``
    (the caller is expected to have made a decision; we do not silently coerce).

    Defaults to ``status='shadow'`` so the edge is inert until proven (§4.3).
    """
    ensure_schema(conn)

    # Closed-set validation (§4.2) — StrEnum(...) raises ValueError for anything off-set.
    pred = Predicate(str(predicate))
    sk = EntityKind(str(src_kind))
    dk = EntityKind(str(dst_kind))
    if (sk, dk) not in _LEGAL_ENDPOINTS[pred]:
        raise ValueError(
            f"relation_edges: illegal endpoints {sk.value}->{dk.value} for predicate "
            f"{pred.value} (§4.2 completeness table)"
        )
    prov = str(provenance)
    if prov not in PROVENANCE:
        raise ValueError(
            f"relation_edges: unknown provenance {prov!r} (expected one of {sorted(PROVENANCE)})"
        )
    conf = float(confidence)
    if not 0.0 <= conf <= 1.0:
        raise ValueError(f"relation_edges: confidence {conf} out of [0,1]")
    src = str(src_identity).strip()
    dst = str(dst_identity).strip()
    if not src or not dst:
        raise ValueError("relation_edges: src_identity / dst_identity must be non-empty")
    st = MemoryStatus(str(status))

    obs = int(observations)
    if obs < 1:
        raise ValueError(f"relation_edges: observations {obs} must be >= 1")
    pol = str(polarity)
    if pol not in POLARITIES:
        raise ValueError(f"relation_edges: polarity {pol!r} not in {sorted(POLARITIES)}")

    eid = edge_id or uuid.uuid4().hex
    vf = valid_from or _now_iso()
    ts = created_at or _now_iso()
    conn.execute(
        """
        INSERT INTO relation_edges
            (edge_id, src_identity, dst_identity, predicate, label, valid_from,
             valid_to, provenance, confidence, quote, status, created_at, observations,
             src_kind, dst_kind, polarity, last_observed_at)
        VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            eid,
            src,
            dst,
            pred.value,
            label,
            vf,
            prov,
            conf,
            quote,
            st.value,
            ts,
            obs,
            sk.value,
            dk.value,
            pol,
            vf,  # birth stamp: last observed = the first evidence moment
        ),
    )
    conn.commit()
    return eid


def close_edge(conn: sqlite3.Connection, *, edge_id: str, at: str | None = None) -> bool:
    """Close a relation's valid-time interval (stamp ``valid_to``). Append-only: it fires once
    (``WHERE valid_to IS NULL`` refuses a re-close / reopen) and never touches ``created_at``.
    Together with :func:`reinforce_edge` these are the only TWO mutations this table allows —
    both monotone (close happens once; observations only grow). Returns whether a row closed.
    """
    ensure_schema(conn)
    ts = at or _now_iso()
    cur = conn.execute(
        "UPDATE relation_edges SET valid_to = ? WHERE edge_id = ? AND valid_to IS NULL",
        (ts, edge_id),
    )
    conn.commit()
    return cur.rowcount > 0


def close_edges_quoted_in(
    conn: sqlite3.Connection, content: str, *, at: str | None = None
) -> list[str]:
    """Close every open edge whose evidence ``quote`` is a substring of
    ``content`` — the §4.6 human-adjudication leg: when a contradiction verdict
    retires a fact entry, the relations that entry evidenced end WITH it.
    Deterministic, bounded, idempotent (already-closed edges don't match the
    ``valid_to IS NULL`` guard); returns the closed edge_ids. Fail-open on an
    empty/whitespace content (closes nothing — never mass-close on bad input).
    """
    ensure_schema(conn)
    hay = (content or "").strip()
    if not hay:
        return []
    ts = at or _now_iso()
    closed: list[str] = []
    for row in conn.execute(
        "SELECT edge_id, quote FROM relation_edges WHERE valid_to IS NULL"
        " AND quote IS NOT NULL AND quote != ''"
    ).fetchall():
        if str(row[1]).strip() and str(row[1]).strip() in hay:
            conn.execute(
                "UPDATE relation_edges SET valid_to = ? WHERE edge_id = ? AND valid_to IS NULL",
                (ts, row[0]),
            )
            closed.append(row[0])
    if closed:
        conn.commit()
        logger.info("close_edges_quoted_in: closed %d edges", len(closed))
    return closed


def reinforce_edge(
    conn: sqlite3.Connection,
    *,
    edge_id: str,
    observations: int,
    confidence: float | None = None,
    at: str | None = None,
    additive: bool = False,
) -> bool:
    """Monotone evidence reinforcement: raise an OPEN edge's strength to ``observations``.

    Strength = number of distinct supporting evidence items (event 蒸馏计数). The caller
    computes the count FROM the evidence itself and this sets ``observations =
    MAX(current, given)`` — so re-running the extraction over the same data is a no-op
    (idempotent), while genuinely new evidence raises it.

    ``additive=True`` switches to **increment** semantics (``observations += given``) for
    the ① ``engaged_with`` attention floor: each session that re-engages an entity is a
    NEW piece of evidence, so the floor's strength must ACCUMULATE = distinct-session
    count = attention weight. MAX-of-1 (the caller passing 1 every session) would freeze
    it at 1 (the point-layer bug); increment fixes it. Callers must fire once per session
    (the session-end callback does); a re-run repair is the deterministic recompute from
    ``memory_deltas`` distinct-session count. ``confidence`` **likewise only
    ratchets up (MAX), INDEPENDENTLY of whether ``observations`` grew** (issue #453): the
    two axes move on their own gates, so a caller that keeps ``observations`` pinned (the
    LLM `reports_to` pass and the activity pass both default `observations=1`) can still
    lift the edge's confidence with stronger evidence. Never touches ``created_at`` /
    ``valid_from``; refuses closed edges. Returns True iff the strength actually grew on
    EITHER axis (observations rose OR confidence ratcheted up).
    """
    ensure_schema(conn)
    obs = int(observations)
    if obs < 1:
        raise ValueError(f"relation_edges: observations {obs} must be >= 1")
    conf = None
    if confidence is not None:
        conf = float(confidence)
        if not 0.0 <= conf <= 1.0:
            raise ValueError(f"relation_edges: confidence {conf} out of [0,1]")
    ts = at or _now_iso()
    # Confidence ratchet — gated ONLY on confidence actually rising, NOT on observations
    # growth (the #453 bug: the MAX used to ride the `observations < ?` UPDATE, so a
    # never-growing observations count froze confidence at its first-seen value). Runs only
    # when a confidence is supplied; `confidence < ?` makes rowcount>0 mean it truly grew,
    # and never ratchets down (the column is NOT NULL, so no NULL edge case).
    conf_grew = False
    if conf is not None:
        conf_cur = conn.execute(
            "UPDATE relation_edges SET confidence = MAX(confidence, ?), last_observed_at = ? "
            "WHERE edge_id = ? AND valid_to IS NULL AND confidence < ?",
            (conf, ts, edge_id, conf),
        )
        conf_grew = conf_cur.rowcount > 0
    # Observations ratchet — MAX (idempotent) by default; additive (increment) for the ① floor.
    if additive:
        obs_cur = conn.execute(
            "UPDATE relation_edges SET observations = observations + ?, last_observed_at = ? "
            "WHERE edge_id = ? AND valid_to IS NULL",
            (obs, ts, edge_id),
        )
    else:
        obs_cur = conn.execute(
            "UPDATE relation_edges SET observations = ?, last_observed_at = ? "
            "WHERE edge_id = ? AND valid_to IS NULL AND observations < ?",
            (obs, ts, edge_id, obs),
        )
    conn.commit()
    return obs_cur.rowcount > 0 or conf_grew


def edges_as_of(
    conn: sqlite3.Connection,
    identities: Iterable[str],
    *,
    as_of: str | None = None,
    status: str | MemoryStatus = MemoryStatus.ACTIVE,
) -> list[sqlite3.Row]:
    """Edges touching any of ``identities`` that are **valid at ``as_of``** (default now) —
    the first hop of the §4.6 traversal.

    Valid-time filter: ``valid_from <= as_of AND (valid_to IS NULL OR as_of < valid_to)``.
    ``status`` defaults to ``active`` so ``shadow`` edges stay out of any traversal — which is
    what keeps P0 extraction (writes ``shadow``) inert against retrieval.
    """
    ensure_schema(conn)
    ids = [str(i).strip() for i in identities if str(i).strip()]
    if not ids:
        return []
    ts = as_of or _now_iso()
    st = MemoryStatus(str(status)).value
    placeholders = ",".join("?" * len(ids))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"""
        SELECT * FROM relation_edges
        WHERE status = ?
          AND (src_identity IN ({placeholders}) OR dst_identity IN ({placeholders}))
          AND valid_from <= ?
          AND (valid_to IS NULL OR ? < valid_to)
        ORDER BY valid_from
        """,
        (st, *ids, *ids, ts, ts),
    ).fetchall()
    return list(rows)


def neighbors(
    conn: sqlite3.Connection,
    seeds: Iterable[str],
    *,
    depth: int = 2,
    as_of: str | None = None,
    status: str | MemoryStatus = MemoryStatus.ACTIVE,
    include_shadow: bool = False,
) -> set[str]:
    """Identities reachable within ``depth`` hops of ``seeds`` via edges valid
    at ``as_of`` — the §3.3 relation head's traversal primitive (the production
    twin of the eval prototype in ``tests/eval/graph_retrieval_eval.py``).

    Returns the REACHED identities only (seeds excluded). ``status`` defaults
    to ``active``, so shadow edges stay out of retrieval (§3.3 策略闸) — with
    today's extraction writing shadow-only, production traversal honestly
    reaches nothing until edges are proven and promoted. ``include_shadow``
    additionally walks shadow edges (§7-3 gain unlock: audited-clean shadow
    may vote, downweighted by the caller — see ``fts.search_associative``).
    """
    frontier = {str(s).strip() for s in seeds if str(s).strip()}
    seen = set(frontier)
    reached: set[str] = set()
    for _ in range(max(0, depth)):
        if not frontier:
            break
        rows = list(edges_as_of(conn, frontier, as_of=as_of, status=status))
        if include_shadow:
            # §7-3 关系头喂食: structural quality is proven (edge-audit 0%
            # hallucination on the full shadow population), so shadow edges may
            # join TRAVERSAL when the caller opts in — retrieval then downweights
            # the shadow-reached pool (fts), it never equals ACTIVE.
            rows += list(edges_as_of(conn, frontier, as_of=as_of, status=MemoryStatus.SHADOW))
        nxt: set[str] = set()
        for r in rows:
            for end in (r["src_identity"], r["dst_identity"]):
                if end not in seen:
                    nxt.add(end)
        reached |= nxt
        seen |= nxt
        frontier = nxt
    return reached


def promote_edges(
    conn: sqlite3.Connection,
    *,
    min_observations: int = 3,
    max_per_identity: int = 20,
    predicate: str = "knows",
) -> int:
    """Promote shadow edges to ACTIVE — the §3.3 边转正判据 (memory-rebuild
    §7-3, designed WITH the RRF pool weights, PR #504 finding).

    Two gates, and the second is the load-bearing one:

    1. **Evidence floor** — ``observations ≥ min_observations``: a
       once-co-occurred pair is not a proven relation.
    2. **Fan-out cap** — per source identity, only the TOP ``max_per_identity``
       strongest edges (by observations, then recency) promote. The cutover
       A/B showed naive threshold promotion makes retrieval WORSE (slotted
       bucket −8~−12pp): the relation head expands EVERY active neighbor into
       a contains-pool, so promotion volume IS dilution volume. A naive
       threshold promotes a hub's entire adjacency; the cap bounds the
       expansion fan-out by construction — the strongest relations are the
       ones worth spreading activation through, exactly the §3.1 residency
       logic (top-K by evidence) applied to edges.

    Idempotent (already-ACTIVE edges count against their identity's cap but
    are not rewritten). Never demotes. Returns the number promoted.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT edge_id, src_identity, observations, status FROM relation_edges"
        " WHERE predicate = ? AND valid_to IS NULL AND observations >= ?"
        " ORDER BY src_identity, observations DESC, created_at DESC",
        (predicate, min_observations),
    ).fetchall()
    promoted = 0
    taken: dict[str, int] = {}
    for row in rows:
        src = row["src_identity"]
        if taken.get(src, 0) >= max_per_identity:
            continue
        taken[src] = taken.get(src, 0) + 1
        if row["status"] == MemoryStatus.ACTIVE.value:
            continue  # occupies a cap slot, already promoted
        conn.execute(
            "UPDATE relation_edges SET status = ? WHERE edge_id = ?",
            (MemoryStatus.ACTIVE.value, row["edge_id"]),
        )
        promoted += 1
    return promoted


def bump_recall(conn: sqlite3.Connection, edge_ids: Iterable[str]) -> None:
    """读也是强化 (§3.3, KV-cache-H2O/testing-effect transfer): every edge a
    delivered tree chain walked gets ``recall_count += 1`` — the read side of
    the consolidation axis (``observations`` is the write side). Feeds the
    strength bias and, later, tiered forgetting (an often-recalled edge resists
    down-precision). Best-effort by contract: callers treat failure as a no-op.
    """
    ids = [str(e).strip() for e in edge_ids if str(e).strip()]
    if not ids:
        return
    ensure_schema(conn)
    conn.executemany(
        "UPDATE relation_edges SET recall_count = recall_count + 1 WHERE edge_id = ?",
        [(eid,) for eid in ids],
    )

"""Semantic fact-space layout precompute for the /model 3D explorer.

Reads the user's own ``index.db`` (read-only, fail-open per table) and writes
``<root>/sem_facts.json`` — the payload ``routes.model_graph`` surfaces as
``sem_geo`` and the model explorer's ``renderSemSpace`` renders as the unified
semantic fact-space: a fact point cloud (XZ = semantic layout, brightness ∝
connection degree) + k-NN connections + emergent faces (spatial-cluster convex
hulls linked to a trunk apex) + Y-as-emergence-level, with the bottom as-of
slider replaying deposition over time.

Output schema (all consumed fail-open by the JS renderer)::

    {
      "facts": [{"i": int, "x": float, "y": float, "z": float,
                 "t": str, "t2": str}, ...],   # y = 0-1 normalized deposition time
      "edges": [[i, j, w], ...],               # undirected, i < j
      "faces": [{"id": str, "sig": str, "level": int, "members": [int]}, ...],
      "edge_source": "vectors" | "graph",      # honest provenance of the edges
    }

with ``x``/``z`` in [-1, 1]. ``t`` is the fact text (label/hover), ``t2`` the
raw deposition timestamp (hover detail).

Design decisions (mirrors the geometry model, memory-rebuild spec §7):

- **Facts** = live ``evo_nodes`` (``is_latest=1 AND status='active'``) — the
  geometry's 点 set, same definition as ``replay/rebuild.py:geometry_snapshot``.
  Capped at ``MAX_FACTS`` newest by deposition time.
- **Edges** — vectors first: facts with an ``entry_vectors`` embedding get
  cosine k-NN (k=6, sim ≥ 0.5). A store with no embeddings (hybrid retrieval
  off / never configured) falls back to the symbolic graph: live
  ``schema_faces`` co-membership pairwise + ACTIVE ``relation_edges`` bridging
  facts that mention the two endpoint identities (both w=0.6). The payload's
  ``edge_source`` labels which path produced the edges — never pretend graph
  edges are semantic similarity.
- **Faces** = Louvain communities (networkx, seed=42) over the edge graph with
  ≥ ``FACE_MIN_MEMBERS`` members; signature = the highest-degree member fact's
  text (first 40 chars).
- **Layout** — two levels: the community supergraph (edge weight = summed
  cross-community edge weight) is laid out by a hand-written numpy
  Fruchterman–Reingold (300 iterations, no scipy dependency) to place cluster
  centers; members scatter sqrt-uniformly on a golden-angle disk around their
  center (radius ≤ 0.42 × the minimum center distance); isolated facts ring
  the outside. Fully deterministic (seeded RNG, stable sorts) so reruns on an
  unchanged store are byte-identical.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime
from itertools import combinations, islice
from pathlib import Path
from typing import Any

import numpy as np

MAX_FACTS = 2000
FACT_TEXT_MAX = 160
KNN_K = 6
KNN_MIN_SIM = 0.5
GRAPH_EDGE_W = 0.6
FACE_MIN_MEMBERS = 5
FR_ITERATIONS = 300
GOLDEN_ANGLE = 2.399963229728653  # radians — phyllotaxis disk scatter
_MAX_PAIRS_PER_FACE = 400  # bound co-membership fan-out on huge faces
_MAX_MENTIONS_PER_SIDE = 3  # bound relation-edge bridging fan-out


# ── DB reads (each fail-open: missing table → empty) ─────────────────────────
def _load_facts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Live evo_nodes (the geometry's 点), newest ``MAX_FACTS`` by deposition time."""
    try:
        rows = conn.execute(
            "SELECT node_id, content, COALESCE(memory_at, gmt_created, '') AS ts"
            " FROM evo_nodes WHERE is_latest = 1 AND status = 'active'"
            " ORDER BY ts DESC, node_id DESC LIMIT ?",
            (MAX_FACTS,),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [{"node_id": r[0], "text": (r[1] or "").strip(), "ts": r[2] or ""} for r in rows]


def _load_vectors(conn: sqlite3.Connection, node_ids: list[str]) -> dict[str, np.ndarray]:
    """entry_vectors for the given node ids (evo_nodes share the entry id space),
    L2-normalized, majority dim only (a mid-migration store may mix models)."""
    if not node_ids:
        return {}
    try:
        rows = conn.execute("SELECT entry_id, dim, vector FROM entry_vectors").fetchall()
    except sqlite3.Error:
        return {}
    wanted = set(node_ids)
    by_dim: dict[int, dict[str, np.ndarray]] = {}
    for entry_id, dim, blob in rows:
        if entry_id not in wanted or not blob:
            continue
        vec = np.frombuffer(blob, dtype="<f4")
        if len(vec) != dim:
            continue
        by_dim.setdefault(int(dim), {})[entry_id] = vec
    if not by_dim:
        return {}
    majority = max(by_dim.items(), key=lambda kv: (len(kv[1]), kv[0]))[1]
    out: dict[str, np.ndarray] = {}
    for entry_id, vec in majority.items():
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            out[entry_id] = (vec / norm).astype(np.float32)
    return out


def _knn_edges(facts: list[dict[str, Any]], vecs: dict[str, np.ndarray]) -> list[list[float]]:
    """Cosine k-NN (k=KNN_K, sim ≥ KNN_MIN_SIM) over the vectored facts."""
    idxs = [i for i, f in enumerate(facts) if f["node_id"] in vecs]
    if len(idxs) < 2:
        return []
    mat = np.stack([vecs[facts[i]["node_id"]] for i in idxs])  # (m, d) unit rows
    sim = mat @ mat.T
    np.fill_diagonal(sim, -1.0)
    edges: dict[tuple[int, int], float] = {}
    k = min(KNN_K, len(idxs) - 1)
    for row, gi in enumerate(idxs):
        order = np.argsort(sim[row])[::-1][:k]
        for col in order:
            s = float(sim[row, col])
            if s < KNN_MIN_SIM:
                break
            gj = idxs[int(col)]
            key = (min(gi, gj), max(gi, gj))
            if s > edges.get(key, -1.0):
                edges[key] = s
    return [[i, j, round(w, 4)] for (i, j), w in sorted(edges.items())]


def _graph_fallback_edges(
    conn: sqlite3.Connection, facts: list[dict[str, Any]]
) -> list[list[float]]:
    """Symbolic-graph edges for stores with no embeddings.

    Two honest sources, both w=GRAPH_EDGE_W: (1) live ``schema_faces``
    co-membership — a face's footprint members are ``member_key(fact_body)``
    hashes, so member facts of the same face connect pairwise; (2) ACTIVE
    ``relation_edges`` — an identity-level edge (A, B) bridges the newest facts
    whose text mentions A to the newest facts mentioning B (bounded substring
    match; an approximation, which is why ``edge_source`` labels it "graph")."""
    edges: dict[tuple[int, int], float] = {}

    def _add(i: int, j: int) -> None:
        if i != j:
            edges[(min(i, j), max(i, j))] = GRAPH_EDGE_W

    # (1) schema_faces co-membership
    try:
        from ..store.schema_faces import member_key

        key_to_idx: dict[str, int] = {}
        for i, f in enumerate(facts):
            if f["text"]:
                key_to_idx.setdefault(member_key(f["text"]), i)
        for (members_json,) in conn.execute(
            "SELECT members FROM schema_faces WHERE valid_to IS NULL"
        ):
            keys = json.loads(members_json or "[]")
            idxs = sorted({key_to_idx[k] for k in keys if k in key_to_idx})
            for i, j in islice(combinations(idxs, 2), _MAX_PAIRS_PER_FACE):
                _add(i, j)
    except Exception:  # noqa: BLE001 — fail-open per source (missing table, bad JSON)
        pass

    # (2) ACTIVE relation_edges bridging identity mentions
    try:
        rel = conn.execute(
            "SELECT src_identity, dst_identity FROM relation_edges"
            " WHERE status = 'active' AND valid_to IS NULL"
        ).fetchall()
        mention_cache: dict[str, list[int]] = {}

        def _mentions(identity: str) -> list[int]:
            if identity not in mention_cache:
                hits = [
                    i
                    for i, f in enumerate(facts)
                    if identity and len(identity) >= 2 and identity in f["text"]
                ]
                mention_cache[identity] = hits[:_MAX_MENTIONS_PER_SIDE]  # newest first
            return mention_cache[identity]

        for src, dst in rel:
            for i in _mentions(src or ""):
                for j in _mentions(dst or ""):
                    _add(i, j)
    except sqlite3.Error:
        pass

    return [[i, j, w] for (i, j), w in sorted(edges.items())]


# ── faces: Louvain communities over the edge graph ───────────────────────────
def _communities(n_facts: int, edges: list[list[float]]) -> list[list[int]]:
    """Deterministic Louvain communities (seed=42), sorted members, stable order."""
    if not edges:
        return []
    import networkx as nx

    g = nx.Graph()
    g.add_nodes_from(range(n_facts))
    for i, j, w in edges:
        g.add_edge(int(i), int(j), weight=float(w))
    comms = nx.algorithms.community.louvain_communities(g, weight="weight", seed=42)
    out = [sorted(int(m) for m in c) for c in comms if len(c) >= 2]
    out.sort(key=lambda c: (-len(c), c[0]))
    return out


def _face_signature(members: list[int], facts: list[dict[str, Any]], degree: list[int]) -> str:
    top = max(members, key=lambda m: (degree[m], -m))
    return facts[top]["text"][:40]


# ── layout: two-level (community supergraph FR + member disk scatter) ────────
def _fruchterman_reingold(
    n: int, edges: list[tuple[int, int, float]], *, iterations: int = FR_ITERATIONS
) -> np.ndarray:
    """Hand-written numpy Fruchterman–Reingold on the unit square (no scipy).

    Deterministic: positions seeded with ``default_rng(42)``. Returns (n, 2)
    positions normalized to max |coord| = 1."""
    if n == 0:
        return np.zeros((0, 2))
    if n == 1:
        return np.zeros((1, 2))
    rng = np.random.default_rng(42)
    pos = rng.uniform(-1.0, 1.0, (n, 2))
    k = math.sqrt(4.0 / n)  # ideal spring length on a 2×2 canvas
    t = 0.3
    dt = t / (iterations + 1)
    wmat = np.zeros((n, n))
    for i, j, w in edges:
        wmat[i, j] = max(wmat[i, j], w)
        wmat[j, i] = wmat[i, j]
    for _ in range(iterations):
        delta = pos[:, None, :] - pos[None, :, :]  # (n, n, 2)
        dist = np.linalg.norm(delta, axis=-1)
        np.fill_diagonal(dist, 1.0)
        dist = np.maximum(dist, 1e-6)
        # repulsion k²/d between every pair; attraction w·d²/k along edges
        force = (k * k) / dist - wmat * (dist * dist) / k
        disp = np.sum(delta / dist[..., None] * force[..., None], axis=1)
        length = np.maximum(np.linalg.norm(disp, axis=1, keepdims=True), 1e-9)
        pos += disp / length * np.minimum(length, t)
        t -= dt
    span = float(np.max(np.abs(pos)))
    if span > 0:
        pos /= span
    return pos


def _layout(
    facts: list[dict[str, Any]], edges: list[list[float]], comms: list[list[int]]
) -> list[tuple[float, float]]:
    """XZ ∈ [-1,1] for every fact: FR-placed cluster centers, sqrt-uniform member
    disks, isolated facts on the outer ring."""
    n = len(facts)
    coords: list[tuple[float, float] | None] = [None] * n
    member_of = {m: ci for ci, c in enumerate(comms) for m in c}

    if comms:
        # community supergraph: weight = summed cross-community edge weight
        sup: dict[tuple[int, int], float] = {}
        for i, j, w in edges:
            ci, cj = member_of.get(int(i)), member_of.get(int(j))
            if ci is None or cj is None or ci == cj:
                continue
            key = (min(ci, cj), max(ci, cj))
            sup[key] = sup.get(key, 0.0) + float(w)
        centers = (
            _fruchterman_reingold(len(comms), [(a, b, w) for (a, b), w in sorted(sup.items())])
            * 0.72
        )  # keep the disks + outer ring inside the unit square
        if len(comms) >= 2:
            dists = [
                float(np.linalg.norm(centers[a] - centers[b]))
                for a, b in combinations(range(len(comms)), 2)
            ]
            disk_r = max(min(dists), 1e-3) * 0.42
        else:
            disk_r = 0.6
        disk_r = min(disk_r, 0.35)
        for ci, members in enumerate(comms):
            cx, cz = float(centers[ci][0]), float(centers[ci][1])
            m = len(members)
            for order, fi in enumerate(members):
                rr = disk_r * math.sqrt((order + 0.5) / m)  # sqrt-uniform disk
                theta = order * GOLDEN_ANGLE
                coords[fi] = (cx + rr * math.cos(theta), cz + rr * math.sin(theta))

    isolated = [i for i in range(n) if coords[i] is None]
    ring_r = 0.95
    for order, fi in enumerate(isolated):
        theta = 2.0 * math.pi * order / max(len(isolated), 1)
        coords[fi] = (ring_r * math.cos(theta), ring_r * math.sin(theta))

    return [
        (max(-1.0, min(1.0, x)), max(-1.0, min(1.0, z)))
        for x, z in coords  # type: ignore[misc]
    ]


# ── deposition time → y ∈ [0, 1] ─────────────────────────────────────────────
def _parse_ts(raw: str) -> float | None:
    try:
        return datetime.fromisoformat(raw).timestamp()
    except (ValueError, TypeError):
        return None


def _normalized_times(facts: list[dict[str, Any]]) -> list[float]:
    stamps = [_parse_ts(f["ts"]) for f in facts]
    known = [s for s in stamps if s is not None]
    if not known:
        return [0.0] * len(facts)
    lo, hi = min(known), max(known)
    span = hi - lo
    if span <= 0:
        return [1.0] * len(facts)
    return [0.0 if s is None else (s - lo) / span for s in stamps]


# ── orchestrator ─────────────────────────────────────────────────────────────
def compute(conn: sqlite3.Connection) -> dict[str, Any]:
    """Build the full sem_facts payload off an open index.db connection."""
    facts = _load_facts(conn)
    vecs = _load_vectors(conn, [f["node_id"] for f in facts])
    if len(vecs) >= 2:
        edges = _knn_edges(facts, vecs)
        edge_source = "vectors"
    else:
        edges = _graph_fallback_edges(conn, facts)
        edge_source = "graph"

    degree = [0] * len(facts)
    for i, j, _w in edges:
        degree[int(i)] += 1
        degree[int(j)] += 1

    comms = _communities(len(facts), edges)
    faces = [
        {
            "id": f"sem-{ci}",
            "sig": _face_signature(members, facts, degree),
            "level": 1,
            "members": members,
        }
        for ci, members in enumerate(comms)
        if len(members) >= FACE_MIN_MEMBERS
    ]

    coords = _layout(facts, edges, comms)
    times = _normalized_times(facts)
    return {
        "facts": [
            {
                "i": i,
                "x": round(coords[i][0], 4),
                "y": round(times[i], 4),
                "z": round(coords[i][1], 4),
                "t": f["text"][:FACT_TEXT_MAX],
                "t2": f["ts"],
            }
            for i, f in enumerate(facts)
        ],
        "edges": edges,
        "faces": faces,
        "edge_source": edge_source,
    }


def generate(db_path: Path, out_path: Path) -> dict[str, Any]:
    """Read ``db_path`` (read-only) → write ``out_path`` → return summary stats."""
    if not db_path.exists():
        raise FileNotFoundError(f"index.db not found: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        payload = compute(conn)
    finally:
        conn.close()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return {
        "facts": len(payload["facts"]),
        "edges": len(payload["edges"]),
        "faces": len(payload["faces"]),
        "edge_source": payload["edge_source"],
        "out": str(out_path),
    }

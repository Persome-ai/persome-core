"Relation-chain delivery with receipt pointers."

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from ..evomem import identity as identity_mod
from ..store import relation_edges as edges_store
from ..store.fts import EntryHit

ROOT = "self"


@dataclass(frozen=True)
class Hop:
    src: str
    dst: str
    predicate: str
    label: str
    observations: int
    valid_from: str
    edge_id: str


@dataclass
class Chain:
    """One root-first path USER → … → anchor. Score = bottleneck strength."""

    hops: tuple[Hop, ...]

    @property
    def score(self) -> int:
        return min((h.observations for h in self.hops), default=0)

    @property
    def identities(self) -> list[str]:
        if not self.hops:
            return [ROOT]
        return [self.hops[0].src, *(h.dst for h in self.hops)]


@dataclass
class Delivery:
    """What the read path hands the consumer: narrative + receipt pointers."""

    lines: list[str] = field(default_factory=list)
    receipts: list[tuple[str, str]] = field(default_factory=list)  # (entry_id, path)
    chained_anchors: list[str] = field(default_factory=list)
    orphan_anchors: list[str] = field(default_factory=list)
    walked_edge_ids: list[str] = field(default_factory=list)


def _oriented(hop_row, tail: str) -> Hop | None:
    """Orient an edge row so it EXTENDS a path ending at ``tail``; None if it
    doesn't touch ``tail``. knows is undirected; directed predicates keep their
    canonical direction in the narrative but may be traversed both ways."""
    src, dst = hop_row["src_identity"], hop_row["dst_identity"]
    if tail not in (src, dst):
        return None
    other = dst if tail == src else src
    return Hop(
        src=tail,
        dst=other,
        predicate=hop_row["predicate"],
        label=hop_row["label"] or "",
        observations=int(hop_row["observations"] or 1),
        valid_from=hop_row["valid_from"] or "",
        edge_id=hop_row["edge_id"],
    )


def chain_to_user(
    conn: sqlite3.Connection,
    anchor: str,
    *,
    as_of: str | None = None,
    beam: int = 3,
    max_hops: int = 4,
) -> Chain | None:
    """Best chain anchor→USER by beam search over ACTIVE edges (§3.4 step 4).

    Beam keeps the top-``beam`` partial paths per depth by bottleneck score;
    the first paths to reach ``self`` compete on score and the best wins.
    Returns the chain ROOT-FIRST (USER → … → anchor), or None (honest orphan).
    """
    anchor = (anchor or "").strip()
    if not anchor:
        return None
    if identity_mod.norm(anchor) == ROOT:
        return Chain(hops=())
    # partial paths grow anchor→…→tail; scored by bottleneck
    frontier: list[tuple[tuple[Hop, ...], str]] = [((), anchor)]
    complete: list[tuple[Hop, ...]] = []
    for _ in range(max_hops):
        candidates: list[tuple[tuple[Hop, ...], str]] = []
        tails = {tail for _path, tail in frontier}
        rows = edges_store.edges_as_of(conn, tails, as_of=as_of)
        for path, tail in frontier:
            visited = {anchor, *(h.dst for h in path)}
            for row in rows:
                hop = _oriented(row, tail)
                if hop is None or hop.dst in visited:
                    continue
                new_path = (*path, hop)
                if identity_mod.norm(hop.dst) == ROOT:
                    complete.append(new_path)
                else:
                    candidates.append((new_path, hop.dst))
        if complete:
            break  # shortest chains first; among them the bottleneck decides
        candidates.sort(key=lambda pt: min(h.observations for h in pt[0]), reverse=True)
        frontier = candidates[:beam]
        if not frontier:
            break
    if not complete:
        return None
    best = max(complete, key=lambda path: min(h.observations for h in path))
    # reverse to root-first: USER → … → anchor
    reversed_hops = tuple(
        Hop(
            src=h.dst,
            dst=h.src,
            predicate=h.predicate,
            label=h.label,
            observations=h.observations,
            valid_from=h.valid_from,
            edge_id=h.edge_id,
        )
        for h in reversed(best)
    )
    return Chain(hops=reversed_hops)


def merge_chains(chains: list[Chain]) -> dict:
    """Prefix-merge root-first chains into a USER-rooted subtree (§3.4 step 4):
    nested dict keyed by hop; shared prefixes render once."""
    root: dict = {}
    for chain in chains:
        node = root
        for hop in chain.hops:
            node = node.setdefault(hop, {})
    return root


def pull_chains(
    conn: sqlite3.Connection,
    hits: list[EntryHit],
    roster: identity_mod.Roster,
    *,
    as_of: str | None = None,
    beam: int = 3,
    max_hops: int = 4,
) -> Delivery:
    """§3.4 step 4-5 for a result set: anchor identities are the roster
    mentions inside each hit's content (the same zero-LLM scan the Q side
    uses); every anchor pulls its chain; chains merge into one subtree; every
    hit contributes its receipt pointer; walked edges get their read
    reinforcement."""
    delivery = Delivery()
    anchors: list[str] = []
    seen: set[str] = set()
    for hit in hits:
        delivery.receipts.append((hit.id, hit.path))
        for name in identity_mod.scan_mentions(hit.content, roster):
            if name not in seen:
                anchors.append(name)
                seen.add(name)
    chains: list[Chain] = []
    walked: list[str] = []
    for anchor in anchors:
        chain = chain_to_user(conn, anchor, as_of=as_of, beam=beam, max_hops=max_hops)
        if chain is None:
            delivery.orphan_anchors.append(anchor)
        elif chain.hops:
            chains.append(chain)
            delivery.chained_anchors.append(anchor)
            walked.extend(h.edge_id for h in chain.hops)
    if walked:
        try:
            edges_store.bump_recall(conn, walked)
            delivery.walked_edge_ids = walked
        except Exception:  # noqa: BLE001 — reinforcement is best-effort
            delivery.walked_edge_ids = []
    delivery.lines = _render_tree(merge_chains(chains), level=0)
    return delivery


def _hop_text(hop: Hop, *, annotated: bool) -> str:
    label = f" ({hop.label})" if hop.label else ""
    note = f"  [strength {hop.observations} · since {hop.valid_from[:10]}]" if annotated else ""
    return f"─{hop.predicate}{label}→ {hop.dst}{note}"


def _render_tree(node: dict, *, level: int, annotated: bool = True) -> list[str]:
    lines: list[str] = ["USER"] if level == 0 else []

    def walk(sub: dict, depth: int) -> None:
        for hop, child in sub.items():
            lines.append("  " * depth + _hop_text(hop, annotated=annotated))
            walk(child, depth + 1)

    walk(node, 1)
    return lines


def render_delivery(delivery: Delivery, *, budget_chars: int = 2000) -> str:
    """§3.4 step 5: path-as-narrative + receipt pointers, compressed to budget
    — NEVER by dropping a chain. Degrade levels: full → no annotations → flat
    one-line chains → identities only. Receipts always survive (they are the
    §2.1 disclosure handles)."""
    receipts = "Receipts: " + " ".join(f"⟨{eid}:{path}⟩" for eid, path in delivery.receipts)
    orphans = (
        "Orphan anchors without a chain to USER: " + ", ".join(delivery.orphan_anchors)
        if delivery.orphan_anchors
        else ""
    )

    def compose(lines: list[str]) -> str:
        parts = ["\n".join(lines), receipts]
        if orphans:
            parts.append(orphans)
        return "\n".join(p for p in parts if p)

    full = compose(delivery.lines)
    if len(full) <= budget_chars:
        return full
    # level 1: drop annotations
    stripped = [ln.split("  [")[0] for ln in delivery.lines]
    out = compose(stripped)
    if len(out) <= budget_chars:
        return out
    # level 2: flatten the tree to single-space indent
    flat = [ln.strip() for ln in stripped]
    out = compose(flat)
    if len(out) <= budget_chars:
        return out
    # level 3: identities only — the chain SHAPE survives even at minimum
    minimal = [ln.split("→")[-1].strip() if "→" in ln else ln for ln in flat]
    return compose(minimal)

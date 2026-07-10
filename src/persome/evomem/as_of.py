"Bitemporal as-of resolution for evolutionary memory nodes."

from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import datetime

from ..logger import get
from .models import MemoryNode
from .store import _row_to_node

logger = get("persome.evomem.as_of")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _le(a: datetime, b: datetime) -> bool:
    """a ≤ b, timezone-safe: if exactly one side is naive, compare naively
    (stored values mix conventions; a TypeError must never break as-of)."""
    if (a.tzinfo is None) != (b.tzinfo is None):
        a, b = a.replace(tzinfo=None), b.replace(tzinfo=None)
    return a <= b


def _successor_ids(row: sqlite3.Row) -> list[str]:
    try:
        return list(json.loads(row["superseded_by"] or "[]"))
    except (TypeError, ValueError):
        return []


def _live_at(rows: list[sqlite3.Row], t: datetime) -> list[sqlite3.Row]:
    """Transaction-clock replay: the version-set that was head at T."""
    by_id = {r["node_id"]: r for r in rows}
    live: list[sqlite3.Row] = []
    for row in rows:
        created = _parse(row["gmt_created"])
        if created is not None and not _le(created, t):
            continue  # not yet written at T
        superseded = False
        for succ_id in _successor_ids(row):
            succ = by_id.get(succ_id)
            if succ is None:
                continue  # dangling pointer — can't date the supersede, keep row
            succ_created = _parse(succ["gmt_created"])
            if succ_created is None or _le(succ_created, t):
                superseded = True  # successor already existed at T
                break
        if not superseded:
            live.append(row)
    return live


def _valid_at(row: sqlite3.Row, t: datetime) -> bool:
    """Validity-clock filter, fail-open on absent/unparseable fields."""
    vf = _parse(row["valid_from"])
    if vf is not None and not _le(vf, t):
        return False
    vu = _parse(row["valid_until"])
    return not (vu is not None and not _le(t, vu))


def nodes_as_of(
    conn: sqlite3.Connection,
    *,
    file_name: str,
    t: datetime,
    user_id: str = "default",
    agent_id: str = "default",
) -> list[MemoryNode]:
    """Resolve an identity's node-set as of T (§1.4 as-of-T node API).

    ``file_name`` is the identity's canonical route (what the §4.3 identity
    funnel resolves a free-form mention to). Returns every node version of
    that file that was (a) already written and not yet superseded at T —
    transaction replay — and (b) not excluded by its validity window —
    world-time filter. Empty list when the identity has no history at T.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM evo_nodes WHERE file_name = ? AND user_id = ? AND agent_id = ?"
        " ORDER BY gmt_created",
        (file_name, user_id, agent_id),
    ).fetchall()
    if not rows:
        return []
    return [_row_to_node(r) for r in _live_at(rows, t) if _valid_at(r, t)]


def node_as_of(
    conn: sqlite3.Connection,
    *,
    node_id: str,
    t: datetime,
    user_id: str = "default",
    agent_id: str = "default",
) -> MemoryNode | None:
    """Resolve ONE chain as of T: from any version on a supersede chain,
    return the version that was live at T (the chain is walked in both
    directions from the given id), or None when the chain had no live,
    validity-passing version yet at T. This is the receipt-pointer
    counterpart: a delivered ⟨entry_id⟩ can be re-asked "and what did this
    say back in March?"."""

    def fetch(nid: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM evo_nodes WHERE node_id = ? AND user_id = ? AND agent_id = ?",
            (nid, user_id, agent_id),
        ).fetchone()

    conn.row_factory = sqlite3.Row
    seed = fetch(node_id)
    if seed is None:
        return None
    chain: dict[str, sqlite3.Row] = {seed["node_id"]: seed}
    frontier = [seed]
    while frontier:
        row = frontier.pop()
        neighbors: list[str] = []
        for col in ("superseded_by", "supersedes"):
            with contextlib.suppress(TypeError, ValueError):
                neighbors.extend(json.loads(row[col] or "[]"))
        for nid in neighbors:
            if nid in chain:
                continue
            nrow = fetch(nid)
            if nrow is not None:
                chain[nid] = nrow
                frontier.append(nrow)
    live = [r for r in _live_at(list(chain.values()), t) if _valid_at(r, t)]
    if not live:
        return None
    # one chain has at most one transaction-live version at T; if validity
    # windows still leave several, prefer the newest written
    live.sort(key=lambda r: r["gmt_created"] or "")
    return _row_to_node(live[-1])

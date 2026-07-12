"""MCP server exposing Persome memory and explicit correction tools.

Uses the official `mcp` Python SDK via FastMCP. Runs either standalone
over stdio (`persome mcp`) or in-daemon over streamable-http / sse,
depending on `[mcp] transport`. Exposes:

  Compressed memory (Markdown layer):
    list_memories, read_memory, search, verify_fact, behavior_patterns,
    get_model_snapshot, entity_graph, read_receipt, recent_activity
  Raw captures (S1 buffer):
    current_context, search_captures, read_recent_capture
  Reference:
    get_schema
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from .. import __version__
from ..config import Config
from ..config import load as load_config
from ..logger import get
from ..prompts import load as load_prompt
from ..security.auth import (
    add_local_api_auth_middleware,
    loopback_http_url,
    reset_browser_auth_state,
    validate_bind_host,
)
from ..security.body_limit import (
    RequestBodyLimitMiddleware,
    RequestConcurrencyLimitMiddleware,
)
from ..store import files as files_mod
from ..store import fts
from ..timeline import attention_trajectory as attention_traj
from . import captures as captures_mod
from .limits import (
    bounded_float,
    bounded_int,
    bounded_optional_text,
    bounded_text,
    bounded_text_list,
)

logger = get("persome.mcp")


def _parse_iso_opt(value: str | None) -> datetime | None:
    """Best-effort ISO8601 → datetime; None on missing/unparseable.

    A parsed *naive* datetime (a relative query like "today" that an LLM
    resolved to an offset-less ISO string) is assumed to be in the daemon's —
    i.e. the user's — current local timezone and made offset-aware, exactly like
    ``store._abs_delta_within`` / the intent pipeline (#586, #134). Timeline
    blocks are stored offset-aware (the aggregator writes ISO with offset and
    ``_row_to_block`` parses it back aware), so a naive bound would raise
    ``TypeError: can't compare offset-naive and offset-aware datetimes`` once it
    reached ``attention_trajectory``'s ``b.start_time <= until`` filter. Keying
    every bound off the local tz keeps the boundary comparable and consistent
    with the rest of the codebase.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def _list_memories(  # type: ignore[no-untyped-def]
    conn, *, include_dormant: bool = False, include_archived: bool = False
) -> dict[str, Any]:
    rows = fts.list_files(conn, include_dormant=include_dormant, include_archived=include_archived)
    return {
        "count": len(rows),
        "files": [
            {
                "path": r.path,
                "description": r.description,
                "tags": r.tags.split() if r.tags else [],
                "status": r.status,
                "entry_count": r.entry_count,
                "created": r.created,
                "updated": r.updated,
            }
            for r in rows
        ],
    }


def _read_memory(  # type: ignore[no-untyped-def]
    conn,
    *,
    path: str,
    since: str | None = None,
    until: str | None = None,
    tags: list[str] | None = None,
    tail_n: int | None = None,
) -> dict[str, Any]:
    p = files_mod.memory_path(path)
    if not p.exists():
        return {"error": f"file not found: {path}"}
    parsed = files_mod.read_file(p)
    entries = parsed.entries
    if since:
        entries = [e for e in entries if e.timestamp >= since]
    if until:
        entries = [e for e in entries if e.timestamp <= until]
    if tags:
        tagset = set(tags)
        entries = [e for e in entries if tagset.intersection(e.tags)]
    if tail_n is not None and tail_n > 0:
        entries = entries[-tail_n:]
    return {
        "path": path,
        "description": parsed.description,
        "tags": parsed.tags,
        "status": parsed.status,
        "updated": parsed.updated,
        "entry_count": parsed.entry_count,
        "entries": [
            {
                "id": e.id,
                "timestamp": e.timestamp,
                "tags": e.tags,
                "body": e.body,
                "superseded_by": e.superseded_by,
                "confidence": e.confidence,
                "conflicted": e.conflicted,
                "occurred_at": e.occurred_at,
            }
            for e in entries
        ],
    }


def _get_model_snapshot(conn, *, redact: bool = True) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """Project the live personal model through the same versioned contract as CLI export."""
    from ..model import build_snapshot, load_last_manifest

    return build_snapshot(conn, redact=redact, build_metadata=load_last_manifest())


def _search(  # type: ignore[no-untyped-def]
    conn,
    *,
    query: str,
    paths: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    top_k: int = 5,
    include_superseded: bool = False,
    breadth: float = 0.0,
    entities: list[str] | None = None,
    include_bodies: bool = False,
) -> dict[str, Any]:
    breadth = min(1.0, max(0.0, float(breadth)))
    chains_text = ""
    if include_superseded:
        # archaeology mode — not an associative question; the associative pools
        # are live-only by design, so the legacy read serves this verbatim
        hits = fts.search_hybrid(
            conn,
            query=query,
            path_patterns=paths,
            since=since,
            until=until,
            top_k=top_k,
            include_superseded=True,
        )
    else:
        # §5 read cutover: associative entrance + §3.4 tree-chain delivery
        # (kill-switch [search] associative_read_enabled)
        from ..retrieval import associative as assoc_mod

        hits, chains_text = assoc_mod.associative_read(
            conn,
            query=query,
            path_patterns=paths,
            since=since,
            until=until,
            top_k=top_k,
            with_chains=True,
            entities=entities,
            mmr_diversity=breadth,
        )
    metas = fts.entry_metadata_map(conn, [h.id for h in hits])
    face_index = (
        _face_membership_index(conn, include_bodies=include_bodies)
        if hits
        else {"by_member": {}, "by_path": {}}
    )
    now = datetime.now().astimezone()
    results = []
    for h in hits:
        m = metas.get(h.id) or {}
        row: dict[str, Any] = {
            "id": h.id,
            "path": h.path,
            "timestamp": h.timestamp,
            # the consumer's cheapest fact-check. A version number / status /
            # responsibility recalled from a 20-day-old entry is a CLAIM ABOUT THE
            # PAST, not the present; verify before reporting it as current.
            "age_days": _age_days(h.timestamp, now=now),
            "content": h.content,
            "rank": h.rank,
            "confidence": m.get("confidence"),
            "conflicted": m.get("conflicted", False),
            "occurred_at": m.get("occurred_at"),
        }
        faces = _related_faces_for(h.content, h.path, face_index)
        if faces:
            # E1.5 — the hit's EXPLANATION: promoted regularities covering this

            # path, opt-in via include_bodies). Present only when a face
            # actually covers the hit; the resident layer as a whole stays

            row["related_faces"] = faces
        results.append(row)
    out: dict[str, Any] = {"query": query, "results": results}
    if chains_text:
        out["chains"] = chains_text
    return out


def _face_membership_index(  # type: ignore[no-untyped-def]
    conn, *, include_bodies: bool = False
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Build the recall-to-schema membership index: Faces by default, Volumes opt-in.

    Level-1 Faces store fact-body member-key hashes, so a fact hit's
    covering regularity is a deterministic lookup (the read-side twin of the
    membership link Phase 3's utility credit walks on the write side). Level-2
    Level-2 Volumes store parent schema file names because no direct Face-to-Volume
    pointer exists yet. Volume coverage is therefore path-based. A store predating
    ``schema_faces`` or containing malformed members fails open to an empty
    index rather than raising."""
    from ..store import schema_faces as faces_store

    index: dict[str, dict[str, list[dict[str, Any]]]] = {"by_member": {}, "by_path": {}}
    try:
        faces_store.ensure_schema(conn)
        levels = "(1, 2)" if include_bodies else "(1)"
        rows = conn.execute(
            "SELECT level, signature, confidence, observations, members FROM schema_faces"
            f" WHERE status = 'active' AND valid_to IS NULL AND level IN {levels}"
        ).fetchall()
    except Exception:  # noqa: BLE001 — the association decorates hits, never breaks recall
        return index
    for r in rows:
        try:
            members = json.loads(r["members"] or "[]")
        except (TypeError, ValueError):
            continue
        face = {
            "level": r["level"],
            "signature": r["signature"],
            "confidence": r["confidence"],
            "observations": r["observations"],
        }
        bucket = index["by_member"] if r["level"] == 1 else index["by_path"]
        for m in members:
            bucket.setdefault(str(m), []).append(face)
    return index


def _related_faces_for(
    content: str, path: str, index: dict[str, dict[str, list[dict[str, Any]]]]
) -> list[dict[str, Any]]:
    from ..store import schema_faces as faces_store

    faces: list[dict[str, Any]] = []
    if content and index["by_member"]:
        faces.extend(index["by_member"].get(faces_store.member_key(content), []))
    if path and index["by_path"]:
        faces.extend(index["by_path"].get(path, []))
    return faces


def _behavior_patterns(conn) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """Return the resident personal-model projection over MCP.

    The learned behavior model — the level-3 root apex ("who is this person")
    plus the promoted (both-provenance + resampling-stable) schema faces — used
    to reach only the app-side grounding digest. MCP-side callers get the same
    resident layer: these live in the ``schema_faces`` table, NOT in the
    ``entries`` retrieval unit, so ``search`` can never surface them.
    """
    from ..store import schema_faces as faces_store

    root_row = faces_store.resident_root(conn)
    faces = faces_store.resident_faces(conn, top_k=8)
    rendered = "\n\n".join(
        block
        for block in (faces_store.render_root(root_row), faces_store.render_residency(faces))
        if block
    )
    root = None
    if root_row is not None:
        root = {
            "signature": root_row["signature"],
            "confidence": root_row["confidence"],
            "created_at": root_row["created_at"],
        }
    return {
        "root": root,
        "faces": [
            {
                "signature": f["signature"],
                "level": f["level"],
                "provenance": f["provenance"],
                "observations": f["observations"],
                "confidence": f["confidence"],
            }
            for f in faces
        ],
        "rendered": rendered,
    }


def _age_days(timestamp: str | None, *, now: datetime) -> int | None:
    parsed = _parse_iso_opt(timestamp)
    if parsed is None:
        return None
    return max(0, int((now - parsed).total_seconds() // 86400))


def _verify_fact(  # type: ignore[no-untyped-def]
    conn,
    *,
    claim: str,
    top_k: int = 8,
    fresh_within_days: int = 7,
) -> dict[str, Any]:
    """Check claim freshness deterministically without an LLM.

    Pulls the freshest LIVE evidence for a claim through the production read
    entrance and reports each hit's age; the caller (an LLM agent) does the
    semantic comparison. The tool's contract is honesty about TIME: it never
    judges the claim itself, it says how stale the best available evidence is.
    """
    from ..retrieval import associative as assoc_mod

    hits = assoc_mod.associative_read(conn, query=claim, top_k=top_k)
    metas = fts.entry_metadata_map(conn, [h.id for h in hits])
    now = datetime.now().astimezone()
    evidence = []
    for h in hits:
        m = metas.get(h.id) or {}
        evidence.append(
            {
                "id": h.id,
                "path": h.path,
                "timestamp": h.timestamp,
                "age_days": _age_days(h.timestamp, now=now),
                "content": h.content,
                "confidence": m.get("confidence"),
                "conflicted": m.get("conflicted", False),
                "occurred_at": m.get("occurred_at"),
            }
        )
    ages = [e["age_days"] for e in evidence if e["age_days"] is not None]
    freshest = min(ages) if ages else None
    stale = freshest is None or freshest > fresh_within_days
    if not evidence:
        note = "No related evidence exists in memory. Do not state this claim as fact."
    elif stale:
        note = (
            f"The freshest related evidence is {freshest} day(s) old. Time-sensitive "
            "facts such as versions, status, ownership, and active work may be stale. "
            "Present this only as historical state or verify it with a current source."
        )
    else:
        note = (
            f"Evidence exists within {freshest} day(s). Read each item to confirm that "
            "it supports the claim; this tool checks freshness, not semantics."
        )
    return {
        "claim": claim,
        "evidence": evidence,
        "freshest_age_days": freshest,
        "stale": stale,
        "note": note,
    }


def _read_receipt(conn, *, entry_id: str) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """Dereference one ``⟨entry_id:path⟩`` receipt handle.

    The chain delivery hands out receipt POINTERS; this is the one-hop
    drill-down that turns a pointer into the entry itself plus the breadcrumbs
    to the NEXT disclosure layer (nearby captures → ``read_recent_capture`` /
    capture receipts). Superseded entries are readable — a receipt is
    archaeology, not a claim about the present — and are labeled as such.
    Reading a receipt reinforces it."""
    row = conn.execute(
        "SELECT id, path, timestamp, tags, content, superseded FROM entries WHERE id = ?",
        (entry_id,),
    ).fetchone()
    if row is None:
        return {"error": f"entry not found: {entry_id}"}
    meta = fts.get_entry_metadata(conn, entry_id) or {}
    temporal = conn.execute(
        "SELECT valid_from, valid_until FROM entry_temporal WHERE entry_id = ?",
        (entry_id,),
    ).fetchone()
    ts = row["timestamp"]
    # Epoch-based proximity uses the same historical ISO parser as capture,
    # timeline, and session ordering (including legacy basic/naive forms).
    captures = conn.execute(
        "SELECT id, timestamp, app_name, window_title FROM captures"
        " WHERE abs(persome_epoch(timestamp) - persome_epoch(?)) <= 1800"
        " ORDER BY abs(persome_epoch(timestamp) - persome_epoch(?)) LIMIT 3",
        (ts, ts),
    ).fetchall()
    if not row["superseded"]:
        fts.increment_retrieval_counts(conn, (entry_id,))
    return {
        "id": row["id"],
        "path": row["path"],
        "timestamp": ts,
        "age_days": _age_days(ts, now=datetime.now().astimezone()),
        "tags": (row["tags"] or "").split(),
        "content": row["content"],
        "superseded": bool(row["superseded"]),
        "confidence": meta.get("confidence"),
        "conflicted": meta.get("conflicted", False),
        "occurred_at": meta.get("occurred_at"),
        "valid_from": temporal["valid_from"] if temporal else None,
        "valid_until": temporal["valid_until"] if temporal else None,
        # next disclosure layer: raw captures near this entry's write time —
        # follow with read_recent_capture(at=…) for an explicitly requested image.
        "nearby_captures": [
            {
                "id": c["id"],
                "timestamp": c["timestamp"],
                "app_name": c["app_name"],
                "window_title": c["window_title"],
            }
            for c in captures
        ],
    }


def _entity_graph(  # type: ignore[no-untyped-def]
    conn,
    cfg,
    *,
    name: str,
    depth: int = 1,
    as_of: str | None = None,
    include_shadow: bool = False,
) -> dict[str, Any]:
    """E2 — the graph layer over MCP: who/what an identity connects to, as of T.

    Resolution goes through the SAME ``resolve_identity`` funnel as the write
    side and the distilled Q (§4.3: one codebook — forks drift). An unresolved
    mention is an honest miss, never a guess. ACTIVE edges are the answer;
    shadow edges (unproven extraction) ride a separate, explicitly-labeled list
    only when asked. Walked ACTIVE edges get their read reinforced (§3.3)."""
    from ..evomem import identity as identity_mod
    from ..retrieval import chains as chains_mod
    from ..store import relation_edges as edges_store

    depth = max(1, min(4, int(depth)))
    roster = identity_mod.load_roster(cfg)
    res = identity_mod.resolve_identity(name, roster)
    if not res.matched:
        return {
            "resolved": None,
            "layer": res.layer,
            "note": (
                f"'{name}' did not resolve to a known identity. The graph may not contain "
                "it yet, or the name may be ambiguous; no guess was made."
            ),
        }
    canonical = res.canonical
    assert canonical is not None

    def _edge_dict(r) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        keys = set(r.keys())
        return {
            "src": r["src_identity"],
            "dst": r["dst_identity"],
            "predicate": r["predicate"],
            "label": r["label"] if "label" in keys else None,
            "observations": r["observations"] if "observations" in keys else None,
            "valid_from": r["valid_from"] if "valid_from" in keys else None,
            "valid_to": r["valid_to"] if "valid_to" in keys else None,
            "status": r["status"] if "status" in keys else None,
        }

    active_rows = edges_store.edges_as_of(conn, [canonical], as_of=as_of)
    shadow_rows = (
        list(edges_store.edges_as_of(conn, [canonical], as_of=as_of, status="shadow"))
        if include_shadow
        else []
    )
    reached = edges_store.neighbors(
        conn, [canonical], depth=depth, as_of=as_of, include_shadow=include_shadow
    )
    chain_text = ""
    try:
        chain = chains_mod.chain_to_user(conn, canonical, as_of=as_of)
        if chain is not None and chain.hops:
            parts = ["USER"]
            for hop in chain.hops:
                parts.append(f"→{hop.predicate}→ {hop.dst}")
            chain_text = " ".join(parts)
        elif chain is not None:
            chain_text = "USER"  # the anchor IS the user
    except Exception:  # noqa: BLE001 — the chain decorates the edges, never breaks them
        logger.exception("chain_to_user failed for %s", canonical)
    try:
        edge_ids = [r["id"] for r in active_rows if "id" in set(r.keys())]
        if edge_ids:
            edges_store.bump_recall(conn, edge_ids)
    except Exception:  # noqa: BLE001 — reinforcement is best-effort
        pass
    out: dict[str, Any] = {
        "resolved": canonical,
        "layer": res.layer,
        "as_of": as_of,
        "edges": [_edge_dict(r) for r in active_rows],
        "neighbors": sorted(reached),
        "chain_to_user": chain_text or None,
    }
    if include_shadow:
        out["shadow_edges"] = [_edge_dict(r) for r in shadow_rows]
    return out


def _recent_activity(  # type: ignore[no-untyped-def]
    conn,
    *,
    since: str | None = None,
    limit: int = 20,
    prefix_filter: list[str] | None = None,
) -> dict[str, Any]:
    rows = fts.recent(conn, since=since, limit=limit, prefix_filter=prefix_filter)
    return {
        "count": len(rows),
        "entries": [
            {
                "id": r.id,
                "path": r.path,
                "timestamp": r.timestamp,
                "content": r.content,
            }
            for r in rows
        ],
    }


def _get_schema() -> dict[str, Any]:
    return {"schema": load_prompt("schema.md")}


_SERVER_INSTRUCTIONS = """\
# Persome — the user's local personal memory

## What this is

Persome is the user's private, local-first memory layer. The user installed it so agents can recover context from their real computer use instead of asking the user to repeat themselves or guessing blindly.

It stores durable facts about the user and their machine, including:

- identity, role, preferences, habits, and working style
- schedule, ongoing projects, people, and organizations
- recent screen-activity summaries, including apps, files, errors, and documents viewed

It exposes two read-only layers:

- **Compressed memory** — curated Markdown files containing distilled facts, decisions, preferences, summaries, and durable context
- **Raw captures (S1 buffer)** — literal recent on-screen content, including visible text, focused elements, URLs, and optional screenshots

The compressed layer tells you that something happened and why it matters.
The raw layer tells you exactly what was on screen.

Use compressed memory for durable knowledge.
Use raw captures for grounding, disambiguation, and exact recent context.
Often, you should move from one into the other.

## When to use

Use Persome whenever the request depends on context that is likely outside the current chat.

This includes:

- recent on-screen activity
- ambiguous references such as "this", "that", "it", "the bug", "the file", "the tab", or "the doc"
- prior project / person / tool context
- learned preferences, habits, or workflow patterns
- writing or generation that should reflect the user's ongoing projects, established framing, terminology, tone, or style
- action selection that should reflect the user's established workflows or destinations
- cross-session continuity
- recent work history, decisions, or ongoing tasks

Canonical triggers:

- "what's the bug of that?"
- "introduce my project"
- "continue what I was doing"
- "write this the way I usually do"
- "draft this in the style of my project"
- "schedule this the way I usually do"
- "put this in the right calendar"
- "what did I decide about X?"

Examples:

- User refers to "that" after viewing code → query Persome before asking them to paste anything.
- User opens a fresh chat and asks about an existing project → retrieve project memory before asking for background.
- User asks for an action that depends on personal workflow → retrieve preference memory before choosing a tool, destination, or account.
- User asks for writing, messaging, or framing that should match prior context, terminology, tone, or preferences → retrieve relevant memory before drafting.

If the user appears to assume shared context from recent computer use, query Persome before asking a clarification question.

When in doubt, look it up.
A missed lookup is often worse than an unnecessary one.
These tools are local and cheap; `[]` or `null` is still useful information.

## When NOT to use

Do not use Persome when:

- the request is fully specified in-chat
- the task is self-contained and does not benefit from user-specific context
- a fresher or authoritative source of truth should be used directly
- the user explicitly wants no prior context used

Persome complements live sources of truth; it does not replace them.
Use it to recover context, not to invent certainty.

## Tools

Persome is a personal model of the user; the tools are its serving API. Three layers,
top-down — who they are (resident), what happened (recall), what was on screen (raw):

### The user model (resident layer — not reachable via text search)

- `behavior_patterns()` — the learned behavior model: one root narrative ("who is
  this person, what matters to them now") + promoted behavior regularities, each
  with evidence counts. Call ONCE early in a conversation that involves
  personalization, recaps, tone/style matching, or predicting what the user wants.
- `entity_graph(name, depth?, as_of?, include_shadow?)` — the relation graph around
  one identity: predicate edges with evidence + validity windows, reachable
  neighbors, and the chain back to the user. `as_of="2026-03-01"` answers about a
  PAST state ("who was his boss in March"). An unknown name returns an honest miss.

### Compressed memory (recall)

- `search(query, paths?, since?, until?, top_k?, breadth?, entities?, include_bodies?)`
  — semantic + keyword recall over distilled facts. Natural language works; you do
  not need the user's original phrasing. Knobs:
  - `entities=["Alex"]` when you KNOW who the question is about (aliases resolve;
    unknown names are ignored) — stronger than hoping the name appears in `query`.
  - `breadth=0.3–0.7` for survey/research questions (diverse angles over
    near-duplicate top hits); leave 0 when grounding a specific fact.
  - `include_bodies=true` to also attach higher-level cross-domain patterns.
- `verify_fact(claim, top_k?, fresh_within_days?)` — freshness check for ONE claim.
  Call before stating time-sensitive facts (versions, task status, who-does-what,
  schedules) as current. It judges TIME only; read the evidence yourself.
- `read_receipt(entry_id)` — dereference a `⟨entry_id:path⟩` receipt (from `chains`
  or any hit id) into the full entry + nearby-capture breadcrumbs. The audit trail
  from any memory down to the on-screen moment it came from.
- `recent_activity(since?, limit?, prefix_filter?)` — newest-first feed across
  memory files. Best for "what has the user been up to" and recency disambiguation.
- `list_memories()` / `read_memory(path, …)` — file index + whole-file reads, for
  when you want a specific document (e.g. `user-profile.md`) rather than a query.

### Raw captures (S1 layer)

- `current_context()` — one-shot snapshot of the current/recent screen context. Default for present-tense or ambiguous-reference questions.
- `search_captures(query, since?, until?, app_name?, limit?)` — keyword search over the raw screen buffer. For exact strings the user saw or typed: error messages, code symbols, file paths, URLs, doc titles.
- `read_recent_capture(at?, app_name?, window_title_substring?, ...)` — hydrate one capture in full. Use on a `search_captures` hit or a `read_receipt` breadcrumb.

### Reference

- `get_schema()` — memory file naming and structural spec. Rarely needed during normal query flow.

## Reading a search result

Each hit carries more than text — use all of it:

- `age_days` — how old the memory is. A large value means "claim about the PAST":
  versions, statuses, and responsibilities may have moved on. Cross-check with
  `verify_fact` before reporting such facts as current.
- `related_faces` — when present, the promoted behavior regularity that EXPLAINS
  this fact (with evidence count). Generalize from it: the single hit is an
  instance of a verified pattern, so the pattern likely holds in new situations.
- `confidence` / `conflicted` — reliability metadata; `conflicted: true` means an
  unresolved contradiction exists — do not present that fact as settled.
- `chains` (top-level) — how the hits connect back to the user, with receipt
  pointers `⟨entry_id:path⟩`. Anchors listed as orphans have no proven link yet.

## Choosing and combining tools

- **Question-type routing**
  - Who is the user / how do they work / match their style → `behavior_patterns()`.
  - Who is X / how does X relate to people & projects / past org states → `entity_graph(...)` (search only finds text; the graph knows structure).
  - What happened / what was decided / durable facts → `search(...)`.
  - Is this still true → `verify_fact(...)`.
  - What is this hit based on → `read_receipt(...)`, then follow its capture breadcrumbs.
  - What am I doing right now → `current_context()`.
  - Exact string the user just saw/typed → `search_captures(...)` → `read_recent_capture(...)`.
  - If unsure between compressed and raw, query `search` and `search_captures` in parallel.

- **The evidence ladder (progressive disclosure)** — every memory is auditable four
  layers down; go only as deep as the user's question demands:
  `chains` narrative → `read_receipt(entry_id)` → `read_recent_capture(at=…)` for optional pixels.

- **For writing / action personalization**
  - `behavior_patterns()` first (how they work), then `search` for the specific
    project/preference facts. Match established terminology, framing, and style.
  - Before side-effecting actions, check memory for the user's workflow defaults —
    then use the authoritative execution tool. Memory never replaces live state.

- **Freshness discipline** — memory is ranked by relevance AND recency, but an old
  strong match can still surface. Before asserting any time-sensitive fact as
  current: check `age_days`, and when it matters, `verify_fact`.

## Decision rule

Default to using Persome when memory could:

- resolve ambiguity
- restore missing context
- avoid making the user restate known information
- personalize writing
- personalize action selection

Do not default to it when the task is already fully specified or when only live state matters.

## If retrieval is weak

If Persome returns little, conflicting, or inconclusive information:

- say that explicitly
- use the partial context if still helpful
- ask a focused follow-up question only after checking
- do not overclaim certainty

Raw captures have bounded retention: older on-screen content is dropped from the S1 buffer. If `search_captures` or `read_recent_capture` returns nothing for something the user did a while ago, that only means the raw capture has aged out — the event may still be summarized in compressed memory. Fall back to `search` / `recent_activity` before concluding it didn't happen.
"""


def _protect_http_app(app: Any, *, host: str, auth_enabled: bool) -> Any:
    """Apply daemon-wide HTTP security to an SDK-created Starlette app.

    FastMCP's own bearer hook requires authentication only on its MCP route;
    custom REST mounts are otherwise left open.  Wrapping the final SDK app is
    therefore the single outer boundary for MCP, REST, and future custom
    routes.  Keep this helper composable with other outer ASGI limits.
    """
    if auth_enabled:
        validate_bind_host(host)
    # Starlette applies middleware in reverse add order.  Add resource limits
    # first and auth last so invalid credentials are rejected before a request
    # body is buffered or a concurrency slot is consumed.
    app.add_middleware(RequestBodyLimitMiddleware)
    app.add_middleware(RequestConcurrencyLimitMiddleware)
    return add_local_api_auth_middleware(app, enabled=auth_enabled)


def build_server(
    cfg: Config | None = None,
    *,
    auth_enabled: bool = True,
):  # type: ignore[no-untyped-def]
    """Construct and return a FastMCP server instance (not yet running)."""
    from mcp.server.fastmcp import FastMCP  # lazy import

    if auth_enabled:
        # HTTP cookies are not port-bound. Invalidate every capability whenever
        # a listener generation is rebuilt so a cookie captured during a crash
        # / port-rebind window cannot be replayed after the daemon recovers.
        reset_browser_auth_state()

    cfg = cfg or load_config()

    # module gates (hybrid dense, pool weights, tags/recency) default to legacy
    # values at import time and were historically wired only at daemon boot — so
    # the standalone `persome mcp` stdio server silently served BM25-only. Wiring
    # here covers EVERY spawn path (stdio and in-daemon; idempotent for the
    # latter): the same full-power read stack regardless of hosting process.
    fts.wire_read_path(cfg)
    # DNS-rebinding + CSRF-to-localhost hardening for the /mcp transport ITSELF. The REST
    # sub-app has its own Origin/Host guard (api/__init__.py), but /mcp lives on the OUTER
    # FastMCP app and never traverses that middleware — so without this it is the one
    # unguarded (and most powerful) surface. FastMCP applies no transport validation unless
    # `transport_security` is passed, so we pass it. Native MCP clients send no Origin and a
    # local Host → allowed; a browser page's foreign Origin or a rebound public Host → 421.
    from mcp.server.transport_security import TransportSecuritySettings

    if auth_enabled and cfg.mcp.transport != "stdio":
        validate_bind_host(cfg.mcp.host)

    class _ProtectedFastMCP(FastMCP):
        """FastMCP whose complete HTTP app shares the local bearer boundary."""

        def streamable_http_app(self):  # type: ignore[no-untyped-def]
            app = super().streamable_http_app()
            return _protect_http_app(
                app,
                host=self.settings.host,
                auth_enabled=auth_enabled,
            )

        def sse_app(self, mount_path=None):  # type: ignore[no-untyped-def]
            app = super().sse_app(mount_path)
            return _protect_http_app(
                app,
                host=self.settings.host,
                auth_enabled=auth_enabled,
            )

    _p = cfg.mcp.port
    server = _ProtectedFastMCP(
        "persome",
        instructions=_SERVER_INSTRUCTIONS,
        host=cfg.mcp.host,
        port=cfg.mcp.port,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[
                f"{cfg.mcp.host}:{_p}",
                f"127.0.0.1:{_p}",
                f"localhost:{_p}",
                "127.0.0.1:*",
                "localhost:*",
                "[::1]:*",
            ],
            allowed_origins=[
                f"http://127.0.0.1:{_p}",
                f"http://localhost:{_p}",
                "http://127.0.0.1:*",
                "http://localhost:*",
                "http://[::1]:*",
            ],
        ),
    )
    # FastMCP otherwise reports the SDK version in the MCP initialize response.
    # The server contract must identify the Persome Runtime release instead.
    server._mcp_server.version = __version__  # noqa: SLF001

    @server.tool()
    def list_memories(include_dormant: bool = False, include_archived: bool = False) -> str:
        """**ALWAYS CALL FIRST** on the first personal-context turn of a conversation.

        List all memory files with descriptions + entry counts. Cheap (one SQLite
        query, no file reads), so the cost of calling is essentially zero.

        Call whenever the user asks about themselves, their schedule, preferences,
        or ongoing work — the response tells you which files exist and what they're
        about (e.g. `event-YYYY-MM-DD.md` for a given day's session-level activity
        log; `user-profile.md` for identity; `user-preferences.md` for habits;
        `project-*.md` / `person-*.md` / `org-*.md` for specific entities).

        If you're about to answer from chat history alone when the user has asked
        about themselves, you've skipped this tool. Go back and call it.
        """
        with fts.cursor() as conn:
            return json.dumps(
                _list_memories(
                    conn, include_dormant=include_dormant, include_archived=include_archived
                ),
                ensure_ascii=False,
            )

    @server.tool()
    def read_memory(
        path: str,
        since: str | None = None,
        until: str | None = None,
        tags: list[str] | None = None,
        tail_n: int | None = None,
    ) -> str:
        """Read the full contents of ONE memory file the user has on disk.

        Use after `list_memories` / `search` points you at a promising file.
        Entries come back chronological. Supports `since` / `until` (ISO timestamps),
        `tags` (filter by any matching tag), and `tail_n` (most recent N entries only).
        """
        path = bounded_text("path", path, maximum=512)
        since = bounded_optional_text("since", since, maximum=64)
        until = bounded_optional_text("until", until, maximum=64)
        tags = bounded_text_list(
            "tags",
            tags,
            maximum_items=64,
            maximum_item_chars=128,
        )
        tail_n = bounded_int(tail_n, minimum=1, maximum=500) if tail_n is not None else None
        with fts.cursor() as conn:
            return json.dumps(
                _read_memory(conn, path=path, since=since, until=until, tags=tags, tail_n=tail_n),
                ensure_ascii=False,
            )

    @server.tool()
    def correct_memory(correction: str) -> str:
        """Update the user's memory when they tell you something in it is WRONG.

        Call this the moment the user corrects a belief about themselves — "Peach isn't my
        name, it's a colleague", "Research Team is an org, not a person", "Alex J. and Alex Jones are the same
        person", "I don't live in Beijing anymore". Pass their correction verbatim. This is a
        directed memory UPDATE (manage memory like model weights): it traces the wrong belief
        back to its source facts, supersedes them through the memory choke-point (receipts kept —
        reversible), or retypes/merges the entity, and logs the update. Downstream summaries (the
        resident root apex, schemas) re-derive off the corrected memory. Returns what changed;
        an empty result means nothing matched (tell the user, don't invent a change).
        """
        from ..writer import correct as correct_mod

        correction = bounded_text("correction", correction, maximum=20_000)
        with fts.cursor() as conn:
            res = correct_mod.update_memory(cfg, conn, correction, source="agent")
        return json.dumps(
            {"kind": res.kind, "applied": res.applied, "reason": res.reason, "ok": res.ok},
            ensure_ascii=False,
        )

    default_top_k = cfg.search.default_top_k

    @server.tool()
    def search(
        query: str,
        paths: list[str] | None = None,
        since: str | None = None,
        until: str | None = None,
        top_k: int = default_top_k,
        include_superseded: bool = False,
        breadth: float = 0.0,
        entities: list[str] | None = None,
        include_bodies: bool = False,
    ) -> str:
        """**ALWAYS CALL** before saying "I don't know" — search the user's memory first.

        Hybrid SEMANTIC + keyword search across COMPRESSED memory: the distilled
        Markdown layer the user has decided is durable knowledge (preferences,
        decisions, schedules, project state, people, summaries). It matches by
        MEANING, not just exact words — you do NOT need the user's original
        phrasing. Describe what you're looking for in plain natural language and
        related memory surfaces even when the wording differs (semantic ranking
        is active when the memory has embeddings; otherwise it degrades to
        keyword search, same call). It does NOT search raw screen content; for
        strings the user merely typed or read on screen (error messages, code
        symbols, file paths from a doc), use `search_captures` instead, OR call
        both in parallel.

        Returns the top-k matching entries with file path + timestamp + `age_days`
        (how old the memory is). Treat a large `age_days` as a claim about the
        PAST: version numbers, task status, who-is-doing-what may have changed —
        cross-check with `verify_fact` before reporting such facts as current.
        When a hit is covered by a learned behavior regularity, it carries
        `related_faces` — the promoted pattern(s) that EXPLAIN this fact (with
        evidence counts); use them to generalize beyond the single entry. By
        default only fact-level regularities (level-1 Faces) are attached;
        pass `include_bodies=true` to also attach higher-level Volumes
        (level 2 — cross-domain fusions) covering schema-file hits.

        Examples (natural language works — don't reduce to bare keywords):
          search(query="when is the interview")          — scheduled interviews
          search(query="how does Alice want the Q3 roadmap done")  — paraphrase ok
          search(query="anything due this week")         — semantic, no exact match
          search(query="how the user prefers to be contacted")     — preferences

        `paths` takes GLOB patterns to scope search, e.g. `['event-*.md']` for
        scheduled events only, or `['project-*.md']` for project notes.

        `breadth` (0.0–1.0, default 0) trades redundancy for COVERAGE: leave 0
        when you want the most relevant answer (grounding a fact); pass 0.3–0.7
        for survey/research-style questions where you want diverse angles
        instead of near-duplicate top hits. `entities` arms the who-lookup
        directly when you KNOW who the question is about (e.g.
        `entities=["Alex"]` while the query text paraphrases) — unknown names
        are ignored, never an error.
        """
        query = bounded_text("query", query, maximum=20_000)
        paths = bounded_text_list(
            "paths",
            paths,
            maximum_items=64,
            maximum_item_chars=512,
        )
        since = bounded_optional_text("since", since, maximum=64)
        until = bounded_optional_text("until", until, maximum=64)
        top_k = bounded_int(top_k, minimum=1, maximum=50)
        breadth = bounded_float(breadth, minimum=0.0, maximum=1.0)
        entities = bounded_text_list(
            "entities",
            entities,
            maximum_items=64,
            maximum_item_chars=256,
        )
        with fts.cursor() as conn:
            return json.dumps(
                _search(
                    conn,
                    query=query,
                    paths=paths,
                    since=since,
                    until=until,
                    top_k=top_k,
                    include_superseded=include_superseded,
                    breadth=breadth,
                    entities=entities,
                    include_bodies=include_bodies,
                ),
                ensure_ascii=False,
            )

    @server.tool()
    def behavior_patterns() -> str:
        """**CALL FIRST for "who is this user / how do they work" context** — the
        learned behavior model, not raw memories.

        Returns the RESIDENT layer of the user's memory: a `root` apex (one
        synthesized "who is this person, what matters to them now" narrative)
        plus promoted behavior regularities (`faces` — patterns that survived
        two independent extraction signals AND resampling stability; each with
        evidence count + confidence). `rendered` is a ready-to-use text block.

        Use for: personalizing tone/framing, predicting what the user wants
        next, daily recaps, choosing defaults that match their working style.
        This layer is NOT reachable via `search` (it lives above the entry
        store), so searching for "behavior pattern" finds nothing; call this instead. Cheap
        (one SQLite read, no LLM). Empty `root`+`faces` just means the nightly
        schema synthesis hasn't accumulated enough signal yet.
        """
        with fts.cursor() as conn:
            return json.dumps(_behavior_patterns(conn), ensure_ascii=False)

    @server.tool()
    def get_model_snapshot(redact: bool = True) -> str:
        """Return the versioned Point/Line/Face/Volume/Root personal-model snapshot.

        This is the stable Runtime boundary used by viewers and external clients. It includes
        build metadata, provenance receipts, geometry counts, and the singleton Root. The call
        is local, read-only, and uncached. ``redact=true`` is the safe default; pass false only
        when the user explicitly needs their unredacted local model.
        """
        with fts.cursor() as conn:
            return json.dumps(_get_model_snapshot(conn, redact=redact), ensure_ascii=False)

    if getattr(cfg.mcp, "read_receipt_enabled", True):

        @server.tool()
        def read_receipt(entry_id: str) -> str:
            """Dereference ONE memory receipt — the `⟨entry_id:path⟩` handles that
            `search`'s `chains` narrative and other tools hand out.

            Returns the full entry (content, tags, timestamps, validity window,
            confidence, `superseded` flag) plus `nearby_captures` — breadcrumbs to
            the NEXT evidence layer (follow with `read_recent_capture(at=…)` for
            the raw on-screen text or explicitly request its screenshot). Use when
            you need to verify what a chain hop or a fact is actually based on;
            this is the audit trail from any memory down to the moment on screen.
            A `superseded: true` entry is history, not the current belief.
            """
            entry_id = bounded_text("entry_id", entry_id, maximum=256)
            with fts.cursor() as conn:
                return json.dumps(_read_receipt(conn, entry_id=entry_id), ensure_ascii=False)

    if getattr(cfg.mcp, "entity_graph_enabled", True):

        @server.tool()
        def entity_graph(
            name: str, depth: int = 1, as_of: str = "", include_shadow: bool = False
        ) -> str:
            """**CALL for "who is X / how does X relate to people & projects"** —
            the relation graph around one identity, optionally AS OF a past date.

            Returns the resolved canonical identity, its edges (predicate /
            label / evidence count / validity window), identities reachable
            within `depth` hops, and `chain_to_user` — how X connects back to
            the user. Pass `as_of="2026-03-01"` to ask about a PAST state
            ("who was his boss in March") — edges closed before or opened after
            that date are excluded. `include_shadow=true` additionally lists
            UNPROVEN extracted edges, clearly separated — treat them as hints,
            never facts. An unresolvable name returns an honest miss (the graph
            doesn't know them yet), not a guess. Zero LLM, one SQLite read.
            """
            name = bounded_text("name", name, maximum=512)
            depth = bounded_int(depth, minimum=1, maximum=4)
            as_of = bounded_text("as_of", as_of, maximum=64, allow_empty=True)
            with fts.cursor() as conn:
                return json.dumps(
                    _entity_graph(
                        conn,
                        cfg,
                        name=name,
                        depth=depth,
                        as_of=as_of or None,
                        include_shadow=include_shadow,
                    ),
                    ensure_ascii=False,
                )

    @server.tool()
    def verify_fact(claim: str, top_k: int = 8, fresh_within_days: int = 7) -> str:
        """**CALL BEFORE STATING time-sensitive facts as current** — freshness check
        for a single claim against the user's memory.

        Time-sensitive facts = anything that changes as work progresses: version
        numbers ("we ship 0.3.9"), task/issue status ("X is still open"), who is
        working on what, schedules, deadlines. Memory recall is ranked by
        relevance and can surface a WEEKS-OLD entry that reads like the present.

        Pass the claim you are about to state (natural language, e.g.
        `verify_fact(claim="the current version is 0.3.9")`). Returns the freshest live
        evidence with per-entry `age_days`, plus `stale` (no evidence within
        `fresh_within_days`) and a `note` telling you how to treat it. The tool
        judges TIME only — read the evidence to judge semantics yourself: if the
        freshest evidence contradicts or postdates your claim, follow the
        evidence; if everything is stale, state it as past status or ask.
        """
        claim = bounded_text("claim", claim, maximum=20_000)
        top_k = bounded_int(top_k, minimum=1, maximum=50)
        fresh_within_days = bounded_int(fresh_within_days, minimum=0, maximum=3650)
        with fts.cursor() as conn:
            return json.dumps(
                _verify_fact(conn, claim=claim, top_k=top_k, fresh_within_days=fresh_within_days),
                ensure_ascii=False,
            )

    @server.tool()
    def recent_activity(
        since: str | None = None,
        limit: int = 20,
        prefix_filter: list[str] | None = None,
    ) -> str:
        """**ALWAYS CALL** when the user references yesterday, last week, earlier, or recently.

        Newest-first cross-file feed of recent memory entries. Best tool for
        open-ended "what's new / what has the user been up to" questions:

          "what happened today?"
          "what was I doing yesterday afternoon?"
          "anything recent about <topic>?"
          "catch me up on this week"

        Use `since` (ISO timestamp) to limit to entries newer than a point in
        time, and `prefix_filter` (e.g. `['event-', 'project-']`) to scope.
        Without filters, returns the most recent N entries across ALL files.

        If the user's question has any temporal recency dimension, this tool
        runs in constant time and is strictly better than guessing.
        """
        since = bounded_optional_text("since", since, maximum=64)
        limit = bounded_int(limit, minimum=1, maximum=200)
        prefix_filter = bounded_text_list(
            "prefix_filter",
            prefix_filter,
            maximum_items=64,
            maximum_item_chars=256,
        )
        with fts.cursor() as conn:
            return json.dumps(
                _recent_activity(conn, since=since, limit=limit, prefix_filter=prefix_filter),
                ensure_ascii=False,
            )

    @server.tool()
    def attention_trajectory(
        since: str | None = None,
        until: str | None = None,
        hours: int = 24,
    ) -> str:
        """Where the user's attention went, with DWELL (time actually spent).

        Answers "what did I spend my attention / focus / time on today / this
        morning / the last N hours". Prefer this over guessing from raw activity
        whenever the question is about focus or time spent.

        Built from the timeline's per-block attention locus (the code-resolved
        focused region of each window): contiguous runs of the same surface — a
        window, a terminal pane, a web page — coalesced with their dwell. Returns:

          - ``by_dwell``: surfaces ranked by TOTAL time spent, longest first
            (what actually mattered), each with ``dwell_minutes`` + the locus
            ``rung`` (pane/content/editing/cursor/focus/fallback).
          - ``trajectory``: the chronological path through surfaces with per-span
            dwell — how the focus moved over the window.

        Arguments:
          since — ISO8601 lower bound. Omit to use the last ``hours``.
          until — ISO8601 upper bound. Omit for "up to now".
          hours — when ``since`` is omitted, look back this many hours (default 24).

        Note: dwell is derived from the per-block ``attention_surface`` the
        timeline aggregator stamps; it is populated going forward, so the window
        only reflects activity captured since the attention-locus pipeline shipped.
        """
        since = bounded_optional_text("since", since, maximum=64)
        until = bounded_optional_text("until", until, maximum=64)
        hours = bounded_int(hours, minimum=1, maximum=8760)
        now = datetime.now().astimezone()
        start = _parse_iso_opt(since) or (now - timedelta(hours=hours))
        end = _parse_iso_opt(until)
        with fts.cursor() as conn:
            spans = attention_traj.attention_trajectory(conn, start, end)
        payload = attention_traj.trajectory_summary(spans)
        payload["window"] = {"since": start.isoformat(), "until": (end or now).isoformat()}
        return json.dumps(payload, ensure_ascii=False)

    @server.tool()
    def read_recent_capture(
        at: str | None = None,
        app_name: str | None = None,
        window_title_substring: str | None = None,
        include_screenshot: bool = False,
        include_ax_tree: bool = False,
        max_age_minutes: int = 15,
    ) -> str:
        """Hydrate ONE raw screen capture — the actual visible_text, focused
        input value, URL, and (optionally) screenshot from the buffer.

        Use this whenever a compressed memory entry isn't specific enough
        (e.g. an event-daily entry says "edited main.py at 14:30" but you
        need the actual code, or "read article" but you need the text).
        Most event-daily sub_tasks include an inline `raw:
        read_recent_capture(at=..., app_name=...)` breadcrumb — call it
        verbatim. For keyword-driven searches across the whole buffer, prefer
        `search_captures` first; this tool fetches one specific moment.

        Arguments:
          at                      — ISO timestamp ("2026-04-22T14:30") or bare
                                    "HH:MM[:SS]" (today local). Omit for the
                                    newest matching capture.
          app_name                — case-insensitive substring of the app name
                                    (e.g. "Cursor", "Claude", "Chrome").
          window_title_substring  — case-insensitive substring of the window
                                    title (e.g. a filename, tab title).
          include_screenshot      — include the base64 JPEG. Default false —
                                    screenshots are large and rarely needed.
          include_ax_tree         — progressive-disclosure "expand": return the
                                    full `ax_tree`, including the browser chrome
                                    (bookmarks / tabs / extensions) that a
                                    browser's `visible_text` folds into a
                                    one-line digest. Default false — large; use
                                    only when you need the folded chrome detail.
          max_age_minutes         — when `at` is given, only return captures
                                    within this many minutes of `at`. Default 15.

        Returns the matching capture as JSON with `timestamp`, `app_name`,
        `window_title`, `url`, `focused_element.value` (what the user was
        typing), and `visible_text` (~10 k chars of rendered AX text). The buffer
        retention is bounded (see `[capture]` in config); older captures have
        their `screenshot` field stripped but keep text. Returns `null` if
        nothing matches.

        Typical flow: read an event-daily entry, notice `[HH:MM-HH:MM, <app>]`,
        then call this with `at="HH:MM"` and `app_name="<app>"` to see the
        actual content from that moment.
        """
        at = bounded_optional_text("at", at, maximum=128)
        app_name = bounded_optional_text("app_name", app_name, maximum=512)
        window_title_substring = bounded_optional_text(
            "window_title_substring",
            window_title_substring,
            maximum=512,
        )
        max_age_minutes = bounded_int(max_age_minutes, minimum=1, maximum=10_080)
        result = captures_mod.read_recent_capture(
            at=at,
            app_name=app_name,
            window_title_substring=window_title_substring,
            include_screenshot=include_screenshot,
            include_ax_tree=include_ax_tree,
            max_age_minutes=max_age_minutes,
        )
        return json.dumps(result, ensure_ascii=False)

    @server.tool()
    def search_captures(
        query: str,
        since: str | None = None,
        until: str | None = None,
        app_name: str | None = None,
        limit: int = 10,
    ) -> str:
        """**ALWAYS CALL** (usually in parallel with `search`) when the user mentions a keyword they'd have typed or read on screen.

        Keyword search over RAW screen captures (the uncompressed S1 layer).
        PREFER this over `search` when the user mentions a keyword they would
        have *typed* or *read on screen* but that may not have made it into a
        compressed memory entry yet — e.g. "find when I saw the term
        'rate limiter'", "what was that error about pyobjc", "the URL I had
        open about Postgres replication". `search` only sees compressed memory;
        this sees every captured screen. When you're not sure which layer has
        it, call both — they're independent indexes and neither is expensive.

        Returns the top-`limit` matching captures (BM25-ranked) with snippet
        highlighting (matched tokens wrapped in `[...]`). Each hit includes
        `file_stem` — pass that as `at` to `read_recent_capture` to get the
        full visible_text.

        Examples:
          search_captures(query="rate limiter")             — find any time it appeared
          search_captures(query="error", app_name="Cursor") — keyword scoped to one app
          search_captures(query="todo", since="2026-04-22T09:00:00+08:00")

        Arguments:
          query     — free-text keywords. FTS5-tokenized (case-insensitive).
          since     — ISO timestamp lower bound on capture time.
          until     — ISO timestamp upper bound on capture time.
          app_name  — case-insensitive substring on the capturing app name.
          limit     — top-K BM25 hits to return.
        """
        query = bounded_text("query", query, maximum=20_000)
        since = bounded_optional_text("since", since, maximum=64)
        until = bounded_optional_text("until", until, maximum=64)
        app_name = bounded_optional_text("app_name", app_name, maximum=512)
        limit = bounded_int(limit, minimum=1, maximum=50)
        results = captures_mod.search_captures(
            query=query,
            since=since,
            until=until,
            app_name=app_name,
            limit=limit,
        )
        return json.dumps({"query": query, "results": results}, ensure_ascii=False)

    @server.tool()
    def current_context(
        app_filter: str | None = None,
        headline_limit: int = 5,
        fulltext_limit: int = 3,
        timeline_limit: int = 8,
    ) -> str:
        """**ALWAYS CALL** for present-tense or ambiguous-pronoun questions about the user's state.

        Two high-value trigger patterns:
          1. Present-tense: *"right now / currently / just now / what am I /
             what's open"* — this is the tool.
          2. Pronoun with no in-conversation antecedent: *"that / this / it /
             the bug / the error / the file"* —
             the user is pointing at their screen, not at chat history.

        Never reply with "I don't have code/context to look at" or ask the user
        to paste something — call this tool first. If it comes back empty,
        then ask. Asking for a paste when this tool would have worked is a
        tool-selection failure.

        Returns a one-shot snapshot of the current screen state — the same kind of
        context you would get if every chat turn began with the user narrating
        their environment. Triggers include:

          - "what am I working on?"
          - "what's open in front of me?"
          - "is the deploy log still streaming?"
          - "summarize the doc I'm reading"

        Returns three sections:

          recent_captures_headline    : last ~5 captures as compact lines
                                        ([HH:MM] App — Window [Role]) — quick
                                        scan of "what apps + windows are live".
          recent_captures_fulltext    : top ~3 captures deduplicated by
                                        (app, window) carrying the FULL
                                        visible_text and focused_element.value
                                        — the actual content on screen.
          recent_timeline_blocks      : the last ~8 1-minute timeline blocks
                                        (LLM-summarized activity slices) so
                                        you can see how the current moment
                                        was reached.

        For drill-down on any specific capture or moment, call
        `read_recent_capture(at=..., app_name=...)` next.
        """
        app_filter = bounded_optional_text("app_filter", app_filter, maximum=512)
        headline_limit = bounded_int(headline_limit, minimum=0, maximum=50)
        fulltext_limit = bounded_int(fulltext_limit, minimum=0, maximum=20)
        timeline_limit = bounded_int(timeline_limit, minimum=0, maximum=50)
        result = captures_mod.current_context(
            app_filter=app_filter,
            headline_limit=headline_limit,
            fulltext_limit=fulltext_limit,
            timeline_limit=timeline_limit,
        )
        return json.dumps(result, ensure_ascii=False)

    @server.tool()
    def get_schema() -> str:
        """Return the memory organization spec (file naming, what each prefix means).

        Rarely needed at query time. Useful only if you need to reason about WHERE
        a new fact would be stored, or explain to the user how their memory is
        organized. For normal "look up a fact" flows, use `search` / `list_memories`
        directly.
        """
        return json.dumps(_get_schema(), ensure_ascii=False)

    # ─── Agent-Native Persome: memory write-back (the loop, Phase 3) ──────────────────────
    from . import memory_write as _memory_write

    @server.tool()
    def remember(content: str, tags: str = "", run_id: str = "") -> str:
        """Write a durable finding back into Persome memory so later agents
        can reuse it. Call this when you learn
        something durable about the user, their project, a tool, or a decision while running.

        Your entry is force-tagged `source:agent-run` (so it stays distinguishable from the
        user's own notes). Pass `run_id` = the value of your `$PERSOME_TASK_ID` env var so the
        finding is attributed to this run.

        Arguments:
          content — the finding to remember (a self-contained sentence or two).
          tags    — optional comma-separated extra tags (e.g. "project-x,decision").
          run_id  — optional; your `$PERSOME_TASK_ID` for per-run attribution.
        """
        content = bounded_text("content", content, maximum=50_000)
        tags = bounded_text("tags", tags, maximum=4096, allow_empty=True)
        run_id = bounded_text("run_id", run_id, maximum=256, allow_empty=True)
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        if len(tag_list) > 64:
            raise ValueError("tags exceeds 64 comma-separated items")
        bounded_text_list(
            "tags",
            tag_list,
            maximum_items=64,
            maximum_item_chars=128,
        )
        with fts.cursor() as conn:
            return json.dumps(
                _memory_write.remember(conn, content=content, tags=tag_list, run_id=run_id),
                ensure_ascii=False,
            )

    from ..api import register_routes

    register_routes(server, cfg, auth_enabled=auth_enabled)
    return server


def _watch_parent_loop(
    initial_ppid: int,
    *,
    poll_seconds: float,
    grace_seconds: float,
    _getppid: Callable[[], int] = os.getppid,
    _sleep: Callable[[float], None] = time.sleep,
    _kill: Callable[[int, int], None] = os.kill,
    _exit: Callable[[int], None] = os._exit,
) -> None:
    """Poll until the spawning client dies, then shut the whole process down.

    Structured like ``cli._watch_parent_death`` (SIGTERM, grace, ``os._exit``
    backstop). On this stdio path no SIGTERM handler is installed — FastMCP's
    stdio transport registers none — so the default disposition terminates the
    process at the SIGTERM, which is safe: the client is gone, no request is
    in flight, and the store is WAL SQLite. The grace window and hard-exit
    backstop only engage if a handler is ever added (e.g. a future FastMCP),
    where they stop a hung shutdown from stranding an orphan whose non-daemon
    readline thread never unblocks. The keyword-only callables are test seams.
    """
    while True:
        _sleep(poll_seconds)
        if _getppid() != initial_ppid:
            logger.info("stdio client (pid %d) exited — shutting down", initial_ppid)
            _kill(os.getpid(), signal.SIGTERM)
            _sleep(grace_seconds)
            _exit(0)


def _start_parent_watchdog(
    poll_seconds: float = 3.0,
    grace_seconds: float = 5.0,
    _getppid: Callable[[], int] = os.getppid,
) -> None:
    """Exit the stdio server once the MCP client that spawned it is gone.

    Stdio servers normally end when stdin reaches EOF, but a client killed
    without closing the pipe (write end inherited by a still-alive session
    leader) never delivers EOF, and orphaned ``persome mcp`` processes
    accumulate silently. Reparenting (``os.getppid()`` changing, to launchd on
    macOS or init/subreaper on Linux) is the reliable death signal. A server
    already running with ppid 1 has nothing to watch: either launchd spawned
    it deliberately, or the client died before we armed — callers narrow that
    race by arming first, before any other startup work.
    """
    initial = _getppid()
    if initial == 1:
        return
    threading.Thread(
        target=_watch_parent_loop,
        args=(initial,),
        kwargs={"poll_seconds": poll_seconds, "grace_seconds": grace_seconds},
        name="mcp-parent-watchdog",
        daemon=True,
    ).start()


def run_stdio() -> None:
    """Run the server on stdio. Blocks until the client disconnects."""
    # Belt and braces for clients that die without closing the pipe: without
    # this, orphaned stdio servers pile up and can race integrity recovery.
    # Armed before build_server so a client death during startup still gets
    # caught (ppid is captured while the client is most likely alive).
    _start_parent_watchdog()
    # Stdio has no HTTP request surface and therefore no bearer header.  Keep it
    # explicitly outside the local HTTP authentication boundary.
    server = build_server(auth_enabled=False)
    server.run()  # FastMCP.run() uses stdio by default


async def run_async(cfg: Config | None = None, *, transport: str | None = None) -> None:
    """Run the MCP server with the configured transport (for use inside the daemon)."""
    cfg = cfg or load_config()
    transport = transport or cfg.mcp.transport
    auth_enabled = transport != "stdio"
    if auth_enabled:
        validate_bind_host(cfg.mcp.host)
    server = build_server(cfg, auth_enabled=auth_enabled)
    if transport == "stdio":
        await server.run_stdio_async()
    elif transport == "sse":
        logger.info("MCP SSE server: http://%s:%d/sse", cfg.mcp.host, cfg.mcp.port)
        await server.run_sse_async()
    elif transport == "streamable-http":
        logger.info("MCP HTTP server: http://%s:%d/mcp", cfg.mcp.host, cfg.mcp.port)
        await server.run_streamable_http_async()
    else:
        raise ValueError(f"unknown MCP transport: {transport!r}")


def endpoint_url(cfg: Config) -> str:
    """Return the public URL where the daemon-hosted MCP server is reachable."""
    transport = cfg.mcp.transport
    if transport == "sse":
        return loopback_http_url(cfg.mcp.host, cfg.mcp.port, "/sse")
    if transport == "streamable-http":
        return loopback_http_url(cfg.mcp.host, cfg.mcp.port, "/mcp")
    raise ValueError(f"endpoint_url only supported for sse/http, got {transport!r}")

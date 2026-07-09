"""MCP server exposing Persome memory as read-only tools.

Uses the official `mcp` Python SDK via FastMCP. Runs either standalone
over stdio (`persome mcp`) or in-daemon over streamable-http / sse,
depending on `[mcp] transport`. Exposes:

  Compressed memory (Markdown layer):
    list_memories, read_memory, search, verify_fact, behavior_patterns,
    entity_graph, read_receipt, recent_activity
  Raw captures (S1 buffer):
    current_context, search_captures, read_recent_capture
  Reference:
    get_schema
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import Any

from ..config import Config
from ..config import load as load_config
from ..intent import store as intent_store
from ..logger import get
from ..prompts import load as load_prompt
from ..store import cooldown_suppressions as cooldown_suppressions_store
from ..store import files as files_mod
from ..store import fts
from ..store import parser_ticks as parser_ticks_store
from ..store import recall_budget_ticks as recall_budget_ticks_store
from ..timeline import attention_trajectory as attention_traj
from . import captures as captures_mod

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
            # 轴D staleness signal (issue #557): how old this memory is, in days —
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
            # very fact (level-1 面 via member_key membership — the same
            # deterministic link Phase 3's utility credit walks; level-2 体 by
            # path, opt-in via include_bodies). Present only when a face
            # actually covers the hit; the resident layer as a whole stays
            # behavior_patterns()'s job (§3.1 塔顶常驻，不随检索重复交付).
            row["related_faces"] = faces
        results.append(row)
    out: dict[str, Any] = {"query": query, "results": results}
    if chains_text:
        # §3.4 链交付：路径即叙事 + 收据指针（⟨entry_id:path⟩ = §2.1 渐进披露把手）
        out["chains"] = chains_text
    return out


def _face_membership_index(  # type: ignore[no-untyped-def]
    conn, *, include_bodies: bool = False
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Membership index for the E1.5 recall↔schema 关联 — 面 by default, 体 opt-in.

    Level-1 面: ``members`` IS the fact bodies' member_key hashes → a fact hit's
    covering regularity is a deterministic lookup (the read-side twin of the
    membership link Phase 3's utility credit walks on the write side). Level-2
    体: ``members`` are the parent SCHEMA FILE names (there is no stored 面→体
    pointer today — parent_face is unpopulated), so the honest deterministic
    coverage for a 体 is BY PATH — it covers hits coming from its member schema
    files' md 投影. Fail-open: a store predating ``schema_faces`` (or a bad
    members blob) yields an empty index, never an error."""
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
    """§3.1 常驻投影 over MCP (issue #557 follow-up).

    The learned behavior model — the level-3 root apex ("who is this person")
    plus the promoted (both-provenance + resampling-stable) schema faces — used
    to reach ONLY the app-side grounding digest and the recognizer's
    schema_prior seam. Per the 满血版 principle, MCP-side callers get the same
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
    """轴D 事实校验 (issue #557) — deterministic, zero-LLM.

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
        note = "记忆中没有相关证据——不要凭空陈述该论断。"
    elif stale:
        note = (
            f"最新相关证据已是 {freshest} 天前。版本号/状态/职责/进行中事项这类"
            "随时间变化的事实可能已过时：只把它作为当时的状态陈述，或先向用户/最新来源核实。"
        )
    else:
        note = (
            f"存在 {freshest} 天内的新鲜证据。请逐条核对证据内容是否真的支持该论断"
            "（工具只保证时效，不判断语义）。"
        )
    return {
        "claim": claim,
        "evidence": evidence,
        "freshest_age_days": freshest,
        "stale": stale,
        "note": note,
    }


def _read_receipt(conn, *, entry_id: str) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """E1.3 — dereference one ``⟨entry_id:path⟩`` receipt handle (§2.1 渐进披露).

    The chain delivery hands out receipt POINTERS; this is the one-hop
    drill-down that turns a pointer into the entry itself plus the breadcrumbs
    to the NEXT disclosure layer (nearby captures → ``read_recent_capture`` /
    ``view_capture``). Superseded entries are readable — a receipt is
    archaeology, not a claim about the present — and are labeled as such.
    Reading a receipt reinforces it (读即强化)."""
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
    # epoch-based proximity: entry/capture timestamps mix 'T' and ' ' separators,
    # so lexicographic BETWEEN misorders — strftime('%s', …) parses both.
    captures = conn.execute(
        "SELECT id, timestamp, app_name, window_title FROM captures"
        " WHERE abs(strftime('%s', timestamp) - strftime('%s', ?)) <= 1800"
        " ORDER BY abs(strftime('%s', timestamp) - strftime('%s', ?)) LIMIT 3",
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
        # follow with read_recent_capture(at=…) or view_capture for pixels.
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
            "note": f"'{name}' 未能消解为已知身份——图里还没有这个人/物，或提法歧义（不硬猜）。",
        }
    canonical = res.canonical

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


def _list_intents(  # type: ignore[no-untyped-def]
    conn, *, scope: str | None = None, status: str | None = None, limit: int = 50
) -> dict[str, Any]:
    limit = max(1, min(200, limit))
    intents = intent_store.recent_intents(conn, start="", end="￿", scope=scope, status=status)
    intents = intents[-limit:][::-1]  # newest first, capped
    return {"count": len(intents), "intents": [i.to_dict() for i in intents]}


def _set_intent_status(conn, *, intent_id: int, status: str) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    # R3 feedback口 whitelist (#631 nit T): clients may only express the three
    # USER-feedback statuses; ``armed``/``expired`` are engine-owned.
    if status not in intent_store.FEEDBACK_STATUSES:
        return {
            "success": False,
            "error": f"invalid status '{status}', expected one of {intent_store.FEEDBACK_STATUSES}",
        }
    ok = intent_store.update_intent_status(conn, intent_id=intent_id, new_status=status)
    return {"success": ok, "intent_id": intent_id, "new_status": status}


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
  - `entities=["张伟"]` when you KNOW who the question is about (aliases resolve;
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
  `chains` narrative → `read_receipt(entry_id)` → `read_recent_capture(at=…)` → pixels (`view_capture`, when available).

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


# Appended to the server instructions ONLY when actuation is enabled — the Computer tools.
_COMPUTER_USE_INSTRUCTIONS = """\

## Computer use — operating the user's Mac (the `ui_*` tools)

When the task needs you to actually DO something on screen (open an app, click a button, fill a
field, run a menu command), you can drive the user's Mac with the `ui_*` tools. You are operating
the user's real machine — be deliberate, and never send/delete/pay without the user's go-ahead.

### The loop: look → act → verify

1. **Open / activate** the target app first:
   - For a WEB task (open a page, fill a form, create a meeting), call `ui_open_app(app, url, note)` —
     Persome opens a browser the NO-STEAL way (its own off-screen instance for multi-instance apps; a
     consent-gated borrow for single-instance ones) so the user's screen isn't disturbed. It returns an
     `app_pid` — pass that `app_pid` to EVERY subsequent verb so they hit the staged instance and not
     the user's own copy of the app (the app name alone is ambiguous). Click within `window_bounds`.
   - Otherwise `ui_activate(app)` (name like "Calculator" or a bundle id). The keyboard verbs act on
     whatever app is focused, so switch focus explicitly.
   - **App guides (read them):** some apps have quirks (AX-rich vs OCR-only vs fully blind, input
     idioms, send traps). The first time you focus such an app, `ui_activate`/`ui_open_app` appends its
     operation guide — follow it. You can also pull it any time with `ui_app_guide(app)` (which lists
     every app that has a guide when there's none for that app). Don't rediscover an app's quirks by
     trial and error on the user's real app — check the guide.
2. **Look** before you act:
   - `ui_find(app, query)` — find a control by its label/value. This is your default; it's cheap
     and returns the `id` the click/set-value verbs need.
   - `ui_snapshot(app)` — the full element graph, when you need to survey what's there.
   - `ui_ocr_locate(app, query)` — ONLY for AX-poor apps (WeChat/Feishu, custom-drawn UIs) where
     `ui_find` comes back empty. Returns screen coordinates for `ui_click_xy`.
3. **Act**, preferring the most precise tool available:
   - `ui_click(app, id, note)` — AXPress a real element (best; returns a diff proving it landed).
   - `ui_perform(app, id, action, note)` — run a NAMED AX action a control advertises when AXPress
     does nothing: a stepper's `AXIncrement`/`AXDecrement`, a dropdown's `AXShowMenu`, a list's
     `AXPick`. See "Filling forms" below.
   - `ui_set_value(app, id, text, note)` — set an AX-addressable field's value.
   - `ui_type(app, text, note)` — type into the focused field when no `id` exists (pixel/Electron
     search boxes). Focus it first.
   - `ui_key(app, keys, note)` — shortcuts & menu keys (`cmd+a`, `cmd+shift+p`, `enter`, `shift+=`).
     Menu items are pressable as AX elements too — `ui_find` the menu item and `ui_click` it.
   - `ui_click_xy(app, x, y, note)` — LAST resort, only with coordinates from `ui_ocr_locate`.
4. **Verify** from the returned AX diff (`verified: true` = something changed). If a step didn't
   land, look again rather than blindly repeating.

### Filling forms & stubborn controls (the general rule)

A control that won't respond to `ui_click`/`ui_click_xy` is usually NOT broken — it just doesn't take
an AXPress. EVERY element lists its real `actions` in the snapshot; read them and use the matching one
instead of forcing clicks:

- **Date / time / number steppers** (a value that won't change on click): use `ui_perform` with
  `AXIncrement` / `AXDecrement`, one call per step (e.g. +2 days = two AXIncrement). First try
  `ui_set_value` if the field is directly editable; fall back to stepping.
- **Dropdowns / popup buttons / comboboxes**: `ui_perform` `AXShowMenu` to open, then `ui_click` the
  revealed item (or `AXPick`).
- **Text fields**: `ui_set_value` (AX) or, for pixel/Electron boxes, focus + `ui_type`.

Do NOT burn turns retrying a click that does nothing, and do NOT reach for AppleScript/coordinates as a
first resort — check the element's `actions` and pick the right verb. If `ui_click` fails it returns a
`hint` naming the actions to use; follow it.

### The `note` argument is required and user-facing

Every acting verb takes a `note`: a SHORT, goal-level sentence about what THIS step is for — the
user sees it in a floating cursor bubble as Persome operates ("正在打开计算器", "Filling in the
address"). Keep it about the goal, not the mechanics.

### Safety — you do not get to send things on your own

Irreversible / outward actions (Send, Delete, Pay, Submit, an Enter that sends a message) are
**gated**: the tool call will pause and ask the user to approve in a Persome dialog. If they decline,
the tool returns `{"error": "denied"}` — accept that and stop; do not try to route around it with a
coordinate click or a different verb. Some apps (Passwords, System Settings) are blocked entirely.
Never type passwords, card numbers, or other secrets into any field.
"""


def build_server(cfg: Config | None = None):  # type: ignore[no-untyped-def]
    """Construct and return a FastMCP server instance (not yet running)."""
    from mcp.server.fastmcp import FastMCP  # lazy import

    cfg = cfg or load_config()
    # #557 design principle: MCP-side callers get the 满血版 memory. The read-path
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

    _p = cfg.mcp.port
    # Append the Computer-use guidance only when the tools are actually registered.
    instructions = _SERVER_INSTRUCTIONS
    if getattr(cfg, "actuation_enabled", False):
        instructions += _COMPUTER_USE_INSTRUCTIONS
    server = FastMCP(
        "persome",
        instructions=instructions,
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
        with fts.cursor() as conn:
            return json.dumps(
                _read_memory(conn, path=path, since=since, until=until, tags=tags, tail_n=tail_n),
                ensure_ascii=False,
            )

    @server.tool()
    def correct_memory(correction: str) -> str:
        """Update the user's memory when they tell you something in it is WRONG.

        Call this the moment the user corrects a belief about themselves — "桃子 isn't my
        name, it's a colleague", "研发群 is an org not a person", "小张 and 张三 are the same
        person", "I don't live in Beijing anymore". Pass their correction verbatim. This is a
        directed memory UPDATE (manage memory like model weights): it traces the wrong belief
        back to its source facts, supersedes them through the memory choke-point (receipts kept —
        reversible), or retypes/merges the entity, and logs the update. Downstream summaries (the
        resident root apex, schemas) re-derive off the corrected memory. Returns what changed;
        an empty result means nothing matched (tell the user, don't invent a change).
        """
        from ..writer import correct as correct_mod

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
        default only the fact-level regularities (level-1 面) are attached;
        pass `include_bodies=true` to also attach the higher-level 体
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
        `entities=["张伟"]` while the query text paraphrases) — unknown names
        are ignored, never an error.
        """
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
        store) — searching "行为模式" finds nothing; call this instead. Cheap
        (one SQLite read, no LLM). Empty `root`+`faces` just means the nightly
        schema synthesis hasn't accumulated enough signal yet.
        """
        with fts.cursor() as conn:
            return json.dumps(_behavior_patterns(conn), ensure_ascii=False)

    if getattr(cfg.mcp, "read_receipt_enabled", True):

        @server.tool()
        def read_receipt(entry_id: str) -> str:
            """Dereference ONE memory receipt — the `⟨entry_id:path⟩` handles that
            `search`'s `chains` narrative and other tools hand out.

            Returns the full entry (content, tags, timestamps, validity window,
            confidence, `superseded` flag) plus `nearby_captures` — breadcrumbs to
            the NEXT evidence layer (follow with `read_recent_capture(at=…)` for
            the raw on-screen text, or `view_capture` for the pixels). Use when
            you need to verify what a chain hop or a fact is actually based on;
            this is the audit trail from any memory down to the moment on screen.
            A `superseded: true` entry is history, not the current belief.
            """
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
        `verify_fact(claim="当前版本是 0.3.9")`). Returns the freshest live
        evidence with per-entry `age_days`, plus `stale` (no evidence within
        `fresh_within_days`) and a `note` telling you how to treat it. The tool
        judges TIME only — read the evidence to judge semantics yourself: if the
        freshest evidence contradicts or postdates your claim, follow the
        evidence; if everything is stale, state it as past status or ask.
        """
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
        """**ALWAYS CALL** when the user references "yesterday / last week / earlier / 刚才 / 上周" etc.

        Newest-first cross-file feed of recent memory entries. Best tool for
        open-ended "what's new / what has the user been up to" questions:

          "what happened today?" / "今天做了啥？"
          "what was I doing yesterday afternoon?"
          "anything recent about <topic>?"
          "catch me up on this week"

        Use `since` (ISO timestamp) to limit to entries newer than a point in
        time, and `prefix_filter` (e.g. `['event-', 'project-']`) to scope.
        Without filters, returns the most recent N entries across ALL files.

        If the user's question has any temporal recency dimension, this tool
        runs in constant time and is strictly better than guessing.
        """
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
        now = datetime.now().astimezone()
        start = _parse_iso_opt(since) or (now - timedelta(hours=max(1, hours)))
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
             what's open / 现在 / 刚才 / 我在"* — this is the tool.
          2. Pronoun with no in-conversation antecedent: *"that / this / it /
             the bug / the error / the file / 那个 / 这个 / 这段 / 这个问题"* —
             the user is pointing at their screen, not at chat history.

        Never reply with "I don't have code/context to look at" or ask the user
        to paste something — call this tool first. If it comes back empty,
        then ask. Asking for a paste when this tool would have worked is a
        tool-selection failure.

        Returns a one-shot snapshot of the current screen state — the same kind of
        context you would get if every chat turn began with the user narrating
        their environment. Triggers include:

          - "what am I working on?" / "我在干嘛？"
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

    @server.tool()
    def list_intents(scope: str = "", status: str = "", limit: int = 50) -> str:
        """List recognized intents from the unified intent stream (newest first).

        Intents are what recognizers (timeline tagging, the session-level
        trajectory recognizer, meeting packs) extracted as the user's actionable
        intent — meetings, reminders, info needs. Use this to see what's been
        recognized and its status.

        This is a raw debug view: it does NOT filter out past-``valid_until``
        rows, and ``status`` accepts the full lifecycle set, not just the
        user-feedback subset.

        Arguments:
          scope   — filter by scene, e.g. 'timeline' or 'session-<id>' (default: all).
          status  — filter by one of 'open' / 'armed' / 'consumed' / 'dismissed'
                    / 'expired' (default: all).
          limit   — max intents, clamped to [1, 200]. Default 50.
        """
        with fts.cursor() as conn:
            return json.dumps(
                _list_intents(
                    conn,
                    scope=scope or None,
                    status=status or None,
                    limit=limit,
                ),
                ensure_ascii=False,
            )

    @server.tool()
    def set_intent_status(intent_id: int, status: str) -> str:
        """Set a recognized intent's status (R3 feedback).

        Use 'consumed' when the user acted on the intent, 'dismissed' when they
        rejected it (the recognizer treats recently dismissed intents as a
        negative prior and avoids re-surfacing the same kind), or 'open' to
        reset.

        Arguments:
          intent_id — numeric id from list_intents.
          status    — 'open' / 'consumed' / 'dismissed'.
        """
        with fts.cursor() as conn:
            return json.dumps(
                _set_intent_status(conn, intent_id=intent_id, status=status),
                ensure_ascii=False,
            )

    @server.tool()
    def current_work_context() -> str:
        """The user's CURRENT work threads — the "现在进行时" answer.

        Use when the question is "what am I working on (now / these days)",
        "how long have I been on X", or any task that should align with the
        user's ongoing undertaking. Returns the active thread (title, origin —
        who assigned it and the verbatim quote — cumulative minutes with an
        `approximate` marker, recent progress) plus background threads and the
        churn/revive telemetry. Complements `current_context` (raw screen
        snapshot): this is the IDENTITY axis (which undertaking), that is the
        TIME axis (what is on screen).
        """
        from ..workthread import review as workthread_review

        with fts.cursor() as conn:
            return json.dumps(workthread_review.current_work_context(conn), ensure_ascii=False)

    @server.tool()
    def correct_work_thread(
        thread_id: str, action: str, rename: str = "", into_id: str = ""
    ) -> str:
        """Correct a work thread (the zero-cost correction port, closed set).

        Actions: 'confirm' (划分是对的), 'not_this' (这不是一条真实的线),
        'rename' (改名 — pass `rename`), 'merge' (两条是一件事 — pass
        `into_id`), 'pin' (人工确认线：免疫 merge 吸收/stale 收割). Every call
        also mints a ground-truth label that calibrates thread confidence.
        """
        from ..workthread import review as workthread_review

        with fts.cursor() as conn:
            return json.dumps(
                workthread_review.apply_correction(
                    conn,
                    thread_id=thread_id,
                    action=action,
                    new_title=rename,
                    into_id=into_id,
                    source="mcp",
                ),
                ensure_ascii=False,
            )

    @server.tool()
    def parser_stats(since: str = "", until: str = "") -> str:
        """Per-app message-parser hit-rate telemetry.

        The timeline aggregator records one tick per window, bucketed by app
        bundle_id: HIT (a registered per-app parser rendered a non-empty
        conversation), MISS (the app had a parser but it declined/rendered
        empty/raised), or FALLBACK (no app in the window had a parser). Use this
        to confirm the parsers are firing and to catch drift — e.g. a 飞书 UI
        revision that breaks the parser shows up as HIT decaying into MISS for
        bundle com.electron.lark.

        Returns: total, by_outcome {hit, miss, fallback}, by_bundle
        {<bundle>: {hit, miss, fallback}}, and hit_rate (hit ÷ total).

        Arguments:
          since — ISO8601 lower bound (inclusive); default: all time.
          until — ISO8601 upper bound (exclusive); default: all time.
        """
        with fts.cursor() as conn:
            return json.dumps(
                parser_ticks_store.stats(conn, since=since or "", until=until or "￿"),
                ensure_ascii=False,
            )

    @server.tool()
    def recall_budget_stats(since: str = "", until: str = "") -> str:
        """Recall budget squeeze-rate telemetry.

        Every ``assemble_background`` call (slow-path recognizer, meeting
        analyzer) records one row: how the shared ``max_chars`` budget was spent
        per layer (schema_prior/scene/behavior/fact/keyword/trail) and whether
        any candidate text was REJECTED for lack of budget (= squeezed). The
        2026-06-10 ablation showed squeezing key memories out collapses
        negative-suppression; this telemetry measures how often that actually
        happens in production, gating the "raise max_chars to 2400" decision.

        Returns: total_ticks, squeezed_ticks, squeeze_rate, by_layer
        {<layer>: {admitted, admitted_chars, rejected, rejected_chars,
        squeezed_ticks}}, rejected_share {<layer>: share of all rejections},
        avg_used, avg_max_chars.

        Arguments:
          since — ISO8601 lower bound (inclusive); default: all time.
          until — ISO8601 upper bound (exclusive); default: all time.
        """
        with fts.cursor() as conn:
            return json.dumps(
                recall_budget_ticks_store.stats(conn, since=since or "", until=until or "￿"),
                ensure_ascii=False,
            )

    # ─── Agent-Native Persome: the app's own task / settings / meeting state ──────────────
    # Read-only projections of `~/.persome/*.json` (the Swift app is the sole writer), so a
    # dispatched agent can see the whole Persome workspace, not just chronicle memory.
    # Spec: docs/superpowers/specs/2026-06-25-agent-native-persome-design.md (Phase 2).
    from . import appdata as _appdata

    @server.tool()
    def list_tasks(status: str = "", limit: int = 50) -> str:
        """List the user's Persome tasks (newest first) — what they've queued/run across agents.

        Metadata only (no log bodies): id, title, status, agent, provenance, working dir,
        timestamps, sessionId. Call `read_task(id)` for the full prompt + log.

        Arguments:
          status — filter to one of queued/running/needsReview/blocked/failed/cancelled/accepted
                   (default: all).
          limit  — max tasks, clamped to [1, 500]. Default 50.
        """
        return json.dumps(
            _appdata.list_tasks(status=status or None, limit=limit), ensure_ascii=False
        )

    @server.tool()
    def read_task(task_id: str) -> str:
        """One Persome task in full — prompt, status, session, turns, AND the captured log body
        (from the app's per-task log sidecar). Returns `{}` when the id isn't found.

        Arguments:
          task_id — the task id (uuid) from `list_tasks`.
        """
        return json.dumps(_appdata.read_task(task_id=task_id) or {}, ensure_ascii=False)

    @server.tool()
    def read_settings() -> str:
        """The user's Persome settings (agent templates, model/effort, toggles). Every BYO provider
        secret is REDACTED — you never receive API keys/tokens, only `<redacted>` placeholders.
        """
        return json.dumps(_appdata.read_settings(), ensure_ascii=False)

    @server.tool()
    def list_meetings(limit: int = 50) -> str:
        """List recorded meetings (id, title, status, timestamps), newest first. `read_meeting(id)`
        for the transcript.

        Arguments:
          limit — max meetings, clamped to [1, 500]. Default 50.
        """
        return json.dumps(_appdata.list_meetings(limit=limit), ensure_ascii=False)

    @server.tool()
    def read_meeting(meeting_id: str) -> str:
        """One meeting record in full, including its transcript text when present. `{}` if not found.

        Arguments:
          meeting_id — the meeting id from `list_meetings`.
        """
        return json.dumps(_appdata.read_meeting(meeting_id=meeting_id) or {}, ensure_ascii=False)

    @server.tool()
    def read_feedback(limit: int = 50) -> str:
        """Recent context-feedback verdicts (accept / dismiss / ignore / completed / manual_baseline)
        the app logged for proactively-surfaced todos — the signal of what the user found useful.

        Arguments:
          limit — max records (newest first), clamped to [1, 500]. Default 50.
        """
        return json.dumps(_appdata.read_feedback(limit=limit), ensure_ascii=False)

    # ─── Agent-Native Persome: memory write-back (the loop, Phase 3) ──────────────────────
    from . import memory_write as _memory_write

    @server.tool()
    def remember(content: str, tags: str = "", run_id: str = "") -> str:
        """Write a durable finding back into Persome memory so the NEXT agent / the recognizer /
        the supervisor can reuse it — the Agent-Native feedback loop. Call this when you learn
        something durable about the user, their project, a tool, or a decision while running.

        Your entry is force-tagged `source:agent-run` (so it stays distinguishable from the
        user's own notes). Pass `run_id` = the value of your `$PERSOME_TASK_ID` env var so the
        finding is attributed to this run.

        Arguments:
          content — the finding to remember (a self-contained sentence or two).
          tags    — optional comma-separated extra tags (e.g. "project-x,decision").
          run_id  — optional; your `$PERSOME_TASK_ID` for per-run attribution.
        """
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        with fts.cursor() as conn:
            return json.dumps(
                _memory_write.remember(conn, content=content, tags=tag_list, run_id=run_id),
                ensure_ascii=False,
            )

    # ─── Actuation: the Computer tools (gated on actuation_enabled; side-effects confirmed) ──────
    # The full benchmarked toolset (AX-first + on-device OCR fallback + keyboard/coordinate hands).
    # Plan: docs/superpowers/plans/2026-06-25-persome-actuation-layer-plan.md
    if getattr(cfg, "actuation_enabled", False):
        from ..actuation import actuator as _act
        from ..actuation import confirm as _confirm
        from ..actuation import gate as _gate
        from ..actuation import locate as _locate
        from ..actuation import skills as _skills
        from ..actuation import stage as _stage
        from ..actuation import takeover as _takeover
        from ..actuation.cursor_hud import hud as _hud

        # Takeover glow + badge on the driven window (kill-switch; spec
        # docs/superpowers/specs/2026-07-02-takeover-glow-overlay-design.md).
        _glow_enabled = bool(getattr(cfg, "actuation_glow_enabled", True))

        # Per-app skills are disclosed progressively: the full manual is appended the FIRST time focus
        # lands on an app (ui_activate/ui_open_app), so the agent doesn't pay for every guide up front.
        # The dedup set is keyed by MCP SESSION, not the daemon's lifetime: the daemon is a long-lived
        # launchd singleton (port 8773) that every concurrently-dispatched agent run shares, so a single
        # daemon-wide set would inject each app's guide only ONCE EVER — only the very first run after the
        # daemon booted would get it, and every later run (other tasks, even days apart) focusing the same
        # app would silently get nothing. That contradicts the per-run "first focus" design + ui_activate's
        # docstring promise. We scope by the streamable-HTTP `mcp-session-id` header (one per client
        # connection — i.e. per agent run), same request-context source as `_actuation_denied`.
        _skill_seen: dict[str, set[str]] = {}
        # Sentinel for requests with no session header (e.g. in-process tests / a transport that omits it):
        # they all share one bucket, preserving the "inject once" semantics within that headerless session.
        _SKILL_NO_SESSION = ""

        def _skill_session_key() -> str:
            """The MCP session id for the current request (stable per client connection / agent run), or a
            shared sentinel when there's no request context / header. Mirrors `_actuation_denied`'s source."""
            try:
                ctx = server.get_context()
                req = getattr(ctx.request_context, "request", None)
                if req is None:
                    return _SKILL_NO_SESSION
                return (req.headers.get("mcp-session-id", "") or _SKILL_NO_SESSION).strip()
            except Exception:  # noqa: BLE001 — never let header parsing break a tool
                return _SKILL_NO_SESSION

        def _skill_on_focus(app: str) -> str:
            """The app's operation manual the first time it's focused IN THIS MCP SESSION, else ''. Lets the
            agent learn an app's quirks without paying for every guide up front (progressive disclosure)."""
            g = _skills.guide_for(app)
            if g is None:
                return ""
            seen = _skill_seen.setdefault(_skill_session_key(), set())
            if g.app in seen:
                return ""
            seen.add(g.app)
            return f"\n\n—— {g.app} operation guide (follow it) ——\n{g.body}"

        def _task_id_header() -> str:
            """The dispatching run's task id (`X-Persome-Task-Id`, set per run by the app's MCP
            provisioning), or '' — codex registers globally and can't carry per-run headers."""
            try:
                ctx = server.get_context()
                req = getattr(ctx.request_context, "request", None)
                if req is None:
                    return ""
                raw = req.headers.get("x-persome-task-id", "") or req.headers.get("x-mens-task-id", "")  # Mens is the legacy name
                return raw.strip()
            except Exception:  # noqa: BLE001 — never let header parsing break a tool
                return ""

        def _glow_tool(
            app: str,
            *,
            kind: str,
            note: str = "",
            point: list[float] | None = None,
            pid: int = 0,
        ) -> None:
            """Feed the takeover glow for a `ui_*` touch on `app` (`kind`: "read"/"act").
            Best-effort visuals — never raises, never affects the tool result."""
            if not _glow_enabled:
                return
            try:
                payload = _takeover.tracker.on_tool(
                    _skill_session_key(),
                    app=app,
                    kind=kind,
                    pid=pid,
                    task_id=_task_id_header(),
                    note=note,
                    point=point,
                )
                if payload:
                    _hud.glow(payload)
            except Exception:  # noqa: BLE001 — glow is cosmetic, the act must proceed
                logger.debug("takeover glow update failed", exc_info=True)

        def _confirm_user(summary: str, app: str, verb: str) -> bool:
            # Real round-trip: block until the user approves in the Persome app (or timeout → deny).
            # The takeover glow flips to `awaiting_confirm` (orange pulse) for the duration, so the
            # user's eye is pulled to the pending approval — then back to `executing` either way
            # (approved = the act fires; denied = the run continues with another step).
            session = _skill_session_key()
            if _glow_enabled:
                try:
                    p = _takeover.tracker.confirm_begin(session, summary=summary)
                    if p:
                        _hud.glow(p)
                except Exception:  # noqa: BLE001
                    logger.debug("takeover glow confirm_begin failed", exc_info=True)
            try:
                return _confirm.request(summary, app=app, verb=verb)
            finally:
                if _glow_enabled:
                    try:
                        p = _takeover.tracker.confirm_end(session)
                        if p:
                            _hud.glow(p)
                    except Exception:  # noqa: BLE001
                        logger.debug("takeover glow confirm_end failed", exc_info=True)

        # Per-run firewall: the app marks an untrusted `.context` run (a task derived from on-screen
        # content, which can carry prompt injection) with an `X-Persome-Actuation: deny` header. We honour
        # it on EVERY ui_* call so such a run can read memory but never touch the Mac. Fail-open to
        # allowed only when there's no request context (e.g. a direct in-process test).
        _DENIED_DICT = {
            "ok": False,
            "error": "actuation_not_permitted",
            "reason": "Computer use is disabled for this run (untrusted provenance).",
        }
        _DENIED = json.dumps(_DENIED_DICT, ensure_ascii=False)

        def _actuation_denied() -> bool:
            try:
                ctx = server.get_context()
                req = getattr(ctx.request_context, "request", None)
                if req is None:
                    return False
                raw = req.headers.get("x-persome-actuation", "") or req.headers.get("x-mens-actuation", "")  # Mens is the legacy name
                return raw.strip().lower() == "deny"
            except Exception:  # noqa: BLE001 — never let header parsing break a tool
                return False

        # Dev accounts default the bbox overlay ON (so they always see what Persome can touch).
        def _show_boxes() -> bool:
            return bool(
                getattr(cfg, "actuation_show_boxes", False) or getattr(cfg.dev, "enabled", False)
            )

        def _act_via_gate(
            app: str, element_id: str, verb: str, note: str, text: str | None, app_pid: int = 0
        ) -> dict:
            # `app_pid` (when >0) targets a SPECIFIC instance — required to drive a virtual-stage
            # window when the user also has that app open (else the actuator resolves the wrong one
            # by name). Falls back to the app name when 0.
            if _actuation_denied():
                return _DENIED_DICT
            pid = app_pid or None
            snap = _act.snapshot(app=app, pid=pid)
            if not snap.get("ok"):
                return {"ok": False, "error": snap.get("error", "snapshot_failed")}
            elements = snap.get("elements", [])
            bundle = snap.get("bundle_id", "")
            el = next((e for e in elements if e.get("id") == element_id), {})
            label = el.get("label", "")
            # Takeover glow: mark the window as being operated BEFORE the (possibly confirm-gated)
            # act, so the halo is already up while the user is deciding.
            _glow_tool(app, kind="act", note=note, pid=int(snap.get("pid") or 0))
            # The persistent Persome cursor HUD owns the visuals, so the act itself stays flash-free.
            g = _gate.Gate(
                confirm=lambda summary: _confirm_user(summary, app, verb),
                perform=lambda *, verb, element_id, text: _act.act(
                    app=app,
                    pid=pid,
                    element_id=element_id,
                    verb=verb,
                    text=text,
                    show_cursor=False,
                    show_boxes=False,
                ),
            )
            result = g.run(
                verb=verb, element_id=element_id, label=label, bundle_id=bundle, text=text
            )
            # Float the Persome cursor at the action point with the step note (+ boxes for dev/opt-in).
            if result.get("ok"):
                _hud.update(result.get("point"), note, elements if _show_boxes() else None)
                # The act point pins WHICH window of the app the glow wraps (decision: the hit one).
                _glow_tool(
                    app,
                    kind="act",
                    note=note,
                    point=result.get("point"),
                    pid=int(snap.get("pid") or 0),
                )
            elif verb == "press":
                # Error guidance (tool-design best practice): AXPress landed nothing, but the element may
                # advertise OTHER actions (a stepper's AXIncrement/AXDecrement, a dropdown's AXShowMenu).
                # Name them + point at ui_perform so the agent self-corrects instead of flailing with
                # ui_click_xy / AppleScript on a control that simply doesn't take a press.
                others = [a for a in (el.get("actions") or []) if a != "AXPress"]
                if others:
                    result["hint"] = (
                        f"AXPress had no effect. This element advertises {others}. Call "
                        f"ui_perform(id=…, action=…) with one of them — e.g. AXIncrement/AXDecrement to "
                        f"step a date/number field, AXShowMenu to open a dropdown, AXPick to choose an item."
                    )
            return result

        def _freeform_via_gate(
            verb: str,
            *,
            app: str,
            note: str,
            keys: str,
            perform,
            point: list[float] | None,
            app_pid: int = 0,
        ) -> dict:
            """Gate + perform a no-element verb (key/type/clickxy). These carry no AX label, so they
            go through `classify_freeform` (submit-key / messaging-app / side-effect note → confirm).
            `app_pid` (when >0) targets a specific instance for the boxes snapshot; the caller's
            `perform` closure threads the same pid into the actuator call."""
            if _actuation_denied():
                return _DENIED_DICT
            d = _gate.classify_freeform(verb=verb, keys=keys, note=note, app=app)
            if not d.allowed:
                return {"ok": False, "error": "blocked", "reason": d.reason}
            # Takeover glow up before the (possibly gated) act — also seeds the session so a
            # confirm_begin during the gate has a session to flip to `awaiting_confirm`.
            _glow_tool(app, kind="act", note=note, pid=app_pid)
            if d.gated:
                summary = note or f"{verb} {keys or ''}".strip() or verb
                if not _confirm_user(summary, app, verb):
                    return {"ok": False, "error": "denied", "reason": "user declined"}
            result = perform()
            if result.get("ok"):
                # Freeform verbs don't snapshot for the act itself, so when the bbox overlay is on
                # (default), grab the app's elements once so the user still sees the boxes Persome is
                # working over (not just the cursor). Skipped when boxes are off → no extra snapshot.
                boxes = (
                    _act.snapshot(app=app, pid=app_pid or None).get("elements")
                    if _show_boxes()
                    else None
                )
                _hud.update(point, note, boxes)
                _glow_tool(app, kind="act", note=note, point=point, pid=app_pid)
            result["gated"] = d.gated
            return result

        async def _gated_async(make_result):
            """Run an actuation tool's body OFF the event loop (firewall-checked on it).

            The MCP SDK runs a *sync* tool body directly on the asyncio loop (it does
            `return fn(...)`, no thread offload — see func_metadata.call_fn_with_arg_validation).
            Anything blocking there freezes the WHOLE daemon: a gated verb's `confirm.request`
            (`threading.Event.wait(60s)`) is the egregious case (SSE + the approve POST can't be
            serviced → always times out → deny), but the read-only tools' actuator subprocess
            (≤10s) and the synchronous on-device OCR (seconds) block it too. So EVERY actuation tool
            (gated or read-only) offloads its body to a worker thread, keeping the loop responsive.
            The X-Persome-Actuation firewall check runs HERE, on the loop, because it reads the
            per-request context; the offloaded body is reached only when allowed."""
            if _actuation_denied():
                return _DENIED
            return await asyncio.to_thread(make_result)

        # ── Eyes: read the UI ──────────────────────────────────────────────────
        @server.tool()
        async def ui_snapshot(app: str, app_pid: int = 0) -> str:
            """Read the addressable UI elements of `app` (name or bundle id) — the control graph the
            other ui_* tools act on. Each element has a stable `id`, role, label, value, bbox, actions.
            Prefer `ui_find` when you already know what you're looking for (cheaper to reason over).

            `app_pid`: when you're driving an instance opened by `ui_open_app` (a virtual stage), pass
            its returned `app_pid` so this reads THAT instance, not the user's own copy of the app."""

            def _body() -> str:
                snap = _act.snapshot(app=app, pid=app_pid or None)
                _glow_tool(app, kind="read", pid=int(snap.get("pid") or 0))
                return json.dumps(snap, ensure_ascii=False)

            return await _gated_async(_body)

        @server.tool()
        async def ui_find(app: str, query: str) -> str:
            """Find AX elements of `app` whose label/value contains `query`. Returns each match's
            `id` (for ui_click/ui_set_value), role, text, a `container` letter (same letter = same
            subtree, to disambiguate duplicate labels), visibility, and bbox. The fast way to locate
            a control without reading the whole snapshot."""

            def _body() -> str:
                res = _locate.ax_find(app, query)
                _glow_tool(app, kind="read")
                return json.dumps(res, ensure_ascii=False)

            return await _gated_async(_body)

        @server.tool()
        async def ui_ocr_locate(app: str, query: str) -> str:
            """For PIXEL-drawn text the AX tree can't see (WeChat/Feishu chat rows, a calculator
            display): OCR the front window of `app` and return ALL matches of `query` as SCREEN
            coordinates (top→bottom), each `{x, y, text}` — feed an (x, y) straight to `ui_click_xy`.
            Use ONLY when `ui_find` comes back empty for an AX-poor app."""

            def _body() -> str:
                res = _locate.ocr_locate(app, query)
                _glow_tool(app, kind="read")
                return json.dumps(res, ensure_ascii=False)

            return await _gated_async(_body)

        # ── Hands: operate the UI ──────────────────────────────────────────────
        @server.tool()
        async def ui_activate(app: str) -> str:
            """Bring `app` (name or bundle id) to the front before operating it. Always do this first
            when you switch which app you're driving — the keyboard verbs act on the focused app. The
            FIRST time you focus an app that has an operation guide, that guide is appended here — read
            it, it tells you the app's quirks (AX-rich vs OCR-only vs blind, input idioms, traps)."""

            def _body() -> str:
                res = _act.activate(app)
                _glow_tool(app, kind="read")
                return json.dumps(res, ensure_ascii=False) + _skill_on_focus(app)

            return await _gated_async(_body)

        @server.tool()
        def ui_app_guide(app: str) -> str:
            """Get the operation guide for `app` (name or bundle id) — how to drive it well (AX-first vs
            OCR vs keyboard-only, input idioms, send/navigation traps). Call this when you start working
            with an app and want its manual on demand; ui_activate also injects it on first focus. Returns
            a `{available, apps:[…]}` menu of every app with a guide when there's no guide for `app`."""
            if _actuation_denied():
                return _DENIED
            g = _skills.guide_for(app)
            if g is None:
                return json.dumps(
                    {"available": False, "app": app, "apps": _skills.list_skills()},
                    ensure_ascii=False,
                )
            return json.dumps(
                {"available": True, "app": g.app, "summary": g.summary, "guide": g.body},
                ensure_ascii=False,
            )

        @server.tool()
        async def ui_click(app: str, id: str, note: str, app_pid: int = 0) -> str:
            """Click (AXPress) the element `id` in `app` (the `id` from ui_find/ui_snapshot). Returns
            the before/after AX diff as proof it landed. Side-effect-labelled targets (Send/Delete/…)
            require user confirmation.

            NOT for controls that ignore AXPress: a stepper / date-time / number picker / dropdown
            advertises its own actions (AXIncrement / AXDecrement / AXShowMenu / AXPick — see the
            element's `actions` in the snapshot) — drive those with `ui_perform`, not this. To enter a
            value use `ui_set_value` (AX field) or `ui_type` (pixel/Electron field). If this returns a
            failure with a `hint`, follow the hint.

            `note`: REQUIRED. A SHORT, user-facing description of what THIS step accomplishes (the
            user sees it in the Persome cursor bubble), e.g. "正在给 xxx 发送消息" / "Opening the compose
            window". Keep it brief and about the goal, not the mechanics.
            `app_pid`: pass ui_open_app's returned app_pid to target a virtual-stage instance."""
            return await _gated_async(
                lambda: json.dumps(
                    _act_via_gate(app, id, "press", note, None, app_pid), ensure_ascii=False
                )
            )

        @server.tool()
        async def ui_perform(app: str, id: str, action: str, note: str, app_pid: int = 0) -> str:
            """Perform a NAMED AX action on element `id` — the way to drive controls that do NOTHING on
            a plain `ui_click` (AXPress). Steppers, date/time/number pickers, dropdowns, disclosure
            triangles and segmented controls expose their OWN actions (AXIncrement / AXDecrement /
            AXShowMenu / AXPick / AXConfirm / AXRaise …) instead of, or on top of, AXPress.

            WHEN TO USE: `ui_snapshot`/`ui_find` list each element's available `actions`. If a control
            ignores `ui_click` / `ui_click_xy` AND its `actions` contain a non-AXPress action, call this
            with that action — e.g. bump a date/time stepper with repeated AXIncrement / AXDecrement
            (call once per step), open a custom dropdown with AXShowMenu, choose an item with AXPick.
            This is the fix for the classic form-filling trap where a date/number field won't take a
            click and AXPress has no effect.
            WHEN NOT TO USE: a normal button or link → `ui_click`. Typing text or setting a field's value
            → `ui_set_value` (AX-addressable field) or `ui_type` (pixel/Electron field). A keyboard
            shortcut / Return-to-submit → `ui_key`.

            `action`: REQUIRED. The EXACT AX action name taken from the element's `actions` list (e.g.
                "AXIncrement", "AXDecrement", "AXShowMenu", "AXPick", "AXConfirm"). It must be one the
                element actually advertises, or the OS rejects it (`action_failed`).
            `note`: REQUIRED short, goal-level description shown in the Persome cursor bubble (e.g.
                "把日期调到 7 月 1 日"). Keep it about the goal, not the mechanics.
            `app_pid`: pass ui_open_app's returned app_pid to target a virtual-stage instance.

            Returns the before/after AX diff as proof it landed; a side-effect-labelled target is
            confirmed first, same as `ui_click`."""
            return await _gated_async(
                lambda: json.dumps(
                    _act_via_gate(app, id, "action", note, action, app_pid), ensure_ascii=False
                )
            )

        @server.tool()
        async def ui_click_xy(app: str, x: float, y: float, note: str, app_pid: int = 0) -> str:
            """Click at SCREEN coordinate (x, y) — the fallback for a pixel-drawn control that
            `ui_ocr_locate` found and AX can't reach. Prefer `ui_click` (by id) whenever a snapshot
            exposes the element; coordinates can't be semantically checked, so this is gated harder
            in messaging/mail apps.

            `note`: REQUIRED short, goal-level description (shown in the Persome cursor bubble).
            `app_pid`: REQUIRED when clicking a virtual-stage window — pass ui_open_app's returned
            app_pid so the click reaches that instance, not the user's own copy of the app."""

            def _do() -> str:
                return json.dumps(
                    _freeform_via_gate(
                        "clickxy",
                        app=app,
                        note=note,
                        keys="",
                        perform=lambda: _act.clickxy(
                            x,
                            y,
                            app=app,
                            pid=app_pid or None,
                            note=note,
                            show_cursor=False,
                            show_boxes=False,
                            background=True,
                        ),
                        point=[x, y],
                        app_pid=app_pid,
                    ),
                    ensure_ascii=False,
                )

            return await _gated_async(_do)

        @server.tool()
        async def ui_set_value(app: str, id: str, text: str, note: str, app_pid: int = 0) -> str:
            """Set the value of editable element `id` in `app` to `text` (a gated side-effect). Best
            for AX-addressable fields (address bars, document text areas). For a pixel/Electron search
            box AX can't address, use `ui_type` instead.

            `note`: REQUIRED short description of what this step is for (shown in the Persome cursor
            bubble), e.g. "正在输入收件人". Keep it brief.
            `app_pid`: pass ui_open_app's returned app_pid to target a virtual-stage instance."""
            return await _gated_async(
                lambda: json.dumps(
                    _act_via_gate(app, id, "setvalue", note, text, app_pid), ensure_ascii=False
                )
            )

        @server.tool()
        async def ui_type(app: str, text: str, note: str, app_pid: int = 0) -> str:
            """Type Unicode `text` (incl. Chinese) into the CURRENTLY FOCUSED field of `app` — for a
            pixel/Electron search box `ui_set_value` can't address (WeChat search, etc.). Click/focus
            the field first. Does NOT press Return (use `ui_key` for that). Gated in messaging apps.

            `note`: REQUIRED short, goal-level description (shown in the Persome cursor bubble).
            `app_pid`: pass ui_open_app's returned app_pid to target a virtual-stage instance."""

            def _do() -> str:
                return json.dumps(
                    _freeform_via_gate(
                        "type",
                        app=app,
                        note=note,
                        keys="",
                        perform=lambda: _act.type_text(
                            text,
                            app=app,
                            pid=app_pid or None,
                            note=note,
                            show_cursor=False,
                            show_boxes=False,
                            background=True,
                        ),
                        point=None,
                        app_pid=app_pid,
                    ),
                    ensure_ascii=False,
                )

            return await _gated_async(_do)

        @server.tool()
        async def ui_key(app: str, keys: str, note: str, app_pid: int = 0) -> str:
            """Post a key combo to the focused app: a single key (`enter`, `tab`, `escape`), a chord
            (`cmd+a`, `cmd+shift+p`, `shift+=`), or a menu shortcut. Use for shortcuts, menu keys,
            Return-to-submit, and char-by-char text entry that `ui_set_value` can't do. An
            enter/return in a messaging app is treated as Send and confirmed first.

            `note`: REQUIRED short, goal-level description (shown in the Persome cursor bubble).
            `app_pid`: pass ui_open_app's returned app_pid to target a virtual-stage instance."""

            def _do() -> str:
                return json.dumps(
                    _freeform_via_gate(
                        "key",
                        app=app,
                        note=note,
                        keys=keys,
                        perform=lambda: _act.key(
                            keys,
                            app=app,
                            pid=app_pid or None,
                            note=note,
                            show_cursor=False,
                            show_boxes=False,
                            background=True,
                        ),
                        point=None,
                        app_pid=app_pid,
                    ),
                    ensure_ascii=False,
                )

            return await _gated_async(_do)

        # ── No-steal staging: open an app WITHOUT touching the user's screen ────
        @server.tool()
        async def ui_open_app(app: str, url: str, note: str) -> str:
            """Open `app` showing `url` to work in, the NO-STEAL way — call this FIRST for a web task
            instead of `ui_activate`. Persome picks the path automatically:

            - A multi-instance app (a browser) → Persome spawns its OWN fresh instance on an off-screen
              virtual display and returns `{strategy:"virtual_stage", app_pid, window_id, window_bounds, ...}`.
              IMPORTANT: drive it by passing that returned `app_pid` to every subsequent verb
              (ui_snapshot/ui_find/ui_click/ui_click_xy/ui_type/ui_key) — the app NAME alone is
              ambiguous when the user also has the app open, so without app_pid the actuator may hit the
              user's copy. Click coordinates inside `window_bounds` ([x,y,w,h] on the virtual display).
              The user's real screen never changes. Call ui_close_app(app_pid) when done.
            - A single-instance app the user owns (Feishu/WeChat) → Persome asks the user to lend it; on
              consent returns `{strategy:"borrow", consented:true}` and you operate it in place (by app
              name, no app_pid); on refusal returns denied. NEVER operate a single-instance app without
              this consent.

            `note`: REQUIRED short, goal-level description (shown to the user), e.g. "打开 Google Meet"."""
            # Firewall check on the loop; the body (which blocks in the borrow confirm) runs in a
            # worker thread so the up-to-60s wait never freezes the daemon's event loop.
            if _actuation_denied():
                return _DENIED

            def _do() -> str:
                res = _stage.open_app(app, url)
                if res.get("strategy") == "borrow":
                    # Pass the agent's goal note as-is; the app frames the borrow ask around app+verb.
                    if not _confirm_user(note, app, "borrow"):
                        return json.dumps(
                            {
                                "ok": False,
                                "error": "denied",
                                "reason": "user declined to lend the app",
                            },
                            ensure_ascii=False,
                        )
                    res["consented"] = True
                # First-focus skill disclosure (same as ui_activate) — append the app's guide once.
                guide = _skill_on_focus(app)
                if guide:
                    res["guide"] = guide
                return json.dumps(res, ensure_ascii=False)

            return await asyncio.to_thread(_do)

        @server.tool()
        def ui_close_app(app_pid: int) -> str:
            """Close a virtual-display staged instance opened by `ui_open_app` (pass its `app_pid`),
            releasing the off-screen display. Call when you're done with a staged app. Borrowed
            single-instance apps don't need this (you never owned them)."""
            if _actuation_denied():
                return _DENIED
            closed = _stage.registry.close(int(app_pid))
            return json.dumps({"ok": True, "closed": closed}, ensure_ascii=False)

    from . import view_capture as view_capture_mod

    view_capture_mod.register(server, cfg)

    from ..api import register_routes

    register_routes(server, cfg)
    return server


def run_stdio() -> None:
    """Run the server on stdio. Blocks until the client disconnects."""
    server = build_server()
    server.run()  # FastMCP.run() uses stdio by default


async def run_async(cfg: Config | None = None, *, transport: str | None = None) -> None:
    """Run the MCP server with the configured transport (for use inside the daemon)."""
    cfg = cfg or load_config()
    transport = transport or cfg.mcp.transport
    server = build_server(cfg)
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
        return f"http://{cfg.mcp.host}:{cfg.mcp.port}/sse"
    if transport == "streamable-http":
        return f"http://{cfg.mcp.host}:{cfg.mcp.port}/mcp"
    raise ValueError(f"endpoint_url only supported for sse/http, got {transport!r}")

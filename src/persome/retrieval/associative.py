"""Multi-slot Q distillation — the §3.2 associative read's Q construction.

Memory-rebuild spec §3.2: the read path has ONE entrance — the present is the
only questioner. Whatever the present offers (a task prompt, recent activity, an
MCP question, screen text) is distilled ZERO-LLM into a multi-slot Q; head
weights then emerge from slot occupancy inside the RRF (an absent slot is zero
votes — no mode switch). This module is that distillation; the engine half is
``store.fts.search_associative`` (the §5 single read choke point).

Slot ↔ head ↔ cost (§3.3):

- **Who (entities)** — ``identity.scan_mentions`` against the roster; hash/
  substring, free. The funnel module owns it (weights arm perception).
- **Where (scene_terms)** — known scene names (apps/channels) matched in the
  text; free. v1 keys on a builtin alias table + caller-provided extras (in
  production the apps seen in ``timeline_blocks.apps_used`` extend it); the
  pool matches entry CONTENT mentions — the reducer preserves ``[App]``
  markers verbatim, so scene lives in the text today. A dedicated scene
  column/index is the later optimization, not a prerequisite.
- **When (since/until)** — a small closed set of deterministic temporal
  expressions (localized month/day forms, YYYY-MM-DD, and relative day words
  anchored at ``now``); free. Deliberately narrow: personal-memory queries
  mostly need day-window anchoring, and an unparsed expression simply leaves the
  slot empty (fail-open to the other heads), never guesses.
- **What (text)** — carried verbatim; the lexical (BM25) and semantic (dense)
  heads read it downstream.

The semantic slot's embedding cadence is the caller's
concern — this module never talks to the network.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ..evomem import identity as identity_mod
from ..logger import get

# Builtin scene aliases: display/canonical scene -> the surface forms a query
# may use. Extended at call time by the apps production has actually seen.
SCENE_ALIASES: dict[str, tuple[str, ...]] = {
    "Feishu": ("\u98de\u4e66", "feishu", "lark"),
    "WeChat": ("\u5fae\u4fe1", "wechat"),
    "DingTalk": ("\u9489\u9489", "dingtalk"),
    "Slack": ("slack",),
    "Mail": ("\u90ae\u4ef6", "\u90ae\u7bb1", "mail", "outlook", "gmail"),
    "Safari": ("safari",),
    "Chrome": ("chrome", "\u6d4f\u89c8\u5668"),
    "Cursor": ("cursor",),
    "Xcode": ("xcode",),
    "Terminal": ("\u7ec8\u7aef", "terminal", "iterm"),
    "Notion": ("notion",),
    "Meetings": ("\u4f1a\u4e0a", "\u4f1a\u8bae", "\u4f1a\u8bae\u91cc", "\u5f00\u4f1a"),
}

_ABS_DATE_RE = re.compile(r"(?:(\d{4})[-\u5e74/])?(\d{1,2})[-\u6708/](\d{1,2})[\u65e5\u53f7]?")
_RELATIVE_DAYS = {"\u4eca\u5929": 0, "\u6628\u5929": 1, "\u524d\u5929": 2}


@dataclass
class MultiSlotQ:
    """The distilled present — what the heads read (§3.2)."""

    text: str
    entities: list[str] = field(default_factory=list)  # Who: roster canonicals
    scene_terms: list[str] = field(default_factory=list)  # Where: matched surface forms
    since: str | None = None  # When: ISO day-window bounds
    until: str | None = None


def distill_time(text: str, *, now: datetime) -> tuple[str | None, str | None]:
    """Resolve ONE past-day window from the text, or (None, None).

    The closed set covers absolute dates and localized relative-day terms,
    anchored at ``now``. An
    absolute date without a year takes ``now``'s year, rolling back one year
    if that would land in the future (queries here ask about the past).
    """
    m = _ABS_DATE_RE.search(text)
    day: datetime | None = None
    if m:
        year = int(m.group(1)) if m.group(1) else now.year
        try:
            day = now.replace(year=year, month=int(m.group(2)), day=int(m.group(3)))
        except ValueError:
            day = None
        if day is not None and m.group(1) is None and day.date() > now.date():
            day = day.replace(year=year - 1)
    if day is None:
        for word, delta in _RELATIVE_DAYS.items():
            if word in text:
                day = now - timedelta(days=delta)
                break
    if day is None:
        return None, None
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    return start.isoformat(), end.isoformat()


def distill_scenes(text: str, *, extra_scenes: list[str] | None = None) -> list[str]:
    """Where slot: surface forms of known scenes mentioned in the text."""
    hay = identity_mod.norm(text)
    hits: list[tuple[int, str]] = []
    seen: set[str] = set()

    def probe(surface: str) -> None:
        key = identity_mod.norm(surface)
        if not key or surface in seen:
            return
        pos = hay.find(key)
        if pos >= 0:
            hits.append((pos, surface))
            seen.add(surface)

    for _canonical, surfaces in SCENE_ALIASES.items():
        for surface in surfaces:
            probe(surface)
    for surface in extra_scenes or []:
        probe(surface)
    return [s for _pos, s in sorted(hits)]


def distill_q(
    text: str,
    roster: identity_mod.Roster,
    *,
    now: datetime,
    extra_scenes: list[str] | None = None,
) -> MultiSlotQ:
    """§3.2: distill the present into the multi-slot Q — zero LLM, never raises."""
    since, until = distill_time(text, now=now)
    return MultiSlotQ(
        text=text,
        entities=identity_mod.scan_mentions(text, roster),
        scene_terms=distill_scenes(text, extra_scenes=extra_scenes),
        since=since,
        until=until,
    )


logger = get("persome.retrieval")


def associative_read(
    conn: sqlite3.Connection,
    *,
    query: str,
    top_k: int = 5,
    since: str | None = None,
    until: str | None = None,
    path_patterns: list[str] | None = None,
    embedder: Any | None = None,
    now: datetime | None = None,
    with_chains: bool = False,
    chain_budget_chars: int = 2000,
    entities: list[str] | None = None,
    mmr_diversity: float = 0.0,
) -> Any:
    """The PRODUCTION read entrance (§3.2/§5 cutover — the single choke point
    every query-time consumer hangs on: MCP search, the chat memory tool, the
    writer tool-loop).

    Distills the caller's query into the multi-slot Q (zero LLM) and routes
    through ``fts.search_associative`` — which itself degrades to
    ``search_hybrid`` when every slot is empty, so a slot-less query is
    byte-identical to the pre-cutover read. Caller-explicit ``since``/``until``
    bounds take precedence over the distilled window (an API caller who set
    bounds meant them). Kill-switch: ``[search] associative_read_enabled``
    (default ON — the 2026-07-03 production sweep verdict: exact parity at the
    0.3 pool weights, slot-golden all 1.0); flipping it off restores
    ``search_hybrid`` verbatim at every switched call site.

    ``with_chains``: also pull the §3.4 tree-chain delivery over the hit set
    (anchor mentions → chains to USER → merged narrative + receipt pointers;
    walked edges get their read reinforced). Returns ``(hits, chains_text)``
    when set, else just ``hits``.

    E1 (MCP full-power entrance spec): ``entities`` lets a caller who KNOWS who
    it is asking about arm the Who head directly — each mention goes through the
    SAME ``resolve_identity`` funnel as the distilled Q (§4.3: one codebook,
    forks drift), unresolved mentions are silently dropped (the explicit slot is
    an enhancement, never a filter), and resolved canonicals merge ahead of the
    distilled entities. ``mmr_diversity`` is the §3.4-3 consumer breadth knob
    (0 = byte-identical accuracy-first default), honored on the degrade path too.
    """
    from .. import config as config_mod
    from .. import paths as paths_mod
    from ..store import fts

    cfg = config_mod.load(paths_mod.config_file())
    if not getattr(cfg.search, "associative_read_enabled", True):
        hits = fts.search_hybrid(
            conn,
            query=query,
            path_patterns=path_patterns,
            since=since,
            until=until,
            top_k=top_k,
            embedder=embedder,
            mmr_diversity=mmr_diversity,
        )
        return (hits, "") if with_chains else hits

    roster = identity_mod.load_roster(cfg)
    q = distill_q(query, roster, now=now or datetime.now(UTC))
    eff_since = since if (since or until) else q.since
    eff_until = until if (since or until) else q.until
    explicit: list[str] = []
    for mention in entities or []:
        res = identity_mod.resolve_identity(mention, roster)
        if res.matched and res.canonical not in explicit:
            assert res.canonical is not None
            explicit.append(res.canonical)
    eff_entities = explicit + [e for e in q.entities if e not in explicit]
    hits = fts.search_associative(
        conn,
        query=query,
        entities=eff_entities,
        scene_terms=list(q.scene_terms),
        since=eff_since,
        until=eff_until,
        path_patterns=path_patterns,
        top_k=top_k,
        embedder=embedder,
        mmr_diversity=mmr_diversity,
    )
    if not with_chains:
        return hits
    chains_text = ""
    try:
        from . import chains as chains_mod

        delivery = chains_mod.pull_chains(conn, hits, roster, as_of=eff_until)
        if delivery.lines or delivery.orphan_anchors:
            chains_text = chains_mod.render_delivery(delivery, budget_chars=chain_budget_chars)
    except Exception:  # noqa: BLE001 — chains decorate the hits, never break the read
        logger.exception("chain delivery failed — returning bare hits")
    return hits, chains_text

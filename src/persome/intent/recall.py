"""The single background-assembly contract for online recognition.

Every online consumer used to assemble its memory background by hand-rolling a
flat FTS query (the meeting analyzer's ``_prefetch_memory``, the active layer's
preference load, chat's tool calls). ``assemble_background`` is the one contract
they converge on: "given the current scene + some hint terms, return the relevant
memory bundle."

Phase 4 upgrades the *implementation* behind that seam from a flat keyword BM25
dump into a **layered background pack**, assembled in priority order so the most
decision-relevant context comes first within a fixed character budget:

    ① 场景意图   — recent intents recognized in THIS scope (the unified stream)
    ② 行为先验   — skill/workflow memory (what dream/pattern learned the user does)
    ③ 相关记忆   — durable entity/topic facts (person/org/project/topic/user/tool)
    ④ 其他命中   — any remaining keyword hits, so nothing relevant is dropped

This is structural/scene-aware recall, not vector search — it stays deterministic
and dependency-free (no embedding model), while giving every pack scene context
and behavioural priors that flat BM25 never surfaced. Swapping in semantic recall
later is again a change behind this one seam.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..logger import get
from ..store import fts as fts_store
from ..store import recall_budget_ticks
from . import store as intent_store

logger = get("persome.intent.recall")


@dataclass
class RecallItem:
    """One structured, cited recall hit — the mirror of a string line in
    :func:`assemble_background`, emitted by :func:`assemble_background_structured`.

    ``content`` is the clean snippet (no ``[path]`` prefix); ``cite`` is a stable
    provenance handle (``mem:<path>`` / ``schema:<file>`` / ``intent:<id>`` /
    ``block:<id>``). ``capture_stem`` / ``timeline_block_id`` are RAW-capture
    handles for scene/timeline items (the stem keys ONE buffer JSON holding both
    the screenshot and the axtree — we emit only the handle string, never bytes).
    """

    layer: str  # schema | behavior | fact | semantic | scene_intent | event | timeline
    content: str
    cite: str
    score: float | None = None
    confidence: str | None = None
    conflicted: bool = False
    capture_stem: str | None = None
    timeline_block_id: int | None = None


def _response_layer(telemetry_layer: str, path: str) -> str:
    """Map a daemon-internal telemetry layer + entry path to a response layer label.

    The keyword/behavior/fact layers all flow through ``_admit_rows`` with a
    telemetry layer string; the user-facing label is derived from the entry's
    ``path`` prefix (more accurate than the telemetry bucket, which lumps the
    fallback layer under ``keyword``). The semantic layer is path-agnostic.
    """
    if telemetry_layer == "semantic":
        return "semantic"
    p = path or ""
    if p.startswith(_BEHAVIOR_PREFIXES):
        return "behavior"
    if p.startswith("event-"):
        return "event"
    if p.startswith("schema-"):
        return "schema"
    return "fact"


def _seen_key(path: str, content: str) -> str:
    """Dedup key for one FTS hit within an assemble_background call.

    Uses the FULL content, not a 40-char prefix (#631 nit DD): several entries in
    one file that share a long boilerplate/template prefix ("用户在项目里反复…")
    used to collapse to a single key, so all-but-one were silently dropped before
    the per-hint cap ever applied. Keys live only for one call, so the full string
    is cheap and collision-free."""
    return f"{path}\x00{content}"


# Memory-file prefixes grouped by recall layer. ``intent-`` is a durable fact
# (the FTS projection of the unified stream); skill/workflow are behavioural.
_BEHAVIOR_PREFIXES = ("skill-", "workflow-")
_FACT_PREFIXES = ("user-", "project-", "person-", "org-", "topic-", "tool-", "intent-")

# Minimum cosine for the semantic layer to surface a dense hit. The layer is unfused
# (no BM25 anchor), so without a floor a scene that relates to nothing would still pull
# the top-k least-distant — sim≈0 noise. te3-large vectors are normalized; 0.2 keeps
# genuinely-related memory and drops near-orthogonal. Tunable on the recall golden.
_SEMANTIC_MIN_SIM = 0.2
# Cap the dense-recall query to its most RECENT chars before embedding: the slow path
# passes the whole session trajectory (tens of thousands of chars), which exceeds te3-large's
# token limit and 400s. Tail = current activity; well under embeddings_client `_MAX_CHARS`.
_QUERY_TAIL_CHARS = 4000
# Durable-memory path globs the semantic layer ranks over (everything EXCEPT event-*/captures
# raw logs — which dominate the vector corpus and would crowd out the facts this layer exists
# to surface). live_matrix GLOBs are positive-only (no NOT-glob), hence the explicit list.
_DURABLE_GLOBS = [
    "user-*",
    "project-*",
    "topic-*",
    "person-*",
    "tool-*",
    "intent-*",
    "thread-*",
    "schema-*",
]

# evomem SSOT switch (design §1.4) — THE chain-head fold. entry_chain（markdown
# 投影时代的派生链索引）已在 PR-7 退役：折叠的唯一链数据源是 evo_nodes。
#
# Q4 裁定: the fold (like backfill / the inverted write口) uses the default scope.
_EVO_SCOPE = ("default", "default")

# Fold to evo_nodes active chain heads (§1.4 折叠改读 evo_nodes).
# event-* entries are Q2-exempt (never in evo_nodes, never on a chain), so they
# keep the ``superseded = 0`` column judgment — a keyword-layer hit on an event-
# entry must NOT be lost by the switch (pinned by test_recall_evo_read).
_EVO_FOLD_SQL = (
    " AND (id IN (SELECT node_id FROM evo_nodes"
    " WHERE user_id = ? AND agent_id = ? AND is_latest = 1 AND status = 'active')"
    " OR (path LIKE 'event-%' AND superseded = 0))"
)

# 冷启动退化折叠（evo_nodes 缺表/为空 = backfill/反转写从未跑过）：直接用 FTS
# 检索投影的 superseded 派生列。P1 不变量 + PR-4 双读对账（连续 0 diff 后退役）
# 证明了 {is_latest=1 AND active} ≡ {superseded=0}（排除 event-），所以这条
# 退化路径与 evo 折叠输出等价——新装机器在跑过一次 `evomem-backfill` 之前
# recall 不会变空，链轨迹（trail）则要等 evo_nodes 就绪后才渲染。
_COLUMN_FOLD_SQL = " AND superseded = 0"


def _evo_fold_ready(conn: sqlite3.Connection) -> bool:
    """冷启动守卫：evo_nodes 表存在且 default scope 非空。

    未就绪 → 折叠退化到 :data:`_COLUMN_FOLD_SQL`（等价折叠，见上），trail 跳过。
    """
    try:
        row = conn.execute(
            "SELECT 1 FROM evo_nodes WHERE user_id = ? AND agent_id = ? LIMIT 1",
            _EVO_SCOPE,
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


def _fts5_match_expr(hint: str) -> str:
    """Render a raw hint as a safe FTS5 MATCH expression.

    FTS5 treats quotes/colons/parens/apostrophes as query syntax — a hint like
    ``User's`` raised ``fts5: syntax error near "'"`` and was silently skipped
    by the OperationalError guard, degrading recall (生产实测 2026-06-10).
    Tokenize on whitespace and wrap every token as a double-quoted FTS5 string
    (internal double quotes doubled): inside a quoted string every character is
    literal, and the tokenizer still splits the phrase into its words, so a
    plain hint matches byte-identically to the bareword form and a multi-word
    hint keeps its implicit-AND semantics.
    """
    return " ".join('"' + t.replace('"', '""') + '"' for t in hint.split())


class _Budget:
    """Shared character budget so all layers together stay within ``max_chars``.

    Besides the admission decision itself, the budget keeps a **telemetry
    side-channel**: per-layer counters of how many texts (and chars) were
    admitted vs rejected. The counters are pure observation — they never feed
    back into the admission decision, so the assembled output stays
    byte-identical to the counter-less budget (P0 invariant). They exist to
    answer the ablation report's open question ("how often does the budget
    actually squeeze a layer out in production?") via ``recall_budget_ticks``.
    """

    def __init__(self, max_chars: int) -> None:
        self.max = max_chars
        self.used = 0
        self.layers: dict[str, dict[str, int]] = {
            layer: {c: 0 for c in recall_budget_ticks.COUNTERS}
            for layer in recall_budget_ticks.LAYERS
        }

    def full(self) -> bool:
        return self.used >= self.max

    def add(self, text: str, *, layer: str = "") -> bool:
        ok = self.used + len(text) <= self.max
        if ok:
            self.used += len(text)
        bucket = self.layers.get(layer)
        if bucket is not None:
            key = "admitted" if ok else "rejected"
            bucket[key] += 1
            bucket[key + "_chars"] += len(text)
        return ok

    @property
    def squeezed(self) -> bool:
        """True when any layer had at least one text rejected for lack of budget."""
        return any(b["rejected"] for b in self.layers.values())


def assemble_background(
    conn: sqlite3.Connection,
    *,
    scope: str,
    hints: list[str],
    per_hint: int = 2,
    max_chars: int = 1200,
    include_events: bool = False,
    schema_prior: list[str] | None = None,
    fold_superseded: bool = False,
    chain_trail: bool = False,
    include_confidence: bool = False,
    recent_events_hours: int = 0,
    workthread_chars: int = 0,
    workthread_min_confidence: float = 0.6,
    dense_query: str | None = None,
    dense_top_k: int = 0,
) -> str:
    """Return a compact, layered memory background for the current scene/hints.

    Layers are assembled in priority order (schema-inference prior → scene intents
    → behavioural priors → durable facts → keyword fallback) and share one
    ``max_chars`` budget, so the most decision-relevant context wins the limited
    space. ``scope`` drives the scene layer; ``hints`` (keyword terms) drive the
    rest, excluding raw ``event-*`` activity unless ``include_events`` is set.

    ``schema_prior`` (D2 seam): inferred user-inertia lines injected as the *first*
    (highest-priority) section. ``None``/empty → output byte-identical to before.

    ``fold_superseded`` — THE chain-head fold (evomem SSOT switch §1.4): when
    True, the keyword layers fold each hit to its evolution-chain head, read
    from the SSOT — ``evo_nodes WHERE is_latest=1 AND status='active'``
    (scope=default per Q4). event-* entries (Q2: exempt from evo_nodes) keep the
    ``superseded=0`` column judgment so keyword hits on them survive. While
    evo_nodes is missing/empty (a fresh install before its first
    ``evomem-backfill``), the fold degrades to the ``superseded=0`` derived
    column — proven equivalent by the P1 invariant + the (since retired, PR-7)
    dual-read reconciliation. Default False → legacy un-folded output,
    byte-identical to the pre-fold era. The entry_chain derived index and its
    ``use_chain_index``/``read_evo_nodes`` flags were retired in PR-7: evo_nodes
    is the only chain store.

    ``chain_trail`` (演化链轨迹 — its OWN flag): when True ON TOP of the fold, a
    chain head with superseded ancestors is annotated inline with a compact
    ``← [曾] …`` / ``← [精炼自] …`` trajectory rendered from the evo_nodes
    bidirectional pointers (whole-chain traversal via
    :func:`evomem.chain.expand_evolution_chains`), so the recognizer sees
    态度演变, not just the latest belief. Renders only when BOTH flags are on
    AND evo_nodes is ready; the fold alone stays a pure, equivalence-preserving
    fold. Default False → byte-identical to today.

    ``include_confidence`` (meta-cognition layer, Hy-Memory migration): when True,
    a hit's snippet is annotated with a compact reliability note read from the
    ``entry_metadata`` index — ``⚠(低置信)`` for a ``confidence:low`` memory and
    ``⚠(冲突未裁决)`` for a conflicted one. high/medium are left unmarked (the
    note exists to *down-weight* shaky memories, not to label every fact). Default
    False → byte-identical to today, no ``entry_metadata`` reference.

    ``recent_events_hours`` (WorkThread S0 基线, spec 2026-06-12 §二): when > 0,
    append a lowest-priority "近期活动" section carrying the most recent
    event-daily entries within the window — the reducer's session summaries with
    their "continued…" narration, i.e. the descriptive "最近在干什么" perception.
    Shares the main budget LAST (it must never squeeze facts out; the squeeze
    telemetry below is the S0 acceptance gauge). 0 → byte-identical to today.

    ``workthread_chars`` (WorkThread S3 工作线层, spec §六-1): when > 0, inject
    the active work thread (+ at most one background thread) as its own section
    positioned right AFTER schema_prior (whose top priority has ablation
    backing) and BEFORE the scene layer — with an **independent** budget of
    this many chars, so it can never squeeze the existing layers (callers raise
    their total accordingly, e.g. 1200 → 1400). Threads below
    ``workthread_min_confidence`` are not injected. 0 → byte-identical.

    Telemetry (ablation 2026-06-10 §4): every call records one best-effort row in
    ``recall_budget_ticks`` — scope, max_chars, used, per-layer admitted/rejected
    counts+chars, ``squeezed`` (any rejection), and the hint terms. Pure
    side-channel: the counters never influence the admission decisions, the
    output stays byte-identical, and a failed write degrades to a warning
    without touching recall.
    """
    # 冷启动守卫（一次/调用）：evo_nodes 未就绪 → 折叠退化到 superseded 派生列
    # （等价折叠），trail 跳过（无链数据可渲染）。
    evo_ready = _evo_fold_ready(conn) if (fold_superseded or chain_trail) else False
    # The trail is its own flag, but only meaningful on top of the chain-head
    # fold (it annotates heads the fold surfaced) and only when the chain store
    # has data.
    render_trail = fold_superseded and chain_trail and evo_ready
    conn.row_factory = sqlite3.Row
    budget = _Budget(max_chars)
    seen: set[str] = set()
    sections: list[str] = []

    if schema_prior:
        prior_text = "# 用户惯性先验\n" + "\n".join(schema_prior)
        # Highest priority: it claims budget first, ahead of every other layer.
        if budget.add(prior_text, layer="schema_prior"):
            sections.append(prior_text)

    if workthread_chars > 0:
        # WorkThread layer (S3): independent budget — rendered against its own
        # cap, its per-layer counters folded into the shared telemetry under
        # layer "workthread" but WITHOUT charging the main ``budget.used``
        # (位次让 schema_prior，预算不挤占主层 — this runs before the main layers,
        # so charging used here would squeeze them out). Its chars are surfaced
        # into the tick's ``used``口径 only at the telemetry write below (#623).
        wt_text = _workthread_layer(
            conn,
            max_chars=workthread_chars,
            min_confidence=workthread_min_confidence,
            budget=budget,
        )
        if wt_text:
            sections.append(wt_text)

    scene = _scene_layer(conn, scope, budget)
    if scene:
        sections.append("# 场景意图\n" + "\n".join(scene))

    priors = _hint_layer(
        conn,
        hints,
        _BEHAVIOR_PREFIXES,
        per_hint,
        budget,
        seen,
        layer="behavior",
        fold_superseded=fold_superseded,
        evo_ready=evo_ready,
        chain_trail=render_trail,
        include_confidence=include_confidence,
    )
    if priors:
        sections.append("# 行为先验\n" + "\n".join(priors))

    facts = _hint_layer(
        conn,
        hints,
        _FACT_PREFIXES,
        per_hint,
        budget,
        seen,
        layer="fact",
        fold_superseded=fold_superseded,
        evo_ready=evo_ready,
        chain_trail=render_trail,
        include_confidence=include_confidence,
    )
    if facts:
        sections.append("# 相关记忆\n" + "\n".join(facts))

    # ⑤.5 语义相关记忆 — dense recall of conceptually-related memory the lexical
    # layers can't reach (no shared keyword). Sits AFTER precise facts (it never
    # squeezes them out) and BEFORE the keyword fallback. The caller passes
    # ``dense_query`` only when the semantic layer is enabled; ``_dense_pool``
    # fail-opens to [] with no creds → byte-identical to the lexical-only output.
    if dense_query and dense_top_k > 0:
        semantic = _semantic_layer(
            conn,
            query=dense_query,
            top_k=dense_top_k,
            budget=budget,
            seen=seen,
            chain_trail=render_trail,
            include_confidence=include_confidence,
            exclude_events=not include_events,
        )
        if semantic:
            sections.append("# 语义相关记忆\n" + "\n".join(semantic))

    fallback = _hint_layer(
        conn,
        hints,
        None,
        per_hint,
        budget,
        seen,
        layer="keyword",
        exclude_events=not include_events,
        fold_superseded=fold_superseded,
        evo_ready=evo_ready,
        chain_trail=render_trail,
        include_confidence=include_confidence,
    )
    if fallback:
        sections.append("# 其他命中\n" + "\n".join(fallback))

    if recent_events_hours > 0:
        # WorkThread S0 基线: descriptive recent-work background, LAST in
        # priority — it informs, it never displaces decision-relevant layers.
        recent = _recent_events_layer(conn, hours=recent_events_hours, budget=budget)
        if recent:
            sections.append("# 近期活动（event-daily 摘要）\n" + "\n".join(recent))

    # Budget telemetry (ablation report 2026-06-10 §4 建议): record how this
    # call spent its budget — per-layer admitted/rejected counts+chars and the
    # derived ``squeezed`` flag — so the real-world squeeze rate is measurable.
    # ``hints`` ride the tick as debugging telemetry (what drove this recall).
    # Best-effort side-channel: a failed write must never affect recall.
    try:
        # Surface the workthread independent budget into the tick口径 ONLY here
        # (#623) — never into the in-memory ``budget.used`` that drives admission
        # (review finding: workthread injects EARLY, so charging ``budget.used``
        # squeezes the main layers out). The true assembly ceiling is
        # ``max_chars + workthread_chars`` (workthread renders against its own
        # cap ON TOP of the main budget), and the true used is the main
        # ``budget.used`` PLUS the workthread chars actually admitted — the
        # latter already folded into ``budget.layers["workthread"]`` by
        # ``_workthread_layer``. Recording only ``max_chars`` / main-``used``
        # systematically under-reports capacity and skews the "raise max_chars?"
        # decision; the per-layer counters (incl. workthread rejected) are
        # already口径-correct in ``budget.layers``.
        wt_admitted_chars = budget.layers.get("workthread", {}).get("admitted_chars", 0)
        recall_budget_ticks.record_tick(
            conn,
            ts=datetime.now().isoformat(timespec="seconds"),
            scope=scope,
            max_chars=max_chars + max(workthread_chars, 0),
            used=budget.used + wt_admitted_chars,
            layers=budget.layers,
            hints=hints,
        )
    except Exception as exc:  # noqa: BLE001 — telemetry must never break recall
        logger.warning("recall budget telemetry write failed (ignored): %s", exc)

    return "\n\n".join(sections)


def assemble_background_structured(
    conn: sqlite3.Connection,
    *,
    scope: str,
    hints: list[str],
    per_hint: int = 2,
    max_chars: int = 1200,
    include_events: bool = False,
    schema_pairs: list[tuple[str, str]] | None = None,
    fold_superseded: bool = False,
    chain_trail: bool = False,
    include_confidence: bool = False,
    recent_events_hours: int = 0,
    dense_query: str | None = None,
    dense_top_k: int = 0,
    include_raw_handles: bool = True,
    per_layer_cap: int = 0,
) -> list[RecallItem]:
    """Structured, cited sibling of :func:`assemble_background` (recall pack endpoint).

    Runs the SAME per-layer helpers in the SAME priority order with a structured sink,
    so the returned items mirror what the string assembler would surface — but each hit
    carries a citation handle (``cite``) and, for scene items, a RAW capture handle.
    Schema items are emitted directly from ``schema_pairs`` (line, source-file) so each
    carries ``schema:<file>``.

    This is read-only telemetry-free (no ``record_tick``): it is an API/inspection path,
    not a recognition tick. ``per_layer_cap`` > 0 keeps at most N items per layer
    (order-preserving) for prompt economy. The dense layer fail-opens to nothing without
    embedding creds, exactly like the string path."""
    evo_ready = _evo_fold_ready(conn) if (fold_superseded or chain_trail) else False
    render_trail = fold_superseded and chain_trail and evo_ready
    conn.row_factory = sqlite3.Row
    budget = _Budget(max_chars)
    seen: set[str] = set()
    sink: list[RecallItem] = []

    for line, source in schema_pairs or []:
        if not line.strip():
            continue
        if budget.add(line, layer="schema_prior"):
            sink.append(RecallItem(layer="schema", content=line.strip(), cite=f"schema:{source}"))

    _scene_layer(conn, scope, budget, sink=sink, include_raw_handles=include_raw_handles)

    for prefixes, telem in ((_BEHAVIOR_PREFIXES, "behavior"), (_FACT_PREFIXES, "fact")):
        _hint_layer(
            conn,
            hints,
            prefixes,
            per_hint,
            budget,
            seen,
            layer=telem,
            fold_superseded=fold_superseded,
            evo_ready=evo_ready,
            chain_trail=render_trail,
            include_confidence=include_confidence,
            sink=sink,
        )

    if dense_query and dense_top_k > 0:
        _semantic_layer(
            conn,
            query=dense_query,
            top_k=dense_top_k,
            budget=budget,
            seen=seen,
            chain_trail=render_trail,
            include_confidence=include_confidence,
            exclude_events=not include_events,
            sink=sink,
        )

    _hint_layer(
        conn,
        hints,
        None,
        per_hint,
        budget,
        seen,
        layer="keyword",
        exclude_events=not include_events,
        fold_superseded=fold_superseded,
        evo_ready=evo_ready,
        chain_trail=render_trail,
        include_confidence=include_confidence,
        sink=sink,
    )

    if recent_events_hours > 0:
        _recent_events_layer(conn, hours=recent_events_hours, budget=budget, sink=sink)

    if per_layer_cap > 0:
        kept: list[RecallItem] = []
        counts: dict[str, int] = {}
        for it in sink:
            n = counts.get(it.layer, 0)
            if n < per_layer_cap:
                counts[it.layer] = n + 1
                kept.append(it)
        return kept
    return sink


def _workthread_layer(
    conn: sqlite3.Connection, *, max_chars: int, min_confidence: float, budget: _Budget
) -> str:
    """当前工作线 section（WorkThread S3, spec §六-1）.

    Renders the active thread + at most ONE background thread against an
    **independent** char budget (it never competes with the main layers — the
    spec's "独立预算 200 字符" contract), then folds its admit/reject counters
    into the shared telemetry under layer ``workthread``.

    Crucially, this layer is injected EARLY (right after schema_prior, before
    scene/behavior/fact/keyword/events), so it must NOT mutate the shared
    ``budget.used`` — doing so would inflate the count every downstream layer
    reads and squeeze them out, breaking the 独立预算不挤占主层 contract (#623
    review: an after-workthread layer like 近期活动 would admit less). Only the
    per-layer counters (which never feed the admission decision) are folded in
    here; the workthread chars are surfaced into the tick's ``used``口径 at the
    telemetry write site (``assemble_background``) by adding the already-folded
    ``layers["workthread"]["admitted_chars"]`` — so the recorded ``used`` /
    squeeze rate / capacity decision reflect the real assembly ceiling
    (``main + workthread``) WITHOUT poisoning admission. The pre-#623 code only
    mirrored ``admitted`` and never counted ``rejected``, so a workthread line
    over its own independent budget vanished from telemetry (rejected read 0);
    that is fixed by charging the local budget under the ``workthread`` layer.
    Threads below ``min_confidence`` are skipped (纠错口上线前的保守闸：错背景
    是可观测但仍有代价的注入). Empty string when nothing qualifies or the
    work_threads table doesn't exist yet.
    """
    try:
        from ..workthread import store as wt_store
    except ImportError:  # pragma: no cover - defensive
        return ""
    try:
        active = wt_store.active_thread(conn)
        background = [t for t in wt_store.list_threads(conn, statuses=("background",), limit=2)]
    except Exception:  # noqa: BLE001 — recall must never break on a missing table
        return ""
    local = _Budget(max_chars)
    lines: list[str] = []
    for thread, label in [(active, "进行中"), *[(t, "后台") for t in background[:1]]]:
        if thread is None or thread.confidence < min_confidence:
            continue
        approx = "≈" if thread.approximate else ""
        origin = f"，{thread.origin_actor} 交办" if thread.origin_actor else ""
        line = f"[{label}] {thread.title}（累计 {thread.total_active_minutes}{approx}min{origin}）"
        recent = thread.progress_notes[-1] if thread.progress_notes else ""
        if recent:
            line += f" 最近：{recent[:60]}"
        # Charge the local (independent) budget under the ``workthread`` layer so
        # both admitted AND rejected are counted (an over-cap line rejected here
        # must surface as a workthread rejection, not vanish — #623 finding M).
        if local.add(line, layer="workthread"):
            lines.append(line)
    # Fold ONLY the independent budget's per-layer counters into the shared
    # telemetry桶 (#623). Counters never feed the admission decision, so this is
    # safe to do here; the shared ``budget.used`` is deliberately NOT touched —
    # this layer runs before the main layers, and inflating ``used`` would
    # squeeze them out (review finding). The workthread chars are added to the
    # tick's ``used``口径 at the telemetry write instead, from the folded
    # ``admitted_chars`` below.
    src = local.layers.get("workthread")
    dst = budget.layers.get("workthread")
    if src is not None and dst is not None:
        for counter in recall_budget_ticks.COUNTERS:
            dst[counter] += src[counter]
    if not lines:
        return ""
    return "# 当前工作线\n" + "\n".join(lines)


def _recent_events_layer(
    conn: sqlite3.Connection,
    *,
    hours: int,
    budget: _Budget,
    sink: list[RecallItem] | None = None,
) -> list[str]:
    """近 N 小时的 event-daily 条目（WorkThread S0 描述性背景）.

    Reads the most recent session-summary entries from ``event-*`` files in the
    FTS projection (timestamp-ordered, no keyword match needed — recency IS the
    relevance signal here) and admits them against the SHARED budget, last in
    priority. Telemetry layer: ``events``.
    """
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="minutes")
    try:
        rows = conn.execute(
            "SELECT path, timestamp, content FROM entries "
            "WHERE path LIKE 'event-%' AND superseded = 0 AND timestamp >= ? "
            "ORDER BY timestamp DESC LIMIT 6",
            (cutoff,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    out: list[str] = []
    items: list[RecallItem] = []
    for row in rows:
        # One line per entry: collapse the multi-line session entry to its
        # summary head — descriptive perception, not a verbatim re-dump.
        text = " ".join(str(row["content"]).split())[:200]
        line = f"[{row['timestamp']}] {text}"
        if not budget.add(line, layer="events"):
            break
        out.append(line)
        if sink is not None:
            items.append(RecallItem(layer="event", content=line, cite=f"mem:{row['path']}"))
    out.reverse()  # chronological (oldest→newest) reads better as a narrative
    if sink is not None:
        items.reverse()  # mirror the string layer's chronological order
        sink.extend(items)
    return out


# Statuses that mean "no longer a live scene intent" — excluded from the recall
# scene layer. ``resolved`` is the evidence-auto-close terminal (识别即更新状态):
# a done/rejected commitment must not re-enter the recognition prompt as positive
# scene material (is_expired also filters it, but exclude it explicitly here too).
_SCENE_RESOLVED_STATUSES = frozenset({"dismissed", "consumed", "resolved"})


def _scene_layer(
    conn: sqlite3.Connection,
    scope: str,
    budget: _Budget,
    sink: list[RecallItem] | None = None,
    include_raw_handles: bool = True,
) -> list[str]:
    """Recent intents recognized in this scope — the scene-aware ① context.

    ``sink`` (side-channel, default None): when set, each admitted intent appends a
    ``scene_intent`` :class:`RecallItem` carrying ``cite=intent:<id>`` plus the RAW
    capture handle (``capture_stem`` for a fast-path intent, else ``timeline_block_id``
    from a ``timeline_block`` evidence ref). ``include_raw_handles=False`` suppresses the
    stem/id (the external-prompt privacy path)."""
    if not scope:
        return []
    try:
        intents = intent_store.intents_for_scope(conn, scope)
    except sqlite3.OperationalError:
        return []
    # Status filter (#533): the scene layer rendered EVERY intent in the scope
    # regardless of status, so a ``dismissed`` (or ``consumed``) intent re-entered
    # the recognition prompt as a POSITIVE "场景意图" — directly contradicting the
    # negative-prior section of the SAME prompt (the recognizer is told "勿重复
    # surface 同类" while this layer re-surfaces the exact thing the user swatted
    # away). Final user feedback must not be re-injected as live context: drop
    # dismissed/consumed here. (open/armed stay; expired is handled just below.)
    intents = [it for it in intents if it.status not in _SCENE_RESOLVED_STATUSES]
    # Lifecycle filter (#546): expired commitments (harvested OR valid_until
    # already passed) are stale context — injecting them re-pollutes exactly
    # the layer the expiry harvest exists to keep clean.
    now_iso = datetime.now().isoformat(timespec="seconds")
    intents = [it for it in intents if not intent_store.is_expired(it, now=now_iso)]
    out: list[str] = []
    for it in intents[-8:]:  # most recent, chronological
        # assignment (WorkThread S0) carries its substance in ``task_text``
        # ("Kevin 让我做 X" 的 X)；带逐字引文+人名地浮出正是 S0 的交付。
        text = str(
            it.payload.get("text") or it.payload.get("task_text") or it.rationale or ""
        ).strip()[:120]
        if not text:
            continue
        actor = str(it.payload.get("assigned_by") or "").strip()
        line = f"[{it.kind}] {text}" + (f"（{actor} 交办）" if actor else "")
        if not budget.add(line, layer="scene"):
            break
        out.append(line)
        if sink is not None:
            stem: str | None = None
            block_id: int | None = None
            if include_raw_handles:
                stem = intent_store.source_capture_stem(it)
                if stem is None:
                    for ev in it.evidence:
                        if getattr(ev, "source", "") == "timeline_block":
                            try:
                                block_id = int(ev.ref_id)
                            except (TypeError, ValueError):
                                block_id = None
                            break
            sink.append(
                RecallItem(
                    layer="scene_intent",
                    content=line,
                    cite=f"intent:{it.id}" if it.id is not None else "intent:?",
                    score=(it.confidence or None),
                    capture_stem=stem,
                    timeline_block_id=block_id,
                )
            )
    return out


def _hint_layer(
    conn: sqlite3.Connection,
    hints: list[str],
    prefixes: tuple[str, ...] | None,
    per_hint: int,
    budget: _Budget,
    seen: set[str],
    *,
    layer: str,
    exclude_events: bool = False,
    fold_superseded: bool = False,
    evo_ready: bool = False,
    chain_trail: bool = False,
    include_confidence: bool = False,
    sink: list[RecallItem] | None = None,
) -> list[str]:
    """FTS hits for ``hints``, optionally constrained to ``prefixes`` (one layer).

    ``fold_superseded`` folds each hit to its evolution-chain head, read from
    ``evo_nodes`` (:data:`_EVO_FOLD_SQL`, scope=default — §1.4; the entry_chain
    fold was retired in PR-7). A subquery (not a JOIN) is used because
    ``entries`` is an FTS5 virtual table and FTS5 MATCH does not compose with a
    regular-table JOIN. event-* entries keep the ``superseded=0`` judgment (Q2
    exemption — they are not in evo_nodes but a keyword hit on them must
    survive). When ``evo_ready`` is False (evo_nodes missing/empty — fresh
    install before its first backfill) the fold degrades to the equivalent
    ``superseded = 0`` derived-column guard (:data:`_COLUMN_FOLD_SQL`).

    When ``chain_trail`` is on (which presupposes the fold surfaced the heads
    AND ``evo_ready``), a hit that is the head of a multi-member chain is
    additionally annotated with a compact evolution trail of its superseded
    ancestors (``← [曾] …``, latest→oldest, rendered from the evo_nodes
    bidirectional pointers) so the recognizer SEES the attitude evolution, not
    just the latest belief.
    """
    out: list[str] = []
    for raw in hints:
        hint = (raw or "").strip()
        if not hint or budget.full():
            continue
        clause = ""
        # FTS5-escape the hint (双引号包裹 + 内部引号翻倍) so apostrophes /
        # quotes / colons inside a hint can't crash the MATCH parser.
        params: list[object] = [_fts5_match_expr(hint)]
        if prefixes:
            clause = " AND (" + " OR ".join("path LIKE ?" for _ in prefixes) + ")"
            params += [f"{p}%" for p in prefixes]
        elif exclude_events:
            clause = " AND path NOT LIKE 'event-%'"
        if fold_superseded:
            if evo_ready:
                # The chain-head fold: evo_nodes active heads (+ Q2 event- exemption).
                clause += _EVO_FOLD_SQL
                params += list(_EVO_SCOPE)
            else:
                # 冷启动退化：等价的 superseded 派生列折叠（见模块常量注释）。
                clause += _COLUMN_FOLD_SQL
        params.append(per_hint)
        # ``id`` is needed to bridge a hit to its chain trail OR its reliability
        # metadata; selecting it only when one of those will be rendered keeps every
        # other path's query (and output) byte-identical.
        columns = "id, path, content" if (chain_trail or include_confidence) else "path, content"
        try:
            rows = conn.execute(
                f"SELECT {columns} FROM entries "
                f"WHERE entries MATCH ?{clause} ORDER BY rank LIMIT ?",
                params,
            ).fetchall()
        except sqlite3.OperationalError as exc:
            # A malformed FTS query (rare residual syntax form) or a corrupt
            # evo_nodes table lands here. Log it so the degradation is VISIBLE
            # (the fix is `persome evomem-backfill`, not hiding the
            # drift); the hint is skipped, recall never raises.
            logger.warning(
                "recall fold query failed for hint %r (evo_nodes corrupt? run "
                "evomem-backfill): %s — degrading to skip",
                hint,
                exc,
            )
            continue
        out.extend(
            _admit_rows(
                conn,
                rows,
                budget=budget,
                seen=seen,
                layer=layer,
                chain_trail=chain_trail,
                include_confidence=include_confidence,
                sink=sink,
            )
        )
    return out


def _admit_rows(
    conn: sqlite3.Connection,
    rows: list,
    *,
    budget: _Budget,
    seen: set[str],
    layer: str,
    chain_trail: bool,
    include_confidence: bool,
    sink: list[RecallItem] | None = None,
) -> list[str]:
    """Render + budget-admit a sequence of entry rows (the shared tail of the keyword
    and semantic layers, so BOTH fold/annotate identically). ``rows`` carry ``path`` +
    ``content`` (and ``id`` when chain_trail/include_confidence). Stops at the first
    row that overflows the budget (caller's loop also halts on ``budget.full()``).

    ``sink`` (default None) is a pure side-channel: when a list is passed, each admitted
    row appends a :class:`RecallItem` at the SAME point its string line is appended (same
    admission/dedup/order), so the structured pack mirrors the string pack. None → the
    string path is byte-identical to before."""
    out: list[str] = []
    for row in rows:
        path, content = row["path"], row["content"]
        key = _seen_key(path, content)
        if key in seen:
            continue
        note = _reliability_note(conn, row["id"]) if include_confidence else ""
        if chain_trail:
            # Trail path: render the head as a single-line "current belief" (machine
            # ``<!-- supersedes -->`` annotation stripped, newlines collapsed). The head
            # claims budget FIRST; only if it lands do we try to also fit the ``← [曾] …``
            # trajectory. A trail that doesn't fit is dropped (head-only) — the trajectory
            # is a low-priority supplement and must never squeeze a hit out of the budget.
            head_snip = f"[{path}]{note} {_clean_belief(content)[:300]}"
            if not budget.add(head_snip, layer=layer):
                break
            seen.add(key)
            trail = _chain_trail_evo(conn, row["id"])
            out.append(
                head_snip + trail if (trail and budget.add(trail, layer="trail")) else head_snip
            )
            if sink is not None:
                sink.append(
                    _row_item(
                        conn, row, path, _clean_belief(content)[:300], layer, include_confidence
                    )
                )
        else:
            snippet = f"[{path}]{note} {content[:300]}"
            if not budget.add(snippet, layer=layer):
                break
            seen.add(key)
            out.append(snippet)
            if sink is not None:
                sink.append(
                    _row_item(conn, row, path, str(content)[:300], layer, include_confidence)
                )
    return out


def _row_item(
    conn: sqlite3.Connection,
    row,
    path: str,
    content: str,
    telemetry_layer: str,
    include_confidence: bool,
) -> RecallItem:
    """Build the structured :class:`RecallItem` for one admitted memory row (sink path
    only). Reads reliability metadata when available so a low-confidence/conflicted memory
    is flagged for the consumer the same way the string note ``⚠(…)`` flags it."""
    conf: str | None = None
    conflicted = False
    try:
        if include_confidence and "id" in row.keys():  # noqa: SIM118 — sqlite3.Row: `in` tests values, not columns
            meta = fts_store.get_entry_metadata(conn, row["id"]) or {}
            conflicted = bool(meta.get("conflicted"))
            if meta.get("confidence") == "low":
                conf = "low"
    except Exception:  # noqa: BLE001 — structured metadata is best-effort, never breaks recall
        pass
    return RecallItem(
        layer=_response_layer(telemetry_layer, path),
        content=content.strip(),
        cite=f"mem:{path}",
        confidence=conf,
        conflicted=conflicted,
    )


def _semantic_layer(
    conn: sqlite3.Connection,
    *,
    query: str,
    top_k: int,
    budget: _Budget,
    seen: set[str],
    chain_trail: bool = False,
    include_confidence: bool = False,
    exclude_events: bool = True,
    sink: list[RecallItem] | None = None,
) -> list[str]:
    """Dense/semantic recall: embed ``query`` once and surface conceptually-related
    memory — what the lexical layers can never reach (no shared keyword needed).

    Reuses the shipped te3-large index via ``fts._dense_pool`` (cached matrix, fail-open
    ``[]`` when dense is off / no creds / no vectors → this layer is then a no-op, so the
    background is byte-identical to the lexical-only output). The dense pool already returns
    only LIVE entries (``live_matrix`` joins ``superseded=0``, which ≡ the evolution-chain
    head per the P1 invariant for non-event entries), so NO extra chain-head fold is applied
    — adding one would wrongly drop live intent/thread hits that aren't entity-chain nodes.
    Hits are fetched as real ``entries`` rows and routed through the SAME ``_admit_rows``
    confidence/trail machinery as the keyword layer; dense rank order is preserved."""
    q = (query or "").strip()
    if not q or top_k <= 0 or budget.full():
        return []
    # The slow path passes the WHOLE session trajectory (`session_events`, tens of thousands
    # of chars) as the query — far past te3-large's 8191-token limit, so the embed 400'd and
    # this layer was a silent no-op in production (the dead-semantic bug). Embed the RECENT
    # TAIL only: it's the current activity (what "recall related memory" should key on) and
    # stays well under the token cap. `_QUERY_TAIL_CHARS` < embeddings_client `_MAX_CHARS`.
    q = q[-_QUERY_TAIL_CHARS:]
    # Scope the dense pool to DURABLE memory paths. The vector corpus is ~70% event-* raw
    # activity logs; the trajectory query is itself event-like, so an unscoped top-k is
    # entirely events — which this layer then excludes, leaving nothing (the second
    # dead-semantic cause). Ranking only durable kinds (user/project/topic/person/tool/
    # intent/thread/schema) surfaces the facts the lexical layers can't reach. The post-fetch
    # event clause stays as belt-and-suspenders. ``min_sim`` drops near-orthogonal hits so a
    # scene related to nothing surfaces nothing (the layer is unfused — no BM25 anchor).
    globs = _DURABLE_GLOBS if exclude_events else None
    ids = fts_store._dense_pool(
        conn,
        query=q,
        path_patterns=globs,
        top_k=top_k * 3,
        embedder=None,
        min_sim=_SEMANTIC_MIN_SIM,
    )
    if not ids:
        return []
    clause = " AND path NOT LIKE 'event-%'" if exclude_events else ""
    params: list[object] = list(ids)
    placeholders = ",".join("?" for _ in ids)
    try:
        rows = conn.execute(
            f"SELECT id, path, content FROM entries WHERE id IN ({placeholders}){clause}",
            params,
        ).fetchall()
    except sqlite3.OperationalError as exc:  # corrupt evo_nodes etc. — never break recall
        logger.warning("semantic recall fetch failed (run evomem-backfill?): %s", exc)
        return []
    by_id = {r["id"]: r for r in rows}
    ordered = [by_id[i] for i in ids if i in by_id][:top_k]  # preserve dense ranking
    return _admit_rows(
        conn,
        ordered,
        budget=budget,
        seen=seen,
        layer="semantic",
        chain_trail=chain_trail,
        include_confidence=include_confidence,
        sink=sink,
    )


def _reliability_note(conn: sqlite3.Connection, entry_id: str | None) -> str:
    """Compact reliability annotation for one hit (meta-cognition layer).

    Reads ``entry_metadata`` and surfaces only the signals that should make the
    recognizer *more cautious*: a ``confidence:low`` memory and a ``conflicted``
    one. high/medium memories are left unmarked so the note draws attention to the
    shaky ones rather than labelling everything. Returns ``""`` when there's no
    metadata row (the common case) or only neutral metadata.
    """
    if not entry_id:
        return ""
    meta = fts_store.get_entry_metadata(conn, entry_id)
    if not meta:
        return ""
    parts: list[str] = []
    if meta.get("conflicted"):
        parts.append("冲突未裁决")
    if meta.get("confidence") == "low":
        parts.append("低置信")
    return f" ⚠({'·'.join(parts)})" if parts else ""


# Caps on the evolution trail so it informs without blowing the recall budget: at
# most this many superseded ancestors (chain depth), each compressed to this many
# chars. A trajectory is a hint of态度演变, not a full re-dump of every old belief.
_TRAIL_MAX_ANCESTORS = 3
_TRAIL_SNIPPET_CHARS = 60

# The machine annotation ``supersede_entry`` appends to a new entry's body
# (``<!-- supersedes: id; reason: ... -->``). It's provenance bookkeeping, not user
# belief, so the trail rendering strips it (and collapses newlines) to keep the
# trajectory readable. Only applied in the trail path → flag-off is unchanged.
_SUPERSEDES_COMMENT_RE = re.compile(r"<!--\s*supersedes:.*?-->", re.DOTALL)


def _clean_belief(content: str) -> str:
    """Strip the ``<!-- supersedes -->`` annotation and collapse whitespace to one line."""
    text = _SUPERSEDES_COMMENT_RE.sub("", content)
    return " ".join(text.split())


# ── evo_nodes chain traversal (evomem SSOT switch §1.4) ─────────────────────


def _evo_get_by_ids(conn: sqlite3.Connection) -> Callable[[list[str]], list]:
    """Conn-bound ``get_by_ids`` for evomem chain traversal (scope=default, Q4).

    Mirrors ``evomem.store.NodeStore.get_by_ids`` (no status filter — chain
    traversal must reach shadowed history nodes) but rides the caller's open
    connection instead of opening a fresh one per hop. Lazy imports keep the
    flag-off path import-free.
    """
    from ..evomem.store import _row_to_node

    def get_by_ids(ids: list[str]) -> list:
        wanted = [i for i in ids if i]
        if not wanted:
            return []
        placeholders = ",".join("?" * len(wanted))
        rows = conn.execute(
            f"SELECT * FROM evo_nodes WHERE user_id = ? AND agent_id = ?"
            f" AND node_id IN ({placeholders})",
            (*_EVO_SCOPE, *wanted),
        ).fetchall()
        by_id = {r["node_id"]: _row_to_node(r) for r in rows}
        return [by_id[i] for i in wanted if i in by_id]

    return get_by_ids


def evo_chain_ordered(conn: sqlite3.Connection, head_id: str) -> list:
    """``[head, *ancestors]`` — the evo_nodes chain of ``head_id``, latest→oldest.

    Whole-chain membership comes from the canonical evomem fold,
    :func:`evomem.chain.expand_evolution_chains` (双向 BFS — its first production
    caller, §1.4); the *rendering order* then walks the ``supersedes`` pointers
    backwards from the hit head. Pointer order — not timestamps — is what makes
    the order deterministic AND byte-compatible with the retired entry_chain
    trail's ``rowid DESC`` (write order) even when a whole supersede burst lands
    within one minute: along a linear chain (anti-fork invariant) the
    predecessor walk IS the reverse write order, with no tiebreak wobble. Multi-
    predecessor nodes (rare: two old entries absorbed by one successor) order
    their predecessors by ``node_id`` descending for determinism.

    Returns ``[]`` when the head is unknown to evo_nodes, ``[head]`` when it is
    an isolated (off-chain) node. Raises ``sqlite3.OperationalError`` when the
    table is missing — callers degrade best-effort.
    """
    from ..evomem import chain as evo_chain

    get_by_ids = _evo_get_by_ids(conn)
    heads = get_by_ids([head_id])
    if not heads:
        return []
    head = heads[0]
    if not head.is_on_chain():
        return [head]
    folded = evo_chain.expand_evolution_chains(
        get_by_ids, [{"node_id": head_id, "score": 0.0, "node": head}]
    )
    members_by_id = {head.node_id: head}
    if folded and folded[0].get("evolution_chain"):
        for n in get_by_ids([it["node_id"] for it in folded[0]["evolution_chain"]]):
            members_by_id.setdefault(n.node_id, n)
    ordered = [head]
    seen_ids = {head.node_id}
    frontier = [head]
    while frontier:
        cur = frontier.pop(0)
        preds = [
            members_by_id[p]
            for p in sorted(cur.supersedes, reverse=True)
            if p in members_by_id and p not in seen_ids
        ]
        for p in preds:
            seen_ids.add(p.node_id)
            ordered.append(p)
        frontier = preds + frontier  # depth-first down the chain
    return ordered


def _chain_trail_evo(conn: sqlite3.Connection, head_id: str) -> str:
    """Render chain-head ``head_id``'s ancestors as a compact trail（§1.4 演化链轨迹）.

    Rendering contract — ``_TRAIL_MAX_ANCESTORS`` cap, ``_TRAIL_SNIPPET_CHARS``
    truncation, ``← [曾]`` for a contradiction vs ``← [精炼自]`` for a
    refinement (discriminator = the ``refined_from`` column), ancestors from the
    bidirectional pointer walk. Best-effort: any ``OperationalError`` (evo_nodes
    missing/corrupt) degrades to an empty trail and is logged — the fix is
    ``persome evomem-backfill``, not hiding the drift.
    """
    try:
        ordered = evo_chain_ordered(conn, head_id)
    except sqlite3.OperationalError as exc:
        logger.warning(
            "recall evo chain-trail query failed for head %r (evo_nodes "
            "missing/corrupt? run evomem-backfill): %s — degrading to head-only",
            head_id,
            exc,
        )
        return ""
    if len(ordered) <= 1:
        return ""
    refined_targets = {n.refined_from for n in ordered if n.refined_from}
    parts: list[str] = []
    for n in ordered[1:]:
        if n.is_latest:
            continue  # the head is rendered separately by the caller
        if len(parts) >= _TRAIL_MAX_ANCESTORS:
            break
        text = _clean_belief(n.content or "")[:_TRAIL_SNIPPET_CHARS]
        if text:
            marker = "[精炼自]" if n.node_id in refined_targets else "[曾]"
            parts.append(f" ← {marker} {text}")
    return "".join(parts)

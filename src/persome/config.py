"""TOML config loader with defaults and per-stage LLM resolution."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from . import paths


@dataclass
class ModelConfig:
    model: str = "deepseek-v4-flash"
    # Optional override; when empty, ``provider_base_url(model)`` falls back to
    # the canonical ``{PROVIDER}_BASE_URL`` environment variable.
    base_url: str = ""
    max_tokens: int | None = None


@dataclass
class CaptureConfig:
    # Where capture inputs (AX tree + screenshot) come from:
    #   "daemon" — the daemon owns OS capture (spawns mac-ax-watcher/mac-ax-helper,
    #              grabs screenshots in-process). Legacy default, byte-identical.
    #   "ingest" — a trusted local producer pushes pre-built payloads via
    #              POST /captures/ingest; the daemon runs no watcher / OS grab.
    source: str = "daemon"
    # Event-driven capture knobs (only used when source == "daemon")
    event_driven: bool = True  # consume mac-ax-watcher events
    heartbeat_minutes: int = 10  # periodic capture even without events
    debounce_seconds: float = 3.0  # for AXValueChanged bursts
    min_capture_gap_seconds: float = 2.0  # between consecutive captures
    dedup_interval_seconds: float = 1.0  # per-event-type dedup window
    same_window_dedup_seconds: float = (
        5.0  # skip repeat non-focus capture in same window within this window
    )
    # Legacy timer knob (kept for back-compat; also treated as a floor on heartbeat)
    interval_minutes: int = 10
    # Tiered buffer retention:
    #   * whole JSON is deleted once older than `buffer_retention_hours`
    #     AND already absorbed by a timeline block
    #   * screenshot (base64) is stripped from JSONs older than
    #     `screenshot_retention_hours` — it's 77% of the bytes and nothing
    #     downstream currently consumes it
    #   * `buffer_max_mb` is a hard ceiling; when exceeded the oldest
    #     already-absorbed files are deleted first (0 disables the cap)
    buffer_retention_hours: int = 168
    screenshot_retention_hours: int = 24
    #   * pixel-axis graded forgetting (memory-rebuild spec §2.1): screenshots in
    #     JSONs older than `screenshot_thumbnail_hours` (but younger than the
    #     strip cutoff) are downscaled in place to a ≤480px JPEG — the 缩略 tier
    #     between full-res and strip (全分辨率→缩略→仅存文本化→删除). 0 disables
    #     (default — byte-identical legacy). Encrypted screenshots are
    #     decrypted/downscaled/re-encrypted; key-unavailable ⇒ untouched.
    screenshot_thumbnail_hours: int = 0
    buffer_max_mb: int = 2000
    include_screenshot: bool = True
    screenshot_max_width: int = 1920
    screenshot_jpeg_quality: int = 80
    ax_depth: int = 100
    ax_timeout_seconds: int = 3
    # OCR fallback for AX-poor apps (WeChat, Feishu, etc.) — on-device PP-OCRv6.
    enable_ocr_fallback: bool = False
    ocr_tier: str = "tiny"  # tiny | small | medium (local PP-OCRv6 weights)
    ocr_min_gap_seconds: float = 15.0
    # Geometry structuring of raw OCR (zero LLM, on-device): reconstruct columns/regions
    # + per-app field labels (WeChat contact/time/preview) instead of a flat noisy join.
    ocr_structured: bool = True
    # cmux signal source (issue #558): when the frontmost app is cmux, read
    # the visible terminal text over its local unix-socket RPC and append it
    # to visible_text. Safe-by-construction: read-only local socket, zero
    # external cost, sub-second deadline, silent degrade on any failure.
    cmux_source_enabled: bool = True
    # Privacy and evidence-retention controls belong to the capture subsystem.
    pause_on_lock: bool = True
    suppress_secure_input: bool = True
    encrypt_screenshots: bool = True
    extended_retention_enabled: bool = True
    actionable_retention_days: int = 7


@dataclass
class TimelineConfig:
    # Wall-clock window length for each aggregator block. 1-min blocks
    # keep timeline entries close to verbatim — the reducer (which runs
    # every flush_minutes ≥5m) is the stage that does real compression.
    window_minutes: int = 1
    cold_lookback_minutes: int = 30
    # Parallel LLM workers used when processing a backlog of closed windows.
    # Has no effect when there is only 1 pending window (steady state).
    max_parallel_windows: int = 4
    # Attention-locus authority flip (Step 1, spec 2026-06-18-attention-locus).
    # When on (default), the aggregator feeds the LLM the code-resolved locus
    # content (PRIMARY/PERIPHERAL, chrome dropped for resolver-backed apps)
    # instead of the raw screen dump. Off = the pre-Step-1 feed; exists so the
    # Phase-5 oracle can A/B locus-on vs locus-off on the same captures.
    attention_locus_enabled: bool = True


@dataclass
class WriterConfig:
    soft_limit_tokens: int = 20_000
    max_tool_iterations: int = 12
    context_token_limit: int = 80_000
    llm_retry_attempts: int = 6
    llm_rate_limit_wait_s: int = 30
    llm_fallback_model: str = ""
    tool_result_max_bytes: int = 16_384
    tool_result_total_budget: int = 131_072
    max_output_tokens_recovery_limit: int = 65_536
    max_output_tokens_recovery_count: int = 3
    use_token_count_api: bool = False
    # How the classifier resolves contradictions surfaced by search_memory:
    #   "abstract"  — synthesize a higher-level rule that supersedes both
    #                 conflicting entries (default, preserves information)
    #   "supersede" — fall back to the legacy behavior of superseding the
    #                 old entry with the new value
    # The classifier prompt always teaches the abstraction path; this flag
    # only changes the directive injected into the user message.
    contradiction_strategy: str = "abstract"
    # Run pending per-file compaction every N completed classifier sessions.
    consolidation_cadence: int = 8


@dataclass
class SessionConfig:
    # Hard cut: no capture-worthy events for this many minutes
    gap_minutes: int = 5
    # Soft cut: single unrelated app focused for this many minutes
    soft_cut_minutes: int = 3
    # Forced cut once a session crosses this many hours
    max_session_hours: int = 2
    # Wall-clock interval between check_cuts() ticks
    tick_seconds: int = 30
    # Incremental flush inside an active session: every flush_minutes, the
    # reducer runs over any newly-closed timeline blocks since the last flush
    # and appends a partial entry to event-YYYY-MM-DD.md. The terminal
    # reduce at session-end covers only the trailing window since the last
    # flush. Minimum effective interval is 5 minutes — anything smaller is
    # clamped up, to keep LLM cost bounded. (Timeline blocks themselves are
    # 1-min wide, so a 5-min flush consumes ~5 blocks.)
    flush_minutes: int = 5


@dataclass
class ReducerConfig:
    # Enable S2 session reduction (on session end + daily safety net)
    enabled: bool = True
    # Local wall-clock time for the daily safety-net tick. 23:55 gives the
    # current open session a chance to close on its own but still catches
    # anything unfinished before the date rolls over.
    daily_tick_hour: int = 23
    daily_tick_minute: int = 55


@dataclass
class ClassifierConfig:
    # Legacy classifier cadence when memory_delta.apply_enabled is false.
    # Clamped to >= 5 minutes.
    interval_minutes: int = 30


@dataclass
class PatternDetectorConfig:
    # Enable evidence-backed pattern detection during terminal finalization.
    enabled: bool = True
    # Two modes:
    #   true  = structured SQL filtering first, then LLM validation (saves tokens)
    #   false = feed raw timeline/capture data directly to LLM (burns tokens, may catch more)
    structured_filter: bool = True
    # How many days of event-daily entries to scan for patterns.
    lookback_days: int = 7
    # Minimum occurrences of a pattern to be considered for LLM validation.
    min_occurrences: int = 2


@dataclass
class MemoryDeltaConfig:
    # One LLM reading of each newly flushed session window emits a structured
    # ``memory_delta {entities, assertions, relations, events}`` persisted to
    # the ``memory_deltas`` table before deterministic application mints or
    # reinforces evomem Points and relation Lines.
    enabled: bool = True
    # Upper bound on session timeline blocks fed to the model.
    max_blocks: int = 120
    # Known-identity roster entries injected into the prompt (§4.1 选择题:
    # entities are references into this roster or an explicit new_entity — the
    # LLM never emits bare strings that probe the store).
    roster_max: int = 60
    # Items whose confidence is below this floor are dropped at the
    # deterministic parse gate (§4.1: judgment belongs to the LLM, gating and
    # identity to code).
    min_confidence: float = 0.5
    # §4.2 确定性 apply：把 gated delta 铸成真实点/边（writer/delta_apply.py）。
    # ON 时 memory_delta 成为写侧点/边的生产者（attention 式多头提取取代 classifier
    # 保守摘要分类——修「点层稀」），且 classifier 的铸点在 tick 里短路退役。
    # 2026-07-04 parity-cleared → ON（生产铸点者；classifier 铸点退役）。
    apply_enabled: bool = True
    # ②事实层：assertions 头 → 实体文件事实条目（喂 schema 的料，spec 2026-07-04）。四头
    # 此前只接三头（entities/relations/events），assertions 抽出即弃 → 实体文件只 1 条点、
    # 够不到 schema min_facts=4。随 apply_enabled 翻 ON（parity-cleared 2026-07-04）。
    apply_assertions: bool = True
    # ② 确定性共现 knows：同一 session 每对 person 互相 knows（subsume legacy relation_extractor
    # 的确定性腿）。live LLM 抽共现关系不稳 → delta relations < legacy;补进 payload 保证
    # delta ⊇ legacy、退役无召回损失。默认 ON（这是安全退役的正确行为，非过度生产）。
    cooccurrence_knows: bool = True


@dataclass
class OrphanReaperConfig:
    # §1.5-2 图侧孤儿收敛：长不出实质 6 谓词边的 entity 点（person/org/project/artifact），
    # gmt_created 龄 > ttl_days 即按噪声遗忘（mark_entry_deleted，收据留、可回放）。这是
    # delta apply「过度生产读全场多铸」的收敛腿——敢多铸一次性工作项，因为孤儿会在此被忘。
    # 默认 OFF（随 apply_enabled 一起翻 ON；无 delta 过度生产时也无害）。
    enabled: bool = False
    ttl_days: int = 30
    max_per_night: int = 200
    # 注意力驱逐阈：只有弱地板边（engaged_with max observations < 此值）且无②层结构边、
    # 且到龄的点才驱逐。反复参与（obs≥此值）或长出结构边 = 高注意力 → 留。
    engaged_keep: int = 2


@dataclass
class MemoryDecayConfig:
    # Text-axis graded forgetting.
    # Nightly (23:55 tail) bounded pass: old ∧ never-retrieved ∧ unprotected
    # durable fact entries are distilled per file into a coarser summary
    # (细节链→粗摘要→一行事实) via the existing choke-point verbs (append with
    # decayed:N + abstracted-from provenance, sources strike-retired — receipts
    # stay in markdown). Retrieved memories are IMMUNE (read = reinforcement);
    # conflicted entries wait for human adjudication; decayed:2 is the floor.
    # Default OFF: lossy transform + nightly LLM cost (stage `memory_decay`).
    enabled: bool = False
    after_days: int = 90  # an entry younger than this is never touched
    max_clusters_per_night: int = 3  # ≤ N LLM calls per night, oldest first
    cluster_min: int = 4  # fewer old-weak details than this in a file → skip
    cluster_max: int = 12  # cap per cluster (model-context guard)
    shrink_ceiling: float = 0.5  # summary must be < Σsource_len × this
    line_max_chars: int = 80  # L2 one-liner hard cap


@dataclass
class SkillCheckConfig:
    # Detect skill matches inside the per-minute timeline LLM call.
    enabled: bool = True


@dataclass
class SchemaConfig:
    # New Point/Line evidence triggers a debounced structural refresh; the daily
    # tick remains the unconditional safety pass.
    enabled: bool = True
    refresh_minutes: int = 30
    daily_tick_hour: int = 0
    daily_tick_minute: int = 15
    # After the per-file miner runs, collide
    # "topic-far but behavior-near" stable schemas into higher-level ones via a
    # deterministic behavior pre-filter + LLM judge (no embedding). Runs as the tail
    # of the same schema-tick (no new daemon task). Default ON: the downside is
    # bounded — a low-quality collision gets a low LLM confidence → born ``forming``
    # → excluded from active model reads (only ``stable`` >= min_confidence fusions
    # are visible), and the sweeper prompt is biased to refuse strained merges. The
    # main cost is the per-tick LLM probes, capped by the topic/behavior pre-filter.
    cross_domain_enabled: bool = True
    cross_domain_behavior_max_distance: float = 0.5  # ≤ this == "behavior-near" (pre-filter)
    cross_domain_min_confidence: float = 0.6  # fused schema below this is born ``forming``
    # One bounded LLM compresses active Volume/Face/profile evidence into the
    # single level-3 Root. Default ON:
    # born active, chain-supersedes the prior root, 3 deterministic gates + fail-open
    # (无 root → residency falls back to resident_faces, so default-ON is safe).
    root_synthesis_enabled: bool = True
    root_token_budget: int = 1500  # the always-resident apex hard budget


@dataclass
class EvomemConfig:
    # Snapshot and integrity side channels for evomem write authority.
    #
    # Daily snapshot (§3.2): at the 23:55 daily-safety-net tick, right after the
    # WAL checkpoint, take a `VACUUM INTO backup/evo-YYYYMMDD.db` online snapshot,
    # verify it (§3.3 checks), and apply retention (recent N dailies + recent M
    # weekly Mondays). A snapshot that fails verification ALERTS and never
    # overwrites an existing good one. ON by default — safe-by-construction:
    # read-only against the live DB, failures alert + log, never block the tick.
    snapshot_enabled: bool = True
    snapshot_keep_daily: int = 7  # keep every daily snapshot from the last N days
    snapshot_keep_weekly: int = 4  # additionally keep Monday snapshots from the last N weeks
    # Chain-invariant self-check (§3.3): runs at daemon startup and after each
    # daily snapshot. Checks quick_check / pointer symmetry / anti-fork / head
    # consistency / acyclicity on evo_nodes, plus the projection reconciliation
    # (active-head count vs entries.superseded=0, alert-only). ON by default —
    # alert-only unless freeze_writes_on_failure is set.
    integrity_check_enabled: bool = True
    # Failure handling (§3.3 #7): when a STRUCTURAL check (1–5) fails, freeze
    # the memory write paths (reads stay available) and wait for a human —
    # no automatic recovery. OFF by default ON PURPOSE: under the default
    # markdown write authority entries is still a markdown projection
    # (rebuild_index self-heals it), so freezing the live markdown write paths
    # on a projection-era false positive would halt production for a
    # recoverable condition. Consider flipping on under write_authority=evomem
    # (evo_nodes is the truth there) or to rehearse the freeze path.
    freeze_writes_on_failure: bool = False
    # Shadow dual-write (§4.2 双写影子期, PR-3): after every markdown main write
    # (append/supersede/delete in store/entries.py — the choke point all nine
    # write stations converge on), incrementally mirror the affected entries
    # into evo_nodes via the SAME mapping the PR-2 backfill uses, keeping the
    # backfilled state fresh. ON by default — safe-by-construction:
    # markdown is still the SSOT and the shadow is disposable; failures/skips
    # only log a warning + bump a cumulative miss counter (an `integrity_alert`
    # check=shadow_write_lag fires every N misses, alert-only, never freezes),
    # and NEVER roll back or block the main write. While evo_nodes is
    # empty/missing (backfill not yet run) every shadow write is a warned skip,
    # so enabling before the backfill is harmless — run `persome
    # evomem-backfill` once to start the shadow phase for real; a lagging
    # shadow is always repaired by re-running that idempotent command.
    shadow_write_enabled: bool = True
    # Write authority (§4.4, PR-6b): WHO is the truth on the write side.
    #   "markdown" (default) — status quo, byte-identical to before this flag
    #     existed: every write station lands on the markdown main write paths
    #     in store/entries.py, the PR-3 shadow hook mirrors into evo_nodes, and
    #     markdown stays the SSOT. P0 discipline: the code default NEVER flips
    #     — a human flips the config only after PR-5 (主读) has been stable
    #     ≥1 week (§4.4 顺序纪律).
    #   "evomem" — the inversion: the same write stations are routed (at the
    #     choke-point write verbs they already converge on) through the evomem
    #     engine — evo_nodes is the truth (single-transaction atomic write),
    #     the entries/entry_metadata/entry_temporal tables become
    #     the FTS retrieval projection (superseded = 0 iff is_latest=1 AND
    #     status='active', §1.4/Q7 — maintained synchronously via the SAME
    #     derived-row helpers markdown mode uses), and memory/*.md becomes a
    #     best-effort human-readable projection regenerated per write (failures
    #     warn + count + alert check=markdown_projection_lag, never roll back
    #     the truth write). The PR-3 shadow hook auto-deactivates (its
    #     direction is reversed); event-*.md (Q2) and skills/ subdir files keep
    #     the legacy markdown path. Rollback (§6) = flip back to "markdown":
    #     legacy write paths + shadow hook resume as-is; run `persome
    #     evomem-project-markdown --live --force` first to flush inversion-era
    #     writes into markdown, then `rebuild-index`.
    write_authority: str = "markdown"
    # Nightly semantic-contradiction self-check (memory-rebuild spec §4.4,
    # writer/contradiction_check.py): at the 23:55 harvest, pair same-file live
    # facts deterministically (char-bigram band similarity — same subject,
    # different claim), LLM-judge ≤ contradiction_max_pairs of them, and MARK a
    # contradiction (entry_metadata.conflicted → recall's ⚠(冲突未裁决)
    # down-weight + a memory_contradictions adjudication row for `persome
    # contradictions`) — never auto-supersede: deleting one side of a
    # disagreement is a human verb. Every judged pair is remembered, so cost
    # decays to zero on a stable memory. OFF by default — it spends nightly
    # LLM calls (stage `contradiction_check`, inherits [models.default]).
    contradiction_check_enabled: bool = False
    contradiction_max_pairs: int = 10


@dataclass
class SearchConfig:
    default_top_k: int = 5
    # Hybrid semantic retrieval (BM25 ⊕ dense te3-large → RRF). Default ON, but the daemon only
    # activates it when an embeddings endpoint (OPENAI_*) is configured — otherwise it stays
    # byte-identical BM25 (no vectors written or queried).
    hybrid_enabled: bool = True
    hybrid_recall_n: int = 50  # BM25/dense candidate pool depth before RRF
    hybrid_rrf_k: int = 20  # RRF fusion constant
    # §3.3 associative RRF pool weights (memory-rebuild §7-3, PR #504 finding): the
    # slot contains-pools (entity/scene/window) and the relation graph-expansion
    # pool vote with these weights against the text backbone (bm25+dense, fixed
    # 1.0). 1.0 = legacy equal-weight fusion — the 2026-07-03 production sweep
    # (production_baseline --cutover-sweep, real store, 3 seeds × 200 auto-golden)
    # showed it REGRESSES slotted queries −6.9pp vs hybrid (systematic, all seeds);
    # 0.3 reaches exact parity (mean Δ 0.000) while keeping the slot heads a real
    # voice for genuine 5W1H queries (deterministic golden slot buckets stay 1.0).
    slot_pool_weight: float = 0.3
    relation_pool_weight: float = (
        1.0  # SS7-8 判决：关系探针 7/12 vs 4/12；auto-golden rel 0.0-1.0 逐字节等值
    )
    # §5 production read cutover (memory-rebuild §3.2): query-time consumers (MCP
    # search / chat memory tool / writer tool-loop) route through
    # retrieval.associative.associative_read — zero-LLM Q distillation feeding the
    # multi-head entrance, degrading to search_hybrid on slot-less queries. ON by
    # default per the 2026-07-03 sweep verdict (exact parity at the 0.3 weights);
    # flipping off restores search_hybrid verbatim at every switched site.
    associative_read_enabled: bool = True
    # §7-3 gain unlock（A 步）：结构审计干净的 shadow 边参与关系头遍历（shadow-only
    # 可达名单独成池，×0.5 降权投票——未证明永不盖过已转正）。§7-8 判决后默认开：
    # 关系探针 +25pp 依赖 shadow 通路，auto-golden 回归逐字节等值（零扰动）。
    relation_include_shadow: bool = True
    # §7-10 池内查询感知重排：contains 池候选按与 Q 的 dense 余弦重排（替换
    # per-needle recency 序）；关=回 recency（降级路径）。
    contains_pool_rerank: bool = True
    # 轴A 匹配面 (issue #557): the FTS5 entries table indexes the tags column, so a bare
    # MATCH can also hit classification labels rather than entry text. False =
    # match the content column only
    # ({content}: filter, read-side, zero migration); True = legacy label-matchable.
    tags_matchable: bool = False
    # 轴B 时间衰减 (issue #557): the RRF fusion is rank-only / time-blind — a 3-week-old
    # "0.3.9" fact outranks yesterday's when it matches slightly better. After fusion each
    # candidate's rank score is multiplied by max(floor, 0.5^(age_days/half_life)) and the
    # list re-sorts (membership never changes; anchored at the caller's `until` else the
    # newest candidate — never the wall clock, so results are a pure function of the
    # store; an age-uniform candidate set keeps its order). 0 disables.
    recency_half_life_days: float = 14.0
    recency_decay_floor: float = 0.2
    embed_model: str = "text-embedding-3-large"
    embed_batch_size: int = 64  # vector-embed-tick batch size
    embed_tick_max: int = 512  # max entries embedded per tick (bounds per-tick cost)


@dataclass
class MCPConfig:
    auto_start: bool = True  # run an in-daemon MCP server
    transport: str = (
        "streamable-http"  # "streamable-http" | "sse" (deprecated 2026-04-01) | "stdio"
    )
    host: str = "127.0.0.1"
    port: int = 8742
    # MCP full-power entrance E1/E2 (spec 2026-07-06-mcp-full-power-memory-entrance):
    # per-tool kill-switches (从宽读 — read-only tools default ON, fail-open).
    read_receipt_enabled: bool = True  # ⟨entry_id:path⟩ 收据把手解引用（渐进披露一跳下钻）
    entity_graph_enabled: bool = True  # 图层直读：谓词边 + as-of + 到 USER 链


@dataclass
class UserConfig:
    # The name of the person this instance belongs to.
    # When set, the chat system prompt will tell the model who it is talking to,
    # so questions like "who am I" are answered correctly in first/second person.
    name: str = ""


@dataclass
class MCPServerSpec:
    """One external MCP server the chat agent should connect to as a client."""

    type: Literal["http", "stdio"] = "http"
    url: str = ""  # for type="http" (streamable-http endpoint)
    command: str = ""  # for type="stdio" (executable name)
    args: list[str] = field(default_factory=list)  # extra args for stdio command


@dataclass
class ChatConfig:
    # Chat uses the Anthropic SDK directly for prompt caching, extended thinking,
    # and tool use. Configured under [chat] in config.toml.
    #
    # API key and base URL are NOT in TOML — they come from the env vars
    # ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_BASE_URL``, loaded from the runtime
    # env file or the user's shell.
    #
    # NO routing prefix here. The chat agent calls the Anthropic SDK
    # directly (``anthropic.AsyncAnthropic`` in ``chat/agent.py``); whatever
    # string sits in ``model`` is sent verbatim in the request body and
    # validated by the gateway. DeepSeek's ``/anthropic`` endpoint only
    # accepts bare names (``deepseek-v4-flash`` / ``deepseek-v4-pro``); a
    # routing-style ``anthropic/...`` prefix would be rejected as an unknown
    # model. Prompt caching for the chat agent is enabled via the
    # ``cache_control`` field on message content (see ``chat/agent.py``),
    # independent of the model name.
    model: str = "deepseek-v4-flash"
    # Extended thinking (Anthropic "thinking" block). 0 disables (default —
    # safe for non-reasoning models like deepseek-v4-flash). Set to >=1024
    # to enable; requires a reasoning-capable model (Claude Opus/Sonnet 4.5+,
    # claude-haiku-4+, deepseek-reasoner via /anthropic gateway, etc.).
    # Streamed back to the UI as ``type: reasoning`` SSE frames.
    thinking_budget: int = 0
    # Shell, arbitrary filesystem, and web tools expand Chat beyond the default
    # model-access boundary. They are available only by explicit opt-in.
    unsafe_local_tools_enabled: bool = False
    # MCP client connections: the chat agent connects to these as a client so the
    # model can invoke their tools alongside the built-in tool set.
    mcp_connect_daemon: bool = True  # auto-connect to daemon's own MCP server
    mcp_servers: list[MCPServerSpec] = field(default_factory=list)  # extra servers


@dataclass
class Config:
    models: dict[str, ModelConfig] = field(default_factory=dict)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    timeline: TimelineConfig = field(default_factory=TimelineConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    reducer: ReducerConfig = field(default_factory=ReducerConfig)
    classifier: ClassifierConfig = field(default_factory=ClassifierConfig)
    writer: WriterConfig = field(default_factory=WriterConfig)
    evomem: EvomemConfig = field(default_factory=EvomemConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    mcp: MCPConfig = field(default_factory=MCPConfig)
    pattern_detector: PatternDetectorConfig = field(default_factory=PatternDetectorConfig)
    memory_delta: MemoryDeltaConfig = field(default_factory=MemoryDeltaConfig)
    memory_decay: MemoryDecayConfig = field(default_factory=MemoryDecayConfig)
    orphan_reaper: OrphanReaperConfig = field(default_factory=OrphanReaperConfig)
    skill_check: SkillCheckConfig = field(default_factory=SkillCheckConfig)
    schema: SchemaConfig = field(default_factory=SchemaConfig)
    user: UserConfig = field(default_factory=UserConfig)
    chat: ChatConfig = field(default_factory=ChatConfig)
    # Cross-cutting runtime/model feature flags. Capture privacy controls live
    # under CaptureConfig because capture workers receive that object directly.
    api_require_local_origin: bool = True
    # Entity and reusable-case enrichment inside the shared model build.
    person_graph_enabled: bool = True
    case_extraction_enabled: bool = True
    # Graph-memory P0-2 (#428): deterministic + LLM relation-edge extraction → SHADOW.
    # Default OFF (shadow-first: prove extraction quality before edges can reach retrieval).
    relation_extraction_enabled: bool = False
    # §7-3 转正扇出上限（promotion volume IS dilution volume）；edge-audit 全量 0%
    # 幻觉后默认 10→20（B 步），sweep 复跑护带。
    edge_promote_fanout: int = 20

    def model_for(self, stage: str) -> ModelConfig:
        """Return stage config (already inherited from default at build time)."""
        return self.models.get(stage) or self.models.get("default") or ModelConfig()


# Provider name -> canonical environment-variable prefix.
_PROVIDER_ENV_PREFIX: dict[str, str] = {
    "anthropic": "ANTHROPIC",
    "openai": "OPENAI",
    "deepseek": "DEEPSEEK",
}


def infer_provider(model: str) -> str:
    """Best-effort provider name from an optional ``provider/model`` string.

    Recognises explicit ``provider/model`` prefixes first; falls back to a few
    well-known bare-name heuristics. Returns ``"openai"`` when unknown — the
    compatibility default so legacy ``gpt-*`` configs keep working.
    """
    head = model.split("/", 1)[0].lower() if "/" in model else ""
    if head in _PROVIDER_ENV_PREFIX:
        return head
    lower = model.lower()
    if lower.startswith("claude"):
        return "anthropic"
    if lower.startswith("deepseek"):
        return "deepseek"
    return "openai"


def provider_api_key(provider: str) -> str | None:
    prefix = _PROVIDER_ENV_PREFIX.get(provider)
    if not prefix:
        return None
    return os.environ.get(f"{prefix}_API_KEY")


def provider_base_url(provider: str) -> str | None:
    prefix = _PROVIDER_ENV_PREFIX.get(provider)
    if not prefix:
        return None
    return os.environ.get(f"{prefix}_BASE_URL")


def _as_dict(section: Any) -> dict:
    return section if isinstance(section, dict) else {}


def _build_models(raw: dict) -> dict[str, ModelConfig]:
    # Build default first so stage sections can inherit only its explicitly-set values.
    default_data = _as_dict(raw.get("default", {}))
    default_allowed = {
        k: v for k, v in default_data.items() if k in ModelConfig.__dataclass_fields__
    }
    default = ModelConfig(**default_allowed)
    models = {"default": default}
    for name, section in raw.items():
        if name == "default":
            continue
        data = _as_dict(section)
        allowed = {k: v for k, v in data.items() if k in ModelConfig.__dataclass_fields__}
        models[name] = ModelConfig(**{**default.__dict__, **allowed})
    return models


def _build_dataclass(cls, raw: dict):  # type: ignore[no-untyped-def]
    allowed = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
    return cls(**allowed)


def _build_chat(raw: dict) -> ChatConfig:
    # Secrets/base URLs are env-only. TOML scalars cover model,
    # thinking_budget, and mcp_connect_daemon.
    scalar_fields = {
        k: v for k, v in raw.items() if k in ChatConfig.__dataclass_fields__ and k != "mcp_servers"
    }
    cfg = ChatConfig(**scalar_fields)
    # Parse [[chat.mcp_servers]] array-of-tables
    raw_servers = raw.get("mcp_servers", [])
    if isinstance(raw_servers, list):
        for entry in raw_servers:
            if isinstance(entry, dict):
                allowed = {
                    k: v for k, v in entry.items() if k in MCPServerSpec.__dataclass_fields__
                }
                cfg.mcp_servers.append(MCPServerSpec(**allowed))
    return cfg


def _build_capture(raw: dict) -> CaptureConfig:
    """Build `[capture]`, accepting the former top-level privacy keys."""
    section = dict(_as_dict(raw.get("capture")))
    legacy = {
        "pause_on_lock": "capture_pause_on_lock",
        "suppress_secure_input": "capture_suppress_secure_input",
        "encrypt_screenshots": "capture_encrypt_screenshots",
        "extended_retention_enabled": "capture_extended_retention_enabled",
        "actionable_retention_days": "capture_actionable_retention_days",
    }
    for current, old in legacy.items():
        if current not in section and old in raw:
            section[current] = raw[old]
    return cast(CaptureConfig, _build_dataclass(CaptureConfig, section))


def load(path: Path | None = None) -> Config:
    path = path or paths.config_file()
    raw: dict = {}
    if path.exists():
        with open(path, "rb") as f:
            raw = tomllib.load(f)

    return Config(
        models=_build_models(_as_dict(raw.get("models"))),
        capture=_build_capture(raw),
        timeline=_build_dataclass(TimelineConfig, _as_dict(raw.get("timeline"))),
        session=_build_dataclass(SessionConfig, _as_dict(raw.get("session"))),
        reducer=_build_dataclass(ReducerConfig, _as_dict(raw.get("reducer"))),
        classifier=_build_dataclass(ClassifierConfig, _as_dict(raw.get("classifier"))),
        writer=_build_dataclass(WriterConfig, _as_dict(raw.get("writer"))),
        evomem=_build_dataclass(EvomemConfig, _as_dict(raw.get("evomem"))),
        search=_build_dataclass(SearchConfig, _as_dict(raw.get("search"))),
        mcp=_build_dataclass(MCPConfig, _as_dict(raw.get("mcp"))),
        pattern_detector=_build_dataclass(
            PatternDetectorConfig, _as_dict(raw.get("pattern_detector"))
        ),
        memory_delta=_build_dataclass(MemoryDeltaConfig, _as_dict(raw.get("memory_delta"))),
        memory_decay=_build_dataclass(MemoryDecayConfig, _as_dict(raw.get("memory_decay"))),
        orphan_reaper=_build_dataclass(OrphanReaperConfig, _as_dict(raw.get("orphan_reaper"))),
        skill_check=_build_dataclass(SkillCheckConfig, _as_dict(raw.get("skill_check"))),
        schema=_build_dataclass(SchemaConfig, _as_dict(raw.get("schema"))),
        user=_build_dataclass(UserConfig, _as_dict(raw.get("user"))),
        chat=_build_chat(_as_dict(raw.get("chat"))),
        # Competitive-enhancement flat toggles (spec 2026-06-23): top-level TOML
        # scalars so config.toml can override the safe defaults.
        api_require_local_origin=bool(raw.get("api_require_local_origin", True)),
        person_graph_enabled=bool(raw.get("person_graph_enabled", True)),
        case_extraction_enabled=bool(raw.get("case_extraction_enabled", True)),
        relation_extraction_enabled=bool(raw.get("relation_extraction_enabled", False)),
        edge_promote_fanout=int(raw.get("edge_promote_fanout", 20)),
    )


DEFAULT_CONFIG_TEMPLATE = """# Persome configuration
# All LLM stages call the Anthropic Messages API via the official SDK (same path
# as chat). The backend speaks only the Anthropic protocol — the official
# endpoint or a compatible gateway (e.g. DeepSeek's /anthropic). ``model`` is a
# BARE name (no routing prefix) sent verbatim to ANTHROPIC_BASE_URL; legacy
# ``anthropic/...`` prefixes are tolerated (stripped). Each stage inherits from
# [models.default]. Prompt caching is automatic (cache_control passes through);
# no model-name prefix is needed.
#
# Secrets (API keys, base URLs) are NOT in this file. They live in
# ``~/.persome/env`` (dotenv format). Canonical env var names: {PROVIDER}_API_KEY and
# {PROVIDER}_BASE_URL where {PROVIDER} ∈ {ANTHROPIC, OPENAI, DEEPSEEK}.
# For CLI debugging you may export the same vars in your shell.
#
# Bring-your-own-key model naming:
# - Official Anthropic endpoint: leave ANTHROPIC_BASE_URL unset and use a bare
#   claude model name, e.g. model = "claude-haiku-4-5".
# - Anthropic-compatible gateways (e.g. DeepSeek): set
#   ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic and use the bare name
#   the gateway serves, e.g. model = "deepseek-v4-flash" (the shipped default).
# Verify your setup any time with `persome doctor` (offline, zero LLM calls).

# Cross-cutting runtime/model switches.
api_require_local_origin = true
person_graph_enabled = true
case_extraction_enabled = true
relation_extraction_enabled = false
edge_promote_fanout = 20

[user]
# Your name — tells the chat assistant who it is talking to.
# When set, questions like "who am I" or "who is <your name>" will be answered
# in second person rather than as a third-party lookup.
# name = "Alice"

[models.default]
model = "deepseek-v4-flash"   # bare name, sent verbatim to ANTHROPIC_BASE_URL gateway

[models.compact]
# Accuracy-sensitive — match or exceed the default.

[models.timeline]
# 1-minute activity normalisation (verbatim-preserving). The reducer,
# which runs every flush_minutes ≥ 5m, is the stage that does real
# compression — timeline only cleans up, de-duplicates, and separates
# independent conversations. A fast/cheap model is strongly recommended:
# the prompt is short, the output is a bounded JSON list, and timeline
# runs on every 1-min window — slow LLM calls here cause the pipeline to
# lag behind real time and delay all downstream memory updates.
# Example override (uncomment): a faster model just for this hot path.
# model = "deepseek-v4-flash"   # bare name; routed to ANTHROPIC_BASE_URL

[models.reducer]
# Session-level S2 reduce-from-blocks. Prompt is short (blocks are already
# compressed) but output quality matters — consider a stronger model here.

[models.classifier]
# Extracts classifiable long-term facts from the day's event-daily entries
# into user-/project-/topic-/tool-/person-/org- files via tool calls.
# Accuracy-sensitive — pick a capable model.

[capture]
source = "daemon"             # "daemon" (daemon owns OS capture) | "ingest" (Swift app pushes captures via POST /captures/ingest; daemon needs no OS permission)
event_driven = true           # capture on window/app/typing events via mac-ax-watcher (source="daemon" only)
heartbeat_minutes = 10        # periodic capture even when nothing happens
debounce_seconds = 3.0        # for AXValueChanged bursts
min_capture_gap_seconds = 2.0 # minimum gap between consecutive captures
dedup_interval_seconds = 1.0  # per-event-type dedup window
same_window_dedup_seconds = 5.0  # don't re-capture the same bundle+window unless 5s have passed (or it's a focus change)
buffer_retention_hours = 168           # 7 days; stale absorbed captures past this are deleted
screenshot_retention_hours = 24        # after 24h, strip screenshot (77% of bytes) but keep AX+text
screenshot_thumbnail_hours = 0         # 像素轴分级遗忘 (memory-rebuild §2.1): >N 小时的截图原位降采样为 ≤480px 缩略图（全分辨率→缩略→仅存文本化→删除的中间档）；0=关（默认，字节等价旧行为）；须 < screenshot_retention_hours 才有意义
buffer_max_mb = 2000                   # hard ceiling; oldest absorbed files evicted first (0 to disable)
include_screenshot = true
screenshot_max_width = 1920
screenshot_jpeg_quality = 80
ax_depth = 100                # Electron apps (Claude Desktop, VS Code, Slack) have deep DOM; 8 only reaches the chrome
ax_timeout_seconds = 3
# OCR fallback for apps that block Accessibility API (WeChat, Feishu, NetEase Music, etc.)
# On-device PP-OCRv6 — the focused-window screenshot is OCR'd locally; nothing leaves the machine.
enable_ocr_fallback = false   # local inference; no network, no API token
# Inference runs in an isolated local worker, so a native Paddle crash does not
# kill the daemon. PERSOME_DISABLE_OCR=1 is the deployment kill switch.
ocr_tier = "tiny"             # tiny (default) | small | medium — local PP-OCRv6 weights
ocr_min_gap_seconds = 15.0    # minimum seconds between OCR runs for the same window
ocr_structured = true              # geometry-structure raw OCR (zero LLM, on-device): columns/regions + per-app field labels
# cmux signal source: real terminal text via cmux's local unix-socket RPC (GPU-rendered
# terminals expose ~no AX text). Read-only, zero external cost, sub-second deadline,
# silent degrade when cmux isn't running — hence default on.
cmux_source_enabled = true
pause_on_lock = true
suppress_secure_input = true
encrypt_screenshots = true       # install.sh provisions PERSOME_SCREENSHOT_KEY; no key -> pixels omitted
extended_retention_enabled = true
actionable_retention_days = 7

[timeline]
window_minutes = 1             # length of each aggregator block (verbatim-preserving normalizer)
cold_lookback_minutes = 30
max_parallel_windows = 4       # parallel LLM workers for backlog catchup (1 = sequential)

[writer]
soft_limit_tokens = 20000
context_token_limit = 80000      # trim message history when estimated tokens exceed this
llm_retry_attempts = 6           # retry LLM call this many times on transient failure
consolidation_cadence = 8        # run pending per-file compaction every N completed sessions

[session]
gap_minutes = 5            # hard cut: idle > 5 min ends the session
soft_cut_minutes = 3       # soft cut: single unrelated app > 3 min
max_session_hours = 2      # forced cut at 2h
tick_seconds = 30          # check_cuts() interval
flush_minutes = 5          # incremental reduce tick inside active sessions (min 5)

[reducer]
enabled = true             # run S2 reducer on session end + daily safety net
daily_tick_hour = 23       # local-time hour for the daily safety-net tick
daily_tick_minute = 55     # local-time minute for the daily safety-net tick

[classifier]
interval_minutes = 30      # durable-fact extraction cadence inside active sessions (min 5)

[pattern_detector]
enabled = true             # detect repeated evidence-backed behavior after session finalization
structured_filter = true   # true = SQL candidate filter first (save tokens); false = raw data to LLM (burn tokens, may catch more)
lookback_days = 7          # scan this many days of event-daily for patterns
min_occurrences = 2        # minimum repetitions to flag as a candidate

[memory_delta]
enabled = true             # one evidence-gated structured extraction per newly flushed session window
max_blocks = 120
roster_max = 60
min_confidence = 0.5
apply_enabled = true       # deterministic Point/Line production after persist
apply_assertions = true
cooccurrence_knows = true

[evomem]
# evomem SSOT switch — survivability base (snapshots + chain self-check).
# All side-channel: with these off, the daemon behaves exactly as before.
snapshot_enabled = true            # daily VACUUM INTO backup/evo-YYYYMMDD.db at the 23:55 tick (after the WAL checkpoint); bad snapshots alert instead of overwriting good ones
snapshot_keep_daily = 7            # keep every daily snapshot from the last N days
snapshot_keep_weekly = 4           # additionally keep Monday snapshots from the last N weeks
integrity_check_enabled = true     # chain-invariant self-check at daemon startup + after each snapshot; failures are structured error logs
freeze_writes_on_failure = false   # when a STRUCTURAL check fails, freeze memory write paths (reads stay available) until a human decides; off = alert-only by default
shadow_write_enabled = true        # PR-3 双写影子期: every markdown main write also shadow-writes the affected entries into evo_nodes (backfill 单条版); failures/skips warn + count only, NEVER touch the main write — run `persome evomem-backfill` once before the shadow phase starts for real
write_authority = "markdown"       # PR-6b 写权反转开关 (§4.4): "markdown" (default) = status quo, markdown is the SSOT + shadow dual-write into evo_nodes; "evomem" = the inversion — engine writes evo_nodes as truth, FTS tables become the retrieval projection, memory/*.md becomes a best-effort human-readable projection (manual edits are overwritten — use `persome evomem-import-markdown <file>`), the shadow hook auto-deactivates, event-*.md stays legacy (Q2). KEEP "markdown" until PR-5 主读 has been stable ≥1 week; a human flips this — never the code default. Rollback = flip back (legacy paths + shadow resume; project --live --force + rebuild-index first to flush)
contradiction_check_enabled = false # 夜间语义矛盾自检 (§4.4): 23:55 收割时对同文件 live 事实做确定性配对 + LLM 判互斥；命中只「标记」——entry_metadata.conflicted（recall 渲染 ⚠(冲突未裁决) 降权）+ memory_contradictions 人裁队列（`persome contradictions`），绝不自动 SUPERSEDE。每晚 ≤ contradiction_max_pairs 次 LLM 调用，已判对永不重判
contradiction_max_pairs = 10       # 每晚判定的候选对上限（按相似度最强优先）

[search]
default_top_k = 5
slot_pool_weight = 0.3             # 联想入口槽池（实体/场景/时窗）RRF 投票权重；文本骨干恒 1.0；1.0=旧版平权（生产扫描判决：1.0 系统性回退 −6.9pp，0.3 精确过带）
relation_include_shadow = true     # §7-3 关系头喂食：审计干净的 shadow 边 ×0.5 降权参与遍历（§7-8 判决默认开：探针 +25pp 依赖它，回归零扰动）
contains_pool_rerank = true        # §7-10 池内 dense 重排（残余探针杠杆）；关=回 recency 序
relation_pool_weight = 1.0         # 关系图扩展池权重（SS7-8 调优判决：真库关系探针 7/12 vs 文本基线 4/12；auto-golden 回归 rel 0.0-1.0 逐字节等值=对普通查询免费）
associative_read_enabled = true    # §5 读路 cutover：查询期消费方（MCP/chat/writer 工具）走联想入口（无槽退化 hybrid）；false=全部回 search_hybrid（kill-switch）
tags_matchable = false             # 轴A (#557)：BM25 只匹配 content 列——分类标签词表（#intent/#kind:meeting/schema/fact…）不再被当正文命中；true=旧行为（kill-switch）
recency_half_life_days = 14.0      # 轴B (#557)：融合后按条目年龄做半衰期衰减重排（rank 分 × max(floor, 0.5^(age/半衰期))）；锚= until 或候选集内最新时间戳（非墙钟，纯确定性）；0 = 关（字节等价旧版）
recency_decay_floor = 0.2          # 衰减下限：老而最相关的持久事实不至于被新噪声淹没；全老候选集因子一致=顺序不变

[mcp]
auto_start = true                 # run an always-on MCP server inside the daemon
read_receipt_enabled = true       # E1.3 收据把手：⟨entry_id⟩ → 条目全文 + capture breadcrumbs（关=不注册该工具）
entity_graph_enabled = true       # E2 图层直读：entity_graph(name, depth, as_of, include_shadow)（关=不注册该工具）
transport = "streamable-http"     # "streamable-http" | "sse" (deprecated 2026-04-01) | "stdio"
host = "127.0.0.1"                # bind address; keep localhost-only by default
port = 8742

[chat]
# Anthropic SDK-based chat assistant. Set ANTHROPIC_API_KEY (and optionally
# ANTHROPIC_BASE_URL to point at e.g. DeepSeek's /anthropic gateway) via
# the env file next to config.toml, or export them in your shell for CLI debugging.
# model = "deepseek-v4-flash"      # bare name sent through the Anthropic SDK
# thinking_budget = 0
unsafe_local_tools_enabled = false # opt in to shell, arbitrary filesystem, and web tools
mcp_connect_daemon = true         # auto-connect to the daemon's own MCP server

# Add extra MCP servers the chat agent should connect to:
# [[chat.mcp_servers]]
# type = "http"
# url = "http://127.0.0.1:9000/mcp"
#
# [[chat.mcp_servers]]
# type = "stdio"
# command = "mcp-filesystem"
# args = ["--root", "/Users/me/projects"]

[skill_check]
enabled = true                    # detect skill matches inside the per-minute timeline LLM call


[schema]
enabled = true                    # induce predictive schema-*.md priors from durable facts
refresh_minutes = 30             # after new Point/Line evidence, refresh Face/Volume/Root at this cadence (min 5)
daily_tick_hour = 0               # local-time hour for the daily schema tick (after safety-net 23:55)
daily_tick_minute = 15            # local-time minute for the daily schema tick
cross_domain_enabled = true       # Hy-Memory cross-domain sweeper: collide topic-far/behavior-near schemas (no embedding); low-quality fusions stay forming, only stable ones enter active model reads
cross_domain_behavior_max_distance = 0.5  # behavior-distance ceiling for the deterministic pre-filter (≤ == behavior-near)
cross_domain_min_confidence = 0.6 # fused cross-domain schema below this confidence is born forming (not injected)
root_synthesis_enabled = true      # synthesize at most one active Root
root_token_budget = 1500

"""


def write_default_if_missing(path: Path | None = None) -> bool:
    path = path or paths.config_file()
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_CONFIG_TEMPLATE)
    return True

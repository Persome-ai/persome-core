"""TOML config loader with defaults and per-stage LLM resolution."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from . import paths


@dataclass
class ModelConfig:
    model: str = "deepseek-v4-flash"
    # Optional override; when empty, ``provider_base_url(model)`` falls back to
    # the canonical ``{PROVIDER}_BASE_URL`` env var (managed by Mens.app).
    base_url: str = ""
    max_tokens: int | None = None


@dataclass
class CaptureConfig:
    # Where capture inputs (AX tree + screenshot) come from:
    #   "daemon" — the daemon owns OS capture (spawns mac-ax-watcher/mac-ax-helper,
    #              grabs screenshots in-process). Legacy default, byte-identical.
    #   "ingest" — the Swift "Persome" app owns OS capture and pushes pre-built payloads
    #              via POST /captures/ingest; the daemon runs no watcher / no OS grab
    #              and needs NO Accessibility / Screen-Recording permission.
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
    # Collect local-only OCR training samples (geometry + structured result, NEVER the
    # screenshot) for future UI-region-model training. Local only; no upload path exists.
    ocr_collect_training_data: bool = True
    # Deprecated cloud-OCR fields, kept only so old config.toml still decodes.
    ocr_api_token: str = ""
    ocr_model: str = "PP-OCRv5"
    # cmux signal source (issue #558): when the frontmost app is cmux, read
    # the visible terminal text over its local unix-socket RPC and append it
    # to visible_text. Safe-by-construction: read-only local socket, zero
    # external cost, sub-second deadline, silent degrade on any failure.
    cmux_source_enabled: bool = True


@dataclass
class TimelineConfig:
    # Wall-clock window length for each aggregator block. 1-min blocks
    # keep timeline entries close to verbatim — the reducer (which runs
    # every flush_minutes ≥5m) is the stage that does real compression.
    window_minutes: int = 1
    cold_lookback_minutes: int = 30
    # Wall-clock horizon of blocks kept warm for tooling / context.
    # 720 × 1-min ≈ 12h.
    recent_context_blocks: int = 720
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
    hard_limit_tokens: int = 50_000
    dedup_window_hours: int = 24
    cold_start_conservative_hours: int = 0
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
    # Trigger an offline consolidation every N completed (classified) sessions.
    # Placeholder today: runs per-file compaction over files flagged
    # ``needs_compact``. PRD-4 will replace this with cross-file consolidation.
    consolidation_cadence: int = 8
    # Offline consolidation (writer/consolidator.py) — cross-file dedup +
    # synthesis driven by an LLM tool-call loop. Triggered manually or by
    # the daemon; never inline with classifier.
    consolidation_max_region_size: int = 50
    consolidation_max_iterations: int = 15
    consolidation_stage: str = "consolidator"


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
    # How often to fire the classifier inside an active session. The
    # terminal classifier still runs at session end over any trailing
    # window that this tick hasn't covered. Clamped to >= 5 minutes.
    interval_minutes: int = 30


@dataclass
class PatternDetectorConfig:
    # Enable pattern detection stage after classifier.
    enabled: bool = True
    # Two modes:
    #   true  = structured SQL filtering first, then LLM validation (saves tokens)
    #   false = feed raw timeline/capture data directly to LLM (burns tokens, may catch more)
    structured_filter: bool = True
    # How many days of event-daily entries to scan for patterns.
    lookback_days: int = 7
    # Minimum occurrences of a pattern to be considered for LLM validation.
    min_occurrences: int = 2
    # Confidence threshold (0.0-1.0) for auto-accepting LLM-validated patterns.
    confidence_threshold: float = 0.7


@dataclass
class MemoryDeltaConfig:
    # Session-end memory_delta consolidator channel (Memory-rebuild Phase 0,
    # spec docs/superpowers/specs/2026-07-02-memory-rebuild-design.md §4.1/§6.2).
    # ONE LLM reading of the just-ended session emits a structured
    # ``memory_delta {entities, assertions, relations, events}`` persisted to
    # the ``memory_deltas`` table with status='shadow' — nothing downstream
    # consumes it yet except the parity report (``persome delta-report``) and the
    # Phase-1 dual-run eval that must reach parity before the four scattered
    # extractors (person name-source / relation LLM pass / case extraction /
    # classifier attribution) retire.
    # 2026-07-04: **parity-cleared** → 翻 ON（生产路）。对拍 delta≥legacy 全头过（确定性档 +
    # 4×--real，relations 靠确定性共现 knows 补齐无召回损失，spec §6.2 Phase-1「平价退役」）。
    enabled: bool = True
    # Upper bound on session timeline blocks fed (model-context guard, mirrors
    # the recognizer's max_blocks).
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
    # Text-axis graded forgetting (memory-rebuild §1.5-5; spec
    # docs/superpowers/specs/2026-07-03-text-axis-graded-forgetting-design.md).
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
class IntentRecognizerConfig:
    # Session-level trajectory intent recognizer (R1).
    # Reads the WHOLE active session as an ordered event log + a marked focus
    # block + recall background, so intents that span minutes (a time proposed
    # in one minute, agreed in the next) get stitched into one. Since R4 (#544)
    # it is the sole intent producer — timeline's per-minute tagging is retired.
    # Trigger: the recognizer fires on each timeline block flush (wired via the
    # timeline task's on_blocks_produced hook), not on a fixed interval.
    max_blocks: int = 200  # upper bound on session blocks fed (model-context guard)
    # Cross-session backdrop: the most recent N timeline blocks from BEFORE the
    # current session, fed as a lower-authority history layer so a commitment
    # split across a session cut (5-min idle gap) can still be stitched. 0 to
    # disable the history layer.
    timeline_history_blocks: int = 20
    # Generic fallback parse for apps WITHOUT a registered per-app parser (#whitelist-minimization):
    # instead of the ``no_parser`` app-whitelist drop, build a best-effort ParsedConversation from the
    # capture's ``visible_text`` (recent lines as ``direction="unknown"`` messages — no fabricated
    # sender/direction) and let the LLM judge. Removes the implicit "only Feishu/WeChat/browser" app
    # whitelist so any IM (Slack/Telegram/钉钉/…) is covered. Noise/cost from non-chat apps
    # (terminals/editors) is bounded by the SAME downstream gates (seen-set/cold-start) + the per-app
    # exponential backoff (a 0-intent app self-cools) + the precision-first LLM. Kill-switch to false
    # restores the app-whitelist drop. See docs/superpowers/specs/2026-06-28-no-parser-removal-*.md.
    fast_generic_fallback: bool = True
    # Stream the fast-path recognizer's LLM call and fire on the first intent's required fields
    # (everything before the display-only ``rationale``, reordered last) instead of waiting for the
    # whole JSON body.
    #
    # DEFAULT OFF — the speculative early-fire is NOT worth its risk at the current payoff:
    #   • measured win is only ~0.2s (the recognizer's output is short — ~220 chars stream in ~0.5s —
    #     so TTFT dominates, not the body). And streaming WITHOUT early-fire is no faster than the
    #     blocking call (``_safe_json`` still needs the full text), so the whole streaming path's value
    #     rests on that ~0.2s.
    #   • risk: if the model emits ``rationale`` NOT last (occasional LLM mis-ordering), the early
    #     prefix is missing the fields after it (e.g. ``when_text``), so the early intent's dedup_key
    #     differs from the final parse's → it does NOT fold → a SECOND row + a SECOND notification.
    #     A truly mis-order-safe early-fire would have to wait for the first intent's closing brace
    #     (incl. rationale) — which erases the latency win.
    # Kept behind this flag (off) until a dedup-key-safe early-fire is worth building. The real daemon
    # latency win is ``fast_disable_thinking`` (~3s), independent of this. true → streaming early-fire.
    fast_streaming: bool = False
    # Disable the model's extended "thinking" for the fast recognizer. The lean per-arrival judge
    # doesn't need chain-of-thought, and DeepSeek's thinking is ~half the call's wall time (measured
    # TTFT 3.3s→1.25s with thinking off). Quality-affecting (chain-of-thought can lift precision), so
    # it is gated + must be validated against the intent-golden LLM eval before trusting it on. The
    # ``thinking:{type:disabled}`` param rides the request body, which the relay forwards verbatim.
    # Default ON: validated quality-safe by the intent-golden LLM A/B (thinking ON→OFF: fast recall
    # 0.9545→0.9545 unchanged, fast precision 0.78→0.81 slightly up, slow P/R 1.0 unchanged, 0 form
    # violations — all thresholds passed). Saves ~half the call's wall time (TTFT 3.3s→1.25s). Flip to
    # false to restore chain-of-thought.
    fast_disable_thinking: bool = True
    # Model override for the fast path. Empty string inherits the
    # ``intent_recognizer`` stage model. Set a faster/cheaper model here to keep
    # the per-arrival call cheap without changing the slow recognizer's model.
    fast_model: str = ""
    # --- Fast-path throttling (recognition-side, distinct from capture's gaps) ---
    # Minimum seconds between two fast-path passes for the SAME app (bundle id).
    # This is a *recognition* rate limit, separate from capture's
    # ``min_capture_gap_seconds``: even when content genuinely changes, we don't
    # re-run the LLM for an app more than once per this window. Keep small so two
    # quick real messages aren't both dropped. 0 disables per-app throttling.
    per_app_min_interval: float = 2.0
    # ``periodic`` (heartbeat / timer) captures are the lowest-signal trigger, so
    # they get a much longer per-app interval than user-driven focus/input/value
    # changes. Applies on top of ``per_app_min_interval`` for periodic triggers.
    per_app_periodic_interval: float = 30.0
    # Per-app exponential backoff: after this many CONSECUTIVE fast-path passes
    # that reached the LLM but yielded 0 intents, the app is skipped for a
    # growing cool-off (``backoff_base_seconds`` doubled per extra miss, capped at
    # ``backoff_max_seconds``). Any pass that yields ≥1 intent resets the counter.
    # 0 disables backoff. This is the main lever against the "white-burn" misses.
    #
    # Softened for the anchor-gate removal (option B, spec 2026-06-26): the lexical
    # pre-gate is gone, so EVERY new arrival reaches the LLM and a chatty thread
    # makes consecutive_miss climb fast. At the measured volume (~26 LLM calls/day)
    # white-burn cost is negligible, so backoff biases to RECALL — fire later (6),
    # recover fast (≤120s) — a real scheduling message after brief chatter must not
    # be skipped for minutes. Calibrate against live fast_path_ticks post-deploy.
    backoff_max_misses: int = 6
    backoff_base_seconds: float = 20.0
    backoff_max_seconds: float = 120.0
    # Per-domain allowlist for browser (K2) captures: when non-empty, only URLs
    # whose host is in this list pass the fast path. Empty = allow all. K1 (chat
    # apps with no per-capture URL) is unaffected — this is scaffolding for K2.
    domain_allowlist: list[str] = field(default_factory=list)
    # --- Hy-Memory migration P0 (flags default off = byte-identical to today) ---
    # Inject inferred user-inertia priors (schema-*.md, D2 layer) as the highest-
    # priority recall section. Off until the schema layer exists; the seam keeps
    # the recognizer call site stable so D2 only fills in the extraction.
    schema_prior_enabled: bool = True
    # R4 schema-level feedback loop: HUD dismiss/accept on an intent flows back
    # onto the schemas whose inferences were injected when it was recognized
    # (``Intent.schema_sources`` provenance) — deterministic confidence deltas
    # (dismiss −0.05 / consume +0.03), stable↔forming flip on the 0.6 threshold,
    # landed through the same ``supersede_entry`` seam the miner uses. ON by
    # default — safe-by-construction: intents without schema_sources are a
    # strict no-op, the write is best-effort (never blocks the status
    # write-back), and a wrongly-decayed schema is recoverable (consumed raises
    # it back; the next re-mine re-judges from facts).
    schema_feedback_enabled: bool = True
    # Reverse-loop G5.1 (spec 2026-06-26 §3.3): record one content-free
    # ``intent_fold_ticks`` row whenever the sink FOLDS a re-recognized intent
    # onto an existing one (instead of inserting) — so "the same thing keeps
    # getting re-recognized N×/session" becomes a measurable signal for tuning the
    # content-fold threshold. Telemetry only, best-effort, never perturbs the fold
    # decision; kill-switch turns the emit off (the fold behaviour is unchanged).
    intent_fold_telemetry_enabled: bool = True
    # G5.2 (spec 2026-06-26 §3.3): when a dormant ``armed`` event-intent is TTL-reaped
    # (never fired in 14 days), flow its source schemas a false-positive negative
    # signal (same ``apply_intent_feedback`` seam as a HUD dismiss) instead of a
    # silent dismiss — the schema that predicted a trigger that never happened
    # should lose confidence. Distinct from the kind-level ``_dismissed_prior``
    # (deliberately NOT trained on TTL reaps); best-effort, only acts when the
    # reaped row has ``schema_sources``. Kill-switch restores the silent reap.
    armed_reap_schema_feedback_enabled: bool = True
    # Few-shot positive taste prior: inject the user's recent POSITIVE feedback
    # (accepted/completed/manual_baseline from ~/.persome/logs/context-feedback.jsonl)
    # as a "what this user acts on" preamble in the recognizer user prompt — the
    # cheap per-user personalization win (the thing per-user fine-tuning would
    # buy, ~free). Positive-only on purpose: dismiss is already double-covered by
    # ``_dismissed_prior`` (R3) + the #533 hard cooldown gate, so re-adding it
    # here would triple-count AND re-tread the documented-weak prompt-soft path
    # #533 replaced (config.py:448). Fail-open (missing file / no positive rows
    # → no preamble, recognition unchanged). See ``intent/taste_profile.py``.
    taste_profile_enabled: bool = True
    taste_profile_days: int = 21  # lookback window for the positive prior
    taste_profile_max_items: int = 6  # newest N items after content-dedup
    # Shared cacheable user-profile prefix: compose schema_prior + taste into ONE
    # stable block rendered in the CACHED prompt prefix of BOTH recognizers —
    # memory for the fast path (which had none) + an elegant volatility-ordered
    # reorder for the slow path (schema_prior leaves the shared recall budget,
    # freeing it for episodic recall). ON by default. OFF = byte-identical
    # fallback: the slow path puts schema_prior back into ``assemble_background``
    # and taste back in the volatile body (the pre-profile layout); the fast path
    # sends its plain system string with no profile. See ``intent/profile.py``.
    user_profile_enabled: bool = True
    # Fold each recall hit to its evolution-chain head, read from ``evo_nodes``
    # (is_latest=1 AND status='active', scope=default per Q4) — THE chain-head
    # fold (SSOT switch §1.4). PR-7 retired the entry_chain derived index along
    # with the ``recall_use_chain_index`` / ``recall_read_evo_nodes`` staging
    # flags: evo_nodes is the only chain store. event-* entries (Q2: never in
    # evo_nodes) keep the superseded-column judgment; while evo_nodes is
    # missing/empty (fresh install pre-backfill) the fold degrades to the
    # equivalent ``superseded = 0`` derived-column guard. False = legacy
    # un-folded recall (returns superseded versions too).
    recall_fold_superseded: bool = True
    # --- 演化链轨迹 (separate flag) ---
    # Surface the evolution trajectory in recall: when a hit lands on a chain
    # head with superseded ancestors, append a compact ``← [曾]``/``← [精炼自]``
    # trail (latest→oldest, rendered from the evo_nodes bidirectional pointers)
    # so the recognizer sees态度演变, not just the latest belief. Decoupled from
    # the fold ON PURPOSE: the trail only renders when BOTH this and
    # ``recall_fold_superseded`` are on (and evo_nodes is ready); the fold alone
    # stays a pure, equivalence-preserving fold.
    recall_chain_trail: bool = True
    # --- Hy-Memory migration 元认知标签 (meta-cognition layer — separate flag) ---
    # Annotate recall hits with a reliability note (``⚠(低置信)`` / ``⚠(冲突未裁决)``)
    # read from the ``entry_metadata`` index, so the recognizer can down-weight a
    # low-confidence or conflicted memory instead of treating every fact as equally
    # certain — the asymmetric-cost lever (a guess must not drive a proactive action
    # a hard fact would). Default ON for everyone: it is safe-by-construction —
    # existing memories carry no confidence tag (no entry_metadata row → no
    # annotation), so it only ever affects NEW classifier-written memories, and its
    # only effect is to make the recognizer MORE cautious on shaky ones (never more
    # eager). Flip to false to fully suppress the recall annotation.
    recall_include_confidence: bool = True
    # --- R3 慢路增量化: cost gate on the slow path's full-session re-read ------
    # The slow recognizer re-reads the WHOLE active session on every block flush
    # (~60s), so the prompt grows linearly with session length (2h cap ≈ 120
    # blocks) and the same bytes are re-sent 100+ times. When the session's
    # event-log exceeds this many blocks, only the most recent N render verbatim;
    # older blocks collapse into ONE deterministic one-line header (pure string
    # assembly, no LLM: 「更早 X 个 block（HH:MM–HH:MM）已省略，涉及 app：…」).
    # Default 60 (≈1h of blocks) — safe-by-construction: a block is only folded
    # after ~N consecutive flushes fed it verbatim, its recognized intents are
    # already persisted (dedup'd) and re-surface through recall's scene layer,
    # so the fold drops only bytes the pipeline has already exhausted. Set 0 to
    # disable (today's unbounded behavior, byte-identical prompt).
    slow_path_max_blocks: int = 60
    # --- #547 慢路锚定 pre-gate -------------------------------------------------
    # The slow path fires on EVERY block flush but only ~14% of ticks recognize
    # anything — most minutes carry no schedulable signal at all. When on, the
    # recognizer scans the blocks NEW since the last tick (entries +
    # focus_structured/focus_excerpt text) with the slow-anchor regex
    # (``event_source.SLOW_ANCHOR_RE`` — the fast ``_ANCHOR_RE`` plus
    # euphemism/willingness cues, one composed source) and skips the LLM call
    # when none hits. Old blocks were covered by the previous tick (LLM-read or
    # gate-proven anchorless), so the miss risk is bounded and arguable (漏 =
    # 有限损失). Skipped ticks are still recorded in ``recognition_ticks``
    # (outcome=skipped_no_anchor) without polluting hit_rate.
    #
    # DEFAULT FLIPPED TO False (gate OFF): on real data the lexical gate skipped
    # only ~12% of ticks while starving info_need/reminder/assignment (recall
    # 6-33%) — they were recognized only ~26min late, when an unrelated anchor
    # finally forced a full pass. Running the recognizer on EVERY block-flush tick
    # costs only ~+12% calls (the gate barely saved) and removes that latency. The
    # output cost of the now-more-frequent empty calls is bounded by the prompt
    # itself: the recognizer no longer emits the (unused) scene_state, so a
    # no-intent tick returns just `{"intents": []}` (~a few tokens). Input is the
    # dominant cost and is largely prompt-cached. Flip back to True (+ pick
    # pregate_mode below) to re-enable the cost gate.
    slow_pregate: bool = False
    # --- pre-gate discriminator: regex (default/baseline) | bge | hybrid --------
    # The slow pre-gate above is a lexical SLOW_ANCHOR_RE cost gate: it catches
    # meeting/commitment cues well but starves info_need / search / un-clocked
    # reminder / assignment (measured recall info_need ~17-62%, reminder ~6-17%),
    # so those intents only get recognized when an unrelated anchor later forces a
    # full-session pass — the latency users feel. ``pregate_mode`` swaps the
    # per-block discriminator WITHOUT touching the rest of the gate logic:
    #   "regex"  — block_has_anchor (SLOW_ANCHOR_RE). The lexical baseline. DEFAULT.
    #   "bge"    — an OPTIONAL bge-small-zh ONNX encoder + a tiny trained head scores
    #              P(block worth an LLM call) >= pregate_bge_threshold. Fail-open:
    #              falls back to "regex" whenever the model/onnxruntime is absent.
    #   "hybrid" — regex OR bge (union: keep the regex hits, add bge recall).
    # The bge model is NO LONGER bundled (dropped to keep the repo/deps lean). To use
    # "bge"/"hybrid", `pip install onnxruntime tokenizers` and set pregate_bge_model_dir
    # to your own exported model dir; otherwise both silently degrade to "regex".
    # (When it WAS bundled, the pregate A/B oracle showed hybrid lifted slow-lane recall
    # 30%→60% over regex — that gain now requires a BYO model.)
    pregate_mode: str = "regex"
    pregate_bge_threshold: float = 0.5
    pregate_bge_model_dir: str = (
        ""  # empty = disabled (no bundled model); set to a BYO exported dir to enable
    )
    # --- 慢路触发：统一入口 + LLM 路由（不再每 tick 都跑慢路）-------------------
    # `every_block` = 旧行为：每个出块 block-flush tick 都跑 recognize_session（贵）。
    # `escalation`  = 统一入口：快路 LLM 是唯一决策点——它在每条 capture 上判断
    #   `needs_trajectory`（这条只看当前会话/单条够不够 vs 需要跨窗口/跨会话轨迹），
    #   true 时给当前会话打升级标记（intent/escalation.py）；block-flush hook 只对
    #   被升级的会话跑慢路。这样"要不要进慢路"由 LLM 决定,而不是定时/锚点。
    #   召回兜底：被标记 OR 距上次慢路 ≥ `slow_fallback_minutes` 才跑；#621 finalize
    #   终局扫仍在(会话结束必补一次)。回退=设 `every_block`。
    slow_trigger: str = "escalation"
    slow_fallback_minutes: float = 10.0  # 长兜底：未被升级的会话也每 N 分钟跑一次;0=纯靠升级
    # --- #576 跨 tick 缝合例外: dangling-anchor stitch --------------------------
    # The #547 skip drops the "问句 → 隔 tick 确认" shape: tick N reads an
    # anchored *question* ("周五开会吗?") and recognizes nothing; tick N+1's only
    # new block is a bare confirmation ("行") with no anchor of its own → the
    # gate skips and the question (now an OLD block) is never re-read → the
    # commitment is never stitched. When a NEW block carries a bare confirmation
    # cue AND one of the last N OLD blocks carries a slow anchor, the pre-gate
    # does NOT skip — it re-runs the LLM so the pair can be stitched. The
    # two-signal AND + this bounded look-back keep it surgical (a bare "好" alone
    # never un-skips; bare confirmations stay OUT of SLOW_ANCHOR_RE because they
    # are too high-frequency to be a primary anchor) so the steady-state skip
    # rate is preserved. 0 disables the exception (pure #547 behavior).
    slow_pregate_stitch_lookback_blocks: int = 5
    # --- #621 终局识别 pass (session finalization) ------------------------------
    # The slow recognizer only ever runs against the ACTIVE session (block-flush
    # hook → current_snapshot). The timeline tick lags the session-window close
    # (~60s), so when a session is hard-cut its trailing blocks materialise AFTER
    # the cut — and current_snapshot is now None or the NEXT session, so those
    # tail blocks never get a recognition pass for the session they belong to. A
    # session whose only schedulable signal lived in those tail blocks therefore
    # gets ZERO recognition for its whole lifetime (~8.3% of block-producing
    # sessions in the field). When on, the block-flush hook ALSO sweeps recently-
    # ended sessions that were never finalized and runs one terminal
    # recognize_session over each — gated by the SAME #547 pre-gate (skips when no
    # anchor in the un-scanned increment) and deduped by the unified sink, so the
    # added cost is ~one bounded, idempotent pass per ended session, not per tick.
    # The grace window below keeps the pass from firing before the aggregator has
    # had time to land the tail blocks (firing too early would re-create the bug).
    # Marked one-shot via sessions.recognized_final_at. Set false to disable.
    finalize_pass: bool = True
    # Seconds a session must have been ENDED before the finalization pass runs —
    # the slack for the timeline aggregator to materialise blocks that close after
    # the cut. Default 180s (3× the 60s timeline tick), comfortably past the
    # window-close + tick latency. Lower bounds the recognition latency for a
    # truly-final session; higher is safer against missing a very-late block.
    finalize_grace_seconds: int = 180
    # --- R3 material-change-republish ------------------------------------------
    # On a dedup hit, compare the re-recognition against the stored row with the
    # deterministic rules in ``intent.sink.material_change`` (confidence ratchet
    # ≥ +0.15 / provenance counterpart_proposed→user_committed). A material
    # change UPDATEs the row in place (id + status preserved; consumed/dismissed
    # are never resurrected) and republishes it on the SSE stream marked
    # ``updated`` (not a new row). Republish count is bounded by construction
    # (the confidence ratchet allows at most ~⌈1/0.15⌉ fires per intent;
    # provenance upgrade fires once), honoring 克制优先 — 宁可漏 republish 不可
    # 重复打扰. False → dedup hits skip outright (legacy surface-once behavior,
    # byte-identical).
    material_republish: bool = True
    # Feed recent event-daily entries (session summaries) into the slow path's
    # recall background. Hours of lookback; 0 disables the layer.
    recall_recent_events_hours: int = 48
    # --- recall 主预算 (#611, ablation 2026-06-10 §4 落地) ------------------------
    # The shared char budget for ``assemble_background``'s main layers
    # (schema_prior → scene → behavior → fact → keyword → events). The 2026-06-10
    # ablation proved that when DECISION-RELEVANT memory (the fact/behavior
    # layers) is squeezed out of this budget, slow-path recognition quality
    # collapses (negative-suppression 6/6 misfires, anchor resolution → 0), and
    # set the action gate: raise the budget to 2400 once the fact-layer squeeze
    # rate is significant (>10%). The #647-corrected telemetry on real traffic
    # showed fact-layer squeeze at ~66% of calls (6.6× the gate) — the squeeze is
    # on the high-value layer, NOT the cheap keyword/events tail the issue
    # assumed — so this lands the ablation's pre-authorized 2400 default. It is
    # NOT a free dial: decision-layer demand has a long tail (p90 ~3.5k, max
    # ~5.6k chars on the measured corpus), and 喂得多≠更好 — over-filling dilutes
    # the prompt and burns volatile-segment tokens with no cache hit; the
    # ablation found NO benefit beyond 2400 and a real dilution risk at 4800.
    # 2400 clears ~44% of the decision-layer squeezes at ~+8% input tokens; the
    # residual long tail is a DELIBERATE capacity tradeoff (漏低优先层 = 有限
    # 损失), not chased higher.
    #
    # 2400 → 2600 (dead-semantic-layer fix): the #647 demand model PREDATED the
    # semantic layer, which was silently dead in production (oversized embed query
    # — fixed alongside). With it working, the recall_budget_sweep over 40 real
    # sessions shows semantic is now the LARGEST decision layer (~1610 chars/
    # session, > fact ~635 > scene ~16), so the decision-layer demand ceiling rose
    # from ~835 to ~2261. At 2400 semantic was squeezed in ~5% of sessions (a
    # 16-char shortfall); 2600 fits scene+fact+semantic with ~0 squeeze while the
    # keyword-fallback dilution zone only begins ~2600 — so this is the smallest
    # cap that holds the now-real decision demand, NOT a move toward the 4800
    # dilution risk (still well under it). The faithful recognition A/B on the
    # FIXED layer is no-harm (0 stable intent removals) + a diffuse non-negative
    # firing lean within the noise band — so giving the layer its full budget is
    # safe; the keyword tail past the decision layers stays a deliberate漏-tail.
    recall_max_chars: int = 2600
    # --- Semantic recall layer (dense te3-large ⊕ the lexical layers) ------------
    # The lexical recall layers (keyword/fact/behavior) need a hint TOKEN to land
    # on the right memory; they can never recall a conceptually-related fact that
    # shares no words with the current scene (user booking a flight → recall their
    # travel preferences). A semantic layer embeds the scene and pulls dense-similar
    # durable memory, folded through the SAME evo_nodes chain-head fold. Default ON
    # but the daemon only ACTIVATES it when an embeddings endpoint (OPENAI_*) is
    # configured AND ``[search] hybrid_enabled`` (``embeddings_client.available()``);
    # a no-creds install is byte-identical (``fts._dense_pool`` fail-opens to []).
    recall_semantic_enabled: bool = True
    # Dense hits surfaced into the slow-path background (sits AFTER durable facts,
    # before the keyword fallback, so it never squeezes precise facts out).
    recall_semantic_top_k: int = 5
    # Fast path (per-arrival K1): same semantic recall, but embedded ONLY for gate
    # survivors (right before the LLM call, never per raw capture) + a per-scope TTL
    # cache so repeated arrivals in one session re-embed ≤~1/min. Keeps the <5s budget.
    fast_recall_semantic_enabled: bool = True
    fast_recall_semantic_top_k: int = 3
    fast_recall_semantic_ttl_seconds: int = 60
    # Reverse-loop POSITIVE prior (spec 2026-06-26 G2/G3): inject a damped,
    # capped "kinds the user actually FINISHES" hint from the typed ``completed``
    # intents (distinct from the coarse jsonl taste few-shot). This flag is the
    # A/B switch — flip OFF for the off-arm of the longitudinal oracle (then the
    # slow path is byte-identical to before). Default ON; the prior is empty (so
    # also byte-identical) until completions accumulate.
    recall_completed_prior_enabled: bool = True
    # --- counterpart_proposed confidence cap (production over-fire fix) ----------
    # The prompt tells the model a ``counterpart_proposed`` item (the OTHER party
    # proposed a time, the user hasn't responded) should carry LOW confidence
    # (建议 ≤0.6) — by design these are "lightly handed back", not auto-surfaced.
    # But the model ignores that soft instruction ~77% of the time (production:
    # 64/83 counterpart_proposed meetings landed ≥0.7), so they sail past the app
    # sentinel's 0.7 surface bar and nag — counterpart_proposed meetings have a
    # ~10% accept rate vs ~56% for user_committed. The generic ``_clamp_confidence``
    # only caps inferred intents at 0.9 (``CONFIDENCE_CAP_INFERRED``), far above
    # the surface bar. This is a SEPARATE, lower deterministic cap that ENFORCES
    # the prompt's stated intent at the single sink entrance (covers fast K1 / slow
    # trajectory / meeting pack). A capped intent still PERSISTS (recall/memory keep
    # it) — it just no longer crosses the proactive-surface bar. Set to 0.9 to
    # restore the prior behaviour (cap == the generic inferred cap = no-op). 0.6
    # matches the prompt; 0.0 disables surfacing of all counterpart_proposed.
    counterpart_confidence_cap: float = 0.6
    # --- per-kind zero-nag confidence ceilings (production over-fire fix) --------
    # The prompt assigns three kinds an explicit LOW confidence ceiling (≤0.4) so
    # they are ZERO-NAG by design — `info_need` (something the user wants to read/
    # check), `meeting_hint` (a vague coordination wish), `backlog` (a deadline-free
    # deferred to-do). All three are low-cost memory/recall signals that must stay
    # BELOW the app sentinel's 0.7 surface bar — they are never meant to pop a
    # proactive proposal. The model honours this unevenly: production
    # `meeting_hint`/`backlog` are 100% ≤0.4, but **`info_need` lands >0.4 in 97% of
    # rows (69/81 ≥0.7)**, so miscalibrated info_need crossed the surface bar and
    # nagged (7 surfaced-and-rejected). This deterministic per-kind ceiling enforces
    # the prompt's stated design at the single sink entrance — UNCONDITIONALLY (even
    # a `user_committed` info_need is a low-cost memory signal, not a high-confidence
    # surface), composing with (min of) the provenance cap above. A capped intent
    # still persists (recall/memory keep it); only its surfacing eligibility drops.
    # The COMPLETE set of the prompt's zero-nag kinds is enforced (meeting_hint /
    # backlog don't currently violate, but enforcing their stated ceiling is
    # defensive + complete, costless today, catches future drift). This flag is the
    # kill-switch — set false to stop enforcing the kind ceilings.
    enforce_kind_confidence_ceilings: bool = True
    # --- incomplete-actionable suppression (production over-fire fix) ------------
    # Some seed kinds become NON-ACTIONABLE without one load-bearing field: a
    # `reminder` with no `text` (no WHAT to remind), a `meeting`/`calendar` with no
    # `when_text` (no WHEN to schedule). The existing soft validation only
    # down-weights ×0.8 (PAYLOAD_MISSING_FIELD_PENALTY), which leaves a 0.9 intent at
    # 0.72 — still above the app sentinel's 0.7 surface bar, so an empty reminder
    # still nags. Production (persome intent-audit): 6 of 6 user-rejected reminders are
    # empty-`text`, vs accepted reminders all carry text — a clean separator. This
    # caps such load-bearing-incomplete intents to ``_INCOMPLETE_ACTIONABLE_CAP``
    # (below the surface bar). NOTE the deliberate ASYMMETRY: `assignment` is NOT in
    # the load-bearing set — 9 of 16 ACCEPTED assignments are missing `task_text`
    # (the `assigned_by`/channel carry it), so its missing field stays a SOFT
    # down-weight. Set false to restore the prior all-soft behaviour (kill-switch).
    suppress_incomplete_actionable_intents: bool = True
    # --- #533 负反馈 (kind, scope) 级闭集硬冷却 ----------------------------------
    # Before #533 the negative-feedback loop was prompt-soft only: dismissed
    # intents render as a "最近被忽略 N 次" prior the model is asked to honor; the
    # only HARD block was an exactly-equal dedup_key, so a kind dismissed many
    # times still re-surfaced under a new wording (new key). This闭集硬冷却 turns
    # repeated dismissals into a deterministic gate at the unified sink (covers
    # fast K1 / slow trajectory / meeting pack uniformly, bypassing the prompt):
    # when a kind is dismissed ≥``cooldown_dismiss_threshold`` times within
    # ``cooldown_window_days`` IN THE SAME SCOPE, that (kind, scope) enters a HARD
    # cooldown for ``cooldown_hours`` measured from the most-recent dismissal —
    # intents of that kind in that scope are dropped (not persisted, not surfaced)
    # for the duration. The asymmetric-cost constitution makes this the right
    # direction: 弹错=复利损失, 漏报=有限损失, so trading recall for precision on a
    # kind the user keeps rejecting is net-positive. Two deliberate tightenings
    # vs the first cut (avoid the「惩罚高反馈用户」trap): (1) (kind, scope) rather
    # than global by-kind, so dismissing reminders in one session can't mute them
    # system-wide; (2) the counting window defaults to the SAME magnitude as the
    # cooldown (1 day vs 24h) — a wide 7-day window + sliding 24h reset would put
    # an active feedback-giver into near-permanent cooldown over sparse dismissals.
    # High-confidence / user_committed intents are EXEMPT from the gate (see
    # sink.persist_intent_result): the hard闸 only压 model-inferred mid/low
    # confidence intents — a verbatim零熵承诺 (宪法 §5) is never denied. The
    # cooldown ALWAYS expires (time-bounded, never a lifetime ban — re-calibration
    # / manual release is #534, out of this batch). ON by default —
    # safe-by-construction: it only ever fires AFTER the user has explicitly
    # dismissed the same kind 3× within ~a day in one scope, every suppression is
    # recorded as telemetry (cooldown_suppressions — 拒绝是金矿, observability is
    # never gated), and it self-heals once the latest dismissal ages out. Flip
    # ``cooldown_enabled`` false to restore prompt-soft-only.
    cooldown_enabled: bool = True
    cooldown_window_days: float = 1.0
    cooldown_dismiss_threshold: int = 3
    cooldown_hours: float = 24.0

    # Fuzzy content fold (重复推送相同语义修复): the ungrounded content fold
    # (sink._find_content_fold_target) folds info_need/reminder/assignment by
    # EXACT normalized text-body equality. Real recognition drifts wording every
    # session, so the SAME to-do lands as many separate open rows (生产实测：一件
    # 「为 PR #102 加 GitHub Actions secret/labels」记成 6 条). When enabled, the
    # fold also collapses bodies whose char-bigram Jaccard ≥ content_fold_similarity
    # (both bodies long enough; short ones stay exact-only) — deterministic,
    # zero-LLM, governed by the golden-set gate. false = exact-only (pre-fuzzy).
    content_fold_fuzzy_enabled: bool = True
    # Conservative by design: distinct same-topic facts must NOT fold (golden
    # negatives pin the floor); only near-identical re-statements clear it.
    content_fold_similarity: float = 0.72
    # Evidence-driven auto-close (识别即更新状态): when a new capture shows an
    # existing OPEN intent is已做/已拒/被取代, the recognizer's resolution channel
    # flips it to the terminal ``resolved`` status (event-driven, rides the
    # per-capture path — no new poll). **DEFAULT OFF (kill-switch)**: off = the
    # ``resolutions`` LLM output is parsed but never acted on (byte-identical
    # behavior). Precision is paramount — a wrong auto-close drops a todo the user
    # did NOT do, worse than leaving it stale — so two independent signals (the
    # LLM resolution AND the deterministic fold-matcher) must agree before a close.
    auto_close_resolved_enabled: bool = False
    # Confidence floor for an evidence auto-close, independent of recognition
    # confidence. A resolution below this is dropped.
    auto_close_min_confidence: float = 0.85
    # Higher floor to auto-close a high-value ``user_committed`` promise (and only
    # on outcome=done) — a thing the user said in so many words must not be closed
    # on a weak signal.
    auto_close_committed_min_confidence: float = 0.95
    # Semantic content fold (intent.embeddings, bge-small-zh cosine): on a char-bigram
    # MISS, a second chance via sentence-embedding cosine — folds PARAPHRASES the lexical
    # layer can't see. **DEFAULT OFF (opt-in)**: adversarial review (test_dedup_rework)
    # proved a one-keyword-apart DISTINCT to-do ("加密钥" vs "加标签", cos 0.85; "调研连接"
    # vs "调研部署", cos 0.90) sits in the SAME cosine band as a genuine paraphrase (cos
    # 0.86–0.89) — no threshold separates them, so a deterministic cosine fold over-folds
    # the 误吞=0 precision guards. The semantic mid-band is the LLM's job (recognizer
    # dedup_against_open + the app-side re-push judge). Flip on only where recall > precision.
    semantic_fold_enabled: bool = False
    # Cosine threshold when enabled — HIGHER than the Jaccard 0.72 because cosine is dense.
    semantic_fold_similarity: float = 0.82
    # Layer 2 — recognizer-time dedup: inject today's still-open intents (cross-scope)
    # into the slow recognizer prompt + instruct the LLM not to re-emit a semantically
    # existing commitment. Stops the duplicate before it's generated (the sink fold is
    # the at-insert backstop). false ⇒ recognizer never sees the existing-open checklist.
    dedup_against_open: bool = True
    # Fold the APP-side semantic-dedup INTO this one recognition call: also feed the user's LIVE Persome
    # tasks + recently-dismissed proactive todos (read off-disk from ~/.persome) into the dedup checklist,
    # so a just-arrived message that re-states something the user ALREADY has a task for (or just
    # dismissed) isn't emitted — without a SECOND LLM round-trip in the app. Lets the app drop its
    # judge LLM (recognition↔memory↔execution share one decision). false ⇒ checklist = open intents only.
    dedup_against_app_state: bool = True


@dataclass
class SkillCheckConfig:
    # Detect skill matches inside the per-minute timeline LLM call.
    enabled: bool = True
    confidence_floor: float = 0.65


@dataclass
class SchemaConfig:
    # D2 schema miner daily tick: clusters durable facts per file and induces
    # predictive ``schema-*.md`` priors the intent recognizer reads back. Scheduled
    # after daily-safety-net (23:55) so it sees the latest closed sessions.
    # Disabling it stops production of new schemas only —
    # existing schema-*.md files stay grep-able and keep feeding the intent prior.
    enabled: bool = True
    daily_tick_hour: int = 0
    daily_tick_minute: int = 15
    # Cross-domain sweeper (Hy-Memory): after the per-file miner runs, collide
    # "topic-far but behavior-near" stable schemas into higher-level ones via a
    # deterministic behavior pre-filter + LLM judge (no embedding). Runs as the tail
    # of the same schema-tick (no new daemon task). Default ON: the downside is
    # bounded — a low-quality collision gets a low LLM confidence → born ``forming``
    # → NOT injected into the recognizer prior (only ``stable`` ≥ min_confidence
    # fusions are), and the sweeper-prompt is biased to refuse strained merges. The
    # main cost is the per-tick LLM probes, capped by the topic/behavior pre-filter.
    cross_domain_enabled: bool = True
    cross_domain_behavior_max_distance: float = 0.5  # ≤ this == "behavior-near" (pre-filter)
    cross_domain_min_confidence: float = 0.6  # fused schema below this is born ``forming``
    # root apex (level-3, 2026-07-04 spec: Memory Root Apex). Tail of the schema-tick —
    # ONE bounded LLM compresses the active 体/面/profile into the SINGLE ≤budget-token
    # always-resident apex "who is this person". Default ON (product ruling 2026-07-04):
    # born active, chain-supersedes the prior root, 3 deterministic gates + fail-open
    # (无 root → residency falls back to resident_faces, so default-ON is safe).
    root_synthesis_enabled: bool = True
    root_token_budget: int = 1500  # the always-resident apex hard budget


@dataclass
class EvomemConfig:
    # evomem SSOT switch — PR-1 survivability base (design doc
    # docs/superpowers/specs/2026-06-10-evomem-ssot-switch-design.md §3).
    # Everything here is a SIDE CHANNEL: with the flags off, the daemon behaves
    # byte-identically to before (P0 discipline). These facilities must run
    # stable in production BEFORE any truth migration (backfill / dual-write)
    # is allowed to land (§3.5 顺序纪律).
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
class MemoryConfig:
    auto_dormant_days: int = 30


@dataclass
class SearchConfig:
    default_top_k: int = 5
    filter_superseded_by_default: bool = True
    # Hybrid semantic retrieval (BM25 ⊕ dense te3-large → RRF). Default ON, but the daemon only
    # activates it when an embeddings endpoint (OPENAI_*) is configured — otherwise it stays
    # byte-identical BM25 (no vectors written/queried). Real production A/B justified the flip:
    # on paraphrase queries (how users actually search Chinese memory) recall@10 went 0.025 → 0.76;
    # on exact-token queries it costs ~0.04 (BM25's home turf). See
    # docs/superpowers/specs/2026-06-25-production-hybrid-retrieval-design.md.
    hybrid_enabled: bool = True
    hybrid_recall_n: int = 50  # BM25/dense candidate pool depth before RRF
    hybrid_rrf_k: int = 20  # RRF constant (benchmark-proven; prod paraphrase win is large at 50/20)
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
    # MATCH also hits classification LABELS (#intent/#kind:meeting/schema/fact/entity…) —
    # on the real store 251 live hits for 'intent' carried the token only in their tags
    # (recognizer CANDIDATE rows recalled by label). False = match the content column only
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
    # Chat assistant powered by the Anthropic SDK. Separate from the litellm-based
    # [models.*] stages because the chat loop needs first-class Anthropic features
    # (prompt caching, extended thinking, tool use) and goes through the native
    # SDK rather than the litellm shim. Configured under [chat] in config.toml.
    #
    # API key and base URL are NOT in TOML — they come from the env vars
    # ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_BASE_URL``, populated by Mens.app
    # (single SoT) or by the user's shell for CLI debugging.
    #
    # NO routing prefix here. The chat agent calls the Anthropic SDK
    # directly (``anthropic.AsyncAnthropic`` in ``chat/agent.py``); whatever
    # string sits in ``model`` is sent verbatim in the request body and
    # validated by the gateway. DeepSeek's ``/anthropic`` endpoint only
    # accepts bare names (``deepseek-v4-flash`` / ``deepseek-v4-pro``); a
    # litellm-style ``anthropic/...`` prefix would be rejected as an unknown
    # model. Prompt caching for the chat agent is enabled via the
    # ``cache_control`` field on message content (see ``chat/agent.py``),
    # independent of the model name. The litellm prefix rule documented
    # elsewhere applies only to other LLM stages (timeline / reducer /
    # classifier / compact) that route through ``writer/llm.py`` — leave
    # those defaults alone.
    model: str = "deepseek-v4-flash"
    # Extended thinking (Anthropic "thinking" block). 0 disables (default —
    # safe for non-reasoning models like deepseek-v4-flash). Set to >=1024
    # to enable; requires a reasoning-capable model (Claude Opus/Sonnet 4.5+,
    # claude-haiku-4+, deepseek-reasoner via /anthropic gateway, etc.).
    # Streamed back to the UI as ``type: reasoning`` SSE frames.
    thinking_budget: int = 0
    # MCP client connections: the chat agent connects to these as a client so the
    # model can invoke their tools alongside the built-in tool set.
    mcp_connect_daemon: bool = True  # auto-connect to daemon's own MCP server
    mcp_servers: list[MCPServerSpec] = field(default_factory=list)  # extra servers


@dataclass
class DebugHudConfig:
    # What the debug HUD (the always-on-top panel shown in debug mode) renders.
    # A single allowlist over every content block; the HUD shows only the keys
    # listed here. Valid keys:
    #   intent / tool_call / thinking / stage  — AGENT ACTIVITY event kinds
    #   health                                 — daemon health + counts
    #   memory                                 — recent memory writes
    # Default is intents only, so the panel is quiet by default; add keys to
    # surface more. Read live via GET /config/debug-hud (no daemon restart).
    show: list[str] = field(default_factory=lambda: ["intent"])


# Valid [debug_hud] show keys, in display order. The app's gear menu offers
# exactly these; the PUT endpoint filters writes to this set.
DEBUG_HUD_KEYS: tuple[str, ...] = (
    "intent",
    "tool_call",
    "thinking",
    "stage",
    "health",
    "memory",
)


def set_debug_hud_show(toml_text: str, show: list[str]) -> str:
    """Return ``toml_text`` with ``[debug_hud] show`` set to ``show``.

    A targeted, formatting-preserving edit (NOT a full re-serialize): replaces
    the ``show = …`` line inside an existing ``[debug_hud]`` section, inserts
    the line if the section exists without it, or appends a fresh section.
    Everything else in the file — comments, ordering, the user's other edits —
    is left untouched. Used by ``PUT /config/debug-hud`` so the app's gear menu
    can persist the allowlist without anyone hand-editing the file.
    """
    rendered = "show = [" + ", ".join(f'"{s}"' for s in show) + "]"
    lines = toml_text.splitlines()
    trailing_nl = toml_text.endswith("\n") or toml_text == ""

    sec = next((i for i, ln in enumerate(lines) if ln.strip() == "[debug_hud]"), None)
    if sec is None:
        prefix = "" if toml_text == "" else ("\n" if trailing_nl else "\n\n")
        return toml_text + f"{prefix}[debug_hud]\n{rendered}\n"

    j = sec + 1
    while j < len(lines):
        s = lines[j].strip()
        if s.startswith("[") and s.endswith("]"):
            break  # next section, no show line found
        if s.startswith("show") and "=" in s:
            lines[j] = rendered
            return "\n".join(lines) + ("\n" if trailing_nl else "")
        j += 1
    lines.insert(sec + 1, rendered)
    return "\n".join(lines) + ("\n" if trailing_nl else "")


@dataclass
class DevConfig:
    # Developer-only gates retained for compatibility while the memory viewer
    # moves from /dev/memory to the formal /model surface.
    enabled: bool = False


@dataclass
class Config:
    models: dict[str, ModelConfig] = field(default_factory=dict)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    timeline: TimelineConfig = field(default_factory=TimelineConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    reducer: ReducerConfig = field(default_factory=ReducerConfig)
    classifier: ClassifierConfig = field(default_factory=ClassifierConfig)
    writer: WriterConfig = field(default_factory=WriterConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    evomem: EvomemConfig = field(default_factory=EvomemConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    mcp: MCPConfig = field(default_factory=MCPConfig)
    pattern_detector: PatternDetectorConfig = field(default_factory=PatternDetectorConfig)
    intent_recognizer: IntentRecognizerConfig = field(default_factory=IntentRecognizerConfig)
    memory_delta: MemoryDeltaConfig = field(default_factory=MemoryDeltaConfig)
    memory_decay: MemoryDecayConfig = field(default_factory=MemoryDecayConfig)
    orphan_reaper: OrphanReaperConfig = field(default_factory=OrphanReaperConfig)
    skill_check: SkillCheckConfig = field(default_factory=SkillCheckConfig)
    schema: SchemaConfig = field(default_factory=SchemaConfig)
    user: UserConfig = field(default_factory=UserConfig)
    chat: ChatConfig = field(default_factory=ChatConfig)
    debug_hud: DebugHudConfig = field(default_factory=DebugHudConfig)
    dev: DevConfig = field(default_factory=DevConfig)
    # --- Competitive-enhancement feature flags (spec 2026-06-23-evomem-...) ---
    # Flat top-level toggles, read defensively via ``getattr(cfg, name, default)``
    # at each feature site (api Origin/Host guard · extraction known-memory
    # priming · evomem vector recall · capture lock/secure-input gating). Kept
    # flat (not nested) so the feature code stays decoupled from this dataclass.
    api_require_local_origin: bool = True
    extraction_known_memory_priming: bool = True
    evomem_vector_recall_enabled: bool = True
    capture_pause_on_lock: bool = True
    capture_suppress_secure_input: bool = True
    # Wave 2: E1 person graph · E2 case extraction · #6 screenshot encryption.
    person_graph_enabled: bool = True
    case_extraction_enabled: bool = True
    # Graph-memory P0-2 (#428): deterministic + LLM relation-edge extraction → SHADOW.
    # Default OFF (shadow-first: prove extraction quality before edges can reach retrieval).
    relation_extraction_enabled: bool = False
    # §7-3 转正扇出上限（promotion volume IS dilution volume）；edge-audit 全量 0%
    # 幻觉后默认 10→20（B 步），sweep 复跑护带。
    edge_promote_fanout: int = 20
    capture_encrypt_screenshots: bool = True
    # Wave 2b: #7 actionable-subset extended retention + provenance · #8 view_capture.
    capture_extended_retention_enabled: bool = True
    capture_actionable_retention_days: int = 7
    view_capture_enabled: bool = True
    # Wave 3: #9 Rewind REST endpoints (/rewind/day, /rewind/screenshot).
    rewind_enabled: bool = True

    def model_for(self, stage: str) -> ModelConfig:
        """Return stage config (already inherited from default at build time)."""
        return self.models.get(stage) or self.models.get("default") or ModelConfig()


# Provider name → canonical env var prefix. Keep this list aligned with
# Mens.app's ``kManagedEnvKeys`` so the App UI surfaces every supported
# provider. New providers: add an entry here AND in the App.
_PROVIDER_ENV_PREFIX: dict[str, str] = {
    "anthropic": "ANTHROPIC",
    "openai": "OPENAI",
    "deepseek": "DEEPSEEK",
}


def infer_provider(model: str) -> str:
    """Best-effort provider name from a litellm-style model string.

    Recognises explicit ``provider/model`` prefixes first; falls back to a few
    well-known bare-name heuristics. Returns ``"openai"`` when unknown — the
    litellm default — so legacy ``gpt-*`` configs keep working.
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
    # Secret/base_url are env-only now (managed by Mens.app). TOML scalars
    # cover model + thinking_budget + mcp_connect_daemon.
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


def load(path: Path | None = None) -> Config:
    path = path or paths.config_file()
    raw: dict = {}
    if path.exists():
        with open(path, "rb") as f:
            raw = tomllib.load(f)

    return Config(
        models=_build_models(_as_dict(raw.get("models"))),
        capture=_build_dataclass(CaptureConfig, _as_dict(raw.get("capture"))),
        timeline=_build_dataclass(TimelineConfig, _as_dict(raw.get("timeline"))),
        session=_build_dataclass(SessionConfig, _as_dict(raw.get("session"))),
        reducer=_build_dataclass(ReducerConfig, _as_dict(raw.get("reducer"))),
        classifier=_build_dataclass(ClassifierConfig, _as_dict(raw.get("classifier"))),
        writer=_build_dataclass(WriterConfig, _as_dict(raw.get("writer"))),
        memory=_build_dataclass(MemoryConfig, _as_dict(raw.get("memory"))),
        evomem=_build_dataclass(EvomemConfig, _as_dict(raw.get("evomem"))),
        search=_build_dataclass(SearchConfig, _as_dict(raw.get("search"))),
        mcp=_build_dataclass(MCPConfig, _as_dict(raw.get("mcp"))),
        pattern_detector=_build_dataclass(
            PatternDetectorConfig, _as_dict(raw.get("pattern_detector"))
        ),
        intent_recognizer=_build_dataclass(
            IntentRecognizerConfig, _as_dict(raw.get("intent_recognizer"))
        ),
        memory_delta=_build_dataclass(MemoryDeltaConfig, _as_dict(raw.get("memory_delta"))),
        memory_decay=_build_dataclass(MemoryDecayConfig, _as_dict(raw.get("memory_decay"))),
        orphan_reaper=_build_dataclass(OrphanReaperConfig, _as_dict(raw.get("orphan_reaper"))),
        skill_check=_build_dataclass(SkillCheckConfig, _as_dict(raw.get("skill_check"))),
        schema=_build_dataclass(SchemaConfig, _as_dict(raw.get("schema"))),
        user=_build_dataclass(UserConfig, _as_dict(raw.get("user"))),
        chat=_build_chat(_as_dict(raw.get("chat"))),
        debug_hud=_build_dataclass(DebugHudConfig, _as_dict(raw.get("debug_hud"))),
        dev=_build_dataclass(DevConfig, _as_dict(raw.get("dev"))),
        # Competitive-enhancement flat toggles (spec 2026-06-23): top-level TOML
        # scalars so config.toml can override the safe defaults.
        api_require_local_origin=bool(raw.get("api_require_local_origin", True)),
        extraction_known_memory_priming=bool(raw.get("extraction_known_memory_priming", True)),
        evomem_vector_recall_enabled=bool(raw.get("evomem_vector_recall_enabled", True)),
        capture_pause_on_lock=bool(raw.get("capture_pause_on_lock", True)),
        capture_suppress_secure_input=bool(raw.get("capture_suppress_secure_input", True)),
        person_graph_enabled=bool(raw.get("person_graph_enabled", True)),
        case_extraction_enabled=bool(raw.get("case_extraction_enabled", True)),
        relation_extraction_enabled=bool(raw.get("relation_extraction_enabled", False)),
        edge_promote_fanout=int(raw.get("edge_promote_fanout", 20)),
        capture_encrypt_screenshots=bool(raw.get("capture_encrypt_screenshots", True)),
        capture_extended_retention_enabled=bool(
            raw.get("capture_extended_retention_enabled", True)
        ),
        capture_actionable_retention_days=int(raw.get("capture_actionable_retention_days", 7)),
        view_capture_enabled=bool(raw.get("view_capture_enabled", True)),
        rewind_enabled=bool(raw.get("rewind_enabled", True)),
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
# ``~/.persome/env`` (dotenv format), managed by Mens.app as the single
# source of truth. Canonical env var names: {PROVIDER}_API_KEY and
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

[models.consolidator]
# Cross-file offline consolidation — reads a working region of recent +
# semantically related entries, dedups / abstracts / merges them.
# Inherits from [models.default] unless overridden.


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
# KILL-SWITCH: the bundled PaddlePaddle can SIGSEGV *during* inference (native fault, #335/#218),
# which — running on an in-process daemon thread — takes the whole daemon down. To hard-disable all
# OCR inference at deploy time WITHOUT a config rebuild, set the env var PERSOME_DISABLE_OCR=1
# (degrades to "no OCR text for AX-poor apps"; paddle is never imported). Subprocess isolation is the
# follow-up root fix.
ocr_tier = "tiny"             # tiny (default) | small | medium — local PP-OCRv6 weights
ocr_min_gap_seconds = 15.0    # minimum seconds between OCR runs for the same window
ocr_structured = true              # geometry-structure raw OCR (zero LLM, on-device): columns/regions + per-app field labels
ocr_collect_training_data = true   # local-only OCR samples (geometry + structured result, NEVER screenshots) for future model training; no upload
# cmux signal source: real terminal text via cmux's local unix-socket RPC (GPU-rendered
# terminals expose ~no AX text). Read-only, zero external cost, sub-second deadline,
# silent degrade when cmux isn't running — hence default on.
cmux_source_enabled = true

[timeline]
window_minutes = 1             # length of each aggregator block (verbatim-preserving normalizer)
cold_lookback_minutes = 30
recent_context_blocks = 720    # ~12h of 1-min blocks
max_parallel_windows = 4       # parallel LLM workers for backlog catchup (1 = sequential)

[writer]
soft_limit_tokens = 20000
hard_limit_tokens = 50000
dedup_window_hours = 24
cold_start_conservative_hours = 0
context_token_limit = 80000      # trim message history when estimated tokens exceed this
llm_retry_attempts = 2           # retry LLM call this many times on transient failure
consolidation_cadence = 8        # trigger offline consolidation every N completed sessions
consolidation_max_region_size = 50    # max entries assembled into a working region
consolidation_max_iterations = 15     # tool-call loop ceiling for the consolidator

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
enabled = true             # detect repetitive behavior patterns after classifier
structured_filter = true   # true = SQL candidate filter first (save tokens); false = raw data to LLM (burn tokens, may catch more)
lookback_days = 7          # scan this many days of event-daily for patterns
min_occurrences = 2        # minimum repetitions to flag as a candidate
confidence_threshold = 0.7 # auto-accept threshold for LLM-validated patterns

[memory]
auto_dormant_days = 30

[evomem]
# evomem SSOT switch — survivability base (snapshots + chain self-check).
# All side-channel: with these off, the daemon behaves exactly as before.
snapshot_enabled = true            # daily VACUUM INTO backup/evo-YYYYMMDD.db at the 23:55 tick (after the WAL checkpoint); bad snapshots alert instead of overwriting good ones
snapshot_keep_daily = 7            # keep every daily snapshot from the last N days
snapshot_keep_weekly = 4           # additionally keep Monday snapshots from the last N weeks
integrity_check_enabled = true     # chain-invariant self-check at daemon startup + after each snapshot (quick_check / pointer symmetry / anti-fork / head consistency / acyclicity / projection reconciliation); alerts via the integrity_alert SSE event
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
filter_superseded_by_default = true
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
# model = "deepseek-v4-flash"      # bare name only — chat agent uses Anthropic SDK directly, NOT litellm; no "anthropic/" prefix
# thinking_budget = 0
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

[intent_recognizer]
max_blocks = 200                  # upper bound on session blocks fed to the model
timeline_history_blocks = 20      # recent pre-session timeline blocks fed as cross-session backdrop (0 to disable)
fast_model = ""                   # model override for the fast path (empty = inherit intent_recognizer stage model)
per_app_min_interval = 2.0        # min seconds between fast-path passes for the same app (recognition rate limit; 0 disables)
per_app_periodic_interval = 30.0  # longer per-app interval for periodic/heartbeat triggers (lowest-signal)
backoff_max_misses = 6            # consecutive 0-intent LLM passes before cool-off (softened for anchor-gate removal: bias recall)
backoff_base_seconds = 20.0       # base cool-off; doubles per extra miss beyond the threshold
backoff_max_seconds = 120.0       # cap on the backoff cool-off (≤2min recover so a real msg after chatter isn't buried)
domain_allowlist = []             # browser (K2) per-domain allowlist; empty = allow all; K1 chat apps unaffected (scaffold)
schema_prior_enabled = true       # MIGRATION ACTIVATED: inject D2 schema-*.md inertia priors (predictive先验) into recall; [] no-op until schemas exist
schema_feedback_enabled = true    # R4 反馈闭环: HUD dismiss/accept 回灌来源 schema confidence (−0.05/+0.03, 0.6 阈值翻 stable↔forming); 无 schema_sources 的 intent 零行为
taste_profile_enabled = true      # Few-shot 正先验: 把该用户最近的正向反馈（accepted/completed/manual_baseline，读 ~/.persome/logs/context-feedback.jsonl）作为「这类他真会动手」的简短前注注入识别器 user prompt——per-user 个性化里 per-user 微调唯一能买到的东西，~free。刻意只做正例：dismiss 已被 _dismissed_prior(R3) + #533 硬冷却闸双重覆盖，再加只会三重计数 + 重踩 #533 已废弃的弱 prompt-soft 路。fail-open（无文件/无正例→不加前注，识别不变）
taste_profile_days = 21           # 正先验的回看窗口（天）
taste_profile_max_items = 6       # 渲染进前注的最新 N 条（content 去重后）
user_profile_enabled = true       # 共享可缓存用户画像前缀: 把 schema_prior + taste 合成一个稳定块, 放进快路 AND 慢路的缓存前缀 —— 快路从此有 memory（原本裸 system 无 memory）, 慢路按易变性重排（schema 让出 recall 预算给情景召回）。off = byte-identical 回退（schema 回到 background、taste 回到易失体、快路裸 system）
recall_fold_superseded = true     # fold recall hits to evolution-chain heads, read from evo_nodes — the SSOT (§1.4; entry_chain + its staging flags retired in PR-7; pre-backfill degrades to the equivalent superseded-column fold)
recall_chain_trail = true         # append ← [曾]/[精炼自] evolution trail to chain heads (rendered from evo_nodes bidirectional pointers; needs the fold on + evo_nodes ready)
recall_include_confidence = true  # meta-cognition: annotate recall hits with ⚠(低置信)/⚠(冲突未裁决) from entry_metadata; safe-by-construction (only affects new tagged memories), ON for everyone
slow_path_max_blocks = 60         # R3 cost gate: most recent N session blocks render verbatim in the slow-path prompt; older blocks fold into ONE deterministic header line (no LLM). 0 = unbounded legacy prompt (byte-identical)
slow_pregate = true               # #547 锚定 pre-gate: skip the slow-path LLM call when the blocks NEW since the last tick carry no slow-anchor (SLOW_ANCHOR_RE = fast _ANCHOR_RE + euphemism cues); skips recorded as recognition_ticks outcome=skipped_no_anchor (hit_rate unpolluted). false = every block flush burns one LLM call (legacy)
slow_pregate_stitch_lookback_blocks = 5  # #576 跨 tick 缝合例外: do NOT skip when a NEW block carries a bare confirmation cue (行/好/可以/ok) AND one of the last N OLD blocks carries a slow anchor — re-run the LLM so a "问句 → 隔 tick 确认" commitment gets stitched. Bare confirmations stay OUT of SLOW_ANCHOR_RE (too high-frequency); the two-signal AND keeps it surgical. 0 disables (pure #547)
pregate_mode = "regex"            # slow pre-gate discriminator: "regex" (lexical, DEFAULT) | "bge" | "hybrid" (regex ∪ bge). The bge encoder is NOT bundled — "bge"/"hybrid" require `pip install onnxruntime tokenizers` + a BYO pregate_bge_model_dir, else they fail-open to regex. (When bundled, hybrid lifted slow-lane recall 30%→60%; that gain now needs a BYO model.)
material_republish = true        # R3: dedup-hit re-recognition with a material change (confidence ratchet >= +0.15 / counterpart_proposed→user_committed) UPDATEs the row (id+status kept, dismissed never resurrected) and republishes marked updated; false = legacy surface-once
recall_recent_events_hours = 48   # feed event-daily session summaries from the last N hours into the slow path's recall background (lowest priority, shares the main budget last); 0 = off
recall_max_chars = 2600           # #611: shared char budget for assemble_background's layers (schema_prior→scene→behavior→fact→semantic→keyword→events). 1200→2400 (#647: fact squeezed ~66%), then 2400→2600 after the dead-semantic-layer fix: with the semantic layer working it's now the LARGEST decision layer (~1610 chars/session per the recall_budget_sweep), so the decision-demand ceiling rose ~835→~2261; 2600 fits scene+fact+semantic at ~0 squeeze (was ~5% at 2400) and the keyword-fallback dilution zone only starts ~2600 — smallest cap that holds the now-real demand, still far under the 4800 dilution risk. Faithful A/B on the fixed layer = no-harm
cooldown_enabled = true           # #533 (kind, scope) 级闭集硬冷却: a kind dismissed >= cooldown_dismiss_threshold times within cooldown_window_days IN THE SAME SCOPE enters a HARD cooldown — that (kind, scope)'s intents are dropped at the sink (bypass prompt, covers fast/slow/meeting) for cooldown_hours from the latest dismissal (anchored on dismissed_at, not recognition ts). user_committed / high-confidence (>=0.9) intents are EXEMPT; every suppression is logged to cooldown_suppressions telemetry. Upgrades the prompt-soft-only negative prior into a deterministic gate (弹错=复利损失). false = restore prompt-soft-only
cooldown_window_days = 1          # #533: lookback window (days) for counting a (kind, scope)'s dismissals — kept SAME-magnitude as cooldown_hours so a sparse-over-a-week feedback-giver is not near-permanently cooled
cooldown_dismiss_threshold = 3    # #533: dismissals of one (kind, scope) within the window needed to trigger the hard cooldown
cooldown_hours = 24.0             # #533: cooldown duration measured from the MOST RECENT dismissal — always expires (no lifetime ban; re-calibration is #534). <=0 disables defensively
content_fold_fuzzy_enabled = true # 重复推送相同语义修复: the ungrounded content fold (sink._find_content_fold_target) folds info_need/reminder/assignment by EXACT normalized body equality; wording drift每 session 让同一件 to-do 落成多条 open 行（生产实测一件事 6 条）. When on, the fold ALSO collapses bodies whose char-bigram Jaccard >= content_fold_similarity (both bodies long enough; short stay exact-only) — deterministic, zero-LLM, governed by the golden-set gate. false = exact-only (pre-fuzzy)
content_fold_similarity = 0.72    # 重复推送相同语义修复: char-bigram Jaccard threshold for the fuzzy content fold. Conservative — distinct same-topic facts must NOT fold (golden negatives pin the floor); only near-identical re-statements clear it
semantic_fold_enabled = false     # 语义去重: on a char-bigram MISS, second-chance fold via sentence-embedding cosine (intent.embeddings, bge-small-zh). DEFAULT OFF (opt-in): a one-keyword-apart DISTINCT to-do sits in the same cosine band as a genuine paraphrase (adversarial review: test_dedup_rework), so a deterministic cosine fold over-folds the 误吞=0 guards. The semantic mid-band is the LLM's job (recognizer dedup_against_open + app-side re-push judge). Flip on only where recall > precision
semantic_fold_similarity = 0.82   # 语义去重: cosine threshold for the embedding fold. HIGHER than Jaccard's 0.72 — cosine is dense (two same-topic CN work strings already ~0.5–0.7); golden negatives pin this floor
dedup_against_open = true         # 语义去重 Layer 2: feed today's still-open intents (cross-scope) into the slow recognizer prompt + instruct the LLM not to re-emit a semantically-existing commitment — stops the duplicate before it's generated. false = recognizer doesn't see the existing-open checklist
auto_close_resolved_enabled = false   # 识别即更新状态: evidence-driven auto-close of stale open intents → terminal 'resolved' status (event-driven, no new poll). DEFAULT OFF (kill-switch): off = resolutions parsed but never acted on. Two signals (LLM resolution + deterministic fold-matcher) must agree before a close; a wrong close drops a real todo
auto_close_min_confidence = 0.85      # 识别即更新状态: confidence floor for an evidence auto-close (independent of recognition confidence)
auto_close_committed_min_confidence = 0.95  # 识别即更新状态: higher floor to close a user_committed promise (and only outcome=done)

[memory_delta]
# Session-end memory_delta consolidator (Memory-rebuild Phase 0, spec §4.1/§6.2):
# ONE LLM read of the just-ended session → structured {entities, assertions,
# relations, events} persisted SHADOW into the memory_deltas table. Consumers:
# `persome delta-report` + the Phase-1 dual-run parity eval (retires the four
# scattered extractors once parity is reached).
enabled = false                   # default OFF — flip true to start the shadow dual-run
max_blocks = 120                  # upper bound on session blocks fed to the model
roster_max = 60                   # known-identity roster entries injected (选择题 — refs or explicit new_entity, never bare store-probing strings)
min_confidence = 0.5              # deterministic parse gate: items below are dropped

[memory_decay]
# 文本轴分级遗忘（memory-rebuild §1.5-5；spec 2026-07-03-text-axis-graded-forgetting-design.md）：
# 夜间（23:55 尾部）有界降精——老（after_days）且从未被检索强化（entry_retrieval_stats=0，
# 读即强化=免疫）且未被保护（conflicted 待人裁/decayed:2 地板/非事实前缀）的持久条目，按文件
# 聚簇经一次 LLM 蒸馏成粗摘要（decayed:1）→ 再老再弱降成一行（decayed:2=地板）。降精走既有
# choke-point 动词：摘要 append 带 abstracted-from 收据，源条目 strike 退役（md 原文仍在=收据）。
# 反幻觉闸：提及子集/收缩上限/非空——任一不过整簇保留。默认 OFF（有损变换+夜间 LLM 成本）。
enabled = false
after_days = 90                    # 小于此天数的条目永不触碰
max_clusters_per_night = 3         # 每晚 ≤N 次 LLM 调用，最老簇优先
cluster_min = 4                    # 同文件 old∧weak 细节少于此数不蒸馏（噪声非压缩）
cluster_max = 12                   # 单簇上限（模型上下文护栏）
shrink_ceiling = 0.5               # 摘要须 < 源总长 × 此系数
line_max_chars = 80                # L2 一行档硬上限


[skill_check]
enabled = true                    # detect skill matches inside the per-minute timeline LLM call
confidence_floor = 0.65           # minimum confidence to record a skill_hint


[schema]
enabled = true                    # D2 schema miner daily tick: induce predictive schema-*.md priors from durable facts
daily_tick_hour = 0               # local-time hour for the daily schema tick (after safety-net 23:55)
daily_tick_minute = 15            # local-time minute for the daily schema tick
cross_domain_enabled = true       # Hy-Memory cross-domain sweeper: collide topic-far/behavior-near schemas (no embedding); ON — low-quality fusions are born forming (not injected), only stable ones bias recognition
cross_domain_behavior_max_distance = 0.5  # behavior-distance ceiling for the deterministic pre-filter (≤ == behavior-near)
cross_domain_min_confidence = 0.6 # fused cross-domain schema below this confidence is born forming (not injected)

[debug_hud]
# What the debug HUD (always-on-top panel, shown in debug mode) renders.
# Allowlist over content blocks — the panel shows ONLY what's listed.
# Keys: intent / tool_call / thinking / stage (AGENT ACTIVITY event kinds),
#       health (daemon health + counts), memory (recent memory writes).
# Default is intents only; add keys to surface more. Applied live (no restart).
show = ["intent"]
"""


def write_default_if_missing(path: Path | None = None) -> bool:
    path = path or paths.config_file()
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_CONFIG_TEMPLATE)
    return True

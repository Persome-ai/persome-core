# Configuration

Runtime config lives at `~/.persome/config.toml` (or `$PERSOME_ROOT/config.toml`). It's created with sensible defaults the first time you run `persome status`.

View the resolved config any time with:

```bash
persome config
```

## `[models.*]` — LLM per stage

Every LLM stage calls the **Anthropic Messages API via the official SDK** (the same path chat uses; litellm was removed). The backend speaks **only the Anthropic protocol** — Anthropic's official endpoint, or any Anthropic-compatible gateway (e.g. DeepSeek's `/anthropic`) pointed at by `ANTHROPIC_BASE_URL`. `model` is a **bare name** sent verbatim to that gateway; legacy `anthropic/...` prefixes are tolerated (stripped). Credentials live in `~/.persome/env` (`ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL`), **not** in this file.

```toml
[models.default]
model = "deepseek-v4-flash"   # bare name; sent verbatim to ANTHROPIC_BASE_URL
# base_url = ""               # per-stage override of ANTHROPIC_BASE_URL (rare)

[models.timeline]     # short-window normalizer — runs constantly, keep cheap but not weak
# inherits from default

[models.reducer]      # session → event-daily entry
# model = "claude-haiku-4-5"  # a stronger model, if your gateway serves it

[models.classifier]   # durable-fact extraction via tool calls
# Accuracy-sensitive; a weak model here poisons dedup.

[models.compact]      # file compaction — accuracy matters
```

Each stage section **inherits every field** from `[models.default]` and overrides only what it sets. If you want a single model everywhere, set `[models.default]` and leave the rest empty.

### Bring your own key — which model name goes with which endpoint

The shipped `[models.*]` defaults are `deepseek-v4-flash` (a bare gateway name); they are kept for gateway compatibility and existing installs. Pick the pairing that matches your credentials — both are just `ANTHROPIC_API_KEY` (+ optional `ANTHROPIC_BASE_URL`) in `env`:

| Endpoint | env | `model` |
|---|---|---|
| Official Anthropic | `ANTHROPIC_API_KEY` only (leave `ANTHROPIC_BASE_URL` unset) | bare claude names, e.g. `claude-haiku-4-5` |
| DeepSeek's Anthropic gateway | `ANTHROPIC_API_KEY` = your DeepSeek key, `ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic` | bare DeepSeek names, e.g. `deepseek-v4-flash` (the shipped default) |
| Any other Anthropic-compatible gateway | key + `ANTHROPIC_BASE_URL` per the gateway's docs | the bare name the gateway serves |

Sanity-check the whole install (env file perms, key present, base URL reachable, Swift helpers, AX trust, data root, port) with `persome doctor` — offline, zero LLM calls, exits 1 on any ✗.

Stage → purpose:

| Stage | Runs | What it does |
|---|---|---|
| `timeline` | every 60s while captures exist | Normalizes a short (default 1-min) capture window into a list of activity records with authored text preserved verbatim. |
| `reducer` | on session end + daily safety net | Turns a session's timeline blocks into one event-daily entry with time-ranged sub_tasks. |
| `classifier` | after each successful reducer run | Reads the just-written entry + context, extracts durable facts into user-/project-/tool-/topic-/person-/org- files via a tool-call loop. |
| `compact` | after commits that flag files | Rewrites a fat file; rejects if >5% noun-phrase loss. |

> **Sidebar title generation is NOT a `[models.*]` stage.** It piggy-backs on `[chat] model` via the Anthropic SDK (`chat.agent.complete_sync`), so any user who can chat at all has a working title generator without configuring an extra `[models.*]` entry. See [api.md → Session title generation](./api.md#session-title-generation).

### Local / self-hosted models

The backend speaks **only the Anthropic Messages API**, so a local model must sit behind an **Anthropic-compatible endpoint** — e.g. an Anthropic-API-shaped proxy in front of Ollama / vLLM. Point `ANTHROPIC_BASE_URL` at that proxy and set `model` to the bare name it serves. A raw Ollama `/api/chat` (Ollama/OpenAI shape) is **not** directly usable.

Things to check before trusting a local setup:

- **Tool-calling support is required** for `classifier` / `dream` / `pattern_detector` / `active` / `consolidator` — the endpoint must implement Anthropic tool use (`tool_use` / `tool_result` blocks). Weak models poison dedup.
- **Context window.** Timeline blocks are 1-min, reducer flushes consume ~5 blocks, a 2-hour session can stack ~24 blocks. Make sure the served context is ≥ 16 k for `timeline`, ≥ 32 k for `reducer` / `classifier`.

## `[capture]`

```toml
[capture]
event_driven = true                  # consume mac-ax-watcher events
heartbeat_minutes = 10               # periodic capture even when nothing happens (0 disables entirely)
debounce_seconds = 3.0               # AXValueChanged bursts collapse to one capture
min_capture_gap_seconds = 2.0        # hard floor between consecutive captures, regardless of event reason
dedup_interval_seconds = 1.0         # same-event-type dedup window
same_window_dedup_seconds = 5.0      # non-focus-change events in the same bundle+window are dropped if within this gap
buffer_retention_hours = 168         # 7 days; stale absorbed captures past this are deleted
screenshot_retention_hours = 24      # after 24h, strip screenshot (77% of bytes) but keep AX+text
buffer_max_mb = 2000                 # hard ceiling (MB); oldest absorbed files evicted first (0 disables)
include_screenshot = true
screenshot_max_width = 1920
screenshot_jpeg_quality = 80
ax_depth = 100                       # Electron apps need deep trees; 8 only reaches chrome
ax_timeout_seconds = 3
cmux_source_enabled = true           # frontmost cmux → append real terminal text via its local socket RPC
```

Tuning notes:

- **`ax_depth`.** Native Cocoa apps are fine at 20. Electron apps (Claude Desktop, VS Code, Slack, Notion) put user content past layer 20 — stay at 100 unless you're CPU-constrained.
- **`debounce_seconds`.** Lower = more captures during typing; higher = fewer near-duplicates.
- **`same_window_dedup_seconds`.** When the user types for a long time in the same document, this is the knob that decides how frequently you re-capture the same (bundle, window) pair. Focus changes always bypass this.
- **`heartbeat_minutes`.** Periodic capture as a safety net. `0` disables it completely (watcher-only). Values `>0` are clamped to a 60s floor.
- **`buffer_retention_hours`.** Whole-JSON deletion cutoff. Default 7 days lets `read_recent_capture` reach back that far — shrink to a few hours if you only care about the current work session, bump if you want longer recall.
- **`screenshot_retention_hours`.** After this many hours the screenshot field is stripped (rest of the JSON stays). Screenshots aren't used by timeline / reducer / classifier today — setting this ≪ `buffer_retention_hours` is what makes long retention cheap. `0` or very large values keep screenshots for the full window.
- **`buffer_max_mb`.** Hard ceiling in MB. When exceeded, the cleanup pass evicts oldest absorbed files until under. Set to `0` to disable (pure time-based retention).
- **`cmux_source_enabled`** (#558). cmux renders terminals on the GPU and exposes ~no AX text, but ships a local unix-socket RPC (`~/Library/Application Support/cmux/cmux-<uid>.sock`). When the frontmost bundle is `com.cmuxterm.app`, `capture/cmux_source.py` reads the visible terminal surfaces (`system.tree` + `surface.read_text`) and appends the lossless terminal text to the capture's `visible_text`; success skips the OCR fallback for that window. Default **on** — safe-by-construction: read-only local socket, zero external cost, one sub-second deadline for the whole conversation, and any failure (cmux not running, hung socket, protocol drift) silently degrades to the AX-only capture with a rate-limited warning.

## `[timeline]`

```toml
[timeline]
window_minutes = 1                # wall-clock aligned (:00/:01/:02/...)
cold_lookback_minutes = 30        # on first run, at most backfill this far
recent_context_blocks = 720       # ~12h of 1-min blocks; consulted by tooling
```

Timeline is always-on and acts as a **verbatim-preserving normalizer** — it de-duplicates snapshots and strips UI chrome but preserves the user's typed text, URLs, titles, and proper nouns unchanged. Real compression happens in the reducer.

`window_minutes` is effectively locked in once blocks exist — changing it later produces new-sized blocks going forward, but old blocks keep their original boundaries (they're keyed by `(start_time, end_time)`). The default 1-min size pairs with the reducer's flush tick (default 5-min) so each flush consumes ~5 blocks. A larger timeline window cuts LLM calls per hour but risks the model sliding from normalization into summarization.

## `[session]`

```toml
[session]
gap_minutes = 5                 # hard cut: idle > 5 min ends the session
soft_cut_minutes = 3            # soft cut: single unrelated app > 3 min
max_session_hours = 2           # forced cut at 2h
tick_seconds = 30               # check_cuts() interval
flush_minutes = 5               # incremental reducer tick inside an active session (min 5)
```

See [session.md](session.md) for what each rule means and how to tune it.

**Flush ticks.** While a session is still active, every `flush_minutes` the reducer wakes up and compresses any new closed timeline blocks into a partial entry in today's `event-YYYY-MM-DD.md`. This makes long sessions visible in near-real-time instead of waiting for the final cut. Minimum effective value is 5 (clamped) to keep LLM cost bounded — at the default 1-min timeline window, a 5-min flush consumes ~5 blocks. The classifier runs on its own separate cadence (see `[classifier] interval_minutes` below) and does not fire per flush.

## `[reducer]`

```toml
[reducer]
enabled = true                   # run S2 reducer on session end + daily safety net
daily_tick_hour = 23             # local-time hour for the daily safety-net tick
daily_tick_minute = 55
```

Setting `enabled = false` disables both the S2 reducer and the classifier. Sessions still close and persist to the `sessions` table, but no event-daily entries or classifier writes land — useful for capture-only debugging.

## `[classifier]`

```toml
[classifier]
interval_minutes = 30           # durable-fact extraction cadence inside active sessions (min 5)
```

While a session is active, the classifier wakes up every `interval_minutes` and extracts durable facts from event-daily entries written since its last pass. The terminal reduce (at session end) runs one more classifier pass over whatever trailing window the tick didn't reach, so nothing is lost between the final tick and the session close. Each pass advances the session's `classified_end` bookmark so entries are never double-classified.

Values `< 5` are clamped to 5 to keep LLM cost bounded. Pair with `[session] flush_minutes`: the reducer flushes at a higher frequency than the classifier, so a classifier tick always has fresh entries to look at.

## `[intent_recognizer]` — Hy-Memory recall/schema flags

The trajectory recognizer and capture fast-path knobs live here (`enabled`, `fast_path`, `backoff_*`, `domain_allowlist`). The flags below are the **Hy-Memory migration** recall/schema toggles (default **on**). They only change the intent recognizer's recall background / schema prior — never the MCP `search` contract. The `recall_use_chain_index` / `recall_read_evo_nodes` staging flags were retired with `entry_chain` in PR-7 (the SSOT cutover): the chain-head fold has exactly one path — `evo_nodes`; leftover keys in an old `config.toml` are silently ignored by the loader.

```toml
[intent_recognizer]
recall_fold_superseded = true    # fold recall hits to evolution-chain heads, read from evo_nodes — the SSOT (§1.4); event-* entries (Q2, never in evo_nodes) keep the superseded-column judgment; pre-backfill (evo_nodes missing/empty) degrades to the equivalent superseded-column fold
recall_chain_trail = true        # append the `← [曾]/[精炼自] …` evolution trail to a chain head (attitude-evolution signal), rendered from the evo_nodes bidirectional pointers; only renders when the fold is on AND evo_nodes is ready
schema_prior_enabled = true      # inject D2 predictive-schema inertia priors (schema-*.md `expected_inferences`, stable-status only, top-8 by confidence) as the highest-priority recall section; [] no-op until schema files exist
schema_feedback_enabled = true   # R4 schema-level feedback loop: HUD dismiss/accept on an intent flows back onto the schemas injected when it was recognized (Intent.schema_sources provenance) — dismiss −0.05 / consume +0.03, stable↔forming flip on the 0.6 threshold via the miner's supersede_entry seam; intents without schema_sources are a strict no-op
recall_include_confidence = true # annotate recall hits with ⚠(低置信)/⚠(冲突未裁决) from the entry_metadata meta-cognition index, so the recognizer down-weights shaky memories; default ON for everyone (safe-by-construction: existing memories have no confidence tag → no annotation, so it only affects new classifier-written memories)
event_intent_enabled = true      # Hy-Memory L7: prospective "下次打开 X 时" intents stored armed, fired armed→open by the per-capture activator (MVP trigger: app_opened); default on — per-capture cost is one indexed armed-lookup
slow_path_max_blocks = 60        # R3 慢路增量化 cost gate: most recent N session blocks render verbatim in the slow-path prompt; older blocks fold into ONE deterministic header line (no LLM). 0 = unbounded legacy prompt (byte-identical)
slow_pregate = true              # #547 慢路锚定 pre-gate: skip the slow-path LLM call when the blocks NEW since the last tick carry no slow anchor (SLOW_ANCHOR_RE = fast _ANCHOR_RE + euphemism/willingness cues); skips recorded as recognition_ticks outcome=skipped_no_anchor (hit_rate unpolluted). false = every block flush burns one LLM call (legacy)
material_republish = true        # R3 material-change-republish: dedup-hit re-recognition with a material change (confidence ratchet >= +0.15 / provenance counterpart_proposed→user_committed) UPDATEs the stored row (id+status kept, dismissed/consumed never resurrected) and republishes it on SSE marked `updated`; false = legacy surface-once
recall_recent_events_hours = 48  # WorkThread S0 基线: feed event-daily session summaries (the reducer's "continued…" narration) from the last N hours into the slow path's recall background as a LOWEST-priority「近期活动」section sharing the main budget last; telemetry layer `events` in recall_budget_ticks; 0 = off (byte-identical)
recall_workthread = true         # WorkThread S3 工作线层: inject the active thread (+ ≤1 background thread) as a「当前工作线」section right after schema_prior, with an INDEPENDENT 200-char budget (stacks ON TOP of recall_max_chars — it can never squeeze the main layers); threads below confidence 0.6 are not injected; telemetry layer `workthread`
recall_max_chars = 2400          # #611: shared char budget for assemble_background's main layers (schema_prior→scene→behavior→fact→keyword→events). 2026-06-10 ablation proved squeezing the DECISION-RELEVANT fact/behavior layers out collapses slow-path quality and set the action gate (>10% fact-layer squeeze → raise to 2400); the #647-corrected recall_budget_ticks telemetry on real traffic showed fact-layer squeeze at ~66% of calls (6.6× the gate) — the squeeze hits the high-value layer, NOT the cheap keyword/events tail the issue assumed — so the default rose 1200→2400. NOT a free dial: decision-layer demand has a long tail (p90 ~3.5k chars on the measured corpus), 喂得多≠更好 (over-filling dilutes the prompt + burns volatile-segment tokens with no cache hit; the ablation found no benefit beyond 2400, a dilution risk at 4800). The residual tail is a deliberate capacity tradeoff (漏低优先层 = 有限损失). The workthread independent budget stacks on top, so the true ceiling with workthread on is recall_max_chars + 200
cooldown_enabled = true          # #533 (kind, scope) 级闭集硬冷却: a kind dismissed >= cooldown_dismiss_threshold times within cooldown_window_days IN THE SAME SCOPE enters a HARD cooldown — that (kind, scope)'s intents are dropped at the unified sink (bypass prompt, covers fast/slow/meeting) for cooldown_hours from the latest dismissal (anchored on dismissed_at, not recognition ts); user_committed / confidence>=0.9 intents are EXEMPT; every suppression is logged to cooldown_suppressions telemetry. false = restore prompt-soft-only
cooldown_window_days = 1         # #533: lookback window (days) for counting a (kind, scope)'s dismissals — SAME-magnitude as cooldown_hours so an active feedback-giver isn't near-permanently cooled over sparse dismissals
cooldown_dismiss_threshold = 3   # #533: dismissals of one (kind, scope) within the window needed to trigger the hard cooldown
cooldown_hours = 24.0            # #533: cooldown duration measured from the MOST RECENT dismissal — always expires (no lifetime ban; re-calibration is #534). <=0 disables defensively
```

Rollback is per-flag: set any back to `false` to drop that signal without losing the others. `entries`/`entry_metadata` and `schema-*.md` are derived retrieval state — `rebuild_index` re-derives them from the current write authority's truth (evo_nodes under evomem authority, markdown tags otherwise), so a flag flip never corrupts memory.

`schema_feedback_enabled` closes the **R4 schema-level feedback loop** (design-philosophy §7 「拒绝是金矿」): the recognizer stamps each produced intent with the `schema-*.md` files whose inferences were injected that round (`Intent.schema_sources`, coarse "当时在场" provenance), and `update_intent_status` — the single seam both HUD write-back entry points (MCP `set_intent_status`, REST `PATCH /intents/{id}`) funnel through — flows a real status transition back onto those schemas: dismissed → confidence −0.05, consumed → +0.03 (clamped to [0,1]; repeated same-status writes are idempotent, so a double HUD click never double-decays). When the confidence crosses the 0.6 stable threshold the schema entry is superseded in place through the same `supersede_entry` seam the miner uses (reason `intent feedback: dismissed/consumed`, body unchanged, tags rewritten), flipping `stable↔forming` — a forming schema automatically exits the stable-only prior injection gate. **Default on** (safe-by-construction): intents with no `schema_sources` change nothing, the write-back is best-effort and never blocks the status update, and a wrongly-decayed schema recovers via consumed feedback or the next re-mine.

`slow_path_max_blocks` is the **R3 slow-path cost gate**: the trajectory recognizer re-reads the whole active session on every block flush (~60s), so the prompt grows linearly to the 2h session cap (≈120 blocks) and re-sends the same bytes 100+ times. Past the cap, only the most recent N blocks render verbatim in the「本会话事件日志」section; older blocks collapse into one deterministic summary line (「更早 X 个 block（HH:MM–HH:MM）已省略，涉及 app：…」— pure string assembly, no LLM call). **Default 60 is safe-by-construction**: a block only ages out after ~N consecutive flushes already fed it verbatim, by which time any intent it carried is persisted (dedup'd) and keeps re-surfacing via recall's scene layer in the background section. Set `0` to restore the unbounded legacy prompt byte-for-byte.

`slow_pregate` is the **#547 slow-path anchored pre-gate**: the trajectory recognizer fires on every block flush, but production telemetry showed only ~14% of those ticks recognize anything — most minutes carry no schedulable signal, so ~86% of slow-path LLM calls were idle burn (the fast path has five cheap gates; the slow path had none). With this on (the default), `recognize_session` first scans the blocks **new since the last tick** (their `entries` plus the `focus_structured`/`focus_excerpt` verbatim backstops) with `event_source.SLOW_ANCHOR_RE` — the fast `_ANCHOR_RE` composed (single source, no drift) with euphemism/willingness cues (改天/回头/找个时间/到时候/见面/复盘/过一遍/串一遍…) so anchorless euphemisms, which the slow lane is the designated catcher for, are never gate-killed — and skips the LLM call when nothing hits. Only **new** blocks are scanned: older blocks were covered by the previous tick (LLM-read, or gate-proven anchorless — blocks are immutable once materialised), so the residual miss risk is bounded and arguable (设计哲学: 漏=有限损失). A skipped tick is still recorded in `recognition_ticks` with `outcome=skipped_no_anchor` — it advances the new-block bookmark and stays out of the `hit_rate` denominator (skips are "never ran", not "ran and recognized nothing"). Set to `false` to restore the legacy behavior (every block flush burns one LLM call).

`material_republish` is the **R3 material-change-republish** switch: previously a dedup hit was skipped outright, so a re-recognition with higher confidence or an upgraded provenance was silently dropped. With this on (the default), `intent/sink.py:material_change` compares the re-recognition against the stored row with deterministic rules — confidence ratchet (new ≥ stored + 0.15; the update writes the new confidence back, so it fires a bounded ≤~3 times per intent) or provenance upgrade `counterpart_proposed→user_committed` — and a material change UPDATEs the row in place (same id, `status` untouched: dismissed/consumed are final and never resurrected) then republishes it in the `intent_recognized` SSE frame with `"updated": true`. Non-material wobbles still skip — 宁可漏 republish 不可重复打扰. Note「when_text 模糊→具体」(e.g.「周五」→「周五15:00」) never reaches this comparison: normalized `when_text` is part of `dedup_key`, so added specificity yields a new key and inserts a new row.

`event_intent_enabled` is the **event-based prospective intent layer** (Hy-Memory L7): when the recognizer marks an intent `activation=on_event` (the user said "下次打开 X 时再…"), the sink stores it `status="armed"` (kept out of the open stream the active layer reads) instead of surfacing it now. On every capture, `intent/activator.py` checks the frontmost app against armed intents' `fire_on="app_opened"` trigger and flips a match `armed→open`, so it surfaces at the moment the user actually opens that app — a 时机门, more restrained than push-on-recognize. **Default on**, cheaply: the per-capture cost is a single indexed `status='armed'` lookup (usually 0 rows), and an armed intent only exists when the user explicitly tied an action to "下次打开 X", so the surface is naturally narrow. Set to `false` to fully disable (no armed intents emitted, activator hook not installed). MVP trigger is `app_opened` only; `url_visit`/`keyword`/`time` are future additions on the same `fire_on` seam.

`recall_include_confidence` is the **meta-cognition layer** (Hy-Memory migration): the classifier records each memory's reliability as `#confidence:high|medium|low` / `#conflicted` heading tags (projected into the `entry_metadata` table). When this flag is on (the default), low-confidence and conflicted hits get a ⚠ note in the recall background so a guess never drives the same proactive action a hard fact would. It is **safe-by-construction** to ship on: pre-existing memories carry no confidence tag, so they produce no annotation — only new classifier-written memories are affected, and the only effect is to make the recognizer more cautious on shaky ones. Storage/write of the tags is always on (pure-additive, no flag); only the recall *rendering* is gated. Flip to `false` to fully suppress the annotation.

`cooldown_*` is the **#533 (kind, scope) 级闭集硬冷却** — the negative-feedback loop upgraded from prompt-soft to a deterministic hard gate (design-philosophy §2/§4 「弹错=复利损失，拒绝是金矿」). Before #533 a dismissed intent only rendered as a "最近被忽略 N 次" prior the model was *asked* to honor (`recognizer._dismissed_prior`); the lone hard block was an exactly-equal `dedup_key`, so a kind the user kept dismissing re-surfaced under a fresh wording (new key). Now `intent/cooldown.py` reads dismissals as a confidence vote: when a kind is dismissed **≥ `cooldown_dismiss_threshold` (default 3)** times within **`cooldown_window_days` (default 1)** **in the same `scope`**, that `(kind, scope)` enters a hard cooldown for **`cooldown_hours` (default 24)** measured from the **most-recent** dismissal — and the gate runs at `intent/sink.py:persist_intent_result`, the single write entrance every producer (fast K1 / slow trajectory / meeting pack) funnels through, so a cooled-down `(kind, scope)`'s intents are dropped (not persisted, not surfaced) **bypassing the prompt entirely**.

The cooldown clock anchors on **`dismissed_at`** — the instant the dismiss ACTION happened (`update_intent_status` stamps it; `ts` is recognition time, which the dismiss path leaves untouched). Anchoring on `ts` was the original blocking bug: a row recognized days ago and dismissed just now carries an old `ts` but a fresh `dismissed_at`, so a `ts`-window would both漏挡 (recent dismisses of old intents fall outside the window) and mistime the clock. The **#532 armed-TTL reaper** flips never-fired `armed` rows to `dismissed` directly (no `dismissed_at`), so it correctly does NOT feed the cooldown — a reminder the user never triggered isn't a rejection of the kind.

Two deliberate tightenings vs the first cut (avoid the「惩罚高反馈用户」陷阱 — the same trap the retired flat `_dismissed_prior` window fell into): **(1)** the cooldown is `(kind, scope)`, not global by-kind — dismissing reminders in one *scene* cannot mute them in a genuinely different scene; **(2)** `cooldown_window_days` defaults to **1** (same magnitude as `cooldown_hours`), not 7 — a wide 7-day window + sliding 24h reset would put an active feedback-giver who sparsely dismisses 3× over a week into near-permanent cooldown. Same-magnitude requires the 3 dismisses to cluster within ~a day.

**Scope dimension for the slow path** (`intent/cooldown.py:_scope_filter`): the cooldown counts dismissals per scene, but the slow trajectory recognizer stamps a **fresh `session-<uuid>` scope every session** (`scope_for_session`), so exact-`scope` matching would reset the count to 0 each session and the hard cooldown would *never fire on the slow path* — the欠抑 hole where #533 is needed most (slow re-recognition under new wording is exactly the failure mode). So all per-session `session-*` scopes **fold into one stable cross-session cooldown domain** (`scope LIKE 'session-%'`): dismissing the same kind across several sessions accumulates toward the threshold, and a fresh re-worded re-statement in yet another new session is dropped. The intents keep their true per-session identity scope in the `intents` table — only the cooldown COUNT folds. fast-K1 (constant `fast-K1` scope) and meeting packs (per-meeting stable `meeting-<id>` scope) are unaffected — neither matches `session-`, both stay exact-scope, so cross-scene isolation between genuinely-distinct scenes is intact.

**Confidence/provenance bypass** (宪法 §5 零熵猎场不该被否决): a verbatim `user_committed` promise — or any intent whose calibrated `confidence >= 0.9` (`CONFIDENCE_CAP_INFERRED`; only user_committed survives the clamp that high) — is **EXEMPT** from the hard cooldown. The闸 only suppresses the model's mid/low-confidence GUESSES at a rejected kind; it must never swallow a thing the user said in so many words (that would contradict the same PR's user_committed confidence-cap exemption). Inferred intents claiming higher are first clamped to 0.9, so the boundary is inclusive-by-design.

**Observability is never gated** (拒绝是金矿): a suppressed intent leaves no `intents` row, so each drop is recorded as a structured, additive trace in the `cooldown_suppressions` table (kind, scope, confidence, ts, cooldown_until) — surfaced under `/intents/stats` as `cooldown_suppressed` ({total, by_kind}) and read by the #534 recalibration. The presentation is闸掉的, the data is not.

The cooldown is **always time-bounded** — it expires `cooldown_hours` after the latest dismissal and self-heals once the user stops dismissing; it is never a lifetime ban (re-calibration / manual release is #534, out of this batch), and `cooldown_hours <= 0` disables it defensively so a misconfig can't become a permanent ban. **Default on, safe-by-construction**: it only ever fires AFTER the user has explicitly dismissed the same kind 3× within ~a day in one scope, the lookup is best-effort (a DB error fails open — 漏挡=有限损失, 硬挡真意图=复利损失), high-confidence promises bypass it, and it self-heals. Set `cooldown_enabled = false` to fully restore the prompt-soft-only behavior. Companion fix in the same change: recall's ① scene layer (`recall._scene_layer`) now filters out `dismissed`/`consumed` intents, which previously re-entered the recognition prompt as positive「场景意图」context and contradicted the same prompt's negative-prior section.

## `[thread_tracker]` — WorkThread 工作线层（"现在进行时"）

Spec: `docs/superpowers/specs/2026-06-12-workthread-design.md`. The pipeline compresses along the **time** axis only; WorkThread adds the orthogonal **identity** axis — folding scattered micro-sessions onto "the same undertaking" ("这一小时和昨天那两小时是同一件事"). The LLM tracker emits a six-op closed set (`open/attach/progress/merge/complete/none`); a deterministic executor (`workthread/executor.py`) runs them — spans-based time accounting (overlaps split evenly + `approximate` marker, no-spans attach counts zero, minutes never come from the model), an all-history semantic dedup gate that auto-revives dormant threads, hysteresis `active` competition (a challenger needs ≥60% of the window's span minutes), and `stale` harvesting after 30 days (pinned threads exempt; inactivity is never completion).

```toml
[thread_tracker]
enabled = true            # tracker stage master switch (executor / thread CLI stay usable when off)
window_minutes = 60       # aggregation window: run when the oldest queued session summary waited this long…
window_sessions = 5       # …or when this many summaries queued, whichever first (~8-12 LLM calls/day at the real 35-micro-session distribution)
disagreement_probe = true # H2 双模型分歧探针: second differently-prompted pass per window; disagreement = label-free uncertainty → down-weights touched threads' confidence + queues them for `thread review-day`
```

The tracker mounts **only** on the terminal-reduce callback (flush 路径不挂); the callback enqueues the session summary into `workthread_queue` and batch-runs per window. Consumers: the recall 工作线层 (`[intent_recognizer] recall_workthread`), the MCP `current_work_context` / `correct_work_thread` tools, and the `persome thread` CLI (`list` / `review-day` — the H1 day-labeling screen, the label factory / `correct` — the closed-set correction port (confirm/not_this/rename/merge/pin), every call mints a ground-truth label that calibrates thread confidence / `stats` — churn/revive/disagreement telemetry / `unfreeze` — re-allow opens after the churn freeze (7-day opens/attaches > 0.3 auto-freezes `open`) / `export-golden` — export an H1-labeled day as an S2 eval fixture skeleton). Telemetry honesty (spec §十): churn/revive are shape proxies only — the runtime quality claim rests on H1's daily ground truth.

## `[schema]` — D2 schema miner daily tick

Drives the `schema-tick` daemon task (see the daemon task table in `CLAUDE.md`). Once per local day it runs the D2 schema miner: clusters durable facts per memory file → induces `schema-*.md` predictive priors, which feed the intent recognizer's `schema_prior` seam (gated on `[intent_recognizer] schema_prior_enabled`). Scheduled just after `[dream]` (23:55) + the safety-net so it consumes freshly-classified facts. Gating-only — no schema is written until enough clustered facts exist.

```toml
[schema]
enabled = true          # run the daily schema-tick (off = no D2 schema mining)
daily_tick_hour = 0     # local wall-clock hour
daily_tick_minute = 15  # → fires at 00:15 local, after dream + daily-safety-net
cross_domain_enabled = true               # Hy-Memory cross-domain sweeper (default on)
cross_domain_behavior_max_distance = 0.5  # behavior-distance ceiling for the pre-filter
cross_domain_min_confidence = 0.6         # fused schema below this is born `forming`
```

`cross_domain_*` is the **cross-domain sweeper** (Hy-Memory batch 2): after the per-file miner runs, it pairs *stable* schemas, keeps those that are **topic-far but behavior-near**, and asks the LLM whether they collide into a higher-level schema (`schema-xdomain-*.md`, same消费链). The behavior dimension is a **deterministic** signature (app set + action-type distribution + hour histogram, traced from facts' `occurred_at` → `timeline_blocks`) — **no embedding**. Runs as the tail of the same `schema-tick` (no new daemon task). **Default on**, with a bounded downside: a low-quality collision gets a low LLM confidence → born `forming` → **not** injected into the recognizer prior (only `stable` ≥ `cross_domain_min_confidence` fusions are), so weak fusions can't pollute recognition. The main cost is the per-tick LLM probes, capped by the topic/behavior pre-filter. Set to `false` to disable.

## `[writer]`

```toml
[writer]
soft_limit_tokens = 20000        # compact trigger on any single file above this
hard_limit_tokens = 50000        # emergency ceiling
dedup_window_hours = 24          # dedup search horizon before appending
cold_start_conservative_hours = 0 # 0 = off
max_tool_iterations = 12         # classifier tool-call loop hard cap
```

The old per-capture trigger knobs are gone — the writer is driven by session boundaries now. See [writer.md](writer.md) for the full trigger model.

## `[memory]`

```toml
[memory]
auto_dormant_days = 30           # files untouched this long are marked dormant in the index
```

Dormant files don't show in `list_memories` by default. Pass `include_dormant=true` from the MCP client to see them. They're never deleted automatically.

## `[evomem]` — SSOT-switch survivability base

Survivability facilities for the **evomem SSOT switch** (design doc `docs/superpowers/specs/2026-06-10-evomem-ssot-switch-design.md` §3, PR-1). Once `evo_nodes` becomes the single source of truth, a corrupt DB means data loss — these are the hedge (snapshots + self-check + write-freeze), not an equivalent replacement for the markdown-replay self-heal. They must run stable in production *before* any truth migration (backfill / dual-write) lands (§3.5). Everything here is a side channel: with the flags off, the daemon behaves exactly as before.

```toml
[evomem]
snapshot_enabled = true            # daily VACUUM INTO backup/evo-YYYYMMDD.db at the 23:55 safety-net tick
snapshot_keep_daily = 7            # keep every daily snapshot from the last N days
snapshot_keep_weekly = 4           # additionally keep Monday snapshots from the last N weeks
integrity_check_enabled = true     # chain-invariant self-check at daemon startup + after each snapshot
freeze_writes_on_failure = false   # structural check failure freezes memory write paths (reads stay available)
shadow_write_enabled = true        # PR-3 双写影子期: mirror every markdown main write into evo_nodes (backfill 单条版; auto-deactivates under write_authority="evomem")
write_authority = "markdown"       # PR-6b 写权反转开关: "markdown"(默认, 现状) | "evomem"(反转)
```

The `dual_read_check_enabled` flag (PR-4 dual-read reconciliation) was retired together with `entry_chain` and the cutover dashboard in PR-7 — the cutover happened, so there is no second chain store left to reconcile against. A leftover key in an old `config.toml` is silently ignored by the loader.

- **`snapshot_enabled`.** At the tail of the daily-safety-net tick (23:55), right after the existing `PRAGMA wal_checkpoint(TRUNCATE)` (so the snapshot reads a fresh main DB), the daemon takes a `VACUUM INTO` online snapshot of `index.db` into `backup/evo-YYYYMMDD.db`. The snapshot lands in a `.tmp` first, is verified with the §3.3 check suite, and is only then atomically promoted — **a snapshot that fails verification alerts (`integrity_alert` SSE event) and is discarded; it never overwrites an existing good snapshot.** Retention: every daily from the last `snapshot_keep_daily` days, plus Monday snapshots from the last `snapshot_keep_weekly` weeks; everything older is deleted automatically.
- **`integrity_check_enabled`.** Runs the chain-invariant self-check (`evomem/integrity.py`) at daemon startup and after each daily snapshot: `PRAGMA quick_check`, bidirectional pointer symmetry, anti-fork (≤1 successor), head consistency (`is_latest=1` ⇒ no successor + active; ≤1 head per chain), acyclicity, and projection reconciliation (`{evo_nodes is_latest=1 ∧ active} ≡ {entries.superseded=0}`; the entry_chain edition retired with the table in PR-7). Every pass records its real finding count into `integrity_check_runs` (pure audit trail). Every failure alerts via `logger.error` + an `integrity_alert` SSE event; projection mismatches are alert-only (they're the self-healable side — `rebuild_index` replays the retrieval projection from the authority's truth).
- **`freeze_writes_on_failure`.** When a STRUCTURAL check fails, freeze every memory write path (markdown writers in `store/entries.py` and the evomem `NodeStore`) until a human decides — reads stay available, and there is **no automatic recovery**. Default **off** on purpose: under markdown authority `entries` is still a projection that `rebuild_index` can re-derive, so freezing production writes on a projection-era false positive would be all cost. Flip it on once you run with `write_authority="evomem"` (or to rehearse the freeze drill).
- **`shadow_write_enabled`.** PR-3 shadow dual-write (design §4.2 双写影子期). After every markdown main write — the three choke-point write paths in `store/entries.py` (`append_entry` / `supersede_entry` / `mark_entry_deleted`) that all write stations converge on — `evomem/shadow.py` incrementally mirrors the affected entries into `evo_nodes` via the **same** mapping the PR-2 backfill uses, keeping the backfilled state fresh (invariant: incremental shadow state == a full backfill rerun, field-for-field). Markdown stays the SSOT and the shadow is disposable: failures/skips **never** roll back or block the main write — they log a warning and bump a cumulative miss counter that emits an `integrity_alert` (check=`shadow_write_lag`, alert-only, never freezes) every 5 misses. Default **on** (safe-by-construction). Cold start: while `evo_nodes` is empty/missing (backfill not yet run) every shadow write is a warned skip with no alert — run `persome evomem-backfill` once to start the shadow phase for real; a chain whose endpoints are missing/stale in `evo_nodes` is also skipped whole-batch (no half-built chains, no one-sided pointers). A lagging shadow — including after a `compact` whole-file rewrite, the one write site that bypasses the three paths — is always repaired by re-running the idempotent backfill.
- **`write_authority`.** PR-6b write-authority inversion (design §4.4) — WHO is the truth on the write side. `"markdown"` (default): status quo — every write station lands on the markdown main write paths in `store/entries.py`, the shadow hook mirrors into `evo_nodes`, markdown is the SSOT; the code default never flips (P0). `"evomem"`: the inversion — the same write verbs are dispatched at the choke point through the evomem engine (`evomem/inversion.py`): `evo_nodes` is the truth (single-transaction atomic write), the `entries`/`entry_metadata`/`entry_temporal` tables become the FTS retrieval projection (`superseded = 0` iff `is_latest=1 AND status='active'`, maintained synchronously via the SAME derived-row helpers; the `entry_chain` leg retired in PR-7), and `memory/*.md` becomes a best-effort human-readable projection regenerated per write (frontmatter carries a `projected:` marker; failures warn + count + alert `check=markdown_projection_lag`, never roll back the truth write; repair = `persome evomem-project-markdown --live`). The shadow hook auto-deactivates (its direction is reversed); `event-*.md` (Q2) and `skills/` subdir files keep the legacy markdown path; compact is deferred (returns a `deferred` note — compact-as-ops via the engine is a follow-up PR). Manual edits to projected files are detected daily (content hash vs `projection_state`) and alert `check=manual_edit_detected` — reimport with `persome evomem-import-markdown <file>`; there is deliberately **no** automatic mtime reimport (Q1c rejected — it would be a second write authority). Flip to `"evomem"` only by hand, after PR-5 (主读) has been stable ≥1 week. Rollback (§6) = flip back to `"markdown"` — legacy write paths and the shadow hook resume as-is; run `evomem-project-markdown --live --force` first to flush inversion-era writes into markdown, then `persome rebuild-index`.

## `[search]`

```toml
[search]
default_top_k = 5
filter_superseded_by_default = true
```

Both apply to MCP `search` calls. Superseded entries are still searchable with `include_superseded=true`.

## `[mcp]`

```toml
[mcp]
auto_start = true                 # run an always-on MCP server inside the daemon
transport = "streamable-http"     # "streamable-http" | "sse" (deprecated) | "stdio"
host = "127.0.0.1"                # keep localhost-only
port = 8742
```

- `streamable-http` — default. Served at `http://<host>:<port>/mcp`.
- `sse` — legacy. Still works but deprecated.
- `stdio` — don't set this in the daemon config; stdio is for per-client spawns via `persome mcp`.

## `[debug_hud]`

Controls what the **debug HUD** renders — the always-on-top panel shown when debug mode is enabled in the app.

```toml
[debug_hud]
show = ["intent"]                 # allowlist of content blocks to render
```

`show` is a single allowlist; the HUD renders **only** the keys listed. Valid keys:

| Key | Shows |
|---|---|
| `intent` | recognized intents (kind + confidence + rationale) — **default** |
| `tool_call` | agent tool calls (name + arguments) |
| `thinking` | agent reasoning text (`llm_text`) |
| `stage` | pipeline stage start/end (classifier, dream, reducer, …) |
| `health` | daemon health + uptime / sessions / memory counts |
| `memory` | most recent memory writes |
| `workthread` | the current work thread + its one-click correction chip（WorkThread S4，spec 2026-06-12 §六-3：✓ confirm / ✕ not_this / 📌 pin；数据走 `GET /work/context` + `PATCH /work/threads/{id}`） |

`intent` / `tool_call` / `thinking` / `stage` are event kinds inside the **AGENT ACTIVITY** feed; `health` / `memory` / `workthread` are separate panels. Default is `["intent"]` so the HUD is quiet — add keys to surface more, e.g. `show = ["intent", "tool_call", "thinking", "health"]`.

Applied **live**: the HUD reads this via `GET /config/debug-hud`, which re-reads `config.toml` on each call, so changes take effect without restarting the daemon (within the HUD's poll interval).

> **You don't need to hand-edit this.** The HUD has a **gear button** (top-right) that opens an in-place checklist of these blocks — ticking a box writes `show` for you via `PUT /config/debug-hud`. Editing the TOML directly still works and is equivalent.

## Top-level toggles — actuation

The actuation layer (computer-use act verbs over macOS AX — `ui_click` / `ui_type` / … MCP tools) is governed by top-level TOML keys (they must sit **above** the first `[section]` header):

```toml
actuation_enabled = false        # default OFF for the open-source release; opt-in
actuation_show_boxes = false     # element-bbox overlay while actuating
actuation_glow_enabled = true    # takeover glow + badge (only relevant when actuation is on)
```

Even with `actuation_enabled = true`, every state-mutating verb goes through a per-action confirm round-trip (no reply within the timeout = deny), and the flag itself is the kill switch.

## Environment overrides

- `PERSOME_ROOT=/some/path` — move `~/.persome/` entirely. Good for tests, throwaway envs, or separating work and personal memory.
- `OPENAI_API_KEY` (or whichever `api_key_env` you set) — picked up at runtime.
- `PERSOME_DISABLE_OCR=1` — **kill-switch** for all on-device OCR inference. The bundled PaddlePaddle can SIGSEGV *during* inference (a native fault, see issues #335/#218); because OCR runs on an in-process daemon thread, that fault takes the whole daemon down. Setting this disables OCR entirely at deploy time **without a config rebuild** — paddle is never imported, and the daemon degrades to "no OCR text for AX-poor apps (WeChat/Feishu/…)" instead of crashing. Equivalent to `capture.enable_ocr_fallback = false` but flippable via env without touching `config.toml`. (Truthy: `1`/`true`/`yes`/`on`.) Subprocess isolation of the OCR crash domain is the planned root fix.

## Validating changes

The daemon reads config once on startup. After editing `config.toml`:

```bash
persome stop && persome start
persome status
```

`status` prints the resolved model for each stage **and probes each stage's provider** with a tiny round-trip (`max_tokens=4`, ~5s timeout). Each row shows one of:

- `gpt-5.4-nano   ✓ 234 ms` — provider answered.
- `claude-haiku-4-5   ✗ AuthenticationError: …` — provider rejected the request. Typos in `model`, missing `api_key_env`, wrong `base_url`, or expired keys all show up here on the first `status` call instead of silently failing inside the writer hours later.

Probes for stages that share an identical `(model, base_url, api_key)` are deduplicated, so the common case (one model for all four stages) makes one network call. Run them in parallel and the whole status command stays under ~5s even if one provider is slow.

To skip the network round-trip — e.g. on a flight, in CI, or just to inspect the resolved config — set the mock env var:

```bash
PERSOME_LLM_MOCK=1 persome status
# rows show: ✓ mocked
```

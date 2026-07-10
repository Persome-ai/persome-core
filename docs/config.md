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

- **Tool-calling support is required** for `classifier` / `pattern_detector` / `active` / `consolidator` — the endpoint must implement Anthropic tool use (`tool_use` / `tool_result` blocks). Weak models poison dedup.
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

## `[schema]` — D2 schema miner daily tick

Drives the `schema-tick` daemon task. Once per local day it clusters durable
facts per memory file and induces `schema-*.md` faces for the personal model.
It runs after the daily safety net so it consumes freshly classified facts.
No schema is written until enough clustered facts exist.

```toml
[schema]
enabled = true          # run the daily schema-tick (off = no D2 schema mining)
daily_tick_hour = 0     # local wall-clock hour
daily_tick_minute = 15  # fires at 00:15 local, after the daily safety net
cross_domain_enabled = true               # Hy-Memory cross-domain sweeper (default on)
cross_domain_behavior_max_distance = 0.5  # behavior-distance ceiling for the pre-filter
cross_domain_min_confidence = 0.6         # fused schema below this is born `forming`
```

`cross_domain_*` controls the cross-domain sweeper: after the per-file miner runs,
it pairs stable schemas, keeps those that are topic-far but behavior-near, and asks
the LLM whether they form a higher-level `schema-xdomain-*.md` face. Its behavior
pre-filter is deterministic (app set, action distribution, and hour histogram),
with no embedding call. Low-confidence output is born `forming` and excluded from
active snapshots. Set `cross_domain_enabled = false` to disable the sweep.

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

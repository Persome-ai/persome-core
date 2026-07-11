# Configuration

Runtime behavior lives in `<PERSOME_ROOT>/config.toml` (`~/.persome/config.toml`
by default). Provider secrets live beside it in `env`, never in TOML. A default
file is created on first initialization.

```bash
persome config   # print the path and current file
persome doctor   # offline prerequisites; no LLM call
persome llm status --check  # live completion and tool-call check
```

The daemon loads configuration once. Restart it after editing.

## Providers and stage models

One profile powers semantic stages and Chat. Persome supports native Anthropic
Messages and OpenAI-compatible Chat Completions. Use the guided path instead of
editing secrets by hand:

```bash
persome llm providers       # presets and locally detected key names
persome llm setup           # select, edit, probe, then save
persome llm status --check  # inspect the effective route and retest it
```

`setup` checks the current profile first, auto-selects when exactly one known
credential is found, prompts when several are available, and accepts keyless
local endpoints. It makes a small completion call and a forced tool call before
saving. A failed connectivity/authentication probe writes nothing. A model that
completes but cannot call tools requires an explicit degraded-mode confirmation.

The resulting non-secret configuration has this shape:

```toml
[models.default]
provider = "openrouter"
protocol = "openai"
model = "anthropic/claude-sonnet-4"
base_url = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY"
# max_tokens = 4096

[models.timeline]
# inherits every unset field from models.default

[models.reducer]

[models.memory_delta]

[models.schema_miner]
```

Only the value named by `api_key_env` is stored in `<PERSOME_ROOT>/env`:

```dotenv
OPENROUTER_API_KEY=...

# Optional dense retrieval:
OPENAI_API_KEY=...
# OPENAI_BASE_URL=https://example.test/v1
```

Common model stages are `timeline`, `reducer`, `classifier`, `memory_delta`,
`pattern_detector`, `case_extractor`, `compact`, `schema_miner`,
`cross_domain_sweeper`, `root_synthesis`, `contradiction_check`, and
`memory_decay`. Tool-loop stages require function/tool calling; JSON stages
require reliable structured output. Without a hosted credential or keyless
local endpoint, Persome still captures and serves BM25, but semantic model
stages remain degraded.

Hosted presets currently include Anthropic, OpenAI, DeepSeek, OpenRouter,
Gemini, Groq, Mistral, xAI, Qwen, Moonshot/Kimi, Zhipu GLM, SiliconFlow,
Together, Fireworks, Cerebras, and Azure OpenAI. Keyless local presets cover
Ollama, LM Studio, and vLLM. Presets describe endpoint defaults, not a blanket
capability guarantee for every model. Use `custom-openai` or
`custom-anthropic` for another compatible gateway.

For migration, a config without `provider` and `protocol` retains the former
`ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` route exactly, even if an older file
contains an ignored `api_key_env`. Running `persome llm setup` tests and
converts that route to explicit fields.

## Capture

```toml
[capture]
source = "daemon"                 # daemon | ingest
event_driven = true
heartbeat_minutes = 10
debounce_seconds = 3.0
min_capture_gap_seconds = 2.0
dedup_interval_seconds = 1.0
same_window_dedup_seconds = 5.0

buffer_retention_hours = 168
screenshot_retention_hours = 24
screenshot_thumbnail_hours = 0
buffer_max_mb = 2000

include_screenshot = true
screenshot_max_width = 1920
screenshot_jpeg_quality = 80
ax_depth = 100
ax_timeout_seconds = 3

enable_ocr_fallback = false
ocr_tier = "tiny"                 # tiny | small | medium
ocr_min_gap_seconds = 15.0
ocr_structured = true
cmux_source_enabled = true
```

- `source="daemon"` owns macOS AX capture. `source="ingest"` accepts records
  from the trusted loopback `/captures/ingest` producer and starts no OS watcher.
- Accessibility permission is required for daemon AX capture. Screen Recording
  is required for screenshot/OCR use.
- OCR is off by default. When enabled, Paddle inference runs in a local worker
  subprocess so a native crash does not kill the daemon. OCR text is backfilled
  into capture search and consumed by timeline/modeling; pixels are not sent to
  an LLM stage.
- `PERSOME_DISABLE_OCR=1` is the deployment kill switch.
- `PERSOME_OCR_IN_PROCESS=1` is a debugging escape hatch that gives up crash
  isolation and should not be used for normal operation.
- Retention only removes captures already absorbed by a closed timeline block.
  Screenshots degrade before AX/OCR text and whole-record deletion.
- `cmux_source_enabled` reads visible terminal text through cmux's local,
  read-only socket because GPU terminal content is AX-poor.

## Timeline and sessions

```toml
[timeline]
window_minutes = 1
cold_lookback_minutes = 30
max_parallel_windows = 4
attention_locus_enabled = true

[session]
gap_minutes = 5
soft_cut_minutes = 3
max_session_hours = 2
tick_seconds = 30
flush_minutes = 5

[reducer]
enabled = true
daily_tick_hour = 23
daily_tick_minute = 55

[classifier]
interval_minutes = 30
```

Timeline windows are wall-clock aligned and idempotent. `flush_minutes` and
`classifier.interval_minutes` have a five-minute effective floor. A successful
flush also models its new Point/Line window under default delta apply. Terminal
reducer failures use persisted `5/15/30/60/120` minute backoff; a daemon task
checks the queue every minute and the daily safety net ignores backoff to catch
anything stranded.

Disabling the reducer preserves capture/session rows but prevents event and
incremental personal-model writes.

## Incremental personal modeling

```toml
[memory_delta]
enabled = true
max_blocks = 120
roster_max = 60
min_confidence = 0.5
apply_enabled = true
apply_assertions = true
cooccurrence_knows = true

[pattern_detector]
enabled = true
structured_filter = true
lookback_days = 7
min_occurrences = 2
```

With the shipped defaults, `memory_delta` is the Point/Line producer. It
persists one gated payload per newly flushed active-session window before
deterministic apply and advances `sessions.delta_end`; terminal finalization
only catches the remaining tail. Disabling `apply_enabled` keeps the audit row
but stops it from changing the model and reactivates the classifier's legacy
terminal write role. This switch is for diagnosis/migration, not a second
normal operating mode.

The pattern detector requires repeated evidence and writes observed behavioral
memory under `memory/skills/skill-*.md`. It does not propose or execute
automation.

Additional top-level enrichment flags:

```toml
person_graph_enabled = true
case_extraction_enabled = true
relation_extraction_enabled = false
edge_promote_fanout = 20
```

Person-graph ingest is deterministic. Case extraction distills reusable
problem/solution evidence. Experimental relation extraction remains off; the
memory-delta relation path is already active.

## Higher geometry

```toml
[schema]
enabled = true
refresh_minutes = 30
daily_tick_hour = 0
daily_tick_minute = 15
cross_domain_enabled = true
cross_domain_behavior_max_distance = 0.5
cross_domain_min_confidence = 0.6
root_synthesis_enabled = true
root_token_budget = 1500
```

After new Point/Line evidence, the Runtime calls the same
`ModelBuildCoordinator` as `persome model build` at the bounded
`refresh_minutes` cadence. The 00:15 tick remains an unconditional daily pass.
Both run pending state formation, enrichment, Face mining, Volume synthesis,
Root synthesis, vectors, and layout. Forming schemas are excluded from active
snapshots. Missing repeated evidence yields a truthful degraded build.

## Writer and maintenance

```toml
[writer]
soft_limit_tokens = 20000
max_tool_iterations = 12
context_token_limit = 80000
llm_retry_attempts = 6
llm_rate_limit_wait_s = 30
llm_fallback_model = ""
tool_result_max_bytes = 16384
tool_result_total_budget = 131072
max_output_tokens_recovery_limit = 65536
max_output_tokens_recovery_count = 3
use_token_count_api = false
contradiction_strategy = "abstract"
consolidation_cadence = 8

[memory_decay]
enabled = false
after_days = 90
max_clusters_per_night = 3
cluster_min = 4
cluster_max = 12
shrink_ceiling = 0.5
line_max_chars = 80

[orphan_reaper]
enabled = false
ttl_days = 30
max_per_night = 200
engaged_keep = 2
```

`consolidation_cadence` runs pending per-file compaction. Memory decay and
orphan reaping are lossy maintenance
and therefore remain explicit opt-ins.

## Storage authority and integrity

```toml
[evomem]
snapshot_enabled = true
snapshot_keep_daily = 7
snapshot_keep_weekly = 4
integrity_check_enabled = true
freeze_writes_on_failure = false
shadow_write_enabled = true
write_authority = "markdown"       # markdown | evomem
contradiction_check_enabled = false
contradiction_max_pairs = 10
```

`markdown` is the default authority and shadows current state to evomem.
`evomem` makes the graph authoritative and projects Markdown/FTS from it. An
operator flips this manually; code never changes the authority. Before a
rollback, project current evomem state to Markdown, then rebuild the index.

Snapshots are verified before atomic promotion. Integrity failures are emitted
as structured error logs. The old runtime SSE event bus no longer exists.
`freeze_writes_on_failure` is intentionally off because a false positive under
Markdown authority should not halt observation.

## Retrieval

```toml
[search]
default_top_k = 5
hybrid_enabled = true
hybrid_recall_n = 50
hybrid_rrf_k = 20
slot_pool_weight = 0.3
relation_pool_weight = 1.0
associative_read_enabled = true
relation_include_shadow = true
contains_pool_rerank = true
tags_matchable = false
recency_half_life_days = 14.0
recency_decay_floor = 0.2
embed_model = "text-embedding-3-large"
embed_batch_size = 64
embed_tick_max = 512
```

Hybrid retrieval automatically degrades to BM25 when no embeddings endpoint is
configured. Query-time consumers use one associative entrance; absent entity,
scene, time, or relation slots simply contribute no votes.

## MCP and Chat

```toml
[mcp]
auto_start = true
transport = "streamable-http"
host = "127.0.0.1"
port = 8742
read_receipt_enabled = true
entity_graph_enabled = true

[chat]
# The complete models.default profile is inherited unless overridden here.
thinking_budget = 0
unsafe_local_tools_enabled = false
mcp_connect_daemon = true

# [[chat.mcp_servers]]
# type = "http"
# url = "http://127.0.0.1:9000/mcp"
```

The daemon HTTP transport hosts MCP, REST, Chat routes, and `/model` on the same
loopback port. `stdio` is started explicitly with `persome mcp`; do not use it
as an in-daemon transport.

Chat always loads skill Markdown as model guidance. Executable
`skills/*/tools.py`, shell, arbitrary filesystem, and Web tools load only when
`unsafe_local_tools_enabled=true`. Configured external MCP servers are another
explicit trust expansion and can have their own network behavior. The Runtime
ships a terminal client (`persome chat`), not a browser Chat page.

Anthropic profiles retain prompt caching and optional extended thinking.
OpenAI-compatible profiles use streamed Chat Completions and the same built-in
and MCP tools; `thinking_budget` is ignored on that protocol.

## Privacy and API flags

Top-level API/model defaults include:

```toml
api_require_local_origin = true
```

Capture privacy defaults, configured inside the `[capture]` table, are:

```toml
pause_on_lock = true
suppress_secure_input = true
encrypt_screenshots = true
extended_retention_enabled = true
actionable_retention_days = 7
```

`install.sh` provisions `PERSOME_SCREENSHOT_KEY` automatically. When screenshot
encryption is enabled but that key is unavailable, the capture remains usable
for AX/text modeling but its screenshot is not persisted.

Old top-level `capture_*` names are read as compatibility fallbacks, but new
configuration should use the nested names.

Keep the local-origin guard and screenshot encryption enabled. The loopback
server has no separate bearer authentication; exposing it through a tunnel
changes the security boundary.

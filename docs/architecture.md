# Runtime architecture

Persome is one local daemon with one production ingestion path. It observes a
person, forms bounded state, updates an auditable personal model, and exposes
that model to local consumers. There is no product workflow or predictor hidden
beside this path.

## End-to-end path

```mermaid
flowchart LR
    AX["macOS AX watcher"] --> S0["S0 debounce and dedup"]
    ING["trusted local ingest"] --> S1
    S0 --> S1["S1 focused element, visible text, URL"]
    OCR["optional local OCR worker"] --> CAP
    S1 --> CAP[("capture buffer and captures FTS")]
    CAP --> TL["one-minute timeline normalizer"]
    TL --> BL[("timeline_blocks")]
    CAP -->|new event-triggered capture| SES["three-rule session cutter"]
    BL --> RED
    SES --> RED["five-minute active reducer"]
    RED --> EVT[("event daily memory")]
    RED --> ACTIVE["active-window modeling"]
    ACTIVE --> DELTA["evidence-gated memory_delta"]
    SES -->|session end| FIN["trailing-window finalizer"]
    EVT --> FIN
    BL --> FIN
    FIN --> DELTA
    DELTA --> APPLY["deterministic Point and Line apply"]
    FIN --> PAT["repeated behavior memory"]
    APPLY --> EVO[("evomem, Markdown, FTS")]
    PAT --> EVO
    EVO --> BUILD["case, schema, cross-domain, Root, vectors"]
    BUILD --> SNAP["versioned personal model"]
    SNAP --> MCP["MCP"]
    SNAP --> VIEW["localhost /model; browser computes layout"]
```

### State formation

1. With `capture.source="daemon"`, the source-versioned Swift watcher emits AX
   events and `capture/event_dispatcher.py` performs event deduplication,
   debounce, and minimum-gap control. With `source="ingest"`, a trusted local
   producer owns OS capture and sends authenticated frames; the daemon starts no
   watcher. Both modes converge in the same scheduler/commit path.
2. `capture/scheduler.py` builds an S1 record. AX is primary in daemon mode. When explicitly
   enabled and AX text is poor, a focused screenshot is sent to an isolated
   local OCR subprocess; its text is backfilled into `captures` FTS.
3. `timeline/aggregator.py` independently consults both the capture JSON and OCR
   backfill, removes UI repetition, preserves authored evidence, and writes
   wall-clock aligned one-minute blocks.
4. In parallel with timeline aggregation, `session/manager.py` receives a hook
   only after a new event-triggered capture is committed, then cuts work using
   idle-gap, single-app soft-cut, and maximum-duration rules. Heartbeat captures
   use `trigger=None`: they may be persisted for timeline context, but do not
   call `SessionManager.on_event` and therefore do not extend a session. A
   content-deduplicated capture also fires no session hook.
5. `writer/session_reducer.py` flushes active sessions every five minutes;
   `writer.agent.model_active_session` turns each new window into Points/Lines.
   Session end writes and models only the trailing range.

### Runtime readiness and ownership

The control path is generation-bound. `.daemon.lock` is acquired before start
preflight and inherited for the complete foreground or double-forked daemon
lifetime. The daemon publishes `.runtime-state.json` with `starting`/`ready`
phase, random generation, current permission probes, OCR policy/worker state,
and its last fresh-capture, ingest-readiness, paused, or locked receipt. HTTP
onboarding reads the same data through authenticated endpoints; HTTP-disabled
mode reads the owner-only file directly.

Lifecycle operations treat `.pid` as compatibility input, then verify user,
command/executable, process start time, and generation again before signaling.
LaunchAgent ownership additionally requires its loaded job program/PID and
configured plist to match the recorded Runtime; `.launchagent-owner` preserves
intent across updates. Ambiguous live state fails closed rather than starting a
second writer.

AX permissions are similarly bound to the actual principals. The immutable
`mac-ax-helper` and optional `mac-ax-watcher` each self-check/request
Accessibility. Their machine-local path is derived from architecture and Swift
source bytes, so same-version installs reuse the exact executable. Changed
helper source resolves a new path and requires a new explicit grant; rollback
resolves the old helper again. `[capture].ocr_policy` independently preserves
`auto`, explicit enabled, or explicit disabled intent across onboarding/update.

The updater holds a separate owner-only lock, builds a marked inactive
`venv.replacement.update`, and atomically exchanges it with `venv`. The old code
stays at the replacement path until the replacement's final background or
LaunchAgent owner passes the mode-aware readiness proof. Transaction phase and
candidate marker are fsynced, so recovery can tell whether exchange occurred
even if the process died before recording the next phase; rollback performs the
same atomic exchange in reverse.

### Incremental and terminal modeling

Every successful active flush enters `writer.agent.model_active_session` and
the windowed memory-delta path. Every reduced session then enters
`writer.agent.finalize_session`, regardless of
whether the terminal reducer wrote a new entry. This matters when prior flushes
already covered the whole session or when the reducer exhausted its LLM retries
and wrote a heuristic fallback.

The finalizer runs:

1. classifier compatibility/incremental catch-up;
2. repeated-pattern detection into `skills/skill-*.md`;
3. one structured `memory_delta` extraction over the unmodeled tail;
4. deterministic gates for quoted evidence, identity, predicate vocabulary,
   and confidence;
5. deterministic apply into current/historical Points and relation Lines.

Each memory-delta window is persisted before apply. `apply_status` allows a
crashed apply to resume without another LLM call. `delta_end` advances after the
caller reports a successful active apply; the session receives `modeled_at`
only after all terminal stages finish. A kernel `session-model.lock`
coordinates daemon, retry, CLI, and model-build callers. Apply is currently a
sequence of independently committing operations: per-item errors may be
collected instead of raised, and an additive edge update can repeat after a
mid-apply crash. The code-fact atlas records this boundary explicitly.

### Higher geometry

New Point/Line evidence triggers a debounced build every 30 minutes by default;
`persome model build` and the unconditional 00:15 build call the same locked
coordinator:

1. recover pending reductions and terminal modeling;
2. initialize the evomem baseline when needed;
3. enrich entities, reusable problem/solution cases, and optional relation edges;
4. mine stable per-domain Faces;
5. synthesize repeated cross-domain Volumes;
6. synthesize at most one Root;
7. backfill vectors when an embeddings endpoint is configured.

The coordinator does not generate viewer coordinates. `/model` receives the
versioned snapshot, and the packaged browser-side
`resources/model_assets/layout.mjs` computes its deterministic 3D layout.

Each stage records complete, skipped, or failed. Missing geometry or a failed
enabled substage makes the build `degraded`. The build never fabricates an
empty replacement for a previously valid Root.

## Daemon tasks

The registry in `src/persome/daemon.py` is the authoritative task list.

| Task | Cadence and responsibility |
|---|---|
| `capture` | Continuous AX watcher or trusted ingest runner; writes deduplicated S1 captures and updates session activity. |
| `session` | Every `session.tick_seconds`; evaluates idle, soft-cut, and timeout boundaries. |
| `reducer-retry` | Every 60 seconds; consumes `next_retry_at`, then sends reduced or heuristic terminal results through the shared finalizer. |
| `daily-safety-net` | At 23:55 by default; force-ends the open session, catches all stranded reduction/modeling work, reprojects, checkpoints, snapshots, prunes telemetry, and runs enabled maintenance. |
| `timeline` | Every 60 seconds; materializes closed timeline windows and applies capture retention. |
| `flush` | Every `session.flush_minutes`; reduces and models the new active-session window as Points/Lines. |
| `classifier-tick` | Legacy-only: every `classifier.interval_minutes` when delta apply is disabled. |
| `vector-embed-tick` | Every 60 seconds when hybrid retrieval is enabled; drains the embedding queue. It is a no-op without credentials. |
| `model-refresh` | Every `schema.refresh_minutes` when new Point/Line evidence exists; refreshes Face/Volume/Root. |
| `schema-tick` | At 00:15 by default; invokes the shared personal-model build. |
| `mcp` | Hosts streamable HTTP MCP, REST routes, and `/model`; restarts with backoff after a crash. |

`--capture-only` keeps `capture`, `session`, `reducer-retry`, the daily safety
net, and configured MCP. It disables the periodic timeline, active-flush,
classifier-tick, vector, and structural schema/model-build tasks. Session-end
callbacks, reducer retry/boot recovery, and the daily safety net still send
terminal reductions through the shared finalizer, so the mode is not a global
writer/finalizer kill switch. It is a diagnostic/embedding mode, not a second
ingestion architecture.

## Storage

`src/persome/paths.py` owns every location. The default root is `~/.persome`.

| Artifact | Role |
|---|---|
| `capture-buffer/*.json` | Bounded raw S1 records and optional encrypted screenshots. |
| `memory/*.md` | Human-readable event, fact, schema, and correction history. |
| `memory/skills/skill-*.md` | Evidence-backed repeated behavior. |
| `index.db` | WAL-mode sessions, FTS5, evomem, relations, geometry, receipts, vectors, and audit tables. |
| `model-build.json` | Owner-only build conditions and stage outcomes. |
| `exports/*.json` | Owner-only, redacted-by-default snapshots. |
| `backup/*.db` | Verified daily SQLite snapshots when enabled. |

Markdown is the default write authority and evomem is its maintained shadow.
An operator may explicitly invert authority to evomem; this does not change the
public snapshot contract. SQLite access must use `with fts.cursor() as conn:` so
readers and writers coexist under WAL mode.

## Public access

- **CLI:** lifecycle, recovery, inspection, correction, and model build/export.
- **MCP:** memory/model reads, provenance drill-down, and explicit audited writes.
- **Viewer:** `persome model open` while the daemon HTTP server is active. It
  exchanges the owner bearer for a one-time browser capability, then reads
  `/model/graph` and packaged local Three.js assets.
- **Snapshot:** schema-versioned JSON for external clients and products.

The Runtime contains no click/type actuation, notification lifecycle, meeting
audio, or evaluation runner.

## Failure semantics

- No selected provider credential (unless using a keyless local endpoint):
  capture and BM25 remain available; semantic stages report
  skips/failures and model status stays degraded.
- OCR worker crash: the worker is restarted/fails open; the daemon survives.
- Reducer failure: persisted exponential retry; final exhaustion writes an
  auditable heuristic event, then still runs terminal modeling.
- Active model failure: `delta_end` does not advance, so the next flush retries a larger window.
- Terminal model failure: `modeled_at` remains null and retry/recovery can resume.
- Model build overlap: `model-build.lock` waits or reports busy.
- Integrity/snapshot failure: structured error logs and optional write freeze;
  there is no removed SSE event bus.

See `capture.md`, `timeline.md`, `session.md`, and `writer.md` for stage details.

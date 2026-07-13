# Runtime architecture

persome-core is a local-first macOS Personal Model Runtime. It owns four
things: observation, state formation, personal-model construction, and model
access. Product notification lifecycles, computer-use actuation, meetings,
task dashboards, and evaluation harnesses are outside this repository.

## Data flow

```text
macOS AX watcher ─┐
optional local OCR├─> capture buffer -> timeline blocks -> sessions
trusted ingest API┘                              |
                                                   v
                                five-minute reducer -> event memory
                                                   |
                                                   v
                             incremental memory_delta/apply
                                                   |
                                 session-end trailing finalizer
                                  classifier + pattern catch-up
                                                   |
                                                   v
                   Points/Lines -> schema Faces -> Volumes -> Root
                                                   |
                            ┌──────────────────────┼──────────────────────┐
                            v                      v                      v
                     model snapshot              MCP                 /model
```

The capture path is macOS-only. The storage, model projection, and offline
tests can run on Linux with macOS-marked tests deselected.

## State formation

1. In `capture.source="daemon"`, the source-versioned native watcher receives
   Accessibility events and the matching helper reads focused trees. In
   `capture.source="ingest"`, a trusted bearer-authenticated producer owns OS
   capture and the daemon starts no watcher. Both modes converge before S1.
   Supported Apple Silicon installs can enable bundled, subprocess-isolated
   PP-OCRv6 as a fallback when an app exposes no usable AX text; AX remains the
   primary daemon-mode signal.
2. `parsers/` normalizes app-specific structures into capture records.
3. `timeline/` groups records into one-minute blocks and preserves authored
   evidence.
4. `session/` cuts bounded work sessions using deterministic rules.
5. `writer/` reduces the active window every five minutes by default and applies
   an evidence-gated memory delta to durable model state. Session end processes
   only the trailing window and detects repeated behavior. The shared service
   is idempotent across daemon, retry, CLI, and build callers.

The detailed stage behavior lives in
[`docs/capture.md`](docs/capture.md),
[`docs/timeline.md`](docs/timeline.md),
[`docs/session.md`](docs/session.md), and
[`docs/writer.md`](docs/writer.md).

## Runtime control plane

`persome onboard` proves the configured mode rather than assuming one universal
capture path. Daemon mode requests Accessibility for the actual immutable
`mac-ax-helper` and optional `mac-ax-watcher` principals, requests Screen
Recording only when the effective pixel policy requires it, and obtains a fresh
capture from the running scheduler. Ingest mode proves the authenticated ingest
runner. Intel, explicit OCR opt-out, paused/locked privacy state, and
HTTP-disabled daemon mode each publish their truthful readiness receipt. The
durable `[capture].ocr_policy` prevents ordinary onboarding or an update from
silently reversing an explicit choice.

Every daemon holds `<PERSOME_ROOT>/.daemon.lock` for its lifetime and publishes
an owner-only `.runtime-state.json` with generation, phase, policy, permission,
worker, and capture/privacy receipts. PID, current-user process identity, start
time, executable/command, and generation are revalidated before signaling.
LaunchAgent ownership also binds the loaded job program/PID and persistent
`.launchagent-owner` intent marker to that Runtime.

Updates build an inactive, transaction-marked virtualenv, then exchange it with
the active venv using one same-filesystem kernel operation. The old code remains
at the replacement path until the new final owner passes onboarding. Fsynced
transaction phase plus the marker permit deterministic rollback even if the
updater crashes immediately around the exchange. Swift AX binaries live outside
either venv at immutable architecture/source-digest paths, so a same-version
reinstall reuses the exact TCC principal, changed helper source requires an
explicit new grant, and rollback resolves the old binary again.

## Model construction

`persome model build` enters `ModelBuildCoordinator`, which takes an exclusive
`<PERSOME_ROOT>/model-build.lock` using `flock`. The same one-shot service is
safe to call from CLI and scheduled runtime paths; a competing caller waits up
to 30 seconds by default or returns `busy` with `--no-wait`.

The coordinator runs, in order:

1. pending state formation;
2. evomem baseline/backfill;
3. entity, reusable-case, and relation enrichment;
4. level-1 schema mining;
5. level-2 cross-domain synthesis;
6. level-3 root synthesis;
7. vector backfill.

Each stage records completion, skip, or failure. Enrichment runs every enabled
substage before surfacing a partial failure, so the manifest cannot call a
partially failed stage complete. Missing geometry marks the build degraded and
never overwrites an existing good Root with an empty result. The manifest is
written atomically with owner-only permissions. The Runtime then
deterministically renders the raw snapshot as owner-only `HUMAN.md`; this
projection makes no additional LLM call and never fabricates a Root.

## Storage

`src/persome/paths.py` is the path authority. The default root is
`~/.persome`; tests set `PERSOME_ROOT` to a temporary directory.

| Artifact | Purpose |
|---|---|
| `capture-buffer/*.json` | bounded raw capture records |
| `memory/*.md` | readable durable memory and schema projections |
| `index.db` | WAL-mode FTS5, model, provenance, sessions, and vectors |
| `model-build.json` | last reproducibility manifest |
| `HUMAN.md` | raw deterministic model reading view, mode `0600` |
| `exports/*.json` | redacted model snapshots, mode `0600` |
| `.runtime-state.json` | owner-only Runtime generation and readiness receipt |
| `native/<source-digest>/` | immutable AX helper/watcher binaries |

Markdown remains the default write authority; evomem can be selected
explicitly. The runtime does not destructively migrate old product tables, and
legacy completed activity can be read through a neutral adapter.

## Public surfaces

- CLI: capture/daemon lifecycle plus `model build|status|export`.
- REST: health, permissions, status, trusted capture ingest, and the model
  viewer; `/model` is the browser viewer. See [`docs/api.md`](docs/api.md).
- MCP: model retrieval, provenance, recent context, and explicit correction.
  See [MCP.md](MCP.md).
- Snapshot: versioned Point/Line/Face/Volume/Root JSON. See
  [MODEL_FORMAT.md](MODEL_FORMAT.md).
- Human view: raw owner-local `HUMAN.md`, backfilled from an existing valid
  Root on startup/onboarding. JSON remains the machine contract.

## Invariants

- Every new Activity relation has a stable source identity and receipt.
- New activity IDs use `event:entry:*` or `event:session:*`; old
  `event:intent:*` records are read-only compatibility data.
- At most one live Root exists.
- A model export is redacted by default and written with mode `0600`.
- A Persome-managed `HUMAN.md` is refreshed automatically; an unknown file at
  that path is preserved instead of overwritten.
- SQLite access uses `with fts.cursor() as conn:` so readers and the writer
  coexist under WAL mode.
- LLM calls flow through `writer/llm.py` over Anthropic Messages or
  OpenAI-compatible Chat Completions. Route metadata lives in `config.toml`;
  key values stay in the owner-only `env` file.

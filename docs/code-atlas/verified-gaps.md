# Verified gaps, couplings, and documentation corrections

This is the atlas's exception ledger. Each item was checked against the current
source and is deliberately classified as one of:

- **runtime correctness gap** — behavior can lose, duplicate, or misreport work;
- **coverage/observability gap** — the intended behavior is not directly proved;
- **architectural coupling** — real behavior whose product semantics should be
  chosen explicitly, not silently treated as a bug;
- **documentation drift corrected here** — code behavior was already coherent,
  but prior prose or diagrams described something else.

The baseline is `origin/main` at `2818634`. This change documents these facts;
it does not silently alter Runtime behavior.

## Open findings at a glance

| # | Classification | Finding | Practical consequence |
|---|---|---|---|
| 1 | Runtime correctness | Delta apply can report partial item failures as fully applied. | A window can advance while some accepted items never land. |
| 2 | Runtime correctness | The advertised 120-minute reducer retry is unreachable. | Actual retries are 5/15/30/60 minutes, then heuristic fallback. |
| 3 | Runtime correctness | Reducer reconstruction drops persisted focus/attention fields. | Its attention section is empty and only normalized entries reach that prompt. |
| 4 | Cost/idempotency boundary | Timeline uniqueness deduplicates rows, not concurrent LLM work. | Two workers can pay for the same minute although only one row persists. |
| 5 | Architectural correctness | Immediate correction re-forward omits Volume recomputation. | Root may be resynthesized from a corrected Face plus stale Volumes. |
| 6 | Projection consistency | Direct evomem writes do not guarantee physical Markdown. | `list_memories` can name a DB-only file that `read_memory` cannot open. |
| 7 | Reproducibility coverage | Build manifest omits some models/prompts. | A build ID does not fully fingerprint every production LLM surface. |
| 8 | Architectural coupling | Shadow relation candidates influence default recall at half weight. | Retrieval may use evidence that the accepted model snapshot intentionally hides. |
| 9 | Architectural coupling | Face activation requires an emergent cross-domain signal. | A repeatedly stable domain-local schema can remain shadow indefinitely. |
| 10 | Test coverage | Several concurrency/fault boundaries lack direct tests. | Regressions can evade the otherwise broad offline suite. |
| 11 | Maintenance ordering | Daily projection runs before optional orphan retirement. | The same tick can discard its verified backup and leave retrieval stale until the next projection. |
| 12 | Scheduling/recovery | Daily maintenance has no durable missed-run catch-up, and early failure skips the tail. | A restart or force-end/writer exception can postpone projection, backup, and integrity work until the next wall-clock day. |
| 13 | Transaction/reconciliation | Daily evomem projection calls a private rebuild primitive. | It lacks the public rebuild's savepoint, busy/source-change retries, and derived vector/stat cleanup. |
| 14 | Projection consistency | Contradiction marks live only in retrieval metadata. | The next evomem projection can erase the warning while the judged-pair ledger prevents a new adjudication. |

## 1. Partial delta apply can be marked fully applied

**Classification:** runtime correctness gap.

[`ApplyResult`](../../src/persome/writer/delta_apply.py) collects per-item
exceptions in `errors`; `apply_delta` deliberately continues through the other
heads. Both the first apply path and stored-payload resume path in
[`memory_delta.py`](../../src/persome/writer/memory_delta.py) set
`apply_status=applied` without inspecting that list.

The write is also not one database transaction. `EvoMemory`/`NodeStore` may open
their own connection, and relation add/reinforce operations commit internally.
The self-engagement floor uses additive observation increments. A process crash
after one such commit but before the window watermark advances can therefore
reinforce the same edge again on replay.

**Decision needed:** define whole-delta atomicity or add stable per-item apply
keys and make `ApplyResult.errors` block the applied status. Add fault injection
between every apply head before claiming replay idempotency.

## 2. The 120-minute reducer retry is unreachable

**Classification:** runtime/documented-contract gap.

[`session_reducer.py`](../../src/persome/writer/session_reducer.py) declares
`(5, 15, 30, 60, 120)` and sets the attempt limit to that tuple's length. Its
failure branch switches to heuristic fallback when `retry_count + 1 >= 5`;
only the other branch indexes the delay tuple. The fifth failed attempt therefore
writes the fallback instead of scheduling index four.

**Current fact:** delays after failed attempts one through four are 5, 15, 30,
and 60 minutes; failure five exhausts into deterministic heuristic memory.

**Decision needed:** either remove the dead 120 value and keep five total
attempts, or change the attempt contract and tests so a 120-minute retry is
actually scheduled.

## 3. Reducer reconstruction drops focus and attention fields

**Classification:** runtime correctness and test-coverage gap.

[`TimelineBlock`](../../src/persome/timeline/store.py) persists
`focus_excerpt`, `focus_structured`, `attention_surface`, confidence, rung,
skill hints, and action trace. Its shared row decoder restores those fields.
However, `_blocks_for_session` in
[`session_reducer.py`](../../src/persome/writer/session_reducer.py) manually
constructs each block with only identity/time/entries/apps/count fields. The
subsequent attention-trajectory formatter therefore receives empty attention
values.

**Consequence:** reducer summaries currently depend on the lossy normalized
`entries` rather than the documented raw-focus backstop, and their attention
section is empty. The separate memory-delta path uses the shared timeline range
query and is not affected by this specific reconstruction bug.

**Decision needed:** use the shared row decoder/query API in the reducer and add
a database-row-to-prompt test proving all focus/attention fields survive.

## 4. Timeline row idempotency is not LLM-cost idempotency

**Classification:** explicit idempotency boundary, not evidence loss.

[`timeline/store.py`](../../src/persome/timeline/store.py) has a unique
`(start_time,end_time)` key and `INSERT OR IGNORE`, so duplicate rows do not
persist. But [`timeline/aggregator.py`](../../src/persome/timeline/aggregator.py)
checks existence, performs normalization/LLM work, and then inserts using
separate connections. Concurrent producers of the same minute can both pass
the precheck.

**Consequence:** stored state converges, while provider cost and latency can be
duplicated. Atlas labels distinguish this storage guarantee from execution
exactly-once semantics.

**Decision needed:** accept the rare duplicate cost, or introduce a durable
window claim/lease before the LLM call.

## 5. Immediate correction does not recompute Volumes

**Classification:** architectural correctness gap.

`_reforward` in [`writer/correct.py`](../../src/persome/writer/correct.py)
target-remine affected Face bundles and immediately calls Root synthesis. It
does not run the cross-domain sweeper or retire/recompute Volumes whose members
or receipts depended on the corrected Face.

**Consequence:** the correction itself remains auditable, but the newly
synthesized Root can consume a stale level-2 Volume until the next complete
structural build.

**Decision needed:** either schedule/perform dependency-aware Volume invalidation
before Root synthesis, or explicitly defer all higher-geometry re-forward to a
full locked build and report that state to the caller.

## 6. Direct evomem writes do not guarantee a physical Markdown projection

**Classification:** projection-consistency gap.

The default observation delta, case extractor, and PersonGraph paths call
`EvoMemory.add_direct`, so accepted Points immediately exist in `evo_nodes`.
The daily `_rebuild_from_evo_nodes` in
[`store/entries.py`](../../src/persome/store/entries.py) rebuilds SQLite
`files`, `entries`, temporal metadata, and FTS. It does **not** create the
corresponding `memory/*.md` file.

Meanwhile, MCP `list_memories` in
[`mcp/server.py`](../../src/persome/mcp/server.py) reads the SQLite `files`
projection, while `read_memory` resolves and parses a physical Markdown path.

**Consequence:** after DB projection, an evomem-only filename can appear in the
list but return `file not found` when read as a file. Search and snapshot can
still see the underlying data.

**Decision needed:** make `read_memory` authority-aware, guarantee physical
projection before publishing a file row, or expose the surfaces as explicitly
different projections instead of one apparent file API.

## 7. Build manifest does not fingerprint every production model and prompt

**Classification:** reproducibility coverage gap.

`_MODEL_STAGES` in [`model/build.py`](../../src/persome/model/build.py) omits at
least the memory-delta and embedding model surfaces. `prompt_hashes` in
[`model/manifest.py`](../../src/persome/model/manifest.py) scans the packaged
`src/persome/prompts` directory, while the live evomem schema miner loads
`src/persome/evomem/prompts/schema_miner.md` from another directory.

**Consequence:** two materially different production configurations can expose
the same manifest model/prompt metadata. The core commit and config hash still
provide useful provenance, but the manifest is not a complete stage-level
reproduction receipt.

**Decision needed:** enumerate all model-using stages from one registry and hash
all packaged prompt roots used by those stages.

## 8. Shadow relations participate in default retrieval

**Classification:** intentional-looking architectural coupling that needs an
explicit product decision.

The default config sets `search.relation_include_shadow=true`.
[`store/fts.py`](../../src/persome/store/fts.py) expands shadow-only neighbors
into a separate relation pool and gives it half the configured relation-pool
weight. [`model/snapshot.py`](../../src/persome/model/snapshot.py), by contrast,
includes only active/open relation edges.

This is internally consistent if “shadow” means an audited hypothesis allowed
to improve recall but not an accepted model claim. It is inconsistent if all
retrieval context is expected to obey snapshot acceptance semantics.

**Decision needed:** document that epistemic distinction as public policy, or
default retrieval to active edges only. Evaluation should measure both recall
gain and false-association cost.

## 9. Face activation is coupled to cross-domain evidence

**Classification:** architectural coupling, not automatically a bug.

[`schema_faces.maybe_promote`](../../src/persome/store/schema_faces.py) requires
level-1 provenance `both`, at least two observations, at least two footprints,
and minimum pairwise footprint Jaccard above the stability threshold. The schema
miner contributes `mined`; the emergent signal is supplied by cross-domain
collision work.

**Consequence:** a domain-local schema can be repeatedly mined and stable yet
remain shadow if no eligible cross-domain counterpart yields an emergent signal.
This makes the semantic meaning of “Face is real” depend on cross-domain
availability, not only local recurrence.

**Decision needed:** retain this as a two-extractor independence rule, or give
domain-local recurrence a separate activation path and reserve cross-domain
evidence for Volume formation.

## 10. Fault and concurrency tests still missing

**Classification:** test coverage gap.

The repository has broad deterministic offline coverage, but the audit did not
find direct tests for:

- `EventDispatcher` debounce/dedup/min-gap as an isolated state machine;
- reducer SQLite reconstruction preserving every focus/attention field;
- crash injection between delta-apply heads and commits;
- `ApplyResult.errors` preventing an applied status/watermark advance;
- concurrent same-window timeline claims preventing duplicate provider work.

These are better next tests than increasing line coverage indiscriminately,
because they sit on state-transition and replay boundaries.

## 11. Daily orphan retirement runs after retrieval projection

**Classification:** maintenance-ordering correctness gap, gated by
`orphan_reaper.enabled`.

[`run_daily_safety_net`](../../src/persome/session/tick.py) first calls
`_rebuild_from_evo_nodes`, then later runs
[`orphan_reaper.run_orphan_reap`](../../src/persome/writer/orphan_reaper.py).
The reaper retires Points through `EvoMemory.commit_retire`/`NodeStore.shadow`
but does not update `entries` or Markdown, and the same tick does not run the
projection again.

The subsequent verified backup checks that active evomem heads equal live
non-event entries. [`backup.create_snapshot`](../../src/persome/evomem/backup.py)
treats the resulting non-structural projection reconciliation finding as
blocking under its default `structural_only=false`, discards the new temporary
backup, and preserves any older backup. Live retrieval can also expose the
retired entry until the next evomem-to-entries projection.

**Decision needed:** move retirement/decay before the one daily projection, or
run one final projection after every evomem mutation and before backup/integrity
verification. Add a daily-sequence test covering reaper enabled + successful
same-day backup + converged retrieval rows.

## 12. Daily maintenance has no durable missed-run recovery

**Classification:** scheduling/recovery gap.

[`run_daily_safety_net`](../../src/persome/session/tick.py) sleeps until the next
local wall-clock time and carries no “last completed day” watermark or pending
maintenance journal. If the daemon is down at the scheduled time, the tail does
not run immediately on restart. Inside the tick, failures from `force_end` or
`writer_agent.run` reach the outer handler and skip projection, forgetting,
checkpoint, backup, integrity, and manual-edit detection; only the later
individual substages have local fail-open handlers.

**Decision needed:** persist a maintenance phase/day journal, run overdue work
at startup, and make the first two steps independently resumable from the
maintenance tail.

## 13. Daily projection bypasses the public rebuild transaction

**Classification:** transaction and reconciliation gap.

The daily helper in [`session/tick.py`](../../src/persome/session/tick.py)
calls private `entries._rebuild_from_evo_nodes` directly. Public
[`entries.rebuild_index`](../../src/persome/store/entries.py) wraps the same
primitive in a savepoint, retries source-change/SQLite busy conditions up to
five times, and removes retrieval-stat/vector/queue rows for entries that no
longer exist.

**Consequence:** an exception during the private daily rebuild can leave an
empty or partially rebuilt projection on its autocommit connection, and stale
derived rows are not reconciled by that call.

**Decision needed:** route the daily task through `rebuild_index` with explicit
evomem authority, or give the private daily entrance the same savepoint/retry
and derived-state cleanup contract.

## 14. Contradiction marks do not survive evomem reprojection

**Classification:** projection-consistency gap, gated by nightly contradiction
checking.

[`writer/contradiction_check.py`](../../src/persome/writer/contradiction_check.py)
sets `entry_metadata.conflicted=1` and records every judged pair in
`memory_contradictions`. It does not update `evo_nodes.conflicted`. The next
evomem-to-entries rebuild deletes/recreates metadata from the evo node value,
which can clear the warning. `seen_pairs` then excludes the already-judged pair
forever, so the nightly judge does not restore it.

**Decision needed:** make the contradiction ledger authoritative during
projection, or persist the conflicted bit through the canonical evo node and
keep resolution/dismissal synchronized across both stores.

## Documentation drift corrected in this change

The following were description errors, not new runtime changes:

| Prior mental model | Code fact now documented |
|---|---|
| Capture → timeline → session as one serial pipe. | A successful committed capture fans out to an independent minute timeline and, only with a real trigger, the session activity clock. |
| Session hook occurs before capture commit. | Dedup, private JSON write, receipt publication, then session hook. |
| Every heartbeat/initial capture refreshes a session. | `trigger=None` captures can persist and enter timeline evidence without starting/extending session activity. |
| Secure input suppresses the whole capture. | It strips AX/text/URL/pixels/OCR but retains timestamp/window metadata; locked screen skips the whole capture. |
| Capture-time secret regex scrubbing. | Structural placeholders are sanitized at capture time; secret-pattern scrubbing is the snapshot/export boundary. |
| OCR writes into the capture JSON. | JSON is committed first; OCR asynchronously backfills only the searchable capture projection. |
| Timeline uniqueness means no duplicate LLM call. | It guarantees one stored row, not one concurrent execution. |
| Delta apply mints an event Point. | Its event head creates a deterministic endpoint identity and participant relation; no direct event Point is minted there. |
| `--capture-only` means no modeling can occur. | Live timeline/flush/structural tasks are disabled, while retry and daily recovery can finalize historical work. |
| Viewer coordinates are part of model build. | Browser-side `resources/model_assets/layout.mjs` computes deterministic display placement. |
| Every route except health requires a bearer. | The canonical nonce-protected browser-bootstrap GET is also bearer-exempt; its one-use nonce must first be issued through an authenticated POST. |
| Direct evomem writes immediately create Markdown. | They immediately create model nodes; searchable and physical-file projections have separate timing. |

When any of these behaviors intentionally changes, update
[`stages.toml`](stages.toml), regenerate the atlas, and add a regression test at
the relevant state boundary.

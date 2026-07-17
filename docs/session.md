# Sessions and incremental modeling

A session is a bounded stretch of one person's focused activity. It is the
atomic unit for incremental reduction and personal modeling. Rows live in
`index.db.sessions`; capture dedup ensures an unchanged screen does not keep a
session alive.

## Boundary rules

`session/manager.py` applies three deterministic rules on every written capture
and every `session.tick_seconds` check:

1. **Idle gap:** no meaningful capture for `gap_minutes` (default 5) closes just
   after the last event time.
2. **Single-app soft cut:** one unrelated app holds focus for
   `soft_cut_minutes` (default 3), unless at least two apps appeared in the
   preceding two minutes.
3. **Maximum duration:** `max_session_hours` (default 2) force-cuts a runaway
   session.

Shutdown and the 23:55 daily safety net also force-end an open session. If the
process crashes before shutdown can run, the next daemon boot closes every
stranded `active` row before accepting a new capture. Each recovered row ends
one microsecond after its latest durable capture or timeline-source timestamp
inside `[start, min(next_session_start, boot_time))`; the next session and boot
clock are caps, not evidence that activity continued until those instants.

Persisted session ranges are half-open: `[start_time, end_time)`. A normal cut
binds `start_time` to the first durable capture timestamp and stores `end_time`
one microsecond after the final event instant. This keeps both endpoint captures
in exactly one terminal range without admitting a later heartbeat.

## State machine

```mermaid
stateDiagram-v2
    [*] --> active: first capture
    active --> active: flush event memory + model Point/Line window
    active --> ended: cut or force-end
    ended --> failed: terminal reducer failed
    failed --> failed: persisted 5/15/30/60/120 minute retry
    ended --> reduced: terminal reduce/no-new-block
    failed --> reduced: recovered or heuristic fallback
    reduced --> reduced: terminal finalizer retry
    reduced --> modeled: classifier/pattern/delta complete
    modeled --> [*]
```

`status` remains `reduced` after modeling; `modeled_at` is the durable terminal
marker shown separately in the diagram.

## Incremental flush

Every `session.flush_minutes` (default 5, minimum 5), `run_flush_tick` reads
closed, cutoff-safe timeline slices since `flush_end`, reduces them, and appends
a `[flush]` entry to `event-YYYY-MM-DD.md`. It advances `flush_end` only through
the last closed block; a failed flush waits for the next, larger window. An
occupied closed minute with no block is a retryable gap, never a watermark
jump. The terminal reduce only covers the trailing range not already flushed
and waits for an occupied off-minute closing block plus raw boundary provenance.
An off-minute terminal slice with no durable occupancy does not wait for an
empty closing block, so crash recovery cannot deadlock on the daemon boot minute.

After each successful flush, `writer.agent.model_active_session` extracts and
applies a memory delta over `[delta_end, flush_end)`. Successful apply advances
`delta_end`, so the next tick reads only new evidence. The same model lock used
by terminal finalization prevents a session-end race. The daemon remains
running throughout.

Under the default memory-delta model path there is no active classifier tick.
If an operator sets `memory_delta.apply_enabled=false`, the legacy classifier
task runs every `classifier.interval_minutes` and advances `classified_end`
only to a closed wall boundary.

## Reducer recovery

Terminal reducer failures set `status=failed`, increment `retry_count`, retain
`last_error`, and write `next_retry_at`. The `reducer-retry` daemon task checks
once per minute. The retry schedule is 5, 15, 30, 60, and 120 minutes.

After the fifth failed attempt, the reducer writes an auditable `heuristic`
event entry and marks the session reduced. This is degraded state formation,
not a silent loss. The same result still enters terminal modeling.

The reducer task runs one unconditional catch-up pass at daemon boot, then
checks due reducer retries once per minute. Its minute-level model recovery is
narrower: it retries only a reduced session whose persisted
`model_retry_reason=awaiting_closing_block` has just become eligible. Generic
classifier, pattern, store, or LLM errors are recovered at boot, by the daily
safety net, or through `persome writer run`; they are not hammered every
minute. The 23:55 safety net also ignores reducer backoff and catches every
stranded `ended` or `failed` row.

## Shared terminal finalizer

`writer.agent.finalize_session` is the only terminal model entrance. It is used
by:

- the asynchronous session-end callback;
- the one-minute reducer retry task;
- the daily safety net;
- `persome writer run`;
- `persome model build`.

It re-reads the session under a cross-process `flock`, skips a non-reduced row,
and returns success immediately when `modeled_at` already exists. It then runs
the enabled classifier compatibility path, pattern detector, and
`memory_delta.ensure_after_session`.

The finalizer runs even when the terminal reducer wrote no new entry, because a
long session may already be fully represented by flush entries. It also runs
after heuristic reducer exhaustion. Only a complete/benign result from every
enabled stage sets `modeled_at`.

Retryable terminal stages contribute their persisted wait reasons together.
When every incomplete stage reports the same reason, that reason is retained;
in particular, a classifier-only `awaiting_closing_block` is eligible for the
same minute retry as a pattern/delta closing-block wait. Mixed reasons or any
hard stage error collapse to `stage_error`, so the minute loop cannot hammer an
unrelated failure.

Each window's memory-delta audit row is the retry boundary. A later active or
terminal pass reuses its post-gate payload and retries only deterministic apply;
it does not pay for a second LLM extraction or reinforce successful edges twice.
Terminal finalization starts at `delta_end`, so it only catches the trailing
unmodeled range.

## Session columns

| Column | Meaning |
|---|---|
| `status` | `active`, `ended`, `failed`, or `reduced`. |
| `start_time`, `end_time` | Half-open activity window, from the first durable capture through just after the final event. |
| `flush_end` | End of the latest reduced subwindow. |
| `classified_end` | Legacy classifier bookmark. |
| `pattern_detected_end` | Pattern detector bookmark. |
| `delta_end` | End of the latest successfully applied Point/Line window. |
| `retry_count`, `next_retry_at`, `last_error` | Persisted reducer recovery. |
| `modeled_at` | All terminal modeling stages completed. |
| `model_retry_reason` | Persisted terminal-model wait class; the minute loop recognizes only a newly eligible closing-block wait. |

Fresh schema is generated in `docs/db-schema.sql`; upgrades add new columns
without dropping old product-era columns.

## Tuning

| Symptom | Setting |
|---|---|
| Focused sessions split too often | Raise `soft_cut_minutes`; multi-app work already has an exception. |
| Thinking pauses end sessions | Raise `gap_minutes`. |
| Normal deep work reaches timeout | Raise `max_session_hours`, but keep a finite bound. |
| Event memory is too delayed | Keep `flush_minutes` at 5; lower values are clamped. |

Session boundaries are deterministic Runtime compression boundaries, not
semantic labels or ground truth.

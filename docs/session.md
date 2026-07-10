# Sessions and terminal finalization

A session is a bounded stretch of one person's focused activity. It is the
atomic unit for reduction and terminal personal modeling. Rows live in
`index.db.sessions`; capture dedup ensures an unchanged screen does not keep a
session alive.

## Boundary rules

`session/manager.py` applies three deterministic rules on every written capture
and every `session.tick_seconds` check:

1. **Idle gap:** no meaningful capture for `gap_minutes` (default 5) closes at
   the last event time.
2. **Single-app soft cut:** one unrelated app holds focus for
   `soft_cut_minutes` (default 3), unless at least two apps appeared in the
   preceding two minutes.
3. **Maximum duration:** `max_session_hours` (default 2) force-cuts a runaway
   session.

Shutdown and the 23:55 daily safety net also force-end an open session.

## State machine

```mermaid
stateDiagram-v2
    [*] --> active: first capture
    active --> active: flush event memory
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
closed timeline blocks since `flush_end`, reduces them, and appends a
`[flush]` entry to `event-YYYY-MM-DD.md`. It advances `flush_end`; a failed flush
waits for the next, larger window. The terminal reduce only covers the trailing
range not already flushed.

Under the default memory-delta model path there is no active classifier tick.
If an operator sets `memory_delta.apply_enabled=false`, the legacy classifier
task runs every `classifier.interval_minutes` and advances `classified_end`.

## Reducer recovery

Terminal reducer failures set `status=failed`, increment `retry_count`, retain
`last_error`, and write `next_retry_at`. The `reducer-retry` daemon task checks
once per minute. The retry schedule is 5, 15, 30, 60, and 120 minutes.

After the fifth failed attempt, the reducer writes an auditable `heuristic`
event entry and marks the session reduced. This is degraded state formation,
not a silent loss. The same result still enters terminal modeling.

The 23:55 safety net ignores backoff and catches every stranded `ended` or
`failed` row. `persome writer run` is the same manual recovery entrance.

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

The memory-delta audit row is the retry boundary. A later finalizer reuses its
post-gate payload and retries only deterministic apply; it does not pay for a
second LLM extraction or reinforce successful edges twice.

## Session columns

| Column | Meaning |
|---|---|
| `status` | `active`, `ended`, `failed`, or `reduced`. |
| `start_time`, `end_time` | Bounded activity window. |
| `flush_end` | End of the latest reduced subwindow. |
| `classified_end` | Legacy classifier bookmark. |
| `pattern_detected_end` | Pattern detector bookmark. |
| `retry_count`, `next_retry_at`, `last_error` | Persisted reducer recovery. |
| `modeled_at` | All terminal modeling stages completed. |

Fresh schema is generated in `docs/db-schema.sql`; upgrades add new columns
without dropping old product-era columns.

## Tuning

| Symptom | Setting |
|---|---|
| Focused sessions split too often | Raise `soft_cut_minutes`; multi-app work already has an exception. |
| Thinking pauses end sessions | Raise `gap_minutes`. |
| Normal deep work reaches timeout | Raise `max_session_hours`, but keep a finite bound. |
| Event memory is too delayed | Keep `flush_minutes` at 5; lower values are clamped. |

Session boundaries are not paper labels or benchmark ground truth. They are a
deterministic Runtime compression boundary that `persome-bench` may replay and
evaluate separately.

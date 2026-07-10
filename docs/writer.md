# Writer

The writer is two LLM stages wired behind session boundaries:

1. **S2 reducer** (`writer/session_reducer.py`) — writes incremental `[flush]` entries during an active session and a final entry when it closes, both to `event-YYYY-MM-DD.md`.
2. **Classifier** (`writer/classifier.py`) — runs on a timer during the active session, then one last trailing-window pass at the terminal reduce. Scans the newly-appended event-daily entries for durable facts and persists them to `user-/project-/tool-/topic-/person-/org-*.md` via a tool-call loop.

Both the reducer and the classifier are periodic during long sessions. The reducer flushes every `session.flush_minutes` so event-daily surfaces activity in near-real-time; the classifier fires every `classifier.interval_minutes` (default 30, min 5) so durable facts are extracted in the same long-running session without waiting for it to end. Each stage tracks its own progress on the sessions row (`flush_end` for the reducer, `classified_end` for the classifier), so every entry is processed exactly once. Session boundaries come from `session/manager.py` (see [session.md](session.md)). No capture-level triage, no global writer loop.

## Triggers

| Trigger | What fires |
|---|---|
| Flush tick (every `session.flush_minutes`, min 5) | `flush_active_session` runs the reducer with `is_final=False` over new closed blocks since `flush_end`. Appends a `[flush]`-tagged entry to today's event-daily. The classifier does **not** fire. |
| Classifier tick (every `classifier.interval_minutes`, min 5) | For the currently-active session, classifies entries appended since `classified_end` (fallback: `session_start`). On a committed pass advances `classified_end`. Silent no-op when no new entries landed since last tick. |
| `SessionManager.on_session_end` callback | `reduce_session_async` spawns a daemon thread with `is_final=True`, covering whatever wasn't flushed yet. On success, its `on_done` callback invokes the classifier over the trailing window `[classified_end, now)` — whatever the 30-min tick hadn't reached yet. |
| Daily 23:55 safety-net cron | `reduce_all_pending` picks up any `ended`/`failed` session rows whose async work didn't finish (e.g. daemon crashed mid-reduce). Also force-ends the currently-open session so day boundaries are clean. |
| `persome writer run` (CLI) | Same as the safety net — useful manually after pulling new code or for recovery. |

## Stage 1 — S2 reducer

For each session that ended, `reduce_session`:

1. Reads `timeline_blocks` in `[flush_end or session.start, session.end)` from SQLite. (For the terminal reduce, `session.end` is set; for a flush tick, the tick uses `now` as the upper bound and the session stays `active`.)
2. If the range is empty, marks the session `reduced` (no-op, terminal only) and returns.
3. Renders the blocks into `prompts/session_reduce.md` and calls the `reducer` LLM stage with `json_mode=True`.
4. Parses `{summary: str, sub_tasks: [str]}`. Each sub_task must look like `[HH:MM-HH:MM, <app>] <action>, involving <...>`.
5. Appends one entry to `event-YYYY-MM-DD.md` (the date of `session.start`). Entry header: `**Session <sid>** (HH:MM–HH:MM)` for terminal reduces, or `**Session <sid> [flush]** (HH:MM–HH:MM)` for flush passes. Flush entries carry a `flush` tag alongside `sid:<sid>` so they're easy to filter later.
6. Advances `flush_end` on the session row. For the terminal reduce, also sets `status=reduced`.

### Retry + heuristic fallback

If the LLM call fails or returns unparseable JSON:

- **Retry queue.** Backoff schedule `5 / 15 / 30 / 60 / 120` minutes (verbatim from Einsia). The session row moves to `status=failed` with `next_retry_at` set; the daily safety-net picks it up. (Flush failures don't schedule retries — the next flush covers a bigger window, and the terminal reduce is authoritative.)
- **Exhausted retries.** A heuristic entry is written (one sub_task per distinct app, tagged `heuristic`), and the row is marked `reduced`. A session is never silently lost.

### Event-daily file ownership

Event-daily files are owned by the reducer. The classifier is **forbidden** from writing to any `event-*.md` path — there's a hard guard in the classifier's tool loop that rejects such calls with an explicit error.

## Stage 2 — Classifier

Two entry points, same core (`classifier.classify_window`):

- **Tick path** — `session/tick.run_classifier_tick` fires every `classifier.interval_minutes` (default 30). For the currently-active session, it classifies the window `[classified_end or session_start, now)` and, on a committed pass, advances `classified_end` so the next tick picks up where it left off.
- **Terminal path** — the reducer's `on_done` callback classifies the trailing window `[classified_end or session_start, now)` right after the final reduce lands. This covers whatever the tick didn't reach (sessions shorter than one interval, or the tail between the last tick and the session close).

Both paths assemble the same prompt inputs:

1. The event-daily entries tagged `sid:<session_id>` whose timestamps fall in the window — these are the focus entries.
2. The timeline blocks covering the window — verbatim-preserving activity slices so the classifier can ground any durable fact against raw evidence.
3. The preceding-day's trailing entries as dedup context (terminal path only — the tick runs inside the day so it doesn't need cross-day context).
4. The memory-file index filtered to exclude `event-*` files.

Both paths then run a bounded tool-call loop over `writer/tools.py`:

| Tool | Purpose |
|---|---|
| `read_memory(path, tail_n?)` | Fetch a memory file's frontmatter + last N entries (default 10). |
| `search_memory(query, top_k?, include_superseded?)` | BM25 search — dedup check before appending. |
| `append(path, content, tags, confidence?, conflicted?, occurred_at?)` | Add a new entry to an existing file. |
| `create(path, description, tags)` | Make a new (empty) memory file. A first entry must be added via `append` in the same round. |
| `supersede(path, old_entry_id, new_content, reason, tags?, confidence?, conflicted?, occurred_at?)` | Replace an old entry. |
| `flag_compact(path, reason)` | Mark a file for compaction (run after commit). |
| `commit(summary)` | End the round. Always called exactly once. |

### Meta-cognition fields (Hy-Memory migration)

`append` / `supersede` carry three optional reliability fields, mirrored from the markdown heading tags (`#confidence:<level>` / `#conflicted` / `#occurred:<iso>`) into the `entry_metadata` derived table (markdown is SSOT; `rebuild_index` re-derives it row-for-row, a row exists iff a non-default tag is present):

- `confidence` (`high`/`medium`/`low`) — `high` = the user explicitly did/said it; `medium` = strong inference; `low` = weak/speculative. Off-vocabulary values degrade to no tag. The classifier prompt asks for this on every write.
- `conflicted` (bool) — set when a candidate contradicts an existing memory but neither supersede (Path A) nor abstract (Path B) clearly applies; surfaces the unresolved tension instead of hard-overwriting.
- `occurred_at` (ISO-8601) — the event's real time when it differs from the write time, so time-ordered recall isn't skewed.

These fields stay attached to retrieval hits and model receipts so consumers can
down-weight low-confidence or conflicted memory. Storage is always on and
pure-additive.

Iteration cap: `writer.max_tool_iterations = 12`.

The prompt is biased toward **doing nothing**: default action is an empty `commit` if no durable signal is present. Raw activity ("used Cursor for 2h", "played Slay the Spire") is explicitly *not* classifiable — that's already captured in the event-daily entry.

## Stage 3 — Compact

After a classifier commit, any file flagged for compaction (by `flag_compact` or by exceeding `soft_limit_tokens`) runs through `writer/compact.py`: LLM rewrite + fact-preservation check (rejects if >5% noun-phrase loss). Separate module so a bad compact can't take the classifier down with it.

On accept, compact now writes the compacted markdown, clears `needs_compact`, then calls `entries_mod.rebuild_index(conn)` (replacing its old per-file FTS re-ingest). This unifies superseded detection on the EVO-02 three-way judgment (`superseded-by` / `refined-from` / strike fallback) and keeps the FTS retrieval projection self-healed after the rewrite — compact is low-frequency, so one full rebuild per accept is acceptable. (This also fixed a latent bug where compact's old two-way judgment missed whole-body strikes.) Under `[evomem] write_authority="evomem"` compact is **deferred** as a whole (`compact_file` returns a `deferred` note, `needs_compact` stays set; compact-as-ops via the engine is a follow-up PR) — so the rebuild it triggers can only ever be the markdown-mode replay, never an evo_nodes projection over a freshly LLM-rewritten file.

## Stage 4 — Schema miner (D2, Hy-Memory)

`writer/schema_miner_stage.py` is the System2 "high-level cognition" stage. It clusters durable facts **per memory file** (`user-/project-/topic-/person-*.md`, non-superseded entries), feeds each cluster of ≥`min_facts` (4) to `evomem.schema_miner.SchemaMiner` (LLM via `[models.schema_miner]` → `call_llm`), and writes the result as a `schema-<slug>.md` memory file — `central_proposition` + `supporting_summary` body, `expected_inferences` + `confidence` + `status` (`stable` when confidence ≥0.6, else `forming`) in tags. A still-`forming` schema is also born **`dormant`** at the *file* level (`create_file(status=…)` writes the status to both the markdown frontmatter and the files-table row, so it survives `rebuild_index` without drift) — hidden from default `list_memories` / FTS until it matures, keeping immature faces out of the active model; a `stable` schema is `active`. The slug derives from the source file, so re-mining the same cluster supersedes the schema in place (idempotent, no file pile-up); each re-mine also flips the file `dormant`↔`active` via `entries.set_file_status` to match the new maturity (promote on `forming→stable`, demote on the reverse). Entry point `mine_schemas_for_user(conn)` is a pure, testable function. `persome.model.schema_reader` exposes stable inferences to snapshot and retrieval consumers.

> `writer/reconcile_apply.py` was deleted in PR-6a (write-entrance groundwork): its translation duty — evomem `ReconcileResult` → write口 — is carried natively by the evomem engine (`engine._apply_op` lands evo_nodes, the projection follows). Its two legacies (ABSTRACT chain-semantics ② and the caller-stage event fence) moved into the engine with it.

## Stage 5 — Cross-domain sweeper

`writer/cross_domain_sweeper.py` runs after the per-file miner inside the shared
model build (gated on `[schema] cross_domain_enabled`, default on). The per-file
miner can only see within-topic regularities; the sweeper finds topic-far but
behavior-near candidates.

- **behavior dimension = deterministic signature, no embedding.** Each stable schema's source facts that carry an `occurred_at` (batch-1 meta-cognition) are traced to the surrounding `timeline_blocks`; the signature is app set + action-type distribution + hour histogram, distance = Jaccard + total-variation. It is a **cheap pre-filter** — an ungrounded schema (no `occurred_at` facts) yields distance 0 and is passed through to the LLM rather than filtered on the offset write-`timestamp`.
- **topic dimension = LLM judge.** Only `_topic_distinct` + behavior-near pairs reach `[models.cross_domain_sweeper]` (`call_llm`, JSON), which decides collision and fuses.
- **landing reuses the model schema reader.** Fused schemas land as `schema-xdomain-<a>__<b>.md` (same `schema-` prefix, stable/forming + confidence tags), so `model.schema_reader.active_schema_inferences` reads them like any schema. Idempotent per unordered pair (re-sweep supersedes in place). `schema-xdomain-*` is excluded from the base set, so a fused schema is never re-fused.
- **default on, bounded downside**: a low-quality collision gets a low LLM confidence → fused schema born `forming` → excluded from active model reads (only `stable` ≥ `cross_domain_min_confidence` fusions are visible), and the prompt is biased to refuse strained merges. The main cost is the per-tick LLM probes, capped by the topic/behavior pre-filter. Set `[schema] cross_domain_enabled = false` to disable. Entry point `sweep_cross_domain(cfg, conn, *, llm_call=None)` is a pure, testable function.

## Sessions table

```sql
CREATE TABLE sessions (
  id TEXT PRIMARY KEY,             -- sess_<12hex>
  start_time TEXT NOT NULL,
  end_time TEXT,
  status TEXT NOT NULL,            -- active | ended | reduced | failed
  flush_end TEXT,                  -- reducer bookmark: upper bound of last reduced window
  classified_end TEXT,             -- classifier bookmark: upper bound of last classifier pass
  retry_count INTEGER NOT NULL DEFAULT 0,
  next_retry_at TEXT,
  last_error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

Lives in `index.db` alongside `entries` / `files` / `timeline_blocks`. The reducer uses it to bookkeep retries; the safety-net cron uses `status IN ('ended','failed')` to find anything still owed work.

## Per-stage model picks

Defaults inherit from `[models.default]`. Override in `config.toml`:

- **`[models.reducer]`** — prompt is short (timeline blocks are already compressed), but output precision matters (time ranges, per-app attribution). A mid-tier model is usually the right trade-off.
- **`[models.classifier]`** — accuracy-sensitive. The classifier decides what becomes long-term memory; a weak model here means either missed facts or poisoned dedup.
- **`[models.timeline]`** — runs every minute of activity as a verbatim-preserving normalizer. Keep it cheap, but don't go too weak — a too-weak model will summarize instead of normalizing and drop authored text.
- **`[models.compact]`** — runs only when files fatten. Match reducer or stronger.

> **`writer/chat_title.py` is NOT a `[models.*]` stage.** Sidebar title generation runs through `chat.agent.complete_sync` (same Anthropic SDK path the chat agent itself uses), reading `[chat] model` + `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL`. Piggy-backing on the chat agent's already-validated provider means title generation "just works" for anyone who can chat, with no extra config.

## Prompt caching

Every stage talks the Anthropic Messages API (via `writer/llm.py` → the official SDK), so `cache_control` breakpoints pass straight through to the gateway natively — no prefix or provider gating. Three stages emit breakpoints to cut input-token cost:

- **`timeline`** — system block (~3.7K tokens of normalizer rules) is cached per call; high reuse across the 60-second tick cadence
- **`classifier`** — tool-loop reuses the same system+tools prefix across every round inside one classifier invocation; same cached prefix again at the next classifier tick (30-minute cadence)
- **`session_reducer`** — preceding-entries section is its own cache breakpoint; that section grows prefix-style across flush ticks (each call's preceding_text ⊇ the prior call's), so flush N+1 hits the cache written by flush N

The `model` field is a **bare name** (e.g. `deepseek-v4-flash`, `claude-haiku-4-5`) sent verbatim to `ANTHROPIC_BASE_URL`; legacy `anthropic/...` prefixes in an old `config.toml` are tolerated (stripped by `_bare_model`). Caching needs the gateway to honor `cache_control` — DeepSeek's `/anthropic` and native Anthropic both do.

**Symptom of a non-caching gateway:** `usage.cache_read_input_tokens` stays at 0 across many requests. The request still succeeds; only the cost optimization is lost.

## Logs

```
~/.persome/logs/writer.log    # reducer + classifier tool-call loops, commit summaries
~/.persome/logs/session.log   # flush tick + classifier tick + terminal reduce callback lines
~/.persome/logs/compact.log   # compact rounds + preservation ratios
```

A flush (every 5 min) produces one reducer line in writer.log + a "flushed" line in session.log. A classifier tick (every 30 min) produces either a "skipped (no session entries in window)" line in session.log, or a write-summary + tool-call trail in writer.log. At session-end you'll see the terminal reducer followed by the terminal classifier callback in session.log; the classifier's own tool calls land in writer.log.

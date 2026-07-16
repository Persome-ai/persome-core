# State and personal-model writers

Persome has narrow, auditable write stations rather than one unrestricted
agent. Session reduction records what happened; windowed modeling creates
Points and Lines incrementally; slower build stages create Faces, Volumes, and
Root.

## Ownership map

| Writer | Input | Output |
|---|---|---|
| session reducer | timeline blocks | `event-YYYY-MM-DD.md` |
| memory delta + apply | one newly flushed session window | owner-alias evidence plus entities, assertions, events, and relation Lines in evomem/FTS/Markdown projection |
| pattern detector | repeated event evidence | `memory/skills/skill-*.md` |
| case extractor | error followed by supported resolution | reusable L5 knowledge Points |
| schema miner | repeated durable facts | level-1 Face candidates |
| cross-domain sweeper | stable topic-distinct Faces | level-2 Volumes |
| root synthesis | active Face/Volume/profile evidence | at most one level-3 Root |
| CLI/MCP correction | explicit user/agent request | audited append, supersede, retype, merge, merge-into-self, reject-owner-alias, or revoke |

No writer owns product tasks, notifications, actuation, or evaluation labels.

## Session reducer

`writer/session_reducer.py` reads timeline blocks in the unflushed part of a
session and asks the `reducer` stage for:

```json
{"summary": "...", "sub_tasks": ["[09:00-09:05, App] action, involving ..."]}
```

Incremental calls append `[flush]` entries and advance `flush_end`. A terminal
call appends the trailing entry and marks the session reduced. An empty terminal
window is a successful no-write reduction because earlier flushes may already
cover it.

When timeline blocks carry attention-locus metadata, the reducer prompt adds up
to 12 dwell-ranked surface titles. Dwell sums only observed block duration;
tolerated gaps keep the trajectory readable but never add time. Raw titles are
whitespace-normalized, limited to 240 characters, JSON-quoted, and explicitly
marked as untrusted data rather than instructions.

Malformed output enters the persisted reducer retry queue. After five attempts,
the deterministic heuristic writes one coarse subtask per observed app, tagged
`heuristic`. Event files are reducer-owned; other writers may read but cannot
write them.

## Windowed memory delta

With shipped defaults (`memory_delta.enabled=true`, `apply_enabled=true`), the
live Point/Line path is:

```text
new timeline window + structured focus evidence
  -> one memory_delta LLM extraction
  -> owner-alias evidence + quote / roster / predicate / confidence gates
  -> repeated owner evidence resolves names and handles to reserved self
  -> append-only memory_deltas audit row
  -> deterministic delta_apply
  -> Points, assertions, events, and relation Lines
```

Every proposed item must quote session evidence. Entity references must resolve
through the identity roster or appear explicitly in the session. Relations use
a closed predicate set. Low-confidence or unsupported items are dropped and
counted.

The roster always includes the reserved `self` endpoint. The same LLM pass may
propose `owner_alias_candidates` only when quoted session evidence identifies a
proper name or handle as an explicit self-identification or owned account. One
non-explicit observation stays `pending` and enters a bounded seven-day
PersonGraph quarantine; two independent sessions promote it to an active alias
of `self`. A quoted explicit
first-person identity statement may promote immediately. Promotion retires an
already-minted duplicate person projection without deleting its audit history.

Configured `memory_delta.owner_aliases` remain a trusted override, but ordinary
operation does not require users to discover or fill the setting themselves.
Persome's own localhost `/model` output is removed from the delta evidence so a
rendered Face, Volume, or Root cannot train the next model window on itself.

`delta_apply` is deliberately limited to the SQLite-backed Point/Line update.
It rejects any batch that also contains an explicit source supersede, because a
Markdown or evomem source-of-truth change cannot share that SQLite savepoint.
User-directed corrections instead use the single correction entrance: one
write-authority decision is frozen for the operation, same-file writers are
serialized across threads and processes, and evomem head replacement is a
transactional compare-and-swap. After the source commits, the retrieval
projection is rebuilt. A projection failure is reported as a committed but
degraded correction and retrying the same replacement is idempotent; a
different replacement for the same retired head is rejected.

The deterministic `self engaged_with <entity>` attention floor is direct
observational evidence, so it becomes an active Line on the first applied
window. LLM semantic relations remain shadow candidates. Repeated deterministic
co-occurrence increments their independent observation count; the background
structural build promotes only candidates meeting the evidence floor and
per-identity fan-out cap.

Persist-before-apply is deliberate. `apply_status` is `pending`, `applied`, or
`failed`; a retry reuses the stored window payload and only resumes apply.
`sessions.delta_end` advances only after success, keeping cost and
relation-observation counts idempotent. Terminal finalization processes only
the remaining tail.

When `apply_enabled=false`, the delta remains an audit artifact and the legacy
classifier regains the terminal durable-fact role. This is a compatibility and
diagnostic switch, not the documented Runtime default.

## Classifier compatibility path

The bounded tool loop in `writer/classifier.py` can read/search memory and use
`create`, `append`, `supersede`, `flag_compact`, and `commit`. It cannot write
`event-*`. Under default delta apply it returns the deliberate skip
`classifier retired (delta apply)` and the periodic classifier task is not
started. The code remains reachable for old stores that explicitly disable
delta apply.

## Behavioral memory

`writer/pattern_detector.py` uses repeated event evidence, deterministic
candidate filtering, and an LLM validation pass. Confirmed observations land in
`skills/skill-*.md` with evidence and `stage: observed`. A single occurrence is
insufficient. Timeline skill echoes are deduplicated by session, so repeated
minute blocks inside one continuous episode count as one observation. MCP
`behavior_patterns` exposes the latest active evidence-backed playbooks beside
the resident Root and Faces. The stage models a person's recurring behavior; it
does not propose scripts, grant permission, or execute automation.

## Higher-level build

The dirty-gated 30-minute refresh, `persome model build`, and the unconditional
00:15 daemon schedule share `model/ModelBuildCoordinator` and one
`model-build.lock`.

### Reusable cases

`case_extractor` deterministically finds error-to-resolution windows, then asks
the LLM to distill supported problem/solution cards. Unresolved errors are not
minted. Cards enter the public deterministic evomem write entrance.

### Attention digest

`attention_digest` is disabled by default. When explicitly enabled, it
deterministically folds the day's attention-locus dwell (the Step-1
`attention_*` columns on timeline blocks) into one ranked
`user-attention.md` fact per calendar day — no LLM. The surface value is a raw,
screen-derived window, pane, tab, or document title; it is whitespace-normalized,
length-bounded, and quoted as data, but it is not anonymized. Enabling this
stage therefore copies that title into independently retained durable memory
and schema-miner input; `persome clean timeline` does not remove the digest.

Only observed block duration counts toward dwell; tolerated gaps in the
trajectory view are not counted. Surfaces under five minutes are excluded. A
same-day re-run supersedes that day's digest atomically instead of appending,
and both the initial and successor nodes record the local day boundary,
observation time, exact dwell, and contributing timeline-block IDs. Because
`user-` is a schema-miner fact prefix, enabled digests can become Face evidence
through the existing promotion gates.

### Faces

`schema_miner_stage.mine_schemas_for_user` groups durable facts per memory
domain. Bundles smaller than four facts are skipped. A schema contains a central
proposition, support summary, expected inferences, confidence, and source
receipts. Low-confidence output is `forming` and excluded from the active model;
stable output contributes a Face. Derived PersonGraph entity/event nodes are
excluded from schema mining; only durable person facts can support a person
Face. Owner-scoped Faces anchor to `self`; collaborators mentioned only in the
supporting receipts are not added as hull identities. Re-mining supersedes the
prior schema in place.

### Volumes

`cross_domain_sweeper` compares stable, topic-distinct schemas using a
deterministic behavior signature before an LLM judge. Confirmed repeated
cross-domain structure becomes a Volume. Person schemas are not eligible inputs:
collaborator behavior cannot be fused into the owner's project/tool/topic model.
Forming candidates stay outside active snapshots. The LLM stage has a hard
per-build probe budget (8 by default), with
live shadow Volumes whose latest result is still stable/promotable ordered before
unseen pairs so bounded builds can satisfy the unchanged two-observation
promotion gate. A negative, failed, or low-confidence retry immediately lowers
that shadow into the oldest-first retry queue; it cannot reserve top priority on
later builds. Probe history is persisted locally, so every finite set of unseen
pairs receives budget before those rejected retries return. Low-confidence
`forming` collisions remain dormant and never contribute promotion evidence.
Deferred candidates remain eligible for later structural builds.

### Root

`root_synthesis` compresses active Face/Volume/profile evidence into at most one
Root under a token budget. A new valid Root supersedes the old one; missing or
failed input never replaces a valid Root with empty content.

## Compaction and forgetting

`writer/compact.py` is per-file only. It rewrites a large Markdown file and
rejects the candidate if the preservation check loses more than 5% of unique
noun phrases. `writer.consolidation_cadence` periodically drains files marked
`needs_compact`; compaction is deliberately per-file.

Optional nightly maintenance is off by default:

- `memory_decay` distills old, never-retrieved fact clusters;
- `orphan_reaper` retires old one-off entity Points with no meaningful edges;
- contradiction check marks an adjudication queue and never auto-deletes one
  side of a disagreement.

Reads reinforce memory: retrieved entries are protected from decay.

## Authority and projections

All writes converge on `store/entries.py` or deterministic evomem entrances.

- `write_authority="markdown"` (default): Markdown is truth; the shadow hook
  mirrors current entries into evomem.
- `write_authority="evomem"`: evomem is truth; FTS and Markdown are projections.
  Event files and skill Markdown retain their narrow legacy path.

Never add another independent truth store. Rebuild commands regenerate the
selected projections and preserve receipts/history.

## LLM and failure rules

- Stage calls use `writer/llm.py` with the profile selected by
  `persome llm setup`. Anthropic Messages and OpenAI-compatible Chat
  Completions share one response/tool-loop contract; keys come from
  `<PERSOME_ROOT>/env`.
- During an explicit MCP `process_pending_model_work` request, the same stage
  contract is temporarily backed by MCP Sampling. The connected client spends
  its own model allowance; Persome receives completions, never its login token.
  This override is request-scoped and does not enable unattended daemon calls.
- Terminal finalization sets `sessions.modeled_at` only after every enabled
  stage completes or reports a deliberate benign skip.
- Semantic-stage errors degrade the model and remain retryable; they do not
  fabricate geometry.
- Explicit model build records stage failures, model names, prompt hashes,
  config hash, input window, and commit in `model-build.json`.

## Logs and recovery

`logs/writer.log` records reducer/modeling calls; `logs/session.log` records
cuts, retries, and finalization; `logs/compact.log` records preservation checks.

```bash
persome writer run    # catch up reductions and terminal modeling
persome model build   # then build Face/Volume/Root/layout
persome model status
```

# State and personal-model writers

Persome has narrow, auditable write stations rather than one unrestricted
agent. Session reduction records what happened; terminal modeling creates
Points and Lines; slower build stages create Faces, Volumes, and Root.

## Ownership map

| Writer | Input | Output |
|---|---|---|
| session reducer | timeline blocks | `event-YYYY-MM-DD.md` |
| memory delta + apply | one reduced session | entities, assertions, events, relation Lines in evomem/FTS/Markdown projection |
| pattern detector | repeated event evidence | `memory/skills/skill-*.md` |
| case extractor | error followed by supported resolution | reusable L5 knowledge Points |
| schema miner | repeated durable facts | level-1 Face candidates |
| cross-domain sweeper | stable topic-distinct Faces | level-2 Volumes |
| root synthesis | active Face/Volume/profile evidence | at most one level-3 Root |
| Chat/MCP correction | explicit user/agent request | audited append, supersede, retype, merge, or revoke |

No writer owns product tasks, notifications, actuation, or benchmark labels.

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

Malformed output enters the persisted reducer retry queue. After five attempts,
the deterministic heuristic writes one coarse subtask per observed app, tagged
`heuristic`. Event files are reducer-owned; other writers may read but cannot
write them.

## Terminal memory delta

With shipped defaults (`memory_delta.enabled=true`, `apply_enabled=true`), the
terminal Point/Line path is:

```text
session timeline + structured focus evidence
  -> one memory_delta LLM extraction
  -> quote / roster / predicate / confidence gates
  -> append-only memory_deltas audit row
  -> deterministic delta_apply
  -> Points, assertions, events, and relation Lines
```

Every proposed item must quote session evidence. Entity references must resolve
through the identity roster or appear explicitly in the session. Relations use
a closed predicate set. Low-confidence or unsupported items are dropped and
counted.

Persist-before-apply is deliberate. `apply_status` is `pending`, `applied`, or
`failed`; a terminal retry reuses the stored payload and only resumes apply.
This keeps cost and relation-observation counts idempotent.

When `apply_enabled=false`, the delta remains an audit artifact and the legacy
classifier regains the terminal durable-fact role. This is a compatibility and
diagnostic switch, not the documented paper Runtime default.

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
insufficient. The stage models a person's recurring behavior; it does not
propose scripts or execute automation.

## Higher-level build

`persome model build` and the 00:15 daemon schedule share
`model/ModelBuildCoordinator` and one `model-build.lock`.

### Reusable cases

`case_extractor` deterministically finds error-to-resolution windows, then asks
the LLM to distill supported problem/solution cards. Unresolved errors are not
minted. Cards enter the public deterministic evomem write entrance.

### Faces

`schema_miner_stage.mine_schemas_for_user` groups durable facts per memory
domain. Bundles smaller than four facts are skipped. A schema contains a central
proposition, support summary, expected inferences, confidence, and source
receipts. Low-confidence output is `forming` and excluded from the active model;
stable output contributes a Face. Re-mining supersedes the prior schema in place.

### Volumes

`cross_domain_sweeper` compares stable, topic-distinct schemas using a
deterministic behavior signature before an LLM judge. Confirmed repeated
cross-domain structure becomes a Volume. Forming candidates stay outside active
snapshots.

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

- Stage calls use `writer/llm.py`; keys come from `<PERSOME_ROOT>/env`.
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

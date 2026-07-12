# Personal model format

The public personal model is a versioned, read-only JSON projection of local
Runtime state. CLI export, MCP `get_model_snapshot`, and the `model` object
inside `/model/graph` expose the same schema; consumers must not import internal
DAOs or query SQLite tables directly. Redaction policy differs: export and MCP
redact by default, while the owner-only loopback viewer uses raw local content.

## Top-level schema

```json
{
  "schema_version": 1,
  "generated_at": "2026-07-10T09:06:00+00:00",
  "build": {},
  "points": [],
  "lines": [],
  "faces": [],
  "volumes": [],
  "root": null,
  "receipts": [],
  "stats": {}
}
```

Consumers must branch on `schema_version`. Package versions do not substitute
for a schema check.

The `build` object has one fixed key set in every state. While a build is in
progress or no valid completed build exists, unavailable identity fields are
`null`, maps are empty, and input-window bounds are `null`; these sentinels do
not claim a successful build.

## Geometry

### Point

A Point is one evomem node, including historical nodes needed to reconstruct
evolution. Important fields include:

```text
id, content, layer, status, is_latest,
supersedes, superseded_by,
occurred_at, valid_from, valid_until, created_at,
file_name, tags, confidence, conflicted, receipt
```

`is_latest` identifies a current chain head; historical Points remain available
for audit and time travel.

### Line

Lines have two forms:

- `kind: evolution`: one Point supersedes another;
- `kind: relation`: a semantic/entity relation with predicate, confidence,
  validity, and provenance.

Activity-derived relation Lines carry the atomic source triplet
`source_kind`, `source_id`, and `source_receipt`. New activity identities use
`event:entry:<id>` or `event:session:<id>`. `event:intent:<id>` exists only for
read-only migration of old data.

### Face

A Face is one active level-1 `schema_faces` row. It contains a behavioral
signature, members, observations, confidence, provenance, anchors, and source
receipts. Promotion requires stable repeated support.

### Volume

A Volume is one active level-2 cross-domain schema. It relates behavior across
otherwise separate owner-scoped topics and carries the same audit fields as a
Face. Person schemas are excluded so evidence about a collaborator cannot be
fused into the memory owner's behavior.

### Root

`root` is `null` or one active level-3 apex. More than one live Root is a
contract error. Root receipts aggregate evidence through its members so the
summary can be expanded back to Points.

## Receipts

Receipts are stable evidence handles rather than embedded raw capture payloads.
Each Point has a receipt; sourced relations and aggregate geometry preserve or
collect those handles. The MCP `read_receipt` tool resolves a handle to current
local evidence.

## Build record

The `build` object records:

```text
build_id, core_commit, models, prompt_hashes, config_hash,
input_window, mode, trigger, started_at, completed_at,
duration_ms, degraded_stages, status
```

No API keys or full configuration values are copied into the manifest.
`build_id` is the stable hash of every other manifest field; `complete` requires
an empty `degraded_stages`, while `degraded` requires at least one stage.

Live HTTP, MCP, and CLI-export snapshots use the last persisted completed or
degraded build record. If no valid build record exists, they report `status: not_built`,
`trigger: no_completed_build`, and a null `build_id`; inspecting the current
database projection never fabricates a successful build. A raw
`model-build.json` with `status: building` is exposed as `building` only while
the process still holds `model-build.lock`. If that lock is free, the marker is
an interrupted-build remnant and public surfaces report `not_built`.

## Stats

`stats` reports Point count, evolution/relation Line counts, Face/Volume/Root
counts, receipt count, and redaction counts. Complete geometry requires
non-empty Points, at least one Line, at least one Face and Volume, and exactly
one Root.

## Redaction and export

```bash
persome model export
persome model export --out ./model-snapshot.json
persome model export --raw  # explicit sensitive-data opt-out
```

Default export applies the Runtime's deterministic secret/PII scrubber, removes
detectable absolute paths and sensitive text categories, writes atomically, and
sets mode `0600`. This is a sharing aid, not a guarantee that all names or
organizations are anonymous. Publishing an export requires separate consent
and anonymization review.

The detailed implementation contract is in
[`docs/model-contract.md`](docs/model-contract.md). The schema golden is
[`tests/fixtures/runtime_model/model_snapshot_v1.golden.json`](tests/fixtures/runtime_model/model_snapshot_v1.golden.json).

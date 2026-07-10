# Personal model format

The paper-facing model is a versioned, read-only JSON projection of local
Runtime state. CLI export, MCP `get_model_snapshot`, and `/model/graph` expose
the same contract; consumers must not import internal DAOs or query SQLite
tables directly.

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
otherwise separate topics and carries the same audit fields as a Face.

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

## Stats

`stats` reports Point count, evolution/relation Line counts, Face/Volume/Root
counts, receipt count, and redaction counts. A complete paper model requires
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
organizations are anonymous. Benchmark publication requires separate consent
and anonymization review.

The detailed implementation contract is in
[`docs/model-contract.md`](docs/model-contract.md). The schema golden is
[`tests/fixtures/paper_model/model_snapshot_v1.golden.json`](tests/fixtures/paper_model/model_snapshot_v1.golden.json).

# Model snapshot contract

`persome.model` is the public, read-only projection of the model stored by the Runtime.
It does not define a second model or run capture/build jobs. It turns the current SQLite state into
one versioned JSON object that a viewer, MCP adapter, or external client can consume without importing
internal DAOs.

The operator surface is:

```bash
persome model build
persome model status
persome model export --out model-snapshot.json
```

`model build` uses an exclusive `<PERSOME_ROOT>/model-build.lock`. It waits up to 30 seconds by
default; `--wait-seconds` changes the bound and `--no-wait` returns `busy` immediately. The kernel
releases the lock on process exit. Successful or degraded runs atomically write an owner-only
`model-build.json`; missing Points, Lines, Faces, Volumes, or Root is recorded as degraded, never
as success. `model status` uses the same completeness rule for its `ready` result.

## Geometry

The contract exposes:

- `points`: evomem nodes, including historical nodes needed to reconstruct evolution chains.
- `lines`: vertical `supersedes` edges and currently active horizontal relation
  edges. Directly observed `engaged_with` attention is active immediately;
  inferred semantic edges remain hidden until repeated evidence promotes them.
- `faces`: active level-1 schemas.
- `volumes`: active level-2 cross-domain schemas.
- `root`: zero or one active level-3 apex. More than one live root is a contract error.
- `receipts`: stable evidence handles for points and sourced relation lines. Face, Volume, and Root
  objects aggregate `source_receipts` through their member chain so the apex can be audited down to
  fact evidence.
- `build` and `stats`: build identity/timing plus auditable object and redaction counts.

`schema_version` starts at `1`. Consumers must branch on this field instead of inferring a version
from package releases.

Every `build` object records the core commit, stage model names, prompt hashes, a config hash, input
window, mock/real mode, timing, and degraded stages. Configuration values themselves are not copied
into the manifest. Fixed inputs and timestamps produce the same `build_id`.

A Face becomes active only after mined and emergent signals agree across stable footprints. A
Volume has one honest producer (the cross-domain sweeper), so it becomes active after two stable
sweeper resamples. This preserves the two-observation bar without inventing a second extractor.

## Evidence sources

Relation edges may carry the nullable triplet `source_kind`, `source_id`, and `source_receipt`.
The triplet is atomic: callers either provide all three fields or none. Activity-derived edges use
new stable IDs `event:entry:<id>` or `event:session:<id>`. `event:intent:<id>`
is read-only compatibility for an old store.

## Privacy and reproducibility

`export_snapshot` redacts deterministic secret/PII categories by default and writes atomically with
mode `0600`. Callers must opt out explicitly with `redact=False`. A fixed `generated_at` and fixed
`build_metadata` produce byte-equivalent model data, assuming the underlying database is unchanged.

The `model` object in loopback `/model/graph` uses the same schema but raw local
content so the owner can inspect the real model. It is not a publication export.

The synthetic contract fixture lives at
[`tests/fixtures/runtime_model/model_seed.json`](../tests/fixtures/runtime_model/model_seed.json). It
contains no screenshots or harvested user data and exercises Point, evolution Line, relation Line,
Face, Volume, Root, and receipt projection from a fresh temporary data root.

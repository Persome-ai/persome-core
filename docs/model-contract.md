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

## Human-readable projection

After a build, Persome deterministically projects the raw current snapshot to
`<PERSOME_ROOT>/HUMAN.md` (`~/.persome/HUMAN.md` by default). The file is an
owner-local `0600` reading surface, not a second model contract: the versioned
JSON snapshot, build manifest, and evidence APIs remain the machine authority.
No additional capture or LLM call is needed to render it.

Daemon startup and `persome onboard` reconcile the file from an existing valid
Root, so upgrades backfill it without rebuilding the user's history. With no
verified Root, Persome writes a truthful forming placeholder and replaces that
placeholder after later model builds. Automatic refresh replaces only files
with Persome's projection marker. If an unrecognized, self-authored
`HUMAN.md` already occupies the path, Persome preserves it and reports the
conflict instead of overwriting it. Direct edits to a managed projection are
not a correction interface and may be replaced; use `persome correct` for model
changes.

`model build` uses an exclusive `<PERSOME_ROOT>/model-build.lock`. It waits up to 30 seconds by
default; `--wait-seconds` changes the bound and `--no-wait` returns `busy` immediately. The kernel
releases the lock on process exit. A run first atomically records `status: building`, invalidating
any older completed manifest before a mutating stage starts. Successful or degraded runs replace it
with an owner-only `model-build.json`. That marker is exposed as `building` only while the process
holds `model-build.lock`; once the lock is free, a leftover marker is exposed as `not_built`, never
as the previous success. Missing Points, Lines, Faces, Volumes, or Root is recorded as degraded.
`model status` also requires a valid completed/degraded manifest before reporting `ready`.

Before each canonical stage starts, Core atomically appends a `running` stage
receipt to the separate owner-only `model-build-stages.json` artifact. It
atomically replaces that receipt with `complete`, `skipped`, `failed`, or
`interrupted` afterward. Thus a normal exception has a fixed failure receipt
and a process crash preserves the last atomic state (`running` when it exited
inside a stage); neither can reuse an older success. This sidecar does not
change the public snapshot or `model-build.json` schema.

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

The execution sidecar binds its final result to that unchanged manifest through
`build_id`, a safe `core_commit` label plus digest, `config_hash`, a full
canonical manifest digest, and a trigger digest. It contains only fixed status/error codes, processing
timestamps, non-negative counters or explicit SHA-256 digests. Prompt/response
text, exception messages, paths, and arbitrary personal strings are invalid.
The callback test seam is marked `override`; callback-returned stage
dictionaries are ignored, so a caller cannot self-report a canonical success.
The canonical Core order is `state_formation`, `evomem_baseline`,
`entity_relation_enrichment`, `schema_miner`, `cross_domain_sweeper`,
`root_synthesis`, `vector_backfill`, and `model_contract`. Complete receipts
have an exact stage-specific output-counter set; the final `model_contract`
counters match the persisted geometry and are the independent attestor's
recomputation surface. Sidecar digests use canonical compact, key-sorted UTF-8
JSON and full SHA-256.
The live HTTP, MCP, and CLI-export projections preserve that persisted manifest
exactly. The manifest `build_id` must match the stable hash of every other manifest field, and
`complete`/`degraded` must agree with an empty/non-empty `degraded_stages` list. If there is no valid
completed or degraded manifest, the projection reports
`status: not_built`, `trigger: no_completed_build`, and a null `build_id`
instead of synthesizing a completed build from the current database contents.
The `not_built` and `building` states keep the same fixed build-object keys;
unavailable commit, config hash, mode, and timestamps are null, model and prompt
maps are empty, and the input-window bounds are null.

A Face becomes active only after mined and emergent signals agree across stable footprints. A
Volume has one honest producer (the cross-domain sweeper), so it becomes active after two stable
sweeper resamples. This preserves the two-observation bar without inventing a second extractor.

## Viewer layout

The loopback viewer projects the snapshot as a deterministic hierarchy; it does not mutate or
re-cluster stored model objects. Root stays at the center, Volumes occupy the inner shell, Faces
form outward semantic clusters, and Points grow as stable local clouds around their primary Face.
The viewer resolves Face membership through `member_receipts` and infers Volume-to-Face membership
from inherited `source_receipts`, because stored `members` may be internal stable keys rather than
public node IDs. Evolution-chain and same-source evidence inherit a Face cluster when possible;
unpromoted evidence remains in deterministic source clusters instead of a flat global ring.

The layout is append-stable for normal chronological growth: existing nodes keep their local
coordinates while later evidence expands the surrounding cloud. `window.__persomeLayoutState`
exposes aggregate layout health for local visual smoke tests without exposing node content or IDs.

The viewer presents that hierarchy as a personal constellation: a Root-centered luminous core,
Volume and Face orbit structures, Point clouds, and restrained ambient depth cues. Its editorial
frame uses the live Root signature as the model's plain-language identity statement so each view is
recognizably personal without changing the snapshot. The `Local only` treatment is descriptive, not
a publishing control. The viewer never uploads the model or exposes its owner-only URL. An explicit
`Share` action renders a fixed-size PNG locally from the unlabeled WebGL constellation, adds the Root
identity statement, up to three highest-level Volume or Face signatures, aggregate layer counts, and
Persome branding, downloads it, and opens an X composer with one of three standard copy variants,
each carrying the Personal Model tag and official account mention. Individual
Point labels, receipts, source names, timestamps, and viewer credentials are excluded from the share
artifact; the owner attaches the downloaded image and confirms the post in X.
Both share actions take their written summaries and aggregate counts from the canonically scrubbed
`/model/share-card` projection rather than the owner-only graph. The adjacent `Card` action remains
a separate renderer that downloads the portrait `my-human-card.png` without opening X.

Visible node labels and their Point, Face, Volume, Root, or context meshes open the same provenance
detail panel. Overview summarizes the evidence footprint, Evidence presents human-readable source
cards with drill-down breadcrumbs, and History keeps Point predecessor/successor versions separate
from derivation sources. Raw IDs, paths, and receipts stay collapsed under technical details. Labels
and tabs are keyboard-focusable; Escape closes the selection. Nodes retain a 12-pixel minimum
screen-space hit target so distant geometry stays selectable. Evolution and relation Lines open
their own human-readable endpoint, exact predicate, and evidence detail through an 8-pixel
screen-space hit target; node hits always win where geometry overlaps. Keyboard focus reveals a
line picker with the same detail action. Raw line and endpoint IDs remain inside collapsed technical
details. Derived hierarchy connectors remain visual-only.
`window.__persomeInteractionState` exposes aggregate interaction counts and hit-target bounds for
local smoke tests.

Zoom is relative to the fitted model: the visible minus, percentage, and plus controls cover 50%
through 400%, the percentage resets to 100%, and the plus, minus, and zero keys provide the same
actions. Wheel and trackpad pinch gestures zoom toward the pointer. `window.__persomeZoomState`
exposes only aggregate distance and percentage values for local visual smoke tests.

## Evidence sources

Relation edges may carry the nullable triplet `source_kind`, `source_id`, and `source_receipt`.
The triplet is atomic: callers either provide all three fields or none. Activity-derived edges use
new stable IDs `event:entry:<id>` or `event:session:<id>`. `event:intent:<id>`
is read-only compatibility for an old store.

The read-only `resolve_evidence` MCP tool and authenticated `GET /model/evidence?ref=...` endpoint
return `label` for human display and retain `reference` as the stable technical handle. Explicit
derivation edges are in `sources`, time-adjacent investigation clues in `context`, and Point version
edges in `history`. A missing retained payload stays inspectable as `status=missing`.

## Privacy and reproducibility

`export_snapshot` redacts deterministic secret/PII categories by default and writes atomically with
mode `0600`. Callers must opt out explicitly with `redact=False`. A fixed `generated_at` and fixed
`build_metadata` produce byte-equivalent model data, assuming the underlying database is unchanged.

The `model` object in loopback `/model/graph` uses the same schema but raw local
content so the owner can inspect the real model. It is not a publication export.
`HUMAN.md` uses the same raw owner-local boundary and likewise must not be
treated as a safe sharing artifact.

The synthetic contract fixture lives at
[`tests/fixtures/runtime_model/model_seed.json`](../tests/fixtures/runtime_model/model_seed.json). It
contains no screenshots or harvested user data and exercises Point, evolution Line, relation Line,
Face, Volume, Root, and receipt projection from a fresh temporary data root.

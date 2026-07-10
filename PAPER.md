# Paper and code map

Persome is split into two research artifacts with different responsibilities:

- **persome-core** forms, stores, exposes, and corrects a personal model from a
  local event stream.
- **persome-bench** evaluates next-personal-state prediction against a pinned
  core release and a versioned model snapshot.

This split prevents benchmark code from depending on a researcher's private
SQLite database and prevents the Runtime from presenting an unfinished
predictor as a paper result.

## Claim-to-artifact map

| Paper claim | Repository | Executable artifact |
|---|---|---|
| State Formation | core | capture -> timeline -> session -> reducer -> terminal model finalizer |
| Personal Weights | core | Point/Line/Face/Volume/Root model and `schema_version: 1` snapshot |
| Evidence and reversibility | core | receipts, bitemporal nodes, `remember`, `correct_memory`, rebuild |
| Model access | core | `/model`, MCP, optional Chat, `persome model export` |
| Next-personal-state Prediction | bench | predictor and replay runner over pinned snapshots |
| Evaluation | bench | datasets, top-k metrics, calibration, cost/latency, and ablations |
| Training Loop | both | bench replay -> observed error -> explicit core correction -> new snapshot |

## What core implements

The Runtime turns observed activity into a model with five inspectable levels:

1. **Point**: a sourced fact or historical fact state.
2. **Line**: an evolution or semantic relation between Points/entities.
3. **Face**: a level-1 behavioral schema supported by multiple observations.
4. **Volume**: a level-2 cross-domain schema supported across Faces.
5. **Root**: at most one active level-3 apex, expandable to source receipts.

The public contract is documented in [MODEL_FORMAT.md](MODEL_FORMAT.md). The
fresh-root synthetic path is pinned by
[`tests/test_paper_model_e2e.py`](tests/test_paper_model_e2e.py) and
[`tests/fixtures/paper_model/`](tests/fixtures/paper_model/).

## What core does not claim

persome-core does **not** ship a trained next-action predictor, benchmark
dataset, paper accuracy number, or reproduction of the paper's prediction
tables. `model build` constructs `theta_person`; it does not predict the next
state. Evaluation belongs in `persome-bench`, which should consume only:

- a pinned core package/commit;
- synthetic fixtures or consented, separately anonymized snapshots;
- the versioned snapshot JSON or trusted local `/captures/ingest` replay API;
- no direct access to a user's raw core SQLite database.

## Reproducibility boundary

The mock pipeline, orchestration order, manifest, snapshot schema, provenance,
and idempotent writes are deterministic under fixed inputs. Real LLM semantic
outputs are not promised to be byte-identical. Every model build records the
core commit, stage models, prompt hashes, config hash, input window, mode,
timing, and degraded stages so a result can be attributed to its conditions.

Run the paper-facing checks in [REPRODUCING.md](REPRODUCING.md).

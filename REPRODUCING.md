# Reproducing the paper runtime

This document reproduces the Runtime artifact, not the paper's prediction
metrics. Prediction datasets, ablations, and result tables live in the separate
`persome-bench` repository and must pin a released core version.

## 1. Install the development environment

Requirements: Python 3.11, `uv`, and macOS 13+ for real capture. The offline
Runtime tests can run on Linux.

```bash
git clone https://github.com/Persome-ai/persome-core.git
cd persome-core
uv sync --all-extras
```

No provider key is needed for the reproducibility gate.

## 2. Run the synthetic paper path

```bash
PERSOME_LLM_MOCK=1 uv run pytest tests/test_paper_model_e2e.py -q
```

The test starts with a new temporary `PERSOME_ROOT` and performs:

```text
synthetic capture ingest
  -> timeline blocks
  -> ended session
  -> reducer/classifier
  -> two idempotent model builds
  -> Point/Line/Face/Volume/Root assertions
  -> redacted 0600 snapshot export
  -> /model graph and offline-asset checks
```

Two builds are intentional: Face and Volume promotion requires repeated stable
observations. The first may be `degraded`; the second must satisfy the complete
geometry contract.

The inputs and expected schema live in:

- `tests/fixtures/paper_model/captures.json`
- `tests/fixtures/paper_model/model_seed.json`
- `tests/fixtures/paper_model/model_snapshot_v1.golden.json`

## 3. Run the complete offline gate

```bash
PERSOME_LLM_MOCK=1 uv run pytest -m "not macos and not integration and not eval"
uv run ruff check .
uv run python scripts/pii_scan.py
```

The first command is the CI behavior gate. Tests marked `macos`, `integration`,
or `eval` are deliberately outside this offline gate.

## 4. Verify generated contracts

```bash
uv run pytest tests/test_openapi_drift.py tests/test_db_schema_drift.py -q
```

The committed `openapi.json` must byte-match the Runtime schema. The database
dump documents fresh-install schema only; legacy compatibility tables are not
created for new users. After an intentional contract change, regenerate with
`scripts/regen_openapi.py` or `scripts/regen_db_schema.py` and rerun the drift
tests.

## 5. Build and inspect a local model

After installing the Swift helpers and granting Accessibility permission:

```bash
bash install.sh
persome doctor
persome start

# Work normally long enough to close at least one session, then:
persome model build
persome model status
persome model export
open http://127.0.0.1:8742/model
```

Exports default to `<PERSOME_ROOT>/exports/`, are redacted, and use mode
`0600`. `--raw` is an explicit sensitive-data opt-out from redaction.

Without a configured LLM key, capture and BM25 retrieval still run, while LLM
stages report degradation. Real Point/Line/Face/Volume/Root construction needs
the configured stages to produce enough stable evidence.

## 6. Reproducibility record

Every `model build` writes `<PERSOME_ROOT>/model-build.json` with:

- core commit and build ID;
- stage model names and prompt hashes;
- config hash and input time window;
- mock/real mode, timing, and trigger;
- failed or incomplete stages.

Fixed mock inputs reproduce the contract deterministically. Real LLM semantic
outputs can vary; the manifest records the conditions instead of claiming
byte-identical natural-language output.

## 7. Benchmark handoff

`persome-bench` should treat `model-snapshot.json` as its primary input and pin:

```text
core package version or commit
snapshot schema_version
fixture/dataset version
predictor configuration
metric implementation version
```

It may replay synthetic events through loopback `/captures/ingest`. It must not
read a person's raw `index.db`, and redaction is not a substitute for an
independent consent and anonymization review.

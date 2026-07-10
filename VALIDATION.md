# Runtime validation

This guide verifies a source checkout and the distributable package. It uses
only synthetic data and does not require provider credentials.

## 1. Install the development environment

Requirements: Python 3.11+, `uv`, and macOS 13+ for live capture. The offline
Runtime tests also run on Linux.

```bash
git clone https://github.com/Persome-ai/persome-core.git
cd persome-core
uv sync --all-extras
```

## 2. Exercise the complete synthetic path

```bash
PERSOME_LLM_MOCK=1 uv run pytest tests/test_runtime_model_e2e.py -q
```

The test starts with an empty temporary `PERSOME_ROOT` and performs:

```text
synthetic capture ingest
  -> timeline blocks
  -> ended session
  -> reducer + memory-delta modeling
  -> two idempotent structural builds
  -> Point/Line/Face/Volume/Root assertions
  -> redacted 0600 snapshot export
  -> /model graph and bundled-asset checks
```

Two builds are intentional: Face and Volume promotion requires repeated stable
observations. The first may be `degraded`; the second must satisfy the complete
geometry contract.

The synthetic inputs and schema golden live in
`tests/fixtures/runtime_model/`.

## 3. Run the public CI gate

```bash
PERSOME_LLM_MOCK=1 uv run pytest -m "not macos and not integration"
uv run ruff check .
uv run ruff format --check .
uv run python scripts/pii_scan.py
```

`macos` tests need Accessibility permission or compiled Swift helpers.
`integration` tests use a real provider or the complete local OCR runtime and
remain outside the offline gate.

## 4. Verify generated contracts

```bash
uv run pytest tests/test_openapi_drift.py tests/test_db_schema_drift.py -q
```

`openapi.json` must byte-match the Runtime schema. The database dump documents
the fresh-install schema. After an intentional contract change, regenerate with
`scripts/regen_openapi.py` or `scripts/regen_db_schema.py` and rerun the
drift tests.

## 5. Build and inspect a local model

After installing the Swift helpers and granting Accessibility permission:

```bash
bash install.sh
persome doctor
persome start

# Work normally while the daemon performs incremental modeling.
persome model status
persome model build
persome model export
open http://127.0.0.1:8742/model
```

Active sessions flush new evidence every five minutes by default. Session end
processes only the trailing window, and structural builds are debounced after
new evidence. Stopping the daemon is not required to evolve the model.

The installer generates `PERSOME_SCREENSHOT_KEY` automatically. Exports
default to `<PERSOME_ROOT>/exports/`, are redacted, and use mode `0600`.
`--raw` is an explicit sensitive-data opt-out from redaction.

Without an LLM key, capture and BM25 retrieval continue while semantic modeling
reports degradation. A sparse model may correctly remain degraded until it has
enough repeated evidence for higher geometry.

## 6. Verify the release artifact

The wheel must carry the Swift helper sources, local Three.js assets, and
PP-OCRv6 weights. Build it, install it outside the source checkout, and run the
installed CLI:

```bash
rm -rf /tmp/persome-wheel-venv /tmp/persome-wheel-root
uv build
uv venv /tmp/persome-wheel-venv --python 3.11
uv pip install --python /tmp/persome-wheel-venv/bin/python dist/persome_core-*.whl
cd /tmp
PERSOME_ROOT=/tmp/persome-wheel-root \
  /tmp/persome-wheel-venv/bin/persome doctor
```

`persome ocr-selftest <image>` performs a full bundled OCR inference check on
Apple Silicon.

## 7. Build record

Every `model build` writes `<PERSOME_ROOT>/model-build.json` with the core
commit, build ID, model and prompt identifiers, config hash, input window,
mock/real mode, timing, trigger, and incomplete stages. Fixed mock inputs
reproduce the schema contract deterministically. Real LLM wording can vary, so
the manifest records conditions instead of claiming byte-identical semantic
output.

## 8. Optional maintenance probes

To check a long-running store without racing the daemon, copy it to a temporary
root and run the consistency/retrieval soak check:

```bash
rm -rf /tmp/persome-soak
cp -R ~/.persome /tmp/persome-soak
PERSOME_ROOT=/tmp/persome-soak uv run python scripts/soak_healthcheck.py
```

The soak check rebuilds the retrieval projection in the copied root. It should
not be pointed at a live store. Provider maintainers can verify prompt-cache
support separately with `scripts/probe_prompt_cache.py`; it uses
`ANTHROPIC_API_KEY` and `ANTHROPIC_BASE_URL` by default and performs two real
LLM calls.

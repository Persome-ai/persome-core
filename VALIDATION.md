# Runtime validation

This guide verifies a source checkout and the distributable package. It uses
only synthetic data and does not require provider credentials.

## 1. Install the development environment

Requirements: Python 3.11+, SQLite 3.42+ with FTS5, `uv`, and macOS 13+ for live
capture. The offline Runtime tests also run on Linux.

```bash
git clone https://github.com/Intuition-Lab/personal-model.git
cd personal-model
uv sync --all-extras --locked
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
uv export --format requirements-txt --all-extras --no-dev --locked \
  --no-emit-project --quiet --output-file /tmp/persome-runtime.txt
uv run pip-audit --requirement /tmp/persome-runtime.txt \
  --require-hashes --disable-pip --progress-spinner off
uv run python scripts/secret_scan.py
uv run python scripts/pii_scan.py
uv run python scripts/language_scan.py
uv run python scripts/check_doc_links.py
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
persome onboard
persome llm status --check
persome ocr status --check
persome doctor
persome status
persome capture-once

# Work normally while the daemon performs incremental modeling.
persome model status
persome model build
persome model export
persome model open
```

Active sessions flush new evidence every five minutes by default. Session end
processes only the trailing window, and structural builds are debounced after
new evidence. Stopping the daemon is not required to evolve the model.

The installer generates `PERSOME_SCREENSHOT_KEY` automatically. Exports
default to `<PERSOME_ROOT>/exports/`, are redacted, and use mode `0600`.
`--raw` is an explicit sensitive-data opt-out from redaction.

Without a configured hosted credential or keyless local endpoint, capture and
BM25 retrieval continue while semantic modeling reports degradation. A sparse
model may correctly remain degraded until it has
enough repeated evidence for higher geometry.

## 6. Verify the release artifact

The wheel must carry the Swift helper sources, local Three.js assets, and
PP-OCRv6 weights. Build it, install it outside the source checkout, and run the
installed CLI:

```bash
rm -rf /tmp/persome-wheel-venv /tmp/persome-wheel-root
uv build --build-constraints build-constraints.txt --require-hashes
uv export --format requirements-txt --all-extras --no-dev --locked \
  --no-emit-project --quiet --output-file /tmp/persome-runtime.txt
uv venv /tmp/persome-wheel-venv --python 3.11
uv pip install --python /tmp/persome-wheel-venv/bin/python \
  --require-hashes --no-build --requirement /tmp/persome-runtime.txt
uv pip install --python /tmp/persome-wheel-venv/bin/python \
  --no-deps dist/persome_core-*.whl
cd /tmp
PERSOME_ROOT=/tmp/persome-wheel-root \
  /tmp/persome-wheel-venv/bin/persome doctor
PERSOME_ROOT=/tmp/persome-wheel-root \
  /tmp/persome-wheel-venv/bin/persome llm providers
```

In an interactive macOS session, `install.sh` runs `persome onboard`. The
onboarding command separately requests Accessibility and Screen Recording,
performs a full bundled OCR worker initialization on Apple Silicon, starts the
daemon, polls the canonical local health endpoint, and requires a fresh capture
record before it succeeds. Non-interactive packaging environments must run the
command later from a logged-in macOS session.
`persome ocr-selftest <image>` remains available for a known-image inference
check.

The update orchestrator is covered without network access or mutation of the
real data root:

```bash
uv run pytest tests/test_updater.py -q
```

These tests pin the official shallow-clone arguments, local-source validation,
daemon/LaunchAgent stop and recovery behavior, `install.sh --update` handoff,
and the public CLI result.

For a GitHub Release produced by the current workflow (older releases are not
retroactively attested), also download `SHA256SUMS`, verify it from the
artifact directory, and constrain GitHub provenance to the release workflow,
tag, and hosted runner:

```bash
shasum -a 256 --check SHA256SUMS
TAG=vX.Y.Z
gh attestation verify persome_core-*.whl \
  --repo Intuition-Lab/personal-model \
  --signer-workflow Intuition-Lab/personal-model/.github/workflows/release.yml \
  --source-ref "refs/tags/${TAG}" \
  --deny-self-hosted-runners
gh attestation verify persome_core-*.tar.gz \
  --repo Intuition-Lab/personal-model \
  --signer-workflow Intuition-Lab/personal-model/.github/workflows/release.yml \
  --source-ref "refs/tags/${TAG}" \
  --deny-self-hosted-runners
```

The release workflow accepts only an administrator-protected version tag whose
commit is reachable from `origin/main`. Its Actions are pinned to full commit
SHAs and its default token permission is read-only; only the final release job
receives `contents: write`.

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
not be pointed at a live store. Verify the active Runtime profile with
`persome llm status --check`; it tests completion and tool calling. Anthropic
endpoint maintainers can additionally inspect prompt-cache behavior with
`scripts/probe_prompt_cache.py`; that protocol-specific probe performs two real
LLM calls.

# Runtime validation

This guide verifies a source checkout and the distributable package. It uses
only synthetic data and does not require provider credentials.

## 1. Install the development environment

Requirements: Python 3.12-3.13, SQLite 3.42+ with FTS5, `uv`, and macOS 13+ for live
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

`macos` tests need the source-versioned Swift helpers and, for live AX tests,
Accessibility grants for the actual `mac-ax-helper` and `mac-ax-watcher`
executables. Granting only the terminal or daemon is not equivalent.
`integration` tests use a real provider or the complete local OCR runtime and
remain outside the offline gate.

The OAuth-safe agent bridge has an offline regression suite; it mocks client
processes and asserts structured tool adaptation, durable budget enforcement,
and removal of provider/token variables from the child environment:

```bash
uv run pytest tests/test_agent_cli_funding.py tests/test_agent_funded_sampling.py -q
```

## 4. Verify generated contracts

```bash
uv run pytest tests/test_openapi_drift.py tests/test_db_schema_drift.py -q
```

`openapi.json` must byte-match the Runtime schema. The database dump documents
the fresh-install schema. After an intentional contract change, regenerate with
`scripts/regen_openapi.py` or `scripts/regen_db_schema.py` and rerun the
drift tests.

## 5. Build and inspect a local model

After installing the Swift helpers and granting Accessibility to both native
principals used by the configured daemon policy:

```bash
bash install.sh
persome onboard
persome llm status --check
persome ocr status --check
persome doctor
persome status

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

`persome onboard` is the authoritative live smoke test because it invokes the
running daemon's capture runner and binds the result to its generation and
lifecycle owner. `persome capture-once` is only a lower-level helper diagnostic:
it runs a new scheduler in the calling CLI, does not prove the event watcher,
daemon ownership, generation, privacy receipt, or isolated worker readiness,
and can race a running daemon. Stop Persome before using it in a focused helper
test; do not use it as release or onboarding proof.

## 6. Verify the release artifact

The wheel must carry the Swift helper sources, local Three.js assets, and
PP-OCRv6 weights. Build it, install it outside the source checkout, and run the
installed CLI:

```bash
rm -rf /tmp/persome-wheel-venv /tmp/persome-wheel-root
uv build --build-constraints build-constraints.txt --require-hashes
uv export --format requirements-txt --all-extras --no-dev --locked \
  --no-emit-project --quiet --output-file /tmp/persome-runtime.txt
uv venv /tmp/persome-wheel-venv --python 3.12
uv pip install --python /tmp/persome-wheel-venv/bin/python \
  --require-hashes --no-build --requirement /tmp/persome-runtime.txt
uv pip install --python /tmp/persome-wheel-venv/bin/python \
  --no-deps dist/persome_core-*.whl
cd /tmp
PERSOME_ROOT=/tmp/persome-wheel-root \
  /tmp/persome-wheel-venv/bin/persome doctor
PERSOME_ROOT=/tmp/persome-wheel-root \
  /tmp/persome-wheel-venv/bin/persome llm providers
/tmp/persome-wheel-venv/bin/python - <<'PY'
from importlib.resources import files

bundled = files("persome") / "_bundled"
required = (
    "build-mac-ax-helper.sh",
    "build-mac-ax-watcher.sh",
    "mac-ax-helper.swift",
    "mac-ax-watcher.swift",
    "mac-vision-ocr.swift",
    "mac-url-handlers.swift",
    "model_assets/LICENSE",
    "model_assets/three.module.js",
    "model_assets/layout.mjs",
    "model_assets/viewer.css",
    "model_assets/viewer.js",
    "model_assets/jsm/controls/OrbitControls.js",
    "model_assets/jsm/renderers/CSS2DRenderer.js",
    "ocr_models/PP-OCRv6_tiny_det/inference.pdiparams",
    "ocr_models/PP-OCRv6_tiny_rec/inference.pdiparams",
)
for relative in required:
    assert (bundled / relative).is_file(), relative
PY
```

The public PyPI distribution is built from the same tracked source with the
name declared by `tool.persome.release.pypi-distribution`. The root project
keeps the `persome-core` compatibility name because the v0.3.0 updater validates
that field before handing off to a newer installer. Build and inspect the PyPI
artifacts independently:

```bash
rm -rf pypi-dist /tmp/personal-model-wheel
uv run python scripts/build_pypi_dist.py --out-dir pypi-dist
uv venv /tmp/personal-model-wheel --python 3.12
uv pip install --python /tmp/personal-model-wheel/bin/python \
  --require-hashes --no-build --requirement /tmp/persome-runtime.txt
uv pip install --python /tmp/personal-model-wheel/bin/python \
  --no-deps pypi-dist/personal_model-*.whl
/tmp/personal-model-wheel/bin/python - <<'PY'
from importlib.metadata import version
from persome import __version__

assert version("personal-model") == __version__
PY
/tmp/personal-model-wheel/bin/persome --help
```

PyPI installations are owned by their Python tool manager rather than
`<PERSOME_ROOT>/venv`. Upgrade them with
`uv tool upgrade --python 3.12 personal-model` (or
the corresponding pipx/pip command), then run `persome onboard` to restart and
re-prove the Runtime. `persome update` detects this shape and exits with those
instructions instead of attempting the source installer's atomic venv exchange.

In an interactive macOS session, `install.sh` runs `persome onboard`. For the
standard daemon mode on Apple Silicon and Intel, onboarding separately requests Accessibility
for the source-versioned capture helper and event watcher, requests Screen
Recording when the effective pixel policy needs it, starts the final owner,
waits for its bundled OCR worker, polls canonical local health and authenticated
permission endpoints, and requires an exact fresh-capture receipt from the
daemon-owned runner. Repeated runs must reuse the same source-versioned native
binaries and ready worker rather than compile new TCC principals or spawn a
    second OCR process. The same command must also report truthfully for Intel
    with Apple Vision OCR, durable OCR/pixel opt-out, trusted ingest, paused/locked update,
LaunchAgent-owned, and HTTP-disabled modes. HTTP-disabled daemon mode proves the
same generation through owner-only `.runtime-state.json`; trusted ingest is
rejected when its authenticated HTTP endpoint is disabled.
Non-interactive packaging environments must run the command later from a
logged-in macOS session.
Interactive source installs also schedule the authenticated local model viewer
to open once after 30 minutes. The detached reminder must survive the installer
terminal, write only to the owner-private `logs/model-open-reminder.log`, and
exit after invoking `persome model open`; it must not install another permanent
LaunchAgent. Verify its CLI and installer contract with
`tests/test_cli_local_access.py` and `tests/test_onboarding.py`.
`persome ocr-selftest <image>` remains available for a known-image inference
check.

The update orchestrator is covered without network access or mutation of the
real data root:

```bash
uv run pytest tests/test_updater.py -q
```

These tests pin the official shallow-clone arguments, local-source validation,
exclusive update ownership, inactive candidate construction, transaction-marker
validation, one-operation directory exchange, crash recovery before/after that
exchange, rejection of absolute candidate shebangs, execution of the relocated
CLI after old-venv cleanup, signal-safe rollback, and final
background/LaunchAgent ownership.
Lifecycle tests also cover daemon lifetime locking, PID reuse, malformed live
generation state, and owner-marker handoff. `tests/test_onboarding.py`,
`tests/test_launchagent.py`, `tests/test_ax_capture.py`, and
`tests/test_ocr_subprocess.py` cover the permission/mode cross-product,
source-versioned helper reuse, durable `ocr_policy`, progress reporting, and
daemon-owned worker/readiness receipts.

For a manual native-identity regression, record the helper and watcher paths
printed by two same-version installs and confirm they are byte-for-byte the same
under `<PERSOME_ROOT>/native/<source-digest>/`. A deliberate helper-source
change must resolve a different directory and require a fresh Accessibility
grant; rolling that update back must resolve the old executable again. Never
copy a newly compiled binary over an existing digest path to make a TCC test
pass.

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
receives `contents: write`. After that GitHub Release succeeds, the `pypi-publish`
job downloads the already verified `personal_model` artifacts and publishes
through the GitHub `pypi` environment with OIDC `id-token: write`; no PyPI API
token is stored in the repository.

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

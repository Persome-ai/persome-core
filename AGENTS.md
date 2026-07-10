# AGENTS.md

Orientation for coding agents in `persome-core`. Code is authoritative; the
documents explain its public contract and operational intent.

## Runtime boundary

Persome Runtime is a local-first macOS daemon that observes one real person's
screen context, forms durable state, builds an auditable personal model, and
serves it to Chat and MCP clients. The public geometry is Point, Line, Face,
Volume, and at most one Root. `/model` renders the current geometry locally.

This repository owns:

- AX capture and optional, local, subprocess-isolated OCR;
- timeline and deterministic session formation;
- incremental session reduction and personal modeling;
- Markdown/SQLite/evomem storage, provenance, correction, and forgetting;
- the versioned model snapshot, local viewer, Chat, REST, and MCP.

It does not own product dashboards, notification/task lifecycles, computer-use
actuation, meeting audio, or evaluation datasets and runners.

- macOS 13+ for live capture; Python 3.11+ via `uv`.
- Package: `src/persome/`; CLI: `persome = "persome.cli:app"`.
- Default data root: `~/.persome`.

## Commands

```bash
uv sync --all-extras
bash install.sh

PERSOME_LLM_MOCK=1 uv run pytest -m "not macos and not integration"
uv run ruff check .
uv run ruff format --check .
uv run python scripts/pii_scan.py

persome doctor
persome start
persome status
persome stop
persome chat
persome mcp
persome model build
persome model status
persome model export
```

Regenerate committed contracts after intentional changes:

```bash
uv run python scripts/regen_openapi.py
uv run python scripts/regen_db_schema.py
uv run pytest tests/test_openapi_drift.py tests/test_db_schema_drift.py -q
```

Commits require DCO sign-off: `git commit -s`.

## Pipeline

There is one production path:

```text
mac-ax-watcher or trusted ingest
  -> S0 debounce/dedup
  -> S1 focused element + visible text + URL
  -> capture buffer
  -> one-minute timeline blocks
  -> deterministic session cuts
  -> five-minute active reducer / event memory
  -> incremental Point/Line modeling
  -> session-end trailing-window finalizer
       -> classifier compatibility/incremental pass
       -> pattern detector
       -> evidence-gated memory_delta + deterministic apply
  -> debounced/daily/explicit structural model build
       -> cases -> Faces -> Volumes -> Root
  -> snapshot / MCP / Chat / localhost viewer
```

The windowed modeling service in `writer/agent.py` is shared by active flush,
session-end finalization, retry, safety-net, CLI recovery, and model build.
`sessions.delta_end`, `sessions.modeled_at`, and `session-model.lock` make it
cross-process idempotent. Do not add another session-modeling entrance.

## Documentation map

| Need | Document |
|---|---|
| Installation and release verification | `README.md`, `VALIDATION.md` |
| Public Runtime flow | `ARCHITECTURE.md` |
| Snapshot contract | `MODEL_FORMAT.md`, `docs/model-contract.md` |
| Public MCP contract | `MCP.md`, `docs/mcp.md` |
| Capture through model build | `docs/architecture.md`, `docs/capture.md`, `docs/timeline.md`, `docs/session.md`, `docs/writer.md` |
| Configuration and secrets | `docs/config.md`, `docs/runtime-internals.md` |
| Privacy boundary | `SECURITY_PRIVACY.md` |

Update matching docs in the same change as behavior.

## Invariants

- Use `src/persome/paths.py` for every runtime path. Tests use `ac_root` and
  must never touch the real data root.
- Use `with fts.cursor() as conn:` for SQLite. The store runs in WAL mode.
- All stage LLM calls go through `writer/llm.py`. Model names are bare gateway
  names; secrets belong in `<PERSOME_ROOT>/env`, never `config.toml`.
- `install.sh` generates and preserves `PERSOME_SCREENSHOT_KEY`. If encrypted
  screenshot persistence lacks a valid key, omit pixels; never write plaintext.
- Default Point/Line production is the windowed `memory_delta` followed by
  deterministic `delta_apply`: active flushes advance `sessions.delta_end` and
  terminal finalization only catches the trailing window. Do not silently
  reintroduce parallel writers.
- Every modeled object preserves evidence receipts and bitemporal history.
- At most one live Root exists. Missing geometry is `degraded`, never fabricated.
- Chat skill Markdown is safe to load. Executable `skills/*/tools.py`, shell,
  arbitrary filesystem, and Web tools require
  `[chat] unsafe_local_tools_enabled = true`.
- The HTTP server is loopback by default. There is no second authentication
  layer; treat local endpoint access as access to personal data.
- Prompt edits follow `docs/prompt-engineering.md`: define an eval criterion,
  then change and verify the smallest prompt surface.
- Live capture uses compiled Swift helpers bundled into the wheel. OCR is off
  by default and runs in an isolated worker when enabled.
- `--capture-only` disables timeline and model-processing tasks, but keeps
  capture, session tracking, reducer recovery/safety net, and configured MCP.

## Naming and provenance

The active name is Persome throughout. `MENS_*` and `OPENCHRONICLE_ROOT` are
annotated compatibility fallbacks only. OpenChronicle provenance remains in
`NOTICE` and `THIRD_PARTY_NOTICES`. Do not add product-specific ports, launchd
labels, paths, or UI behavior to this Runtime.

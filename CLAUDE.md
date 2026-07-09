# CLAUDE.md

Guidance for Claude Code (claude.ai/code) and other coding agents working in this repository.
This is the **agent orientation** doc — commands, architecture map, code patterns, invariants.
Deep dives live in [`docs/`](docs/INDEX.md); **code is the source of truth**, these explain why.

## What this is

**Persome Runtime** (`persome-core`, package `persome`) is a **local-first, macOS screen-context
memory daemon**. It captures macOS Accessibility (AX) tree events as you work — falling back to
on-device screenshot OCR (PP-OCRv6, bundled weights, no network) for AX-poor apps — compresses them
through a deterministic pipeline, and stores durable **Markdown memory** plus a **SQLite** index
(FTS5 + a dense vector index). It exposes an **MCP server** so tool-capable agents can query that
memory.

The thesis (see the [Personome paper](https://persome-ai.github.io/persome/)): an LLM predicts the
next **token**; a Personome predicts a **person's next action**, with memory as the *weights* of that
personal model. This runtime is the **中台 (middle platform)**: it is the canonical, standalone
source that products build on. **Mens** (a native macOS app) is the first such product and consumes
this repo as a pinned dependency; future products consume it the same way. Product-specific
configuration (ports, launchd labels, data-root) lives in the **consumer**, never here.

- **Platform**: macOS 13+ only (`capture/watcher.py` spawns compiled Swift AX helpers).
- **Python**: 3.11+ (`.python-version` = 3.11), managed with **`uv`**. PaddlePaddle ships cp311–313
  wheels only, so the build Python stays in that band.
- **Package**: `src/persome/`, CLI entry `persome = "persome.cli:app"`.

## Commands

```bash
uv sync --all-extras                 # install deps
bash install.sh                      # compile Swift AX helpers + write ~/.persome/env (BYO key)

# Offline test gate (what CI runs; no LLM key, no network) — see CONTRIBUTING.md
PERSOME_LLM_MOCK=1 uv run pytest -m "not macos and not integration and not eval"
uv run pytest tests/test_session_manager.py          # a single file
uv run ruff check && uv run ruff format --check      # lint / format
uv run python scripts/pii_scan.py         # PII gate (required — see CONTRIBUTING.md)

# Daemon lifecycle (after install)
persome start                        # MCP HTTP endpoint http://127.0.0.1:8742/mcp
persome status
persome stop
persome mcp                          # stdio MCP transport (for Claude Desktop / Cursor)
```

The CLI carries a large surface of **read-only observability + maintenance** subcommands (zero-LLM
unless noted) — run `persome --help` for the full list. The load-bearing groups:

- **Memory / retrieval**: `rebuild-index`, `memory-viz` (writes `sem_facts.json` for the 3D board),
  `as-of` (bitemporal node resolution), `faces-report`, `delta-report`, `decay-report`.
- **evomem** (the SSOT/graph engine): `evomem-backfill`, `evomem-project-markdown [--live]`,
  `evomem-import-markdown`, `contradictions` / `contradictions-resolve`. Write-authority is
  `[evomem] write_authority` (**default `"markdown"`** — markdown is the SSOT + shadow dual-write;
  `"evomem"` inverts it. A human flips it, never the code default). See `docs/writer.md`.
- **Intent / feedback / work-threads**: `intent-audit`, `intent-restamp`, `feedback-report`,
  `thread list|tui|review-day|correct|stats`.
- **Day-0 profiler**: `bootstrap [--dry-run|--no-llm|--shallow]`.
- **launchd handoff**: `launchagent install|status|uninstall`.

## Working in this repo

Full contributor workflow — dev setup, the offline test gate, lint, the **required PII gate**,
branch/PR rules, and **DCO sign-off** — is in [`CONTRIBUTING.md`](CONTRIBUTING.md). In short:

- Branch off `main`; open PRs into `main`. Sign every commit (`git commit -s` — DCO required).
- **CI is the gate** (`.github/workflows/`): the offline unit gate (`PERSOME_LLM_MOCK=1`, no key) +
  ruff + the `pii_scan.py` PII gate. There is no server-held secret in this tree — bring your own key.
- **Same-PR docs rule**: a code change that alters a documented behavior updates the matching
  `docs/*.md` in the same PR (`docs/INDEX.md` maps behavior → doc).

## Architecture map

The end-to-end shape lives in [`docs/`](docs/INDEX.md) — read the relevant page rather than
rediscovering it here:

| Question | Doc |
|---|---|
| The pipeline (capture → S1 parser → timeline → session → reducer → classifier), the **daemon task table**, on-disk state | [`docs/architecture.md`](docs/architecture.md) |
| LLM keys / auth / daemon lifecycle / `~/.persome/` layout / build-vs-runtime split | [`docs/runtime-internals.md`](docs/runtime-internals.md) |
| `config.toml` keys, per-stage model inheritance, secrets | [`docs/config.md`](docs/config.md) |
| MCP tool surface + transports | [`docs/mcp.md`](docs/mcp.md) |
| Capture / timeline / session-cut rules / reducer+classifier / memory-file format | [`docs/capture.md`](docs/capture.md) · [`docs/timeline.md`](docs/timeline.md) · [`docs/session.md`](docs/session.md) · [`docs/writer.md`](docs/writer.md) · [`docs/memory-format.md`](docs/memory-format.md) |
| Product/technical whitepaper | [`docs/persome-overview.md`](docs/persome-overview.md) |
| **Design philosophy & method** — the *why* of the intent layer, how to change it without regressing | [`docs/design-philosophy-intent.md`](docs/design-philosophy-intent.md) · [`docs/data-driven-iteration.md`](docs/data-driven-iteration.md) · [`docs/prompt-engineering.md`](docs/prompt-engineering.md) |

One ingestion path, **no modes**: `mac-ax-watcher` → S0 dispatcher → S1 parser → capture-buffer →
1-min timeline blocks → session cutter (3 rules) → S2 reducer → classifier → Markdown + FTS5 + MCP.

## Code patterns (the invariants an agent must not break)

- **Paths**: `src/persome/paths.py` is the single source of truth for on-disk locations. The data
  root resolves `PERSOME_ROOT` → `MENS_CONTEXT_ROOT` → `OPENCHRONICLE_ROOT` (legacy fallbacks), else
  `~/.persome`. Tests use the `ac_root` fixture (a `tmp_path` root) — never touch the real store.
- **Database**: always `with fts.cursor() as conn:` from `src/persome/store/fts.py`. SQLite is
  WAL-mode so the MCP reader and the writer coexist without blocking.
- **LLM calls go through ONE path** — `src/persome/writer/llm.py`, which speaks the **Anthropic
  Messages API** via the official SDK (litellm was removed; it mis-serialized custom tools for the
  DeepSeek `/anthropic` gateway). `call_llm` keeps an OpenAI-shaped return so every stage
  (`run_tool_loop` / `extract_*`) is unchanged. `[models.<stage>].model` is a **bare name**
  (e.g. `claude-haiku-4-5`, `deepseek-v4-flash`) sent verbatim to the gateway; `anthropic/…` /
  `deepseek/…` prefixes are tolerated (`_bare_model` strips them). Prompt caching is automatic
  (`cache_control` is passed through — no model-name gate).
- **Secrets are NEVER in `config.toml`.** LLM keys / base URLs live in `~/.persome/env` (dotenv,
  `chmod 600`), loaded before the daemon double-forks; business code reads plain `os.environ`.
  **Bring your own key** — nothing ships in this tree. Without a key, capture + BM25 retrieval still
  work and LLM stages degrade cleanly. Canonical keys: `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL`
  (+ optional `OPENAI_API_KEY` / `OPENAI_BASE_URL` for dense embeddings).
- **Prompts** are Markdown templates in `src/persome/prompts/`. **Before editing any LLM prompt**,
  read [`docs/prompt-engineering.md`](docs/prompt-engineering.md): success-criterion + eval first,
  apply the technique ladder in order, verify each change against the eval noise band, prefer
  few-shot / routing over stuffing brittle rules.
- **Tests** run on a temp `PERSOME_ROOT`; LLM calls are mocked via `PERSOME_LLM_MOCK=1`
  (`fake_llm` fixture). Markers: `macos` (needs Swift helpers), `integration` (real providers),
  `eval` (slow regression evals) — the default Linux gate deselects all three.
- **Swift helpers** (`mac-ax-watcher`, `mac-ax-helper`, …) are compiled by `install.sh` and bundled
  in the wheel via `pyproject.toml` `force-include`; they will not run off macOS.
- **`--capture-only`** disables the timeline aggregator + MCP server; capture, session, and the
  daily safety-net still run.

## Naming — persome, with legacy shims

Everything is **persome** end-to-end: repo, PyPI dist (`persome-core`), CLI (`persome`), import
package (`persome`), data dir (`~/.persome`), PyInstaller EXE (`Persome Backend`). Two former names
survive **only as backward-compat shims**, always annotated in code:

- **`Mens`** — the product this daemon was first built for. `MENS_*` env vars (e.g.
  `MENS_CONTEXT_ROOT`, `MENS_PARENT_PID`) are read as **legacy fallbacks** after the `PERSOME_*`
  form, so an existing Mens install keeps working. "Mens is the legacy name" marks each shim.
- **`OpenChronicle`** — the upstream MIT project this was forked from (`OPENCHRONICLE_ROOT`
  fallback; provenance is preserved in `NOTICE` / `THIRD_PARTY_NOTICES`).

When a product embeds this runtime, it sets its own `config.toml` (port, etc.) and may export the
legacy env — the runtime honors both. Do not add product-specific values (a specific port, a
launchd label, `~/.mens`) into this tree; those belong to the consumer.

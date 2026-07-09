# persome-core Documentation Index

Single entry point for everything written about persome-core (the persome daemon + memory + MCP server). Use this page to find which doc to read for which question. **Code is the source of truth**; these docs explain why and how.

## Tier 1 — Start here

| Doc | When to read |
|---|---|
| [`README.md`](../README.md) | First-time install, what the project does, how to connect an agent |
| [`CLAUDE.md`](../CLAUDE.md) | AI / developer working in this codebase — commands, architecture map, code patterns, secrets, workflow |

## Tier 2 — Architecture & design

| Doc | When to read |
|---|---|
| [`architecture.md`](architecture.md) | The end-to-end pipeline (capture → S1 parser → timeline → session → reducer → classifier), daemon task table, on-disk state layout |
| [`persome-overview.md`](persome-overview.md) | Product + technical whitepaper: pain points, capability matrix, data flows, JSON schemas |
| [`db-schema.sql`](db-schema.sql) | **Generated** whole-picture SQLite schema (`index.db` + `meeting_*.db`), grouped by owning module. Regenerate with `uv run python scripts/regen_db_schema.py`. Drift is enforced by `tests/test_db_schema_drift.py` |

## Tier 2.5 — Design philosophy & method

The *why* behind the intent layer and how to change it without regressing quality. Read before designing/tuning intent recognition, proactive intervention, memory/recall, or optimizing any measurable metric.

| Doc | When to read |
|---|---|
| [`design-philosophy-intent.md`](design-philosophy-intent.md) | Persome as an intent predictor (the LLM analogy — memory = weights); the trust/cost-asymmetry constitution; the DROP/FIRE/DEFER recognizer; completeness via orthogonal axes |
| [`data-driven-iteration.md`](data-driven-iteration.md) | Optimizing a metric and proving it: the four-layer oracle, noise bands, real-data reckoning, epistemic-vs-aleatoric stop condition, the adversarial refuter gate |
| [`prompt-engineering.md`](prompt-engineering.md) | Before adding/editing any LLM prompt in `src/persome/prompts/`: success-criterion-first, the technique ladder, verify each change against the eval noise band |
| [`research/`](research/) | Personome paper research — references and the related-work survey (Agent Memory landscape) |

## Tier 3 — Operations & configuration

| Doc | When to read |
|---|---|
| [`config.md`](config.md) | `config.toml` keys, per-stage model setup, model inheritance, env vars, secret management |
| [`mcp.md`](mcp.md) | MCP tool surface (`search`, `read_memory`, `recent_activity`, ...), transport modes, integration guidance |
| [`runtime-internals.md`](runtime-internals.md) | Where LLM keys / auth tokens / daemon PID / data dirs live; build-time vs runtime split; the "hard-won" runtime behaviour |
| [`troubleshooting.md`](troubleshooting.md) | Common runtime symptoms and how to diagnose |

## Tier 4 — API contract

| Doc | When to read |
|---|---|
| [`api.md`](api.md) | Detailed route-by-route description of the HTTP API |
| [`api-pitfalls.md`](api-pitfalls.md) | Known footguns when calling the API |
| [`../openapi.json`](../openapi.json) | **Generated** spec — single source of truth. Regenerate with `uv run python scripts/regen_openapi.py`. Drift is enforced by `tests/test_openapi_drift.py` |

## Tier 5 — Pipeline stages (deep dives)

| Doc | When to read |
|---|---|
| [`capture.md`](capture.md) | Event-driven capture, AX trees, app-specific parsers |
| [`timeline.md`](timeline.md) | 1-min window normalization, anti-hallucination design |
| [`session.md`](session.md) | The three cut rules (hard / soft / timeout) and tunables |
| [`writer.md`](writer.md) | Reducer + classifier loop, retries, supersede semantics |
| [`memory-format.md`](memory-format.md) | Markdown memory file structure, frontmatter, file-naming conventions |

## Tier 5.5 — Architecture design specs (the *why* behind the code)

Dated design specs for the core memory / retrieval / intent architecture — the reasoning, invariants, and edge cases behind what the code does. Read when changing that subsystem. (Authored during Mens development; each carries a provenance note. Curated selection — more may be migrated over time.)

**Memory model**

| Spec | Subject |
|---|---|
| [`specs/2026-07-02-memory-rebuild-design.md`](specs/2026-07-02-memory-rebuild-design.md) | The memory **geometry** — a USER-rooted points/lines/faces/body graph (= the predictor's weights θ); six-head RRF associative retrieval + tree-chains; identity resolution funnel; the three-tier eval pyramid |
| [`specs/2026-06-27-filesystem-memory-grounding-design.md`](specs/2026-06-27-filesystem-memory-grounding-design.md) | **Memory = weights**, in practice — read memory + live attention off disk and prepend a fenced digest to every dispatched run, so recognition ↔ memory ↔ execution share one context |
| [`specs/2026-07-04-memory-root-apex-design.md`](specs/2026-07-04-memory-root-apex-design.md) | The level-3 **root apex** — the single ≤1500-token always-resident "who is this person" summit synthesized nightly; everything below becomes recall + progressive disclosure |
| [`specs/2026-06-25-reducer-anchor-preservation-design.md`](specs/2026-06-25-reducer-anchor-preservation-design.md) | Reducer anti-hallucination — preserving `[App]` anchors and verbatim evidence through S2 compression |
| [`specs/2026-07-08-db-schema-dump-design.md`](specs/2026-07-08-db-schema-dump-design.md) | The `index.db` schema surface — tables and their roles |

**Retrieval & eval**

| Spec | Subject |
|---|---|
| [`specs/2026-06-25-production-hybrid-retrieval-design.md`](specs/2026-06-25-production-hybrid-retrieval-design.md) | Hybrid retrieval — BM25 ⊕ dense (te3-large) fused via RRF; the production A/B that flipped it ON (Chinese paraphrase recall 0.025 → 0.76) |
| [`specs/2026-06-25-recognizer-semantic-recall-design.md`](specs/2026-06-25-recognizer-semantic-recall-design.md) | Weaving semantic recall into the recognizer's own context |
| [`specs/2026-06-25-longmemeval-production-pipeline-design.md`](specs/2026-06-25-longmemeval-production-pipeline-design.md) | A production-faithful LongMemEval harness (compress + classify, not verbatim) |

**Intent & recognition**

| Spec | Subject |
|---|---|
| [`specs/2026-06-25-proactive-followup-engine-design.md`](specs/2026-06-25-proactive-followup-engine-design.md) | 识别即办 — a kind-agnostic proactive follow-up engine over four orthogonal seams (intent→action / capability ladder / output sink / place-never-send gate) |
| [`specs/2026-06-26-faceted-fast-path-intent-schema-design.md`](specs/2026-06-26-faceted-fast-path-intent-schema-design.md) · [`plan`](specs/2026-06-26-faceted-fast-path-implementation-plan.md) | The fast-path recognizer schema — the `DROP/FIRE/DEFER` decision as a function of orthogonal axes, + its implementation plan |
| [`specs/2026-06-28-no-parser-removal-whitelist-minimization-design.md`](specs/2026-06-28-no-parser-removal-whitelist-minimization-design.md) | Removing the `no_parser` whitelist gate — judge by content, not by app identity |
| [`specs/2026-06-30-intent-harvest-sse-design.md`](specs/2026-06-30-intent-harvest-sse-design.md) | Intent lifecycle harvest + live status over SSE |
| [`specs/2026-06-30-intent-kind-separability-diagnostic-design.md`](specs/2026-06-30-intent-kind-separability-diagnostic-design.md) | A diagnostic oracle for per-intent-kind separability |

## Tier 6 — Cross-component / runtime knowledge

This repo ships no separate agent-skills library. Cross-component knowledge that doesn't belong to one pipeline stage lives in:

| Topic | Where |
|---|---|
| Agent orientation — commands, code patterns, load-bearing invariants, the persome/Mens naming shim | [`../CLAUDE.md`](../CLAUDE.md) |
| Hard-won runtime behavior — where LLM keys / auth / daemon PID / data dirs live, build-time vs runtime, recovering a wedged daemon | [`runtime-internals.md`](runtime-internals.md) |

## Conventions for adding new docs

- **Same-PR rule**: every code change that affects a documented behavior updates the relevant doc in the same PR.
- **Where to put a new doc**:
  - A new pipeline stage → `docs/<stage>.md` (and add a row to Tier 5 above)
  - A new operational concern → extend `config.md` or `troubleshooting.md` rather than creating a new file
  - Cross-component / runtime knowledge → `runtime-internals.md` or the orientation doc `../CLAUDE.md`
- **Cross-link aggressively**: relative links to other docs. Don't restate; point.

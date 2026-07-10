# Documentation index

Code is the source of truth. The repository root contains the paper-facing
contract; this directory contains implementation details for maintainers.

## Paper-facing documents

| Document | Purpose |
|---|---|
| [`../README.md`](../README.md) | Install, run, and understand the Runtime. |
| [`../PAPER.md`](../PAPER.md) | Paper claim to core/bench artifact map. |
| [`../ARCHITECTURE.md`](../ARCHITECTURE.md) | Runtime boundary and end-to-end data flow. |
| [`../REPRODUCING.md`](../REPRODUCING.md) | Synthetic fresh-root and release reproduction. |
| [`../MODEL_FORMAT.md`](../MODEL_FORMAT.md) | Versioned model snapshot contract. |
| [`../MCP.md`](../MCP.md) | Public MCP tools and trust boundary. |
| [`../SECURITY_PRIVACY.md`](../SECURITY_PRIVACY.md) | Data, egress, redaction, and threat model. |
| [`../CONTRIBUTING.md`](../CONTRIBUTING.md) | Development gates and DCO workflow. |

## Runtime implementation

| Document | Purpose |
|---|---|
| [`architecture.md`](architecture.md) | Detailed pipeline, task registry, storage, and code map. |
| [`capture.md`](capture.md) | AX/OCR ingestion and app-specific attention extraction. |
| [`timeline.md`](timeline.md) | Minute-window aggregation and evidence preservation. |
| [`session.md`](session.md) | Session cut rules and reducer/classifier bookmarks. |
| [`writer.md`](writer.md) | Durable fact, schema, Volume, and Root construction. |
| [`memory-format.md`](memory-format.md) | Markdown memory file format. |
| [`model-contract.md`](model-contract.md) | Snapshot implementation details and build locking. |
| [`mcp.md`](mcp.md) | MCP implementation reference. |
| [`api.md`](api.md) | Minimal REST/Chat routes and OpenAPI drift rule. |
| [`config.md`](config.md) | `config.toml` fields and feature gates. |
| [`runtime-internals.md`](runtime-internals.md) | Secrets, daemon lifecycle, paths, and recovery. |
| [`troubleshooting.md`](troubleshooting.md) | Operational diagnosis. |
| [`prompt-engineering.md`](prompt-engineering.md) | Prompt change discipline. |
| [`db-schema.sql`](db-schema.sql) | Generated fresh-install SQLite schema. |

## Research context

Background references live in [`research/`](research/). Benchmark datasets,
prediction runners, ablations, and result tables do not belong here; they live
in the separate `persome-bench` repository.

## Maintenance rules

- A behavior change updates its matching documentation in the same PR.
- Add current subsystem facts to an existing canonical document instead of a
  dated design spec.
- Regenerate `openapi.json` and `db-schema.sql` after intentional contract
  changes, then run their drift tests.
- Examples and fixtures must be synthetic and pass `scripts/pii_scan.py`.

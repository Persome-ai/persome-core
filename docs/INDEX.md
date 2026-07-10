# Documentation index

Code is the source of truth. Root documents define the public Runtime contract;
this directory explains implementation details for maintainers.

## Public documents

| Document | Purpose |
|---|---|
| [`../README.md`](../README.md) | Install, run, and understand the Runtime. |
| [`../ARCHITECTURE.md`](../ARCHITECTURE.md) | Runtime boundary and end-to-end data flow. |
| [`../MODEL_FORMAT.md`](../MODEL_FORMAT.md) | Versioned personal-model snapshot contract. |
| [`../MCP.md`](../MCP.md) | Public MCP tools and trust boundary. |
| [`../SECURITY_PRIVACY.md`](../SECURITY_PRIVACY.md) | Data, egress, redaction, and threat model. |
| [`../VALIDATION.md`](../VALIDATION.md) | Offline gates and clean-package verification. |
| [`../CONTRIBUTING.md`](../CONTRIBUTING.md) | Development workflow and DCO policy. |

## Runtime implementation

| Document | Purpose |
|---|---|
| [`architecture.md`](architecture.md) | Detailed pipeline, task registry, storage, and code map. |
| [`capture.md`](capture.md) | AX/OCR ingestion and app-specific attention extraction. |
| [`timeline.md`](timeline.md) | Minute-window aggregation and evidence preservation. |
| [`session.md`](session.md) | Session cuts, active flush, retry, and trailing finalization. |
| [`writer.md`](writer.md) | Point/Line writing plus Face, Volume, and Root construction. |
| [`memory-format.md`](memory-format.md) | Markdown memory-file format. |
| [`model-contract.md`](model-contract.md) | Snapshot implementation and build locking. |
| [`mcp.md`](mcp.md) | MCP implementation reference. |
| [`api.md`](api.md) | REST/Chat routes and OpenAPI drift rule. |
| [`config.md`](config.md) | `config.toml` fields and feature gates. |
| [`runtime-internals.md`](runtime-internals.md) | Secrets, daemon lifecycle, paths, and recovery. |
| [`troubleshooting.md`](troubleshooting.md) | Operational diagnosis. |
| [`prompt-engineering.md`](prompt-engineering.md) | Prompt-change discipline. |
| [`db-schema.sql`](db-schema.sql) | Generated fresh-install SQLite schema. |

## Maintenance rules

- Update the matching document in the same change as a behavior change.
- Add current subsystem facts to an existing canonical document instead of a
  dated design note.
- Regenerate `openapi.json` and `db-schema.sql` after intentional contract
  changes, then run their drift tests.
- Keep all committed examples and fixtures synthetic and run
  `scripts/pii_scan.py` before pushing.

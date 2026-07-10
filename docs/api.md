# HTTP API

Persome exposes a deliberately small loopback HTTP API from the same ASGI
application that hosts MCP. HTTP owns health, trusted capture ingestion, the
model explorer, and optional Chat. Memory retrieval and correction live in MCP.

The generated contract is [`openapi.json`](../openapi.json). Regenerate it after
route or model changes:

```bash
uv run python scripts/regen_openapi.py
```

`tests/test_openapi_drift.py` requires the committed file to byte-match the live
runtime schema.

## Runtime routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness probe. |
| GET | `/permissions` | macOS Accessibility state. |
| GET | `/status` | Daemon, capture, session, memory, and provider status. |
| POST | `/captures/ingest` | Ingest one capture from a trusted local producer. |
| GET | `/model` | Open the offline Point/Line/Face/Volume/Root explorer. |
| GET | `/model/graph` | Read visualization data plus the versioned model snapshot. |
| GET | `/model/node?id=...` | Resolve one graph point to receipts and its relation tree. |

The model page loads its pinned Three.js modules from `/model/assets/*`. Those
static routes are package resources and are intentionally omitted from OpenAPI.

## Chat routes

| Method | Path | Purpose |
|---|---|---|
| GET, POST | `/chat/sessions` | List or create local chat sessions. |
| GET, DELETE | `/chat/sessions/{session_id}` | Read or delete a session. |
| GET, POST | `/chat/sessions/{session_id}/messages` | Read messages or stream a reply. |

Chat consumes the same memory and provenance interfaces as MCP. It is not a
second model store. Shell, arbitrary filesystem, and Web tools are omitted by
default and require `[chat] unsafe_local_tools_enabled = true`.

## Model contract

`GET /model/graph` includes a `model` object with the same schema returned by the
MCP `get_model_snapshot` tool and CLI `persome model export`:

```text
schema_version, generated_at, build,
points, lines, faces, volumes, root, receipts, stats
```

Every Line derived from activity carries `source_kind`, `source_id`, and
`source_receipt`. Legacy `event:<id>` identities are normalized to
`event:intent:<id>` and are read only when an old `intents` table exists.

## Security boundary

- The server defaults to `127.0.0.1`.
- Origin and host guards reject non-loopback browser access.
- `/captures/ingest` assumes a trusted local producer; it is not a public upload API.
- Model assets and graph data load from the same loopback server with no CDN dependency.
- LLM and embedding egress only use endpoints configured by the user.
- Unknown and removed product/admin routes return `404`.

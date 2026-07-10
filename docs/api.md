# HTTP API

Persome exposes a loopback HTTP API from the same ASGI application that hosts
MCP. The generated, machine-readable contract is [`openapi.json`](../openapi.json).
Regenerate it after route or model changes:

```bash
uv run python scripts/regen_openapi.py
```

`tests/test_openapi_drift.py` requires the committed file to byte-match the
runtime schema.

## Response envelope

Most endpoints return one of these shapes:

```json
{"success": true, "data": {}}
```

```json
{"success": false, "error": "message", "detail": "optional detail"}
```

Consult OpenAPI for each request and response body. Unknown resources return
`404`; validation failures return `422`.

## Runtime routes

### Health and permissions

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness probe. |
| GET | `/permissions` | macOS Accessibility and Screen Recording state. |
| GET | `/status` | Daemon, capture, session, memory, and model-provider status. |

### Capture and state formation

| Method | Path | Purpose |
|---|---|---|
| POST | `/captures/ingest` | Ingest a capture from a trusted local producer. |
| GET | `/captures/current` | Recent capture and timeline context. |
| GET | `/captures` | Search raw captures. |
| GET | `/captures/recent` | Read the nearest recent capture. |
| GET | `/timeline` | Query normalized timeline blocks. |
| GET | `/attention/trajectory` | Query the derived attention trajectory. |

### Memory and retrieval

| Method | Path | Purpose |
|---|---|---|
| GET | `/memories` | List durable memory files. |
| GET | `/memories/{path}` | Read a memory file with optional filters. |
| POST | `/memories/append` | Explicit, auditable memory write. |
| GET | `/search` | Search durable memory. |
| GET | `/activity` | Read recent durable activity. |
| GET | `/recall/pack` | Return a structured, receipt-bearing recall pack. |

### Transitional intent observability

The following routes remain during the model-source migration. They are not a
claim that the paper runtime already implements next-state prediction.

| Method | Path | Purpose |
|---|---|---|
| GET | `/intents` | Read stored intent rows. |
| PATCH | `/intents/{intent_id}` | Update an intent status. |
| GET | `/parser/stats` | Read parser telemetry. |

### Reference and maintenance

| Method | Path | Purpose |
|---|---|---|
| GET | `/schema` | Read the Markdown memory schema. |
| GET | `/config` | Read resolved configuration. |
| GET, PUT | `/config/raw` | Read or replace local TOML configuration. |
| GET, PUT | `/config/debug-hud` | Read or update observability filters. |
| POST | `/daemon/pause` | Pause capture. |
| POST | `/daemon/resume` | Resume capture. |
| POST | `/daemon/capture-once` | Request one capture. |
| POST | `/indices/rebuild` | Rebuild the memory index. |
| POST | `/indices/rebuild-captures` | Rebuild the capture index. |
| POST | `/consolidate` | Run memory consolidation. |
| GET | `/events/stream` | Multiplexed server-sent event stream. |

### Optional chat

| Method | Path | Purpose |
|---|---|---|
| GET, POST | `/chat/sessions` | List or create local chat sessions. |
| GET, DELETE | `/chat/sessions/{session_id}` | Read or delete a session. |
| GET, POST | `/chat/sessions/{session_id}/messages` | Read messages or stream a reply. |

Chat uses the same memory and provenance interfaces as MCP. It is an optional
consumer of the personal model, not a second model store.

## Security boundary

- The server defaults to `127.0.0.1`.
- Origin and host guards reject non-loopback browser access.
- Raw captures and configuration are local sensitive data.
- LLM and embedding egress only use endpoints configured by the user.
- `openapi.json` describes the API surface, not an authorization grant.

## Removed product surfaces

The paper runtime does not expose work-thread tracking, meeting assistance,
computer-use actuation, day-0 filesystem profiling, memoir/book generation, or
background product run boards. Upgrades do not destructively drop their legacy
SQLite tables, but new databases no longer create them and current code no
longer reads or writes them.

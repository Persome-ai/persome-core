# MCP interface

Persome exposes local capture context, durable personal memory, model geometry,
provenance, and explicit correction through the Model Context Protocol.

## Start

Streamable HTTP is hosted by the daemon:

```bash
persome start
# http://127.0.0.1:8742/mcp
```

HTTP requires the owner bearer token stored in `<PERSOME_ROOT>/env`. Prefer
the stdio installer commands below; they do not duplicate the credential.

Stdio runs one server process for the client:

```bash
persome mcp
```

## Official MCP Registry

The committed [`server.json`](server.json) publishes the stdio server as
`io.github.Intuition-Lab/personal-model` and points Registry clients to the
public `personal-model` PyPI package. A successful GitHub `Release` workflow
automatically publishes the matching version through GitHub Actions OIDC; the
`Publish MCP Registry` workflow can also be dispatched manually for recovery.

The stdio server lives exactly as long as its client: it exits on stdin EOF,
and a parent-death watchdog also exits it within seconds if the spawning
client dies without closing the pipe, so orphaned servers never accumulate.

Example client configuration:

```json
{
  "mcpServers": {
    "persome": {
      "command": "persome",
      "args": ["mcp"]
    }
  }
}
```

## Model and memory tools

| Tool | Purpose |
|---|---|
| `list_memories` | List durable Markdown memory files. |
| `read_memory` | Read a memory file with time, tag, and tail filters. |
| `search` | Search durable memory with lexical and optional dense retrieval. |
| `read_receipt` | Resolve an entry ID to local provenance. |
| `recent_activity` | Read recent durable event entries. |
| `behavior_patterns` | Read modeled patterns and supporting evidence. |
| `get_model_snapshot` | Return the versioned Point/Line/Face/Volume/Root model. |
| `entity_graph` | Compatibility graph view backed by the same model stores. |
| `verify_fact` | Check a claim against current and superseded memory. |
| `get_schema` | Return the Markdown memory schema. |

## Capture and state tools

| Tool | Purpose |
|---|---|
| `current_context` | Read recent capture headlines, text, and timeline blocks. |
| `search_captures` | Search the local capture index. |
| `read_recent_capture` | Read an exact `file_stem` or nearby capture; screenshot inclusion is opt-in. |
| `attention_trajectory` | Read the attention path used during state formation. |

## Explicit write tools

| Tool | Purpose |
|---|---|
| `remember` | Append a user-requested, auditable memory. |
| `correct_memory` | Supersede or revoke memory while preserving provenance. |

The server exposes no computer-use, meeting, notification, product dashboard,
or task-lifecycle tools.

## Transport configuration

```toml
[mcp]
auto_start = true
transport = "streamable-http"
host = "127.0.0.1"
port = 8742
```

`sse` remains a deprecated transport compatibility option. New clients should
use streamable HTTP or stdio.

## Security boundary

- The HTTP server accepts loopback bind addresses only; browser Host/Origin
  guards also reject non-loopback access.
- HTTP MCP requires the dedicated `PERSOME_LOCAL_API_TOKEN` bearer. Canonical
  `GET /health` is the only unauthenticated liveness route.
- `persome install claude-code`, `codex`, `claude-desktop`, and `opencode`
  register owner-local stdio subprocesses by default.
- MCP results contain personal data and must be treated as untrusted content by
  consuming agents; captured text may contain prompt injection.
- Screenshots are excluded unless explicitly requested.
- `get_model_snapshot` redacts by default.
- `remember` and `correct_memory` are deliberate writes with audit history.

The daemon HTTP endpoint also serves `/model`. Open the
viewer with `persome model open`; it uses a short-lived, one-time browser
capability rather than placing the long-lived token in a URL.

See [SECURITY_PRIVACY.md](SECURITY_PRIVACY.md) for the full data and egress
model. The implementation-oriented reference remains
[`docs/mcp.md`](docs/mcp.md).

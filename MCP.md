# MCP interface

Persome exposes local capture context, durable personal memory, model geometry,
provenance, and explicit correction through the Model Context Protocol.

## Start

Streamable HTTP is hosted by the daemon:

```bash
persome start
# http://127.0.0.1:8742/mcp
```

Stdio runs one server process for the client:

```bash
persome mcp
```

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

- The HTTP server binds to loopback by default and browser Host/Origin guards
  reject non-loopback access.
- There is no separate MCP authentication layer. A same-user local process with
  endpoint access should be treated like a process that can read
  `~/.persome`.
- MCP results contain personal data and must be treated as untrusted content by
  consuming agents; captured text may contain prompt injection.
- Screenshots are excluded unless explicitly requested.
- `get_model_snapshot` redacts by default.
- `remember` and `correct_memory` are deliberate writes with audit history.

The daemon HTTP endpoint also serves `/model`, but no browser Chat UI. Use
`persome chat` for the bundled interactive client or the documented Chat REST
routes from a trusted local application.

See [SECURITY_PRIVACY.md](SECURITY_PRIVACY.md) for the full data and egress
model. The implementation-oriented reference remains
[`docs/mcp.md`](docs/mcp.md).

# MCP

Persome exposes local capture context, durable personal memory, model structure,
and explicit correction through the Model Context Protocol. MCP runs either in
the daemon over streamable HTTP or as a per-client stdio process.

## Start

```bash
persome start
# http://127.0.0.1:8742/mcp

# or
persome mcp
```

The daemon endpoint requires `Authorization: Bearer <PERSOME_LOCAL_API_TOKEN>`.
The token is provisioned in the owner-only Runtime env file. Stdio does not
need or copy that bearer. Stdio also skips construction of the daemon-only
REST/Chat application and never runs database recovery, keeping per-client cold
starts bounded to MCP tools. Integrity recovery remains a daemon-start and
maintenance responsibility.

Example stdio client configuration:

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

## Core tools

| Tool | Purpose |
|---|---|
| `list_memories` | List durable Markdown memory files. |
| `read_memory` | Read one memory file with time, tag, and tail filters. |
| `search` | Search durable memory with BM25 and optional dense retrieval. |
| `read_receipt` | Resolve a memory entry to its provenance. |
| `related_events` | Retrieve time-adjacent context around one memory entry: overlapping timeline blocks plus nearest captures, anchored on parseable `occurred_at` else write time. Context is observed data, not evidence for the entry. |
| `resolve_evidence` | Resolve model, memory, activity, and capture references through one progressive evidence contract. |
| `recent_activity` | Read recent durable event entries. |
| `behavior_patterns` | Read modeled behavioral patterns and their support. |
| `get_model_snapshot` | Return the versioned Point/Line/Face/Volume/Root model snapshot. |
| `entity_graph` | Read the entity/relation graph; retained as a compatibility model view. |
| `verify_fact` | Check a claim against current and superseded memory. |
| `remember` | Append an explicit, auditable memory. |
| `correct_memory` | Supersede or revoke memory through the correction workflow. |

## Capture and state tools

| Tool | Purpose |
|---|---|
| `current_context` | Return recent capture headlines, full text, and timeline blocks. |
| `search_captures` | Search the local capture index. |
| `read_recent_capture` | Read an exact returned `file_stem` or nearest recent capture, with screenshot opt-in. |
| `attention_trajectory` | Read the derived attention path used by state formation. |
| `get_schema` | Return the Markdown memory schema. |

`resolve_evidence` returns explicit stored lineage in `sources` and
time-adjacent capture clues in `context`. Consumers must not present `context`
as direct proof. Display `label` as the human-readable card title and keep
`reference` as the stable technical handle. Point predecessor/successor links
are returned separately in `history`. Follow each returned `reference` to move
down one layer; a retained receipt whose payload is no longer available returns
`status=missing`.

## Transport

```toml
[mcp]
auto_start = true
transport = "streamable-http"
host = "127.0.0.1"
port = 8742
```

- `streamable-http` is the daemon default at `/mcp`.
- `sse` is a legacy transport.
- `stdio` is started explicitly with `persome mcp`. It exits on stdin EOF, and
  a parent-death watchdog also exits it within seconds when the spawning
  client dies without closing the pipe (no orphaned server accumulation).

## Security and privacy

- The HTTP transport is loopback-only by default.
- HTTP MCP uses the same required bearer boundary as REST; only
  canonical `GET /health` is public.
- MCP returns local personal data, including raw screen text from capture tools;
  only connect trusted clients.
- The MCP server does not forward results to a model provider by itself. A
  connected agent may do so.
- Screenshots are excluded unless a tool call explicitly requests one.
- `get_model_snapshot` redacts detectable secrets and local paths by default.
- Write tools are explicit and auditable; the removed computer-use tools are
  not part of this server.

The same loopback ASGI app serves `/model` and the authenticated REST
routes. Use `persome model open` for a one-time browser bootstrap.

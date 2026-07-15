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

Stdio processes are shared-database clients: they can read and apply explicit
row-level memory writes, but they do not create or migrate schema and they
disable per-connection WAL auto-checkpoints. Onboarding normally initializes
the database already; for a brand-new or externally upgraded data root, run
`persome start` once before connecting stdio clients. While the daemon is live,
it pre-initializes every registered store schema, publishes an exact revision
and schema fingerprint, keeps the schema-owner connection, and performs WAL
maintenance. A stdio client rejects a missing, stale, future, or mismatched
receipt, and its SQLite authorizer rejects any schema-changing statement that
escapes the client-safe store helpers.

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
| `behavior_patterns` | Read modeled behavioral patterns plus evidence-backed observed workflow playbooks. |
| `get_model_snapshot` | Return the versioned Point/Line/Face/Volume/Root model snapshot. |
| `entity_graph` | Read the entity/relation graph; retained as a compatibility model view. |
| `verify_fact` | Check a claim's freshness and explain existing open contradiction ledger rows. |
| `remember` | Append an explicit, auditable memory. |
| `correct_memory` | Supersede or revoke memory through the correction workflow. |
| `process_pending_model_work` | Process a bounded number of pending sessions with the connected client's model allowance through MCP Sampling. |
| `get_pending_model_work` | Inspect the semantic session backlog without invoking a model. |

## Capture and state tools

| Tool | Purpose |
|---|---|
| `current_context` | Return recent capture headlines, full text, and timeline blocks. |
| `search_captures` | Search the local capture index. |
| `read_recent_capture` | Read an exact returned `file_stem` or nearest recent capture, with screenshot opt-in. |
| `attention_trajectory` | Read the derived attention path used by state formation. |
| `get_schema` | Return the Markdown memory schema. |

`search_captures` degrades explicitly: while the daemon's index-health report
is degraded or stale, its JSON payload carries an `index_health` object
(`status`, `index`, `capture_state`, `index_backlog`, `note`), and a corrupt
capture index raises an actionable tool error rather than returning silently
partial results.

## Wearable tools

| Tool | Purpose |
|---|---|
| `query_health_metrics` | Query owner-authorized wearable observations by metric, provider, time range, and limit. |

Wearable results are raw consumer-device observations with source and import
timestamps. Clients must not present them as medical diagnoses.

`resolve_evidence` returns explicit stored lineage in `sources` and
time-adjacent capture clues in `context`. Consumers must not present `context`
as direct proof. Display `label` as the human-readable card title and keep
`reference` as the stable technical handle. Point predecessor/successor links
are returned separately in `history`. Follow each returned `reference` to move
down one layer; a retained receipt whose payload is no longer available returns
`status=missing`.

`behavior_patterns` includes only active skill files with a current
evidence-backed observed-pattern entry. Later trigger echoes do not replace the
playbook. A playbook can guide personalization and imitation, but it never grants
a client permission to execute the observed actions.

`verify_fact` remains a deterministic read: it does not judge whether a claim is
true. When a recalled entry participates in an open contradiction ledger row,
the result includes the recorded reason and bounded competing claim. Resolved or
dismissed rows are not replayed.

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
- Read tools do not invoke a model provider. The explicit
  `process_pending_model_work` tool sends modeling prompts and tool results back
  to the originating trusted client through MCP Sampling; the client remains in
  control of its model, authentication, approval policy, and allowance.
- Screenshots are excluded unless a tool call explicitly requests one.
- `get_model_snapshot` redacts detectable secrets and local paths by default.
- Write tools are explicit and auditable; the removed computer-use tools are
  not part of this server.

## Agent-funded modeling

`process_pending_model_work(max_sessions=1)` is the provider-neutral path for
using a model entitlement already available in Codex, Claude, or another MCP
client. Persome negotiates the client's `sampling` and `sampling.tools`
capabilities. When supported, stage prompts run through
`sampling/createMessage`; no subscription credential or OAuth token is read,
copied, or persisted by Persome.

The operation must originate in a client tool call and is bounded to 1–10
sessions. Persome does not initiate MCP Sampling from its background daemon.
Cancelling the originating tool call cancels any in-flight Sampling request;
the per-call Sampling deadline does the same, and either path rejects further
Sampling calls so no additional client allowance is spent.
Clients that only implement MCP tools return
`client_missing_sampling_with_tools`; use another compatible client, a local
provider, or a configured API provider in that case. Existing scheduled writers
continue to use the configured provider route.

The same loopback ASGI app serves `/model` and the authenticated REST
routes. Use `persome model open` for a one-time browser bootstrap.

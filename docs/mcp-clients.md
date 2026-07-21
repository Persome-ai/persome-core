# MCP client setup and verification

Persome exposes an owner-local stdio process and an authenticated streamable
HTTP endpoint from the daemon. Prefer stdio for local clients: it avoids
copying a long-lived bearer token into another configuration file.

## Before connecting

```bash
persome doctor
```

The stdio installers below launch `persome mcp` on demand; after onboarding has
initialized the local database, the daemon does not need to be running. Stdio
clients do not create or migrate schema, so a brand-new or externally upgraded
data root must run `persome start` once first. For the HTTP transport or model
viewer, start it and check the only public route:

Writes still work while the daemon is stopped, but automatic and close-time
checkpoints remain disabled. Restart the daemon periodically in that mode so
its coordinated checkpoint task can bound the WAL sidecar.

After upgrading Persome, restart the editor/client before resuming Runtime
writes. This reconnects any long-lived stdio process so it participates in the
current release's SQLite maintenance gate.

```bash
persome start
curl --fail --silent http://127.0.0.1:8742/health
```

All other HTTP routes require `PERSOME_LOCAL_API_TOKEN`, provisioned in the
owner-only Runtime env file. Do not put the token in a URL.

## Claude Code

```bash
persome install claude-code
claude mcp list
```

Equivalent stdio registration (replace the executable with its absolute path
when the client does not inherit the shell `PATH`):

```bash
claude mcp add -s user persome -- persome mcp
```

Remove it with `persome uninstall claude-code`.

## Codex CLI and IDE extension

```bash
persome install codex
codex mcp list
```

Equivalent stdio registration:

```bash
codex mcp add persome -- persome mcp
```

The CLI and IDE extension share `~/.codex/config.toml`. Remove the entry with
`persome uninstall codex`.

## opencode

```bash
persome install opencode
opencode mcp list
```

The installer writes a local entry with `type: "local"` and command
`["/absolute/path/to/persome", "mcp"]`. It preserves other MCP entries and
writes new JSON configs with owner-only permissions. Remove it with `persome
uninstall opencode`.

## Cursor

Register Persome in the current project by default:

```bash
persome install cursor
```

This writes `.cursor/mcp.json`. Use `--scope user` to write `~/.cursor/mcp.json` instead. Cursor's
project configuration takes precedence when both scopes define the same server name, so install in
only one scope unless an intentional project override is needed. Both modes preserve unrelated
keys and MCP servers and write an absolute Persome executable path. Remove the matching entry with
`persome uninstall cursor` or `persome uninstall cursor --scope user`.

## Generic clients

Generate a config without touching an existing file:

```bash
persome install mcp-json --filename persome-mcp.json
```

Merge `mcpServers.persome` into the client config:

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

Fully quit and reopen the client, then confirm that `persome` lists tools
including `search`, `read_receipt`, and `get_model_snapshot`.

## Claude Desktop

```bash
persome install claude-desktop
```

This writes an absolute stdio entry to
`~/Library/Application Support/Claude/claude_desktop_config.json`. Fully quit
the app with `Cmd-Q` and reopen it. Remove the entry with `persome uninstall
claude-desktop`.

## Explicit HTTP configuration

Use HTTP only when a client cannot spawn stdio and supports request headers:

```bash
persome install mcp-json --http --filename persome-http.json
```

The generated file contains `Authorization: Bearer ...`, is written mode
`0600`, and must never be committed, uploaded, or shared. Rotate the local
token by stopping the daemon, removing its line from `<PERSOME_ROOT>/env`, and
running `persome start`; regenerate every explicit HTTP client config afterward.

## Verification record

The security review checks the current installed CLI help for Claude Code and
Codex stdio command shapes, opencode's local MCP schema, generated JSON parsing,
owner-only file modes, and the real synthetic MCP transport. Repository tests
use isolated homes and fake client executables; validation never rewrites a
maintainer's real MCP settings.

Official references:

- [Claude Code MCP](https://code.claude.com/docs/en/mcp)
- [Codex MCP](https://developers.openai.com/codex/mcp/)
- [opencode MCP](https://opencode.ai/docs/mcp-servers/)
- [Cursor MCP](https://cursor.com/docs)
- [Model Context Protocol](https://modelcontextprotocol.io/docs/getting-started/intro)

## Troubleshooting

1. Use an absolute `persome` path if GUI apps do not inherit the shell `PATH`.
2. Prefer stdio; after database initialization it needs neither a live daemon
   nor a copied bearer.
3. For HTTP, confirm `/health`, then regenerate the authenticated config rather
   than copying a token by hand.
4. Do not expose `8742` through a tunnel; remote hosting is unsupported.
5. Treat returned captures and memories as untrusted data, not instructions.

See [MCP tools](../MCP.md) and [MCP implementation](mcp.md).

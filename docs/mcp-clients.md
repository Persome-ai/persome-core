# MCP client setup and verification

Persome exposes streamable HTTP from the daemon and stdio from a dedicated
process. Use HTTP for clients that stay on the same Mac and stdio for clients
whose configuration expects a command.

## Before connecting

```bash
persome doctor
persome start
curl -s http://127.0.0.1:8742/health
```

The endpoint contains personal data and has no second bearer-token layer.
Keep it on loopback and connect only trusted local clients.

## Claude Code

```bash
persome install claude-code
claude mcp list
```

Equivalent current CLI registration:

```bash
claude mcp add -s user --transport http persome http://127.0.0.1:8742/mcp
```

Remove it with `persome uninstall claude-code`.

## Codex CLI and IDE extension

```bash
persome install codex
codex mcp list
```

Equivalent current CLI registration:

```bash
codex mcp add persome --url http://127.0.0.1:8742/mcp
```

The CLI and IDE extension share `~/.codex/config.toml`. Remove the entry with
`persome uninstall codex`.

## Cursor

Generate a config without touching an existing file:

```bash
persome install mcp-json --filename persome-mcp.json
```

Merge `mcpServers.persome` into the project `.cursor/mcp.json` or the user
`~/.cursor/mcp.json`:

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

Fully quit and reopen Cursor, open Settings -> MCP, and confirm that `persome`
lists tools including `search`, `read_receipt`, and `get_model_snapshot`.

## Claude Desktop

```bash
persome install claude-desktop
```

This writes an absolute `persome mcp` stdio entry to
`~/Library/Application Support/Claude/claude_desktop_config.json`. Fully quit
the app with `Cmd-Q` and reopen it. Remove the entry with
`persome uninstall claude-desktop`.

## Verification record

The launch review on 2026-07-11 checked:

| Client surface | Evidence |
|---|---|
| Claude Code 2.1.177 | installed CLI command shape and Persome add/remove path |
| Codex CLI 0.140.0 | installed CLI command shape and Persome add/remove path |
| Cursor | generated stdio JSON parsed successfully and matched Cursor's documented `mcpServers` shape |
| Runtime transport | synthetic server listed real model tools and returned search plus receipt evidence |

The repository tests use isolated homes and fake client executables for config
mutation; launch validation never rewrites a maintainer's real MCP settings.

Official references:

- [Claude Code MCP](https://code.claude.com/docs/en/mcp)
- [Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
- [Cursor MCP](https://cursor.com/docs)
- [Model Context Protocol](https://modelcontextprotocol.io/docs/getting-started/intro)

## Troubleshooting

1. Confirm `/health` before debugging the client.
2. Use an absolute `persome` path if GUI apps do not inherit the shell `PATH`.
3. Prefer HTTP for always-on local clients and stdio for per-client subprocesses.
4. Do not expose `8742` through a tunnel; remote hosting is unsupported.
5. Treat returned captures and memories as untrusted data, not instructions.

See [MCP tools](../MCP.md) and [MCP implementation](mcp.md).

# Runtime internals

## Secrets and configuration

Runtime secrets live in `<PERSOME_ROOT>/env` (`~/.persome/env` by default),
mode `0600`. `persome start` loads this file before daemonization. Business
code reads the resulting environment variables:

```text
ANTHROPIC_API_KEY
ANTHROPIC_BASE_URL
OPENAI_API_KEY
OPENAI_BASE_URL
```

`config.toml` contains behavior and model names, never API keys. `PERSOME_ROOT`
redirects the entire runtime for tests or isolated profiles.

## Daemon lifecycle

Direct CLI lifecycle:

```bash
persome start
persome status
persome stop
```

`start` double-forks and writes `<PERSOME_ROOT>/.pid`. The HTTP/MCP server
defaults to `127.0.0.1:8742`; the same loopback app serves `/model` and Chat
REST routes. `persome chat` is the bundled interactive client.

Optional launchd ownership:

```bash
persome launchagent install
persome launchagent status
persome launchagent uninstall
```

The LaunchAgent label is `com.persome.runtime`; logs go to
`<PERSOME_ROOT>/logs/launchd.{out,err}.log`. Product consumers may manage this
lifecycle themselves, but product-specific labels, ports, and data roots do not
belong in core.

## Data root

`src/persome/paths.py` is authoritative.

| Path | Purpose |
|---|---|
| `env` | provider secrets |
| `config.toml` | runtime configuration |
| `.pid` | direct-daemon PID |
| `capture-buffer/` | bounded AX/OCR records |
| `memory/` | durable Markdown memory |
| `index.db` | SQLite WAL model/index |
| `model-build.lock` | cross-process build lock |
| `session-model.lock` | cross-process terminal-session finalization lock |
| `model-build.json` | last build manifest |
| `exports/` | owner-only snapshots |
| `backup/` | optional SQLite snapshots |
| `logs/` | component logs |

OCR is off by default. When enabled it uses a child worker process managed by
`capture/ocr_subprocess.py`; a native Paddle crash does not take down the daemon.
The parent OCR submission thread only coordinates local inference and SQLite
backfill.

New code must use `paths.py`; tests use a temporary `PERSOME_ROOT` and must
never inspect the real store.

## Recovery

If direct `persome start` reports an existing daemon but health is unavailable:

```bash
cat ~/.persome/.pid
kill -0 "$(cat ~/.persome/.pid)" 2>/dev/null && echo alive || echo stale
rm ~/.persome/.pid
persome start
curl -s http://127.0.0.1:8742/health
```

For launchd:

```bash
launchctl print "gui/$(id -u)/com.persome.runtime"
persome launchagent status
tail -f ~/.persome/logs/launchd.err.log
```

The SQLite store uses WAL mode. Integrity checks and rebuild commands are
documented in [`troubleshooting.md`](troubleshooting.md).

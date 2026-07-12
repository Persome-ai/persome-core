# Runtime internals

## Secrets and configuration

Runtime secrets live in `<PERSOME_ROOT>/env` (`~/.persome/env` by default),
mode `0600`. `persome start` loads this file before daemonization. Business
code reads the resulting environment variables:

```text
PERSOME_LLM_API_KEY (active Runtime provider)
OPENAI_API_KEY / OPENAI_BASE_URL (optional dense retrieval)
PERSOME_SCREENSHOT_KEY
PERSOME_LOCAL_API_TOKEN
```

`config.toml` contains behavior plus the provider id, protocol, model, endpoint,
and key variable name, never the key value. `persome llm setup` writes that
profile only after a live check. `PERSOME_ROOT`
redirects the entire runtime for tests or isolated profiles.
`install.sh` generates the machine-local screenshot key and local HTTP bearer
automatically and preserves both across reinstalls; neither is a provider
credential.

## Daemon lifecycle

Direct CLI lifecycle:

```bash
persome start
persome status
persome stop
```

`persome update` fetches official `main` into a temporary checkout, stops the
current lifecycle owner, invokes `install.sh --update`, and lets onboarding
prove the replacement Runtime before completion. A prior LaunchAgent is
restored with the new executable after installation. The updater pins both
`PERSOME_ROOT` and the installer's `PERSOME_INSTALL_HOME` to the active data
root so isolated/custom profiles cannot be redirected to `~/.persome`.
Unlike a fresh install, update mode keeps the previous virtualenv backup through
onboarding. Only a successful OCR/health/capture proof commits the replacement;
the installer stops a failed replacement daemon before restoring the backup.

`start` double-forks and writes `<PERSOME_ROOT>/.pid`. The HTTP/MCP server
is restricted to loopback and defaults to `127.0.0.1:8742`; the same app serves `/model` and the
REST routes. Except for canonical `GET /health`, the outer app requires the
dedicated bearer provisioned in the owner-only env file. `persome model open`
uses the one-time browser exchange.

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
| `env` | provider secrets plus generated screenshot and local-API credentials |
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

Supported installs enable OCR through `persome ocr setup`. It uses a child
worker process managed by `capture/ocr_subprocess.py`; a native Paddle crash
does not take down the daemon. The parent OCR submission thread only coordinates
local inference and SQLite backfill. Quick health checks inspect configuration,
Runtime, weights, kill switch, and Screen Recording without loading Paddle;
`persome ocr status --check` verifies the worker engine.

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

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
profile only after a live check. `[capture].ocr_policy` records OCR intent:
`auto` is unconfigured, while `enabled` and `disabled` are durable user choices
that ordinary onboarding and updates preserve. `PERSOME_ROOT`
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

`persome update` fetches official `main` into a temporary checkout and asks the
installer to build a marked, inactive `venv.replacement.update`; the current
`venv` remains executable throughout preparation. The candidate is created as a
relocatable uv environment, and activation rejects any `persome` console script
that still embeds the inactive candidate path. Under `.update.lock`, the
updater stops the current lifecycle owner and exchanges those two same-filesystem
directories in one kernel operation (`renameatx_np(RENAME_SWAP)` on macOS). It
then restores the prior background/LaunchAgent owner and runs onboarding with an
explicit owner requirement. Only a successful mode-aware permission, OCR-policy,
health, and capture/readiness proof advances through `committing`; the old venv
is retained at the replacement path until that point. Failure or recovery uses
the same atomic exchange in reverse before restoring the old owner. The
transaction id and `preparing`/`prepared`/`activated`/`committing` phase are
fsynced in `.update-state.json`, so a crash between the exchange and phase write
is recoverable from the candidate marker. The updater pins both `PERSOME_ROOT`
and the installer's `PERSOME_INSTALL_HOME` to the active data root so an isolated
profile cannot be redirected to `~/.persome`.

Model builds materialize `<PERSOME_ROOT>/HUMAN.md` after producing the current
raw snapshot. Daemon startup and `persome onboard` also reconcile it, which
backfills a valid existing Root after `persome update` or after
`uv tool upgrade personal-model` plus onboarding without recapture or an LLM
call. Reconciliation emits a forming placeholder when no Root exists and
refuses to replace an unmarked file at that path.

`start` holds `<PERSOME_ROOT>/.daemon.lock` from preflight through the entire
foreground or double-forked daemon lifetime. This prevents two concurrent
starters from both passing the PID check. The daemon writes a numeric `.pid` for
one-release compatibility plus an owner-only `.runtime-state.json` containing
its random generation, start/update times, readiness phase, effective capture
and OCR policy, native permission probes, and the last capture/privacy receipt.
Lifecycle operations resolve PID, current-user ownership, command/executable,
process start time, and (when present) generation immediately before signaling.
A dead or unrelated reused PID is stale; a live Persome-shaped process with an
invalid generation fails closed instead of allowing a second writer.

The HTTP/MCP server
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
`<PERSOME_ROOT>/logs/launchd.{out,err}.log`. A successful install writes the
owner-only `.launchagent-owner` intent marker. Ownership proof requires the
loaded launchd job, its configured program, its live PID, and the recorded
Runtime process to agree; merely finding a plist or marker is insufficient.
Product consumers may manage this lifecycle themselves, but product-specific
labels, ports, and data roots do not belong in core.

## Data root

`src/persome/paths.py` is authoritative.

| Path | Purpose |
|---|---|
| `env` | provider secrets plus generated screenshot and local-API credentials |
| `config.toml` | runtime configuration |
| `.pid` | compatibility PID receipt; never sufficient by itself for signaling |
| `.runtime-state.json` | owner-only generation, phase, permission, OCR, and capture receipt |
| `.daemon.lock` | lifetime single-Runtime lock inherited across background forks |
| `.launchagent-owner` | durable launchd lifecycle intent |
| `.update.lock`, `.update-state.json` | exclusive updater lock and crash-recovery transaction |
| `venv/` | active installed Runtime |
| `venv.replacement.update` | inactive candidate before exchange; retained old venv after exchange |
| `venv.previous.committed.*`, `venv.failed.update.*` | best-effort post-commit or failed-candidate cleanup |
| `native/<source-digest>/` | immutable machine-local AX helper and watcher binaries |
| `capture-buffer/` | bounded AX/OCR records |
| `memory/` | durable Markdown memory |
| `index.db` | SQLite WAL model/index |
| `model-build.lock` | cross-process build lock |
| `session-model.lock` | cross-process terminal-session finalization lock |
| `model-build.json` | last build manifest |
| `HUMAN.md` | Persome-managed raw model reading view; owner-only mode `0600` |
| `.integrity-recovery.pending.json` | resumable full-database recovery phase journal |
| `.integrity-config-recovery.pending.json` | pre-quarantine config intent and authority guard |
| `.integrity-recovery.json` | last completed quarantine/recovery report |
| `exports/` | owner-only snapshots |
| `backup/` | optional SQLite snapshots |
| `logs/` | component logs |

Supported installs enable OCR through `persome ocr setup` or an explicit
`persome onboard --tier ...`. `persome ocr disable` records a durable opt-out;
an ordinary `persome onboard` and every update preserve that policy and tier.
The child worker managed by `capture/ocr_subprocess.py` isolates native Paddle
faults from the daemon. Quick health checks inspect configuration, Runtime,
weights, kill switch, and Screen Recording without loading Paddle. Run
`persome ocr status --check` to verify the worker engine. In trusted-ingest mode,
the producer owns the OS permission boundary; the daemon starts no AX watcher
and requires authenticated HTTP ingest readiness.

## Native helper identity

The wheel ships Swift source rather than a mutable helper binary. At install or
first resolution, Persome hashes a format version, the machine architecture,
and the source bytes, then compiles under
`<PERSOME_ROOT>/native/<source-digest>/`. A root-scoped build lock and atomic
rename prevent partial or concurrent publication. Existing executable files at
that immutable path are reused exactly, so reinstalling the same source version
does not manufacture a new TCC principal. The capture helper and, when
`event_driven=true`, watcher each run their own `--check-accessibility` and
`--request-accessibility` action; the daemon or invoking terminal cannot prove
their grants on their behalf.

A helper-source change intentionally resolves a new digest path and therefore
requires an explicit new Accessibility grant during onboarding. Rollback runs
the old wheel source and deterministically resolves the prior binary path again.
Never overwrite a digest path in place; that would break both the immutable
identity contract and macOS permission diagnostics.

New code must use `paths.py`; tests use a temporary `PERSOME_ROOT` and must
never inspect the real store.

## Recovery

If direct `persome start` reports unsafe or ambiguous lifecycle state, inspect
without deleting or signaling from the numeric PID alone:

```bash
persome status
cat ~/.persome/.pid
cat ~/.persome/.runtime-state.json | jq .
launchctl print "gui/$(id -u)/com.persome.runtime" 2>/dev/null || true
lsof -nP -iTCP:8742 -sTCP:LISTEN
```

Do not `kill $(cat .pid)` or remove `.pid`/`.runtime-state.json` while a live
Persome-shaped process may exist: the number may have been reused, or the
generation receipt may be the evidence preventing a second writer. Stop the
owning Desktop app or LaunchAgent first, then rerun `persome stop` or
`persome start`; dead and unrelated reused PIDs are treated as safely stale. If
the port belongs to another application, stop it or change the configured port
rather than erasing Runtime receipts.

For launchd:

```bash
launchctl print "gui/$(id -u)/com.persome.runtime"
persome launchagent status
tail -f ~/.persome/logs/launchd.err.log
```

The SQLite store uses WAL mode. Integrity checks and rebuild commands are
documented in [`troubleshooting.md`](troubleshooting.md).

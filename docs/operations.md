# Operations and data control

This guide covers the supported platform, permissions, local paths, lifecycle,
inspection, correction, export, erasure, and uninstall behavior.

## Support matrix

| Host | AX capture | PP-OCRv6 | Daemon, MCP, model |
|---|---|---|---|
| macOS 13+ Apple Silicon | yes | yes, bundled and local | supported |
| macOS 13+ Intel | yes | no compatible Paddle runtime | supported without OCR |
| Linux | no | no | offline tests and development only |
| Windows | no | no | unsupported |

The installer uses Python 3.11-3.13. Xcode Command Line Tools are required to
compile the Swift AX helpers.

## Permissions

| Permission | Required | Purpose | Effect when disabled |
|---|---|---|---|
| Accessibility | yes for live collection | reads the focused AX tree, application, window, selected control, and visible text | daemon remains healthy but produces no useful live captures |
| Screen Recording | yes in the standard install | supplies focused-window pixels to the enabled local OCR worker and encrypted screenshot retention | AX collection continues, but health reports OCR degraded and AX-poor surfaces remain sparse |
| Full Disk Access | no | not used | no effect |
| Automation / Apple Events | no | not used by the Runtime | no effect |

Grant permissions to the executable that launches Persome. If a terminal starts
the daemon, grant that terminal. If launchd later owns the daemon, rerun
`persome doctor` after the handoff and confirm live capture.

Interactive `install.sh` runs `persome onboard`. It presents separate native
dialogs for Accessibility and Screen Recording, waits for both live TCC probes,
verifies the OCR worker, starts the daemon, polls local health, and writes one
fresh capture. Rerun `persome onboard` after changing the launcher identity;
use `persome ocr disable` for an explicit OCR opt-out.

## Local paths

`PERSOME_ROOT` overrides the default `~/.persome` root.

| Path under the root | Contents |
|---|---|
| `config.toml` | non-secret Runtime configuration |
| `env` | provider credentials, screenshot key, and local API bearer; mode `0600` |
| `capture-buffer/` | AX text and optional encrypted screenshot payloads |
| `memory/` | readable Markdown memory |
| `index.db` | FTS, canonical evomem nodes, relations, sessions, and geometry |
| `chat-history/`, `skills/` | legacy Chat-era data from older releases; purged by `persome clean all` (stale Chat-era `logs/chat.log*` files are removed at startup) |
| `exports/` | redacted model exports by default; mode `0600` |
| `backup/` | SQLite safety snapshots containing personal model state |
| `logs/` | daemon and launchd logs; may contain operational context |
| `model-build.json` | latest build manifest and degraded-stage report |
| `venv/` | dedicated installer environment; code, not personal data |

Treat the whole root as sensitive. Backups and exports are copies of personal
data, not harmless metadata. Persome enforces `0700` on the root/data
directories and `0600` on sensitive files, repairs legacy modes once after an
upgrade, and gives its LaunchAgent umask `0077`.

## Startup recovery

When the Runtime is stopped, startup validates `index.db` before opening it.
If only a derived search index is malformed, Persome rebuilds `captures_fts`
from authoritative `captures` rows and `entries` from Markdown/evo_nodes. It
also handles older SQLite builds that collapse both damaged FTS projections
into a generic malformed-database error, while verifying every regular table
before the narrow index reset. Core database damage is still quarantined as
`index.db.corrupt.<timestamp>` for inspection. The running daemon owns active
SQLite writes, so `persome status`, MCP clients, and a second `persome start`
do not repair or quarantine an open database.

## Lifecycle and first recall

```bash
persome onboard
persome llm status --check
persome ocr status --check
persome doctor
persome start
persome status
persome pause
persome resume
persome stop
```

## Update the Runtime

```bash
persome update
```

The updater fetches a fresh shallow copy of the official `main` branch instead
of editing a user's source checkout. It stops either the background daemon or
the owner LaunchAgent, invokes the same locked wheel installer with existing
LLM/MCP setup preserved, reruns onboarding and its fresh-capture proof, and
restores prior LaunchAgent ownership. Configuration, secrets, capture history,
memory, model state, and logs under `PERSOME_ROOT` are not replaced.

`persome update --source /path/to/checkout` is the explicit developer/offline
path. The supplied tree must have the complete Persome source layout; the
updater never pulls or rewrites it.

The default active-session flush is five minutes. Timeline closure and model
processing add bounded local work, so the operational target for first useful
recall is at most ten minutes after valid AX capture and provider availability.
Without a hosted credential or keyless local endpoint, capture and BM25 recall
still work; semantic modeling reports a degraded state. Run
`persome llm setup` to change the active route, then restart the daemon.

Use these checks when memory does not appear:

```bash
persome doctor
persome status
persome timeline tick
persome model status
tail -F ~/.persome/logs/*.log
```

## Inspect

```bash
persome status
persome model status
persome delta-report
persome faces-report
persome root-report
persome contradictions
persome as-of --help
persome model open
```

The viewer and `GET /model/graph` are raw owner-local inspection surfaces.
Model export and MCP snapshot export redact by default.

## Correct and revoke

```bash
persome correct --help
persome contradictions-resolve --help
persome entity-retype --help
```

Trusted agents can call MCP `remember` and `correct_memory`. Correction keeps
the prior state and receipts so the change remains auditable. Use erasure, not
correction, when history itself must be deleted.

## Export

```bash
persome model export
persome model export --out ~/Desktop/persome-model.json
```

Exports default to `<PERSOME_ROOT>/exports/`, are redacted, and use mode `0600`.
`--raw` deliberately disables redaction and should never be attached to a
public issue or release.

## Delete personal data

Stop the daemon first so no writer can race the deletion.

```bash
persome stop

# Delete every file under memory/, FTS entries, canonical evomem, relations,
# Faces, Volumes, Root, exports, projections, backups, and recovery markers.
# Captures/timeline remain.
persome clean memory

# Delete all personal data, including captures, timeline/session state, model,
# legacy Chat-era history and skills, exports, backups, logs, and SQLite files.
# Keep config, env, and the installed venv.
persome clean all
```

Clean commands refuse to run while the daemon PID is live, even with `--yes`,
so a writer cannot retain an open SQLite handle or recreate data mid-erasure.
They require confirmation unless `--yes` is passed. `clean captures`
removes capture-buffer files and indexed capture rows, including capture rows
inside daily, unfinished, and integrity-quarantine SQLite copies. `clean
timeline` applies the same rule to timeline blocks. Explicit clean operations
use SQLite core and FTS5 secure deletion, VACUUM, WAL truncation, and journal
cleanup; unreadable recovery copies are removed rather than retaining a hidden
copy of data the user asked to erase. The FTS rebuild also clears historical
segment terms produced before secure-delete became the default.

## Uninstall

Remove MCP client entries while the CLI still exists:

```bash
persome uninstall claude-code
persome uninstall codex
persome uninstall claude-desktop
persome uninstall opencode
```

Then remove the daemon, LaunchAgent, shim, and dedicated virtual environment:

```bash
bash uninstall.sh
```

This keeps personal data and configuration. Full erasure is explicit:

```bash
bash uninstall.sh --delete-data --yes
```

The script refuses `/` and the home directory as install roots, and only removes
the CLI shim when it points to the expected Persome virtualenv.

## Backup and restore

Before moving data, stop Persome and copy the entire root so SQLite WAL sidecars,
Markdown, encryption material, and exports stay together. Do not publish the
copy. The `backup/` directory contains safety snapshots, but it is not a remote
backup service.

See [troubleshooting](troubleshooting.md),
[runtime internals](runtime-internals.md), and
[security and privacy](../SECURITY_PRIVACY.md).

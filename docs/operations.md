# Operations and data control

This guide covers the supported platform, permissions, local paths, lifecycle,
inspection, correction, export, erasure, and uninstall behavior.

## Support matrix

| Host | AX capture | PP-OCRv6 | Daemon, MCP, Chat, model |
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
| Screen Recording | only for OCR or retained screenshots | supplies focused-window pixels to the local OCR worker or encrypted screenshot retention | AX collection continues; OCR-poor surfaces remain sparse and no screenshot can be retained |
| Full Disk Access | no | not used | no effect |
| Automation / Apple Events | no | not used by the Runtime | no effect |

Grant permissions to the executable that launches Persome. If a terminal starts
the daemon, grant that terminal. If launchd later owns the daemon, rerun
`persome doctor` after the handoff and confirm live capture.

## Local paths

`PERSOME_ROOT` overrides the default `~/.persome` root.

| Path under the root | Contents |
|---|---|
| `config.toml` | non-secret Runtime configuration |
| `env` | provider credentials and screenshot key; mode `0600` |
| `capture-buffer/` | AX text and optional encrypted screenshot payloads |
| `memory/` | readable Markdown memory |
| `index.db` | FTS, canonical evomem nodes, relations, sessions, and geometry |
| `chat-history/` | local terminal/API Chat sessions |
| `exports/` | redacted model exports by default; mode `0600` |
| `backup/` | SQLite safety snapshots containing personal model state |
| `logs/` | daemon and launchd logs; may contain operational context |
| `model-build.json` | latest build manifest and degraded-stage report |
| `venv/` | dedicated installer environment; code, not personal data |

Treat the whole root as sensitive. Backups and exports are copies of personal
data, not harmless metadata.

## Lifecycle and first recall

```bash
persome doctor
persome start
persome status
persome pause
persome resume
persome stop
```

The default active-session flush is five minutes. Timeline closure and model
processing add bounded local work, so the operational target for first useful
recall is at most ten minutes after valid AX capture and provider availability.
No-key mode still supports capture and BM25 recall; semantic modeling reports a
degraded state until a provider is configured.

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
open http://127.0.0.1:8742/model
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

# Delete Markdown, FTS entries, canonical evomem, relations, Faces, Volumes,
# Root, exports, projections, and backups. Captures/timeline/Chat remain.
persome clean memory

# Delete all personal data, including captures, timeline/session state, model,
# Chat history, exports, backups, logs, and SQLite files. Keep config, env,
# installed venv, and custom skills.
persome clean all
```

Both commands require confirmation unless `--yes` is passed. `clean captures`
removes both capture-buffer files and indexed capture rows.

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

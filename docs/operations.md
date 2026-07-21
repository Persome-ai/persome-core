# Operations and data control

This guide covers the supported platform, permissions, local paths, lifecycle,
inspection, correction, export, erasure, and uninstall behavior.

## Support matrix

| Host | AX capture | Local OCR | Daemon, MCP, model |
|---|---|---|---|
| macOS 13+ Apple Silicon | yes | PP-OCRv6, bundled and local | supported |
| macOS 13+ Intel | yes | Apple Vision, system and local | supported |
| Linux | no | no | offline tests and development only |
| Windows | no | no | unsupported |

The installer uses Python 3.12-3.13. Xcode Command Line Tools are required to
compile the Swift AX helpers.

## Permissions

| Permission | Required | Purpose | Effect when disabled |
|---|---|---|---|
| Accessibility | yes for daemon-mode live collection | lets the source-versioned `mac-ax-helper` and optional `mac-ax-watcher` read focused AX structure and events | daemon remains healthy but produces no useful live AX captures |
| Screen Recording | yes when screenshots or effective OCR are enabled | supplies focused-window pixels to local OCR and encrypted screenshot retention | AX collection continues; pixel features are unavailable |
| Full Disk Access | no | not used | no effect |
| Automation / Apple Events | no | not used by the Runtime | no effect |

Accessibility belongs to the native executables that actually call AX, not to
the terminal or Python daemon that launches them. Onboarding requests and probes
`mac-ax-helper` first and, when `event_driven=true`, `mac-ax-watcher` separately.
Screen Recording is checked by the Runtime process that captures pixels. If the
Runtime lifecycle or helper source changes, rerun `persome onboard` and let it
name any new principal explicitly.

Interactive `install.sh` runs `persome onboard`. Standard daemon mode presents
separate native dialogs for the helper and watcher Accessibility grants, then
Screen Recording when the configured pixel policy requires it. It starts the
final lifecycle owner, verifies its isolated OCR worker when enabled, and writes
one fresh capture through that daemon's runner. The authenticated `/permissions`
endpoint invokes the actual helper/watcher probes and the Runtime's Screen
Recording preflight. HTTP-disabled daemon mode publishes the same generation's
owner-only state receipt; trusted ingest proves its authenticated runner and
does not claim daemon-owned macOS permissions. Intel uses the same worker
contract through Apple Vision; explicit OCR/pixel opt-out reports the actual
remaining AX/pixel capabilities.
Updates preserve paused/locked privacy state without forcing a frame. The first
OCR load can take up to two minutes; repeated runs publish progress and normally
reuse the worker.

`[capture].ocr_policy` makes intent durable: `auto` is a fresh/unconfigured
state, while `enabled` and `disabled` are explicit choices. `persome onboard`
without `--tier` preserves the current choice; `persome onboard --tier tiny` or
`persome ocr setup` enables OCR, and `persome ocr disable` records the opt-out.
The final notification is non-blocking.

Each compiled AX helper lives at an immutable
`native/<source-digest>/<helper-name>` path. Same-version reinstalls reuse the
exact files and grants. A helper-source change produces a new path and requires
an explicit new Accessibility grant; update rollback returns to the old wheel
and old helper path.

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
| `HUMAN.md` | raw deterministic reading view of the current model; owner-only mode `0600` |
| `.integrity-recovery.pending.json` | crash-resumable full-database quarantine/replay journal |
| `.integrity-config-recovery.pending.json` | config-quarantine intent retained until database authority is reconciled |
| `.pid`, `.runtime-state.json` | compatibility PID plus owner-only generation, phase, permission, OCR-worker, and capture/privacy receipt |
| `.daemon.lock` | lifetime single-Runtime lock inherited across background forks |
| `.launchagent-owner` | durable intent that launchd owns Runtime lifecycle |
| `.update.lock`, `.update-state.json` | exclusive update lock and crash-recovery phase metadata |
| `native/<source-digest>/` | immutable machine-local AX and Vision OCR binaries; code, not personal data |
| `venv.replacement.update`, `venv.previous.committed.*`, `venv.failed.update.*` | candidate, retained-old, and cleanup environments during a transaction; code, not personal data |
| `venv/` | dedicated installer environment; code, not personal data |

Treat the whole root as sensitive. Backups and exports are copies of personal
data, not harmless metadata. Persome enforces `0700` on the root/data
directories and `0600` on sensitive files, repairs legacy modes once after an
upgrade, and gives its LaunchAgent umask `0077`.

`HUMAN.md` is regenerated from the model without capture or an LLM call. A
valid pre-existing Root is backfilled during daemon startup or onboarding; no
Root produces an honest forming placeholder. Refreshes overwrite only the
Persome-managed projection marker. An unknown file at that path is left in
place and reported rather than adopted or overwritten.

## Startup recovery

When the Runtime is stopped, startup validates `index.db` before opening it.
If only a derived search index is malformed, Persome rebuilds `captures_fts`
from authoritative `captures` rows and `entries` from Markdown/evo_nodes. It
also handles older SQLite builds that collapse both damaged FTS projections
into a generic malformed-database error, while verifying every regular table
before the narrow index reset. Core database damage is still quarantined as
`index.db.corrupt.<timestamp>` for inspection. Persome then restores the newest
structurally verified daily snapshot when one exists and reconciles the current
Markdown memory projection without making LLM calls. Without a usable snapshot,
it still rebuilds the retrieval projection and Point state from Markdown; the
recovery marker lists database-only components that could not be reconstructed.
In both cases the old `model-build.json` is invalidated, because Faces, Volumes,
and Root must be verified by a fresh `persome model build` rather than reported
as a completed build from stale metadata. Details and recovered row counts are
written to `.integrity-recovery.json`.

Before moving a damaged database, Persome atomically writes
`.integrity-recovery.pending.json` with the exact quarantine destination and
phase. If the process exits after quarantine, snapshot copy, or projection
replay, the next stopped-Runtime startup resumes that journal instead of
treating a missing `index.db` as a clean install. The journal is removed only
after a completed recovery marker is durable. Snapshot recovery remains
best-effort: the marker records its timestamp and warns that writes since that
snapshot may be absent. Retained capture-buffer JSON can be reconciled with
`persome rebuild-captures-index --merge` when the marker reports a pending
replay. Stop the Runtime before running this offline reconciliation, then start
it again afterward. Recovery mode preserves older snapshot-backed captures
whose buffer JSON has already aged out; the command without `--merge` remains
an exact buffer-only rebuild and deletes such rows.
Unreadable or semantically invalid versioned journals (missing fields, unknown
phases, or paths outside the canonical recovery namespace) are themselves moved
to a timestamped forensic quarantine before normal integrity checks continue;
they do not become permanent startup blockers.

Config replacement has its own earlier intent journal,
`.integrity-config-recovery.pending.json`. Persome writes it before moving a
corrupt `config.toml`, and keeps database replay authority unknown until the
database is verified or restored. A structurally complete `evo_nodes` table in
a snapshot is not by itself proof of evomem authority: it can be a lagging
shadow from a Markdown-authoritative runtime. Recovery compares the snapshot's
last retrieval projection with both surviving sources before replay. If neither
source is uniquely provable, it preserves snapshot entries, snapshot nodes, and
current Markdown, writes `write_authority = "unknown"`, and waits for the owner
to choose. The intent is cleared only after that choice has transactionally
rebuilt the retrieval projection; an evomem choice also reprojects canonical
nodes to Markdown. This prevents a crash-created default from silently choosing
either source.

The running daemon owns active SQLite writes, so `persome status`, MCP clients,
and a second `persome start` do not repair or quarantine an open database.

Runtime startup also fails closed on ambiguous process state. `.daemon.lock`
serializes concurrent starters for the daemon's entire lifetime; `.pid` is
resolved against current-user ownership, executable/command, process start
time, and the generation in `.runtime-state.json` before any signal. Never
manually signal or delete state based only on the numeric PID. Stop the owning
Desktop app or LaunchAgent first when a live Persome process has an invalid
generation receipt.

## Lifecycle and first recall

```bash
persome onboard
persome llm status --check
persome ocr status --check
persome doctor
persome status
persome pause
persome resume
persome stop
```

`persome onboard` leaves the proved Runtime running. A following `persome start`
should report that it is already running; stopping and starting again is not
part of normal installation or update. Use `persome start` only when status says
the Runtime is safely stopped.

## Update the Runtime

```bash
persome update
```

The updater fetches a fresh shallow copy of official `main` instead of editing a
user's checkout. The installer builds a transaction-marked
`venv.replacement.update` while `venv` remains the working old installation.
After stopping either the background daemon or owner LaunchAgent, the updater
atomically exchanges those directories in one same-filesystem kernel operation,
restores the prior owner with the new executable, and runs mode-aware onboarding
against that final generation. Configuration, secrets, capture history, memory,
model state, logs, `ocr_policy`, and lifecycle intent under `PERSOME_ROOT` are
not replaced.

The exchanged old venv remains at `venv.replacement.update` until permission,
OCR-policy, health, owner, and capture/readiness proof all pass. Only then is it
renamed for post-commit cleanup. A failed proof or interruption exchanges the
directories back before restoring and proving the prior Runtime owner. A
transaction marker plus fsynced `preparing`/`prepared`/`activated`/`committing`
state makes crashes on either side of the exchange deterministic to recover.
INT, TERM, and HUP share that recovery path; further signals cannot interrupt
rollback. Concurrent updates are serialized by the owner-only root lock.
Invoking `bash install.sh` from a checkout when Persome is already installed
delegates to `persome update --source <checkout>` so manual reinstalls receive
the same stop, rollback, and post-install proof guarantees.

The updated Runtime reconciles `HUMAN.md` from the current valid Root during
startup/onboarding, so `persome update` backfills existing users without new
capture or model calls. Package-manager users receive the same reconciliation
after `uv tool upgrade --python 3.12 personal-model` followed by `persome onboard`.

`persome update --source /path/to/checkout` is the explicit developer/offline
path. The supplied tree must have the complete Persome source layout; the
updater never pulls or rewrites it.

## Capture diagnostics

Use `persome onboard` for an end-to-end proof. Its capture request runs inside
the active daemon and binds permission, worker, privacy/mode receipt, generation,
and lifecycle ownership.

`persome capture-once` is a lower-level developer diagnostic. It constructs a
new capture provider and scheduler in the calling CLI, so it does not prove the
daemon's event watcher, lifetime lock, generation, owner, privacy receipt, or
isolated OCR-worker readiness. It may also race a running capture scheduler.
Stop Persome before using it to isolate helper output; a successful path is not
an onboarding or release acceptance result.

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
`HUMAN.md` is another raw owner-local inspection surface. Model export and MCP
snapshot export redact by default, and their versioned JSON remains the machine
authority.

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
# Faces, Volumes, Root, HUMAN.md, exports, projections, backups, and recovery markers.
# Captures/timeline remain.
persome clean memory

# Delete all personal data, including captures, timeline/session state, model,
# legacy Chat-era history and skills, exports, backups, logs, and SQLite files.
# Keep config, env, and the installed venv.
persome clean all
```

Both `clean memory` and `clean all` remove the root-level `HUMAN.md` path as
part of an explicit erasure, even when refresh previously preserved it as an
unrecognized user-authored file.

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
persome uninstall cursor                 # project .cursor/mcp.json
persome uninstall cursor --scope user    # ~/.cursor/mcp.json
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

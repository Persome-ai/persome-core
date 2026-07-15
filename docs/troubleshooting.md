# Troubleshooting

Work from symptoms to cause. Each section links to the relevant log file under `~/.persome/logs/`.

Start with the install self-check. It covers an absent/private env file, a
missing selected-provider credential, endpoint reachability, uncompiled Swift
helpers, Accessibility, and the daemon port in one offline pass:

```bash
persome doctor   # ✓/✗/⚠ per prerequisite; exits 1 if anything FAILS; zero LLM calls
```

## Daemon won't start

Persome does not trust a PID number by itself. It checks current-user ownership,
the executable/command shape, process start time, and the random generation in
`.runtime-state.json`, then rechecks that identity immediately before signaling.
It also holds `.daemon.lock` for the complete daemon lifetime so concurrent
`start` calls cannot create two writers.

If `persome start` reports an unsafe Runtime lifecycle state, inspect it without
deleting or signaling from `.pid` alone:

```bash
persome status
cat ~/.persome/.pid
cat ~/.persome/.runtime-state.json | jq .
launchctl print "gui/$(id -u)/com.persome.runtime" 2>/dev/null || true
lsof -nP -iTCP:8742 -sTCP:LISTEN
```

A dead PID or one reused by an unrelated process is safely stale and the next
start can overwrite it. A live Persome-shaped process with an invalid generation
receipt is intentionally ambiguous: stop its owning Desktop app or LaunchAgent,
then retry. Do not `kill $(cat ~/.persome/.pid)` or delete `.pid`/
`.runtime-state.json` while it may be live; that can signal a reused PID or
remove the evidence preventing a second daemon.

Symptom: foreground start immediately exits without error.

```bash
persome start --foreground
```

Read the console output. Common culprits:

- `OSError: [Errno 48] Address already in use` → another process holds port 8742. `lsof -i :8742` to find it.
- A missing selected-provider credential does not prevent capture startup; it
  degrades LLM stages. Run `persome llm setup`, then verify with
  `persome llm status --check`. Key values belong in `~/.persome/env`, never
  `config.toml`.
- `mac-ax-helper` / `mac-ax-watcher` binary missing → run `bash install.sh`.

## Update fails

`persome update` downloads official `main` before stopping the current Runtime,
so a network or Git failure leaves the daemon untouched. If installation fails
after shutdown, the updater attempts to restart the surviving installation (or
restore prior LaunchAgent ownership) and preserves `PERSOME_ROOT` data and
secrets. Fix the reported installer or permission error, then rerun:

```bash
persome update
```

For a reviewed local checkout or an offline repair:

```bash
persome update --source /path/to/personal-model
```

The installer prepares `venv.replacement.update` without touching the active
`venv`. After preparation, the updater stops the current owner and atomically
exchanges the two directories; the old venv remains available at the replacement
path until the new final owner passes mode-aware onboarding. Pressing Ctrl-C
exchanges the old venv back before restarting it, and repeated Ctrl-C cannot
abort rollback. Running `bash install.sh` over an existing installation delegates
to this same transactional local-source path.

If a prior process died, the next `persome update` reads the owner-only
transaction id, phase, and candidate marker and deterministically recovers even
when the crash happened after the atomic exchange but before its phase write.
Do not remove `.update-state.json`, `.update.lock`, or either venv directory by
hand. An invalid live PID/generation receipt also fails closed; stop the owning
Desktop app/LaunchAgent, inspect `.pid` and `.runtime-state.json`, then retry.

## Captures are empty / tree has no content

Most common cause: **Accessibility permission is missing for an actual native
capture principal.** The source-versioned `mac-ax-helper` reads each focused AX
tree, and `mac-ax-watcher` subscribes to events when `event_driven=true`.

```bash
persome onboard
cat ~/.persome/capture-buffer/*.json | jq '.ax_tree | length' | head
```

If the tree is `{}` or tiny across the board, rerun `persome onboard` and follow
the separate prompts for `mac-ax-helper` and `mac-ax-watcher`. Granting only
Terminal, iTerm2, Warp, VS Code, Python, or `persome` does not prove those helper
grants. Same-version reinstalls reuse the exact executable under
`~/.persome/native/<source-digest>/`; if helper source changed, the new digest
path is a new macOS principal and must be granted explicitly. A rollback resolves
the old path and old grant again.

The daemon watcher waits after an initial denial and polls the non-prompting TCC
status. Granting Accessibility while the daemon is still running should restart
event capture automatically within about two seconds. A restart remains a valid
fallback for old installs or a changed TCC principal.

Second most common cause: **`ax_depth` too shallow for Electron apps.** See [capture.md](capture.md#ax-depth-the-1-footgun).

In standard daemon mode on Apple Silicon or Intel, the installer completes Accessibility,
Screen Recording, local OCR, daemon health, and a fresh capture through
`persome onboard`. Other configured modes print their own readiness receipt.
Rerun that end-to-end gate first:

```bash
persome onboard
persome ocr status --check
```

Do not substitute `persome capture-once` for this check. That command creates a
one-shot scheduler in the calling CLI and can diagnose helper output, but it does
not prove the running daemon's watcher, generation, lifecycle owner, privacy
receipt, or OCR-worker readiness. Stop the Runtime before using it as an isolated
developer diagnostic.

If it reports disabled, missing permission, or an incomplete worker setup, run:

```bash
persome ocr setup
persome stop
persome start
persome doctor
```

The setup command opens Screen Recording settings when needed. OCR worker
failures leave the daemon alive. `PERSOME_DISABLE_OCR=1` disables inference
entirely; remove it if health reports `disabled_by_environment`.

If OCR returns after you intentionally disabled it, inspect `ocr_policy` in the
`[capture]` section. `disabled` is the durable opt-out and ordinary
`persome onboard` preserves it. `persome onboard --tier ...` and
`persome ocr setup` are explicit enable actions. `auto` means the install has
not yet recorded an explicit choice. Updates preserve all three states and the
selected tier.

## No event-daily entries appearing

Entries begin landing in `~/.persome/memory/event-YYYY-MM-DD.md` during the
five-minute active-session flush and are finalized at session boundaries.
Silence usually means one of three things.

### 1. No sessions are closing

Check `session.log`:

```bash
tail -30 ~/.persome/logs/session.log
```

If there's a single `session started` line but no `session ended`, the cutter thinks you're still in one session. Normal during continuous work. Force a boundary for debugging by pausing briefly (`persome pause`, wait > `session.gap_minutes`, `resume`).

### 2. Timeline is empty for the session's window

Check `timeline.log`:

```bash
tail -30 ~/.persome/logs/timeline.log
```

If you see window scans but no production, it's usually one of:

- **No captures in the window.** The timeline skips empty windows. Confirm captures exist: `ls ~/.persome/capture-buffer/ | wc -l`.
- **LLM call failing.** Look for `timeline aggregator failed`. Check `[models.timeline]` config.

The reducer handles empty timelines gracefully — it marks the session `reduced` with no entry. If *all* your sessions end up with empty timelines, the aggregator is the root cause.

### 3. Reducer is failing

Check `writer.log`:

```bash
tail -50 ~/.persome/logs/writer.log
```

Look for `reducer failed (retry N/5)` lines. After 5 failed attempts the reducer writes a heuristic entry tagged `heuristic` and marks the session `reduced` — you should *never* see a permanently-stuck session.

The `reducer-retry` task first catches up persisted ended sessions at boot, then
checks due rows every minute using the 5/15/30/60/120 minute schedule. If it is
absent from startup logs, confirm `[reducer] enabled = true` and restart.

Force a catch-up pass:

```bash
persome writer run
```

This runs the same code path the daily 23:55 cron uses.

## Personal model has no Points or Lines

The shipped live writer is `memory_delta`, not the 30-minute classifier. A
successful five-minute active-session flush creates its Point/Line window while
the daemon keeps running. Inspect:

```bash
persome delta-report
persome model status
tail -80 ~/.persome/logs/writer.log
```

Look for `active Point/Line window modeled` and `memory_delta` skip/failure
reasons. Unsupported quotes, unknown
identity references, off-vocabulary predicates, and confidence below the floor
are deliberately dropped. An LLM/store failure leaves `modeled_at` empty so
recovery can retry. An `apply_status=failed` row is resumed without a second
LLM extraction.

Under the default `[memory_delta] apply_enabled = true`, the classifier reports
`classifier retired (delta apply)` and its periodic daemon task is not started.
The following diagnostics apply only if you explicitly set apply off:

Signs it's misbehaving rather than doing its job:

- `classifier ended without commit at iter N` in `writer.log` — the model bailed without calling `commit`. Usually means the stage model is too weak to follow the tool-call protocol. Try a stronger `[models.classifier]`.
- `forbidden: classifier cannot write to event-*` — the classifier tried to write back to an event-daily file. This is always rejected. If every session triggers it, the classifier prompt isn't landing; check that `classifier.md` exists under `src/persome/prompts/`.
- Classifier writes duplicates every session — the stage model is skipping its `search_memory` dedup check. Upgrade the model, don't add code.

## Timeline blocks not appearing

Check `timeline.log`:

```bash
tail -30 ~/.persome/logs/timeline.log
```

If you see window scans but no production, the aggregator window is empty. The fallback heuristic still produces *something*, so total silence means the tick itself isn't firing.

Force a scan:

```bash
persome timeline tick
persome timeline list -n 5
```

## MCP client can't connect

Symptom: Claude Code / Cursor reports the server unreachable.

1. Confirm the installed entry uses owner-local stdio (the secure default):

   ```bash
   claude mcp list | grep persome
   persome mcp
   ```

   Stop the foreground diagnostic with `Ctrl-C`. If the GUI client cannot find
   `persome`, reinstall the entry so it contains an absolute executable path.

2. For an explicitly configured HTTP client only, is the daemon healthy and
   is the authenticated config current?

   ```bash
   persome start
   curl --fail --silent http://127.0.0.1:8742/health
   persome install mcp-json --http --force --filename persome-http.json
   ```

   The generated file is mode `0600` and contains the bearer; never commit or
   share it.

3. Re-register a stale client entry:

   ```bash
   persome install claude-code
   claude mcp list | grep persome
   ```

If `mcp.auto_start = false`, the daemon intentionally will not host HTTP; stdio
installers continue to work.

## A remote MCP client cannot see localhost

The Runtime supports loopback HTTP and local stdio. A cloud-hosted MCP
client cannot reach `127.0.0.1`; exposing Persome through a tunnel changes the
privacy boundary and is not a supported deployment. Use a local
MCP client.

## MCP client connects but doesn't use the memory

Symptom: Claude Code / Cursor / ChatGPT is attached, but when you ask *"when is my interview?"* it says "I don't know" instead of calling `search` or `list_memories`.

Two levers:

1. **Restart the client.** MCP `instructions` and tool descriptions are only re-read on reconnect. After updating Persome, restart the client session.
2. **Nudge once.** Tell the client explicitly: *"check persome for my interview time."* A single prompt usually anchors subsequent turns.

## MCP client answers from compressed memory without drilling into raw captures

Symptom: ask *"what code did I write in main.py at 14:30?"* and the agent paraphrases from the event-daily sub_task (`"edited main.py"`) instead of reading the actual code.

Cause: the agent isn't reaching for the raw-capture layer. Either it stopped at `search` / `read_memory` (compressed), or its session pre-dates the drill-down surface.

Fixes:

1. **Check the reducer is emitting breadcrumbs.** Every sub_task should end with ` — raw: read_recent_capture(at="HH:MM", app_name="…")`. Open today's `event-YYYY-MM-DD.md` and verify. If a line has no breadcrumb, the reducer's output didn't match the canonical `[HH:MM-HH:MM, <app>]` prefix — check `logs/writer.log` for the reduced entry text.
2. **Try `search_captures` directly.** Ask the agent *"search captures for <keyword>"* or *"what's in current_context"*. If those work, the FTS index is healthy and the issue is tool-selection, not retrieval.
3. **Rebuild the captures index if it's empty or out of date.** Run `persome rebuild-captures-index`. Compare `SELECT COUNT(*) FROM captures` against `ls ~/.persome/capture-buffer | wc -l` — they should match modulo one active capture.
4. **Restart the client** after updating Persome — server-level `instructions` (which teach the two-layer model) are only read on reconnect.

## Long session got chopped in half

Symptom: a real 3-hour focused-work session produced two event-daily entries with a mid-session boundary.

Cause: `session.max_session_hours` (default 2) force-cut it. Raise to 4 in `[session]` if this is routine for you. Keep it finite — a runaway session is worse than a clean split.

## Session cuts every few minutes during real work

Symptom: event-daily has many short entries for what was clearly one focused stretch.

Two likely causes:

1. **`session.soft_cut_minutes` too aggressive.** You're single-apping for >3 min (say, a long read in the browser). Raise to 5–10.
2. **`session.gap_minutes` too short.** Idle stretches during thinking are ending sessions. Raise to 8–10.

The frequent-switching exception (≥2 distinct apps in the last 2 min) already defuses the soft cut for multi-app work — if you're still seeing cuts, one of the above two is the knob.

## High CPU / disk from capture

Symptom: laptop fan spinning during capture activity.

Tuning levers, in order:

1. `same_window_dedup_seconds = 15.0` (up from 5.0) — cuts re-capture rate during long typing in a single document.
2. `debounce_seconds = 5.0` (up from 3.0) — batches more keystroke events.
3. `min_capture_gap_seconds = 5.0` — hard-limits capture rate.
4. `include_screenshot = false` — screenshots are the heaviest single cost per capture.
5. `ax_depth = 50` — if you don't need deep Electron content.

Restart the daemon after any `[capture]` change.

## FTS search returns nothing but files exist

Cause: the index drifted from disk (manual edits without `rebuild-index`, power loss mid-write, bug).

Fix:

```bash
persome rebuild-index
```

Under default Markdown authority this rewrites `entries`, `files`, and
`entries_fts` from disk. Under evomem authority, project from evomem instead;
direct Markdown edits are not truth.

## `/model` is empty or incomplete

The viewer only exists while the daemon's HTTP MCP task is active. Confirm
`[mcp] auto_start = true`, run `persome model open`, then run:

```bash
persome model build
persome model status
```

A new store can truthfully have Points but no Face, Volume, or Root. Those
levels require repeated stable evidence and model status should remain
`degraded`; the viewer does not invent missing geometry. Check
`model-build.json` for failed stages and `/model/graph` for the exact snapshot
currently consumed by the viewer.

After automatic database quarantine, inspect
`~/.persome/.integrity-recovery.json`. Persome restores a verified snapshot when
available and otherwise replays the surviving Markdown memory projection, but
it deliberately marks the structural model as not built. Run `persome model
build` once the Runtime LLM profile passes `persome llm status --check`; the
Viewer will not label an unverified projection as `Build complete`.
If config and database recovery leave
`.integrity-config-recovery.pending.json` and `write_authority = "unknown"`,
both Markdown and evomem were intact but neither was provably canonical. Inspect
the retained snapshot/quarantine, then explicitly set the value to `markdown`
or `evomem` and rerun a stopped-Runtime command. Persome reconciles the selected
source before unfreezing; choosing evomem also replaces its conflicting
Markdown projection.
If the marker says `capture_buffer_replay_available: true`, run `persome
rebuild-captures-index --merge` to upsert retained owner-only JSON without
deleting older snapshot-backed captures before rebuilding downstream history.

## Resetting

Start from scratch without reinstalling:

```bash
persome stop
persome clean all -y        # keeps config.toml
persome start
```

Full nuke including config:

```bash
persome stop
rm -rf ~/.persome
persome start                # recreates config.toml with defaults
```

## Getting more signal

```bash
tail -F ~/.persome/logs/*.log
```

All sinks in one terminal. When in doubt, run `start --foreground` and keep this tail open in another pane.

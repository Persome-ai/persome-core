# Capture

Capture is the only layer that touches the outside world. It produces one JSON file per observation into `~/.persome/capture-buffer/`; nothing above it ever talks to macOS directly.

In `source="daemon"` mode, live capture requires Accessibility for the native
`mac-ax-helper` and, when event-driven capture is enabled, the separate
`mac-ax-watcher` executable. The terminal and Python daemon do not read AX on
their behalf. Screen Recording is additionally required when screenshot storage
or effective OCR needs pixels. In a standard interactive Apple Silicon install,
`persome onboard` explains and requests each required principal separately,
verifies bundled OCR, starts the final daemon owner, checks local health, and
proves one fresh capture inside that daemon before returning success. The
authenticated `/permissions` endpoint runs the actual helper/watcher trust
checks plus the Runtime's Screen Recording preflight. With HTTP auto-start
disabled, the same generation publishes its permission, worker, phase, and
capture receipts to owner-only `.runtime-state.json`.

In `source="ingest"` mode a trusted local producer owns macOS AX and pixel
permissions and sends bearer-authenticated frames to `/captures/ingest`; the
daemon starts no OS watcher. Onboarding proves that ingest runner is ready
instead of manufacturing a daemon AX capture. This mode requires an enabled
HTTP transport because the authenticated endpoint is its only input channel.

## Two signal sources

The following two sources apply to daemon capture mode.

**`mac-ax-watcher`** (primary, event-driven). A vendored Swift binary that subscribes to AX notifications across all running apps: window focus, value changes (typing), title changes, app activation. It emits one JSON object per event on stdout. The Python side reads that stream line-by-line in `capture/watcher.py` → `capture/event_dispatcher.py`.

**Heartbeat timer** (fallback). Every `heartbeat_minutes` (default 10), the scheduler fires a capture even if no event arrived — so long idle periods leave a trail. Set `heartbeat_minutes = 0` to disable entirely (watcher-only); values `>0` are clamped to a 60-second floor.

Both funnel into `capture_once` in `capture/scheduler.py`, which runs:

1. `ax_capture.capture_frontmost(focused_window_only=True)` — one-shot invocation of `mac-ax-helper` for the current window, pruned to `ax_depth` layers.
2. `s1_parser.enrich()` — extracts `focused_element`, `visible_text`, and `url` from the AX tree (see [S1 fields](#s1-fields) below).
3. `cmux_source.maybe_inject()` — when the frontmost bundle is cmux, appends the real terminal text read over cmux's local socket RPC (see [cmux signal source](#cmux-signal-source) below); a successful injection skips step 4's OCR fallback for this window.
4. OCR fallback — when the AX render produced no usable content and `enable_ocr_fallback` is on, submit a focused-window screenshot to an isolated local worker.
5. `screenshot.grab()` — unless `include_screenshot = false`.
6. `window_meta.active_window()` — app name, title, bundle_id via `NSRunningApplication`.
7. Write `{iso8601_safe}.json` to the buffer.

The filename is ISO-8601 with `:` → `-` and `+` → `p` / `-` → `m` for the TZ offset. Example: `2026-04-21T17-07-32p08-00.json`.

The same capture scheduler also invokes `SessionManager.on_event` (wired as a `pre_capture_hook` in `daemon.py`), so the session cutter sees every written, non-duplicate observation without a separate subscription path.

## Local OCR fallback

The installer runs `persome onboard`, whose OCR step checks the native Runtime
and bundled weights, requests Screen Recording after an explicit explanation,
persists enabled intent, then starts the daemon and waits for the daemon-owned
isolated worker to initialize. `[capture].ocr_policy` distinguishes `auto`
(fresh/unconfigured), `enabled`, and `disabled` (explicit opt-out). Ordinary
onboarding and every update preserve an explicit policy and selected tier;
`persome onboard --tier ...` and `persome ocr setup` explicitly enable OCR,
while `persome ocr disable` records disabled intent. The standalone setup repair
command keeps its own explicit worker check. The
focused screenshot is used locally and is never placed in an LLM prompt. The
OCR path is:

```text
focused screenshot bytes
  -> local OCR worker subprocess
  -> text + geometry
  -> app-aware structuring when available
  -> captures FTS backfill
  -> timeline/modeling fallback when AX text is empty
```

The subprocess is the native-crash boundary: a Paddle fault fails the OCR call
without killing the daemon. `PERSOME_DISABLE_OCR=1` prevents Paddle from being
loaded at all. `PERSOME_OCR_IN_PROCESS=1` exists only for debugging and removes
that isolation.

The helper and watcher are compiled into immutable
`<PERSOME_ROOT>/native/<source-digest>/` directories keyed by a format version,
architecture, and Swift source bytes. A same-version reinstall returns the exact
existing executables, preserving their macOS TCC identity. Changed helper source
uses a new digest path and therefore requires a deliberate new Accessibility
grant; rollback resolves the old wheel source and old binary again.

```bash
persome onboard            # permissions + OCR + daemon + health + fresh capture
persome ocr setup          # enable, request permission, verify worker
persome ocr status         # quick config/runtime/model/TCC state
persome ocr status --check # also start and verify the worker engine
persome ocr disable        # explicit opt-out; restart to apply
```

## Debounce / dedup / gap

Four time-based knobs throttle the event firehose (`capture/event_dispatcher.py`):

| Knob | Default | What it does |
|---|---|---|
| `debounce_seconds` | 3.0 | `AXValueChanged` events within this window collapse — only the last triggers a capture. Prevents one-capture-per-keystroke during typing. |
| `dedup_interval_seconds` | 1.0 | Same `(event_type, app)` pair within this window is dropped outright. |
| `min_capture_gap_seconds` | 2.0 | Hard floor between consecutive `capture_once` calls, regardless of event reason. |
| `same_window_dedup_seconds` | 5.0 | Non-focus-change events in the same `(bundle_id, title)` pair collapse within this window. Focus changes always bypass it. |

Tune these if you see `capture.log` flooded; the defaults produce a few hundred captures per work-day, comfortably under the buffer retention.

### Content dedup (no time window)

On top of the time-based knobs, the scheduler compares each built capture against the previous one by a content fingerprint (`hash(bundle + title + focused_element.value + visible_text + url)`, in `capture/scheduler.py`). If the fingerprint matches, the capture is **not** written and the session manager's `pre_capture_hook` is **not** fired.

This catches the case the time knobs can't: a screen that doesn't change (lock screen overnight, a paused video, an idle IDE) keeps generating AX events with the same content indefinitely. Without content-dedup those would both fill the buffer and keep the current session from ever idling out. Timestamps, triggers, and screenshots are excluded from the fingerprint so only meaningful changes count.

## cmux signal source

cmux (`com.cmuxterm.app`) renders terminals on the GPU; its AX tree carries only window chrome (workspace tab titles, buttons — spike #558 measured ~30 chars median of content per app subtree). Instead of OCR, `capture/cmux_source.py` talks to cmux's local unix-socket RPC (`~/Library/Application Support/cmux/cmux-<uid>.sock`, newline-delimited JSON — the same protocol the official `cmux read-screen` CLI uses):

1. `system.tree` — visible windows → selected workspace → panes → selected surfaces.
2. `surface.read_text` with `surface_id` (the UUID; the `surface`/`surface_ref` param spellings are **ignored** by the server and silently fall back to the focused surface) for each visible terminal surface. Browser/filepreview surfaces and unselected tabs are skipped.
3. The texts are appended to the capture's `visible_text` under `### [cmux terminal] <workspace · surface>` section headers, and `cmux_text_injected: true` is set on the capture.

Downstream stages (timeline, `focus_excerpt`, `captures_fts`) consume `visible_text` as-is — there is no cmux-specific path beyond this injection. Discipline: the whole socket conversation shares one sub-second deadline; per-surface budget 6 k chars (tail-kept), total 12 k; a single bad surface (tree/type drift, e.g. "Surface is not a terminal") is skipped without aborting the rest; any other failure degrades silently to the AX-only capture with a rate-limited warning. Successful injection skips the OCR fallback for that window. Gate: `[capture] cmux_source_enabled` (default on).

Privacy note: terminal text can contain secrets echoed on screen. There is no general `visible_text` redaction layer in the capture pipeline today; helper-level `[REDACTED]` handling only covers AX password inputs. Terminal text therefore carries the same risk profile as other screen full text.

## AX depth — the #1 footgun

AX Trees for native Cocoa apps are shallow (5–15 layers). Electron apps (Claude Desktop, VS Code, Slack, Notion) nest user content 20–60 layers deep under chrome.

**Default `ax_depth = 100`** was chosen after diagnosing silent capture misses: a 90-second Claude Desktop conversation about an interview at 18:00 was producing captures where "18:00" appeared at character 5639 of the tree — past any reasonable prune limit. At depth 8, the tree contained only window chrome and sidebar headers; at depth 100, the full conversation was there.

If you're running on limited hardware and only care about native apps, lowering to 30 is safe. Don't go below 20.

Diagnostic:

```bash
./resources/mac-ax-helper --app-name Claude --depth 30 --raw | wc -c
# vs.
./resources/mac-ax-helper --app-name Claude --depth 100 --raw | wc -c
```

A 10×+ ratio means there's content past depth 30 you'd miss.

## What's in a capture file

```json
{
  "timestamp": "2026-04-21T09:07:32.123456+00:00",
  "schema_version": 2,
  "trigger": { "event_type": "window_focus_changed", "app": "Claude", ... },
  "window_meta": {
    "app_name": "Claude",
    "bundle_id": "com.anthropic.claudefordesktop",
    "title": "New conversation — Claude"
  },
  "focused_element": {
    "role": "AXTextArea",
    "title": "Message composer",
    "value": "I have an interview at 18:00",
    "is_editable": true,
    "value_length": 30
  },
  "visible_text": "### New conversation — Claude\n...",
  "url": null,
  "ax_tree": { ... pruned tree with roles, titles, values ... },
  "ax_metadata": { ... },
  "screenshot": {
    "image_base64": "iVBORw0KGgoAAAANS...",
    "mime_type": "image/jpeg",
    "width": 1920,
    "height": 1200
  }
}
```

`trigger` is `{"event_type": "heartbeat"}` for timer captures and `{"event_type": "manual"}` for `capture-once`. Screenshot is omitted entirely when `include_screenshot = false`.

Secure fields (password inputs) are replaced with `"[REDACTED]"` at the helper level — the Python side never sees them.

## S1 fields

Ported from Einsia-Partner's `s1_collector`. These are what downstream LLM stages consume — the raw `ax_tree` is kept only for future vision-model support and debugging.

- **`focused_element`** — `{role, title, value, is_editable, value_length}` for the currently focused AX element. This is the user's cursor context: what they're typing into, which sidebar row is selected, etc.
- **`visible_text`** — a length-capped markdown rendering of the AX tree (up to ~10 k chars). What the user is currently reading on screen.
- **`url`** — regex-extracted from `visible_text` when present; `null` otherwise.

Editable values are placeholder-cleaned before they enter S1. The native AX
helper's focused-element projection first uses `AXPlaceholderValue`, then a
bounded fallback for Chromium's exact `.placeholder` descendant shape. The raw
tree retains that evidence; the Python S1 projection applies the same structural
check to the tree, older helpers, watcher triggers, and trusted-ingest
producers. An exact placeholder descendant is removed only inside its owning
editable subtree and only when its text is locally paired with that control.
Matching text is then cleared from the parent projection. Ordinary page text
and broad CSS classes that merely contain the word `placeholder` remain intact.
Historical timeline/MCP
reads and `rebuild-captures-index` apply the same sanitizer, so replaying an old
buffer cannot turn input hints into authored text; index rebuilds also preserve
DB-only OCR backfills. Explicit `--raw` native captures retain the diagnostic AX
structure.

Persisted screenshots are **not** passed to timeline, reducer, memory-delta, or
schema prompts. They support optional local provenance drill-down and debugging.
When `encrypt_screenshots=true`, `PERSOME_SCREENSHOT_KEY` seals them with
AES-256-GCM. `install.sh` generates this machine-local key automatically and
preserves it across reinstalls. If the key is absent or malformed, persistence
fails closed by omitting pixels while retaining AX text and metadata. Set
`include_screenshot=false` when persistent pixels are not required; OCR can
still take an ephemeral focused screenshot when enabled.

## Buffer hygiene — tiered retention

Captures are pruned by the timeline tick, not the writer. After each timeline scan, `capture_scheduler.cleanup_buffer` applies three age-based passes (oldest-safe-first), gated on "this file has already been absorbed by a closed timeline block":

| Pass | Condition | Action |
|---|---|---|
| **Delete** | mtime older than `buffer_retention_hours` (default **168** = 7 days) | Whole JSON removed |
| **Strip screenshot** | mtime older than `screenshot_retention_hours` (default **24**) | Rewrite JSON without `screenshot` field; sets `screenshot_stripped: true`. The AX tree, `visible_text`, `focused_element`, and `url` stay |
| **Evict by size** | Total buffer > `buffer_max_mb` (default **2000**, i.e. 2 GB; `0` disables) | Delete oldest files until under the cap |

The separate `buffer_max_mb` limit is a hard disk-safety boundary. It evicts
oldest captures even when the reducer watermark has stalled; this can sacrifice
an unabsorbed trailing frame, but prevents an ingest or reducer failure from
growing the buffer without bound. Ingest timestamps more than five minutes in
the future are replaced with the server clock so they cannot evade ordering and
retention. Accepted timestamps are normalized to UTC with fixed-width
microseconds, preventing same-second ID collisions. Upgrade paths can still
contain older local-offset filenames, so search, timeline, and retention compare
their parsed instants rather than raw strings; daylight-saving fall-back cannot
reverse processing order. Atomic-write remnants from a crashed process have no
recovery contract and are removed after a five-minute race-safety grace period.

Why tiered: the screenshot base64 is ~77% of each capture's bytes and is not
needed to build the durable model. Stripping it at 24h drops each stale capture
to ~20% of its original size while preserving AX/OCR evidence for local search.

To wipe manually:

```bash
persome clean captures
```

## Search index — `captures_fts`

Every successful capture write is also indexed into an FTS5 virtual table (`captures_fts`, backed by a `captures` content table — see `src/persome/store/fts.py`). This is what powers the MCP `search_captures` and `current_context` tools, which let LLM clients reach the raw screen content directly without having to scan JSON files on disk.

**Lifecycle.**

| Event | Effect on index |
|---|---|
| `_write_capture` (write-through) | Upsert one row into `captures` (`INSERT OR REPLACE` on the file stem). Triggers keep `captures_fts` in sync. |
| `cleanup_buffer` time-based delete | FTS deletion is attempted before each JSON deletion; filesystem erasure remains authoritative if SQLite is temporarily unavailable, and the final reconciliation removes stale searchable rows after recovery. |
| `cleanup_buffer` size-based eviction | Same, including hard-cap eviction of unabsorbed files when necessary and continued eviction after one unlink failure. |
| Screenshot strip | **Untouched.** Strip only removes the base64 image; the indexed text (`visible_text`, `focused_value`, `window_title`, `app_name`, `url`) is unchanged. |
| `persome rebuild-captures-index` | Clear stale rows, then index every surviving `~/.persome/capture-buffer/*.json`. Use `--merge` after snapshot recovery to preserve older rows whose JSON aged out. |

**Indexed columns.** Only the searchable text is in FTS: `app_name`, `window_title`, `focused_value`, `visible_text`, `url`. Filterable metadata (timestamp, bundle_id, focused_role) lives on the `captures` table for `WHERE`-clause filtering. Screenshots are deliberately not duplicated — the JSON file on disk stays the authoritative copy of the raw image bytes.

**Tokenizer.** `unicode61 remove_diacritics 2` — case-insensitive, accent-folded, Unicode-aware. Same setup as the compressed-memory `entries` index.

If `captures_fts` falls out of sync (e.g. capture worker crashed mid-write, or the daemon was killed during cleanup), the index is recoverable in one shot. Rebuild first clears stale rows, then indexes every surviving JSON file:

```bash
persome rebuild-captures-index
```

## Pause

```bash
persome pause
```

Drops a `~/.persome/.paused` sentinel. The watcher keeps streaming but `capture_once` short-circuits on sentinel presence. `resume` removes the sentinel.

## Smoke test

```bash
persome capture-once
```

This is a low-level developer diagnostic. It creates a provider and scheduler in
the calling CLI, writes one immediate capture, and can confirm the one-shot
helper returns useful AX content. It does **not** run through the active daemon's
capture runner, prove the event watcher, bind a Runtime generation or lifecycle
owner, validate a privacy/mode receipt, or wait for the daemon-owned OCR worker.
It may race the running scheduler, so stop Persome before using it to isolate a
helper problem. Use `persome onboard` for onboarding, update, and release proof.

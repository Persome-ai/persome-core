# Security and privacy model

persome-core observes screen context. Treat its data root and local APIs as
sensitive personal data, even when the model snapshot is redacted.

## Local data

The default data root is `~/.persome` and can be redirected with
`PERSOME_ROOT`.

| Data | Location | Notes |
|---|---|---|
| capture records | `capture-buffer/` | AX text and optional screenshot payloads |
| durable memory | `memory/*.md` | readable facts and schemas |
| indexes/model | `index.db` | SQLite WAL, FTS5, vectors, provenance, geometry |
| provider/local API secrets | `env` | dotenv file, mode `0600` |
| build metadata | `model-build.json` | hashes/IDs, no API keys |
| human-readable model | `HUMAN.md` | raw deterministic projection, mode `0600`; not redacted for sharing |
| exported model | `exports/*.json` | redacted by default, mode `0600` |
| Runtime receipts | `.runtime-state.json`, `.update-state.json` | owner-only generation, policy, and transaction metadata |
| native AX binaries | `native/<source-digest>/` | immutable machine-local permission principals |

The Runtime enforces mode `0700` on the data root and personal-data
directories, and mode `0600` on databases, capture records, logs, snapshots,
and other sensitive files. The first start after upgrading repairs legacy
group/world-readable modes. The launchd job also runs with umask `0077` so new
artifacts are private from creation.

Default capture retention is seven days. Screenshot payloads are stripped
earlier by the configured screenshot retention window, except explicitly
actionable captures covered by extended retention. On supported Apple Silicon
installs, explicit onboarding can enable OCR after requesting Screen Recording
and proving the isolated worker can load bundled PP-OCRv6 weights. Inference
remains local. `[capture].ocr_policy` records `auto`, explicit `enabled`, or
explicit `disabled` intent; ordinary onboarding and updates preserve explicit
intent and tier. `persome ocr disable` is the durable opt-out.

Lock-screen detection is privacy-conservative: when both macOS probes are
unavailable or error, capture pauses until a probe can establish that the
session is unlocked.

`install.sh` generates a machine-local `PERSOME_SCREENSHOT_KEY` and preserves it
across reinstalls. When `encrypt_screenshots=true`, a missing or malformed key
fails closed: the Runtime keeps AX text and metadata but omits persistent pixels
instead of writing a plaintext screenshot. OCR can still use an ephemeral
screenshot when persistent screenshot storage is off.

The Runtime requires SQLite 3.42 or newer. It enables both SQLite core
`secure_delete` and FTS5's persistent `secure-delete` option so deleted memory
and capture terms are removed from ordinary pages and full-text shadow indexes.
On the first open after this security upgrade it also rebuilds both FTS indexes
from live rows and vacuums the database, removing segment terms left by deletes
performed by older releases. The rebuild commits atomically with its recorded
milestone exactly once, and the follow-up vacuum/WAL truncation retries on
later opens (or the daily checkpoint) if concurrent readers keep the database
busy — a busy database defers the one-time cleanup instead of refusing
connections.

## macOS permission principals

In daemon capture mode, Accessibility is granted to the native executable that
uses the API: `mac-ax-helper` for focused-tree reads and, when event-driven
capture is enabled, `mac-ax-watcher` for notifications. Granting the terminal,
Python daemon, or launchd job is not a substitute. Onboarding explains, requests,
and live-probes each required native principal separately. Screen Recording is
preflighted by the Runtime process that obtains pixels. In trusted-ingest mode,
the producer owns those macOS permissions and Persome proves authenticated
ingest readiness rather than claiming a daemon grant.

The wheel ships helper source. Persome derives a path from a format version,
architecture, and source-byte digest, compiles once under
`<PERSOME_ROOT>/native/<source-digest>/`, and publishes the executable with an
atomic rename. A same-version reinstall reuses the exact binary and macOS grant.
Changing helper source intentionally creates a new principal that requires an
explicit grant. Rolling back runs the prior source version and resolves the old
binary again; no updater overwrites an existing digest path.

## Runtime and update integrity

The daemon holds `.daemon.lock` for its entire lifetime. Lifecycle code treats
`.pid` as compatibility metadata, not authority: current-user ownership,
executable/command, process start time, and the random generation in the
owner-only `.runtime-state.json` are verified and rechecked immediately before a
signal. A live Persome-shaped process with ambiguous generation fails closed so
a second writer cannot start. LaunchAgent ownership additionally binds the
owner marker, loaded job program/PID, configured plist, and Runtime receipt.

Updates hold one owner-only root lock and build an inactive, transaction-marked
candidate while the active virtualenv remains untouched. Activation exchanges
the two same-filesystem directories in one kernel operation. The old venv stays
available until the new final owner passes mode-aware permission, OCR-policy,
health, and capture/readiness proof. Fsynced phase metadata and the candidate
marker make a crash immediately before or after exchange recoverable; rollback
atomically exchanges the old Runtime back before restoring its lifecycle owner.

## Network egress

There is no telemetry or update phone-home. Runtime egress occurs only through
configured capabilities:

1. The endpoint selected under `[models.default]` receives prompts for enabled
   LLM stages over Anthropic Messages or OpenAI-compatible Chat Completions.
   Stage prompts can contain derived personal context, including raw memory
   text, screen text, window titles, URLs, focused-field values, and timeline
   blocks.
2. `OPENAI_BASE_URL` receives embedding inputs when hybrid dense retrieval is
   enabled and a provider is configured.

Capture and BM25 retrieval work without provider credentials. LLM-dependent
model stages report degradation rather than silently claiming success.

## Local API boundary

- REST and streamable HTTP MCP are restricted to loopback (`127.0.0.1` by default).
- Browser Host and Origin guards reject non-loopback model access.
- `install.sh` and `persome start` provision a dedicated high-entropy
  `PERSOME_LOCAL_API_TOKEN`. Every REST, viewer, and HTTP MCP route
  requires `Authorization: Bearer ...`; only canonical `GET /health` and the
  single-use browser capability exchange are public.
- `persome model open` exchanges the long-lived bearer for a 60-second,
  one-use URL and an HttpOnly, SameSite=Strict cookie scoped to a fresh,
  unguessable `/model/<session>/` path.
  Protected responses use `Cache-Control: no-store`.
- Local clients installed by Persome use stdio by default, so no bearer is
  copied into their configuration. Explicit `install mcp-json --http` writes
  an authenticated owner-only file that must not be committed or shared.
- `/captures/ingest` is a trusted local producer interface, not a public upload
  endpoint.
- MCP tool execution itself has no provider egress, but a connected agent may
  send returned personal data to its own model provider.
- `/model/graph` is a raw owner-local inspection surface. Default CLI/MCP model
  export is redacted; the browser viewer is not a safe publication artifact.
- `HUMAN.md` is also a raw owner-local inspection surface, despite its readable
  format and `0600` mode. Do not publish or attach it as though it were a
  redacted export.
- Wildcard and LAN binds are rejected even when a bearer is configured because
  the Runtime does not terminate TLS. Exposing it through a tunnel changes the
  privacy boundary and is not a supported deployment.

## Agent safety

Captured screen text and memory are untrusted data. They may contain prompt
injection or malicious instructions. MCP consumers must keep data and control
channels separate and must not execute instructions merely because they appear
in a capture or memory result.

The Runtime exposes no click, type, takeover, meeting-audio, notification, or
task-execution tools. Its MCP writes are limited to explicit `remember` and
`correct_memory` operations. Model-generated `memory/skills` files remain
untrusted data; the Runtime never promotes them into instructions or
executable tools.

## Corrections and revocation

`persome correct` and MCP `correct_memory` supersede, retype, merge, or revoke a
belief through the model's correction path. Previous states keep receipts so a
change is auditable and reversible. Rebuild operations derive current indexes
from the selected write authority; they do not erase provenance history.

For irreversible deletion, stop the daemon and run `persome clean memory` or
`persome clean all`. The memory command also removes canonical evomem state,
relations, geometry, every file under `memory/` (including interrupted atomic
writes), the generated `HUMAN.md`, exports, projections, backups, and recovery
markers. The all command also removes `HUMAN.md`, captures, timeline/session
state, legacy Chat-era history and skills from older releases, logs, and SQLite
files while preserving config, env, and the installed virtualenv. See
[operations and data control](docs/operations.md).
These explicit erasure commands remove the root-level `HUMAN.md` path even if
automatic refresh had preserved it as an unrecognized user-authored file.

`persome clean captures` and `persome clean timeline` scrub the same tables
from retained SQLite snapshots, unfinished `.tmp` snapshots, and integrity
quarantine copies. Journals and orphan sidecars are removed. Explicit clean
operations enable SQLite/FTS secure deletion, compact free pages, and truncate
the WAL; a recovery copy that cannot be reliably scrubbed is removed rather
than silently retaining the requested data. All clean commands refuse to run
while the verified Runtime identity or its lifetime lock is live.

## Export caveat

Default snapshot export removes detectable secrets, PII categories, and local
paths. It does not guarantee that every person, organization, project, or
writing style is anonymous. Never publish a real snapshot without informed
consent and a separate anonymization review. Committed fixtures in this
repository are synthetic and pass the PII gate.

## Threat model

| Threat | Mitigation and residual risk |
|---|---|
| Other local OS account/process | Owner-only storage plus bearer authentication prevents port access from becoming personal-data access; browser viewer cookies are additionally scoped to an unguessable per-session path because cookies do not isolate localhost ports. |
| Same-user malicious process | Out of isolation scope; it can read owner credentials and files. |
| Provider exfiltration | User chooses endpoints; no-key capture/BM25 mode remains available. |
| Prompt injection | Generated memory is served as data, never as instructions or executable tools. Consuming MCP clients must still enforce their own tool policy. |
| Malicious MCP client | Bearer/stdio access is an explicit personal-data capability; connect only trusted clients. |
| Supply chain | Locked dependencies and the full build-backend closure are pinned; installer fallback downloads are checksum-verified; wheel smoke tests inspect bundled Swift sources, viewer assets, and OCR weights outside the checkout; Actions use immutable SHAs and least privilege; releases made by the current workflow are checksummed and attested from an administrator-protected tag reachable from `main`. |
| Accidental publication | Synthetic fixtures, PII scan, default redaction, and owner-only export permissions. |

Vulnerability reporting instructions are in [SECURITY.md](SECURITY.md).

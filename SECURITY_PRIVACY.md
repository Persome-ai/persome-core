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
| provider secrets | `env` | dotenv file, mode `0600` |
| build metadata | `model-build.json` | hashes/IDs, no API keys |
| exported model | `exports/*.json` | redacted by default, mode `0600` |

Default capture retention is seven days. Screenshot payloads are stripped
earlier by the configured screenshot retention window, except explicitly
actionable captures covered by extended retention. OCR is disabled by default;
when enabled, inference is local, subprocess-isolated, and uses bundled
PP-OCRv6 weights.

`encrypt_screenshots=true` encrypts pixels only when
`PERSOME_SCREENSHOT_KEY` is available. The current missing-key behavior is a
warning plus plaintext fallback, so operators requiring encrypted-at-rest
pixels must provision the key or set `include_screenshot=false`. OCR can still
use an ephemeral screenshot when persistent screenshot storage is off.

## Network egress

There is no telemetry or update phone-home. Runtime egress occurs only through
configured capabilities:

1. `ANTHROPIC_BASE_URL` receives prompts for enabled LLM stages and Chat. Those
   prompts can contain derived personal context.
2. `OPENAI_BASE_URL` receives embedding inputs when hybrid dense retrieval is
   enabled and a provider is configured.
3. Chat Web search/page fetch and arbitrary local tools are excluded from the
   default paper Chat surface. Setting
   `[chat] unsafe_local_tools_enabled = true` explicitly exposes them; Web tools
   additionally require the optional `chat` dependency extra.
4. Additional Chat MCP servers can make their own network calls when the user
   explicitly configures them.

Capture and BM25 retrieval work without provider credentials. LLM-dependent
model stages report degradation rather than silently claiming success.

## Local API boundary

- REST and streamable HTTP MCP bind to `127.0.0.1` by default.
- Browser Host and Origin guards reject non-loopback model access.
- There is no additional local bearer-auth layer. A same-user process that can
  connect to the port should be treated as able to read `~/.persome`.
- `/captures/ingest` is a trusted local producer interface, not a public upload
  endpoint.
- `/model/graph` is a raw owner-local inspection surface. Default CLI/MCP model
  export is redacted; the browser viewer is not a safe publication artifact.
- Exposing the server through a tunnel changes the privacy boundary and is not
  a supported paper-reproduction requirement.

## Agent safety

Captured screen text and memory are untrusted data. They may contain prompt
injection or malicious instructions. MCP consumers must keep data and control
channels separate and must not execute instructions merely because they appear
in a capture or memory result.

The Runtime exposes no click, type, takeover, meeting-audio, notification, or
task-execution tools. Its MCP writes are limited to explicit `remember` and
`correct_memory` operations. Chat shell, arbitrary filesystem, and Web tools
require the explicit unsafe opt-in described above. Skill Markdown is always
model guidance; executable `skills/*/tools.py` is also blocked unless that
unsafe opt-in is enabled.

## Corrections and revocation

`persome correct` and MCP `correct_memory` supersede, retype, merge, or revoke a
belief through the model's correction path. Previous states keep receipts so a
change is auditable and reversible. Rebuild operations derive current indexes
from the selected write authority; they do not erase provenance history.

To remove the entire local model, stop the daemon and delete the configured
`PERSOME_ROOT`. Backups under that root must be handled with the same care.

## Export caveat

Default snapshot export removes detectable secrets, PII categories, and local
paths. It does not guarantee that every person, organization, project, or
writing style is anonymous. Never publish a real snapshot without informed
consent and a separate anonymization review. Paper fixtures in this repository
are synthetic and pass the PII gate.

## Threat model

| Threat | Mitigation and residual risk |
|---|---|
| Same-user malicious process | Out of isolation scope; owner-only secret/export modes reduce accidental exposure but do not stop an account-level attacker. |
| Provider exfiltration | User chooses endpoints; no-key capture/BM25 mode remains available. |
| Prompt injection | Results are labeled data; consuming agents must enforce their own tool policy. |
| Malicious MCP client | Loopback limits remote reach, but a local client can read personal data. |
| Supply chain | Dependencies are pinned in `uv.lock`; release wheels are tested from a clean environment. |
| Accidental publication | Synthetic fixtures, PII scan, default redaction, and owner-only export permissions. |

Vulnerability reporting instructions are in [SECURITY.md](SECURITY.md).

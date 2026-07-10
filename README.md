# Persome

**The local-first Personal Model Runtime for macOS.** Persome observes the apps
you already use, turns cross-app activity into an inspectable model of a real
person, and serves that model to Chat and MCP agents.

[![CI](https://github.com/Persome-ai/persome-core/actions/workflows/ci.yml/badge.svg)](https://github.com/Persome-ai/persome-core/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/Persome-ai/persome-core)](https://github.com/Persome-ai/persome-core/releases)
[![License: Apache-2.0](https://img.shields.io/badge/code-Apache--2.0-blue)](LICENSE)
[![macOS 13+](https://img.shields.io/badge/macOS-13%2B-black)](#platform-support)
[![MCP](https://img.shields.io/badge/interface-MCP-0b7285)](MCP.md)

![Persome's local personal-model viewer showing synthetic Points, Lines, Faces, a Volume, and Root](docs/assets/persome-model-hero.png)

_Actual `/model` screenshot produced by `scripts/sample_demo.py`: 4 synthetic
Points, 2 Lines, 2 Faces, 1 Volume, and 1 Root. It contains no personal data._

## Product job

Persome runs quietly on one Mac and does four jobs:

1. **Collect** focused macOS Accessibility (AX) context across apps, with an
   optional on-device OCR fallback for AX-poor surfaces.
2. **Model** observations into sourced facts, evolving relations, stable
   patterns, cross-domain structure, and one current Root.
3. **Serve** local memory and model tools over MCP, plus an optional terminal
   Chat that uses the same tools.
4. **Give control back** through receipts, time travel, correction, export, and
   deletion.

This is the Runtime, not a hosted account or a single assistant's private
memory. One local model can be used by Claude Code, Codex, Cursor, or another
trusted MCP client.

## Five-minute sample demo

See the whole model without an API key, Accessibility permission, or access to
your real `~/.persome` data. This path requires Git and
[`uv`](https://docs.astral.sh/uv/getting-started/installation/):

```bash
git clone https://github.com/Persome-ai/persome-core.git
cd persome-core
uv run python scripts/sample_demo.py
```

The script opens `http://127.0.0.1:8743/model`, serves MCP at
`http://127.0.0.1:8743/mcp`, and deletes its temporary synthetic data when you
press `Ctrl-C`. To inspect the exact search, receipt, and snapshot payloads:

```bash
PERSOME_LLM_MOCK=1 uv run python scripts/sample_demo.py --json
```

With the sample server still running, verify the actual MCP transport from a
second terminal:

```bash
uv run python scripts/verify_sample_mcp.py
```

This sample path is deliberately separate from the real-data path below.

## Quick start with your data

Requirements: macOS 13 or newer and Xcode Command Line Tools. The installer
finds or installs `uv`, provisions Python 3.11-3.13, compiles the Swift AX
helpers, generates the local screenshot-encryption key, and offers to register
detected MCP clients.

```bash
git clone https://github.com/Persome-ai/persome-core.git
cd persome-core
bash install.sh

persome doctor
persome start
open http://127.0.0.1:8742/model
```

Grant **Accessibility** to the terminal or app that launches Persome in
**System Settings -> Privacy & Security -> Accessibility**. This permission is
required to read focused AX text and structure. Grant **Screen Recording** only
when enabling OCR fallback or screenshot retention; it supplies pixels to the
local OCR worker. Persome does not require Full Disk Access.

An LLM key is optional for collection and BM25 recall, but required for real
semantic modeling. `install.sh` can save an Anthropic key or an
Anthropic-compatible gateway key to the owner-only `~/.persome/env` file.
Nothing ships with a key.

```bash
# If the installer was run without a key:
printf 'ANTHROPIC_API_KEY=%s\n' 'your-provider-key' >> ~/.persome/env
chmod 600 ~/.persome/env
persome stop || true
persome start
```

Active work is reduced every five minutes by default. A first useful recall is
therefore expected within ten minutes of valid capture plus a working semantic
provider; `persome status`, `persome model status`, and the viewer explain sparse
or degraded states instead of inventing geometry.

## Proof points

### Local-first

- Durable Markdown, SQLite/FTS5, model snapshots, and logs live under
  `~/.persome` unless `PERSOME_ROOT` is set.
- AX is the default signal. Optional PP-OCRv6 runs locally in an isolated
  subprocess with bundled weights.
- The HTTP/MCP server binds to `127.0.0.1` by default, and there is no telemetry.
- Only configured semantic stages send derived text to the LLM or embedding
  endpoint you choose.

### Cross-app

The Swift watcher reads the focused AX tree across native and browser apps.
Persome normalizes focused element, visible text, window, application, URL, and
time into one capture and session pipeline. OCR is a fallback, not a parallel
cloud recorder.

### Agent-ready

- Streamable HTTP MCP: `http://127.0.0.1:8742/mcp`
- stdio MCP: `persome mcp`
- Local Chat: `persome chat`
- Stable model contract: `persome model export` and `GET /model/graph`
- Evidence tools: `search`, `read_receipt`, `verify_fact`, and
  `get_model_snapshot`

## Connect an MCP client

Start Persome first, then register the endpoint:

```bash
persome start
persome install claude-code
persome install codex

# Generate a stdio config that can be merged into Cursor's MCP config:
persome install mcp-json --filename persome-mcp.json
```

| Client | Verified configuration | Check |
|---|---|---|
| Claude Code | `persome install claude-code` | `claude mcp list` |
| Codex CLI / IDE | `persome install codex` | `codex mcp list` |
| Cursor | merge the generated `mcpServers.persome` object into `.cursor/mcp.json` or `~/.cursor/mcp.json` | Cursor Settings -> MCP |

The canonical JSON shape is:

```json
{
  "mcpServers": {
    "persome": {
      "command": "persome",
      "args": ["mcp"]
    }
  }
}
```

See [MCP client setup and verification](docs/mcp-clients.md) for HTTP configs,
uninstall commands, tested client versions, and privacy boundaries.

## Real MCP query with a cited answer

The following result is generated by the committed synthetic sample through the
same `search` and `read_receipt` implementation exposed by MCP.

```text
Tool: search
Input: {"query":"When does the user prefer focused writing?","top_k":2}

Top result:
  id:        20260701-0800-d4e5f6
  path:      project-work.md
  timestamp: 2026-07-01T08:00
  content:   The user reserves mornings for focused writing and review.

Tool: read_receipt
Input: {"entry_id":"20260701-0800-d4e5f6"}
```

A grounded client response can then say:

> The user prefers mornings for focused writing and review.
> [project-work.md, 2026-07-01 08:00;
> receipt `20260701-0800-d4e5f6`]

The receipt is resolvable, the superseded earlier statement remains available
as history, and the answer does not rely on the model's unsupported memory.

## Benchmark and verification status

This repository reports Runtime engineering evidence, not a paper-quality
personalization benchmark.

| Gate | Public evidence | Current status |
|---|---|---|
| Fresh root -> complete geometry | `tests/test_runtime_model_e2e.py` | deterministic synthetic pass |
| MCP search -> receipt | `sample_demo.py` + `verify_sample_mcp.py` | real streamable HTTP MCP, deterministic synthetic pass |
| Offline Runtime behavior | `pytest -m "not macos and not integration"` | complete offline suite; no provider key |
| Package completeness | clean wheel install + bundled Swift, Three.js, and PP-OCRv6 checks | required by CI/release |
| Secret and personal-data safety | `secret_scan.py` + `pii_scan.py` | required by CI/release |
| Memory quality / next-action prediction | separate benchmark repository | **not reported here** |

The sample uses synthetic fixtures and cannot establish recall quality on a
real person. No cross-user benchmark, next-action accuracy, latency percentile,
or comparison win is claimed. The launch machine's three isolated source
installs had an 11.896-second median with a warm `uv` cache; conditions and
limitations are recorded in [benchmark scope](docs/benchmarks.md).

## Why Persome

These projects solve adjacent but different jobs:

| System | Primary job | Where Persome differs |
|---|---|---|
| [screenpipe](https://github.com/screenpipe/screenpipe) | searchable local screen/audio history and developer platform | Persome centers an evolving Point/Line/Face/Volume/Root personal model with correction and receipts for MCP agents. |
| [Mem0](https://github.com/mem0ai/mem0) | a memory layer populated by application or conversation events | Persome begins with ambient macOS work context, owns the local capture/session pipeline, and exposes an inspectable model rather than only a memory API. |
| Assistant/platform memory | convenience inside one provider or client | Persome is a local Runtime shared across trusted MCP clients; data, export, correction, and deletion remain under the user's control. |

Persome is not a replacement for a full screen archive, a hosted vector memory,
or a provider's preference feature. Choose it when the core requirement is a
local, cross-app, auditable model that multiple agents can query.

## How it works

```mermaid
flowchart LR
  AX[macOS AX watcher] --> S0[S0 debounce]
  OCR[Optional local OCR] --> S1[S1 normalized capture]
  S0 --> S1
  S1 --> BUF[Capture buffer]
  BUF --> TL[1-minute timeline]
  TL --> SES[Deterministic sessions]
  SES --> DELTA[5-minute memory delta]
  DELTA --> PL[Points and Lines]
  PL --> FV[Faces and Volumes]
  FV --> ROOT[Root]
  PL --> RET[BM25 and optional dense retrieval]
  FV --> MCP[MCP, Chat, export, viewer]
  ROOT --> MCP
  RET --> MCP
```

Every modeled object keeps source receipts and bitemporal history. A sparse
store can truthfully contain Points and Lines without a Face, Volume, or Root.
The viewer shows that incomplete state rather than fabricating one.

Read [Runtime architecture](ARCHITECTURE.md), the
[model contract](MODEL_FORMAT.md), and the detailed
[maintainer architecture](docs/architecture.md).

## Inspect, correct, export, and delete

```bash
# Inspect
persome status
persome model status
persome faces-report
persome contradictions
open http://127.0.0.1:8742/model

# Correct or revoke one memory while retaining its audit trail
persome correct --help
# Agents can also call MCP correct_memory.

# Export a redacted owner-only snapshot (0600)
persome model export

# Delete model memory, or all captures/timeline/model state
persome stop
persome clean memory
persome clean all
```

For a complete uninstall that preserves personal data by default:

```bash
bash uninstall.sh

# Explicitly remove the remaining data, config, env, exports, and logs:
bash uninstall.sh --delete-data --yes
```

Client registrations are removed separately and idempotently:

```bash
persome uninstall claude-code
persome uninstall codex
persome uninstall claude-desktop
```

See [operations and data control](docs/operations.md) for exact paths, backup
advice, export sensitivity, reset behavior, and manual removal steps.

## Privacy boundary

- Personal data remains local until a configured model stage or connected agent
  sends selected text to its own provider.
- MCP capture tools can return raw screen text, titles, URLs, and focused-field
  values. Connect only clients you trust.
- Screenshots are omitted from MCP by default and encrypted at rest when
  retention is enabled.
- `persome model export` is redacted by default; `--raw` is an explicit opt-out.
- There is no built-in remote account, sync service, telemetry, meeting audio
  capture, computer-use actuation, or filesystem profiler.

Read [Security and privacy](SECURITY_PRIVACY.md) before using real personal
data, and report vulnerabilities through [SECURITY.md](SECURITY.md).

## Platform support

| Platform | Capture | Local OCR | Runtime / MCP |
|---|---|---|---|
| macOS 13+ on Apple Silicon (`arm64`) | supported | bundled PP-OCRv6 | supported |
| macOS 13+ on Intel (`x86_64`) | supported AX path | unavailable because Paddle does not ship the required Intel wheel | supported |
| Linux | no live macOS capture | not packaged | offline tests and development only |
| Windows | unsupported | unsupported | unsupported |

Python 3.11-3.13 is supported by the installer. See
[operations](docs/operations.md) and [troubleshooting](docs/troubleshooting.md).

## Persome and Personome

**Persome** is this open-source Runtime and project name. **Personome** is the
research term for the learned model of one person: a dynamic state assembled
from sourced observations, relations, stable patterns, and higher-level
structure. The product name stays Persome in commands, packages, paths, APIs,
and documentation.

## Paper and architecture-note status

This repository ships the executable Runtime and an implementation-oriented
architecture note. The architecture documents are not a peer-reviewed paper,
and the Runtime's synthetic gates are not publication benchmarks. The paper,
benchmark suite, data statements, and project publication will live as separate
artifacts with independent licenses before release. See
[licensing boundaries](LICENSES.md) and [benchmark limitations](docs/benchmarks.md).

## Roadmap

The public roadmap is issue-driven:

- more tested MCP client integrations;
- richer first-run permission diagnostics;
- explicit import/export interoperability;
- Intel and future-macOS compatibility evidence;
- a separate, reproducible personal-model benchmark suite.

Browse [starter issues](https://github.com/Persome-ai/persome-core/issues) or
start a design question in
[Discussions](https://github.com/Persome-ai/persome-core/discussions).

## Contributing and community

Read [CONTRIBUTING.md](CONTRIBUTING.md), follow the
[Code of Conduct](CODE_OF_CONDUCT.md), and use [SUPPORT.md](SUPPORT.md) to choose
the right channel. Every commit requires DCO sign-off, and CI blocks known
secrets, personal data, non-English source text, contract drift, lint failures,
and offline regressions.

If an inspectable, user-owned personal model is useful to your agents, star the
repository and share the MCP client or workflow you want Persome to support.

## License

Runtime code is Apache-2.0. Paper, benchmark, project-note, third-party, and
personal-data boundaries are explained in [LICENSES.md](LICENSES.md). Required
incorporated-work notices remain in [NOTICE](NOTICE) and
[THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES).

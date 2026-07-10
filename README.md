# persome-core

persome-core is the local-first Personal Model Runtime for macOS.

![CI](https://github.com/Persome-ai/persome-core/actions/workflows/ci.yml/badge.svg)
![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)
![PyPI](https://img.shields.io/pypi/v/persome-core)
![macOS: 13+](https://img.shields.io/badge/macOS-13%2B-lightgrey)

## What it is / Why

persome-core captures macOS Accessibility tree events as a person works. For
AX-poor apps, optional on-device PP-OCRv6 can read a focused-window screenshot
without network egress. OCR is bundled but disabled by default; AX capture is
the default observation path.

It distills captures into durable Markdown memory files plus SQLite with FTS5 and a vector index. The model has explicit layers: a Point is a sourced fact, a Line is an evolution or semantic relation, a Face is a behavioral pattern, a Volume is a cross-domain structure, and Root is the single apex that can be expanded back to its receipts.

The [Personome paper](https://persome-ai.github.io/persome/) states the thesis: an LLM predicts the next token, and a Personome predicts a person's next action, with memory as the weights of your personal model.

## Quickstart

macOS 13+ only. Python 3.11 is managed with uv.

```bash
git clone https://github.com/Persome-ai/persome-core
cd persome-core
bash install.sh
```

**Grant Accessibility permission** (required — this is how persome reads the AX tree):
open **System Settings → Privacy & Security → Accessibility** and enable the terminal
you launch persome from (Terminal, iTerm2, Warp, VS Code, …). Without this grant the
daemon runs but captures nothing.

If you enable `[capture] enable_ocr_fallback = true` or retain screenshots,
also grant **Screen Recording** to the same executable. Restart Persome after
changing either macOS permission.

Configure your LLM key (bring your own — nothing ships in the tree):

```bash
mkdir -p ~/.persome
touch ~/.persome/env
chmod 600 ~/.persome/env
# install.sh already prompts for this key. If you skipped it, append here
# (>> so you never overwrite a key install.sh wrote):
cat >> ~/.persome/env <<'EOF'
ANTHROPIC_API_KEY=your-anthropic-key
# Optional Anthropic-format gateway, for example DeepSeek's /anthropic endpoint:
# ANTHROPIC_BASE_URL=https://your-gateway.example.com/anthropic
# Optional hybrid dense retrieval (OpenAI-compatible embeddings endpoint):
# OPENAI_API_KEY=your-openai-key
# OPENAI_BASE_URL=https://your-embeddings.example.com/v1
EOF
```

Self-check, then start:

```bash
persome doctor   # ✓/✗ per prerequisite (env file, key, Swift helpers, AX grant, port); no LLM calls

persome start
# MCP HTTP endpoint: http://127.0.0.1:8742/mcp
# Local Point/Line/Face/Volume/Root explorer: http://127.0.0.1:8742/model

# Use the same model from a local terminal chat (there is no browser Chat UI):
persome chat

# Build and inspect the current personal model:
persome model build
persome model status
persome model export

# Or use stdio:
persome mcp
```

Bring your own key. No key ships with the code. Without a key, capture and BM25 retrieval still work, and LLM-dependent stages degrade cleanly.

### From observation to a personal model

Leave the daemon running while you work. It groups observations into one-minute
timeline blocks and deterministic work sessions. When a session ends, the
terminal finalizer reduces its activity, extracts an evidence-gated structured
memory delta, and deterministically applies durable entities, assertions,
events, and relations as Points and Lines. It also records repeated behavioral
patterns as evidence-backed skill memory.

At 00:15 local time by default, the daemon runs the same locked build as
`persome model build`: pending session recovery, case extraction, schema mining,
cross-domain synthesis, Root synthesis, and vector backfill. A new or
sparse store is expected to be `degraded` until repeated evidence is sufficient
for a Face, Volume, and Root. `persome model status` explains which geometry is
missing; the viewer reflects the current store rather than filling gaps with
synthetic data.

An LLM key is therefore optional for collection and BM25 access, but required
for real semantic modeling. `PERSOME_LLM_MOCK=1` exists only for deterministic
tests and produces synthetic verification output, not a real person's model.

The optional Chat surface is model-focused by default. Shell, arbitrary
filesystem, and Web tools require the explicit
`[chat] unsafe_local_tools_enabled = true` opt-in. Web search/page fetch also
requires installing the `chat` extra (`persome-core[chat]`).

## Use it from your agent (MCP)

Claude Code over HTTP:

```bash
claude mcp add --transport http persome http://127.0.0.1:8742/mcp
```

Claude Desktop over stdio:

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

Cursor over stdio:

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

## Architecture

```text
Swift watcher / trusted ingest
  -> S0 debounce
  -> S1 parse (focused_element / visible_text / url)
  -> capture buffer
  -> 1-minute timeline blocks
  -> session segmentation (three deterministic rules)
  -> reducer
  -> terminal model finalizer

+-----------------------+
| Personal model        |
|                       |
| memory_delta          | evidence gates -> Points / relation Lines
| pattern detector      | repeated behavior -> skill memory
| case + schema mining  | reusable solutions -> Faces
| cross-domain + Root   | Volumes -> one auditable apex
| tiered forgetting     | read = reinforcement
| adjudication          | explicit correction / contradiction queue
+-----------------------+

+-------------------+
| Retrieval         |
|                   |
| six-head RRF      | BM25 + dense vectors + entity + scene + time + relation
| tree-path return  | chain-to-USER with receipt pointers
| time travel       | query memory as it was at time T
+-------------------+
```

## Privacy

- Persistent model data stays under `~/.persome`.
- Enabled LLM stages send derived context to the endpoint you configure; optional dense retrieval sends embedding inputs to its configured endpoint.
- There is no telemetry.
- OCR is optional, subprocess-isolated, and fully local.
- Secrets live in a 0600 env file at `~/.persome/env`.
- The capture buffer has a tiered retention policy.
- The runtime does not include computer-use actuation, meeting audio capture, or filesystem profiling.

See [SECURITY_PRIVACY.md](SECURITY_PRIVACY.md) for the exact egress and trust
boundary.

## How it compares

| System | What it stores | Relationship to persome |
| --- | --- | --- |
| mem0 / Zep / Letta | Conversational memory. | Orthogonal. They remember chats, while persome remembers screen behavior. |
| screenpipe | Raw screen recordings + OCR. | persome uses structured AX capture distilled into behavioral memory with geometry and forgetting. |
| Rewind / Limitless | Closed source. | persome-core is Apache-2.0. |

## Provenance

persome-core is derived from Einsia/OpenChronicle (MIT). Its provenance and retained upstream license notices are preserved in [NOTICE](NOTICE) and [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES).

## Paper

[Personome](https://persome-ai.github.io/persome/): an LLM predicts the next token, and a Personome predicts a person's next action, with memory as the weights of your personal model. This repository implements state formation, personal weights, provenance, and model access. It does not claim to ship the paper's next-action predictor. Prediction datasets, metrics, and ablations belong in the separate `persome-bench` repository so evaluation can pin a released Runtime version without coupling benchmark code to private local storage.

The exact claim-to-code boundary is in [PAPER.md](PAPER.md). Reproduce the
fresh-root synthetic model with [REPRODUCING.md](REPRODUCING.md).

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) — Runtime boundary and pipeline.
- [MODEL_FORMAT.md](MODEL_FORMAT.md) — versioned model snapshot.
- [MCP.md](MCP.md) — public agent interface.
- [SECURITY_PRIVACY.md](SECURITY_PRIVACY.md) — data, egress, and threat model.
- [docs/INDEX.md](docs/INDEX.md) — maintainer references.

## License

Apache-2.0.

# persome-core

persome-core is the local-first Personal Model Runtime for macOS.

<!-- hero: 3D memory geometry visualization (persome memory-viz) — video/gif here -->
<!-- ![3D memory geometry visualization](TODO: hero video/gif from `persome memory-viz`) -->

![CI](https://github.com/Persome-ai/persome-core/actions/workflows/ci.yml/badge.svg)
![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)
![PyPI](https://img.shields.io/pypi/v/persome-core)
![macOS: 13+](https://img.shields.io/badge/macOS-13%2B-lightgrey)

## What it is / Why

persome-core captures macOS Accessibility tree events as you work. For AX-poor apps, it falls back to on-device screenshot OCR using PP-OCRv6 with 6.2 MB bundled weights and zero network.

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

# Or use stdio:
persome mcp
```

Bring your own key. No key ships with the code. Without a key, capture and BM25 retrieval still work, and LLM-dependent stages degrade cleanly.

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
Swift watcher
  -> S0 debounce
  -> S1 parse (focused_element / visible_text / url)
  -> capture buffer
  -> 1-minute timeline blocks
  -> session segmentation (three deterministic rules)
  -> reducer
  -> classifier
  -> memory/*.md

+-------------------+
| Consolidation     |
|                   |
| memory_delta      | one LLM read per session + deterministic gates
| schema mining     |
| schema_faces      | points / lines / faces / volumes
| root apex         |
| tiered forgetting | read = immunity
| adjudication      | semantic contradictions
+-------------------+

+-------------------+
| Retrieval         |
|                   |
| six-head RRF      | BM25 + dense vectors + entity + scene + time + relation
| tree-path return  | chain-to-USER with receipt pointers
| time travel       | query memory as it was at time T
+-------------------+
```

## Privacy

- All data stays on the machine under `~/.persome`.
- The only network egress is the LLM endpoint you configure, plus an optional embeddings endpoint.
- There is no telemetry.
- OCR is fully local.
- Secrets live in a 0600 env file at `~/.persome/env`.
- The capture buffer has a tiered retention policy.
- Egress paths are few enough to verify by grepping the source.
- The runtime does not include computer-use actuation, meeting audio capture, or filesystem profiling.

## How it compares

| System | What it stores | Relationship to persome |
| --- | --- | --- |
| mem0 / Zep / Letta | Conversational memory. | Orthogonal. They remember chats, while persome remembers screen behavior. |
| screenpipe | Raw screen recordings + OCR. | persome uses structured AX capture distilled into behavioral memory with geometry and forgetting. |
| Rewind / Limitless | Closed source. | persome-core is Apache-2.0. |

## Provenance

persome-core is derived from Einsia/OpenChronicle (MIT). Its provenance and retained upstream license notices are preserved in [NOTICE](NOTICE) and [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES).

## Paper

[Personome](https://persome-ai.github.io/persome/): an LLM predicts the next token, and a Personome predicts a person's next action, with memory as the weights of your personal model. This repository implements state formation, personal weights, provenance, and model access. Prediction datasets, metrics, and ablations belong in the separate `persome-bench` repository so evaluation can pin a released Runtime version without coupling benchmark code to private local storage.

## License

Apache-2.0.

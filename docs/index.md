---
layout: default
title: Persome
description: Local-first Personal Model Runtime for macOS
---

# Persome

Persome is a local-first macOS Runtime that collects focused cross-app context,
builds an inspectable model of one real person, and exposes it to trusted MCP
clients.

![Synthetic Persome model with Points, Lines, Faces, Volume, and Root](assets/persome-model-hero.png)

The image is a real Runtime screenshot generated from synthetic fixtures. Open
the authenticated real-data viewer with `persome model open` after installation.

**[Star Persome on GitHub](https://github.com/Intuition-Lab/personal-model)** to
follow releases and help prioritize the next MCP integrations.

## Try the synthetic model

```bash
git clone https://github.com/Intuition-Lab/personal-model.git
cd personal-model
uv run python scripts/sample_demo.py
```

## Build your model

```bash
bash install.sh
persome onboard
persome status
persome llm status
persome ocr status
persome doctor
persome model open
```

The installer already runs onboarding in an interactive macOS session and
leaves the Runtime running; the explicit `persome onboard` above is an
idempotent recheck, not a prerequisite `stop`/`start` cycle. It requests
Accessibility for the actual source-versioned AX helper and watcher, applies
the durable OCR policy, proves the final Runtime owner and generation, and
returns the receipt appropriate to daemon, ingest, paused, or locked mode.
When an active local Obsidian vault is detected, onboarding also offers a
read-only, one-click history import so the first model need not start empty.
See [Import existing knowledge](importing.md) for Obsidian, local folders, and
Notion Markdown exports.

Update an existing installation from any directory with `persome update`. The
command builds an inactive candidate, atomically exchanges it with the active
virtualenv, preserves local configuration and personal data, and commits only
after the same mode-aware onboarding proof. An interrupted update exchanges the
old Runtime back before restoring its lifecycle owner.

Persome keeps data under `~/.persome`, uses local AX capture by default, offers
optional on-device OCR, and serves streamable HTTP MCP at
`http://127.0.0.1:8742/mcp` behind an owner-local bearer. Normal client
installers use stdio and do not copy that credential.

## Read next

- [Product experience and Quick Start](https://github.com/Intuition-Lab/personal-model#readme)
- [Architecture](architecture.md)
- [LLM provider configuration](config.md#providers-and-stage-models)
- [MCP clients](mcp-clients.md)
- [Operations and data control](operations.md)
- [Import existing knowledge](importing.md)
- [Benchmark scope](benchmarks.md)
- [Security and privacy](https://github.com/Intuition-Lab/personal-model/blob/main/SECURITY_PRIVACY.md)
- [Releases](https://github.com/Intuition-Lab/personal-model/releases)

Persome is the project and Runtime name. Personome is the research term for the
learned personal model. Publication artifacts and research benchmarks are
separate works with independent licenses.

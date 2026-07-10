---
layout: default
title: Persome
description: Local-first Personal Model Runtime for macOS
---

# Persome

Persome is a local-first macOS Runtime that collects focused cross-app context,
builds an inspectable model of one real person, and exposes it to trusted Chat
and MCP clients.

![Synthetic Persome model with Points, Lines, Faces, Volume, and Root](assets/persome-model-hero.png)

The image is a real Runtime screenshot generated from synthetic fixtures. The
same viewer is available at `http://127.0.0.1:8742/model` after installation.

## Try the synthetic model

```bash
git clone https://github.com/Persome-ai/persome-core.git
cd persome-core
uv run python scripts/sample_demo.py
```

## Build your model

```bash
bash install.sh
persome doctor
persome start
open http://127.0.0.1:8742/model
```

Persome keeps data under `~/.persome`, uses local AX capture by default, offers
optional on-device OCR, and serves streamable HTTP MCP at
`http://127.0.0.1:8742/mcp`.

## Read next

- [Product experience and Quick Start](https://github.com/Persome-ai/persome-core#readme)
- [Architecture](architecture.md)
- [MCP clients](mcp-clients.md)
- [Operations and data control](operations.md)
- [Benchmark scope](benchmarks.md)
- [Security and privacy](https://github.com/Persome-ai/persome-core/blob/main/SECURITY_PRIVACY.md)
- [Releases](https://github.com/Persome-ai/persome-core/releases)

Persome is the project and Runtime name. Personome is the research term for the
learned personal model. Publication artifacts and research benchmarks are
separate works with independent licenses.

# Persome: Build your Personal Model

<!-- mcp-name: io.github.Intuition-Lab/personal-model -->

**The open-source Personal Model that makes every AI yours.**

Persome learns how you actually think and work from focused activity captured on your Mac after you grant macOS permission—then gives Codex, Claude Code, and other trusted MCP-compatible clients evidence-linked context to continue your work and make grounded decisions.

**Runs locally on your Mac. Private by default. Yours to inspect, correct, export, and delete.**

[![CI](https://github.com/Intuition-Lab/personal-model/actions/workflows/ci.yml/badge.svg)](https://github.com/Intuition-Lab/personal-model/actions/workflows/ci.yml) [![Release](https://img.shields.io/github/v/release/Intuition-Lab/personal-model)](https://github.com/Intuition-Lab/personal-model/releases) [![License: Apache-2.0](https://img.shields.io/badge/code-Apache--2.0-blue)](LICENSE) [![macOS 13+](https://img.shields.io/badge/macOS-13%2B-black)](#2-install-with-your-data) [![MCP](https://img.shields.io/badge/interface-MCP-0b7285)](MCP.md) [![Official MCP Registry](https://img.shields.io/badge/Official_MCP_Registry-Persome-6f42c1)](https://registry.modelcontextprotocol.io/?q=Persome)

[Try the synthetic demo](#1-five-minute-synthetic-demo) · [Install with your data](#2-install-with-your-data) · [Connect an MCP client](#3-connect-a-trusted-mcp-client) · [Star Persome on GitHub](https://github.com/Intuition-Lab/personal-model)

![Illustration of a mature Persome Personal Model with evidence-linked Points, Lines, Faces, Volumes, and a Root](docs/assets/readme/personal-model.png)

_Concept illustration of a mature Personal Model. The deterministic Runtime proof is shown in the demo below._

---

## What is it?

Your `HUMAN.md`

Persome connects activity into progressively deeper context:

| Layer | Meaning |
| --- | --- |
| **Point** | A sourced observation or event |
| **Line** | A relationship or change over time |
| **Face** | A pattern supported by related evidence |
| **Volume** | A higher-order structure across projects or areas of life |
| **Root** | The current integrated model of you |

New evidence can strengthen, revise, or overturn an earlier inference. Every important claim keeps receipts.

---

## Use cases

### 1. One Root — A Model of You

**Thousands of moments. One evolving model of you.**

Persome turns sourced observations into relationships, patterns, higher-order structure, and one current Root: what matters now, how you tend to decide, and where your attention is moving.

<p align="center"><img src="docs/assets/readme/one-root.png" alt="One Root — A Model of You. An activity stream becomes 1,000+ Points, 300+ Lines, 80+ Faces, 20+ Volumes, and one evolving model. A Personal Model example on the right shows current goals and quality standards." width="100%"></p>

From Points to Lines, Faces, Volumes, and one Root—a living model of who you are and what matters now.

### 2. Same AI. Different You.

<p align="center"><img src="docs/assets/readme/same-ai-different-you.png" alt="" width="100%"></p>

**The model is the same. The person it understands is different.**

Two people can give the same AI the same prompt and deserve different answers. Your Personal Model changes how an agent prioritizes, decides, writes, and acts—because it understands who it is working for.

The same prompt should not produce the same answer for everyone. Give AI a model of you.

### 3. One MCP — Turn coding agents into proactive agents

<p align="center"><img src="docs/assets/readme/one-mcp.png" alt="" width="100%"></p>

**Your coding agent finds its own work**

Connect Persome once through MCP. Codex, Claude Code, and other trusted agents can use the same model of your goals, priorities, working patterns, and boundaries.

Persome identifies unfinished work, ranks it against your priorities, and separates safe local tasks from decisions that need you. The connected agent executes; you keep authority over external actions.

#### Continue where you left off

<p align="center"><img src="docs/assets/readme/continue-where-you-left-off.png" alt="Persome helps a coding agent continue unfinished work. The left panel lists README, onboarding, and MCP tasks; the Codex panel restores the previous work state, current goal, next step, project directory, Git status, and unstaged changes." width="100%"></p>

#### Work while you sleep

<p align="center"><img src="docs/assets/readme/work-while-you-sleep.png" alt="Persome Work While You Sleep interface. It organizes 30 open loops, filters five tasks that are safe to complete by permission scope and action type, and presents a morning report of completed local work, pending decisions, external actions, and permission boundaries." width="100%"></p>

---

## Install, connect, and verify

**Choose the path that matches what you want to prove.** The synthetic demo and the real-data install are intentionally separate.

### 1. Five-minute synthetic demo

Try the complete Persome model without touching your personal data. This path requires Git and [uv](https://docs.astral.sh/uv/getting-started/installation/), but no API key, macOS Accessibility permission, or access to your existing `~/.persome` data.

```text
git clone https://github.com/Intuition-Lab/personal-model.git
cd personal-model
uv run python scripts/sample_demo.py
```

The script opens the local viewer at `http://127.0.0.1:8743/model` and deletes its temporary synthetic data when you press `Ctrl-C`. Add `--showcase` for the denser, still fully synthetic graph shown below.

![Persome local personal-model viewer rendering a dense synthetic Point, Line, Face, Volume, and Root graph](docs/assets/persome-model-hero.png)

_Actual `/model` screenshot produced by `scripts/sample_demo.py --showcase`: 424 synthetic Points, 146 Lines, 12 Faces, 4 Volumes, and 1 Root. It contains no personal data._

### 2. Install with your data

Requirements: macOS 13 or newer and Xcode Command Line Tools. For the shortest package-managed installation:

```text
uv tool install personal-model
persome onboard
persome model open --after 30
```

The distribution is named `personal-model`; the installed CLI is `persome`.

For the most explicit source-based first run:

```text
git clone https://github.com/Intuition-Lab/personal-model.git
cd personal-model
bash install.sh
```

After successful interactive onboarding, the source installer schedules the one-shot 30-minute viewer reminder automatically.

**What onboarding proves**

- `persome onboard` explains each macOS request before it appears.
- Accessibility is granted to the versioned `mac-ax-helper` and, only when event-driven capture is enabled, `mac-ax-watcher`.
- Screen Recording is requested only when the effective screenshot or local-OCR policy requires pixels. Persome never requires Full Disk Access.
- On Apple Silicon, onboarding verifies the isolated local OCR worker when OCR is enabled.
- It proves the final lifecycle owner and Runtime generation, then reports a fresh-capture receipt in standard daemon mode or an explicit readiness/privacy receipt for supported alternate modes such as trusted ingest.

An LLM is optional for collection and BM25 recall, but required for semantic modeling. If provider setup was skipped, run:

```text
persome llm setup
persome llm status --check
```

### 3. Connect a trusted MCP client

Register whichever owner-local clients you use:

```text
persome install claude-code
persome install codex
persome install claude-desktop
persome install opencode
```

These stdio registrations launch the MCP process on demand, so the daemon does
not need to be running after onboarding has initialized the local database, and
no HTTP bearer is copied into client configuration. Schema creation and
migration remain daemon-owned; a brand-new or externally upgraded data root
must run `persome start` once before stdio clients use it. Stdio writes remain
available while the daemon is stopped, but WAL maintenance waits for the daemon;
start it periodically if you use write tools in that mode so the WAL stays bounded.

For Cursor, generate a stdio configuration and merge its `mcpServers.persome` object into `.cursor/mcp.json` or `~/.cursor/mcp.json`:

```text
persome install mcp-json --filename persome-mcp.json
```

> MCP access is a personal-data capability; register only clients you trust.

### 4. Verify and ask grounded questions

```text
persome status
persome model status
persome model open

# Only if you configured a semantic provider:
persome llm status --check
```

A sparse or degraded model can be valid early; Persome reports missing geometry instead of fabricating Faces, Volumes, or a Root.

After connecting an MCP client, try:

> Search my Persome memory for **[topic]**. Use `search`, open the strongest result with `read_receipt`, and cite the source path, timestamp, and receipt ID. If the evidence is missing or conflicting, say so instead of guessing.

Active work is reduced every five minutes by default. With valid capture and a working semantic provider, a first useful recall is operationally expected within about ten minutes—not guaranteed as a benchmark result.

### 5. Update Persome

For a `uv tool` installation, upgrade with the package manager and re-run Runtime proof:

```text
uv tool upgrade --python 3.12 personal-model
persome onboard
persome model open --after 30
```

After any upgrade, restart editors that host a Persome stdio MCP process before
resuming Runtime writes. A process loaded from the previous release cannot join
the new cross-process SQLite maintenance gate until the editor reconnects it.

For an installation created by `install.sh`, run the transactional updater from any directory:

```text
persome update
```

`persome update` preserves configuration, credentials, personal data, capture policy, and lifecycle intent, and performs its own mode-aware onboarding before committing the update. Do not use it to update a package-manager-managed installation.

---

<p align="center"><a href="https://github.com/Intuition-Lab/personal-model"><b>Star Persome on GitHub</b></a> · <a href="https://registry.modelcontextprotocol.io/?q=Persome">Official MCP Registry</a> · <a href="https://github.com/Intuition-Lab/personal-model/blob/main/docs/mcp-clients.md">MCP client setup</a> · <a href="https://github.com/Intuition-Lab/personal-model/blob/main/SECURITY_PRIVACY.md">Security &amp; privacy</a></p>

<details>
<summary>Contributors</summary>

<!-- ALL-CONTRIBUTORS-LIST:START - Do not remove or modify this section -->
<table>
  <tbody>
    <tr>
      <td valign="middle">
        <a href="https://github.com/Singularity-tian"><img src="https://avatars.githubusercontent.com/u/113085728?v=4&amp;size=112" width="56" align="left" alt="Singularity" /></a>
        &nbsp;&nbsp;<strong><a href="https://github.com/Singularity-tian">Singularity</a></strong><br />
        &nbsp;&nbsp;<sub>💻&nbsp;Code</sub>
      </td>
      <td valign="middle">
        <a href="https://github.com/GouBuliya"><img src="https://avatars.githubusercontent.com/u/163627234?v=4&amp;size=112" width="56" align="left" alt="Li_Xufeng" /></a>
        &nbsp;&nbsp;<strong><a href="https://github.com/GouBuliya">Li_Xufeng</a></strong><br />
        &nbsp;&nbsp;<sub>💻&nbsp;Code</sub>
      </td>
      <td valign="middle">
        <a href="https://github.com/SiyiZhu1"><img src="https://avatars.githubusercontent.com/u/132850441?v=4&amp;size=112" width="56" align="left" alt="Siyi" /></a>
        &nbsp;&nbsp;<strong><a href="https://github.com/SiyiZhu1">Siyi</a></strong><br />
        &nbsp;&nbsp;<sub>🎨&nbsp;Design</sub>
      </td>
    </tr>
    <tr>
      <td valign="middle">
        <a href="https://github.com/kevinaimonster"><img src="https://avatars.githubusercontent.com/u/172621334?v=4&amp;size=112" width="56" align="left" alt="Kevin" /></a>
        &nbsp;&nbsp;<strong><a href="https://github.com/kevinaimonster">Kevin</a></strong><br />
        &nbsp;&nbsp;<sub>💻&nbsp;Code</sub>
      </td>
      <td valign="middle">
        <a href="https://github.com/huachenjie238-oss"><img src="https://avatars.githubusercontent.com/u/261379605?v=4&amp;size=112" width="56" align="left" alt="huachenjie238-oss" /></a>
        &nbsp;&nbsp;<strong><a href="https://github.com/huachenjie238-oss">huachenjie238-oss</a></strong><br />
        &nbsp;&nbsp;<sub>📈&nbsp;Growth</sub>
      </td>
      <td valign="middle">
        <a href="https://github.com/JingYangGit"><img src="https://avatars.githubusercontent.com/u/169429757?v=4&amp;size=112" width="56" align="left" alt="Jing@Meowy" /></a>
        &nbsp;&nbsp;<strong><a href="https://github.com/JingYangGit">Jing@Meowy</a></strong><br />
        &nbsp;&nbsp;<sub>📈&nbsp;Growth</sub>
      </td>
    </tr>
    <tr>
      <td valign="middle">
        <a href="https://github.com/AMTso7aw"><img src="https://avatars.githubusercontent.com/u/113247039?v=4&amp;size=112" width="56" align="left" alt="Zhiheng Chen" /></a>
        &nbsp;&nbsp;<strong><a href="https://github.com/AMTso7aw">Zhiheng Chen</a></strong><br />
        &nbsp;&nbsp;<sub>💻&nbsp;Code</sub>
      </td>
    </tr>
  </tbody>
</table>
<!-- ALL-CONTRIBUTORS-LIST:END -->

</details>

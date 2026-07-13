# Readme

**About**: **Build your HUMAN.md.**

<p align="center"><b>[Personal Model]</b></p>

<p align="center"><b>Build your Personal Model →</b><br>☆ Star Persome on GitHub</p>

### **Build your Personal Model**

The open-source personal model that makes every AI yours.

Persome learns how you actually think and work from everything you see, say, hear, and do—then gives Codex, Claude Code, and any MCP agent the context to pick up where you left off, understand what matters, and work while you sleep.

<p align="center"><img src="docs/assets/readme/personal-model.png" alt="图片展示了Persome个人模型的界面，左侧有“LIVE PERSONAL MODEL”及“THE SHAPE OF YOU”等文字，强调系统思维创始人将个人上下文转化为可检验产品、证据支持决策和可持续动量。右侧是模型可视化界面，显示了1012个点、326条线、18个面、6个体积和1个根节点，还呈现了可调整的货币、原型先探索等信号，以及反馈循环、投资判断平衡等概念。右侧还配有如何阅读模型的说明，如点代表观察到的事实，线代表演变或关系等。" width="100%"></p>

**Runs locally on your Mac. Private by default. Yours to inspect, correct, export, and delete.**

### What is it？

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

### Use case

1. **One Root — A Model of You**

**Thousands of moments. One evolving model of you.**

Persome turns sourced observations into relationships, patterns, higher-order structure, and one current Root: what matters now, how you tend to decide, and where your attention is moving.

<p align="center"><img src="docs/assets/readme/one-root.png" alt="图片展示了“ONE ROOT - A MODEL OF YOU”概念，说明其将活动流转化为个人模型。从左至右，活动流（如笔记、Bugs等）形成点（1000+时刻）、线（300+连接）、面（80+模式）、体（20+主题），最终生成一个模型（1个，类型为模型）。右侧是个人模型示例，包含当前目标、质量标准等信息，底部强调“Thousands of moments. One evolving model of you.”，与上下文介绍的个人模型从众多时刻中生成、不断演进的内容相呼应。" width="100%"></p>

From Points to Lines, Faces, Volumes, and one Root—a living model of who you are and what matters now.

1. **Same AI. Different You.**

<p align="center"><img src="docs/assets/readme/same-ai-different-you.png" alt="" width="100%"></p>

**The model is the same. The person it understands is different.**

Two people can give the same AI the same prompt and deserve different answers. Your Personal Model changes how an agent prioritizes, decides, writes, and acts—because it understands who it is working for.

The same prompt should not produce the same answer for everyone. Give AI a model of you.

1. **One MCP - Trun coding agent into proactive agent** 

<p align="center"><img src="docs/assets/readme/one-mcp.png" alt="" width="100%"></p>

**Your coding agent finds its own work body**

Connect Persome once through MCP. Codex, Claude Code, and other trusted agents can use the same model of your goals, priorities, working patterns, and boundaries.

Persome identifies unfinished work, ranks it against your priorities, and separates safe local tasks from decisions that need you. The connected agent executes; you keep authority over external actions.

**-Continue where you left off** 

<p align="center"><img src="docs/assets/readme/continue-where-you-left-off.png" alt="这张图片展示了Persome作为编程代理的功能场景，核心呈现“继续未完工作”的能力。页面左侧标注了三个待处理任务，分别为README、Onboarding和MCP，附带对应时间；右侧CODEX区域显示已恢复的工作状态，即此前准备Persome的启动事宜，当前目标是让Personal Model在五分钟内可见，下一步工作为修复新用户引导的验证，同时呈现了当前项目目录、git状态及未暂存的文件改动，印证该工具可将个人工作模型同步给编程代理，帮助用户从断点处继续推进工作。" width="100%"></p>

**-Work while you sleep**

<p align="center"><img src="docs/assets/readme/work-while-you-sleep.png" alt="图片展示了Persome的“Work While You Sleep”功能界面。左侧“30 OPEN LOOPS”部分，呈现了Personal Model、Tests、Dependencies、Codebase、Docs等模块及对应数值。中间“5 SAFE TO COMPLETE”部分，有Safe Task Filter，可筛选权限范围、数据访问、网络、命令、外部动作等。右侧“MORNING REPORT”部分，列出已完成本地任务、等待决策任务、外部动作等，还展示了权限边界设置。该图与上下文介绍的Persome功能相呼应，直观呈现其工作模式。" width="100%"></p>

### Install, connect, and verify

**Choose the path that matches what you want to prove.** The synthetic demo and the real-data install are intentionally separate.

#### 1. Five-minute synthetic demo

Try the complete Persome model without touching your personal data. This path requires Git and [uv](https://docs.astral.sh/uv/getting-started/installation/), but no API key, macOS Accessibility permission, or access to your existing `~/.persome` data.

```text
git clone https://github.com/Intuition-Lab/personal-model.git
cd personal-model
uv run python scripts/sample_demo.py
```

The script opens the local viewer at `http://127.0.0.1:8743/model` and deletes its temporary synthetic data when you press `Ctrl-C`. Add `--showcase` for the denser, still fully synthetic graph shown in the README.

#### 2. Install with your data

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

#### 3. Connect a trusted MCP client

Register whichever owner-local clients you use:

```text
persome install claude-code
persome install codex
persome install claude-desktop
persome install opencode
```

These stdio registrations launch Persome on demand, so the daemon does not need to be running and no HTTP bearer is copied into client configuration.

For Cursor, generate a stdio configuration and merge its `mcpServers.persome` object into `.cursor/mcp.json` or `~/.cursor/mcp.json`:

```text
persome install mcp-json --filename persome-mcp.json
```

> MCP access is a personal-data capability; register only clients you trust.

#### 4. Verify and ask grounded questions

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

#### 5. Update Persome

For a `uv tool` installation, upgrade with the package manager and re-run Runtime proof:

```text
uv tool upgrade personal-model
persome onboard
persome model open --after 30
```

For an installation created by `install.sh`, run the transactional updater from any directory:

```text
persome update
```

`persome update` preserves configuration, credentials, personal data, capture policy, and lifecycle intent, and performs its own mode-aware onboarding before committing the update. Do not use it to update a package-manager-managed installation.

<p align="center"><a href="https://github.com/Intuition-Lab/personal-model"><b>Star Persome on GitHub</b></a> · <a href="https://registry.modelcontextprotocol.io/?q=Persome">Official MCP Registry</a> · <a href="https://github.com/Intuition-Lab/personal-model/blob/main/docs/mcp-clients.md">MCP client setup</a> · <a href="https://github.com/Intuition-Lab/personal-model/blob/main/SECURITY_PRIVACY.md">Security &amp; privacy</a></p>

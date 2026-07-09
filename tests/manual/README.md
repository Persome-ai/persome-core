# Actuation manual harnesses

On-device, opt-in harnesses that drive **real macOS apps** through the Persome actuation layer
(AX-first + on-device PP-OCRv6 fallback). They are **not** part of the offline gate — they need
a logged-in desktop, the agent apps, and a DeepSeek key. They print a per-step trace and a verdict.

## The actuator binary (shared dependency)

Every harness shells out to `/tmp/mac-ax-actuator`. Build it from source before running:

```bash
swiftc -O -framework Cocoa -framework ApplicationServices \
  resources/mac-ax-actuator.swift -o /tmp/mac-ax-actuator
```

It exposes two subcommands the harnesses use: `snapshot` (dump the app's AX tree as JSON elements
with `role`/`label`/`value`/`bbox`/stable `id`) and `act` (verbs: `press`, `setvalue`, `key`,
`type`, `clickxy`; `--no-cursor` keeps it background-safe; `--cache-before` reuses the prior
after-state). Override the path with `PERSOME_AX_ACTUATOR`.

## Harnesses

| File | What it does | Run |
|---|---|---|
| **`bench_cases.py`** | The growing reproducible computer-use suite (see below). | `PERSOME_ACTUATION_E2E=1 DEEPSEEK_API_KEY=… uv run python3 tests/manual/bench_cases.py [names…]` |
| **`bench_meeting_invite.py`** | Flagship cross-app flow: schedule a 腾讯会议 meeting → copy the join link → paste it into the 飞书 chat (without sending). ~18-22s. | `PERSOME_ACTUATION_E2E=1 … uv run python3 tests/manual/bench_meeting_invite.py` |
| **`bench_open_gmail.py`** | How fast/reliably the layer opens Gmail in Tabbit, per LLM model. | `… uv run python3 tests/manual/bench_open_gmail.py` |
| **`reset_env.py`** | Between-run reset (OFF the timed path): restart 腾讯会议, cancel leftover test meetings, clear the 飞书 draft. Run before re-running the meeting flow. | `uv run python3 tests/manual/reset_env.py` |
| **`actuation_chat_harness.py`** | P2 end-to-end: send a REAL message through the layer (opt-in). | — |
| **`deepseek_computer_use.py`** | Free-form DeepSeek-driven computer-use demo over the layer. | — |
| **`actuation_flamegraph.py`** | Flame graph + per-step / per-state timing for an actuation trace. | — |
| **`skills/`** | Per-app operation manuals (`lark.md`, `tencent-meeting.md`) — progressively injected the first time focus lands on that app. Knowledge, not scripts. | — |

## `bench_cases.py` — the suite

One generic DeepSeek-V4-Flash loop drives every case; cases differ **only in data** (a task prompt,
a deterministic `verify()`, an optional `reset()`/`cleanup()`). Every case gets the SAME app-agnostic
tools — `activate` / `ax_snapshot` / `ax_find` / `ocr_locate` / `clickxy` / `ax_set_value` /
`ax_press` / `type_text` / `key` / `done` — so **the LLM does the thinking; the tools are just hands
and eyes**. Nothing is scripted to a task; change the prompt and the same tools solve the new task.

| Case | App | Tests | Tier |
|---|---|---|---|
| `calc` | Calculator | AXButton press + OCR-verify a pixel display | solid |
| `calc-scientific` | Calculator | **menu-bar nav** (显示 → 科学) + OCR verify | solid |
| `calc-programmer` | Calculator | **menu-bar nav** (显示 → 程序员) + OCR verify | solid |
| `browser` | Tabbit | identify the address bar by position → `ax_set_value` + enter; verify real navigation | solid (slow) |
| `textedit` | TextEdit | `ax_set_value` into a document AXTextArea | solid |
| `textedit-keytype` | TextEdit | **key-driven char-by-char typing** (key h/e/l/l/o — Unicode `type` doesn't land here) | solid |
| `textedit-paste` | TextEdit | paste a seeded clipboard (cmd+v) into the doc | solid |
| `textedit-clear` | TextEdit | clear the doc (cmd+a + delete) → verify empty | solid |
| `textedit-newwindow` | TextEdit | **window management** (cmd+n) → window-count verify | solid |
| `textedit-fonts` | TextEdit | **menu-bar nav** (格式 → 显示字体) → Fonts panel (window-title oracle) | solid |
| `finder` | Finder | a real keyboard shortcut (`cmd+alt+l`); reset closes stale windows | solid (slow) |
| `sysprefs` | System Settings | OCR-locate + click a top-level pane (蓝牙) | ~85% (hard nav) |
| `sysprefs-screenrec` | System Settings | nested 隐私 → 录屏与系统录音 nav; verify **no toggle was flipped** | ~85% (hard nav) |
| `vscode-palette` | VSCode | `cmd+shift+p` command palette | solid |
| `vscode-menu-palette` | VSCode | **Electron menu-bar nav** (查看 → 命令面板) | solid (slow) |
| `wechat-open` | WeChat | **AX-poor app**: OCR search + `type_text` + region-gated OCR verify | solid |
| `clipboard-paste` | TextEdit→Tabbit | **cross-app + clipboard**: cmd+a/c → switch app → cmd+v | solid (slow ~60s) |
| `lark-open` | 飞书 | disambiguate the right chat row by container/region | ~85% (hard nav) |

> Plus more reliable Calculator + TextEdit variants (28 cases total — see `grep '"name"'` in the source):
> calc-programmer · **calc-keytype / -divide / -subtract / -add / -multiply2 / -decimal** (full keyboard
> arithmetic; `+` uses `shift+=`) · **calc-copy** (→clipboard) · **calc-to-textedit** (cross-app) ·
> **textedit-keytype** (per-char `key` — Unicode `type` doesn't land in TextEdit) · **textedit-copy /
> -cut / -paste** (clipboard trio) · textedit-clear · textedit-newwindow.

Interaction patterns exercised across the 28: AX-button-press, AX-set-value, OCR+clickxy, key-combo,
**key-driven typing**, **paste/clipboard**, **window management**, OCR+`type_text`-into-search, **menu-bar
navigation** (AXMenuBarItem→AXMenuItem, pressable directly via AX — works on native AND Electron apps),
**cross-app + clipboard**, and oracle kinds (AX field value / window title / window count / region-OCR /
clipboard-via-AX / toggle-state). The three nav/disambiguation cases (`lark-open`, `sysprefs`,
`sysprefs-screenrec`) are genuinely harder (model visual disambiguation + macOS sidebar-click variance)
and sit ~85%; the rest are solid (some slow — the heavy-AX apps Finder/Tabbit/VSCode have large trees,
and Finder/Tabbit can degrade under back-to-back runs, so run those sparingly). Don't chase 100% on the
nav cases by over-prescribing the hint — over-scripting the model's decision is what breaks generality.

### Constraints (hard)

- **Only DeepSeek-V4-Flash** (`deepseek-v4-flash`), text-only, `thinking: disabled`.
- **Reset after every run** so a green run today is green tomorrow (filesystem/AppleScript/AX/OCR
  oracles, never the same AX read the model just drove where avoidable).
- **No scripting the LLM's task decisions.** Per-app *skills* (markdown manuals) and generic *tools*
  (`type_text`, position-tagged element lists) are fine; baking a specific task's steps into a tool
  is not — that defeats the benchmark.

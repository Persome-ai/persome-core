# Proactive Follow-up Engine — design spec

> **Provenance.** 本设计 spec 成文于 Persome 产品 **Mens**（原生 macOS app）的开发中；文中出现 *Mens* 处指"某个 Persome 驱动的产品 / 预测器实例"，个别指向 Mens 代码库的路径/机制是**说明性示例**。属于 persome-core 的 daemon 部分（记忆 / 意图 / 检索 / 捕获）直接适用。

**Date:** 2026-06-25
**Status:** implemented (engine + seams + §6 oracle green) — the pure planner/rules/gate
(`MensKit/FollowUp*.swift`), the app coordinator (`FollowUpCoordinator`), the capability resolver +
artifact read-back (`MensKit/CapabilityResolver.swift`), and the live seams
(`LiveFollowUpSeams.swift`) are in; routing is wired into `ContextSentinel` and **the whole context chain
defaults ON** (`contextEnabled` + `contextProactiveEnabled` + `contextFollowUpEnabled` all default-true)
so a fresh install is genuinely open-the-box active — the OS Accessibility/Screen-Recording consent
prompt is the effective capture gate, and turning any flag off falls back to the generic todo path. The
executor is wired live (`LiveFollowUpExecutor.live()`): the
**vision computer-use MCP is a default dependency** in the agent's own config, so the capability ladder's
vision floor is **always available** and the executor runs the goal through a one-shot `claude -p` (goal
via stdin) whose configured screen tools create the meeting, then reads back the join URL; a degrade to
local-only happens only when that run actually FAILS, never because "no tier exists". **Remaining
on-device wiring** (out of the offline gate, like the daemon launchd lifecycle): the daemon-side
`when_text → start_at/end_at/time_confidence` resolution (the consuming-product Swift side decodes it
leniently and falls back to `when_text` today) and the daemon-focus query that supplies
`focusedCounterpart` to enable the in-chat paste (today nil ⇒ outward safely downgrades to clipboard).
The actual screen automation + clipboard/⌘V are OS effects verified on-device. 见相关实现计划。
**Scope:** Generalize the proactive layer from **识别即推 (recognize → propose a todo)** to
**识别即办 (recognize → carry out a follow-through and place the result back into the user's
context)** — driven by ambient screen-observed intent, executed by the existing agent-dispatch
engine, with a human gate on every outward action. Builds on the context daemon (recognition),
the consuming-product proactive surface (`ContextSentinel`), agent dispatch (execution), and EventKit
sync (见相关设计，the calendar/reminder sinks).

> **The north star — a system, not a use case.** The motivating story is: *A messages B "明天下午三点
> 聊一下", B replies "好", and B's product immediately creates a meeting in B's usual tool, names it
> after the meeting with A, copies the join link to the clipboard, and pastes it into the chat with A
> for B to send.* This spec exists to make that story work **perfectly while refusing to overfit to
> it.** The meeting story must fall out as **data through a general engine**, not as a hand-built
> chain. The acceptance bar is the regression set in §6: sibling scenarios (share-a-doc, answer-free/busy,
> a deadline announcement, an email call-invite) must work through the **same four seams** with only
> data/config changing — **no new code path per scenario, and no `if kind == "meeting"` anywhere in the
> engine.** If narrowing any seam to "meeting" turns a §6 scenario red, the seam is correct.

---

## 1. Goals

1. **One engine, four seams (the abstraction).** A recognized, actionable intent flows through:
   **(A)** intent → a structured `FollowUp{goal, capabilities, sinks, gate}`; **(B)** a capability
   ladder that resolves the best available executor for the goal; **(C)** a set of output **sinks**
   the result is delivered into; **(D)** a gate policy keyed on *outward-ness*, not on kind. The
   meeting story is one row of data through these four seams.
2. **Reuse the substrate.** Recognition (daemon), proactive surfacing + dedup + feedback
   (`ContextSentinel`), execution (agent dispatch + skills/MCP), and the calendar/reminder sinks
   (`CalendarSyncCoordinator` / `EventKitSyncer` / `EventKitMapping`) already exist. The engine wires
   them; it does **not** re-implement them, dispatch, finalize, or re-key `dispatchOrder`.
3. **Outward actions are always human-gated.** Anything that leaves the user's machine or writes into
   another app (paste into a chat field, a reply draft, a "send") is **placed, never sent** — a human
   action (the user pressing Enter, or an explicit confirm) is the gate. Pure-local, reversible sinks
   (the in-app calendar/reminder/task) may auto-apply under existing 识别即推 semantics.
4. **Provider/app-agnostic execution with graceful degradation.** Goal → executor resolves
   **skill/API → AX action → vision computer-use MCP**, picking the highest tier available for the
   target. No provider or app name is hard-coded in the engine; the click MCP is the universal floor.
5. **Anti-overfit is an enforced invariant, not a guideline.** The §6 regression scenarios are the
   oracle; they are encoded as `--selftest` scenarios + `swift test` so a meeting-shaped narrowing of
   any seam fails the gate (per the repo's "build a measurable oracle first" rule).

## 2. Decisions (locked with the user)

| Decision | Choice | Consequence |
|---|---|---|
| Framing | **A general follow-up engine; the meeting case is its first *instance*** | No `if kind == "meeting"`. Meeting-specific values (tool, title template, target field) are **data** produced by seam A, not branches. |
| Recognition coverage | **Universal screen-capture** (daemon AX/OCR) | Any IM/email/app the daemon can see is a signal source — WeChat/Feishu/Slack/iMessage/Mail. No per-IM API integration required for the *signal*. |
| Confirmation semantics | **The user's own natural action is the gate** | B's "好" is the human-in-the-loop confirm — the product does **not** pop a second dialog for it. For the outward placement, B pressing **Enter** is the send gate. |
| Outward placement | **Place, never auto-send** | Result lands in clipboard / the focused chat field / a reply draft; the product never presses Send/Enter. Defends against wrong-field/wrong-link and the "send on the user's behalf" boundary. |
| Execution mechanism | **Agent task + skill/MCP** (preferred) **→ vision computer-use MCP (fallback)** | The consuming product dispatches a build-the-follow-up agent task; it gets the goal's capabilities via launch-time MCP config. GUI control is an **off-the-shelf click MCP** wired to the agent — *not* product-authored automation. |
| GUI tier status | **Default dependency, lowest preference, provider-agnostic** | The vision computer-use MCP ships as a **default dependency** in the agent's config, so the vision floor is **always available** — the ladder still prefers a real skill/API when present, but falls to vision for anything without one (e.g. 腾讯会议 personal, WeChat — AX-poor). Brittle/slow → **read-back verification mandatory**; on a real run failure, fall back to local-only sinks and **do not** place a bad result outward. |
| Time resolution | **Daemon-side, folded into the recognition LLM call** | `when_text ("明天下午三点")` → absolute `start/end` ISO + a `time_confidence`, **zero extra tokens**; the raw `when_text` is preserved so the user can sanity-check. |
| Latency | **Event-driven (SSE), not the 60s poll** | "立马" requires the event-driven path (`handle`/`flushBurstNow`), per the "事件驱动为主" principle. The agent round-trip is still seconds-to-tens-of-seconds — "fast", not "instant". |
| Calendar/Reminder sinks | **Reuse EventKit sync as-is** | A timed follow-up → calendar event; a no-time todo → Reminder. Already built; the engine just emits the task. |

## 3. Non-goals

- **No launchd / while-quit execution.** Same MVP boundary as everywhere else — the engine acts only
  while the consuming product is open. Capture stops on app quit (`stopForAppTermination`).
- **No product-authored GUI automation.** The click/computer-use capability is an **existing MCP** wired
  to the dispatched agent. We do not write pixel-clicking or app-driving code.
- **No auto-send, ever.** The engine never presses Send/Enter, never submits a form, never posts.
  This is a hard line (mirrors the global safety boundary).
- **No per-provider Swift API clients in v1.** The API/skill tier rides the agent + its skills/MCP.
  A native daemon-side API client (for sub-second latency) is a possible later optimization, explicitly
  deferred.
- **No new recognition model.** Seam A consumes the daemon's existing open-set intents; it adds time
  resolution + a goal/delivery mapping, not a new recognizer.
- **No cross-machine coordination.** This is B's machine reacting to B's screen. A's side is out of scope.

---

## 4. Architecture — the four seams

```
 daemon: observe (AX/OCR) → recognize intent {kind, text, when_text→start/end, with, importance, urgency, confidence}
   │  (event-driven push, not 60s poll)
   ▼
 A. Intent → FollowUp        seam A: classify "actionable & how to follow through"
   │     FollowUp{ goal: NL string, capabilities: [Capability], sinks: [Sink], gate: Gate }
   ▼
 D. Gate policy              seam D: outward? → require human gate. local-only? → may auto-apply.
   │     (the user's "好" already satisfied the *recognition* confirm; outward placement waits for Enter)
   ▼
 B. Capability resolution    seam B: for each capability, pick skill/API → AX action → vision MCP
   │     → dispatch ONE agent task (existing engine), launch env wires the chosen MCP/skill set
   │     → agent executes the goal, READS BACK the produced artifact (e.g. join_url), returns it
   ▼
 C. Output sinks             seam C: place the artifact into each sink
         { mensCalendarEvent | reminder | inAppTask | clipboard | pasteIntoFocusedField | replyDraft }
```

### 4.A Intent → `FollowUp` (the "what should happen" layer)

- Input: a daemon intent (open-set `kind`, `text`, resolved `start/end`, `with`, importance/urgency/
  confidence, evidence).
- Output: a pure value
  ```
  FollowUp {
    goal: String                 // natural-language objective for the agent, e.g.
                                  // "Create a video meeting titled '与 A 的会议' at 2026-06-26T15:00,
                                  //  return only the join URL."
    capabilities: [Capability]   // e.g. [.createMeeting], [.fetchShareLink], [.readCalendarFreeBusy]
    sinks: [Sink]                // e.g. [.mensCalendarEvent, .clipboard, .pasteIntoFocusedField(target)]
    gate: Gate                   // .autoLocal | .humanPlacement | .explicitConfirm
    grounding: String            // why-this-surfaced block (reused from the proposal writer)
  }
  ```
- **Principle in the prompt, specifics in progressive code** (原则：提示词写短小、app-agnostic 的原则，
  具体走条件化 / 渐进式代码).
  The goal text is written by an LLM proposal writer from a short, app-agnostic principle ("turn a
  confirmed commitment into an executable follow-through goal + name it from context"); the
  capability/sink/gate selection is small deterministic code keyed on **abstract features** of the
  intent (does it reference a time? a person? a document? an answerable question?), **never** on a
  literal kind string like `"meeting"`. A goal-template table may key on `kind` for the *wording*, but
  a missing/unknown kind must degrade to a generic "surface as a todo" follow-up, never crash and never
  hard-require a known kind.
- Fail-open: if the writer fails, degrade to a deterministic template + a local-only todo sink (an LLM
  failure never blocks a follow-up), exactly as the current `DeepSeekProposalWriter` does.

### 4.B Capability resolution (the "how" layer — the 3-tier ladder, generalized)

- A `Capability` (`.createMeeting`, `.fetchShareLink`, `.readCalendarFreeBusy`, `.bookRoom`, …) resolves
  to the highest available **executor tier** for the current target:
  1. **skill / API** — a named skill or MCP that calls a real API (Feishu/Lark via the already-integrated
     lark skills; Zoom; 腾讯企业版). Clean + most robust.
  2. **AX action** — drive an AX-friendly native app by accessibility element (press, set-value) reusing
     the daemon's AX pipeline (read → also act). No pixels.
  3. **vision computer-use MCP** — an **off-the-shelf** screen-control MCP wired to the agent; screenshot →
     locate → click. The universal floor for AX-poor apps (WeChat, 腾讯会议 desktop — the same apps that
     already need OCR). Brittle + slow.
- Resolution is **provider/app-agnostic**: the engine picks a tier by *what's available*, not by a
  hard-coded app name. Wiring = the consuming product launches the dispatched agent with the resolved
  skill/MCP set in its launch config (`ShellEnv` / the agent command template + `--mcp-config`), the same
  seam that already resolves PATH and env.
- **Read-back verification is mandatory for tiers 2–3** (and good practice for 1): the agent must read the
  produced artifact (e.g. the actual join URL from the created meeting) and return it; an unverifiable
  result is treated as failure → no outward placement, local-only sinks only.

### 4.C Output sinks (the "where it lands" layer — the genuinely new general surface)

A `Sink` is a delivery channel for the produced artifact. The engine owns the rails; each sink is small:

| Sink | Mechanism | Outward? | Status |
|---|---|---|---|
| `mensCalendarEvent` | emit a timed task → EventKit forward-sync | no (own data) | ✅ built |
| `reminder` | emit a no-time todo → EventKit forward-sync | no (own data) | ✅ built |
| `inAppTask` | `store.add` a (possibly held) task on the board | no | ✅ built |
| `clipboard` | `NSPasteboard` write | no (local) | 🔨 trivial |
| `pasteIntoFocusedField(target)` | set the focused AX text element's value / paste, **iff** the focused element matches `target` (e.g. the chat with A, identified via the daemon's focused-window/element + conversation context) | **yes** | 🔨 new |
| `replyDraft` | place a drafted reply into the focused compose field (email/IM) | **yes** | 🔨 new |

- **Outward sinks are gate-controlled (seam D).** `pasteIntoFocusedField` / `replyDraft` only ever *place*
  text; they never submit. If the focused element no longer matches `target` (the user switched away),
  the placement is **skipped** and the artifact stays on the clipboard with a notification — never paste
  into the wrong field.
- Sinks are independent and best-effort: a failed outward paste must not undo the local calendar/reminder
  that already succeeded.

### 4.D Gate policy (the "when" layer — by outward-ness, not kind)

- `Gate.autoLocal` — all sinks are local/reversible (own calendar, reminder, in-app task) → may
  auto-apply, held under existing 识别即推 semantics (the held `.context` task still costs no tokens until
  Run).
- `Gate.humanPlacement` — has an outward sink. The product performs execution + local sinks, then **places**
  the outward artifact (clipboard + paste) but **stops at the user's natural action** (Enter to send). The
  user's prior confirm ("好") authorized the *recognition*; the Enter authorizes the *send*.
- `Gate.explicitConfirm` — low time-confidence, ambiguous target, or a high-impact action → surface a
  notification first (the existing sentinel banner) with the parsed details for one-tap confirm before any
  outward placement.
- The policy is a pure function of (sink outward-ness, time_confidence, impact) — **kind never appears.**
  This is the seam that operationalizes the global safety boundary ("sending any message on
  the user's behalf" requires permission; outward placement + no auto-send keeps us inside it).

---

## 5. The meeting story as the first instance (data, not a chain)

The motivating story is the four seams filled with one row of data — **no engine code is meeting-aware**:

| Seam | Value for the meeting story |
|---|---|
| Recognition | daemon: `kind=meeting`, confirmed via session trajectory ("A proposed; B replied 好"), `when_text="明天下午三点"` → `start=2026-06-26T15:00`, `with=["A"]`, high confidence |
| A → FollowUp | `goal="Create a video meeting titled '与 A 的会议' starting 2026-06-26T15:00; return only the join URL."`, `capabilities=[.createMeeting]`, `sinks=[.mensCalendarEvent, .clipboard, .pasteIntoFocusedField(chat-with-A)]`, `gate=.humanPlacement` |
| D gate | outward sink present → `.humanPlacement`: execute + local sinks now, place link, wait for B's Enter |
| B capability | `.createMeeting` → Feishu skill if available ↘ else vision MCP on 腾讯会议 desktop; agent reads back the real `join_url` |
| C sinks | calendar event at 15:00 (+ optional Reminder "准备与A的会议"); `join_url` → clipboard; if focus is the chat with A, paste it in |
| Finish | B presses Enter → link goes to A |

If 腾讯会议 (personal, no API, AX-poor) is the target, only seam **B** changes tier (skill → vision MCP);
**A/C/D and the rest of the pipeline are identical.** That tier swap touching nothing else is the proof
the chain isn't meeting-shaped.

---

## 6. Regression scenarios — the anti-overfit oracle (must all pass through the same seams)

Encoded as `--selftest` scenarios (engine) + `swift test` (pure seams). Each is the **same four seams**
with different data. **Any of these going red when a seam is narrowed toward "meeting" is the signal the
generalization broke.**

| # | Scenario | A goal | B capability | C sinks | D gate |
|---|---|---|---|---|---|
| R1 | **Meeting (given)** | create meeting, return link | meeting skill ↘ vision MCP | calendar + clipboard + paste-to-A | humanPlacement (好→Enter) |
| R2 | A asks "把 X 文档发我" | fetch X's share link | drive/doc skill | clipboard + paste-to-A | humanPlacement |
| R3 | A asks "周五有空吗" | check free/busy, draft an answer | calendar read | replyDraft into the chat | humanPlacement (Enter) |
| R4 | Group sets "明天交周报" | build deadline + prep task | none (local only) | reminder + in-app AI task | **autoLocal** (auto) |
| R5 | Email invites a call | same as R1, **signal source = Mail** | meeting skill | calendar + replyDraft to email | humanPlacement (no auto-send) |
| R6 | Unknown/odd `kind`, actionable text | generic "surface as todo" | none | in-app held task | autoLocal |

- **R6 is the degrade guard:** an unknown kind must NOT crash or be dropped — it falls to a generic
  todo follow-up. (A seam A that hard-requires `kind ∈ {meeting,…}` fails R6.)
- **R4 vs R1 is the gate guard:** R4 is local-only → auto; R1 is outward → human-gated. (A gate policy
  keyed on kind instead of outward-ness fails one of them.)
- **R5 is the signal-source guard:** swapping IM→Mail changes only the recognition source; the engine is
  unchanged. (A pipeline that assumes "IM" fails R5.)
- **R2 vs R1 is the capability guard:** different capability + sink, same machinery. (A `createMeeting`-only
  executor path fails R2.)

Additionally, a **static guard**: a `swift test` (or a grep-based selftest assertion) asserts the engine
source contains **no literal `"meeting"` / provider-name branch** in seam code — the meeting specifics
must live only in data/goal-templates/config.

---

## 7. Security & safety boundary

- **No auto-send.** The engine places into clipboard / focused field / draft and stops. The user's Enter
  is the only thing that sends. This keeps every outward action inside the global boundary ("sending a
  message on the user's behalf" requires explicit per-action human consent).
- **Outward placement is target-verified.** `pasteIntoFocusedField(target)` checks the live focused element
  still matches `target` at placement time; mismatch → skip + keep on clipboard. Never paste into a window
  the user switched to.
- **GUI tier is sandboxed + read-back verified.** The computer-use MCP is scoped to the target app for the
  duration of the build-task; the agent must read back the produced artifact; an unverified result yields
  local-only sinks (calendar/reminder) and **no outward placement** — never push an unverified/wrong link.
- **Capability firewall on the goal text.** As with the proposal writer, the LLM writes only the *goal
  brief*; code owns the rails (which sinks, which gate). Screen-derived text feeding the goal is wrapped in
  the existing untrusted-data fence (`sanitizeFence`) — observed content is **data, not instructions**
  (it can never escalate the gate or add a sink).
- **Permissions.** Reuses Accessibility (focused-field placement / AX actions) + Screen-Recording (vision
  tier / OCR) already requested by the daemon/voice; calendar/reminder sinks reuse the EventKit TCC. No new
  entitlement; new usage strings only if a new TCC surface appears. The whole engine is behind an explicit
  Settings toggle (default off), gated additionally on `contextEnabled` + sign-in.
- **Never logged.** Produced links/drafts and the screen text behind them never enter the task log
  (only a short, content-free status label), matching the supervisor/context rules.

---

## 8. What's new vs reused

| Concern | Owner | Status |
|---|---|---|
| Recognize confirmed intent + `when_text→start/end`, `with` | daemon recognizer | ✅ mostly; add time resolution + surface `when_text/with/start/end` through `ChronicleIntent` (currently dropped at the Swift boundary) |
| Event-driven push (no 60s poll for actionable intents) | daemon SSE + `ContextSentinel` | 🔨 wire the event path |
| Seam A: intent → `FollowUp{goal, capabilities, sinks, gate}` | **consuming product** (new, pure + testable) | 🔨 new — replaces the meeting-specific proposal branch |
| Seam B: capability → executor tier + agent-MCP wiring | **consuming product** (`ShellEnv` launch config) | 🔨 接线 + tier resolver; MCPs/skills are off-the-shelf |
| Seam C: output sinks (clipboard / paste / draft) | **consuming product** (new) | 🔨 new general surface; calendar/reminder/task reused |
| Seam D: gate policy by outward-ness | **consuming product** (pure) | 🔨 new, small |
| Capture join_url / artifact from agent output | **consuming product** | 🔨 new (parse agent result) |
| Calendar event + Reminder | EventKit sync | ✅ reused unchanged |
| Proactive surface / dedup / feedback log | `ContextSentinel` | ✅ reused; the FollowUp rides the existing dedup + feedback machinery |

**Net new work is three small things + glue:** seam A (intent→FollowUp), seam C (output sinks), seam
D (gate) — plus capturing the artifact and wiring the agent's MCP. Everything "hands-on-screen",
calendar/reminder, and recognition is reused or off-the-shelf.

## 9. Phasing

- **Phase 1 — local-only, no outward, no GUI tier.** Seam A + C (calendar/reminder/in-app task sinks only)
  + D (`autoLocal` path) + daemon time resolution. Tier-1 (skill/API) execution where it exists; otherwise
  the follow-up is a held in-app task (识别即推 today, just goal-shaped). Ships the generalization with
  **zero new outward risk**. Covers R4/R6 fully and R1/R2/R3/R5 minus the outward placement.
- **Phase 2 — outward placement + capability ladder.** Clipboard + `pasteIntoFocusedField` + `replyDraft`
  sinks, the `humanPlacement` gate, AX-action + vision computer-use MCP tiers, read-back verification.
  Completes R1/R2/R3/R5 end-to-end (the full meeting story).
- **Phase 3 (deferred) — native daemon API clients** for sub-second latency on hot providers; learned gate
  windows; richer target detection.

## 10. Open questions (decide before the plan)

1. **Phase-1 first provider for tier-1 execution:** Feishu/Lark (already integrated) is the lowest-friction
   pilot; 腾讯会议 personal forces the vision tier (Phase 2). Recommend Feishu first.
2. **Target-field identification precision** for `pasteIntoFocusedField` — how strictly must "this is the
   chat with A" match before we paste? (Proposal: require the daemon's focused-window/conversation to name
   the same counterpart `with`; otherwise downgrade to clipboard-only + notification.)
3. **Reschedule semantics** (out of the dedup path): a meeting whose time changes in chat should **update**
   the existing event (via `externalEventID`), not create a second — this is "detect a change", the inverse
   of "suppress a duplicate". Confirm it's a Phase-2 item.

---

## Related specs

- The calendar/reminder sinks (seam C, reused) — 见相关的 EventKit 同步设计。
- The in-app board (in-app task sink) — 见相关的日程看板设计。
- The context daemon + sentinel (seams A/D substrate) — 见相关的 context 集成设计。
- The feedback loop the FollowUp rides — 见相关的 context-feedback 契约设计。

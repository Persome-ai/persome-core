---
app: WeChat
bundles: com.tencent.xinWeChat
summary: AX-blind AND OCR-blind (renders black to capture) — drive by KEYBOARD ONLY via ⌘F search
aliases: 微信, Weixin
surface: gui
tiers:
  t0: []             # no agent-native interface (and Persome never sends on WeChat anyway → checkpoint)
  t1: ax-blind       # AX-blind AND OCR-blind → T2 floor degrades to keyboard-only (⌘F search)
---
# Skill: WeChat / 微信 (com.tencent.xinWeChat) — keyboard-only

WeChat 4.x is the **worst case**: its window exposes **almost nothing to AX** (a snapshot returns only
the menu bar + the window frame — no chat list, no input box, no messages), AND its content area
**does not OCR** — it renders to a surface that screen capture gets back as black, so `ui_ocr_locate`
finds nothing either. You cannot SEE or READ WeChat. So you cannot locate things by id or by pixel.

**Therefore: drive WeChat by KEYBOARD ONLY.** Keyboard events still reach whatever WeChat has focused,
even though you can't see it. Use WeChat's own search to navigate deterministically rather than clicking.

## Open a chat and type into it (keyboard-only)
1. `ui_activate("WeChat")` — bring it to the front (keyboard verbs act on the focused app).
2. `ui_key("WeChat", "cmd+f", note=…)` — focuses WeChat's search box.
3. `ui_type("WeChat", "<exact contact or group name>", note=…)` — types the name into search.
4. `ui_key("WeChat", "enter", note=…)` — opens the **top** search result; its message input is now focused.
5. `ui_type("WeChat", "<message text>", note=…)` — types into the focused message input.
6. To SEND: `ui_key("WeChat", "enter", note=…)` (Enter sends by default in WeChat).

`ui_type` posts real key events to the focused field, so it works even though WeChat has no AX text
field. Use an **exact, unique** name in step 3 so the top result is unambiguous.

## Hard limits — you are blind here, so be careful
- You **cannot verify** the result (no AX, no OCR). After sending, tell the user you can't confirm it
  landed and ask them to check — don't claim success you can't see.
- If `⌘F` does NOT focus search on this build, step 3 would type the name into the **currently-open
  chat** and step 6 would send it there — a wrong-recipient misfire. Because you can't see, you can't
  detect this. Only run the full send when the user has explicitly accepted that risk for this recipient.
- A send (`enter` in a messaging app) and typing are gated as side-effects → the user is asked to
  confirm. NEVER send without that confirmation.
- Prefer the user opening the target chat themselves when zero misfire risk is required; then you only
  `ui_type` + `ui_key("enter")` into the chat they chose.

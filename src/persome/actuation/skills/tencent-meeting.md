---
app: 腾讯会议
bundles: com.tencent.meeting
summary: Tencent-drawn home (pixels → OCR for the one tap); the booking form is full AX once open
aliases: Tencent Meeting, meeting, 腾讯会议 Tencent Meeting
surface: gui
tiers:
  t0: []             # no agent-native meeting API registered (P1: lark-vc/zoom for other providers)
  t1: ax-mixed       # home is Tencent-drawn pixels (T2/OCR for the one tap); booking form is full AX
---
# Skill: 腾讯会议 / Tencent Meeting (com.tencent.meeting) — book a meeting

The main window is Tencent's own framework (WeMeetFramework): the **home-screen icon buttons are pixel
images AX can't read**, but the **「预定会议」 booking form, once open, is fully AX-readable/writable**
(lazy-loaded — the tree is built when the content appears). So: **OCR only for the one home-screen tap,
then pure AX inside the form.**

## Booking flow
1. `ui_activate("腾讯会议")`.
2. The home «预定会议» is a pixel button → `ui_ocr_locate("腾讯会议", "预定会议")` for its screen coords →
   `ui_click_xy` to open the form. (After it opens, a click's AX diff lists the newly-appeared actionable
   elements — use those ids directly, no need to re-snapshot.)
3. **Title**: in the snapshot/diff find the `AXTextField` (placeholder「请输入会议主题」) →
   `ui_set_value` it to the meeting title.
4. **Date / time (only if you must set a specific time)**: the date field is a **stepper**, not a
   button — AXPress (`ui_click`) does NOTHING. Check the element's `actions`: it advertises
   `AXIncrement` / `AXDecrement`. Use `ui_perform(id, "AXIncrement")` once per day to advance the date
   (e.g. +2 days = two AXIncrement calls), reading the diff each time to confirm the date moved. Do the
   same for the hour/minute steppers if present. **Most "send me a meeting link" tasks do NOT need an
   exact time** — the link is a room URL independent of the scheduled time, so unless the user pinned a
   specific time, skip this step and keep the default. (Persome already records the agreed time separately.)
5. **Submit**: find the bottom `AXButton "预定"` → `ui_click` it.
6. **Get the link**: the result page's whole invite (incl. `https://meeting.tencent.com/…`) is AX-readable
   text — read it via `ui_snapshot` and pull the link. Don't click the pixel «复制全部信息» button.

## Traps
- The date/time fields are **steppers** (AX, but no AXPress) — drive them with `ui_perform` +
  `AXIncrement`/`AXDecrement`, never repeated clicks or AppleScript. If a control truly has no AX
  actions (a pixel dropdown), keep the default rather than fighting it.
- AX `ui_set_value` / `ui_click` (AXPress) / `ui_perform` don't move the cursor or steal focus, so they
  run quietly; only the one home-screen `ui_click_xy` flicks the cursor (it warps back after).

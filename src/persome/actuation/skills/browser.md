---
app: Google Chrome
bundles: com.google.Chrome, com.brave.Browser, com.microsoft.edgemac, org.chromium.Chromium
summary: Open web tasks with ui_open_app (off-screen no-steal stage); drive the stage by its app_pid
aliases: Chrome, Brave, Edge, Chromium
surface: gui
tiers:
  t0: []             # no agent-native web driver registered yet (P1: cmux browser / playwright)
  t1: ax-rich        # Chromium exposes an AX tree → ui_find + ui_perform on the app_pid stage
---
# Skill: Browsers (Chrome/Brave/Edge) — no-steal web tasks

For any WEB task (open a page, fill a form, create a meeting link, research), **do NOT `ui_activate`
the user's browser** — that would take over their screen. Instead open a fresh instance the no-steal
way:

1. `ui_open_app("Google Chrome", "<url>", note=…)` — Persome spawns its OWN instance on an **off-screen
   virtual display** and returns `{strategy:"virtual_stage", app_pid, window_id, window_bounds:[x,y,w,h]}`.
   The user's real screen never changes.
2. **Drive that instance by its `app_pid`** — pass the returned `app_pid` to EVERY verb
   (`ui_snapshot`/`ui_find`/`ui_click`/`ui_click_xy`/`ui_type`/`ui_key`). The app NAME alone is
   ambiguous when the user also has the browser open, so without `app_pid` the actuator may hit the
   USER's window instead of the stage. (This is the #1 mistake — always thread `app_pid`.)
3. Coordinates for `ui_click_xy` must fall inside `window_bounds` (which sit on the virtual display, off
   the user's screens). The page's own AX (Chromium exposes web-content AX) is readable via
   `ui_snapshot(app, app_pid)` / `ui_find(app, query)` — prefer `ui_click`/`ui_set_value` by element id
   over raw coordinates whenever the snapshot exposes the control.
4. When finished, `ui_close_app(app_pid)` to release the off-screen display.

## Notes
- Reading the page (snapshot/find) is safe and unprompted; a navigation/submit you announce as a
  side-effect is gated.
- The stage is the agent's own session — it has no access to the user's cookies/logins. If the task
  needs the user's logged-in browser session, that's the single-instance `borrow` path instead (Persome
  asks the user to lend it), not a stage.

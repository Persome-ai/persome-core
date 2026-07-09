---
app: Feishu
bundles: com.electron.lark, com.bytedance.lark
summary: Electron, AX-rich — but type is dropped (use ui_set_value/⌘V); search needs a real input event
aliases: Lark, 飞书, 飞书 Feishu
surface: gui
tiers:
  t0: [lark-im, lark-calendar, lark-doc, lark-drive]   # agent-native — prefer for send/calendar/doc/drive
  t1: ax-rich                                           # in-app nav via ui_find + ui_perform/ui_set_value
---
# Skill: 飞书 / Lark (com.electron.lark) — paste a link into someone's chat (never send)

Lark is Electron; the actuator force-enables `AXManualAccessibility`, so the AX tree is rich and
`ui_find` / `ui_click` / `ui_set_value` work well. But three traps:

- **Text entry only takes `ui_set_value` (write the element's value directly) or ⌘V paste.** Char-by-char
  `ui_type` (synthetic keystrokes) is **dropped** by Lark. To put text in a field, `ui_set_value` it.
- **Search box**: `ui_set_value` changes the value but does **not** fire the search onChange → no results.
  If you must use search, after `ui_set_value` send a real input event (a `ui_key` char, or clear +
  ⌘V paste) so the results refresh.
- **Which chat am I in?** An empty message box (`AXTextArea`, width>100) carries no peer name in its
  AXValue — don't infer the current conversation from the input box. Read the conversation title / the
  message area instead.

## Paste a link into the chat with «<person>»
1. **Open that person's conversation** — the same name appears in many places (chat-list row, chat
   title, message history), so use `ui_find("Feishu", "<name>")`, which lists every match by
   container/region, e.g.:
   ```
   [40] AXStaticText "温子墨" container A / chat-list  visible  → the 1:1 chat row (this is the one)
   [41] AXStaticText "温子墨: [图片]…" container A / chat-list  hidden  (a preview, not an entry)
   [42] AXStaticText "温子墨" container B / chat-area  (the chat title/history, not an entry)
   ```
   Pick the row whose container is the **chat list**, whose label is **exactly the name** (not "name: …"
   preview), and that is **visible** → `ui_click_xy` its bbox center to open the conversation.
2. **Confirm you switched**: `ui_find`/`ui_snapshot` again and verify the chat title is this person.
3. **Paste the link**: find the message input `AXTextArea` (width>100) → `ui_set_value` it to the link.
   **Never press Enter, never click Send** — only place it in the input; the user sends it themselves.

## Safety
- Paste only into the authorized person's conversation; if you clicked the wrong person or couldn't
  confirm, **don't paste** — leave the link on the clipboard for the user.
- Never send a message, never click delete.

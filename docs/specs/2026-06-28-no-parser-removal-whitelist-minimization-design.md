# Remove the `no_parser` app-whitelist gate · whitelist-minimization principle

> **Provenance.** 本设计文档成文于 Persome 产品 **Mens**（原生 macOS app）的开发中；文中出现 *Mens* 处指"某个 Persome 驱动的产品 / 预测器实例"，个别指向 Mens 代码库的路径/机制是**说明性示例**。属于 persome-core 的 daemon 部分（记忆 / 意图 / 检索 / 捕获）直接适用。

**Date:** 2026-06-28
**Status:** implemented (behind `intent_recognizer.fast_generic_fallback`, default on)

## What / Why

The K1 fast path's `no_parser` gate was an **implicit app whitelist**: a capture whose frontmost
bundle had no registered per-app parser (`parsers._REGISTRY` = Feishu, WeChat, browsers) was dropped
(`OUTCOME_NO_PARSER`) before ever reaching the LLM. So the recognizer was structurally blind to every
other IM (Slack / Telegram / 钉钉 / Discord / Lark variants / …) — not because the content wasn't an
intent, but because the *app* wasn't on the list. That is exactly the kind of gate that overfits to
the apps we happened to write parsers for and silently caps coverage.

**Decision:** stop gating recognition on app identity. When no per-app parser matches, fall back to a
**generic parse** of the capture's already-rendered `visible_text` and let the precision-first LLM
judge — the same way every covered app is judged. Recognition is gated by *content* (the
`not_conversation` / `empty` structural gates + the LLM), never by an app allowlist.

## Design

`intent.event_source._generic_conversation(cfg, capture, bundle)`:
- Reads the capture's `visible_text` (the S1-rendered text — already chrome-folded by `s1_parser` /
  `generic_render`, and OCR-backfilled for AX-poor apps).
- Takes the **recent** lines (tail = current activity, capped at `_GENERIC_MAX_LINES = 40`) as
  `Message(sender=None, body=line, direction="unknown")`.
- **Never fabricates sender/direction.** A generic surface has no who-said-what geometry, so direction
  is honestly `"unknown"` (the LLM judges intent direction-agnostically — `received`/`sent`/`unknown`
  all flow through `intent_fast.system.md`). This is the explicit lesson from the Feishu
  direction-mis-attribution bug: a missing fact is `unknown`, not a guess.
- Returns `None` when the fallback is off or there's no usable text → the caller records the honest
  gate (`no_parser` when off, `empty` when no content). No fake message is ever invented.

`on_capture` now: `parser is None` (or a registered parser with no usable AX tree) → generic fallback
instead of the `no_parser` drop. WeChat (OCR-first) and Feishu/browser (structured) paths are
unchanged — they keep their richer per-app parse with real direction/sender.

## Bounding the cost (why "process more apps" doesn't flood the LLM)

Removing the whitelist means terminals / editors / arbitrary windows now reach the parse step too.
The blast radius is bounded by gates that already exist — **not** by re-adding an app filter:
- `empty` / `not_conversation` drop windows with no message-like content.
- the **seen-set + cold-start** prime: a window's existing content is baselined once, not re-fired.
- the **per-app exponential backoff** (`record_outcome`): an app that keeps producing 0 intents
  (a terminal, an editor) self-cools — its sampling rate decays, so steady-state LLM cost on
  non-chat apps tends to ~0 without any hard-coded exclusion.
- the **precision-first LLM**: "拿不准就返回空" — non-conversational text yields no intent.

Per-app attribution stays visible in `fast_path_ticks` (bucketed by `bundle_id`), so a misbehaving
app surfaces in telemetry and can be addressed with data, not a guess.

## Kill-switch / rollback

`intent_recognizer.fast_generic_fallback = false` restores the exact `no_parser` app-whitelist drop.

## Validation

- `tests/test_event_source.py`: generic fallback builds an unknown-direction conversation for an
  unregistered bundle; off → `None` (no_parser restored); empty text → `None`.
- intent-golden **deterministic** gate green; the broader intent/parser suite shows only the
  pre-existing `test_intent_recognizer` / `test_slowpath_incremental` failures (unrelated).
- Coverage of the *quality* of generic recognition on real non-whitelisted IMs is a production-data
  question (watch `fast_path_ticks` by bundle + the feedback loop), not a fixture one — don't claim a
  fixture proved it.

## Principle (recorded in agent-docs/design-philosophy-intent.md)

**Minimize whitelists.** A gate that admits only an enumerated set (apps, domains, kinds) overfits to
what we listed and silently caps generalization. Prefer **content/behaviour-based gates + the
precision-first LLM**, with cost bounded by stateful self-limiting gates (seen-set, backoff). A
whitelist needs an explicit justification (a hard safety/cost boundary that no content signal can
replace) — and even then, document it and keep it as small as possible. The remaining allowlist
(`domain_allowlist`, K2 browser scaffold) is empty/inactive by default for the same reason.

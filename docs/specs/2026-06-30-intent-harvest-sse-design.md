# 意图过期收割的实时 SSE（auto-close 收尾）设计

> **Provenance.** 本设计文档成文于 Persome 产品 **Mens**（原生 macOS app）的开发中；文中出现 *Mens* 处指"某个 Persome 驱动的产品 / 预测器实例"，个别指向 Mens 代码库的路径/机制是**说明性示例**。属于 persome-core 的 daemon 部分（记忆 / 意图 / 检索 / 捕获）直接适用。

**Date:** 2026-06-30
**Status:** Implemented (daemon-only; app side already merged in #364)
**Slug:** `intent-harvest-sse`

## 1. 为什么

#364「证据驱动自动关闭」让**两处**状态翻转变实时(SSE `intent/resolved`)：① 证据自动关闭(reconcile)、② 用户 PATCH 采纳/忽略。**第三处**——每日 23:55 + daemon 启动 catch-up 的**过期收割**(`expire_overdue_intents`)——当时记为 follow-up。本 PR 补上：让 harvest 翻转(`open/armed→expired`、`armed→dismissed`)也发 SSE，app 即刻撤掉对应陈旧建议卡，而不是等下次 reconcile poll。

## 2. 设计

- 三个 harvest leg(`expire_overdue` #546/#629、`expire_stale_armed` #532、`expire_stale_open` #612)**返回被收割的 ids**(原来返回 count；长度即 count，向后兼容地把 3 个调用点 + 测试改成 `len(...)`)。
- `session/tick.py:expire_overdue_intents` 对每个 id 调 `intent_store.publish_intent_status_change(id, new_status=, previous_status=, reason=)`(reason: `harvest_overdue`/`harvest_armed_ttl`/`harvest_ungrounded_ttl`)。best-effort(publish 自吞错)；只发被收割行,无行则不发。
- **app 侧零改动**：#364 的 `ContextSentinel.handleStatusChange` 已对**任一终态** new_status(resolved/consumed/dismissed/**expired**)删卡，harvest 发的 expired/dismissed 直接被它消费。

## 3. 不变量

- 收割逻辑/幂等性不变(各 leg 的 `WHERE status=...` 守卫不动)；只是把"翻了哪些行"透出来发 SSE。
- best-effort SSE：发布失败绝不影响收割或 daemon 启动/每日 tick。
- 无识别路径改动 → intent-golden 确定性档天然绿。

## 4. 验证

- `PERSOME_LLM_MOCK=1 uv run pytest tests/test_harvest_sse.py`(收割每行发对应 SSE + 无行不发 + 行真翻 expired)+ 既有 `expire_*` 收割/幂等测试(改 `len()` 后全绿)+ intent-golden 确定性档 + ruff。

## 5. 关键文件

- `src/persome/intent/store.py`(`expire_overdue`/`expire_stale_armed`/`expire_stale_open` 返 `list[int]`)
- `src/persome/session/tick.py`(`expire_overdue_intents` 发 SSE + 返 len)
- `src/persome/cli.py`(intent-restamp 调用点 len())；`tests/test_harvest_sse.py`(新)

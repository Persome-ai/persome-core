# Reducer 锚点保留 — 量化"去噪是否误伤具体事实",并据此决定是否改 reducer

> **Provenance.** 本设计文档成文于 Persome 产品 **Mens**（原生 macOS app）的开发中；文中出现 *Mens* 处指"某个 Persome 驱动的产品 / 预测器实例"，个别指向 Mens 代码库的路径/机制是**说明性示例**。属于 persome-core 的 daemon 部分（记忆 / 意图 / 检索 / 捕获）直接适用。

> Status: design. 依赖产线管线 harness（`2026-06-25-longmemeval-production-pipeline-design.md`）。
> 触发：生产 pipeline A/B 显示压缩后 single-session-user QA −0.233（两次跑一致）——具体事实题
> 在压缩后丢分。本 spec 的目标是**先证伪/确认这是不是真 reducer 问题**，再决定改不改生产。

## 1. 背景 + 一个容易误判的点

生产记忆链是 `AX 捕获(噪声) → timeline(抽 verbatim 进结构化 block) → reducer(压成 summary+sub_tasks)
→ classifier(抽实体) → FTS`。**压缩在生产是必需的**：原始输入是 GB/天 的 AX 噪声，不压没法存/检索；
reducer 的活是"从噪声里抽信号"。benchmark 的 verbatim 基线（无损干净会话）对生产其实不公平——生产
从没有那个东西。

**但 reducer 其实已经有很强的 verbatim 保留规则**（`session_reduce.system.md`）：
> Verbatim preservation rule … include it verbatim in the matching `sub_task`. Do NOT replace
> `user typed "buy milk, eggs, flour"` with `user typed a shopping list`. Do NOT drop URLs or file paths.

它依赖一个前提（同 prompt 首句）：
> The timeline stage was instructed to preserve authored text … the content inside quotes is the
> user's own typed text — you must carry it forward.

即 **reducer 的保留规则靠 timeline 阶段先把 verbatim 放进 `[<app>] <context>: <what>. "<verbatim>".
Involving: …` 格式的 block**。

## 2. 当前测量被 harness 污染（必须先排除）

pipeline harness（`pipeline._session_captures`）**跳过 timeline 阶段**，把每个 turn 直接当 block 内容
喂进去：`[Chat] user: <原文>` —— **不是** reducer 期望的"引号内 verbatim"格式。于是 reducer 的
verbatim-preservation 规则**没有引号可抓 → paraphrase → 具体事实被压没**。所以 −0.233 里有多少是
"真 reducer 压缩损失" vs "harness 喂错格式" **现在分不清**。

（这与 temporal=0 的教训同款：harness 的输入保真度问题，会冒充成"生产 bug"。先修 harness 再下结论。）

## 3. 方案：两阶段，先证伪再改生产

### Phase A — 修 harness 喂对格式，re-measure（先做，零生产改动）
- `pipeline._session_captures` / block 合成：把每个 turn 渲染成 reducer 期望的格式，关键是**把事实承载的
  原文放进引号**：`[Chat] <speaker> said: "<turn 原文(截断≤1000字)>". Involving: —`。这让 reducer 现有的
  verbatim 规则有东西可抓。
- 可选更高保真：跑一遍真实 `timeline` 阶段（captures→blocks 的 LLM 归一化）而非合成——但那加一档 LLM
  成本；Phase A 先用"引号格式合成"这个零成本近似。
- **re-measure pipeline A/B**（需 OpenRouter 充值，~14min）。判据：
  - 若 single-session-user 的 −0.233 **显著收窄**（如 → −0.05）→ 损失主要是 harness 喂错格式，
    **reducer 没问题，不改生产**，只更新 spec 记录。
  - 若 **仍 ≈ −0.2** → reducer 确实在压具体事实 → 进 Phase B。

### Phase B — 改 reducer（仅当 Phase A 证明 reducer 真丢事实）
最小、可量化的改造，**不推翻现有压缩**（压缩仍必需），而是加一层"事实锚点"：
- `session_reduce.system.md` 输出 schema 增一个字段 `fact_anchors`: 从本窗口原样抽取的**检索关键锚点**——
  具体值、日期、数字、专有名词、"我的 X 是 Y"型断言，每条 `<entity>: <verbatim value>`（如
  `degree: "Business Administration"`、`trip count: "3 trips"`）。规则：只抽**原文出现过**的，不推断、
  不编；摘要继续负责"干了啥"，锚点负责"被问到时捞得回"。
- `session_reducer.py`：把 `fact_anchors` 渲进 event 条目正文（FTS 可检索的那段），跟 sub_tasks 同级。
- 守住既有不变量：`{summary, sub_tasks}` 字段不动（向后兼容）、event-daily 投影格式兼容、golden gate 绿。
- **用 pipeline harness 量 Phase B 的增益**（开/不开 fact_anchors 的 A/B），只保留噪声带外的真实提升。

## 4. 验证

1. Phase A：harness 改 + `PERSOME_LLM_MOCK=1 uv run pytest tests/eval/longmemeval/`（确定性档绿）
   + ruff；然后 OpenRouter 充值后 `run_pipeline --reduce-model deepseek/deepseek-v4-flash` 全量 sample-30，
   对比 single-session-user ΔQA 收窄到多少。
2. Phase B（若需要）：reducer prompt + 渲染改 → daemon 既有 gate（`test_intent_golden_deterministic`
   等）+ reducer 单测绿 → pipeline harness A/B（fact_anchors on/off）量增益。

## 5. 边界 / 诚实

- **不主张"别压缩"**：生产输入是 AX 噪声，压缩必需；本 spec 是"压得更准（去噪但保事实锚点）"。
- **先证伪**：当前 −0.233 被 harness 喂错格式污染，Phase A 没收窄之前，不动生产 reducer。
- Phase B 的 fact_anchors 会增加 event 条目长度 + 轻微 token 成本，用 A/B 确认净正才合入。

# 识别器语义召回层（dense recall into the intent recognizer）

> **Provenance.** 本设计文档成文于 Persome 产品 **Mens**（原生 macOS app）的开发中；文中出现 *Mens* 处指"某个 Persome 驱动的产品 / 预测器实例"，个别指向 Mens 代码库的路径/机制是**说明性示例**。属于 persome-core 的 daemon 部分（记忆 / 意图 / 检索 / 捕获）直接适用。

> Status: implemented. 把已上线的混合检索（te3-large dense ⊕ BM25 → RRF，
> `2026-06-25-production-hybrid-retrieval-design.md`）织进**识别器自身的
> 记忆召回**——慢路 `recall.assemble_background` 与快路 `fast_recognizer`。

## 为什么

识别器原来的记忆背景全是**词面/结构**召回：`assemble_background` 的关键词层对每个 `hint`（jieba
切的 2-4 字 term）跑 `entries MATCH`，其它层按 scope/recency/schema 取。它永远召不回**与当前场景
零词面重叠、但语义相关**的记忆——用户在订机票，词面层不可能联想到「他偏好靠窗/不坐红眼」。

向量检索的价值不是「关键词→query」，而是**语义属性**：从当前活动在**更高抽象维度**召回概念相关
的记忆。这正是识别器最该有的能力。

**真实库实测**（987 条向量，自然语言场景查询）：词面层 0 命中 / 语义层 5 命中（用户真实的
intent/thread）——例「我最近在忙的项目」「接下来要做的事情」词面层全军覆没，语义层全部命中且语义对路。

## 设计

- **共享层** `recall._semantic_layer`：把场景文本 embed 一次，经 `fts._dense_pool` 取 dense-近邻
  的活跃 entry（`live_matrix` 已 JOIN `superseded=0`，按 P1 不变量 ≡ 链头，故**不再叠 evo 链折叠**
  ——否则会错杀非实体链的 intent/thread 活跃命中）。命中走与关键词层**同一条** `_admit_rows`
  （`_seen_key` 去重 + confidence/trail 注释 + `_Budget` admission），dense 排序保序。
- **`_dense_pool` 加 `min_sim`**（默认 0.0，`search_hybrid` 不变；语义层传 0.2）：语义层无 BM25
  锚，没有相似度地板会把「与任何记忆都不相关」的场景也拉回 top-k 噪声；te3-large 归一化向量下
  0.2 留住真正相关、丢掉近正交。
- **慢路**（`assemble_background`）：新增 `dense_query`/`dense_top_k` 参数，语义 section 插在**durable
  facts 之后、keyword fallback 之前**（不挤占精确事实）；调用方 `recognizer.recognize_session` 传
  本会话轨迹文本 `session_events`。每次 `recognize_session` 一次 embed（block-flush 频率，矩阵缓存）。
- **快路**（`fast_recognizer`）：在 `<5s` 预算下——**只对闸幸存者**（过了 origin/parser/seen/throttle/
  anchor、正要调 LLM 时）embed，且**按 scope TTL 缓存**（默认 60s），即每会话每分钟最多 re-embed 一次；
  小 block（top_k 3，600 字符）注入 volatile body（场景特定，不进 cached profile）。

## 不变量（沿用混合检索的 posture）

- **无 creds / 关闭 / 空 query → 字节等价**：`_dense_pool`/`embed` fail-open 到 `[]`/`None`，语义层
  即空 section，背景与词面-only 逐字节相同。`config` 两个开关默认 ON，但 daemon 仅在
  `embeddings_client.available()` 时真激活（同混合检索）。
- **evomem 折叠/置信/trail**：语义命中走同一 `_admit_rows`，行为一致。
- **fail-open**：任何异常 → 该层空，识别器从不 raise。

## 配置（`IntentRecognizerConfig`）

`recall_semantic_enabled`(True) / `recall_semantic_top_k`(5) ·
`fast_recall_semantic_enabled`(True) / `fast_recall_semantic_top_k`(3) /
`fast_recall_semantic_ttl_seconds`(60)。

## 验证

- `tests/test_recall_semantic.py`（默认 gate，零网络，concept→one-hot 替身）：零词面重叠的语义命中、
  无 creds 字节等价、关闭无 section、superseded 不召回、快路 TTL 缓存（一次 embed 复用）、关闭即空。
- 既有 recall / intent-golden 确定性档全绿（重构后逐字节等价）。
- 真实库冒烟（987 向量）：自然语言查询词面 0 / 语义 5。
- **识别增益 — 已实测，结论 NULL-but-harmless（诚实）**：两支真 LLM bench（不进默认 gate，需 creds）：
  - `tests/eval/semantic_recall_bench.py`：合成档（一句欠定场景 + 仅注入语义背景）——中性，Δ 在噪声内。
  - `tests/eval/semantic_session_ab.py`：**保真档**——真 `recognize_session` 跑在真实 `index.db` 的**字节拷贝**上
    （真记忆语料 entries+vectors+FTS、真 blocks、完整 `assemble_background`），唯一变量是 `recall_semantic_enabled`
    开/关；逐 block 增量回放（生产是每 block-flush 累积，一次性会严重 under-fire）、关 `slow_pregate`（无锚门会把
    无锚 tick 直接 skip，两臂对称故无偏）、清全部 intents（避免拷贝库里的旧意图被跨 scope dedup 吞掉）、重叠系数
    模糊匹配（同义不同长不算误删）。`--runs N` 对每臂每会话多跑取多数票去噪。
  - **⚠️ 第一轮 A/B 作废（测了一个假死的层）**：随后把 `recall_max_chars` 当超参 sweep 时发现，慢路语义层在生产
    **静默假死**——它把整段会话轨迹（34K–77K 字符）当 dense query 去 embed，远超 te3-large 8191 token 上限 →
    embed 400 → `[]`。叠加 `_MAX_CHARS=24000` 对中文不安全、向量库 70% 是 event 把 dense top-k 占满（语义层又排
    event）两个 bug，语义层每次真实调用录用 **0** 条。所以最初那轮「OFF 0.21 / ON 0.33」测的是**根本没运行的层**。
  - **修复后（fix PR，3 连环 bug）**：语义层 0 → 每会话 ~5 条 durable 命中、需求 ~1610 字符/会话（现在最大的决策层）。
    在**修好的层**上重跑保真 A/B（4 会话 × 3 run）：均值 firing OFF 0.00 → ON 0.25（Δ+0.25，弥散、无稳定簇），
    稳定簇新增 0 / **删除 0** → **不伤识别**（两轮实验合计 0 实质性稳定删除）+ 一个噪声带内的弥散非负倾向。
    （relay 当晚超时使 OFF 基线偏塌，绝对值噪声大，但 no-harm 结论稳。）
  - **结论（修正版）**：稠密/语义层被证实的价值在**检索质量**（改写 recall 0.025→0.76）与**下游用记忆写 brief**；
    对识别 **fire 判定**的影响是**中性偏正、在噪声带内，但确认不伤**（后者由会话内文本驱动）。本层 fail-open、零回归、
    creds-gated 默认 ON；预算据 sweep 由 2400 调 **2600**（容纳语义层 ~1610 需求，~0 挤压，不进 keyword 稀释区）。
    真正量化识别级增益仍需带标签的跨会话语义 golden 或更高保真回放基线——列 follow-up。

## 范围外

- captures 全文（无向量，留 BM25）；改 embedding 模型；带标签的跨会话识别语义 golden（follow-up，上面 bench 是无标签 DIFF）。

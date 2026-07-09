# Memory Root Apex — 体之上的第 4 维 root（单例·预算封顶·唯一常驻）

> **Provenance.** 本设计 spec 成文于 Persome 产品 **Mens**（原生 macOS app）的开发中；文中出现 *Mens* 处指“某个 Persome 驱动的产品 / 预测器实例”，个别指向 Mens 代码库的路径/机制是**说明性示例**。属于 persome-core 的 daemon 部分（记忆 / 意图 / 检索 / 捕获）直接适用。

- **Status**: Draft（待产品方过）
- **Date**: 2026-07-04
- **Depends on**: `schema_faces` 同表递归（面=1/体=2）、维度判据 §1.2、
  filesystem-memory-grounding（`MemoryDigest`/`runGrounding`，见 `2026-06-27-filesystem-memory-grounding-design.md`）、associative_read 读路 cutover
- **心智模型对表**: 产品 north-star（root = 永远加载的“权重摘要”）、
  意图设计哲学的维度判据元公式（§9）

## 〇 一句话

在 `schema_faces` 的同表递归上再加 **level 3 = `root`**：整张记忆图**唯一的、token 预算封顶的、
永远常驻**的顶点（体之上）。除 root 外的一切（体/面/点/facts/raw/今日事件）都从常驻区移出，
改走 **recall（`associative_read`）+ 渐进式披露**。root 成为渐进披露的**唯一入口**。

**产品方 2026-07-04 拍板**：root 由**夜间 apex 合成**（方案 b）产出；预算**最大 1500 token**。

## 一 root 是维度判据的第 4 格

维度判据元公式（§1.2）：**元素的维度 = 完备描述它所需的最小引用集的型**。

| 维度 | 元素 | 引用集 | level |
|---|---|---|---|
| ∅ | 点（实体/事实） | 空 | — |
| 两点 | 边（关系） | 两个点 | — |
| 点集 | **面** | 一组点 | 1 |
| 面集 | **体** | 一组面 | 2 |
| **体集** | **root** | **所有活跃体** | **3（新）** |

root = 引用集为「**全部活跃体**」的那个唯一元素 = 整图 apex。这不是新机制，是递归的第 4 格
（`record_face(level=3)` 一套操作复用）。

### root 独有的两条硬不变式（面/体都没有）

| 轴 | 面(1) | 体(2) | **root(3)** |
|---|---|---|---|
| **基数** | 多 | 多 | **恰好 1（singleton）** |
| **长度** | 不限 | 不限 | **≤ 1500 token（硬预算）** |
| **常驻** | 否 | 否（转正才进旧塔顶拼盘） | **唯一常驻** |

- **Singleton**：`schema_faces` 中 `level=3 AND valid_to IS NULL` 的行**恒为 1**。新 root 经 chain
  **supersede** 旧 root（`superseded_by` 指针，绝不共存），语义同 SELF 的唯一性。冷启动时 0 个（见 §5）。
- **Token 封顶**：root 的**渲染文本** ≤ 1500 token（`root_token_budget`）。在产出口硬 enforce（§2 闸），
  永不下发超预算 root。

## 二 产出：夜间 apex 合成（方案 b）

`writer/root_synthesis.py`，挂在 **schema-tick 尾**（face mining + cross-domain sweep **之后**，吃最新体），
`session_tick` 调度，gated on `[schema] root_synthesis_enabled`（**默认 OFF**，先 shadow 攒量）。

### 输入（压缩的压缩，不碰原始点）

1. 活跃 **level-2 体** 的签名（central proposition）+ 各体的成员面把手
2. 活跃 **level-1 面** top-k 签名（补体没覆盖到的稳定面）
3. `MemoryDigest.profileFacts`（耐久 identity/preference/project 事实）

### 一趟有界 LLM（`prompts/root_synthesis.md`）

产出「**这个人的单一 apex 速写**」：他是谁、**最要紧的是什么**、当前在推进的大事——一段**压缩叙事**，
句中对可下钻的体挂**收据把手** `⟨体-signature⟩`（渐进披露的下钻锚）。**≤1500 token**。

### 落库 + 三道确定性闸（产出口 enforce，任一失败 → 不下发/退回旧 root）

1. **Token 闸**：> 1500 → 一次 re-compress 重试；仍超 → 句界硬截断 + `…⟨truncated⟩` 收据。
2. **提及子集闸**（反幻觉，复用 `identity.scan_mentions`）：root 只能点名**存在于输入活跃集**的体/实体。
3. **非空闸**：空/退化产出 → 保留旧 root，不 supersede。

**产品方 2026-07-04 拍板：默认 ON → root born active**（不走 face 的 shadow 稳定期重采样——那是
多面池概念，不适合 singleton 夜间重derive 的 apex）。语义：**最新一次通过三道闸的合成 = 当前 active
root**，chain-supersede 旧 root。三道确定性闸（token/提及子集/非空）即安全网；任一失败 → **保留旧
root，不 supersede**（绝不回退到空）。禁孤儿由 default-ON + activation 测试满足（§7）。

## 三 常驻收缩：resident = { root } + attention（MECE）

「除 root 其他都 recall」= 常驻集从「top-5 面签名 + identity/today/projects 拼盘」**收缩成单个 root**。

| 常驻区 | 内容 | 预算 | 语义轴 |
|---|---|---|---|
| **root** | 单一 apex 叙事（level-3 active） | ≤1500 tok | **“是谁 / 最要紧”（稳定）** |
| **attention** | 实时 now/屏幕/rewind/工作线 | 其原预算 | **“现在”（易变）** |
| ~~identity/today/projects 拼盘~~ | ~~多文件前置~~ | — | **删——转 recall** |
| ~~resident_faces top-5~~ | ~~面签名~~ | — | **删——被 root 取代** |

- root 与 attention **正交**：root 是“是谁”，attention 是“现在”，不混（attention 不塞进 root 污染其
  singleton-稳定语义）。
- 其余**全部** → recall（`associative_read`）+ 渐进披露：体/面/点/facts/raw/**今日事件**，经
  `$MENS_MEMORY_DIR` 指针 + MCP 树链 + 收据 `⟨id:path⟩` 按需拉。

### 改动面

- daemon `store/schema_faces.py`：`resident_root(conn)`（取 level-3 active 单例）取代
  `resident_faces`/`render_residency` 作为常驻投影；`render_root(row, budget)` 渲染。
- 消费产品（Mens）侧 `MemoryDigest`：常驻记忆块 = **root only**（经 `ChronicleIndexReader` 从 index.db
  读 level-3 active 单例，缺则 §5 回退）；删 identity/today/projects 拼盘（转 recall）。
- 消费产品（Mens）侧 grounding（`TaskRunner.runGrounding`）：常驻 = root（≤1500 tok）+ attention；`.context` 仍套
  `privateContextCaveat`。

## 四 渐进披露：root 为根前缀

- root 携把手 `⟨体-signature⟩` / 收据 `⟨id:path⟩` → agent/recall 从 **root → 体 → 面 → 点 → facts → raw** 下钻。
- MCP 树链交付（`retrieval/chains.py`）：交付链共享前缀锚由 USER 上抬为 **ROOT→USER→…**（root 是常驻的
  apex 摘要，收据让 agent 按把手点名请求具体体/面）。
- 与 LLM 类比对齐：**root = 永远在上下文的、被最大压缩的用户表征（≈常驻 system prompt / 学到的用户
  权重摘要）；其余 = 按需拉取的 KV-cache**。

## 五 冷启动 / 回退（“有没有 root 了”的 MECE）

| 状态 | 常驻记忆块 |
|---|---|
| **无 root**（新装 / flag 刚开 / 库空） | 回退今日 `resident_faces` top-k（现行为），fail-open |
| **root shadow**（合成未转正） | 仍回退（shadow 不常驻），保守 |
| **root active** | **只投 root** |

破损（读 index.db 异常）→ 回退现行为，可见 degrade（warning），绝不空常驻。

## 六 config / rollout（默认 ON，产品方 2026-07-04 拍板；禁孤儿由 ON+activation 测试满足）

- `[schema] root_synthesis_enabled = **true**`（默认 ON）、`root_token_budget = 1500`。
- 消费产品侧 `Settings.groundResidentRootOnly = **true**`（默认 ON；无 root 时 §5 回退兜底，所以默认开是安全的）。
- **首根即时可见**：`root-synth`（手动触发一趟合成，部署后立即跑一次，不必等 00:15 的 schema-tick）。
- 观测：`root-report`（当前 root 状态 provenance/status/token 数 + 预览 + 冷启动回退情况）。
- 因为 born-active + 回退兜底，**开箱即用**：无 root→回退现拼盘；`root-synth`/夜间 tick 一产出即切 root 常驻。

## 七 测试

- daemon：`test_schema_faces` 补 level-3 + **singleton 不变式**（新 root supersede 旧、活行恒 1）；
  `test_root_synthesis`（mock LLM：token 闸截断、提及子集反幻觉、非空、chain supersede、冷启动回退）；
  维度判据 golden 补 dim-4=体集→root。
- 消费产品侧：`MemoryDigestTests` 补 root-only 常驻渲染 + 冷启动回退；端到端自测补 grounding 常驻=root+attention。
- 闸：daemon intent-golden 确定性档不受影响；root 合成走 mock LLM 进默认 gate。

## 八 不变式 / 红线

1. **Singleton**：任何时刻至多 1 个 live root（`level=3 AND valid_to IS NULL`）。
2. **Token 封顶**：下发的 root ≤ 1500 token，产出口硬 enforce。
3. **反幻觉**：root 只点名输入活跃集里存在的体/实体。
4. **Fail-open**：无 root/破损 → 回退现行常驻，绝不空。
5. **默认 ON · born-active · gated by tests**：合成默认 ON、root 一产出即 active；禁孤儿由 activation
   测试（§7 端到端自测 + test_root_synthesis）满足；三道确定性闸 + fail-open 回退是安全网（非 shadow 期）。
6. **root ⟂ attention**：root 只承“是谁/最要紧”，attention 承“现在”，不混。

## 九 明确不做（本 spec 边界）

- root 的**多语言/风格**定制（先中文速写，够用再说）。
- root 参与**写路**（root 是只读投影/召回入口，绝不铸点/改事实）。
- 体之上再加 level 4+（root 已是 apex；singleton 就是终点）。
- 把 attention 也 apex 化（attention 是易变流，不进 schema_faces 递归）。

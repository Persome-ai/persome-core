# 正交 facet 化的快路意图识别（faceted fast-path intent schema）

> Status: design (proposed). 把事件驱动**快路**（`intent/event_source.py` + `intent/fast_recognizer.py`，
> K1 通讯·到达）的意图表示从**扁平 `kind`** 重构成一组**正交 facet 元组**。**Phase A** 只换会话流通道
> 的输出表示（同触发、溶解 `kind`、LLM 收缩到只出语义核）；**Phase B**（另立 spec）再把"新事件探测"
> 泛化到非会话通道（K8 系统信号 / K2 内容面），让 monitor/inferred 象限有数据源。本 spec 是这条演进
> 的**设计权威**——后续识别端以这里的正交轴图像为准。

## 为什么

三个并存的问题，根因是**用一张平铺的 `kind` 清单去覆盖一个开放世界**：

1. **`kind` 纠缠了多个轴。** `meeting/calendar/reminder/assignment/info_need/meeting_hint/backlog` 其实是
   "动作目的 × 时间性 × 来源"的一个**粗糙合并投影**——同一件事换个时间性就要换 kind，加一种新意图就要
   加一类。平铺清单永远不可能 MECE（设计里留 `unrouted` 兜底＝承认了不穷尽）。
2. **场景表（#273 K1–K8）混轴。** 它把"感知通道"（怎么认）当成了"意图分类"（要什么）。K1 通讯 / K2 阅读
   是交互模式轴、K6 是对手方轴、K7 是可得性轴、K8 是通道轴——四个轴塞进一张表，重叠与缝隙是数学必然。
3. **最值钱的象限没被覆盖。** "系统推断出来的意图"（CI 红了该处理、到点该写日报）是主动助手的前沿，
   但快路当年用"误判才贵"**故意排除**了这个低置信象限，且它本来就活在**非会话通道**，而快路只认 K1。

完备性不来自"枚举场景"，来自**定义若干正交轴，让任何场景都是积空间里的一个点**。本 spec 把散落在各
子系统里的轴（daemon `kind`、`provenance`、FollowUp `Gate`、K 表、`importance×urgency`）**提升成一张
显式的正交 facet 表**，并据此重构快路。

## 设计原则

- **感知 ⊥ 意图。** 先把"信号从哪个通道来（怎么认）"和"用户要什么（是什么）"彻底分开。K 表降级成
  **一条独立的感知轴（轴 0）**，识别管线消费它，意图语义**不**由它定。
- **意图 = 正交 facet 的积。** 一个意图是 5 个 facet 各取一值的元组；场景是积空间里的点，不是"类"。
- **每个 facet 全域化（含兜底值）。** 任何 facet 都有一个"无/环境/未知"的合法值——一个说不清的东西就是
  `(monitor, ambient, open, inferred, internal)` 这个低置信点，要不要 surface 交给 `importance×urgency`。
  **`unrouted` 被消解。**
- **强确定的不进 LLM；语义的才进。** 按"信号在不在表面"切，不按 facet 切：表面可 derivable 的
  （时间锚解析、消息方向、对手方、Telos→外向 映射）走确定性；潜在/语义的（Telos、隐式周期、任意条件、
  体内对象、言语行为）才花 LLM。
- **prompt 写原则、代码写具体。** LLM 的 prompt 只讲抽象原则（"如果隐含某种节奏或系在未发生的事件上，
  即使没逐字写出来也说出来"），**不**列 `if 日报 then daily` 的例子表——泛化交给模型，硬编留给确定性层。

## facet 全域

### 轴 0 · 感知通道（怎么认 — Phase A 固定为 `conversational`）

`conversational`（离散消息）｜`content`（可消费的页/文档）｜`authoring`（用户产出的内容面）｜
`structured_push`（平台发的卡片/事件）｜`system_signal`（非窗口：剪贴板/下载/通知/时钟/传感器）｜
`opaque`（AX 穷/媒体/被动 → OCR 或放弃）。

> 这就是 K1–K8，正确地只当成感知轴。**Phase A 只实现 `conversational`**；其余是 Phase B 各自的"新事件
> 探测器"。`monitor`/`inferred` 象限主要活在 `content`/`system_signal`，所以 Phase A 做完它们仍不覆盖——
> 这正是 B 的价值所在，不是 A 的缺陷。

### 意图 5 facet（是什么）

| Facet | 全域（末值为兜底） | Phase A 谁来定 |
|---|---|---|
| **① Telos** 动作目的 | `commit`(钉未来义务) · `acquire`(取信息/物件) · `produce`(造/改产物) · `transact`(改外部状态：发/交/付/订/publish) · `delegate`(交给人/agent) · `monitor`(盯状态、变了才动) | **LLM**（不可约语义核） |
| **② Object** 对象 | `self` · `person` · `org` · `agent` · `ambient` | 确定性默认=会话对手方；**LLM** 仅当对象≠对手方（`object_hint`） |
| **③ Temporality** 时间性 | `immediate` · `scheduled`(有锚) · `conditional`(系在未发生事件上) · `recurring`(周期) · `open`(无时) | 确定性核={immediate/scheduled/open}（`when_text`→`normalize`）；**LLM** 仅 lift {conditional, recurring} 当其隐式（`condition_hint`/`recurrence_hint`） |
| **④ Provenance** 来源/意志 | `committed`(用户已承诺) · `proposed`(对方提议/用户提议待定) · `inferred`(谁都没说，模式/状态推出) | **LLM** 出言语行为读数 + 方向做确定性先验/校验（见装配规则）。会话通道上 `inferred` 几乎不出现 |
| **⑤ Outwardness** 外向/代价 | `internal`(只落本地/记忆) · `outward_reversible`(摆草稿，用户再发) · `outward_irreversible`(发/交/付/publish) | **纯确定性**：Telos→sink→外向；focus 不匹配则降级（沿用 `focusMatches`） |

叠一层正交标量：`importance × urgency`（每条 0–1，给主动哨兵判象限），与 5 facet 正交，沿用现有定义。

## 管线（Phase A）

闸全在 LLM 之前不变（origin → parser → seen-set → 锚点 → throttle，见 `event_source.on_capture`）。
变的是**幸存者之后**这一段：

```
gate 幸存者 ──► 单次 LLM（语义核+潜在槽，共享缓存前缀）──► 确定性装配 ──► 打扰闸 ──► sink
  (会话流)         telos / provenance / *_hint / confidence       when_text→normalize→③
                                                                  方向→④ 先验/校验
                                                                  对手方→② 默认
                                                                  telos→⑤ 外向
  确定性旁路（零 LLM，与 LLM 调用并存）────────────────────────────┘     来源④ × 外向⑤ 闸
                                                                       inferred ⇒ 零打扰
                                                                       外向 ⇒ place-never-send
```

**单次，不并行。** 一次调用→一个缓存前缀→一份输出：跨 facet 一致性白送、最便宜、延迟可预测，且 daemon
`call_llm` 同步路径不动。并行（分类/抽取两路）**收进抽屉**当逃逸口——只有当 oracle 显示某 facet 被
"一个 prompt 塞太多指令"拖垮时，才数据驱动地把那一支拆出去并行。

### 单次 LLM 契约

LLM **不**再 re-parse 时钟、**不**出外向性。它只吐语义核 + 潜在槽：

```json
{
  "telos": "commit|acquire|produce|transact|delegate|monitor",
  "provenance": "committed|proposed",
  "object_hint": "<对象≠对手方时的实体；否则省略>",
  "recurrence_hint": "<隐含周期，如 daily/weekly；否则省略>",
  "condition_hint": "<系在未发生事件上时的触发事件逐字；否则省略>",
  "when_text": "<逐字时间短语，留给 normalize 解析；无则省略>",
  "confidence": 0.0, "importance": 0.0, "urgency": 0.0,
  "rationale": "一句话依据",
  "needs_trajectory": false
}
```

**`*_hint` 是潜在信号逃逸口，不是字段清单**，受两条硬约束防胖：
1. **只在"表面确定性层会算错"时才出**——表面能 derivable 的（写明的时钟、对手方本人）不许塞 hint。
2. **每个 hint 必须有确定性消费方**：`recurrence_hint`→生成周期任务（`Recurrence`）；`condition_hint`→armed
   触发（`activation=on_event`）；`object_hint`→改 ② 对象。**没有消费方的 hint 不许加。**

### 确定性装配规则（零 LLM）+ 冲突优先级

1. **③ 时间性** = `normalize(when_text)` 得 {immediate/scheduled/open}；`condition_hint` 非空 → 覆盖为
   `conditional`（并记触发事件）；`recurrence_hint` 非空 → 覆盖为 `recurring`。hint 覆盖默认。
2. **② 对象** = `object_hint` 优先；否则 = 会话对手方（parser `with`/sender）；皆无 → `ambient`。
3. **④ 来源** = LLM 的言语行为读数为主，**方向做确定性校验/先验**：LLM 说 `committed` 但方向是
   `received` 且 `confidence` 不高 → 装配层**压回 `proposed`**（防误动作）。`confidence=1.0` 只认逐字承诺
   （`dir=sent` 的明确排期），任何推断 ≤0.9——沿用现 prompt 的校准。
4. **⑤ 外向** = `FollowUpRules`-风格的 Telos→sink 映射（数据表，app 侧已有）；focus 不匹配 → 降级到本地
   `clipboard`/held。

### 打扰闸（沿用 #3 的不变量）

- **`provenance=inferred` ⇒ 强制零打扰**：只落记忆/进 monitor 队列，**永不弹窗**（与现 assignment/backlog
  零打扰同机制）。弹窗只留给 `committed`/`proposed`。
- 弹不弹由 **来源④ × 外向⑤** 决定，**不由 Telos**。
- **外向 sink 仍 place-never-send**：只摆草稿，用户按 Enter 才发——即使 ④ 误判成 `committed`，最坏也只是
  多摆一个草稿，绝不自动发出。这是最后一道兜底。

## `kind` 的处置：降级为派生投影（迁移桥，显式临时）

新表示的**正则**是 facet 元组；`kind` 不再是真相，降级为 `kind = 投影(telos, temporality, object)` 的一个
**确定性派生**，只为让现有消费者（`FollowUpRules`、app、intent-golden）在一次重构里不全崩。投影表：

| facet 元组 | 派生 `kind` |
|---|---|
| commit + (scheduled/conditional) + person | `meeting` |
| commit + (scheduled/conditional) + 非 person 日程 | `calendar` |
| commit + self | `reminder` |
| commit + person + open/vague(euphemism) | `meeting_hint` |
| delegate(received→user) | `assignment` |
| acquire | `info_need` |
| produce / monitor（Phase A 新象限，会话通道罕见） | facet-only（暂无 kind 投影，下游按 facet 消费或忽略） |

> Phase B 之后下游逐步改成**直接按 facet 消费**，`kind` 投影随之退役。本表是桥，不是终点。

## 不变量（重构后必须仍成立）

1. **精度优先**：锚点闸 + 单次 LLM 仍"拿不准返回空"；分类不定 → 不产意图（宁漏不误，慢路兜）。
2. **<5s 预算**：单次 LLM + 缓存前缀；`gather` 不引入（单次无需）。`lean_focus` 仍只裁新到的消息。
3. **never-raise**：LLM/装配任一步异常 → fail-open 到确定性默认，不崩 capture（沿用 `recognize_event`）。
4. **事件只评一次（#274）**：`_mark_seen` 仍在闸前一次性标记；facet 化不改事件探测。
5. **cost-gate-on-survivors**：LLM 只在过了全部闸的幸存者上跑，不是每个 capture。
6. **place-never-send** + **inferred 零打扰**：见打扰闸。
7. **快慢路由位** `needs_trajectory` 继续搭同一次调用。

## Oracle / 评测（先有 oracle，再动手）

- **intent-golden 从按 `kind` 改为按 facet 元组**（`tests/eval/golden/intent_golden.yaml` schema 升级）：
  正例标全 5 facet，确定性档校验"装配规则 + hint 消费 + 投影"零 LLM，LLM 档算 telos/provenance 的
  firing precision/recall。
- **旧例零回归**：现有 golden 例先经"facet→`kind` 投影"回填，断言投影后与旧 `kind` 一致——保证一次重构不
  改外部行为。
- **新象限补例**：为 `recurring`(日报/周会)、`conditional`(等发布完/CI过)、`object≠对手方`(帮我约B) 各补
  golden，量"潜在槽"是否被 lift 出来。这是泛化是否真被保住的度量。
- 噪声带、`PERSOME_EVAL_*` 阈值、确定性档进默认 gate 的约定，全沿用现 intent-golden 两档结构。

## Phase B（另立 spec，本 spec 只留接口）

把"新事件探测"抽象成**按通道的新颖性闸接口**——`conversational` 的 seen-set 只是它的一个实现：

- `system_signal`(K8)：OS 事件本身就是离散新事件，无需 seen-set；解析 = 事件载荷；相关性闸 = 事件类型白名单。
- `content`(K2)：新颖性 = 停留/导航；解析 = `BrowserParser`→`WebPage`（已有，今天被 `not_conversation` 丢）。

接进同一个"单次 LLM 语义核 + 确定性装配"下游——**扩通道 = 加一个探测器，不改主干**。`monitor`/`inferred`
象限随 B 的新数据源自然进来，"汇报材料到点推送""状态监视"落在 `(produce/monitor, *, recurring/conditional,
inferred, *)`，不是临时特例。

## 非目标 / 待定

- **不做并行 LLM**（除非 oracle 证明单 prompt 指令干扰）；不做精度投票（除非某 telos 误判率高）。
- Telos 6 值的边界（尤其 `monitor` vs `acquire`、`monitor` 是触发后的 produce 还是持续意图）**先松着**，
  等 oracle 数据回来再收死，不在开写前钉。
- Phase A 不碰非会话通道、不碰 app 侧 `FollowUpRules` 的 kind→facet 改造（投影桥扛着）。

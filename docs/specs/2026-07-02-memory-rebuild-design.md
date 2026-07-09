# Memory 重构设计（Memory Rebuild）

> **Provenance.** 本设计 spec 成文于 Persome 产品 **Mens**（原生 macOS app）的开发中；文中出现 *Mens* 处指“某个 Persome 驱动的产品 / 预测器实例”，个别指向 Mens 代码库的路径/机制是**说明性示例**。属于 persome-core 的 daemon 部分（记忆 / 意图 / 检索 / 捕获）直接适用。

**Date:** 2026-07-02
**Status:** **Executing** —— epic 分支 `refactor/memory-rebuild`；执行计划见 §6
**Slug:** `memory-rebuild`

> 本文档**取代**其前身设计（已归档为决策考古）。正文**只写现行有效的定稿**；修正轨迹在附录 B 决策日志。
>
> **配套图（mockups）**：AX tree → Memory 路径图（**执行对照表，先看这张**）；定稿模型 3D 交互画布。

---

## 0. 一句话与判决

**Mens = LLM + Memory = 意图预测器**（北极星公式）。Memory 是这个人的**权重** θ——一张**以 USER 为根、随时间生长的图**。

**判决**：现有“意图识别”链路本质是在**现在进行时上做叙述**（“他现在在看/做什么”），不是预测——生产数据：accept ~31%、34 次弹出仅 2 次有效裁决、over-fire 为实测主问题且 data-bound。**Prediction = f(context; θ=Memory)——权重没练好，预测器输出即噪声。**

**顺序**：砍伪预测（Phase 0）→ 建 Memory（Phase 1-2）→ 在权重上重做真预测（Phase 3）。

---

## 1. 本体：一张图

### 1.1 时态三分（一切设计的骨架）

| 时态 | LLM 对应 | 载体 | 与 Memory 的关系 |
|---|---|---|---|
| **过去** | 权重 weights | `event-*.md` · `evo_nodes` · `relation_edges` · schema | **= Memory，图只装这一层** |
| 现在 | 输入 context | captures · timeline_blocks · workthread | Runtime 素材，不进图 |
| 将来 | 输出 token | 预测的意图/动作 | Prediction 产物，不进图 |

**生命周期切分**（不是按表拉黑）：`intents` 里 open/armed（悬而未决的将来）绝不进图；**done 终态**（consumed/resolved/completed）= 事情发生且完结 = 过去事实 → 进图铸 Activity 点。已终结的工作线同理。

### 1.2 维度递进：点 · 边 · 面 · 体（+ 时间）

**一切都是事物。** 用户/他人/项目/文档不是不同物种，“是什么”只是事物的一个状态属性。

- **点（0 维）= 事物 + 状态轴**：`kind`（SELF/PERSON/ORG/PROJECT/EVENT/ARTIFACT，正交轴闭集）× `validity`（持续者 live/historical；发生者只有终态入图）× `consolidation`（证据数）× `provenance`（confirmed/inferred）。
- **边（1 维）= 关系 + 状态轴**：方向 × 谓词（6 闭集，下表）× 极性 × **observations（证据棘轮）** × provenance × 有效期。一对点可有多条平行边（多重图——每条边的强度/时效独立演化）。
- **面/体（2/3 维）= 统一 schema**：一群点因彼此有边而**涌现**的簇；体 = 面之上同一操作递归一层。详见 §4.5。
- **时间 = f(T)**：点·边·面全部随 T 演化，bitemporal（事务时间 `created_at` 不可变 ⟂ 有效时间 `valid_from/to` 可回填）；**可持久化**（主席树语义：只追加、不删改、as-of-T 任意回放）。as-of-T 是一等检索操作（“他三月的老板”）。

#### 点的良构判据（形式化，产品方裁定）

设候选 x 的状态函数 σ(x) = ⟨id, kind, validity, consolidation, provenance⟩，
位置函数 π(x)（§7-6：θ=kind 扇区、y=时态带、r=证据强度、抖动=hash(id)）。

**x 是图中的一个点，当且仅当**：

1. **唯一指称（rigid designation）**：referent(id(x)) 存在且唯一——同一 id 的全部证据
   指向同一个体。类（「群聊」）、角色（「客户」「面试官」）、泛称（「团队」）指向的是
   集合或函数，不是个体 → 不是点。
2. **状态自足（completeness）**：σ(x) 在每条状态轴上取定值，且每个值仅由 x 自身的
   证据决定——不引用任何其他点/边/面/体。
3. **可自定位（self-locatability）**：π(x) = f(id(x), σ(x)) 单点可算——只看 σ(x) 就能
   把 x 放进三维空间；f 的定义域只含 x 自己。

**维度判据（统一形式，四个维度同构）**：元素 e 的维度 = 完备描述它所需引用的其他图
元素的**最小集合的型**——

| 完备描述最少要引用 | e 是 |
|---|---|
| ∅（自足） | **点**（0 维） |
| 恰好两个点 | **边**（1 维） |
| 一个点集（因边涌现） | **面**（2 维） |
| 一个面集 | **体**（3 维） |
| 引用什么都无法唯一确定 | **不是元素**——是某条轴上的**值** |

推论（人裁/接纳的判定链）：
- 「群聊」= 某个具体群（点，kind=org）的**形态属性**（开集 label，同边的 label 纪律），
  不是 kind 轴新值——「研发群」是点，「群聊」是它的状态之一。
- 「客户」= knows 边的 label；「某人的老板」要引用另一个点 → 是**边**不是点；
  「面试官」= 未消解的角色指称——消解出唯一自然人前**不铸点**。
- **接纳闸（§4.3 漏斗的职责）**：唯一指称当下不可判 → 不拒也不铸，留 shadow 孤儿区
  等证据（宁缺毋滥的点版）；类/角色/形态一经识别 → 改记为轴上的值，永不成点。

### 1.3 边的 6 谓词闭集（§九 正交轴推导，语义细节进开集 `label`）

| 关系族 | 谓词 | 规范方向 | 合法端点 | label 例 |
|---|---|---|---|---|
| 参与 | `participates_in` | agent→活动 | {SELF,PERSON,ORG}→{PROJECT,EVENT} | works_on/负责 |
| 归属 | `part_of` | 部分→整体 | {SELF,PERSON}→ORG · PROJECT→ORG · ARTIFACT→PROJECT | member_of/雇于 |
| 社交·层级 | `reports_to` | 下级→上级（可传递） | {SELF,PERSON}→PERSON | 汇报给 |
| 社交·一般 | `knows` | 无向（存规范序） | {SELF,PERSON}→PERSON | 客户/同事 |
| 指涉 | `about` | 事物→主题 | {EVENT,ARTIFACT}→{PROJECT,PERSON,ORG,EVENT} | 关于/提到 |
| 依赖 | `depends_on` | 依赖方→来源 | {PROJECT,EVENT,ARTIFACT}→{PROJECT,EVENT,ARTIFACT} | blocked_by |

规则：每条 tie 只存一个规范方向；`src×dst` 完备矩阵已冻结（新增关系先回矩阵——落得进 label 就进 label，落不进才谈加轴）；关系结束 = `valid_to` 收口，**不删**。

### 1.4 两层正交（与 evomem 的关系）

evomem（**纵轴**：一个节点自己随时间怎么变，SUPERSEDE 版本链）⟂ 关系图（**横轴**：节点之间什么关系）。接口：**边认稳定 identity 不认版本**（person_graph 规范名/链头）；“该 identity 在时刻 T 什么状态”由 evomem 解析——〔已建（Phase 2）：`evomem/as_of.py` `nodes_as_of`（identity 的 file_name + T → 当时活的节点集）/ `node_as_of`（链上任一版本 id + T → 链在 T 时的版本，收据指针可回问「三月它怎么说」）——事务钟回放（gmt_created + supersede 指针，主席树语义）∧ 有效钟过滤（valid_from/until 缺省 fail-open）；CLI `mens as-of`；生产 cutover（检索/交付挂 as-of 参数）随 §7-3 一起〕。

### 1.5 硬不变量（图的宪法）

1. **单连通、根在 USER**：任意时刻只有 USER 所在连通块算记忆；**实质连接只数 6 谓词边**（observed 是 provenance，不是边——否则不变量沦为免费赠品）。
2. **孤儿收敛**：连不上 USER 的点入 shadow 孤儿区 + TTL 30 天——长出实质边转正，到期按噪声遗忘。**单连通靠遗忘收敛，不靠万能边。**
3. **逐层锚根（rollup）**：每个面归入某个体、每个体传递包含 USER——整座塔每层锚回根。roll 不到根的面 = 伪 schema。
4. **append-only / 可回放**：从不 in-place 改、从不物理删；同一 `(query, as_of)` 必得同一子图。
5. **有损蒸馏**：单 event=弱 episodic 痕迹，反复强化才升 durable semantic；老/弱记忆按龄多级降精（细节链→粗摘要→一行事实），而非二值删除。
6. **结束只认证据，禁静默超时收口**：`valid_to`/`valid_until` 的收口必须由证据（quote 里的明确结束语言）或人裁触发——证据骤停 ≠ 关系结束，dormancy-TTL 只适用于从未连上 USER 的孤儿（#2）。绝不允许“N 天没新证据就自动收口”。

这些不变量的**统一存在理由**：保证任何检索命中都能拉出一条**到 USER 的完整链**（§3.4）。

---

## 2. 表示：向量 Memory · 符号收据 · 双编码

### 2.1 三层与 SSOT（调和后的明确表述）

```
raw 收据层 = 同一时刻的两个投影，互相印证
  ├─ 文本投影：AX tree 提取 text → captures/timeline → event-*.md  ← md 是符号收据的 SSOT
  └─ 像素投影：对应屏幕截图（OCR/VLM 描述 = 它的文本化）          ← 证据链的最底层
      │ 蒸馏 consolidation
      ▼
Memory = 双编码，互为索引
  ├─ 符号骨架：图（点/边/schema 表 + md 投影随行）  ← 结构化真相，可审计可编辑
  └─ 向量血肉：实体/边/schema 嵌入                  ← 派生索引，可整体删除重建
      │ 读时（三级读法 + 渐进式披露）
      ▼
    LLM（token in）
```

- **Memory 的本体形态是向量**（权重本就稠密；符号语料是训练数据）——consolidation 本质是压缩，压缩的自然产物是稠密表示。
- **每个向量必须指回收据，收据必须可下钻到底**：①可审计（信任承重墙——用户永远能读到“这团向量凭什么”，一路读到那一刻的屏幕）；②可遗忘（删收据→连坐删向量→重建，绕开 unlearning）。
- **收据的渐进式披露（四层，默认只交付最上层）**：链叙事 → `event-*.md` → AX text（capture/timeline 块）→ 截图。消费方按需逐层拉取（`$MENS_MEMORY_DIR` 读取 / MCP pull），**从不默认内联**——像素只在显式下钻时进 prompt。AX-poor 场景截图是唯一忠实记录，此时 OCR 文本充当其文本投影。
- **像素层按龄降精先于文本**（挂 §1.5-5 分级遗忘）：全分辨率 → 缩略 → 仅存 OCR/VLM 文本化 → 删除——像素最重最先忘，文本投影长留，证据链降精不断链。〔已建（Phase 2）：`cleanup_buffer` 的缩略层（`capture.screenshot_thumbnail_hours`，默认 0=关）——整删/剥离两档之间原位降采样 ≤480px；加密截图解密→降采样→重加密（无钥 fail-open 不动密文）；actionable 延长保留帧保全分辨率；剥离后 OCR/AX 文本仍在 `captures` 表=「仅存文本化」档〕
- **SSOT 分层**：md = 符号收据层与 schema 签名的 SSOT；结构化表（`relation_edges`/`schema_faces`）= 图层真相，md 投影随行；向量 = 纯派生，删了可全量重建。三者从不互相矛盾——矛盾即 bug。

### 2.2 三级读法（冻结 API 模型只吃 token）

| 级 | 机制 | 状态 |
|---|---|---|
| ① 寻址 | 向量近邻选锚 → 沿索引取符号收据 → 序列化进 prompt | 已在生产（hybrid dense 半） |
| ② 反演 | 向量簇解码成摘要文本（schema 签名即雏形） | 雏形已有 |
| ③ 原生注入 | soft prompt / kNN-LM——向量直接进 embedding 空间 | 需本地模型，远期 |

**向量决定“想起什么”，符号承载“说出什么”**——我们在模型外替它做那层它做不了的 attention（token 接口上的“伪 embedding”），给冻结的大脑外挂海马体。细颗粒来自骨架（向量长在图原子上，不是 chunk 上）；**压缩比即产品**（护城河“Context 组装”的字面机制）。

---

## 3. 读：塔顶常驻 + 联想检索（单入口 · 六头 RRF · 树链）

### 3.1 塔顶常驻，塔底检索

| 塔层 | 规模 | 去处 |
|---|---|---|
| User info + 体 + 面（`both` 转正 + consolidation top-K） | O(1) | **常驻 system prompt**（对接 `MemoryDigest.identityFiles`，prompt cache 前缀近乎免费） |
| 点/边/收据 | O(n) | 联想检索按命中取 |

**转正 = 常驻，shadow = 只可检索**——晋升门槛（§4.5）由此获得消费端意义。常驻块有 top-K 预算上限。社区算法（面的涌现）= **巩固时的 self-attention**（涌现质心为 Q、迭代到收敛），跑在夜间；它把反复注意的模式固化成面 → 常驻 = 给读时 cross-attention 预计算缓存。

### 3.2 读的唯一入口：现在质询过去（联想式召回）

**读没有模式之分——不存在独立的“提问模式”。** Mens 没有“用户向记忆库提问”的产品面；盘点记忆的全部消费者，每一个的 Q 都是现在进行时：

| 消费者 | Q 是什么 |
|---|---|
| run grounding（`$MENS_PROMPT` 前缀） | 任务 prompt + workthread |
| session 末巩固器 | 刚结束的这一场 |
| MCP pull（agent 中途来问） | agent 此刻的问题文本 |
| Phase 3 真预测 | 当下 context（定义即如此） |

显式问题只是语义密度较高的现在素材。由此读写完全对称，时态纪律在读侧闭环：

```
写：现在（session 末）─蒸馏→ 点/边 ─→ 长进图（巩固）
读：现在（每一刻）  ─蒸馏→ 多槽 Q ─→ 撞进图（联想）─→ 沿边扩散 ─→ 拉树链召回
```

即 **spreading activation**：当下命中图中同名点，激活沿边扩散、按边强度加权、随距离衰减、超预算截断。LLM 类比闭合：**蒸馏现在 = 算 Q，图的身份索引 = K，边 = 学到的关联，召回 = QK 命中拉 V（树链）**。

**Q 构造零 LLM**（热路红线由此保住）：实体槽 = 对图 roster（identity+别名集，NFKC）做确定性别名匹配（哈希，微秒）；场景/时间槽 = 当前 app/scope/时刻，免费；词面槽 = 屏上字面词。唯一要 embedding 调用的是语义槽——**按成本降频**：挂事件边界（workthread 更新/session 软切/burst 合并窗口）跑并缓存；高语义密度 Q（MCP 显式提问、grounding 构造时）同步跑一次。这是成本调度，不是模式。自增强回路：图越大 → roster 越全 → 运行时“看见谁”的识别越强——**权重反过来武装感知**。

退化兜底：槽全空（无实体命中、embedding 不可用）→ fail-open 到新近+场景（“我刚才在干嘛”），读路径永不阻断。

### 3.3 六头检索（5W1H ↔ 数据列双射 = 完备，非清单）

**① 寻址头（6）**：

| 疑问槽 | 头 | K | 索引 | Q 构造成本 |
|---|---|---|---|---|
| What·义 | 语义头 | 内容嵌入 | ANN | embedding 调用（降频+缓存） |
| What·形 | 词面头 | BM25 | FTS5 | 免费 |
| Who | 实体头 | identity/别名 | 哈希 | 免费（别名匹配） |
| When | 时间头（双钟：有效+事务） | 四个时间列 | B-tree | 免费 |
| Where | 场景头 | scope/source/app | 枚举 | 免费 |
| Why/How | 关系头 | 6 谓词 | 枚举 | 免费（图遍历） |

**② 先验偏置（3）**：强度（`observations` + **`recall_count`——读也是强化**，检索命中即计数，testing effect）· 新近（时间衰减）· 近根（到 USER 最强路径 = 记忆度）。
**③ 策略闸（2）**：status（shadow 不出场）· provenance（inferred 降权）+ 隐私围栏。

**头权重无模式开关，从槽占用涌现**：当下素材实体密集（屏上全是人名）→ 实体头多路命中，RRF 自动抬权；素材是一段纯散文 → 语义/词面头扛分；缺席的槽 = 零票。RRF 的本职就是融合不齐的多路信号，无需人工切换。这也统一了路由学习的信号（一种 Q、一种反馈）：日后对比学习学的就是这组融合权重，成熟后 MoE 稀疏激活 + 死头监控。

完备性：数据侧逐列归位无剩余；加头规则同谓词矩阵——占不到未覆盖的格子不许加。**每头一桶“只有它能答”的 golden 准入**。

### 3.4 检索单元 = 树链（不是原子）

**原子是打分单位，链是交付单位。** 执行序（全部 P1 设计本体）：

1. **硬头先砍**（实体/时间/场景/关系精确索引剪枝）+ **early exit**（唯一高置信命中即出）；
2. **软头排序**（语义 ANN + BM25 只扫残余）；
3. **多路 RRF + 三偏置选锚**（推广已生产的 2 头 RRF）+ **检索温度/MMR——消费端旋钮**（grounding 低温求准；DR 式任务高温+MMR 求广；由调用方传参，不是模式）；
4. **骨架拉链**：锚 → USER 的路径，**束搜索** top-b 链（分数 = 路径瓶颈强度）；多链在根处共享前缀 → **并成一棵以 USER 为根的小子树**（束搜索是“从锚拉回根”，§3.2 的扩散是“从锚往外亮”，共用同一套边权）；
5. **序列化交付**：路径即叙事（`USER →works_on→ 项目X →involves→ 某人`，每跳带 label/时间/强度 + **收据指针**）；消费方按 §2.1 渐进式披露按需下钻（event md → AX text → 截图），从不默认内联；超预算做 **prompt 压缩**而非砍链。

as-of-T 下 = 在可持久化结构上做树链查询（主席树 + 树链合体）。命中原子/边 `recall_count++`。
两个 P2 新能力：**推测性预取**（闲时按 workthread 预拉子树，回忆零延迟——联想模式的天然延伸）· **记忆当少样本**（捞用户自己的行为样例做 few-shot——让 LLM 模仿你，不只是知道你）。

---

## 4. 写：event → Memory（单干多头）

接线全景见 pipeline mockup。**热路（capture→timeline）零 LLM、零改动。**

### 4.1 时态闸：session 末的唯一一次 LLM 阅读

session 三刀切（硬切/软切/超时）后，**巩固器 consolidator**（原 reducer 与慢路阅读合并为一次调用，prompt cache 命中）读全场，**一次阅读、多头输出**：

- ① `event-*.md` —— durable 陈述（raw 符号收据层）；
- ② **`memory_delta`** `{entities, assertions, relations, events}` —— 结构化抽取物，一个 JSON 收编原 person 名源 / relation LLM pass / case 抽取 / classifier 归属四条散路。
  各头字段随退化轴修复补齐（审计）：`entities` 带 `kind`（闭集
  person|org|project|artifact——点·种类轴的**唯一**分型生产者）+ `ended`（该实体的
  有效期在本 session 被证据宣告结束，如离职/项目收尾）；`relations` 带 `polarity`
  （闭集 `+`/`-`/`0`，默认 `0`，仅 quote 明确带情感极性才标 ±）+ `ended`（quote 明确
  说这段关系结束了）。ended 与其它字段同受 quote 闸——摘不出结束语言就不许标。**entities 是 roster 选择题**（prompt 里带已知 roster，输出已知 identity 引用或显式 `new_entity` 声明）——约束解码，不输出裸字符串去撞库。

原则：**判断归 LLM（仅此一处），身份归代码（§4.3），强度归计数。**

### 4.2 确定性 apply（零 LLM）

- **点**：身份消解走 §4.3 的单一漏斗（NFKC 规范名 + 别名集 → SUPERSEDE 合并，`sightings++`）；低置信单见不上提；done 终态铸 `event:<id>` Activity 点（consumed=user_committed，resolved/completed=inferred）。
- **边**：6 谓词 + 端点闭集校验（非法即拒）；**强度 = observations = 证据数**（sightings/共现桶/终态事件=1），`reinforce_edge` 单调 MAX 棘轮——同数据重跑 no-op，新证据才涨；无向 knows 用规范序去重。LLM 提议的关系过三重闸：quote 原文摘自过去文本 / src·dst 在已知 roster / 置信 ≥0.7——缺一即弃。
- 全部写 **shadow**，eval 放行前不进检索。

### 4.3 身份消解：两个体系的接缝（entity linking）

> **接纳闸（§1.2 良构判据的执行点）**：漏斗消解的不只是「哪个名字是同一个人」，还包括
> 「这个名字配不配是点」——类/角色/泛称（群聊/客户/面试官/团队）不得铸实体；不可判的
> 留 shadow。良构性只有拿着证据才可判，所以闸的动作是**推迟**（shadow）而非武断拒绝。

LLM 输出自由字符串（“张总”），Memory 存规范 identity（`person:张伟`）——接缝原则：**能确定的用机制钉死，不能确定的让它可收敛、可测量**。不追求单次命中 100%，追求 miss 有去处、有记录、可收敛。

1. **选择题不填空**：见 §4.1——LLM 不负责命中，只负责判断；命中是代码的事。
2. **单一码本（tokenizer 对齐）**：身份消解只许有一个实现 `resolve_identity()`——巩固器 apply、联想 Q 构造（§3.2）、MCP pull 三处共用同一 NFKC 规范化、同一别名表、同一模糊层级。**分叉即漂移，漂移即 miss**（接线纪律，进 §6.3 验收单）。
3. **分层漏斗**：精确 hash → NFKC 规范化 → 别名集 → 模糊层（称谓剥离“张总→张”/拼音/编辑距离）→ 语义兜底（embedding 近邻，复用语义头索引）→ 仍不中：**不硬并**，铸 shadow 候选点 + 孤儿 TTL。纪律：**合并宁缺毋滥**（错并污染图——两个同名人并成一人），**候选宁滥毋缺**（漏配只是暂多一个孤儿，TTL 兜底）。
4. **miss = 训练数据**：巩固器 session 内共指消解出的别名（“张总”=张伟）**写回别名集**——读侧确定性匹配下次直接命中；每个 miss 只有两条路：后续证据并进已知点（别名集+1，命中率单调涨）或 30 天无边被遗忘（本就是噪声）。命中率不是设计常数，是**随图生长单调收敛的量**——§3.2“权重反过来武装感知”的具体机制。OOV 不丢弃，走 byte-fallback（shadow 孤儿），高频者自然长进词表。

### 4.4 夜间“睡眠”（既有 tick，零新 timer）

00:15 schema miner（签名/mined 路）· 00:20 enrichment（面聚类 emergent 路 + 对账转正）· 23:55 收割（孤儿 TTL / 语义矛盾自检〔两条 live 事实互斥→标记交 SUPERSEDE 或人裁〕/ 分级降精 / 快照+完整性）。

### 4.5 统一 schema（面/体是一个对象）

图的「面」和 daemon 的 `schema-*.md` 是**同一规律的两个投影**，统一为一个对象：

```
Schema: members(足迹) × signature(签名, md 投影) × provenance(mined|emergent|both)
        × observations/confidence/validity/bitemporal/status × level(1=面, 2=体, 同表递归)
```

**双抽取器喂同一对象**：miner 挖签名、聚类出足迹；足迹 Jaccard/签名匹配 → 确定性归并，provenance 升 **both** = 双信号 = 转正门槛（转正即常驻，§3.1）。P2 附加闸：重采样稳定性（证据子采样重聚类，簇不稳不转正）。

---


### 4.6 结束判定器（valid_to / valid_until 的收口者）

时效孪生轴（边 `valid_to` · 点 `valid_until`）此前**没有任何生产者**（审计：
128 边全 open、59 实体全 NULL——historical 渲染与 as-of 回放的历史侧永远空转）。三条腿，
全部证据驱动（§1.5-6 不变量：禁静默超时收口）：

1. **delta 结束信号**（主路）：§4.1 的 `ended` 字段 → apply 时对匹配边调 `close_edge`
   （幂等、只收口不删）、对实体节点回填 `valid_until`。Phase 1 apply 落地前 shadow 攒量。
2. **人裁联动**（已接线）：`contradictions-resolve --keep A` 裁掉 B 时，同步收口
   quote 出自 B 正文的 relation_edges（`close_edges_quoted_in`，确定性子串匹配、
   有界、记日志）——人裁是最可信的结束信号。
3. **终态三分的 resolved 腿**：`auto_close_resolved_enabled`（已建默认 off）
   消费识别器 `resolutions` 证据——**Phase 0 砍了识别器后此腿无生产者**，随 Phase 3
   真预测复活一起激活，不单独翻。

## 5. 全局红线

1. **热路零 LLM**；2. **单 choke point**（写 = session 末巩固调用 + 夜间 tick；读 = 联想检索单入口〔§3.2 蒸馏 Q → `search_hybrid`〕 + `MemoryDigest` 常驻；身份消解 = 单实现 `resolve_identity`，写读三处共用〔§4.3〕）；3. **默认 OFF / shadow / fail-open**（图坏了退回平铺 grounding，绝不阻断）；4. **隐私 place-never-send**（序列化进 prompt 一律包 untrusted fence；**截图属最高敏级**——只在显式下钻时进 prompt，永不随链默认内联）；5. **daemon-free 保住**（`relation_edges` 在 `index.db`，消费产品（Mens）Swift 侧纯 SQL 可拉链）；6. 图只削认知熵，不承诺偶然熵（诚实先于聪明）。

---

## 6. 执行计划

### 6.1 处置图（砍留判据：叙述现在的砍，巩固过去的留）

**❌ 砍（Phase 0 全部默认翻转，零删码可回滚）**：快路 K1（每 capture LLM 全家桶）`fast_path=off` · 慢路的 intents 输出通道（留阅读换 delta）· intent sink 全家（停喂冻结）· active 层 + 消费产品侧弹卡 + `.context` 生成 `active.enabled=off` · armed activator。

**✅ 留**：Memory 生产线全部（capture→…→retrieval）+ expiry 收割三件套（清存量）+ workthread（诚实的 Runtime 层）+ meeting/voice + 反馈管道（休眠，Phase 3 复活）。

**🔄 改**：session 末读全场调用换牌——识别器 → 巩固器（§4.1）。

### 6.2 分期

| Phase | 内容 | 交付判据 |
|---|---|---|
| **0 · 砍** | 默认翻转 + memory_delta 通道（shadow 双跑起步） | app 安静化；常开 LLM 成本降到 session 末一次；接线验收单① |
| **1 · 建** | **第一件事：memory benchmark + B0 基线冻结**（§7，给现状打分存档，之后每步有参照）；delta 双跑对拍（每头一桶 golden）→ 平价退役四抽取器；**单一 `resolve_identity` + 分层漏斗**（§4.3，delta apply 与读侧共用）；联想 Q 构造（零 LLM 蒸馏现在，§3.2）+ 六头 RRF + 树链 + recall_count + early exit + 温度/MMR + 压缩；as-of-T API；both 转正→常驻投影 | 检索 golden（关系/多跳/时序/who/where）+ 接缝 golden（§7.1）过噪声带 vs B0；双跑平价；夜间 tick 瘦身 |
| **2 · 深化** | schema_faces 落库 + 重采样闸 · 矛盾自检 · 分级遗忘（**含像素轴**：截图按龄降精，§2.1） · 推测预取 · 记忆当少样本 | 各自 eval 门 |
| **3 · 真预测重生** | 现在时 context 为 Q、Memory 为 KV 的 attention 式预测；反馈管道复活为 RLHF | 这才叫 intention prediction |

### 6.3 链路洁净纪律（防“写了但没接上”）

1. **禁孤儿逻辑**：任何落地代码必处两态之一——**ON + activation 测试**（证明真实链路中被执行，如 delta 确实被 apply 消费写进表）或 **SHADOW + 双跑消费者**（对拍 eval 在读它）。两者皆无 = review 必打回。
2. **每 Phase 交付带接线验收单**：flag 状态表（on/off/shadow、谁消费）+ 每条新路径的 activation 证据。无验收单不算交付。
3. **默认翻转与文档/eval 门同 PR**。

### 6.4 终局清除（脏代码物理删除，不留僵尸）

**删除门**：Phase 1 判据全绿 + Phase 0 安静运行 ≥2 周无回滚诉求 + 删除 PR 全闸绿。
**删除清单**：快路全家（event_source 闸/节流/退避、fast_path_ticks 及其 API、activator）· sink 喂识别器部分（cooldown/R3/schema-feedback；被 delta apply 复用的去重/temporal grounding 改挂新主不删）· active 层（active-tick/pending_actions/弹卡路径/proposal LLM/.context 生成端）· **配套 config flags 本身**（永久 off 的开关就是僵尸）· 对应测试/golden/telemetry 写入端/文档行/selftest 场景。
**保留**：`intents` 表 schema（只读历史）· 收割件（清完存量随最后一批删）· 休眠反馈管道 · workthread。
**仪式**：专项删除 PR，逐条列删除物，不夹带任何新功能。

> **执行记录（`refactor(daemon): delete cut prediction pipeline` PR）**：产品方拍板**豁免「安静 ≥2 周」门**，保留「全闸绿 + 只删不夹带」。已按清单物理删除:
> - **快路全家**（13 叶子模块）：`intent/{event_source,fast_recognizer,activator,escalation,reconcile,softnag_tracker,taste_profile}.py` · `store/{fast_path_ticks,recognition_ticks,softnag_snapshots}.py`
> - **慢路识别器**：`intent/recognizer.py`（`recognize_session`/pregate/escalation/finalize sweep）
> - **active 层**：`writer/active.py` · `store/pending.py`（`pending_actions` + `active-tick`）
> - **接线**：capture `post_capture_hook` · timeline block-flush hook · `active-tick` TaskDefinition · `/actions` + fast-path/recognition stats + `inject_intent` 路由 · `list_pending_actions`/`mark_action_done` MCP 工具
> - **flags**：`ActiveConfig` · `IntentRecognizerConfig.{enabled,fast_path,event_intent_enabled}` · `_apply_phase0_migration`（daemon 侧）· `[active]` toml
> - **配套**：cut 测试/golden/bench · `prompts/active.md` · pre-push 闸从 intent-golden/recognizer-snapshot 层重指向到 memory/graph 确定性 eval · daemon 文档相关行
>
> **一处原则性偏离**：清单写「删 sink 喂识别器部分（cooldown/R3/schema-feedback）」，但实测 `intent.sink` 是**共享基建**——MCP 建意图 + 反馈闭环仍在用它的去重/fold/cooldown，非识别器专属。故 **sink 整体保留**，只删其原主调用方（recognizer）;同理 `IntentRecognizerConfig` **类本身保留**（承载 sink/cooldown/schema 配置），只删死开关字段。删 sink 内部会改保留路径行为，违「只删不夹带」。
> **保留验证**：离线全量 daemon 测试 **2421 passed / 0 failed**;消费产品（Mens）侧 `Settings.normalize` 的 `phase0ProactiveCutApplied` shim 未触碰。

### 6.5 代价与回滚

主动式门面在里程碑前熄灯——接受（弹错的复利损失比安静贵）。Phase 0 一个 revert 恢复；Phase 1 退役以双跑平价为前提。

---

## 7. 评测体系（先建秤，再优化）

方法论对齐数据驱动迭代方法论 + harness-loop：**评测体系本身是一等交付物，不是事后补票**——秤不建好，“Memory 建成了”无法声明，后续一切优化无从谈起。两个用途：①冒烟（回归不出门）；②优化基线（每个改动有“比什么”、有噪声带判赢）。

### 7.1 三层金字塔

| 层 | 内容 | 何时跑 | 用途 |
|---|---|---|---|
| **冒烟**（秒级 · 确定性 · `MENS_CONTEXT_LLM_MOCK=1`，零 LLM key） | schema/DAO 不变量测试 + 漏斗确定层（§4.3）+ 检索确定性档（关系图 golden 确定档已在 pre-push） | **每次 push**（pre-push 闸，沿 intent-golden 既例） | 回归不出门 |
| **组件**（分钟级 · golden 桶） | ① 每头一桶独赢 golden（关系/多跳/时序已有，补 who/where）——头是数据准入的；② 接缝 golden（别名变体/称谓/拼音→规范 id，§4.3）；③ delta 双跑对拍（vs 被收编的四条散路，产物平价才退役）；④ 抽取 precision（shadow 期抽样幻觉边率） | 改到对应组件时 + 夜间 | 坏了能定位到层 |
| **端到端**（真数据 · memory benchmark） | 真 `~/.mens`（Mens 产品侧根；persome-core 默认 `~/.persome`）+ 回放 fixtures（沿 session-replay 既例）：`联想 Q + 六头 + 树链` vs 基线，指标 = recall@k · 链完整率（命中能否拉通到 USER）· token 成本（压缩比即产品）；Q 侧两类样例：散文问题（语义/词面扛分）vs 实体密集现在快照（实体/场景扛分） | Phase 边界 + 大改后 | 声明整体好坏 |

### 7.2 基线纪律

1. **B0 先冻结**：Phase 1 动手前，用同一套 benchmark 给**现状**（hybrid 2 头 + 平铺 grounding）打分存档——之后每个改动都有参照物。
2. **过噪声带才算赢**：同一 eval 重跑的方差 = 噪声带；增益小于噪声带 = 无效改动，不合（temperature-0.2 负结果的教训——别把噪声当成果）。
3. **基线滚动**：某版本过带转正后成为新基线 Bn；每次 eval 输出 json 随 golden 入库，优化史可追溯。
4. **生产计数器喂 eval**：mention→命中率 · 孤儿铸造率/**后并率**（后并率高 = 漏斗某层太保守，用数据调层）· recall_count 分布 · 命中链长分布——生产暴露的失效模式**铸成新 golden case**（“miss=训练数据”对 eval 自身同样成立）。

### 7.3 Runtime 自检

连通性/rollup 完整性检查进夜间 tick（`integrity_check_runs` + alert）——生产图的冒烟测试。

---

## 8. 现状与范围

> **审计**：spec vs 现状全面对照见相关审计文档。
> 结论：不一致的大头是**甲 部署断层**（分支未上线，running=pre-rebuild 旧管线仍在跑 fast_path/识别器）
> + **乙 Phase-1 未建**（memory_delta apply 通道不存在、classifier 未退役 → 点层仍 classify 式稀疏；
> §4.6 结束判定器 inert、§1.5 不变量未强制）；**丙 真漂移审后归零**（双消解/append-only 都是设计内）。

- **已发货（P0，已合 main）**：`relation_edges`（bitemporal + 6 谓词闭集 + observations 棘轮）· 过去层抽取器（生命周期切分 + Activity 通道 + 证据数强化）· 关系/多跳/时序 golden + 确定性档进 pre-push 闸。全部默认 OFF/shadow。
- **本 epic（Phase 0-2）**：见 §6.2；所有子 PR base 到 `refactor/memory-rebuild`，里程碑达标后整体回 main。
- **超出本 epic**：Phase 3 真预测、③级原生注入（本地模型）、场景适配器（P3 岔口）、因果边。

---

### 7-6 记忆可视化（dev 看板 · mockup 为权威 · 完备分类）

用户建立的 Memory 的**可视化真身**：`GET /dev/memory`（dev gate 下）把真库渲染成
定稿画布 mockup——**mockup 即验收权威**。数据来自
`GET /dev/memory-graph`。视觉语言按元认知纪律定义：**每个几何维度先列正交状态轴，再把
每个轴映射到恰好一个视觉通道**（笛卡尔积全覆盖，无重载、无空格；lens 只切换颜色通道，
位置是稳定态的纯函数——as-of 回放只改可见性/颜色，布局不跳）。

**点（0 维 = 事物）—— 轴 × 通道**：

| 状态轴 | 值域 | 视觉通道 | 映射 |
|---|---|---|---|
| kind 种类 | self·person·org·project·artifact·event | **方位角扇区 θ** | self=原点；person [0°,200°)、org [200°,253°)、project [253°,306°)、artifact [306°,360°)；event 不占扇区（高度带已区分，全周分布） |
| 时态/validity | 连续新近度 × {live·historical·terminal} | **高度 y（连续下沉轴）** | y = 新近被观察的浮在 live 水面，按「距上次证据的天数」连续下沉（~24 天沉满带）——**沉降是字面化的遗忘方向**；historical（边全收口）追加下沉一档；event 终态=底层环。修订：原三段离散带浪费连续通道 → 连续下沉；同日真库复审：年龄分布本身紧（5–15 天成团），度量映射（线性/对数）都保团 → **分位数等化**（按年龄排名铺满带——序保留、可区分由构造保证，精确天数在详情卡）。数据侧同批修复：`add_edge` 出生即戳 `last_observed_at`（原来只有 reinforce 写，生产 108/109 NULL 饿死此轴） |
| consolidation 证据强度 | observations 棘轮 | **半径 r**（近=强，§3.3 近根偏置）+ 大小（冗余强调同轴） | 无边者 r=外壳（自然滑向遗忘边缘） |
| 连通性 | in-component · orphan | **贴图** | 实心圆 vs 空心虚线环 + ghost 标签（§1.5-2 孤儿区；连通性≠时态，只占贴图一个通道） |
| lens 填充色 | 种类色 / validity 色 / 记忆度渐变 | **颜色**（唯一随 lens 变的通道） | 记忆度=到 USER 最强路径瓶颈强度（Bellman，historical 边 ×0.5） |
| provenance | confirmed·inferred | （端点未载——详情卡诚实缺席） | |

**边（1 维 = 关系）—— 轴 × 通道**（遮蔽序：historical 灰 > lens 色；透明度 = status × 时效 × 聚焦）：

| 状态轴 | 视觉通道 |
|---|---|
| observations 证据棘轮 | **粗细**（恒开） |
| 时效 live/historical | historical = 灰+细+低透明，仍连通（最高优先，压过 lens 色） |
| status shadow/active | **透明度**（shadow 恒暗；标签只给 active 或 obs≥3——控噪） |
| 作用面族 | mod lens 色：结构(part_of/reports_to/depends_on) 蓝 · 动态(participates_in) 橙 · 指涉(about) 绿 · 亲和(knows) 紫 |
| 方向 | dir lens 锥头：规范方向单头 / knows 无向双头 |
| 极性 | val lens 色：＋绿 −红 0 灰 |
| provenance/confidence | 详情层（不占几何通道） |

**面（2 维 = 点因边涌现的簇）**：颜色 = **面身份色**（face_id 确定性色相——mockup 每面一色；
provenance 走**线型**：both=实线亮框+0.15 填充 / 单路=虚线弱框+0.05 填充；status ✓/·shadow 进
标签）。**角的位置 = 它涌现自的锚点实体的坐标，绝不含 USER**（体才锚根）。按可见非孤儿锚点数
n 完备分派：

| n | 渲染 |
|---|---|
| ≥3 | 凸包（角=锚点坐标），质心标签 `面▸签名 [prov✓/·shadow]` |
| 2 | 双锚半透梭（粗圆柱），中点标签——2 点撑不起 2 维，梭是诚实的降维 |
| 1 | 锚点光环（虚线环贴该实体），标签随行——「关于这一个事物的规律」 |
| 0 | USER 上方塔板（无空间落点的诚实 fallback） |

**体（3 维 = 面之上同一操作递归）**：颜色 = 体身份色；框恒实线（0.45）+ 0.03 填充。**角 =
成员面锚点的并集 ∪ USER**（§1.5-3 逐层锚根，mockup 语义）。按可见非孤儿锚数 m 分派：
m≥2 → 凸包(锚∪USER)；m=1 → USER↔锚半透梭；m=0 → 高层塔板。

**派生物必须从点的 raw 重建**（产品方裁定）：边/面/体全部是派生层——点集清理（测试
实体 shadow）后，`relation_edges` 从 person_graph 时间线+终态 intents 重铸（证据时间
valid_from），`schema_faces` 清表后从当前事实底座重新 mine+sweep（跑两轮=两个足迹快照，
稳定面自然转正）。锚推导覆盖**足迹事实正文**（不只签名）：`_face_anchors(source, signature,
facts)` = source 实体（person-/org-）∪ user-* → self ∪ `scan_mentions(签名+全部事实文本,
roster)`——锚太稀会让面退化成光环/塔板。

**为此补的数据（storage 决定）**：`relation_edges` 持久列 `src_kind`/`dst_kind`（EntityKind）
+ `polarity`（闭集 `+`/`-`/`0`，默认 `0`）；`schema_faces` 的 `anchors` 列。全部走
`_EXTRA_COLUMNS` ALTER 回填，老库兼容。

入口：dev 看板「记忆图」tab（懒加载 iframe）；免重打包 override = `<root>/dev_memory.html`。

**生产退化审计（修复的事实依据）**——坍缩的轴全是「存储/渲染就绪、写侧无
生产者」的轴，修复=补生产者，按级联根排序：

| 退化轴 | 生产值域 | 缺的生产者 → 修法 |
|---|---|---|
| 点·kind | {self,person,event} | **实体分型**：delta `entities.kind` 已带闭集（§4.1）——apply 落地即解锁；过渡：图端点直读 org-*/project-* 实体文件为分型点（无边=诚实孤儿）；存量误收（org 被记成 person）出人裁清单，不自动改 |
| 边·谓词族 | 2/6（knows·participates_in） | about/part_of/depends_on 需要非 person 端点——kind 修复的级联；reports_to=0 是 data-bound（证据语料无层级语言，探针已证 LLM 腿正常） |
| 边·极性 | 全 `0` | delta `relations.polarity`（§4.1）；确定性腿恒 `0` 正确 |
| 边·时效 / 点·validity | 全 open / 全 NULL | §4.6 结束判定器三条腿 |
| 终态三分 | 只 consumed | resolved=auto_close（Phase 3 复活激活）；completed=app 反馈回路 data-bound |
| 边·recall_count | 全 0 | 无需修——读路 cutover 刚切，等真实查询流量 |

健康轴（真实分布，通道有效）：observations 1–92 长尾 · confidence 五档 · provenance
双值 · 面 provenance/status/level/锚 · 点 consolidation · valid_from 15 天纵深。
时间轴 = 客户端 f(T)（真 bitemporal 回放 + ▶ 播放）；边 `valid_from` 必须是**证据时间**
（首见桶/共现桶/intent ts），否则时间轴只剩「bootstrap 前/后」两态。

### 7-8 联想头增益解锁（E→A+B，平价复盘的执行）

§7-3 落到 0.3 权重后的现状是**精确平价 = 参与但零贡献**。三个根因（golden 饱和 /
关系头 ACTIVE 饥饿 active=10/109 / 槽池 LIKE 与文本头重复投票）按序解锁：

- **E · golden 去饱和**：检索 golden 新增 `relation_shadow` 对抗桶（3 case，生产拓扑
  匿名化——「人→其 shadow works_on 项目」「shadow 共现搭档」「二跳全 shadow 链」）+
  每条配一个**同 concept 硬负例**（dense 自信捞错的 plausible-but-wrong——没有它，小
  语料零相似平序是抽签）。附带修复 eval 诚实性：`_dense_pool` 相似度下限改**严格正**
  （零/负余弦不是候选；te3 空间 no-op）。目标矩阵钉进 B7 基线：bm25/hybrid/
  associative(active-only) 三者 0.0（真对抗+饥饿被测量）、`associative_shadow` 列 1.0
  （增益通道）、全部既有桶与 B6 等值。
- **A · 关系头喂食**：`neighbors(include_shadow=)` + `search_associative(
  relation_include_shadow=)` —— shadow-ONLY 可达名单独成池 ×0.5 降权（未证明永不盖过
  已转正）；依据 = edge-audit 全量 0% 结构幻觉。配置 `[search] relation_include_shadow`
  默认关。
- **B · 转正扇出**：`promote_edges` 缺省 10→20（`edge_promote_fanout` 可配，tick 接线）。
  注意既有语义：promote 只转正 knows（predicate 参数缺省）——participates_in 大边
  （self→项目 ×134）留 shadow，由 A 的 shadow 池覆盖。
- **sweep 复跑判决**（3 seeds×200 auto-golden，shadow×{top10,top20} 网格）：全配置
  overall 0.4967 / slotted 0.6235 **分毫不差** = 零回退；该仪器结构上测不到关系跳增益
  （auto-golden 的 gold 从自身内容采样，跳边只添邻居噪声）——增益的尺是对抗桶（0→1）。
  对照 hybrid slotted 0.667 的 −4.4pp 差 = 70 条 slotted 里 3 次命中、种子间不一致，
  在单命中噪声带内，待更大 n 复核。

**§7-8 续 · 权重调优判决（尺与燃料就位后）**：

- **确定性网格**（slot{0.1–0.7}×rel{0.1–1.0} 共 20 配置）：全桶恒 1.0——含对抗桶。
  结构性结论：小语料 + rrf_k=20 下 RRF 算术是「池成员身份 ≫ 池内排名 ≫ 权重」，
  确定性 golden 天生是 **0/1 通路尺**不是权重尺；在其中雕刻权重敏感 case = 手工雕
  RRF 算术（拒绝）。权重敏感性只存在于大语料。
- **真库关系探针集**（`production_baseline --relation-probes`，12 条从真实 shadow
  拓扑挖的 hop 查询，gold=提及目标不提查询人的 live entries）——第一把权重尺：

  | 配置 | recall@10 |
  |---|---|
  | 文本双头 / rel≤0.3（shadow 开或关） | 4/12 |
  | rel=0.5 + shadow | 5/12 |
  | rel=1.0 + shadow | **7/12（+25pp）** |

- **auto-golden 回归护带**（3 seeds×200，slot 固定 0.3）：rel 0.0↔1.0 **逐字节等值**
  （关系池对非关系查询零扰动——提权免费）。
- **判决：`relation_pool_weight` 默认 0.3→1.0（弱支配：处处≥、关系类严格>）**；
  slot 维持 0.3（1.0 的回退判决仍然有效，且无提权增益证据）。剩余 headroom：
  5/12 探针在任何权重下都不中（contains-pool 排名问题，非权重问题——下一个杠杆）。
- **cutover 状态判定：GO**——从「精确平价」升级为「关系类查询 +25pp、其余零扰动」。

### 7-9 contains-pool 公平分席（§7-8 残余 5/12 的修复）

**诊断**（探针逐条取证）：5/12 不中的根因不是池内排序、不是数据缺失，是**逐 needle
顺序灌池 + 全局截断的结构性饥饿**——44 个可达邻居按字母序展开，第一个 needle（某人X）
独占全部 50 席，字母序靠后的目标（某人丙/研发会/某总戊）0 席（搭便车席位除外）。诊断表：

| 探针 | 目标可达 | 邻居数 | 目标字母序位 | 池内席位（修前） |
|---|---|---|---|---|
| 用户甲→某人丙 | ✓ | 44 | 18 | 2（搭便车） |
| 用户乙→某人丙（某医学院） | ✓ | 44 | 18 | 2 |
| 某人（某模型团队）→某人丁 | ✓ | 44 | 21 | 9 |
| 用户甲→研发会 | ✓ | 44 | 29 | **0** |
| 用户乙→某总戊 | ✓ | 44 | 9 | 2 |

**修法：round-robin 公平分席**——每 needle 轮流取一席（各自池内保持 newest-first），
44 needle × 50 席 = 人人保底；单 needle 池行为不变。**探针 7/12 → 9/12**（生产默认
rel=1.0 档）；auto-golden 回归 rel 0.0↔1.0 仍逐字节等值；B8 确定性对拍原样通过
（golden needle 集仅 1-2 个，公平分席不改变命中集——无漂移故**不滚基线**，滚全同基线
无意义）。

**负结果（已回滚，防重试）**：needle 按边强度排序（observations 优先拿席）实测
9/12 → 8/12——强 hub 把弱边目标的席位推进 RRF 排位尾部；字母序轮转的中立性是
数据验证过的选择。

**残余 2-3/12 定性**：目标在池内有席（1-3 个）但 RRF 排位尾部出 top-10——席位落在
"每 needle 最新一条"上而最新未必是 gold。下一杠杆 = **池内查询感知排序**（池候选按
与 Q 的 dense 相似度重排——需要池级向量重排机制，独立立项，不在本轮硬凑）。

### 7-10 池内 dense 混排（§7-9 残余 3/12 的杠杆）

**机制**：contains 类池（entity/scene/relation/relation_shadow）取完席位后、进 RRF
融合前，对池内候选做**查询感知重排**（`fts._rerank_by_query_sim`）：te3 向量表里逐
候选算与 Q 的余弦，得 sim 序。放行判据（探针）驱动的两轮设计：

- **纯 sim 替换 recency 序 = 负结果（已回滚，防重试）**：9/12 → 8/12。赢下
  席位落在最新非 gold 条目的 case，但输掉 3 条含时间意图的探针——那些查询含「最近/现在/状态」，
  **recency 本身就是查询意图**，丢弃它是把一个信号换另一个，不是叠加。
- **定稿：recency⊕sim 池内 RRF 混排**——`_rrf_fuse(ids, sim_order, rrf_k=5)`（recency
  序与 sim 序做池内小 RRF，两序都靠前者胜出）。**探针 9/12 → 10/12**（生产默认
  rel=1.0+shadow 档，达标）。

**旋钮**：`SearchConfig.contains_pool_rerank`（默认 **true**；关=回纯 recency 序）；
无向量候选缀尾、embedder 缺失/异常 fail-open 返回原序。重排块放在 early-exit
**之后**——「唯一硬命中 → dense 永不跑」的时延不变量原样保留（order-only change）。

**auto-golden 归因 cell（同 run 受控对比，rel × rerank 四格）**：rel=0.3 档 rerank
on/off 逐字节等值（低权重下零扰动）；rel=1.0 档 rerank ON 0.7054 vs OFF 0.6902
（**+1.5pp，重排是正贡献**）。初测吓人的 rel=1.0 slotted −4.6pp 归因于 **rel=1.0
本身**（rerank 关着也在，§7-8 已接受的 trade-off；跨 run ~2pp 漂移属噪声带）——
不是本轮变更的回归。B8 确定性对拍原样通过（20/20 无漂移，故不滚基线）。

**残余 2/12 定性（语料粒度，非排序问题）**：目标信息主要活在人物条目里的两条探针——
gold 判据排除同 person 文件共提（`NOT LIKE person`），
而这两条的目标信息主要活在人物条目里；纯 sim 能捞回但代价是 recency 意图探针
（净 −1）。进一步增益需要 per-query 意图路由（含时间词→recency 权重高）或语料
粒度改善，独立立项，不在本轮硬凑。

### 7-7 建图标准流程 × 每步 oracle（训练权重的 SOP，审计）

「添加点、建图」= 训练这个人的权重 θ。流程存在但此前散落各节——收拢成一张**阶段 × 产物 ×
oracle** 表；没有 oracle 的阶段是诚实缺口（不许拿"跑通了"当"有效"）：

| # | 阶段 | 产物 | oracle / benchmark | 状态 |
|---|---|---|---|---|
| 1 | 感知采集（capture→timeline） | timeline_blocks | 无质量 oracle（仅 parser_ticks 遥测） | **缺口**（低优——零 LLM 确定性层） |
| 2 | 时态闸+巩固（session 末一读） | memory_delta 各头 | **delta 双跑对拍**（每头一桶 golden + parity 判决，确定性档进闸） | ✓ |
| 3 | 身份消解（漏斗） | 规范身份 | **identity golden**（歧义护栏/别名碰撞，进闸） | ✓ |
| 4 | 点的接纳（§1.2 维度判据） | 铸点/降影/归轴值 | **维度判据 golden**（`dimension_criterion.json` 22 案例 = 全部人裁裁定 + 奠基案例「群聊」+ 正例对照 + 边界推迟；确定性接纳闸 `evomem/dimension_criterion.py` 五步 MECE 链，四格裁定全覆盖，进 pre-push 闸） | ✓（LLM 档=delta 接纳原则，世界知识唯一性归它） |
| 5 | 边铸造（确定性腿+LLM 腿） | relation_edges shadow | 图 golden（进 benchmark）＋ **`mens edge-audit` 幻觉边率抽样**（`evomem/edge_audit.py`：observations 分层抽样（低证据超配+闲档下溢向低证据）；确定性档五检=端点存在/矩阵合法/kind 一致/源存在/quote 回溯（合成 quote 按构造复算——共现边复算共享分钟桶）；`--llm` 语义蕴含档默认关；10 案例 golden 进 pre-push 闸） | ✓ |
| 6 | 转正（边 promote / 面 maybe_promote） | active 集 | 边：**cutover sweep 判决**（权重×转正网格，已跑）；面：重采样闸单测 | ✓ / 面缺生产观测 |
| 7 | 面/体涌现（miner+sweeper） | schema_faces | 无质量 golden（只有折叠/转正机制测试） | **缺口 P2**：schema 质量 golden（签名可证伪性+足迹稳定性） |
| 8 | 遗忘/收口（decay·TTL·§4.6 结束判定） | 降精/降影/valid_to | decay 三反幻觉闸+report；结束判定器新建未验 | **缺口 P2**（等数据） |
| 9 | 检索消费（读=对权重的前向） | 命中+树链交付 | **memory_benchmark B0→B6 冻结基线**（六槽桶+图桶+chain_rate+token_cost，精确对拍进闸）+ `production_baseline --real` | ✓（最强的一环） |

纪律：任何对某阶段的"优化"必须以该行 oracle 的**过噪声带**为放行判据（data-driven-iteration
方法论）；oracle 缺口行先补 oracle 再谈优化。读侧（9）已是黄金标准——写侧要对齐到同等水平，
P1 两件（维度判据 golden、edge-audit 幻觉边率）是下一步。

## 附录 A：LLM 技术迁移地图（速查；已织入正文）

| 迁移件 | 出处 | 归宿 | 分期 |
|---|---|---|---|
| 联想式召回（QK 命中拉 V） | attention / spreading activation | §3.2 | **P1 必做** |
| 实体抽取选择题化 · OOV byte-fallback · 别名 BPE 式学得 | constrained decoding / tokenizer | §4.1 / §4.3 | **P1 必做** |
| 读也是强化 recall_count | KV-cache H2O / testing effect | §3.3 | **P1 必做** |
| early exit / 检索温度+MMR / 束搜索 / prompt 压缩 | 自适应计算 / 采样 / beam search / LLMLingua | §3.4 | **P1** |
| 推测性预取 / 记忆当少样本 | speculative decoding / ICL | §3.4 | P2 独立立项 |
| 重采样转正闸 / 分级遗忘 / 矛盾自检 | bagging / 量化 / Constitutional AI | §4.5 / §1.5 / §4.4 | P2 |
| 对比学习路由 + MoE 稀疏 | contrastive / MoE | §3.3 | 数据攒够 |
| 场景适配器 | LoRA | — | P3 岔口 |
| （已内建）蒸馏 / RLHF / attention sink / prompt cache | — | §4 / §6.2-P3 / §1.5 / §3.1 | 已在 |

## 附录 B：决策日志（考古，正文不再复述）

1. **时态纪律的三跳**：P0-2 初版从活跃 intents 抽边（将来污染权重）→ 过度矫正为整表拉黑（断了 Activity 通道）→ 定稿为**生命周期切分**（done 终态可进）。
2. **表示层箭头调转**：早期表述“符号是真相、向量只是索引”→ 定稿为“**Memory 本体=向量，raw=符号收据**”（对齐 LLM 类比到底）；SSOT 分层表述见 §2.1。
3. **数学口径修正**：非 cell complex（重叠簇破坏复形结构）→ **嵌套重叠超图塔**；`u*∈∂(B)` 是类型错误 → 传递 rollup；θ 为非参数记忆。
4. **检索单元升级**：1 跳邻域 → **树链**（1 跳是退化情形）。
5. **schema 命名统一**：图的“面”与 daemon `schema-*.md` 由“两个东西”定为**一个对象两投影**。
6. **边强度补全**：初版 dedup 直接 skip（重复观察不强化）→ observations 证据棘轮 + 读侧 recall_count，写读共构 consolidation 轴。
7. **抽取架构收敛**：8 个 LLM 阶段各读一遍原文（考古地层）→ **单干多头 memory_delta**（resolutions 通道为先例）。
8. **战略定调**：先砍伪意图识别再建 Memory（§0 判决）；epic 分支推进。
9. **读侧单模式化**（定稿轮）：初版隐含“文本查询”入口 → 中间版提出联想/提问双模式（联想只走实体+关系头）→ 被产品方两连追问收敛为**单入口**：联想模式照用六头 RRF（头权重从槽占用涌现，非模式开关）；再确认 Mens 无“向记忆库提问”的产品面（连 MCP 提问也是现在进行时素材）→ “提问模式”取消，读只有 §3.2 一个入口。
10. **接缝定稿（entity linking，§4.3）**：回答“怎么保证 LLM 抽取物命中 Memory”——不靠单次准确率，靠四机制：roster 选择题（约束解码）· 单一 `resolve_identity`（写读同码本）· 分层漏斗（合并宁缺毋滥/候选宁滥毋缺）· miss=训练数据（别名学得写回、孤儿 TTL 收敛）；配接缝 oracle（§7.1-② + §7.2-4）。
11. **raw 双投影 + 渐进式披露（§2.1）**：初版收据链到 `event-*.md` 即止 → 产品方指出 raw 还有 AX 提取 text 和对应截图两层 → 定稿：raw = 同一时刻的文本投影（AX→captures→md）+ 像素投影（截图，证据链最底层）；读侧四层渐进披露（链叙事→md→AX text→截图，默认只交付最上层，像素只在显式下钻时进 prompt）；像素按龄降精先于文本（分级遗忘的像素轴，P2）。
12. **评测体系升格（§7）**：原 §7 是五条验收清单 → 产品方要求“评测体系确保数据可优化 + 建基线”→ 升格为三层金字塔（冒烟秒级确定性进 pre-push / 组件 golden 桶 / 端到端 memory benchmark）+ 基线纪律（B0 冻结为 Phase 1 第一件事、过噪声带才算赢、基线滚动入库、生产失效模式铸成新 golden）。
13. 完整脑暴过程与前版 spec 见归档的前版设计文档。

---

Generated with Claude Code

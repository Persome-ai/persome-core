# Personome 相关工作研究笔记：Agent Memory 的三条路线

> 用途：为 Personome 技术报告（"Predict Your Next State"）的 Related Work / 定位服务的**参考文档**（非正文）。
> 收录方式：忠实转述来源；凡属推断显式标注「（推断）」。所有条目尽量带 arXiv id / 出处以便回查。
> 起点：arXiv:2606.29778（Mandol），由此顺藤梳理整条脉络。
> 日期：2026-07-06。

---

## 0. 定位坐标：记忆的三条路线（来自 Mandol §2.1）

Mandol 把 agent 记忆明确分成三条互斥路线，并**主动放弃两条、只做第一条**：

| 路线 | 定义 | 代表（Mandol 点名） | Mandol 态度 | Personome 立场 |
|---|---|---|---|---|
| **Explicit storage** | 记忆 = 可检索的显式存储（向量/图/文本） | Mem0 / Zep / MemOS / EverMemOS / 本文 | **采用** | 对立面（我们不止于此） |
| **Parametric memory** | 记忆 = 内化进**模型权重** | [3] Titans、[48] Retroformer | 放弃（"可解释性差、更新成本高"） | **我们押注这条** |
| **Latent memory** | 记忆 = 推理期的 hidden state / KV cache | [47] Memory³、[49] MemGen | 放弃（"预训练/多阶段训练成本高"） | 相邻可借 |

**Personome 的一句话定位**：重新拾起被系统派放弃、但对个性化**预测**更本质的 weights 路线——Memory 不是被查的静态库，而是被训的个人权重；配 loss/reward 反馈闭环随行为自迭代。

---

## 1. Mandol 详解（arXiv:2606.29778，2026-06-29，cs.DB）

- **标题/作者**：*Mandol: An Agglomerative Agent Memory System for Long-Term Conversations*。中科院软件所 + Microsoft Research。代码 github.com/AgentCombo/Mandol。
- **一句话**：把长期对话 agent 的记忆从"异构向量库+图库拼装"重构为**单进程、内存原生的统一数据结构**（SemanticMap + SemanticGraph），配**全程零 LLM 的定量检索管线**，准确率/token/延迟三维同时 SOTA。

### 1.1 骨架（三问三答）
1. **统一表示** → 分层记忆模型：
   - **basic 层** memory unit = raw text + **user intent** + 时空 metadata + 预训练模型编码的语义向量；memory space 逻辑隔离（一个 unit 可属多 space）；两类边——显式结构边（时间序/实体引用/状态更新，规则建）+ 隐式语义边（相似度，**按需算不存储**）。
   - **abstract 层**：LLM 通过 "abstraction-linking" 凝聚出 episodic（事件链）/ semantic（实体图）/ emotional（偏好演化链）三种抽象记忆；每个抽象节点存 `source_unit_uids` → **可回溯**到原始 basic 单元。
2. **统一存储 + 低延迟混合查询** → **SemanticMap**（KV，unit ID 为 key，三索引：倒排/dense/SPLADE 稀疏）+ **SemanticGraph**（轻量邻接表，只存显式边），**共享同一套 unit ID** → hybrid retrieval 成原子操作；冷数据 LRU/LFU 分页落 DuckDB。
3. **token 预算内准确检索（零 LLM）** → routing（意图分类器分配 per-source 预算）→ BM25+SPLADE+dense 并行召回 → RRF 融合 → selective subgraph expansion（补多跳证据）→ cross-encoder 重排得 S_ce → **MAD 动态阈值去噪**（τ = median − κ·MAD, κ=2.5）→ **arbitration 冲突消解**（S_arb = w_rel·S_ce + w_temp·f_time + w_source·f_source；f_time 时间指数衰减，f_source 置信度 basic1.0/epi0.8/sem0.6，新证据覆盖旧）→ **MMR** token 约束生成（λ·Rel − (1−λ)·Red，Red 按实体+源重叠算冗余）。

### 1.2 评估
- **数据集**：LoCoMo（10 段多会话，1986 问，Single/Multi-hop/Temporal/Open-domain）；LongMemEval（500 问，6 类：SS-Pref/SS-Asst/Temporal/Multi-S/Know.Upd./SS-User）。
- **基线**：Mem0 / MemU / Zep / MemOS / EverMemOS。backbone GPT-4o-mini / 4.1-mini；检索后端 Qwen3-Embedding-0.6B + bge-reranker-v2-m3。
- **头条**：LoCoMo 89.48%（+3.35 vs 最强基线 EverMemOS）；LongMemEval 85.0%（+7.2），token 少 17–20%；检索延迟对 MemOS Search mean ~5.4×、P99 ~8.2×。

### 1.3 关键：它是 Personome 的干净对立面
- §2.1 逐一点名 parametric / latent 两条路后**主动放弃**，原文："Explicit storage thus remains necessary in practical systems, and our work focuses on this direction."
- Mandol 的 Memory **不进前向、不训练、无反馈闭环**（w_rel/w_temp/w_source、κ、λ、1.0/0.8/0.6 全是手工超参）；Personome 的 Memory = 训练权重 + 反馈=RLHF + 随行为自迭代。→ **正交对立轴清晰**。

### 1.4 即便走 weights 路线仍可借的机制
1. memory unit 显式带 **user intent** 字段 + emotional/preference 的 **state-update 边 / 偏好演化链** → 现成的"用户状态随时间演化"监督来源。
2. **f_time 指数衰减 + f_source 置信度 + 冲突消解** → "记忆 reinforce/decay"的确定性可解释近似，可当我们学习式衰减的 baseline。
3. **`source_unit_uids` 可回溯抽象** → 给权重化记忆补可解释性的设计模板。
4. **MMR 相关-冗余权衡 + token 硬预算** → 我们把 context 喂预测头那步照样需要，可直接复用。
5. **LoCoMo / LongMemEval**（尤其 Temporal / Multi-session / Knowledge-update） → 现成 benchmark，可同台对打，用"意图/状态预测准确率"补充其 QA accuracy。

### 1.5 Mandol 点名、值得顺藤摸瓜的种子
- [3] **Titans**（Learning to Memorize at Test Time）— parametric / test-time。
- [48] **Retroformer** — parametric（反思式策略权重更新）。
- [47] **Memory³** — latent（explicit hidden-state memory）。
- [49] **MemGen** — latent。

---

## 2. 沿脉络的学术现状（2024–2026）

> 以下由并行文献研究补全（四支：parametric/test-time · latent/KV · explicit agent-memory landscape · personalization/next-action prediction）。
> 待填。

### 2.A 参数化记忆 & 测试时学习（Memory-as-Weights —— Personome 押注的这条）

**⭐ 第一梯队：记忆即权重，测试时用梯度/loss 写入（Personome 的机制母本）**

| 论文 | arXiv · 年月 | 机制（记忆存哪、怎么写） | 对 Personome |
|---|---|---|---|
| **Titans**（*Learning to Memorize at Test Time*） | 2501.00663 · Google · 2025-01 · NeurIPS'25 | 记忆 = 一个深层 **MLP 的权重**；写入门=**surprise(loss 梯度)+momentum**，配自适应**遗忘门**；更新规则本身元学习出来 | **"Memory=权重"的字面实现**；**把 surprise 门换成用户采纳/忽略奖励即成 RLHF 化个人写入器**。差异:通用序列压缩、非个性化、无人类反馈 |
| **GradMem**（*Write Context into Memory with Test-Time GD*） | 2603.13875 · ICML'26 | 冻结模型，只对**前缀 memory token** 做几步测试时 GD，写入=对重建 loss 迭代优化 | 把"写记忆"显式定义成**一次带 loss 的优化操作**=Personome 需要的"写入=学习"原语 |
| **TTT layers**（*RNNs with Expressive Hidden States*） | 2407.04620 · ICML'25 | 隐状态 = 一个小模型的**权重**(TTT-Linear/MLP)，每 token 对自监督 loss 做**一步 GD** | "隐状态即权重、更新即学习"最干净范式；"预测下一状态"可落成 TTT 式按人 online 模型 |
| **End-to-End TTT for Long Context** | 2512.23675 · 2025-12 | 测试时用 NTP 更新 **Transformer 本体权重**；训练时**元学习好初始化** | **"同一基座+每人一套权重" = meta-learned init + per-user test-time weights** 的技术对应 |
| **Nested Learning / Hope** | 2512.24695 · Google · NeurIPS'25 | "优化器即记忆"+自修改更新规则+**多时间尺度连续记忆**(不同频率权重块) | 给"分层权重=不同时间尺度记忆"理论骨架(秒级注意/日级习惯/长期画像各一层、各自更新频率) |
| **ATLAS** | 2505.23735 · Google · 2025-05 | Titans 升级，对**一段上下文窗口做最优记忆化**(非逐 token 贪心) | 对应 Personome 批量消化一段会话再更新权重 |
| **Test-time Regression**（统一框架） | 2501.12352 · Stanford · 2025-01 | 把"记忆化=测试时回归问题"，三选择涵盖线性注意力/SSM/DeltaNet | 设计个人记忆更新器的"设计空间地图" |

**⭐ 第二梯队：反馈闭环 / 个性化（最贴 Personome 的 RLHF-as-feedback + per-user）**

- **TSUBASA**（*Long-Horizon Personalization via Evolving Memory + Self-Learning w/ Context Distillation*）· **arXiv 2604.07894 · 2026-04** —— **唯一一篇明确"长周期按人个性化 + 把用户经验内化(≈写进权重)"**：写侧记忆动态演化,读侧 context distillation 把用户经验蒸馏进模型;Qwen-3 上比 Mem0/Memory-R1 帕累托更优。**可借"context distillation 内化"作为把外部记忆固化成个人权重的手段。**
- **On-the-Fly VLA Adaptation via Test-Time RL**（2601.06748 · 2026-01）—— **"反馈信号→测试时更新权重"闭环最清晰样例**:部署期在线 RL 精调策略、**保留 SFT 先验不灾难遗忘**;把"任务进度奖励"换成"用户采纳/忽略"即同构。
- **REFINE**（*Reinforced Fast Weights w/ Next-Sequence Prediction*，2602.16704 · Princeton/NAVER · 2026-02）—— 用 **RL(GRPO) 优化"快权重写入规则"本身**;对应 Personome 用采纳信号 RL 出更好的记忆更新策略。
- **Retroformer**（2308.02151 · Salesforce · ICLR'24；Mandol 点名的 [48]）—— **反馈闭环(奖励→改记忆)原型**,但记忆是**改写后的 prompt(文本非权重)**;提醒"记忆放 prompt vs 放权重"是可选轴。

**第三梯队：理论基础 / 评测警示**

- **Transformers Learn In-Context by Gradient Descent**（2212.07677 · ICML'23）—— 证明单层线性注意力的一步前向 = 对回归 loss 的一步 GD → 为"**上下文即隐式权重更新**"这一 Personome 核心类比提供理论合法性。
- **⚠️ Beyond Perplexity**（*Behavioral Evaluation for Deployment-Memory Claims in TTT*，**2607.00368 · CMU/MBZUAI · 2026-07-01 最新**）—— 批评 TTT 用 perplexity/loss 宣称"记住了"是**假信号**:实测一步 LoRA 更新后 support loss 降了但**自由回忆仍为零**。**直接影响 Personome eval 设计:别用 loss 下降宣称"学到了用户",要测行为级(无上下文时能否真复述/预测该用户)。**
- **Dynamic Cheatsheet**（2504.07952 · Stanford）—— 非参数文本记忆的对照锚:隐式成功反馈+记忆复用不改权重也能自改进 → Personome 应留作对照基线。
- **智能体记忆综述**（2602.06052，60 作者，三轴分类:载体/认知类型/**主体 agent-centric vs user-centric**）—— 领域地图与术语(user-centric 记忆)。

**检索命中未逐一 fetch（供延伸）**：FAAST(2605.04651,闭式一遍编译进快权重、免梯度写入器)、TNT(2511.07343)、LaCT(2601.00671)、Hebbian+梯度可塑性(2510.21908)、**Panini(2602.15156,论证参数化知识编辑容量小/易退化 vs 非参数可寻址——"写权重"的反方论据,须对照)**、**Substrate Asymmetry in User-Side Memory(2606.11712,直谈"用户侧记忆"载体不对称,Personome 是 user-side,值得回查)**。

**未核实(凭记忆,勿引 id)**：Ba et al. Fast Weights(2016)、Schmidhuber/Schlag fast-weight programmers、Learning-to-learn by GD²(2016)、DeltaNet/Gated DeltaNet、MAML。

---

## 3. 综合定位：Personome 在这张地图上的位置

### 3.1 一张坐标 + Personome 的落点
两条轴划出四个象限：**横轴 = 记忆载体**（外部文本/图 ↔ 内部权重/latent）；**纵轴 = 有无学习闭环**（静态 ↔ 反馈更新）。

- **外部 × 静态**：Mem0 / Zep / MemOS / A-Mem / HippoRAG / **Mandol** ——"被查的库"，绝大多数在此。
- **外部 × 学习**：Memory-R1 / MEM1 / Mem-α / Retroformer ——RL 优化"怎么存取"，但奖励=QA 正确率，记忆仍是文本。
- **内部 × 静态**：Memory³ / Cartridges / Larimar / Memory Layers ——记忆进 KV/参数，但离线写入、无反馈。
- **内部 × 学习**：**Titans / TTT / GradMem / End-to-End TTT / MemGen / On-the-Fly VLA / REFINE** ——测试时用梯度/RL 改权重。
- **× 个性化**（第三轴，正交）：**OPPU（per-user LoRA）/ Latent Personal Memory / TSUBASA / PUMA** ——把上面的机制按人隔离。

**Personome = 内部 × 学习 × 个性化 × 预测**四者交集，且预测目标是"用户下一状态/意图"——这个交集点**当前无人正落**：最接近的 Latent Personal Memory 缺"预测下一状态"的时序目标，PUMA 缺"记忆即持久 per-user 权重"，TSUBASA 缺显式人类反馈奖励，Titans 缺个性化与人类反馈。**这就是 Personome 的新颖性空位。**

### 3.2 必引 / 必区分的"贴身"论文（写作时逐一划界）
| 论文 | 与 Personome 重叠 | 我们必须讲清的差异 |
|---|---|---|
| **Latent Personal Memory**（2606.20911） | per-user latent 权重 + 学习闭环 | 它是输入侧 soft prompt、偏 response 个性化；我们是**预测下一状态的时序模型** |
| **PUMA**（2605.24647） | 把用户建成动力系统、预测下一状态 | 它 latent 状态是低维心理阶段、无持久 per-user 权重；我们**记忆=持久个人权重** |
| **OPPU**（2402.04401） | per-user LoRA = "记忆即权重"字面先例 | 它离线一次性；我们**在线随采纳/拒绝反馈自进化** |
| **Titans**（2501.00663） | 测试时用梯度改权重、惊奇门+遗忘门 | 它自监督 surprise、非个性化；我们**用户反馈门 + 按人隔离** |
| **TSUBASA**（2604.07894） | 长周期个性化 + 经验内化 | 它无显式人类奖励闭环；我们**拒绝=负样本的 RLHF** |
| **Mandol**（2606.29778） | 长期记忆系统 | 它显式声明放弃 weights 路线、无学习、不进前向；我们做它放弃的那条 |

### 3.3 三条可直接落地的机制借用（照搬即用）
1. **写入器**：把 `memory/*.md` 追加改成 **Titans/GradMem 式"对 loss 做几步 GD 更新一小块个人权重"**，写入门/遗忘门由**采纳/拒绝奖励**驱动（On-the-Fly VLA/REFINE 给了"在线 RL 更权重且不灾难遗忘基座"的范式）。
2. **基座↔个人**：**End-to-End TTT 的 meta-learned init + per-user test-time weights** = "同一基座换权重即换模型"的技术兑现；Nested Learning 的多时间尺度层 = 把点/线/面/体分层为不同更新频率的权重块。
3. **持有你的语料→context**：**Cartridges（self-study 蒸馏成可挂载 KV）+ Context Distillation** = "个人语料→便携 latent 包"，比每次重读 markdown 省且天然"换记忆=换模型"。

### 3.4 评测红线（写作与自研都要守）
- **别用 loss/perplexity 宣称"学到了用户"**——**Beyond Perplexity（2607.00368）**实测 loss 降但自由回忆为零。评测必须**行为级**：无上下文时能否真复述/预测该用户下一步。
- **协议照抄**：LaMP 的 **Time-split**（早期历史训、未来交互测）+ LifeSim-Eval / ChronosBench / BehaviorChain 的**长程多轮行为链**；用 **MEMPROBE**（隐式恢复用户隐藏状态）作"是否真学到你"的 oracle。
- **管理精度预期**：真实 next-action 极难（网购 17% / ProactiveMobile 19% / GPT-5 7%）——卖点不是绝对精度，而是**个人 memory 把 baseline 抬起来**；务必留一条**非参数对照臂**（现有 `memory/*.md`+`index.db`）量化"写进权重"的净增量，避免为参数化而参数化（Panini 2602.15156 是反方论据）。

### 3.5 下一步深读清单（未核实但疑似高相关，回查后再定位）
- **Mem-W: Latent Memory-Native GUI Agents**（2605.09317）—— latent 记忆 + GUI，与 Mens 的 AX/GUI 场景疑似最贴。
- ~~**Substrate Asymmetry in User-Side Memory**（2606.11712）~~ —— **已核实真实存在,见下方 §3.6（含命名冲突警告）**。
- ~~**From Storage to Experience**（2605.06716）+ **Rethinking Memory Mechanisms**（2602.06052）~~ —— **均已核实,见 §3.6 末（两篇是最强定位/叙事锚）**。

---

## 3.6 深读核实结论（§3.5 清单已逐篇打开验证）

### ✅ 硬点一（已解决）：命名冲突——"PERSOMA" 是一个现有的个性化**方法**；本项目已定名 **Personome**
从 Substrate Asymmetry 参考文献解析到确切引用并核实原文：**PERSOMA = "PERsonalized SOft proMpt Adapter Architecture for Personalized Language Prompting"**（Hebert et al., **arXiv:2408.00960 · 2024-08-02**；作者含 Karatzoglou / Sayana / Kuzmin 等 **Google 系**）。
- **更正**：Substrate Asymmetry 把它与 LaMP/UQABench 并列称"benchmark"**不准确**——它是**方法/架构**（把用户历史重采样压缩成 **soft prompt embeddings**，配 LoRA 做个性化语言提示），**不是数据集**。原文一句："resampling and compressing interactions as free form text into expressive soft prompt embeddings"。→ **它不能当我们的评测集。**（真正可用的 benchmark 是同列的 **LaMP 2304.11406** 与 **UQABench 2502.19178**：后者评 user embedding，接口范式与 Personome 同构——**已深读,见 §4**（判决:不能直接当评测集,但协议/接口/自动生成 QA 流水线可借。）
- **原工作名曾用 "Persoma"，与 PERSOMA 撞名（真实且不小）**：拼写/发音几乎一致、**同一子领域（LLM 个性化）**、Google 作者、2024 已发表、且 PERSOMA 正是 **soft-prompt 个性化**方法（还是 Latent Personal Memory 2606.20911 那条 soft-prompt 谱系的前作）——比"撞一个无关 benchmark"更糟。
- **✅ 决策（2026-07-06）：已改名为 Personome**（读 PER-so-nome）。与 PERSOMA 尾音明显区分（Person**ome** = person + -ome「总体」，对标 gen**ome** / connect**ome** 的"某物完整总体"科学语感，正好 = 一个人点/线/面/体的完整可预测结构）。**碰撞核实（诚实）**：Personome 在 **AI/ML/NLP/agent-memory 领域完全无人占用**（这是最要紧的一档）；但存在 **Personome Inc.**（分子肿瘤学/个性化医疗公司）+ "personome/personomics" 是个性化医学用词——**均跨领域、非知名品牌**，比同领域的 PERSOMA 安全一个量级。备选 Personeme（干净但术语腔重）、Egon（有同名数据质量软件 egon.com + 常见人名）均已弃用。已同步替换正版 + 辅助两篇飞书文档。
- **仍需保留的动作**：Related Work 里**仍要引用并区分** PERSOMA(2408.00960) 作为"soft-prompt 个性化前作"（它做 response 个性化、无下一状态预测、无反馈闭环），以及同列真 benchmark **LaMP(2304.11406) / UQABench(2502.19178)**（后者接口范式同构但不能直接用,见 §4）。

### 已核实（真实存在，可引）

**1. Substrate Asymmetry in User-Side Memory: A Diagnostic Framework** · Youwang Deng（独立研究）· **arXiv 2606.11712v1 · 2026-06-10 · under review**
- **几乎为 Personome 核心命题量身定制的对照实验**：把"记忆写进权重"（**γ-LoRA**，per-user LoRA）vs"记忆写进文本"（**BGE dense RAG**）做成可证伪 A/B（50 用户合成语料 + LaMP 真实探针）。
- **判据不是"权重优于文本",而是按正交轴分工**（三轴：behavioral consistency / factual presence / factual absence）：**γ-LoRA 决定性赢行为风格/voice；RAG 决定性赢事实弃权(该弃权就弃权)；事实召回是混合区**。因果消融：清零那批 LoRA 权重 → absence-probe +33pp 但 presence-probe −20pp（同一套 attention 21–35 层机件反向承载两种能力）。还发现 **"alignment tax on parametric user-memory"**（RLHF 更重的 Instruct 模型上不对称加剧）。
- **对 Personome（诚实）**：支撑"Memory=权重"是**有条件**成立——"像这个人说话/预测其行为 Schema"→权重有据；"精确召回此人事实 + 该弃权时弃权"→需文本/检索兜底。**结论应是混合载体、按能力轴分工,而非二选一。** 术语可直接借（substrate asymmetry / 三正交轴 / routing-as-classification），且其"正交轴分解"与本仓 CLAUDE.md 的 MECE 元认知同构。**判决：直接可引。**

**2. Panini: Continual Learning in Token Space via Structured Memory** · Rajesh 等 · UCLA · **arXiv 2602.15156 · 2026-02 · ICML'26**
- 冻结权重，把新经验写进**可寻址外部语义记忆**（GSW 实体/事件 QA 网），多跳检索——非参数化持续学习（NPCL）。反对"往权重写"：**"参数约 2 bit/param"**、**"知识编辑十次更新后就退化"**、无原生溯源/撤回、稳定性-可塑性困境（引 CLS 神经科学框架）。六个**事实型 QA** benchmark，省 token 2–30×。
- **对 Personome（诚实评估）= 弱反驳、可切割**：① 它反的是**知识编辑（往权重写离散事实）**,全部证据来自事实型 QA,**零个个性化实验**——Personome 的 per-user 权重学的是**偏好/意图分布**(参数化主场),它没测过我们的主场；② 它其实是**分层论(CLS)**,原文承认"最终要 parametrize",与 Personome 心智模型(LLM 慢层=新皮层 / Memory 快层=海马)**同构,甚至可反引为"需快慢双系统"的背书**；③ **回应策略**：把 Memory 分层——事实/情景层(可撤回、需溯源)认它、切给非参数 `memory/*.md`+`index.db`；偏好/意图层用**行为级 eval 量化"随更新次数不退化反增益"**正面守住。**别在"离散事实可撤回性"上硬刚(那是它主场)。判决：弱反驳可切割。**

**3. Mem-W: Latent Memory-Native GUI Agents** · Guibin Zhang 等（通讯 颜水成）· LV-NUS Lab · **arXiv 2605.09317v1 · 2026-05**
- 把 GUI agent 历史轨迹压成 **latent memory tokens**(Q-Former 压缩器,底座冻结,只训压缩器)拼进输入,自蒸馏 + **结果导向 RLOO**(任务成功二元奖励)训练;procedural(跨会话)vs working(会话内)记忆二分;截图+结构化动作输入(**不碰 AX tree**),预测下一个 GUI 动作;**per-task/per-domain,无用户模型**。
- **对 Personome = 表面像、内核正交**：载体(latent vs 外部可读 markdown)、粒度(per-task vs **per-user**)、是否训模型(训压缩器 vs **免训练**)、反馈(任务成功自动 rollout vs **人的采纳/拒绝**)、目标(单任务下一 GUI 动作 vs **人的下一意图/状态**)——**几乎每轴相反**。它恰是 Mens 的**反向设计选择**(我们故意选外部/可读/per-user,因为护城河=Context 组装、信任=承重墙)。
- **但表层相似度高(GUI+截图+memory+动作预测),审稿人大概率点名要求区分 → 必须进 Related Work 并写清差异,否则被质疑 novelty。** 可借：procedural/working 二分 ≈ 我们的 durable memory / attention-rewind；**outcome-aware supervision(用成败给记忆打权重)与"采纳=正/拒绝=负"同构**,可作"latent 版的我们"对比引用。**判决：外围（必引以切割,非威胁）。**

### 两篇综述（最强定位/叙事锚，均已核实）

**4. Rethinking Memory Mechanisms of Foundation Agents in the Second Half: A Survey** · Wei-Chieh Huang 等（60 作者）· **arXiv 2602.06052 · 2026-01（v3 02-10）** —— **两篇里给 Personome 定位/背书最强的一篇。**
- 提出**三条正交轴**（正是我们要的坐标系）：**substrate = internal(含 "Weights" §3.1.2) vs external** · **cognitive = episodic/semantic/sensory/working/procedural** · **subject = agent-centric vs user-centric**；外加 §5.2 "**Parameterized Memory Policies**" / §5.2.1 "Policy Internalization into Parameters" 专章。
- **它把 user-centric 和 weight/parametric memory 都做成了显式格子,并把我们要占的格子明列为未来空白**：§9.4 "**Life-Long Personalization** and Trustworthy Memory" + §9.1 "Continual Learning & **Self-Evolving Agents**"。
- **Personome 一句定位（可直接写进论文）**："按 [2602.06052] 三轴,Personome 落在 **subject=user-centric × substrate=internal(weights) × learning-policy=internalize-into-parameters** 的交点,而该交点正是其 §9.4/§9.1 点名为 open 的方向——我们做的正是这篇 survey 说该做但还空着的事。"

**5. From Storage to Experience: A Survey on the Evolution of LLM Agent Memory Mechanisms** · Luo 等 · **arXiv 2605.06716 · 2026-05 · ACL'26 Findings** —— **叙事骨架 / 演化论证锚。**
- 演化时间轴 **Storage(轨迹保存) → Reflection(轨迹精炼) → Experience(轨迹抽象)**；Experience 阶段分 **explicit(可读可编辑) / implicit(内化进参数) / hybrid(积累→内化循环,把显式记忆当高容量 cache 再压缩内化)**,且**主张 hybrid**——与 Substrate Asymmetry 的"按轴分工/混合载体"结论互相印证。前沿机制 **proactive exploration + cross-trajectory abstraction**。
- **"From storage to experience" 叙事可直接背书 Personome 产品逻辑**（存储用户轨迹→反思/折叠→抽象成行为 Schema/权重→主动预测下一步）。定位话术："Personome 位于该轴最前沿的 Experience 阶段,并把其只在通用 agent 层讨论的 proactive/implicit-experience 机制**专门化到单个用户**。"
- **诚实边界**：该篇个性化着墨极少(agent-centric 为主),user-centric 论点只能靠 2602.06052 撑;别拿它背书个性化。

### 核实小结（诚实边界）
- **5 篇全部真实存在,arXiv id 全部正确,无杜撰**。命名冲突 PERSOMA-as-benchmark 属实,须决策。
- **两篇综述都没有把"预测用户下一动作/状态"写成命名机制** → 这是 Personome 的**原创贡献点**,正确写法是"把 survey 标为 open 的 user-centric×continual-learning 空白,具体化为一个意图/下一状态预测器",**不能**声称是任一 survey 已提出的概念。
- 净判决：Substrate Asymmetry **直接可引**（且把我们的命题诚实校准为"按轴分工的混合载体"）；Panini **弱反驳可切割**（甚至 CLS 分层可反引为我们背书）；Mem-W **外围必引以切割**；两篇综述 = **最强定位坐标 + 叙事骨架**。

**对 Personome 最可借的具体点**：
1. **写入=带 loss 的优化,而非追加 markdown**（GradMem/TTT/Titans 一致）——这才是"Memory=训练权重"的字面兑现。
2. **把 Titans 的"惊奇门+遗忘门"换成"用户反馈门"**（采纳/拒绝驱动写多少/忘多少）;On-the-Fly VLA + REFINE 给出"部署期在线 RL 更新且不灾难遗忘基座"的落地范式。
3. **"同一基座+每人一套权重" = End-to-End TTT 的 meta-learned init + per-user test-time weights**;Nested Learning 的多时间尺度层给分层蓝图。
4. **eval 必须行为级**（Beyond Perplexity 实测 loss 降但回忆为零）——契合"golden 当 LLM eval",loss 只当训练内部量别当验收。
5. **保留"文本记忆"对照臂**（Dynamic Cheatsheet/Retroformer/Panini）——用行为级 eval 量化"写进权重"相对现有 `memory/*.md`+`index.db` 的增量,避免为参数化而参数化。
### 2.B 潜在记忆（latent / hidden-state / KV-cache）

**⭐ 第一梯队 —— latent + 个性化 + 学习闭环（与 Personome 最同构，两篇 2026 新作是重中之重）**

- **Latent Personal Memory**（*Represent Personal Memory as Dynamic Soft Prompts*）· Samsung 系 · **arXiv 2606.20911 · 2026-06**
  - 把**每个用户的个人记忆表示为一组可学习的连续向量（动态 soft prompt）**，前向时作 conditioning 前缀注入，无显式检索；PEFT 梯度在交互中持续更新、含旧记忆衰减。
  - → **几乎是 Personome「Memory = 每人独有的训练权重」命题的直接实现**（同一基座 + per-user latent 权重 + 学习闭环 + 显式个性化）。差异：它是输入侧 soft prompt、偏 response 个性化；Personome 要的是"预测下一状态"的时序模型。**头号可借/须区分对象。**
- **PUMA**（*Know You Before You Speak: User-State Modeling*）· NUS/北航/中科院 · **arXiv 2605.24647 · 2026-05**
  - 把用户建成**部分可观测动力系统**：**transition model**（状态随轮次+系统动作演化）+ **observation model**（隐状态→话语），用"最小化预期自由能"选动作，变分推断持续更新信念。
  - → **与"预测这个人的下一个动作/状态"几乎完全对齐**，是 Personome 心智模型（世界模型预测世界下一态 → Personome 预测人下一态）的学术化身，且显式建模**动作条件下的状态转移**。**Personome 的形式化框架可直接借它。**
- **MemGen**（*Weaving Generative Latent Memory*）· **arXiv 2509.24704 · 2025-09**（Mandol 点名的 [49]）
  - `memory trigger` + `memory weaver` 把当前推理状态编织成 **latent token 序列**注入；**GRPO 强化学习**训练，自发涌现 planning/procedural memory。
  - → **自进化 + RL 闭环**与 Personome「采纳/忽略=奖励做 RL」一致；印证 RLHF 类比可在 **latent 记忆**上跑通，而不止 prompt 层。差异：通用 agent、非 per-user。
- **Titans**（*Learning to Memorize at Test Time*）· Google · **arXiv 2501.00663 · 2025-01**（Mandol 点名的 [3]）
  - 记忆 = 一个 MLP 的**权重**，**测试时用梯度在前向中在线更新自身**，写入门 = **surprise（惊奇度）+ 动量**，配自适应**遗忘门**；三种集成 MAC/MAG/MAL。
  - → **「Memory = 被反馈更新的权重」这条 Personome 核心命题的最强机制原型**。差异：surprise 是自监督 next-token 误差，非用户采纳/拒绝的显式奖励、非个性化。**把"惊奇度写入门"换成你们的采纳/拒绝奖励即成闭环。**

**第二梯队 —— 潜在记忆的存储机制（怎么把记忆放进 KV / 参数）**

| 论文 | arXiv · 年月 | 机制 | 对 Personome |
|---|---|---|---|
| **Memory³** | 2407.01178 · 2024-07（Mandol [47]） | 把 KV-cache 显式化为"第三种记忆"，外部知识编码成**稀疏 KV** 注入 attention | latent 分支架构底座；差异：通用知识、离线写入 |
| **Cartridges**（+ at Scale / Learned Structure） | 2506.06266 · 2508.17032 · 2606.04557 | `self-study` 把长语料**离线蒸馏成可挂载的小 KV-cache**，吞吐 26× | **「持有你的语料→组装 context」的高效落地**：个人语料→便携 latent 包，天然"换记忆=换模型" |
| **Larimar** | 2403.11901 · 2024-03 | 分布式情景记忆，**one-shot 写入 + latent readout 操控 decoder + 选择性遗忘** | "快写、可控读、可遗忘"的读写原语 |
| **Memory Layers at Scale** | 2412.09764 · Meta · 2024-12 | 可训练 KV lookup，**加参数不加 FLOPs**，扩到 128B 记忆参数 | 若把个人记忆做成可微、可扩展参数记忆的工程模板 |
| **Context Distillation as Latent Memory** | 2605.28889 · 2026-05 | 把上下文蒸馏进 latent，明确提"**per-user 蒸馏记忆、免全量重训**" | 与"换权重即换模型"契合；闭环反馈不显式 |
| **RMT / Memorizing Transformers** | 2207.06881 / 2203.08913 | memory-token 段间递归 / 非可微 kNN 外部 KV 检索 | memory-slot 与"即时写入无重训获取新知"的奠基范式 |

**评测/仪器**：**MEMPROBE**（*Probing Long-Term Agent Memory via Hidden User-State Recovery*，**arXiv 2606.24595 · 2026-06**）——把隐藏用户特征埋进对话、测 agent 能否**推断恢复**（而非直接问答）。**不是机制而是"个人模型是否真学到了你"的评测范式**，正好补 Personome 潜在分支缺的 golden/oracle（契合 harness-loop：动机制前先建评测）。**LatentMAS**（ICML'26，多 agent 共享 KV/隐状态而非文本）→ 印证"三者共享一份 context"的 latent 交接思路。

**未核实/须回查（勿直接引用）**：**Mem-W: Latent Memory-Native GUI Agents**（2605.09317，latent+GUI，与 Mens 的 AX/GUI 场景疑似高度相关，**强烈建议深读**）、Bi-Mem（2601.06490）、个性化 agent 综述（2602.22680，含"内部记忆=参数/KV/隐状态"分类学，可作骨架）、Latent Context Compilation（2602.21221）。
### 2.C 显式 Agent-Memory 系统全景（Mandol 那一派 · Personome 直接对标）

**一句话判断**：这一派几乎全是**被查询的静态记忆库**——写入时抽取/去重、读取时检索，服务于"回答关于过去的问题"（LoCoMo/LongMemEval 式 QA）。有"学习闭环"的只有 RL-for-memory 一小簇，但其奖励优化的是**记忆管理/问答准确率**，输出仍是"对查询的答案"而非"对意图的前向预测"。**没有一个把记忆当每个用户的权重、送进前向 pass、用采纳/拒绝做强化**——这正是 Personome 的空位。

**主战场（存储+检索派，按相关度）**

| 系统 | 出处 · 年月 | 记忆表示 | 时间推理 | 状态更新/冲突消解 | 学习闭环 |
|---|---|---|---|---|---|
| **Mem0** | arXiv 2504.19413 · ECAI'25 · 2025-04 | 抽取-巩固-检索 + graph 变体 | 部分(QA) | ✅ 写入时 LLM ADD/UPDATE | ❌ |
| **Zep / Graphiti** | arXiv 2501.13956 · 2025-01 | 时序知识图，双时态边(t_valid/t_invalid) | ✅✅ 最强 | ✅ 边失效 | ❌ |
| **MemOS** | arXiv 2505.22101 / 全版 2507.03724 | MemCube 统一 parametric/activation/plaintext | 部分 | ✅ 调度/迁移/融合 | ❌("可演化"=调度非 RL) |
| **EverMemOS** | arXiv 2601.02163 · 2026-01 | engram 生命周期 MemCell→MemScene；**含 Foresight 时限预测信号** | ✅ | ✅ 语义巩固+更新画像 | ⚠️ 有 foresight 但无奖励闭环 |
| **A-Mem** | arXiv 2502.12110 · NeurIPS'25 | Zettelkasten 自组织笔记网 | ❌ | ✅ Memory Evolution 回改旧笔记 | ❌ |
| **Letta / MemGPT** | MemGPT arXiv 2310.08560 · 2023-10 | "LLM 即 OS"，两级记忆+函数自编辑 | ❌ | ✅ 自编辑 | ❌ |
| **MemU / Cognee** | 仅 repo，无核心 arXiv | 文件式 / 图记忆平台 | 部分 | ✅ 摘要/重加权 | ⚠️ 启发式，非 RL |

**RL-for-memory 簇（最接近"学习"，Personome 新颖性的最强挑战——必须点名区分）**

| 系统 | 出处 · 年月 | 学习闭环性质 |
|---|---|---|
| **Memory-R1** | arXiv 2508.19828 · 2025-08 | RL(PPO/GRPO)训 Memory Manager(ADD/UPDATE/DELETE/NOOP)，仅 152 QA 对；**奖励=下游 QA 正确率**，输出仍是答案 |
| **MEM1** | arXiv 2506.15841 · 2025-06 | 端到端 RL，恒定内存融合巩固与推理；目标=长程任务效率 |
| **Mem-α** | arXiv 2509.25911 · 2025-09 | RL 学"如何构建记忆"，优化记忆构建质量 |

> **关键区分**：这三者证明"记忆管理可被 RL 优化"，但奖励来自**任务/问答正确性**，学的是"怎么存/取"；没有一个学"预测这个人下一步"、也没把用户采纳/拒绝当奖励。Personome 的 RLHF-式反馈（拒绝=负样本）+ "预测下一动作"目标与它们**正交**。

**奠基工作**：Generative Agents（arXiv 2304.03442 · UIST'23，Memory stream + Reflection + **Planning 前向规划**，但 prompt-based、逐 agent、无反馈学习、是模拟非真实用户预测）；HippoRAG / HippoRAG2（2405.14831 / 2502.14802，PPR 检索，纯读无更新无预测）。

**基准（全派都是"回顾式 QA"，无一评"预测"）**：LoCoMo（超长对话回忆 QA）、LongMemEval（500 题，多会话子项是硬骨头）、BEAM（百万-千万 token 生产规模）、**StreamMemBench**（arXiv 2606.14571 · 2026-06，"面向未来的辅助"，测跨任务经验迁移——最接近 Personome 但仍是被动迁移非实时预测）。当前 SOTA：LoCoMo 已近饱和挤在 ~92–93%（MemU/ByteRover/Mem0 互有胜负、多为自评）；LongMemEval Mem0 报 94.4%（多会话仅 70.7%）。**没有任何"预测用户下一状态"的排行榜存在——这本身就是 Personome 的定位空白。**

**2026 邻近入场者（威胁新颖性，正文引用前须二次核实）**：*From Storage to Experience: A Survey on the Evolution of LLM Agent Memory*（2605.06716，标题即印证"从存储到经验"叙事）、MemPro（2606.00619）、LifeSim（2603.12152）、KnowMe-Bench（2601.04745）、PersonaTree（2606.04780）、Personalize-then-Store（2605.25535）、CloneMem（2601.07023）。

**Personome 相对这一派的差异化卖点**：
1. **预测 vs 回顾**——全派回答"关于过去的问题"，Personome 预测**下一个动作/状态**（连排行榜都不存在的方向）。
2. **记忆=权重、进前向**——他们外挂静态库拼进 prompt；Personome 把每人 memory 当个性化训练权重（换权重=换预测器），直接参与前向。
3. **真反馈闭环**——多数无学习；RL 簇也只用 QA 正确率当奖励；Personome 用用户采纳/拒绝当奖励学"预测准不准"。
4. **单人预测基座 vs 通用记忆中间件**——竞品是"给任意 agent 挂记忆"的 2B 中间件；Personome 是针对单个人的意图预测基座（持有"你"这个买不到的语料）。
### 2.D 个性化 / 用户建模 / 下一步意图·动作·状态预测（"我们要预测什么"这一侧）

**⭐ 第一梯队：直接对应"预测人的下一个状态/动作/意图"**

| 论文 | arXiv · 年月 | 预测目标 + 反馈闭环 | 对 Personome |
|---|---|---|---|
| **BehaviorChain**（*How Far are LLMs from Being Our Digital Twins?*） | 2502.14642 · 2025-02 | persona 的**连续行为链**(next behavior，非 next token)；prompt/检索式；无闭环 | **框架同构**："数字孪生=预测一个人的行为链"=Personome 定位；可借"预测目标=行为链"评测范式 |
| **LifeSim**（*Long-Horizon User Life Simulator*） | 2603.12152 · 2026-03 | **BDI**(信念-欲望-意图)驱动的意图轨迹；LifeSim-Eval 8 域 1200 场景 | **目标定义高度一致**；关键发现"LLM 在**隐式意图/持续偏好**上严重不足"正是 memory-as-weights 要补的洞；可借 BDI 目标定义+评测 |
| **ChronosBench**（*Proactive Long-term Intent Maintenance*） | 2601.09382 · 2026-01 | 长期意图 + **触发条件**(何时主动出手)；合成数据微调；有闭环 | **机制同构 Mens 本体**："armed 意图+app_opened 触发+主动 follow-up"的论文版 |
| **ProUtt**（*Proactive Prediction of Next User Utterance*） | 2601.09713 · 2025-12 | 经**意图树**预测 next utterance；**偏好数据合成(DPO 式)** | **目标+反馈都同构**："预测下一步"+"偏好数据(采纳/拒绝)当训练信号"=Personome RLHF 设想 |
| **Can LLM Agents Simulate Multi-Turn Human Behavior?** | 2503.20749 · 2025-03 | **next user action**；31865 真实网购 session；微调+reasoning trace | 方法论直接可借="从行为日志+why 微调"；硬数据 prompt-only 11.86%→微调 17.26%(next-action 极难) |
| **Customer-R1**（*Personalized Simulation via RL*） | 2510.07230 · 2025-10 | **next action**；persona 条件化 + **RL(action-correctness 奖励)** | **最接近 Personome 的 RLHF 闭环**："预测动作→正确性/采纳当奖励→RL"，可借奖励设计 |

**第二梯队：个性化"怎么实现"——检索 vs 权重 vs 嵌入 vs 对齐**

- **OPPU**（*One PEFT Per User*）· **arXiv 2402.04401 · EMNLP'24** —— 给**每个用户挂一个可训练 LoRA** 存其行为偏好，冻结基座。**= Personome「Memory=每个人独有训练权重」比喻的字面先例**，"换权重即换模型"最强引用支撑;差异:OPPU 离线一次性,Personome 要在线随反馈自进化。
- **个性化 LLM 综述**（2502.11528）—— 三分法**输入级/模型级/目标级**,给 Personome 定位坐标系:Mens 现在在"输入级(digest 注入 prompt)",蓝图要往"模型级+目标级"走。引言可直接引。
- **TAP-PER**（*Beyond Retrieval: Compact User Representations*，2606.04547）—— **user-state prefix embedding** 替代检索,比 OPPU 少 130× 参数;"user-state 分量"拆法契合。
- **Reward Factorization**（MIT,2503.06358）—— per-user 奖励=基奖励线性组合,**~10 条反馈**推断偏好向量 → 冷启动少样本个性化理论。
- **T-POP**（2509.24696 · ICML'26）—— 冻结 LLM,**在线 pairwise 偏好 + dueling bandits** 解码期 steer,不改权重 → 冷启动另一条路;"采纳=正/拒绝=负"正是 pairwise,探索/利用指导何时主动试探。
- **AI PERSONA**（2412.13103）—— life-long personalization + ever-changing profile,与"随用户状态自迭代"愿景同频(实现细节待补,别过度引申)。

**第三梯队：主动性 / GUI·端上下一步预测 / 经典基准**

- **ProactiveMobile**（2602.21858）—— 端上**潜在意图推断**+可执行函数生成,**最贴近 Mens 场景**;硬数据微调仅 19.15%、GPT-5 7.39% → **主动意图推断是当前最难的洞**。
- **Mobility Prediction w/ LLM Agent**（2606.05130）—— 个体 next-location,**"快路(历史规律)+慢路(取证)"** = **架构惊人相似于 Mens 的 fast/slow 识别器**,可引为架构佐证。
- **LaMP**（2304.11406）—— 个性化基准鼻祖;**Time-split 设定(早期历史训、预测未来交互)对 Personome 极有用**,是"预测下一状态"的时间外推评测协议。
- 外围:LongLaMP（2407.11016,长文个性化）、PersonaLLM（2305.02547,人格表达一致性)。

**未核实/须回查**：MemoryCD（2603.25973）、AdaMem（2606.21144，学"该记什么"）、PersonaTree（2606.04780）、MobileDreamer（2601.04035，GUI world model）、PiSAR（2605.29400，屏幕条件动作预测）。

**对 Personome 目标定义/评测最有用的 5 点**：
1. **预测目标显式定为"意图驱动的下一行为/行为链"**（LifeSim BDI + BehaviorChain 行为链 + ProUtt 意图树三者共识:先建意图层再落行为),与 Mens"意图识别为本"一致。
2. **评测用"时间切分 + 长程多轮"**（LaMP Time-split + LifeSim-Eval / ChronosBench），别用单步准确率。
3. **"Memory=训练权重"硬先例 = OPPU（per-user LoRA）**；再用 Reward Factorization / TAP-PER 说明权重可以是低维偏好系数/user-state prefix。
4. **反馈闭环:把采纳/拒绝当 pairwise 偏好**（ProUtt/Customer-R1/T-POP/Reward Factorization），冷启动借 T-POP/Reward Factorization 少样本路线。
5. **管理精度预期**：真实 next-action 极难（网购 17%、ProactiveMobile 19%、GPT-5 7%）——Personome 卖点不是绝对精度,而是**个人 memory 把 baseline 抬起来**。

---

## 4. 评测集核实：UQABench（已深读，2502.19178）

**元数据**：*UQABench: Evaluating User Embedding for Prompting LLMs in Personalized Question Answering* · Liu 等 · **阿里巴巴淘天集团** · **arXiv:2502.19178（v1 2025-02, v2 2025-04）· KDD'25** · 数据+代码已开源（[github.com/OpenStellarTeam/UQABench](https://github.com/OpenStellarTeam/UQABench),Qwen2.5-3B-Instruct + HF/Kaggle 数据集）。

**它评什么**：评"把**用户行为序列压成 user embedding、当 soft prompt 喂给 LLM**"这条路能否答对关于该用户的个性化问题（Embedding-based Generative Recommendation）。
- 数据 = **淘宝点击日志**（184,520 用户 / 994,447 商品 / 31,317,087 交互，9:1 时序切分），非天然 QA——用"任务模板 + 用户历史"**自动生成** 7,192 条带 ground-truth 的个性化 QA。
- 接口 = encoder(SASRec/HSTU/Mamba4Rec…) → **Adapter** → soft-prompt tokens（Mean=1 / Q-Former=16）→ **与问题文本拼接喂 Qwen2.5-3B**，冻结 encoder 只 fine-tune 对齐。
- **含真正的"预测下一步"**：三大任务里 **Action prediction（IP/AP）= next-item / next-attribute prediction**（原文"what item the user will click **next**"），本身就是 test set 的时序 next-action、天生 time-split；另有 Sequence understanding(回顾) + Interest perception(状态刻画)。

**对 Personome 的契合度（诚实判决：不能直接当评测集；方法论+接口范式可直接借，数据须用自有日志改造重建）**：
- ✅ **接口范式几乎同构**——"user embedding 作 soft prompt 喂 LLM 评个性化 QA" = Personome "Memory 作 per-user 表征喂 LLM"；它甚至给了现成接线口（Adapter→soft-prompt），**理论上可把 Personome 的个人记忆向量当它的 "user embedding" 直接插进去测**。这是它对我们最大的价值。
- ✅ **含 next-action 任务**（Action prediction），比一般回顾式 personalization benchmark 更贴。
- ❌ **域/模态/预测空间不对口**：它预测淘宝**有限 item 词表**里的下一个点击；Mens 输入是 **AX tree + 截图**（非结构化、多模态、无可枚举候选集），且要预测的是跨 app 的**意图/状态**（比"下一个点击"更高层）——7,192 条淘宝 QA **无法直接用于 Mens**。
- ❌ **不评核心机制**：Personome 的 **反馈驱动 per-user 权重更新（accept/reject 当 RL 奖励）** 在 UQABench 里完全没有对应（它 encoder 冻结、LLM 冻结，只评静态 embedding）。
- **可落地用法**：借它的 (a) **任务分类学与评测协议**（把 Action prediction 映射为"预测下一意图"）、(b) **soft-prompt 注入接口范式**、(c) **"模板+用户历史→自动生成带 ground-truth QA"的流水线**——套在**自有 AX 日志**（`index.db` 的 sessions/timeline_blocks/intents）上,重建一个 **"Mens-UQABench"** 域内预测基准。这与 §3.4 "用行为级 eval + time-split" 的红线一致,也接 MEMPROBE(隐式恢复用户隐藏状态)那条 oracle。

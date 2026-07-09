---
purpose: 数据驱动自迭代的方法论——优化任何"可度量的指标"（recall/precision/延迟/质量率）时，先建可度量的 oracle，让数据选下一步，只保留经验证的改进，并诚实区分"方法受限 vs 数据受限"
read_when: 要把某个指标做上去并证明它（"把 X 做好并证明"）、建 eval/golden/benchmark/feedback harness、跑 /loop 或 autoresearch 自迭代、优化 recall/precision/延迟/质量分、或被要求"数据驱动迭代 / 自迭代 / 沉淀迭代方法论"时
human_note: 本文档约束 AI 在做指标优化时的行为纪律。它是 design-philosophy-intent.md 的"代价不对称"宪法应用到迭代过程；数据层、真值核算、熵判停条件、对抗红队。证据来自一次真实的过夜自迭代。
---

# 数据驱动系统迭代（data-driven iteration）

> **Provenance.** 本文成文于 Persome 的开发中；文中的战例与文件路径（`autoresearch/…`、`SelfTest.swift`、Swift 门禁等）来自更早的 Mens 迭代，是**说明性示例**——方法本身与具体产品无关，直接适用于 persome-core（其门禁是 `uv run pytest` 离线档 + ruff + pii_scan）。

> 先建**可度量的 oracle**，让**数据**选下一个杠杆，只保留**经验证**的改进。在**真实数据**上校验——
> 你的合成 harness 偏乐观。分清指标卡住是**方法受限**还是**数据受限**，到了天花板就**停**，别对噪声 p-hack。

适用：任何"把某个数字做上去并证明"的任务——意图识别 precision、检索 recall、延迟、去重误吞率、
质量分。不适用：没有可度量目标的主观任务（那种先找代理指标，或退回人工判断）。

方法的两半：**先建反馈 harness、再 改→过门禁→读→修**（爬坡循环），加上本文强调的**数据层**、
**真值核算**、**熵判停条件**、**对抗红队**——即一次真实迭代里**最容易骗到自己**的地方。

---

## 0. 一句话流程

```
选指标 → 建 oracle（四层）→ 立 baseline → 循环[ 从数据诊断 → 一次原子改动 → 复测(噪声带) →
keep 或 revert → 记日志 ] → 真实数据校验 → 对抗红队 → 接入生产+永久门禁 → 到天花板就停
```

---

## 1. 先建 oracle（四层）——没有标尺就不能爬坡

头几轮花在**建度量**，不是建功能。四层各回答一个不同的问题：

- **L0 二元门禁（我有没有弄坏？）** 项目自带的 build/test/typecheck + 集成自测。提交前必须绿。必要但不充分。
- **L1 质量 eval（它到底好不好？）** 一个**标注/golden 集**打成一个**数字**（precision/recall/accuracy）。
  复用既有 eval 内核，新增 case 放**独立文件**，绝不扰动已有门禁。打分算术本身要被单测，headline 数才可信。
- **L2 行为模拟（不等就能知道会发生什么？）** 把合成流喂进**真实逻辑**（实际的选择/去重/gating 纯函数）
  跑在模拟时钟上；断言不变量 + 打印投射行为（频率、折叠率）。让时间/用量相关的行为能离线度量。
- **L3 数据分析管线（损失在哪？= 火焰图的类比）** 拉**真实**遥测、画图（候选在哪被丢的漏斗、kind/结果
  直方图、逐轮趋势），产出**诊断→下一杠杆**。stdlib HTML+SVG 足够。这一层让循环**数据驱动**而非猜测驱动。

> 本仓库实例：L1 = `autoresearch/loop-260617-2337/run_quadrant_eval.py` + `quadrant_golden.yaml`；
> L2 = `SelfTest.swift` 的 `scenarioContextFrequencySim`/`…SemanticDedup`；
> L3 = `.loop/loop_analyze.py` + `results.tsv` 日志。

**生产同源模型**：eval 用与生产**同一家**模型跑（本仓库慢路是 DeepSeek，eval 就走 DeepSeek），
否则你 prompt 调到的增益不一定迁移到生产。

---

## 2. 迭代循环（爬坡）

每轮：**定位**（读日志 + L3 诊断）→ **诊断**（数据里**最大的那个缺口**，不是直觉）→ **一次原子改动** →
**复测**（先跑 L0；绿了再跑该改动应当推动的那个指标）→ **keep 或 revert**：

- **KEEP + commit** 当且仅当 L0 绿 **且** 目标指标超噪声带地改善 **且** 没有别的回归。
- 否则 `git restore` 并**记录失败原因**（被推翻的假设也是数据——别再盲目重试）。
- **记日志**到一个状态 journal：迭代号、假设、改动、before→after、KEPT/REVERTED、下一步。

这一步**就是**模型知道自己代码错没错的方式：门禁 + 数值 delta 是客观 oracle。到了**实测平台期**
（N 轮无真实增益）就停，别制造虚假动作。

---

## 3. 真实数据核算（最重要的一课）

**你的合成 harness 会骗你。** 一个手写 golden 量的是"在精选 case 上的分类"，**不是**它在真实输入上
**到底会不会触发**。在相信"完成"前，把系统在**真实捕获数据**上回放（只读 / 在副本上——**绝不动生产**）。
真实数字大概率比 golden 数字差；那个差距才是重点。

### 3.1 读**原始**数据，不是蒸馏层（本仓库踩过的最大坑）

本次循环 Stage-1 子任务只读了 `intents.rationale`（系统**蒸馏后**的总结），得出"零信号"的结论。
而系统**漏识别**的东西**根本不在** intents 表里——它埋在 `captures.visible_text`（原始屏幕文本）。
亲读原始层后，立刻找到两条被识别器漏掉的真实"重要但不紧急"项。**教训：disambiguation 必须读系统当初
看到的同一份原始输入，不是它的产物。**

### 3.2 熵判停条件（concluding 前先分类缺口）

- **认知熵（epistemic / 可约）**：信号**在**数据里，系统欠提取 → 更好的模型/prompt/上下文窗能修 → **继续迭代**。
- **偶然熵（aleatoric / 不可约）**：信号**不在**数据里 → 任何模型迭代都没用 → 换数据源、采更有代表性的窗口、
  或零成本把问题还给人。**停止硬磨**——再多算力都是浪费。

> 本次裁定：Goal-2 供给率 0.93/活跃小时（目标 ≥3）。对全部 46 个零意图活跃小时做 stronger-reader 扫描，
> 只找到 **1** 条真实漏识别——**判定为偶然熵**（活动本身没有 3/hr 可推送意图）。强行拉 recall 到 3/hr =
> 制造误报，违反 precision 约束。**但**那 1 条揭示了一条**薄而精确的认知熵尾巴**（议程内点名给用户的交付物），
> 这一条修了（recall 0.56→1.0）。"大体偶然熵 + 可修的认知熵尾巴"是常见的真实形态。

### 3.3 disambiguation test（下结论前必跑）

让一个**更强的读者**（更大的模型，或你自己）读**系统当初看到的同一份原始输入**：
- 找到系统漏的 → 认知熵（可修，继续）。
- 什么真东西都没找到 → 偶然熵（停）。

**它必须是对原始输入的真正独立阅读**——关键词 grep 或同模型重放都**不算**；而且"我以为找到的"必须过
**对抗审查**（真是目标类吗？真是用户的吗？不是 boilerplate/别人的/UI chrome？）。本次循环里，第一遍
"更强读者"曾**over-claim 了 5 条**漏识别，对抗复核显示它们其实是别人的聊天、第三方 UI、模板文字——
反而**确认了偶然熵**。**stronger-reader 读 + 对抗 refuter 要配对使用。**

---

## 4. 纪律——伪装成进展的反模式

- **信任 delta 前先量噪声——报一个区间，不是点估计。** LLM eval 轮间会变；跑 ≥3 次报 `mean ± spread`。
  **spread ≥ 增益（或 ≥ 你离目标的余量）就不算真改进**——你在 p-hack 一次幸运抽样。基于 count 的 headline
  可能凑巧重复而单个 case 在翻转，要看 per-case 翻转率不只看聚合。
  > 本次 q4（注入轨迹上下文）precision 0.54→0.65 看着涨，但增益 0.11 ≈ 噪声带 0.09 → **判 discard**。
  > 后期两个 eval "miss" 复跑 3 次都是正确值 → 确认是采样噪声，**不为噪声做任何改动**。
- **连接到 mission——端到端 oracle（本方法论第一次跑差点翻车的陷阱）。** 一个孤立指标（如识别 recall）
  若被下游 gate 在到达用户前丢弃，就毫无价值。永远加一个跑**整条路径** input→…→用户可见效果 的 oracle。
  > 本次具体翻车：`kind=backlog` 在 conf≤0.4 被识别，但呈现 gate 要 ≥0.7 且把该 kind 排除在 proposable 之外
  > ——一个**完美**识别的项也永远到不了用户。L1 recall 在涨，端到端用户价值恒为 0。**画不出从指标到
  > 用户可见改变的线，就是在优化一个数字，不是系统。** 后来转向 importance×urgency 象限（与 kind 正交）+
  > 哨兵 gating，才真正打通。
- **代价不对称是法律。** 若一类错误更贵（误推、误报），那一侧的回归 = REVERT，哪怕 headline 涨了。
  别拿 precision 换 recall。（见 `design-philosophy-intent.md`。）
- **一次只改一处**——否则无法归因 delta。
- **harness 就是 oracle——自己复跑验证。** 别信子任务一句"过了"。至少把廉价的确定性门禁自己再跑一遍。
- **每次 kept 改动后备份**（commit + push + 状态 journal）——尤其无人值守的 run。

---

## 5. "完成"前——对抗红队门禁（必做，非可选）

自测不够；harness 会以它自己看不见的方式出错。任何"完成"/PR/ship 前，派一个**对抗子任务，职责是 REFUTE**
（不是 confirm）每一条承重声明——偏怀疑、歧义默认 REFUTED、跑真命令。它必须攻击：指标噪声（复跑 N 次）、
eval 有效性（strawman 负例？打分 bug？"被单测的算术"真覆盖了这条路径吗？）、端到端可达性（赢的改动到得了
用户吗？）、任何"我找到了 X"的提取（真的，还是 boilerplate/别人的/UI？）、循环测试（stub 是不是只是回声了
测试自己的答案、真组件根本没被运动？）。把幸存的声明当作**唯一真实**的结果。**在 PR 前跑，不是 PR 后。**

> 本次红队抓出两条 over-claim 并修正：(1) Goal-3 去重的 judge prompt **逐字写进了测试集的实体名**
> （某个真实人名/面试官/两个具体时间点）——held-out 新实体上失效；去记忆化改成通用原则后，原集
> 误吞 0.0 / 重复弹 0.028 仍达标且**泛化**。(2) agenda guard=1.0 依赖注入的用户身份 hint（合理，daemon 本就
> 知道屏主，但必须标注）。**所有 headline 数都复现、无造假**——这是红队的正面确认。

---

## 6. 数据稀缺时——分清"方法受限 vs 数据受限"

指标卡住，先问：是**机制**不行，还是**评测集太小/信号太稀**？这两者的处置完全相反。

> 本次：Goal-1 象限 precision 在真实数据上卡 0.54。诊断发现真实数据只有 **5 个 distinct** 正例（这个用户
> 一周的活动就这么多）——N=5 时一个边界 case 就摆动 precision ±0.15，**0.90 根本不可证**。对抗复核
> （RELABEL-VERDICT）确认残留 FP 都在阈值 ±0.05 内且**没标错**——再调就是 p-hack。
> **但**：换一个**足够大的合成 golden**（22 例，走真实 slow_lane）跑出 **precision 0.90 / recall 0.90**——
> **证明机制本身达标**，真实数据的 0.54 纯粹是**数据稀缺**，不是机制极限。
>
> 处置：(a) 诚实报告真实数据的天花板 + 它是数据受限；(b) 用足够大的集证明机制可达标；
> (c) 把评测管线**自动化**，等更多真实数据积累后直接复测——而不是对 N=5 硬调 prompt。

**真值授权（贯穿全程）**：不造正例、不夸大；信号稀疏就**如实说**。需要真值却缺失的目标（本次：
importance↔accept 相关性、firing-precision、dismiss——真实数据里 0 个真实 accept），标 **UNDECIDABLE**，
**不伪造**。诚实的"测不了"远胜一个造出来的数字。

---

## 7. 接入生产 + 永久门禁——"跑通之后 Automated"

sidecar eval 验证了的改进，**必须落进真实代码**，否则只是纸面 win：
- 把调好的 prompt/逻辑接进生产路径（本次：importance×urgency 评分进慢路+快路识别器 + 哨兵 gating，端到端）。
- 把校准后的阈值写进仓库 eval 的**门禁**（env 阈值变量），让回归被真正卡住（本次：backlog/quadrant 的
  LLM 档 floor 从 0.0 校准到 P≥0.80/R≥0.60；确定性 shape 档进默认 gate）。
- 改完跑全套门禁（persome-core：`PERSOME_LLM_MOCK=1 uv run pytest -m "not macos and not integration and not eval"` +
  `ruff check` + `python scripts/pii_scan.py`），**自己确认全绿**再 push。

---

## 8. 自迭代机制（/loop / autoresearch 无人值守）

- **/loop 动态模式**；用 `ScheduleWakeup` 重排。委派的活优先**完成驱动**节奏（子任务的完成通知唤醒你）+
  一个长兜底，而不是密集定 tick（密集 tick 为空检查烧 prompt 缓存）。
- **把有界的活委派给子任务**以保持**编排者上下文精简**；编排者自己留住 keep/revert 决策（oracle 调用）。
  子任务读真实数据时要**上下文 bounded**——SQL 聚合/substr 截断，**绝不**把整列原始文本读进自己的 context
  （本次第一个 builder 子任务就是把原始文本读爆了上下文而崩溃）。
- **一个 worktree 一个写者。** 绝不在活子任务正在编辑的 worktree 上跑 `git restore`/commit——一次 mid-eval
  的 restore 会污染它的度量（吃过亏）。并发的活隔离到各自 worktree。
- 持久化一个**状态 journal**（+ 把规格当可重读的契约），让循环能跨 context 压缩续上；记一个 cadence
  override / DONE 标记，让游离的唤醒能干净终止。
- **到天花板就停，别对噪声 p-hack。** 实测平台期 / 偶然熵 / 残留缺口都是采样噪声时——停，发完成通知，
  不强行制造改动。

---

## 9. 本仓库的完整实例（活档案）

一次真实的过夜 autoresearch，每个上面的原则都有据可查：
- 契约 + 验收标准：`autoresearch/loop-260617-2337/ACCEPTANCE-CRITERIA.md`
- 状态 journal + 逐轮日志：`LOOP-STATE.md`、`results.tsv`
- L1/L2/L3 harness：`run_quadrant_eval.py`、`run_dedup_eval.py`、`run_agenda_eval.py`、
  `daemon/.../golden/quadrant_golden.yaml` + 两档测试
- 真实数据核算 + 熵裁定：`SIGNAL-EXPANSION.md`、`G2-MISSED-INTENTS.md`、`RELABEL-VERDICT.md`
- 对抗红队：`REDTEAM-NIGHT.md`
- 最终账：`FINAL-REPORT.md`

弧线（一个警世故事 + 一个正面收尾）：循环先以为达标（recall 0→"0.82"，precision 1.0）；**对抗红队**推翻
了几乎全部——指标噪声主导、端到端 inert、over-claim 的漏识别。修正后重建：转 importance×urgency 象限
（连接到 mission）、足够大的集证明机制达标 0.90/0.90、去记忆化让去重泛化、诚实标注数据受限项。
**教训：过自己写的 harness 没什么意义；只有端到端 oracle + 噪声带 + 对抗 refuter + 真原始数据阅读 +
方法vs数据受限的区分，才告诉你真相。**

## 10. 大型 benchmark 必须断点续跑（resumability）——硬规范

**任何会跑很久 / 烧外部额度 / 依赖网络的 benchmark 或预计算，都必须能断点续跑。** 不是
可选优化，是硬规范。踩过的坑:LongMemEval 的候选-bundle 预计算(全量 470 题 × top-50 ×
3072 维 embedding)在 `ingested 470` 之后撞上 OpenRouter key 额度墙挂掉,**之前几分钟的
embedding 全部丢失、从零重来**——因为它是"全算完才落盘"的一次性脚本。

### 必须满足的属性

1. **增量落盘(checkpoint)**:每完成一个工作单元(一道题 / 一个分片 / 一次 embedding)
   就**立刻持久化**它的结果(append-only JSONL / 按 key 的缓存文件),绝不"全部算完再一次性
   写"。进程被 kill / 额度耗尽 / 网络断,已完成的单元必须已经在磁盘上。
2. **重启跳过已完成(resume)**:启动时先读已落盘的结果,**跳过已完成单元**,只算缺的。
   幂等——重复跑产出相同最终结果,不重复付费。
3. **稳定 key**:每个工作单元有确定性 id(题用 `question_id`,embedding 用**内容 hash**),
   断点续跑靠它对齐。embedding 缓存按 `sha256(text)` 存,换实验/换扫描参数都能复用,
   **同一段文本一辈子只 embed 一次**。
4. **昂贵层与廉价层分离**:把"贵且慢"的外部调用(embedding / rerank API / reader LLM)
   一次性算成可复用的中间产物(本仓库的 **candidate bundle**:每题 BM25 top-N 候选 +
   每候选 cosine + 是否证据),之后所有超参/配置扫描都是**纯本地算术(毫秒级)**,不再碰网络。
   这同时满足"续跑"和"几秒出结果"两个目标。
5. **分片可独立重跑**:并行分片(`--shard i/n`)各自落盘(`--scores-out`/`--verdicts-out`),
   单个分片失败只重跑那一片,不重跑全部;合并从落盘结果精确重算。

### 落地清单(给 harness 作者)

- [ ] embedding/rerank/LLM 调用走**内容-hash 持久缓存**(命中即免费、免网络、可续跑)。
- [ ] 贵的外部计算先沉淀成**中间 bundle**,扫描只读 bundle。
- [ ] 长任务**增量 append** 结果,不"末尾一次性 dump"。
- [ ] 启动**读已完成集 → 跳过**,幂等。
- [ ] 分片各自落盘 + 幂等合并;失败只补缺片。
- [ ] 跑前打印外部额度/key 状态,避免跑到一半撞墙(撞墙也只丢未落盘的最后一个单元)。

**判据:任何"跑了好几分钟、被打断就得从头来"的 benchmark 脚本都不合规,必须改造。**

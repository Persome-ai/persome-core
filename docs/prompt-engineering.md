---
purpose: 约束本仓库所有 LLM prompt 的编写与迭代方式——先有可测的成功标准再调、按官方技术阶梯从低成本到高成本逐项加、每改一版用 eval 噪声带验证真伪、用 few-shot/分流替代硬规则堆叠
read_when: 新增或修改任何 prompt（`src/persome/prompts/*.md`、eval harness prompt、intent 识别 prompt），或被要求"调 prompt / prompt engineering / 优化某个 LLM 阶段的质量"时
human_note: 本文档约束 AI 如何写 prompt；人类用于审计 AI 被告知的 prompt 方法论。技术清单源自 Anthropic 官方 prompt-engineering 文档，反例源自 LongMemEval reader prompt 的真实迭代
---

# Prompt Engineering 方法论（仓库统一规范）

> **Provenance.** 本文成文于 Persome 的开发中；文件路径以 persome-core 为准（daemon prompts 在 `src/persome/prompts/`），个别战例引自更早的迭代，作说明用。

本仓库所有 LLM prompt 的编写和迭代都遵循本文档。它不是"写 prompt 的灵感清单"，
而是一套**有顺序、有验证、有停止条件**的工程流程。技术清单源自 Anthropic 官方
[Prompt engineering overview](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/overview)
+ [Prompting best practices](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices)；
迭代纪律与 [`data-driven-iteration.md`](data-driven-iteration.md) 一脉相承（prompt 是其中一种"改动"，eval 是其 oracle）。

## 0. 黄金前提：先有成功标准 + 可测试，再调 prompt

> Anthropic 官方原话:做 prompt engineering **之前**必须有 ①清晰的成功标准 ②对标准做实证测试的方法 ③一版初稿 prompt。
> 而且 **"Not every failing eval is best solved by prompt engineering"** —— 延迟/成本常该换模型，召回缺失常该改检索，不是改 prompt。

落到本仓库:**改 prompt 前先问"我用什么 eval 验证这次改动是真提升？"** 如果答不上来,先建 eval(见 `data-driven-iteration.md` 的四层 oracle),不要凭感觉改。
> 注:下述 `tests/eval/` harness 与数据由真实使用数据采收而来,**团队本地保存(gitignored),不随开源仓库分发**。外部贡献者改 prompt 时,请在 PR 中说明验证方式,维护者会代跑 eval。
- 意图识别 prompt → `tests/eval/golden/intent_golden.yaml` 两档 runner
- 检索/QA 类 prompt → `persome-bench` 中的 LongMemEval harness

## 1. 技术阶梯（从低成本到高成本，按序应用，够了就停）

每加一级就回 eval 验证一次,**不要一次堆满**。顺序本身就是优先级。

| # | 技术 | 一句话 | 何时用 |
|---|---|---|---|
| 1 | **清晰直接 (be clear & direct)** | 指令简单、直白,放在正确的"高度"(altitude) | 永远先做这个 |
| 2 | **少样本示例 (multishot/few-shot)** | 给 2–5 个**多样、典型**的范例,胜过堆规则 | 行为难用一句话讲清、或有固定风格时 |
| 3 | **思维链 (chain of thought)** | 让模型先想/先列证据再给结论 | 推理、计数、多证据聚合类任务 |
| 4 | **XML 标签** | 用 `<context>`/`<question>` 等标签分区输入 | 输入有多块、需要模型分清边界时 |
| 5 | **System prompt / 角色** | 给模型角色定位 | 需要稳定的人设/视角时 |
| 6 | **Prefill** | 预填 assistant 开头约束格式/风格 | 要强制输出格式或开头时 |
| 7 | **Prompt chaining / 分流** | 把任务拆成多步或按输入类型走不同 prompt | 单一 prompt 要同时满足互相冲突的目标时 |
| 8 | **长上下文技巧 + prompt 缓存** | 长文档放前面、挂 `cache_control` 断点 | 大量稳定前缀(见 CLAUDE.md 的 prompt caching 段) |

## 2. 头号反模式:往一个 prompt 里堆硬规则 / edge-case 清单

> Anthropic 官方点名的 failure mode:**"hardcoding complex, brittle logic in their prompts... stuffing a laundry list of edge cases"** —— 制造脆弱性、难维护、并且会在一类输入上帮倒忙。

**本仓库的真实反例(必须记住):** LongMemEval reader prompt 的 v2 迭代,往统一 prompt 里塞了
"穷尽扫描所有片段 + how many/which 类穷举计数"的硬规则。结果:
- ✅ 跨会话拼接 0.57→0.67、知识更新 0.80→1.00(聚合类受益)
- ❌ **个人偏好 0.73→0.63**(同一条"穷举计数"规则把"抓要点做个性化推荐"带偏了)

教训:**两种作答风格冲突时,正确解法是 #2(给每类范例)或 #7(按题型分流不同 prompt),
不是往一个 prompt 里叠更多规则。** 我们最终用 per-type prompt 路由解决——和 per-type
top_k / per-type 检索模式同构。

**当"规则"其实是一个决策边界时,正面解法是「完备的正交轴」**:别一条条枚举特例(每次 eval
失败补一条「不该输出 X」「除非 Y」——永远有没覆盖的格子,不断冒新 gap),而是找出决定这个决策的
**几个正交轴**,画**笛卡尔积表**逐格填,让 prompt 直接表达那张表。完备(MECE:互斥+穷尽)定义一次
覆盖所有格子、抗 gap、且把真分歧显式定位到具体一格。详见 `design-philosophy-intent.md` §9
(快路 `DROP/FIRE/DEFER` = 前向性 × 自包含性 × 锚类型 × 承诺来源 的函数,就是这么从散装清单
重写成表的)。

## 3. 迭代循环:改一处 → 跑 eval → 看噪声带 → 留真删假

prompt 改动是 LLM 行为改动,**run-to-run 有噪声**。遵守 `data-driven-iteration.md` 的纪律:

1. **一次只改一处**(一级技术 / 一段措辞),否则无法归因。
2. **跑 eval ≥3 次**,报 `mean ± band`(spread)。LongMemEval harness 有 `noiseband.py` 直接出带。
3. **delta 必须 > band 才算真提升**。单次 n=30 的 per-type 波动常达 ±0.05~0.07——单跑一次涨了不算数。
4. **真的留、假的删**。不要因为"看起来更全面"就保留一个在噪声内的改动。
5. **end-to-end 验证**:prompt 改的是某一环,要确认下游没被它破坏(v2 提升聚合却伤偏好,正是只看局部的代价)。

## 4. 守住已验证的不变量(改 prompt 别推翻它们)

仓库里有些 prompt 行为是**用 eval 反复确认过、且有回归护栏**的,改 prompt 时不要无意推翻:
- **意图识别**:婉转表达要识别、状态闸要抑制误触发、dedup 折叠——`intent_golden.yaml` 钉死,
  改 `intent_recognizer.system.md` / `intent_fast.system.md` 前先跑确定性档。
- **prompt 原则化、代码具体化**(见 `design-philosophy-intent.md`):prompt 写**短的、app-agnostic 的
  原则**,把"针对某个 app/某个例子"的具体逻辑放进**条件代码**,不要把过拟合某一个例子的长篇说明硬写进 prompt。
- **评分类 prompt(judge / rubric)是测量仪器,不是优化对象**:改了它 = 改了标尺,分数失去可比性。
  要么不动,要么明确声明"非官方 judge"并重建基线。

## 5. Checklist(提交任何 prompt 改动前自检)

- [ ] 这次改动有对应的 eval 吗?(没有先建)
- [ ] 我只改了一处吗?
- [ ] 跑了 ≥3 次、delta 超过噪声带吗?
- [ ] 下游/其他类型没被这次改动拖累吗?(end-to-end)
- [ ] 我是在加范例/分流,还是在堆硬规则?(后者要警惕)
- [ ] 有没有推翻某个被 golden/benchmark 钉死的不变量?
- [ ] 评分类 prompt 我没动(或已声明非官方)?

## 相关文档
- [`data-driven-iteration.md`](data-driven-iteration.md) — prompt 是"改动"的一种,本文是它在 prompt 上的特化;四层 oracle / 噪声带 / 认知熵 vs 偶然熵在那里。
- [`design-philosophy-intent.md`](design-philosophy-intent.md) — "prompt 原则化、代码具体化"的来源。
- 仓库根 [`CLAUDE.md`](../CLAUDE.md) 的 **LLM calls / Model names** 段（裸模型名 + 自动 prompt caching）— 技术阶梯 #8 的具体落地。
- prompt 实际存放点:`src/persome/prompts/*.md`;eval harness prompt 在各 `tests/eval/*`(团队本地)。

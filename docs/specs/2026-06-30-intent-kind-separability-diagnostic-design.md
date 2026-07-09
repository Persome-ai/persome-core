# per-kind 阈值可分性诊断（intent-audit 扩展）设计

> **Provenance.** 本设计文档成文于 Persome 产品 **Mens**（原生 macOS app）的开发中；文中出现 *Mens* 处指"某个 Persome 驱动的产品 / 预测器实例"，个别指向 Mens 代码库的路径/机制是**说明性示例**。属于 persome-core 的 daemon 部分（记忆 / 意图 / 检索 / 捕获）直接适用。

**Date:** 2026-06-30
**Status:** Implemented (diagnostic only; no threshold applied — data-bound)
**Slug:** `intent-kind-separability-diagnostic`

## 1. 为什么

精度 oracle（`persome intent-audit`, PR #365）的 baseline 选出真杠杆 = **per-kind 过度触发**（accept 率 31%，集中在 meeting ~18% / info_need ~25%）。自然的下一步是「给过度触发的 kind 加一道更高的 surfacing 阈值」。但**盲加阈值会伤召回**（杀掉用户其实想要的那几条）。在动手之前必须先**用数据回答**：对每个 kind，有没有一个 confidence / importance×urgency 阈值能**干净地**把"采纳"和"拒绝"分开？

一次真数据 peek 已证伪了最直觉的杠杆：meeting 的 importance×urgency **不可分**（采纳/拒绝都聚在 <0.5，盲加 iu 阈值会连采纳一起杀）。所以需要一个**可复现的诊断**，而不是一次性 ad-hoc 查询。

## 2. 是什么

扩 `intent.audit`：`_kind_separability(rows)` —— 对每个 kind，只用**过去的用户裁决**（采纳=`consumed`；真拒=`dismissed` 且 `dismissed_at` 非空，**排除引擎 TTL 收割**），对每个候选 score（`confidence`、`importance×urgency`）扫阈值，取 **Youden's J**（=灵敏度+特异度−1）最大的那个：
- **SEPARABLE**（best J ≥ 0.5）：存在干净分隔阈值 → 输出该 score≥T 下「保住几条采纳 / 砍掉几条拒绝」，供数据佐证的 gated 阈值决策；
- **NOT_SEPARABLE**（J<0.5）：score 分不开 → 过度触发是**识别质量问题**，别 threshold-tune（会伤召回）；
- **INSUFFICIENT**（任一类 < 3 样本）：诚实不判，不造假信号。

纯·确定性·零 LLM·只读（随 intent-audit 的 JSON + 文本一并输出）。

## 3. 真数据结论（the honest finding）

- assignment: **NOT separable**（best iu J=0.19）→ 识别质量问题。
- meeting: **borderline SEPARABLE** via confidence≥0.95（J=0.51）→ 砍 39/41 拒绝，**但只保住 5/9 采纳（丢 44% 召回）**。
- info_need: SEPARABLE via confidence≥0.85（J=0.57，保 3/3、砍 4/7）。
- reminder: SEPARABLE via confidence≥0.9（J=0.5，砍 6/6、**丢 3/6 采纳**）。

**判断**：这些 "SEPARABLE" 都在 **J≈0.5 的边界**、**采纳样本极小（n=6–9）**、且**有真实召回代价**。这是 **data/noise-bound，不是干净杠杆**。按方法论（"know when it's data-bound, not method-bound"）+ 不伤召回护栏：**只交付诊断，不在 n=9 的噪声上自动套阈值**（那是 product/召回权衡，留给人，且需更多数据或 LLM 档 eval 验召回）。诊断把精确权衡数字交到人手上。

## 4. 不变量 / 安全

- 只读·确定性·fail-open·PII-free（沿用 oracle）。无识别路径改动 → intent-golden 确定性档天然绿。
- 只数 USER 裁决（`dismissed_at` 非空），harvest 收割不进样本。
- 样本不足 = INSUFFICIENT，从不造假 verdict。

## 5. 验证

- `PERSOME_LLM_MOCK=1 uv run pytest tests/test_intent_audit.py`（15 例：含 SEPARABLE 干净分/NOT_SEPARABLE 重叠/INSUFFICIENT/只数用户拒）+ intent-golden 确定性档 + ruff。
- 真 DB：`persome intent-audit` 末段打印 per-kind 诊断（聚合数 + verdict，PII-free）。

## 6. 后续

- 数据攒够（per-kind 采纳 ≥ ~30）后重跑诊断；若某 kind 稳定 SEPARABLE 且召回代价可接受 → 再做 gated per-kind surfacing 阈值（带本诊断的数字佐证 + LLM 档 eval 验召回不跌）。
- assignment 这类 NOT_SEPARABLE 的 → 走识别质量（prompt/few-shot）而非阈值，且 golden-eval-gated。

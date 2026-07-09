# LongMemEval — production-faithful pipeline harness (compress + classify, not verbatim)

> **Provenance.** 本设计文档成文于 Persome 产品 **Mens**（原生 macOS app）的开发中；文中出现 *Mens* 处指"某个 Persome 驱动的产品 / 预测器实例"，个别指向 Mens 代码库的路径/机制是**说明性示例**。属于 persome-core 的 daemon 部分（记忆 / 意图 / 检索 / 捕获）直接适用。

> Status: implemented (Phase 1). Companion to the earlier verbatim-harness design.
> Code under `tests/eval/longmemeval/{pipeline.py,run_pipeline.py}`.

## 1. Why

The verbatim harness ingests each LongMemEval haystack session **as-is** into the FTS
`entries` table and retrieves over that perfect text — so it measures **retrieval over
lossless memory**. Production never has lossless memory: the daemon runs

```
timeline_blocks → session_reducer.reduce_session (LLM compress → summary+sub_tasks)
                → event-YYYY-MM-DD.md
                → classifier (tool loop) → topic-/person-/project-*.md (cross-session entities)
                → FTS5 entries  ← what fts.search retrieves
```

So the verbatim recall@10≈0.98 is an **upper bound production cannot reach**: if the
reducer drops a fact, no retriever can find it. This harness routes the haystack through
the **real reduce + classify stages** (same prompts, same DeepSeek model as production) so
the benchmark measures the actual end-to-end system — the "does our memory *formation*
keep the answer?" axis the verbatim harness can't see.

## 2. The metric swap (load-bearing)

LongMemEval's native metric is **session-recall@k** — did we retrieve evidence session X,
matched by the `sid` encoded in the entry path. **The full pipeline destroys session
boundaries**: `event-YYYY-MM-DD.md` merges same-date sessions, and the classifier abstracts
facts across sessions into entity files. There is no longer a per-session entry to attribute
a `sid` to, so **session-recall@k no longer applies**. We replace it with:

- **End-to-end QA accuracy** (primary): reader retrieves over the PRODUCED memory →
  answers → official per-type judge (reuse `judge.py`). Δ vs the verbatim QA baseline
  (OVERALL 0.866) = **what production's memory formation costs**.
- **Answer-survival rate** (diagnostic): one LLM call per question — "does the produced
  memory contain the answer to Q?". Separates *compression dropped the fact* (survival=0)
  from *retrieval/reader failed* (survival=1, QA=0). Reported as survival × QA per type.

## 3. Design — per-question isolated full pipeline

Each question gets its **own** temp `PERSOME_ROOT` store, so sessions from different
questions never merge in a shared `event-*.md`. Per question Q (sessions `S_i`, dates `D_i`):

1. **Seed** blocks + a `sessions` row per `S_i` via the production-faithful
   `tests/eval/proactivity_sessions.seed_session` — each turn becomes a "capture"
   (`focus_structured = "<role>: <content>"`, `apps=["Chat"]`, `ts` pinned to `D_i + j·min`).
   One LongMemEval session → one daemon `sessions` row + one `timeline_block` per turn.
2. **Reduce** each session: real `session_reducer.reduce_session` (DeepSeek) → compressed
   entry in `event-<D_i>.md` (real same-date aggregation).
3. **Classify** each session in date order: real `classifier.classify_after_reduce` →
   cross-session entity files (`topic-/person-/project-*.md`).
4. **Retrieve over the whole store** (the store *is* Q's memory) — `search_for_question`
   with `path_glob="*"` (the produced entries aren't `topic-lme-<qid>-*`-named), default
   tuned RRF + hybrid pool — then `generate.generate_all` reader → `judge.judge_all` +
   survival.

## 4. Fidelity caveats (honest)

- The **`timeline` aggregator LLM stage** (captures→block entries) is skipped — chat turns
  are already clean text; the load-bearing losses are reduce + classify. So this slightly
  *over*-estimates fidelity vs production (which also loses signal at the timeline stage).
- **Chat-session → synthetic timeline-block** mapping is an approximation (chat turns
  aren't AX captures), but it drives the REAL reduce/classify prompts + model.
- **Per-question isolation** means cross-QUESTION entity sharing isn't modeled — correct
  for LongMemEval's per-question structure, but a real daemon has one global memory.

## 5. Resumability (data-driven-iteration §10)

- Per-question **checkpoint**: `run_pipeline` appends each question's verdict to
  `--verdicts-out` JSONL and **skips question_ids already present** on resume — a crash /
  credit-exhaustion re-run re-builds only the unfinished questions.
- **Reduce-LLM cache** (`pipeline.py`, sqlite keyed by `sha256` of the exact reducer
  messages): a `call_llm` wrapper caches the `reducer` stage; an identical reducer call
  (same blocks + preceding context — common on re-run, occasional cross-question) is a free
  hit. The `classifier` stage is NOT cached (stateful tool loop over prior entity files).

## 6. Cost

First run `--sample-per-type 30` (~210 q × ~53 sessions ≈ 11k reduce + 210 classify
tool-loops + 210 gen/judge/survival), sharded + resumable. Reduce/classify/reader/judge all
DeepSeek via the existing `ANTHROPIC_BASE_URL` wiring; embeddings reuse the warm
`lme_emb_cache.db`.

## 7. Out of scope (this phase)

Wiring RRF / hybrid-pool / cross-encoder into the **production** `fts.search` (needs a real
memory vector index) — a separate effort. This phase makes the *benchmark* faithful; it does
not change production retrieval.

## 8. 首跑结果 — 生产 reducer 压缩的真实损失(sample-30,reduce-only)

reduce 模型 = **deepseek-v4-flash = 生产 `[models.default]`/`[models.reducer]` 实配模型**
(已核 `config.py`),所以这不是"某个更狠的模型"的上界,而是**生产 reducer 阶段的真实损失**。
高并发经 OpenRouter 多 provider 扇出跑完(~30min,reduce 成功率 99.2%)。

| 题型 | verbatim QA | pipeline QA | ΔQA | survival |
|---|--:|--:|--:|--:|
| **OVERALL** | 0.805 | **0.557** | **−0.248** | 0.443 |
| temporal-reasoning | 0.700 | **0.000** | **−0.700** | **0.000** |
| single-session-assistant | 1.000 | 0.500 | −0.500 | 0.533 |
| multi-session | 0.600 | 0.333 | −0.267 | 0.433 |
| single-session-user | 0.967 | 0.733 | −0.233 | 0.800 |
| knowledge-update | 0.867 | 0.667 | −0.200 | 0.833 |
| abstention | 0.833 | 0.933 | +0.100 | 0.033 |
| single-session-preference | 0.667 | 0.733 | +0.067 | 0.467 |

**结论:**
1. **生产 reducer 压缩平均吃掉 ~25 个 QA 点**(0.805→0.557)。verbatim recall@10≈0.98 是对无损
   原文的召回,是幻觉;生产存压缩记忆,端到端 ~0.56。这是 verbatim harness 测不到的记忆-形成轴。
2. **temporal 归零(QA 0.000 / survival 0.000)= 确凿生产 bug**:reducer 压掉了精确日期/时间戳,
   时间推理题的答案在记忆里**不复存在**,检索救不回。**可直接改的产品改进:reducer 必须保留时间锚点。**
3. **survival 分两类失败:** 低 survival = 压缩把答案丢了(temporal/multi,改 reducer);高 survival
   但低 QA = 答案还在、检索/reader 没找到(knowledge-update 0.83 / ss-user 0.80,可靠检索改进救)。
4. **反直觉的 +:** abstention +0.10(压掉干扰 → 更敢拒答)、preference +0.07(压成"用户喜欢 X"恰是偏好题要的)。

**剩余唯一会让损失变小的标尺:** classify 关闭(`--no-classify`)。分类器抽 person/topic/project 实体
可能救回部分事实,所以 **−0.25 是全管线损失的下界**(开 classify 大概率损失更小)。其余 fidelity 标尺
(timeline 阶段跳过、合成 block、每题隔离)见 §4,次要。

**性能(分布式高并发):** reducer 从单 DeepSeek 账号(~6 并发上限,~0.9 reduce/s,3.5h)改路由到
OpenRouter 多 provider 扇出 + v4-flash → ~5.3 reduce/s,sample-30 全量 ~30min(~7×)。`run_pipeline
--reduce-model deepseek/deepseek-v4-flash --reduce-workers 40`,8 分片。空响应重试 + ```json 围栏剥离
是高并发下的两个必需 robustness 修复(否则静默丢 reduce → 假结果)。

## 9. 更正 — temporal 归零是 HARNESS 的日期 bug,不是生产 reducer

§8 把 temporal QA 0.000 当成"生产 reducer 压掉时间锚点"。**这是错的,予以收回。** 根因是本
harness 的 `_session_captures` 合成映射:它把会话起点设为 `midnight(date) + sess_idx*128 分钟`,
对第 20+ 个 session 会 **+1.78 天跨过午夜,把日期推到错误的日子**,还丢了真实 HH:MM。temporal 题
考的就是日期,被我自己搞乱 → 归零。

修复:`_session_captures` 改用**会话的真实 haystack 日期+时间**(+ 秒级 per-session tiebreak 防
block 碰撞、不跨天)。这是个**客观正确的代码 bug 修复**(原映射确实把日期算错了)。

**但修复对 temporal 的实际效果 = 未验证(重要,别再误读)。** date-fix 全量重跑跑到一半 OpenRouter
credit 耗尽($40 用满)→ 165/210 题(含**全部 36 道 temporal**)记忆为空。曾一度以为"temporal
6/8=75% 恢复",**经核实那 6 道全是 `entry_count=0` 的空记忆题**——reader 瞎蒙、宽松的 temporal
judge(允许 off-by-one)算对的**假阳性**,不是真恢复。故:**temporal 是否恢复,零个有效数据点,
待干净重跑才能下结论。**

**唯一有效的 date-fix 数据**(记忆非空):ss-user 30 道 ΔQA −0.233(与 §8 一致)、abstention 15 道
ΔQA −0.367(§8 是 +0.10,此处仅 15 道,样本小不可比)。其余题型 credit 耗尽前未跑成。

**结论:§8 的 OVERALL −0.248 仍是已知最完整的数字(虽含 artifact-broken 的 temporal);干净的
temporal-修复后 A/B 待 OpenRouter 充值重跑(~14min)。不要据 §8 的 temporal=0 改生产 reducer。**

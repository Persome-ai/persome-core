# 实现 plan：正交 facet 化的快路意图识别（含删词面闸）

> **Provenance.** 本设计文档成文于 Persome 产品 **Mens**（原生 macOS app）的开发中；文中出现 *Mens* 处指"某个 Persome 驱动的产品 / 预测器实例"，个别指向 Mens 代码库的路径/机制是**说明性示例**。属于 persome-core 的 daemon 部分（记忆 / 意图 / 检索 / 捕获）直接适用。

> 配套 spec：`2026-06-26-faceted-fast-path-intent-schema-design.md`。
> 范围 = Phase A（只 `conversational` 通道）。oracle 先行、`kind` 投影桥保零回归、爆炸半径最小。

## 两条主线

1. **表示重构**：扁平 `kind` → 正交 facet 元组（facet 进 `payload`，`kind` 变确定性投影）。
2. **成本闸重构（决定：B）**：删快路 ④ 词面正则闸，单次 LLM 当裁判；throttle/backoff 升为唯一的
   **自适应**前置成本闸（由真实 LLM 0/≥1 结果驱动）。

## 数据依据（删闸决定 + backoff 初值）

`~/.persome/chronicle/index.db` `fast_path_ticks`，8888 capture / 9 天（2026-06-17~26）：

- 终止 gate：`non_user 49.8% · not_conversation 25.2% · no_parser 20.3%`（共 95.3%，在 ①② 就挡，
  不近 LLM）；`no_anchor 2.0%(181)` · `no_unseen 1.7%` · `recognized 0.6%(51)` · `throttled 0.1%(9)`。
- **删闸后 LLM 调用 ≈ 232/9天 ≈ 26/天**（现 6/天）——绝对量噪声级，加缓存可忽略 → **B 成本验证通过**。
- `recognized` 白烧率 **47/51=92%**（persisted=0）→ backoff 是唯一阻尼，且当前对 B 太激进。

## backoff 初值（删闸后；偏召回、只挡洪流、快恢复）

| 配置 | 现值 | 初值 | 理由 |
|---|---|---|---|
| `intent_recognizer.per_app_min_interval` | 2.0 | **2.0** | 合并连发，2s 后真消息仍过 |
| `intent_recognizer.backoff_max_misses` | 3 | **6** | 只在真洪流起退避 |
| `intent_recognizer.backoff_base_seconds` | 30 | **20** | 起步更缓 |
| `intent_recognizer.backoff_max_seconds` | 600 | **120** | 冷却 app ≤2min 恢复，真排期不被埋 |

> caveat：单用户/9天/IM 量偏低的快照；结构稳、支持 B，绝对量小。**这是初值，实现后对 live
> `fast_path_ticks` 校准**（量删闸后 0-intent 调用/分钟 + 有无真意图被 backoff 跳过），非终值。

## 改动清单（按文件）

**🆕 新增**
- `src/persome/intent/facets.py`：`Facets` 数据类（telos/object/temporality/provenance/outwardness +
  condition/recurrence/object_entity）；`assemble(llm_out, *, direction, counterpart, when_text)->Facets`
  （确定性装配 + 冲突优先级，见 spec §装配规则；丢弃无消费方的 hint）；`project_kind(Facets)->str`
  （迁移桥投影表）；`TELOS_OUTWARDNESS` 数据表。**纯函数、零 LLM、零网络。**
- `tests/test_intent_facets.py`：assemble 各规则 + project_kind 投影 + hint 丢弃 的确定性单测。

**✏️ 改**
- `src/persome/prompts/intent_fast.system.md`：契约从「kind + per-kind 字段」→「telos + provenance +
  `*_hint` + when_text + scores + needs_trajectory」。prompt 写**原则**（隐式周期/任意条件「即使没逐字写
  也说出来」），不列例子表；`*_hint` 两条硬约束写进 prompt。
- `src/persome/intent/fast_recognizer.py::recognize_event`：新增**快路专用装配路径**——解析新 JSON →
  `facets.assemble(...)` → `intent.kind = project_kind(...)`、`intent.payload = {**facets, when_text,
  scores}` → 复用现成 `sink.persist_intent` + `stamp_temporal`。**不动共享的
  `recognizer.intents_from_payload`（慢路也用）。**
- `src/persome/intent/event_source.py::on_capture`：**删快路 ④** —— 去掉 `should_recognize_k1` 调用 +
  `no_anchor` drop 分支；verdict 简化为 {throttled, recognize}；**throttle 提到无条件**（每条 seen-set
  幸存者都过 throttle，其 LLM 0/≥1 结果喂 backoff）。`no_anchor` 遥测桶退役。
- `src/persome/config.py`：backoff 四个键改初值（见上表）。

**⚠️ 不动（去风险）**
- `_ANCHOR_RE` 保留（**慢路** `SLOW_ANCHOR_RE` 组合复用它；只删快路对 `should_recognize_k1` 的使用，
  该函数无其它引用则一并删）。
- `intents` 表 schema、`dedup_key`、`stamp_temporal`、`sink`、seen-set(③)、慢路、app、`FollowUpRules`。

## 实现顺序（oracle 先行）

1. **`facets.py` + 单测**：assemble + project_kind，零 LLM。先立 oracle 支点。
2. **升级 intent-golden**（`tests/eval/golden/intent_golden.yaml` + 确定性档）：
   - 现有快路正例标全 facet；加**投影零回归断言**（facets→kind == 旧 kind）；
   - 补**新象限例**：`recurring`(日报/周会) · `conditional`(等发布完/CI过) · `object≠对手方`(帮我约B)，
     量「潜在槽」是否被 lift。
3. **改 prompt** `intent_fast.system.md` 到新契约。
4. **接 `recognize_event`** 走新装配路径（慢路 `intents_from_payload` 不动）。
5. **删闸 + throttle 无条件化 + backoff 初值**（`on_capture` + `config.py`）。
6. **LLM 档**（真 key）在新 golden 上量 telos/provenance 的 firing precision/recall，对噪声带迭代 prompt。
7. **端到端**：fixture capture → `recognize_event` → 查 `intents` 表：payload 带 facet、kind 投影对、
   resolved_at/valid_until 对、来源×外向打扰闸生效。

## 验收 / 不变量（每步都要过）

- 现有 intent-golden **零回归**（经 facet→kind 投影断言）。
- 新象限 golden：recurring/conditional/object≠对手方 被正确 lift。
- 不变量：精度优先（拿不准空）、<5s（单次缓存调用、`lean_focus` 仍裁新到达）、never-raise、
  事件只评一次(#274)、cost-gate-on-survivors、place-never-send、inferred 零打扰、`needs_trajectory` 搭车。
- 删闸校准：实现后对 live `fast_path_ticks` 量 0-intent 调用/分钟在带内 + 无真意图被 backoff false-skip；
  按需微调 backoff 四值。
- 本地四闸（daemon 侧）：`PERSOME_LLM_MOCK=1 uv run pytest tests/eval/test_intent_golden_deterministic.py
  tests/test_eval_metrics.py tests/test_intent_facets.py -q` 绿。

## 打扰闸（Phase A 接线但少触发）

`provenance=inferred ⇒ 零打扰`：会话通道 inferred 几乎不出现，Phase A **存 provenance 进 payload +
surfacing 决策处加 来源×外向 判断**，完整发挥等 Phase B 有 inferred 数据源。

## 非目标

不做并行 LLM（除非 oracle 证明单 prompt 指令干扰）；不做精度投票；不碰非会话通道（Phase B）；
不碰 app 侧 `FollowUpRules` 的 kind→facet 改造（投影桥扛着）。

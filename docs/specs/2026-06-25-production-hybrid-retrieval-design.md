# 生产混合语义检索 — 把 benchmark 验证过的 RRF + te3-large 接进守护进程记忆检索

> **Provenance.** 本设计文档成文于 Persome 产品 **Mens**（原生 macOS app）的开发中；文中出现 *Mens* 处指"某个 Persome 驱动的产品 / 预测器实例"，个别指向 Mens 代码库的路径/机制是**说明性示例**。属于 persome-core 的 daemon 部分（记忆 / 意图 / 检索 / 捕获）直接适用。

> Status: design（待实现）。来源：LongMemEval 实验里**唯一验证过有真实增益**的检索方案 =
> BM25 ⊕ dense 的 **RRF 融合 + 混合候选池**（`2026-06-22` / `2026-06-25` 两个 spec）。本 spec 把它
> 从 benchmark adapter 迁进**生产记忆检索**，并做必要适配。用户明确授权：用 **te3-large**（非本地
> bge）、建**向量数据库**、为效果可大改结构/设计原则。

## 1. 目标 / 上什么

把生产记忆检索从**纯 BM25**（FTS5）升级成**混合语义检索**：
```
查询 → BM25 top-N (FTS5) ⊕ dense top-N (te3-large 向量 cosine) → RRF 融合 → top-k
       └─────────────── 混合候选池(并集) ───────────────┘
```
- **嵌入器 = OpenAI `text-embedding-3-large`（3072 维）**，经 Persome relay + 用户 JWT 调用（不 bundle key）。
- **RRF 融合**：复用已存在但默认关的 `evomem/vector_recall.py`（已实现 BM25/LIKE ⊕ dense 的 RRF,k=60),
  泛化成共享的 `fts.search_hybrid`。
- **混合候选池**:BM25 top-`recall_n` ∪ dense top-`recall_n`(dense 腿对全语料 cosine)→ 并集 RRF,
  让 BM25 零词面重叠漏掉的纯语义匹配也能进。
- 参数起点 `recall_n=50 / rrf_k=20`(benchmark 调优值)——**但必须在生产数据上重调,见 §5。**

## 2. 不上什么(诚实)

- **cross-encoder rerank**:benchmark 上是 data-bound(噪声带证伪,非验证增益)+ 每查询一次 Cohere 云调用
  (成本/延迟)。**不进生产**,留作未来可选 rerank 档(gated)。
- **reader prompt 调优**:六次全失败,不涉及。
- **dense-first ANN 索引**(sqlite-vec/faiss):语料 10k–50k 向量暴力 cosine 够用(~50-100ms),**v1 不引入
  ANN**;>100k 向量或 P99 吃紧再上(§7)。

## 3. 架构(四块)

### 3.1 嵌入经 relay(te3-large,无 bundle key)
- **新建 `src/persome/writer/embeddings_client.py`**:`embed(text) -> list[float] | None` / `embed_batch(texts)`。
  urllib POST 到 `{provider_base_url("openai")}/api/llm/embeddings`,JWT 走 `x-api-key`(同 LLM 路径),
  `{model:"text-embedding-3-large", input:[...]}`,OpenAI-shaped 响应。**fail-open 返回 None**。
- **config 已就绪**:`provider_api_key("openai")`/`provider_base_url("openai")`(`config.py:919`,已定义未用)。
  env:`OPENAI_API_KEY=<JWT>` / `OPENAI_BASE_URL=<relay>`,由 App EnvVault 写入 `~/.persome/chronicle/env`。
- **新 persome-server 路由 `/api/llm/embeddings`**(另一个 repo,本 spec 只定契约):JWT 鉴权 → 转 OpenAI
  embeddings → 计量 `usageKind="embedding"`。契约同 `/api/llm/anthropic`、`/api/context-feedback`。
- **依赖**:加 `numpy`(轻量,cosine + 向量打包);urllib 无需新依赖。

### 3.2 向量索引(新表 + 写入即嵌入 + 回填)
- **新表**(`fts.py` 加 `_ensure_entry_vectors`,在 `connect()` 第 285 行后):
  ```sql
  CREATE TABLE IF NOT EXISTS entry_vectors (
    entry_id TEXT PRIMARY KEY, vector BLOB NOT NULL,  -- 3072×float32
    model TEXT NOT NULL, embedded_at TEXT NOT NULL );
  ```
- **写入即嵌入(异步,不阻塞)**:`derived_append_rows`(`entries.py:81`,所有写汇聚点,含 evomem inversion)
  在 `fts.insert_entry` 后**只入队**(标记该 entry 待嵌入),**绝不在写锁内同步调网络**——否则阻塞 capture。
- **新守护任务 `vector-embed-tick`**(`daemon.py` 任务注册表,gated on `[retrieval] hybrid_enabled`):
  周期性 drain 待嵌入队列 → `embed_batch` → upsert `entry_vectors`。eventual:向量稍后到位,未嵌入的
  entry 检索时走 BM25 fallback(§3.3)。
- **回填**:`rebuild-index`(`entries.py:635` 的 `_ingest_markdown_file`/`_rebuild_from_evo_nodes`)对缺向量的
  entry 批量嵌入;一次性 `vector-backfill` CLI。
- **失效维护**:supersede/retire(`derived_supersede_rows`/`derived_retire_rows`)删对应 `entry_vectors` 行。

### 3.3 混合检索(`fts.search_hybrid`)
- **泛化 `evomem/vector_recall.py` 的 `fuse`** 成共享函数:输入 BM25 hits + dense hits → RRF(rrf_k 可配)。
- **`fts.search_hybrid(conn, query, *, top_k, recall_n=50, rrf_k=20, path_patterns, since, until)`**:
  1. BM25 = 现有 `fts.search`(OR + bm25),top `recall_n`;
  2. dense = embed(query) → 对**全活向量**(filter path/superseded)brute-force cosine,top `recall_n`(numpy);
  3. 并集 → RRF 融合 → top_k(EntryHit,与现有同形,无缝替换)。
- **fail-open**(load-bearing):embed(query)=None(relay down/signed-out/未配)→ **退回纯 BM25**,
  字节等价于今天的行为,检索绝不因向量层失效而坏。
- **接入 4 个调用方**(优先级):`mcp/server.py:145`(search_memory,agent 用,最高价值)→
  `chat/tool_handlers.py:21` → `writer/tools.py:92`(dream/classifier)→ `writer/consolidator.py:284`。
  每处把 `fts.search(...)` 换 `fts.search_hybrid(...)`(签名向后兼容,gated)。识别器 recall
  (`intent/recall.py` 裸 SQL 分层)是 Phase 3 再议。

### 3.4 开关与降级
- **新 config `[retrieval] hybrid_enabled`(默认 OFF)**。OFF = 纯 BM25,零向量成本,字节等价现状。
- 任何环节(无向量/relay down/numpy 缺)→ fail-open BM25。**向量层永远是叠加增强,不是依赖。**

## 4. 设计原则的改变(用户已授权,如实记录)

| 原则(现状) | 改后 | 缓解 |
|---|---|---|
| Persome 不 bundle 任何 key、检索本地/免费/即时 | 记忆检索依赖**云嵌入**(te3-large):写时入队嵌入(异步)、查时 embed query(网络);**计量成本** | JWT 走 relay(不 bundle key,同 LLM);fail-open BM25;flag 默认关 |
| 离线可用 | 向量层离线/登出失效 | fail-open 纯 BM25,功能不缺只是降语义 |
| 零额外重依赖 | 加 `numpy` | 轻量、纯 cosine 用 |

**这是真实的原则让步(检索从"本地免费"变"云增强")——故全程 fail-open + 默认关 + 先在生产数据验证再开。**

## 5. 适配关键:benchmark 参数不直接迁移,必须重调(诚实)

- benchmark 用 **te3-large(英文)+ recall_n=50/rrf_k=20**,在 **LongMemEval(英文、每题小 haystack)** 上调。
  生产是**中文为主、跨天大语料、查询形态不同**(supervisor memory search / agent search_memory)。
- **迁移的是"模式"**(RRF 融合 + 混合池有效),**不是具体参数**。`recall_n/rrf_k` + 是否开混合池,
  **必须在生产记忆上重调**。
- **需要生产检索 oracle**:新建一个记忆检索 golden(类似 `intent_golden`),用真实/脱敏记忆 + 已知该捞回的
  条目,量 recall@k——在它上面重调参数、做 A/B,而不是套用 benchmark 的 0.866/50/20。

## 6. 上线分期(每期可独立验证、可回滚)

1. **Phase 1 — 向量索引(无检索改动)**:`entry_vectors` 表 + `embeddings_client` + 写入入队 + `vector-embed-tick`
   + 回填,全程 gated `[retrieval] hybrid_enabled`(默认关 → 不产生任何向量调用)。验证:开 flag 后向量按写入
   增长、relay 计量正常、capture 不被阻塞。
2. **Phase 2 — `fts.search_hybrid` 接入**:泛化 `vector_recall.fuse` → 接 4 个调用方(gated)。验证:flag 关
   = 纯 BM25 字节等价;flag 开 = 混合检索,fail-open 路径覆盖。
3. **Phase 3 — 生产重调 + A/B + 切开**:建记忆检索 golden,重调 `recall_n/rrf_k`,在生产数据 A/B(混合 vs BM25),
   噪声带外为正才把默认翻 ON。识别器 recall 接入也在此期评估。

   **状态(已落地 / data-bound)**:记忆检索 golden(`tests/eval/golden/memory_retrieval_golden.yaml`,生产
   形态、中文为主、含 lexical/paraphrase/作用域桶)+ recall@k 谐 + `recall_n/rrf_k` 扫格 harness
   (`tests/eval/memory_retrieval_eval.py`)+ 确定性 gate 档(`tests/eval/test_memory_retrieval_deterministic.py`,
   concept→one-hot embedder 替身,断言 BM25 漏中文改写、hybrid 捞回——证明 RRF 杠杆通路,零网络)**已落地进默认
   gate**。

   **✅ 真实 A/B 已跑通,默认已翻 ON(creds-gated)**:用 Azure te3-large(bring-your-own-key,
   `embeddings_client` 已兼容 Azure `/deployments/…?api-version=` + `api-key` 头)给生产 987 条 backfill 真
   向量,在两种查询形态上 A/B:
   - **改写查询(真实用户搜索形态,LLM 生成 80 条)**:BM25 recall@10 = **0.025**(中文改写下词面近乎失效)→
     hybrid = **0.76**(+0.737);recall@20 = 0.025 → 0.80。**这是 hybrid 的真正价值,决定性证据。**
   - **词面查询(照抄 token,罕见)**:hybrid 比 BM25 略低(−0.02 ~ −0.04,任何 `recall_n/rrf_k` 都为负——
     BM25 主场,dense 给近重复 event 桶加兄弟噪声)。
   据此把 `[search] hybrid_enabled` **默认翻 ON**,但 `daemon._run` 仅在 `embeddings_client.available()`(配了
   `OPENAI_*` 端点)时激活写入入队 + dense 读路,**没配 embedding 端点的安装保持字节等价 BM25、不攒 `vector_queue`**——
   fail-open 让 default-ON 对所有用户安全。读路 `live_matrix` 加了进程内矩阵缓存(validity-token 失效),避免每查询
   重建(50k 向量 ~8–9× 提速)。识别器 `recall.assemble_background` 的混合化仍待评估。

   **生产 BM25 基线(option B,已跑通)**:`tests/eval/production_baseline.py` 把真实
   `~/.persome/chronicle/index.db` 一致性快照到临时库(**绝不动生产、用 `_bm25_pool` 零写回**),自派生
   keyword 查询(每条 = 目标 entry 自身的若干 token,真值 = 该 entry id),端到端跑 BM25 recall@k,
   **只输出聚合数字(隐私安全,不吐任何记忆内容)**。确定性 gate 档
   `tests/eval/test_production_baseline_deterministic.py`(合成库,零网络)守住快照/派生/recall 管线。
   **实测(987 条活跃记忆,sample=200)**:BM25 recall@10 = **0.66**(seed 0/1/2 = 0.67/0.645/0.66,
   噪声带 ±0.013);q_tokens 1/2/3 = 0.42/0.67/0.865;k 5/10/20 = 0.58/0.67/0.735。**逐桶真值(每桶抽满)**:
   schema 1.0 / thread 1.0 / intent 0.92 / project 0.90 / user 0.76 / person 0.74 / event 0.64 / **skill 0.49**
   (n=97,真正低点)。**这是 BM25 的词面天花板(自取 token 查询)**——hybrid 的增益落在 paraphrase 查询上,
   需 te3-large 凭据才能加 dense 臂做 A/B;此基线是那次 A/B 的对照地板。报告图:
   `assets/2026-06-25-hybrid-retrieval-baseline.png`。

   **<0.8 桶归因(诊断:深度恢复 / 同类相残 / token DF)**:四个低桶(skill 0.49 / event 0.64 / person 0.74 /
   user 0.76)**都不是召回失败而是排序失败**——`r@200≈0.94–1.0`,目标几乎总在候选池里,只是排在 10 名外。
   两个根因:(1) **高频泛 token 淹没**(漏掉的查询其 token 平均 DF 是命中的 2–10 倍:skill 355/185、event
   306/115、person 337/84、user 211/29),BM25 的 OR 候选池被共享该词的条目灌满;(2) **同类近重复相残**
   (miss 时 top-10 混进同类型别的条目:event **0.96** / skill 0.84 / user 0.71;person 0.33 是例外,纯泛词所致)。
   含义:这正是 dense+RRF 的靶子——dense 按语义而非词频排序,能把目标从高频洪流顶上来;且 `r@200≈0.95` 说明
   只需在 BM25 池上做一次 **dense 重排**就能显著抬 recall@10。有凭据后量化。

## 7. 范围外
- ANN 索引(sqlite-vec/faiss)——语料超 100k 向量再上。
- cross-encoder rerank(data-bound,云成本)。
- 识别器 `recall.assemble_background` 的混合化(Phase 3 评估,非首期)。

## 8. 验证
- 单测:`embeddings_client`(mock relay,fail-open)、`fts.search_hybrid`(RRF 融合 + fail-open 退 BM25)、
  `entry_vectors` schema/写入/失效——全进默认 gate(`PERSOME_LLM_MOCK=1`,零网络)。
- 端到端:开 flag 在临时 root 写几条记忆 → 向量入库 → hybrid 检索返回语义命中;关 flag → 字节等价 BM25。
- 生产 oracle(Phase 3):记忆检索 golden 的 recall@k + A/B 噪声带。
- 既有 daemon gate(intent-golden 等)保持绿;docs-drift(daemon task registry 改了要同步 CLAUDE.md 表)。

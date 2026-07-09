# 整体 DB schema 参考文件(db-schema dump)

> **Provenance.** 本设计文档成文于 Persome 产品 **Mens**（原生 macOS app）的开发中；文中出现 *Mens* 处指"某个 Persome 驱动的产品 / 预测器实例"，个别指向 Mens 代码库的路径/机制是**说明性示例**。属于 persome-core 的 daemon 部分（记忆 / 意图 / 检索 / 捕获）直接适用。

> Status: approved (2026-07-08). 为分散在 ~25 个 store 模块里的 SQLite DDL 生成一份**可提交、可再生成、
> CI 防漂移**的整体 schema 参考文件 `docs/db-schema.sql`。模式完全对齐既有惯例
> `scripts/regen_openapi.py` + `tests/test_openapi_drift.py`。**不改动任何现有 store 代码**。

## 为什么

- `index.db` 的 DDL 按模块分散(各模块 `CREATE TABLE IF NOT EXISTS` + 自带 `ADD COLUMN` migrate),
  这是有意设计;但代价是**整体 schema 在仓库里没有任何一处完整存在**。
- `fts.connect()` 只 ensure 了 ~11 个模块;另有 ~10 个模块(workthread、relation_edges、memory_deltas、
  evomem `evo_nodes` 等)是惰性建表——人和 AI agent 想看全貌只能全库 grep。
- 手写文档会漂移;中心化重构风险高且与 per-module migrate 设计冲突。生成式参考文件 + CI 校验是
  准确性与成本的最优点。

## 产出物

1. **`docs/db-schema.sql`**(提交入库,生成文件)
   - 文件头注释:声明为生成文件、勿手改、用 `uv run python scripts/regen_db_schema.py` 再生成。
   - **按来源模块分节**(注释分隔),节的归属由"注册表逐步 apply + sqlite_master 前后 diff"自动推导,
     不需要手工维护映射。
   - DDL 取 `sqlite_master.sql` **原文**(不改写),节内按 (type, name) 确定性排序。
   - 过滤 FTS5 影子表(`entries_data` / `entries_idx` / `captures_fts_*` 等)。
   - 末尾附 `meeting_*.db` 一节(独立小库,每会议一个文件,含 `transcripts` / `pushes`)。
2. **`src/persome/store/schema_dump.py`**(生成逻辑)
   - 注册表:有序列表 `(section, apply_fn)`。第一项走真实路径 `fts.connect(tmp_path)`
     (覆盖 fts 自身 SCHEMA + connect 内 ensure 的模块);其余为各惰性模块的
     `ensure_schema(conn)` / evomem 的 `_CREATE_SQL` + `_migrate(conn)` 等模块级函数。
   - **不实例化 store 类**(如 `NodeStore()` 会经 `fts.cursor()` 摸真实用户库),一律只对临时
     连接调用模块级函数;整个 dump 过程绝不接触 `~/.persome`。
   - 每 apply 一步,对 `sqlite_master` 做前后 diff → 该步新增对象即该节内容。
3. **`scripts/regen_db_schema.py`**(壳子照抄 `regen_openapi.py`,写文件、打印路径与字节数)
4. **`tests/test_db_schema_drift.py`**,两个测试:
   - **漂移**:内存重新生成,与已提交文件逐字比对;失败消息提示运行再生成脚本。
     纯 SQLite、无网络、无 macOS 依赖,落在 CI 的 `-m "not macos and not integration and not eval"` 集合内。
   - **完整性**:正则扫 `src/persome/**/*.py` 的 `CREATE [VIRTUAL] TABLE [IF NOT EXISTS] <name>`,
     断言每个表名都出现在生成结果里——新模块惰性建表却忘了注册进 dump 时 CI 直接报错。

## 不做的事

- 不动任何现有 store 模块、不合并 DDL、不改 migrate 模式——纯增量。
- 不做手写领域说明文档(表含义看各模块 docstring);不做运行时校验。

## 成功标准

- `uv run python scripts/regen_db_schema.py` 幂等生成 `docs/db-schema.sql`;连续两次运行输出逐字节一致。
- 任一模块 DDL 变更而未再生成时,CI 漂移测试失败。
- 源码中出现新的 `CREATE TABLE` 而未注册进 dump 时,CI 完整性测试失败。
- 生成过程不读写 `~/.persome`。

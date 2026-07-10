"""EvoMemory engine —— 编排 store + reconciler + evolution chains.

三系统对外入口（SSOT 切换设计 §1.3：engine 是唯一写口，两类入口对应两类写需求）：
- ``add``  —— **reconcile 路径**：System1 同步写，召回候选 → LLM 四操作决策 +
  铁律兜底 → 执行 ops。classifier / chat 记忆抽取这类「新事实 vs 旧记忆
  要调和」的站点走这条。
- ``apply_ops`` / ``add_direct`` —— **确定性路径**：不调 LLM，直接落已确定的 op。
  intent 投影、reducer 行、schema miner 原地 supersede 这类
  「写什么早已确定」的站点走这条（纲领不变式三：确定性写入不许塞进 LLM 决策）。
- ``commit_node`` / ``commit_supersede`` / ``commit_retire`` —— **反转写口
  （PR-6b）**：接收 caller 已按共享映射（``backfill.map_entry_to_node``）备好的
  ``MemoryNode``，过 ``_validated_file_name`` 栅栏（event 硬拒，Q2）后单事务原子
  落 evo_nodes。``write_authority="evomem"`` 时 ``evomem/inversion.py`` 把九个
  写站点收敛到的三条写口动词（append/supersede/delete）经此落真相——op 决策已
  在站点侧完成（chat 抽取 / classifier / miner 各自的 LLM 或确定性逻辑），
  engine 不再调 reconciler 重新决策，保证「同输入 → 投影 byte-identical」的
  迁移纪律；reconcile 调和（``add``）作为语义升级与写权反转解耦，留待后续
  按站点显式启用。
- ``search`` —— 召回 + 演化链折叠，返回链头代表 + ``evolution_chain``。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from ..store import files as files_mod
from . import vector_recall
from .chain import expand_evolution_chains
from .models import MemoryLayer, MemoryNode, ReconcileAction, ReconcileOp
from .reconciler import Reconciler
from .store import NodeStore

# add() 召回候选时，除子串命中外再兜底纳入的活跃链头数量上限（MVP，小库够用）。
_CANDIDATE_FALLBACK_LIMIT = 20

# 召回分词：抓连续 ASCII 词，CJK 按单字。store.search 是整串 LIKE，跨措辞召回靠
# 在 engine 层按 token 拆开后并集（MVP，不引向量；够端到端验证）。
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[一-鿿]")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text or "")


def _new_id(now: datetime) -> str:
    """``make_id`` 形态的 node_id（``YYYYMMDD-HHMM-6hex``，§1.2）。

    与 entry_id 共用同一生成器，三套 id 空间合一（backfill 已用
    ``node_id = entry_id``；engine 直写节点的 id 也落同一形态，markdown 投影
    heading 不需要双 id）。时间部分取本地时区分钟，与 ``store/entries.py``
    的 ``_now_iso_minute`` 同口径。
    """
    # 延迟导入：store/entries.py 顶层 import evomem（integrity/shadow），模块级
    # 反向 import 会在「先 import store.entries」的加载顺序下成环。
    from ..store.entries import make_id

    return make_id(now.astimezone().strftime("%Y-%m-%dT%H:%M"))


def _now() -> datetime:
    return datetime.now(UTC)


def _validated_file_name(file_name: str) -> str:
    """写口 ``file_name`` 路由校验（§1.2/§1.3）。

    - 空串 = 未路由（兼容既有调用方；markdown 投影器对空 ``file_name`` 节点跳过
      并计数）。
    - 非空时归一 ``.md`` 后缀并过 ``VALID_PREFIXES`` 校验（校验落在写口，与
      markdown 写口的 ``validate_prefix`` 同一把尺）。
    - **caller-stage event 栅栏**（Q2 裁定，自 legacy 适配层
      ``writer/reconcile_apply.py``（已于 PR-6b 删除）遗产移交）：``event-*`` 行为日志量大、append-only、永不入链，**不进 evo_nodes**
      ——event 写入留在旧直写口，由 caller 层负责不路由到 engine；engine 在落库
      入口再硬拒一道，防止 PR-6b 迁移期某个站点误把 event 流量灌进真相表（会同时
      打破 integrity 投影对账与 backfill 幂等的 event 豁免口径）。
    """
    if not file_name:
        return ""
    name = file_name if file_name.endswith(".md") else f"{file_name}.md"
    prefix = files_mod.validate_prefix(name)
    if prefix == "event":
        raise ValueError("event-* entries are exempt from evo_nodes; keep them on markdown")
    return name


class EvoMemory:
    def __init__(
        self,
        *,
        user_id: str = "default",
        agent_id: str = "default",
        reconciler: Reconciler | None = None,
        store: NodeStore | None = None,
        cfg: object | None = None,
    ) -> None:
        # reconciler 可为 None（PR-6b）：反转写口 / 确定性入口不需要 LLM 决策方；
        # 只有 ``add``（reconcile 路径）要求它，调用时缺失则显式报错。
        self.user_id = user_id
        self.agent_id = agent_id
        self._reconciler = reconciler
        self._store = store or NodeStore(user_id=user_id, agent_id=agent_id)
        # 向量召回开关源（默认关 → 行为与改动前逐字节一致）。``None`` 时惰性读全局
        # config；测试可直接注入 ``SimpleNamespace(evomem_vector_recall_enabled=True)``。
        self._cfg = cfg

    @property
    def store(self) -> NodeStore:
        return self._store

    # -- System1: 写 -----------------------------------------------------

    def add(
        self,
        text: str,
        *,
        layer: MemoryLayer = MemoryLayer.L2_FACT,
        file_name: str = "",
        tags: str = "",
    ) -> list[str]:
        """reconcile 路径：召回候选 → LLM 四操作决策 → 执行 ops，返回新产生的 node_id 列表。

        ``file_name``/``tags`` 是 §1.2 的投影路由与语义 tag，写在本次产生的每个新
        节点上；``file_name`` 过 :func:`_validated_file_name`（含 event 栅栏）。
        """
        if self._reconciler is None:
            raise RuntimeError(
                "EvoMemory.add() requires a Reconciler (reconcile path);"
                " deterministic writes should use apply_ops/add_direct/commit_*"
            )
        file_name = _validated_file_name(file_name)
        candidates = self._gather_candidates(text)
        result = self._reconciler.reconcile([text], candidates)
        return self._run_ops(result.ops, layer=layer, file_name=file_name, tags=tags)

    def apply_ops(
        self,
        ops: list[ReconcileOp],
        *,
        layer: MemoryLayer = MemoryLayer.L2_FACT,
        file_name: str = "",
        tags: str = "",
    ) -> list[str]:
        """确定性写入口（§1.3）：不调 LLM，按序执行**已经确定**的 op 列表。

        intent 投影（append-only 永不入链）、reducer 行、schema
        miner 的原地 supersede 这类「写什么早已确定」的写需求走这条——把它们塞进
        ``add`` 的 reconcile 决策违反纲领不变式三（确定性优先）。op 形态与
        reconcile 路径完全同构（同一 ``_apply_op``），只是决策方从 LLM 换成 caller。

        注意 caller-stage event 栅栏（Q2，见 :func:`_validated_file_name`）：
        ``event-*`` 永不经本入口落 evo_nodes。

        返回新建/演化产生的 node_id 列表（DELETE 不产新节点）。
        """
        file_name = _validated_file_name(file_name)
        return self._run_ops(ops, layer=layer, file_name=file_name, tags=tags)

    def add_direct(
        self,
        content: str,
        *,
        layer: MemoryLayer = MemoryLayer.L2_FACT,
        file_name: str = "",
        tags: str = "",
    ) -> str:
        """确定性单条 ADD（:meth:`apply_ops` 的最常用形态），返回新节点 id。"""
        op = ReconcileOp(action=ReconcileAction.ADD, content=content, layer=layer)
        return self.apply_ops([op], layer=layer, file_name=file_name, tags=tags)[0]

    # -- 反转写口（PR-6b，写权反转）---------------------------------------
    #
    # ``add``/``apply_ops`` 自带节点构造（``_make_node``），但九个现行写站点的
    # 写形态携带 op 词汇表表达不了的全部 SSOT 字段（元认知三件套 / temporal /
    # refined_from / schema 四元组），且其值必须与 backfill/shadow 的共享映射
    # 逐字段一致（增量真相 == 等价 backfill 的不变式延续）。所以反转写口接收
    # caller（``evomem/inversion.py``）经 ``backfill.map_entry_to_node`` 备好的
    # 完整节点，engine 只负责：写口栅栏（``_validated_file_name``，event 硬拒）
    # + 单事务原子真相写。

    def commit_node(self, node: MemoryNode) -> str:
        """ADD 形态的反转写口：栅栏校验后落一个新链头节点。"""
        node.file_name = _validated_file_name(node.file_name)
        self._store.save(node)
        return node.node_id

    def commit_supersede(
        self, node: MemoryNode, *, old_id: str, old_valid_until: str | None = None
    ) -> str:
        """SUPERSEDE 形态的反转写口：原子落新链头 + 退役旧节点（含双向指针）。

        ``old_valid_until``：旧节点的退役时刻（COALESCE 落，幂等）。旧节点缺失
        时 ``NodeStore.save_and_supersede`` 抛 ``KeyError``——存在性检查由
        caller 先行（与 markdown 写口的 ``ValueError`` 错误面对齐）。
        """
        node.file_name = _validated_file_name(node.file_name)
        self._store.save_and_supersede(node, old_id=old_id, old_valid_until=old_valid_until)
        return node.node_id

    def commit_retire(self, node_id: str, *, valid_until: str | None = None) -> None:
        """DELETE 形态的反转写口：孤儿退役（shadow 无后继）+ COALESCE 退役时刻。"""
        self._store.shadow(node_id, valid_until=valid_until)

    def _run_ops(
        self, ops: list[ReconcileOp], *, layer: MemoryLayer, file_name: str, tags: str
    ) -> list[str]:
        new_ids: list[str] = []
        for op in ops:
            new_id = self._apply_op(op, layer=layer, file_name=file_name, tags=tags)
            if new_id is not None:
                new_ids.append(new_id)
        return new_ids

    def _gather_candidates(self, text: str) -> list[MemoryNode]:
        """token 重叠命中的活跃链头 + 兜底纳入近期活跃链头（去重保序）。

        MVP 的 LIKE 子串检索常常无法跨措辞命中（"现在喝茶" 不含于 "喝咖啡"），
        故再并入 ``all_latest()`` 的近期链头作为候选，让 reconciler 有机会判定矛盾。
        """
        seen: set[str] = set()
        candidates: list[MemoryNode] = []
        for hit in self._recall(text, top_k=_CANDIDATE_FALLBACK_LIMIT):
            node = hit["node"]
            if node.node_id not in seen:
                seen.add(node.node_id)
                candidates.append(node)
        for node in self._store.all_latest()[:_CANDIDATE_FALLBACK_LIMIT]:
            if node.node_id not in seen:
                seen.add(node.node_id)
                candidates.append(node)
        return candidates

    def _apply_op(
        self, op: ReconcileOp, *, layer: MemoryLayer, file_name: str = "", tags: str = ""
    ) -> str | None:
        """按 teardown §4 执行单条 op；返回新节点 id（DELETE 无新节点返回 None）。"""
        op_layer = op.layer or layer
        if op.action is ReconcileAction.ADD:
            return self._save_head(op.content, op_layer, file_name=file_name, tags=tags)

        if op.action is ReconcileAction.SUPERSEDE and op.target_id is not None:
            # 落新链头 + shadow 旧节点必须原子，否则崩在两步之间会留下两个活跃
            # 链头，破坏「每条演化链唯一活跃链头」不变量（issue #427）。
            node = self._make_node(
                op.content, op_layer, supersedes=[op.target_id], file_name=file_name, tags=tags
            )
            self._store.save_and_supersede(node, old_id=op.target_id)
            return node.node_id

        if op.action is ReconcileAction.UPDATE and op.target_id is not None:
            # 同向精炼：新节点独立成头，旧节点 shadow（不进演化链，``enters_chain()``
            # 保持 SUPERSEDE-only）。出处补洞（审计 3.1，§1.3 裁定）：新节点记
            # ``refined_from = 旧节点 id``——engine 旧状只 shadow 不记出处，投影/trail
            # 因此无法渲染「← [精炼自]」。落新 + shadow 旧必须原子，否则崩在两步
            # 之间会留下两个活跃链头（issue #448，同 #427 SUPERSEDE）。
            node = self._make_node(
                op.content, op_layer, file_name=file_name, tags=tags, refined_from=op.target_id
            )
            self._store.save_and_shadow(node, old_id=op.target_id)
            return node.node_id

        if op.action is ReconcileAction.DELETE and op.target_id is not None:
            self._store.shadow(op.target_id)
            return None

        if op.action is ReconcileAction.ABSTRACT and op.source_ids:
            # WRITE-02 N→1 合成：新建合成链头并原子收编(shadow)所有 source_ids，否则
            # 落到下面兜底 ADD = 纯新增，N 个源节点不退出活跃链头，N→1 收敛退化成 N+1
            # 并存（issue #416）。原子性同 #427：避免崩在「建新 + 逐个 shadow」中间留下
            # 多活跃链头。
            #
            # 链语义②（legacy 适配层 reconcile_apply（已删）遗产移交，SSOT 切换 §1.3）：多源出处是正交
            # provenance 边——记在合成节点的 ``abstracted_from`` JSON 列——**不是**
            # 线性演化链；源节点走 retire（shadow），不写 ``superseded_by`` 单指针
            # （N 源指一后继会撞链 back-map 的单前驱模型）。与 backfill/markdown 侧
            # 的 ``#abstracted-from`` 多值 tag + 逐源 strike 形态一致。
            node = self._make_node(
                op.content,
                op_layer,
                file_name=file_name,
                tags=tags,
                abstracted_from=list(op.source_ids),
            )
            self._store.save_and_retire_sources(node, source_ids=op.source_ids)
            return node.node_id

        # 兜底：铁律已把非法 targeted op 降级为 ADD，这里只剩异常形态，按 ADD 落地。
        return self._save_head(op.content, op_layer, file_name=file_name, tags=tags)

    def _make_node(
        self,
        content: str,
        layer: MemoryLayer,
        *,
        supersedes: list[str] | None = None,
        file_name: str = "",
        tags: str = "",
        refined_from: str | None = None,
        abstracted_from: list[str] | None = None,
        schema_summary: str | None = None,
        schema_inferences: list[str] | None = None,
        schema_confidence: float | None = None,
    ) -> MemoryNode:
        now = _now()
        return MemoryNode(
            node_id=_new_id(now),
            content=content,
            layer=layer,
            supersedes=list(supersedes or []),
            is_latest=True,
            memory_at=now,
            gmt_created=now,
            user_id=self.user_id,
            agent_id=self.agent_id,
            file_name=file_name,
            tags=tags,
            refined_from=refined_from,
            abstracted_from=list(abstracted_from or []),
            schema_summary=schema_summary,
            schema_inferences=schema_inferences,
            schema_confidence=schema_confidence,
        )

    def _save_head(
        self,
        content: str,
        layer: MemoryLayer,
        *,
        supersedes: list[str] | None = None,
        file_name: str = "",
        tags: str = "",
        schema_summary: str | None = None,
        schema_inferences: list[str] | None = None,
        schema_confidence: float | None = None,
    ) -> str:
        node = self._make_node(
            content,
            layer,
            supersedes=supersedes,
            file_name=file_name,
            tags=tags,
            schema_summary=schema_summary,
            schema_inferences=schema_inferences,
            schema_confidence=schema_confidence,
        )
        self._store.save(node)
        return node.node_id

    # -- 召回 ------------------------------------------------------------

    def search(self, query: str, top_k: int = 10) -> list[dict]:
        """召回 + 演化链折叠。链上命中回溯到链头并附 ``evolution_chain``。"""
        hits = self._recall(query, top_k=top_k)
        return expand_evolution_chains(self._store.get_by_ids, hits)

    def _resolve_cfg(self) -> object:
        """返回向量召回开关源：注入的 ``cfg`` 优先；否则惰性读全局 config。

        惰性 + 失败兜底（任何加载异常 → 一个 falsy 占位，开关判定为关）：召回不该因
        config 读不到而崩——回退纯 LIKE 是安全默认。
        """
        if self._cfg is not None:
            return self._cfg
        try:
            from .. import config as config_mod

            return config_mod.load()
        except Exception:  # noqa: BLE001 — fail-safe: 读不到 config 即按开关关处理
            return object()

    def _lexical_recall(self, query: str, *, top_k: int) -> list[dict]:
        """整串 + 逐 token 的 LIKE 并集召回，同节点保留最高 score（子串路径，原 ``_recall``）。"""
        best: dict[str, dict] = {}
        terms = [query.strip(), *_tokenize(query)]
        for term in terms:
            if not term:
                continue
            for hit in self._store.search(term, top_k=top_k):
                nid = hit["node_id"]
                if nid not in best or hit["score"] > best[nid]["score"]:
                    best[nid] = hit
        ranked = sorted(best.values(), key=lambda h: h["score"], reverse=True)
        return ranked[:top_k]

    def _recall(self, query: str, *, top_k: int) -> list[dict]:
        """召回入口：LIKE 子串路径 ⊕（开关开时）向量召回 RRF 融合。

        开关默认关 / 模型不可用 / 冷启动 → ``vector_recall.fuse`` 原样返回 LIKE 命中，
        与改动前逐字节一致。开关开 + 模型可用时，对活跃链头做向量召回并 RRF 融合，
        让跨措辞的同义召回浮出（LIKE 漏的 paraphrase）。
        """
        lexical_hits = self._lexical_recall(query, top_k=top_k)
        return vector_recall.fuse(
            query,
            lexical_hits,
            top_k=top_k,
            cfg=self._resolve_cfg(),
            candidates_provider=self._store.all_latest,
        )

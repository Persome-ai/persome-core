"""他人关系图（person relationship graph）—— evomem 之上的"屏幕上出现过的其他人"目录。

evomem 的 ``L4_IDENTITY`` 建模的是**用户自己**；本模块在 ``L5_KNOWLEDGE`` 层补一张
**他人**目录：从已识别数据里的人名建实体节点，每人携带别名、类别（category）、以及一
条 append-only 的交互事件时间线，并产出一个 ``build_person_context`` 文本块供上层注入
prompt（上层负责包进 untrusted fence —— 本层只产纯数据/纯文本，不拼信任指令）。

设计要点（与 evomem 铁律对齐）：

- **写入只走 evomem 公共写入口**（``EvoMemory.add_direct``，确定性路径，无 LLM）——
  实体与事件都作为 evo_nodes 节点存在，不绕过演化链、不分叉、不悬空。
- **实体节点（entity）**：每人恰好一个活跃链头，``content`` 是规范名（canonical
  name），别名/类别/交互计数侧载在 ``tags`` + ``schema_summary`` JSON 里。同一个人不
  同别名/写法第二次到来时，**SUPERSEDE 旧实体头**（``add`` reconcile 不适用——写什么
  早已确定，走确定性 SUPERSEDE op），别名并集进新头，**不新建重复实体**。
- **事件节点（event）**：每次交互一条 append-only 节点（永不 supersede，永不入链），
  ``file_name`` 路由到该人的 ``person-<slug>.md``，``occurred_at`` 落交互时刻。时间线 =
  该人名下的全部事件节点按 ``occurred_at`` 排序。
- **名字来源是可注入 seam**（``PersonNameSource``）：生产调用方使用
  ``model.entity_source.MemoryPersonNameSource`` 从 durable person facts 和
  event entries 读取；测试可注入假数据。
- **隐私保守**：人名是从屏幕推断、非用户录入。只见一次（``sightings == 1``）且无 category
  的人按"暂存、不上提"处理 —— ``list_persons(min_sightings=...)`` 可滤掉，
  ``build_person_context`` 仍返回其已知的少量交互（不报错）。
- **开关默认 off**：``getattr(cfg, "person_graph_enabled", False)`` 兜底；关时所有
  写操作 no-op（不建图）。
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from ..logger import get
from .engine import EvoMemory
from .models import MemoryLayer, MemoryNode, ReconcileAction, ReconcileOp

_log = get("persome.evomem.person_graph")

# 实体/事件节点在 tags 里的判别标记（空格分隔 tag，与 evomem 既有 tags 口径一致）。
_TAG_ENTITY = "person-entity"
_TAG_EVENT = "person-event"

# 实体节点把别名/类别/交互计数侧载进 schema_summary（一个 JSON blob）——复用既有列，
# 不动 models/schema。事件节点同样用 schema_summary 携带它归属的规范名（canonical），
# 便于无 file_name 依赖地把事件归回某人。
_META_CANONICAL = "canonical"
_META_ALIASES = "aliases"
_META_CATEGORY = "category"
_META_SIGHTINGS = "sightings"


def _now() -> datetime:
    return datetime.now(UTC)


def _norm(name: str) -> str:
    """规范化人名用于"同一个人"判定：NFKC + 折叠空白 + 去首尾 + casefold。

    中文姓名无大小写，casefold 是兜底英文别名（"Alice"/"alice"）；全角→半角靠 NFKC。
    返回空串表示该名无效（空/纯空白）——caller 跳过。
    """
    folded = unicodedata.normalize("NFKC", name or "").strip()
    folded = " ".join(folded.split())
    return folded.casefold()


def _slug(canonical: str) -> str:
    """规范名 → ``person-<slug>.md`` 的 slug（保守：仅保留字母数字 + CJK，其余转 -）。

    file_name 仅作 markdown 投影路由；slug 冲突不影响实体身份（身份由 schema_summary
    的 canonical 判定），所以这里只要稳定、合法即可。
    """
    out: list[str] = []
    for ch in unicodedata.normalize("NFKC", canonical or "").strip():
        if ch.isalnum():
            out.append(ch.lower())
        elif out and out[-1] != "-":
            out.append("-")
    slug = "".join(out).strip("-")
    return slug or "unknown"


@dataclass
class PersonEvent:
    """一条"屏幕上出现过的某人"交互事件（seam 产出的最小单元）。

    - ``name``：本次出现使用的人名写法（可能是别名）。
    - ``summary``：这次交互的一句话描述（什么场景/做了什么）；空则用默认措辞。
    - ``occurred_at``：交互时刻；缺省取 now。
    - ``category``：可选的人物类别（如 "colleague"/"client"）——非用户录入，保守可空。
    - ``aliases``：本次额外带出的别名（如同一行里既有 "Alice" 又有 "爱丽丝"）。
    - ``confidence``：来源置信度（0-1）；低置信只见一次的人按保守策略不上提。
    """

    name: str
    summary: str = ""
    occurred_at: datetime | None = None
    category: str | None = None
    aliases: Sequence[str] = field(default_factory=tuple)
    confidence: float = 1.0


@dataclass
class PersonEntity:
    """一个他人实体（实体链头节点的领域视图）。"""

    node_id: str
    canonical: str  # 规范名（显示用，原始大小写/写法）
    aliases: list[str]
    category: str | None
    sightings: int
    last_seen: datetime | None

    @property
    def seen_once(self) -> bool:
        """只见过一次 —— 隐私上"暂存、不过度上提"的判别位。"""
        return self.sightings <= 1


class PersonNameSource(Protocol):
    """名字来源 seam：产出待入图的 :class:`PersonEvent` 列表。

    Implementations only read durable model inputs and never trigger capture or
    classification.
    """

    def events(self) -> list[PersonEvent]: ...


class EmptyPersonNameSource:
    """默认空源：不确定来源时的安全兜底（不建任何图）。"""

    def events(self) -> list[PersonEvent]:  # noqa: D102
        return []


def _parse_ts(value: object) -> datetime | None:
    """Parse an ISO timestamp, ALWAYS returning an aware datetime (naive → UTC).

    Real stores mix naive and aware strings (minute-granularity legacy rows vs
    tz-suffixed ones); a mixed list makes ``sort`` raise TypeError — which the
    relation extractor's fail-safe then swallows into "0 people, 0 edges".
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _meta_of(node: MemoryNode) -> dict:
    """读实体节点侧载的别名/类别/计数 JSON（容错空/坏）。"""
    try:
        data = json.loads(node.schema_summary or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


class PersonGraph:
    """他人关系图 —— evomem 之上的薄层（写只走 ``add_direct`` 确定性入口）。

    ``cfg`` 决定开关：``getattr(cfg, "person_graph_enabled", False)``，默认 off。关时
    所有写操作（``ingest`` / ``record``）是 no-op；读操作正常返回（已有数据照查）。

    ``min_confidence``：低于此置信度且只见一次的人不进图（隐私保守 —— 低置信单次出现
    很可能是误识别的屏幕文本）。已多次出现的人不受此限（重复出现是真实信号）。
    """

    def __init__(
        self,
        memory: EvoMemory,
        *,
        cfg: object | None = None,
        name_source: PersonNameSource | None = None,
        min_confidence: float = 0.6,
    ) -> None:
        self._mem = memory
        self._cfg = cfg
        self._source = name_source or EmptyPersonNameSource()
        self._min_confidence = min_confidence

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._cfg, "person_graph_enabled", False))

    # -- 写 --------------------------------------------------------------

    def ingest(self) -> list[str]:
        """从注入的 name source 拉取事件并入图；返回受影响（新建/演化）的实体规范名列表。

        开关 off → no-op（返回空）。隐私门：低置信(`< min_confidence`)且该人此前从未见过
        的事件被跳过（避免把误识别的屏幕文本上提成实体）。
        """
        if not self.enabled:
            return []
        touched: list[str] = []
        for event in self._source.events():
            canonical = self.record(event)
            if canonical is not None:
                touched.append(canonical)
        return touched

    def record(self, event: PersonEvent) -> str | None:
        """记录单条交互事件：找到/新建该人实体（别名合并）+ append 一条事件节点。

        返回该人的规范名（canonical）；开关 off / 名字无效 / 被隐私门挡下 → None。
        别名合并经 evomem SUPERSEDE（确定性 op），保证同一个人始终唯一活跃实体头。
        """
        if not self.enabled:
            return None
        norm = _norm(event.name)
        if not norm:
            return None

        existing = self._find_entity(norm, event.aliases)
        # 隐私保守：低置信 + 该人此前从未见过 → 不上提（不建实体、不落事件）。
        if existing is None and event.confidence < self._min_confidence:
            _log.debug("person_graph: skip low-confidence first sighting %r", event.name)
            return None

        if existing is None:
            canonical = event.name.strip()
            entity = self._create_entity(event, canonical)
        else:
            canonical = existing.canonical
            entity = self._merge_entity(existing, event)

        self._append_event(entity_canonical=entity.canonical, event=event)
        return entity.canonical

    def _create_entity(self, event: PersonEvent, canonical: str) -> PersonEntity:
        aliases = _dedup_aliases([canonical, *event.aliases])
        meta = {
            _META_CANONICAL: canonical,
            _META_ALIASES: aliases,
            _META_CATEGORY: event.category,
            _META_SIGHTINGS: 1,
        }
        nid = self._mem.add_direct(
            canonical,
            layer=MemoryLayer.L5_KNOWLEDGE,
            file_name=f"person-{_slug(canonical)}",
            tags=_TAG_ENTITY,
        )
        # add_direct 不接 schema_summary，故合并后通过一次 SUPERSEDE 把 meta 落上去——
        # 为保持"唯一活跃头 + 走公共写口"，新建直接补一次确定性 supersede 落 meta。
        nid = self._supersede_entity(nid, canonical, meta)
        return PersonEntity(
            node_id=nid,
            canonical=canonical,
            aliases=aliases,
            category=event.category,
            sightings=1,
            last_seen=event.occurred_at or _now(),
        )

    def _merge_entity(self, existing: PersonEntity, event: PersonEvent) -> PersonEntity:
        """别名/类别并集 + 计数 +1，经 SUPERSEDE 落新实体头（不分叉）。"""
        aliases = _dedup_aliases([*existing.aliases, event.name, *event.aliases])
        category = existing.category or event.category
        sightings = existing.sightings + 1
        meta = {
            _META_CANONICAL: existing.canonical,
            _META_ALIASES: aliases,
            _META_CATEGORY: category,
            _META_SIGHTINGS: sightings,
        }
        nid = self._supersede_entity(existing.node_id, existing.canonical, meta)
        return PersonEntity(
            node_id=nid,
            canonical=existing.canonical,
            aliases=aliases,
            category=category,
            sightings=sightings,
            last_seen=event.occurred_at or _now(),
        )

    def _supersede_entity(self, old_id: str, canonical: str, meta: dict) -> str:
        """走 evomem 确定性 SUPERSEDE op 落带 meta 的新实体头，退役旧头。

        通过 ``apply_ops`` 走公共写口（``_apply_op`` 的 SUPERSEDE 分支 →
        ``save_and_supersede`` 原子落新 + shadow 旧），不绕过演化链。meta 经
        ``schema_summary`` 列承载（_make_node 透传）——但 ``apply_ops`` 不暴露
        schema_summary 形参，故这里直接用 engine 的反转写口 ``commit_supersede``
        构造带 schema_summary 的节点，仍是 engine 公共写入口、单事务原子。
        """
        from .engine import _new_id

        node = MemoryNode(
            node_id=_new_id(_now()),
            content=canonical,
            layer=MemoryLayer.L5_KNOWLEDGE,
            supersedes=[old_id],
            is_latest=True,
            memory_at=_now(),
            gmt_created=_now(),
            user_id=self._mem.user_id,
            agent_id=self._mem.agent_id,
            file_name=f"person-{_slug(canonical)}.md",
            tags=_TAG_ENTITY,
            schema_summary=json.dumps(meta, ensure_ascii=False),
        )
        return self._mem.commit_supersede(node, old_id=old_id)

    def _append_event(self, *, entity_canonical: str, event: PersonEvent) -> str:
        """append-only 落一条事件节点（永不 supersede/入链）。

        ``occurred_at`` 落交互时刻；``schema_summary`` 携带它归属的 canonical，便于无
        file_name 依赖地把事件归回某人。走 ``add_direct`` 公共写口（确定性 ADD）。
        """
        op = ReconcileOp(
            action=ReconcileAction.ADD,
            content=event.summary or f"与 {entity_canonical} 的一次交互",
            layer=MemoryLayer.L5_KNOWLEDGE,
        )
        # add_direct 不接 occurred_at/schema_summary，用 apply_ops 也不接——所以事件节点
        # 同样用 engine 反转写口 commit_node 落（带 occurred_at + canonical 归属）。
        from .engine import _new_id

        now = _now()
        node = MemoryNode(
            node_id=_new_id(now),
            content=op.content,
            layer=MemoryLayer.L5_KNOWLEDGE,
            is_latest=True,
            memory_at=event.occurred_at or now,
            gmt_created=now,
            user_id=self._mem.user_id,
            agent_id=self._mem.agent_id,
            file_name=f"person-{_slug(entity_canonical)}.md",
            tags=_TAG_EVENT,
            occurred_at=(event.occurred_at or now).isoformat(),
            schema_summary=json.dumps({_META_CANONICAL: entity_canonical}, ensure_ascii=False),
        )
        return self._mem.commit_node(node)

    # -- 读 --------------------------------------------------------------

    def _entity_nodes(self) -> list[MemoryNode]:
        # §1.2 维度判据: the person roster is the person- prefixed entities —
        # the file taxonomy IS the kind axis's SSOT, so an adjudicated retype
        # (person-研发群.md → org-研发群.md) drops the entity out of the person
        # roster (and out of knows-edge extraction) by construction.
        return [
            n
            for n in self._mem.store.all_latest()
            if _TAG_ENTITY in (n.tags or "").split() and (n.file_name or "").startswith("person-")
        ]

    def _find_entity(self, norm_name: str, extra_aliases: Iterable[str]) -> PersonEntity | None:
        """按规范化名 + 别名集匹配既有活跃实体头。"""
        wanted = {norm_name, *(_norm(a) for a in extra_aliases)}
        wanted.discard("")
        for node in self._entity_nodes():
            meta = _meta_of(node)
            cand_canonical = _norm(meta.get(_META_CANONICAL, node.content))
            known = {_norm(a) for a in meta.get(_META_ALIASES, [])}
            known.add(cand_canonical)
            known.discard("")
            # 需"规范名一侧吻合",而非仅共享一个泛化别名:两个不同的人恰好共用名字
            # 别名("Alex Chen" vs "Alex Wong",都别名 "Alex")彼此的 canonical 都不在
            # 对方集合里 → 不合并;而同一个人的真别名("Alex" 后续出现 "Alex Chen" 且
            # 带别名 "Alex")仍会折叠,因为短 canonical "alex" 在新记录的 wanted 里。
            if norm_name in known or cand_canonical in wanted:
                return self._to_entity(node, meta)
        return None

    def _to_entity(self, node: MemoryNode, meta: dict | None = None) -> PersonEntity:
        meta = meta if meta is not None else _meta_of(node)
        return PersonEntity(
            node_id=node.node_id,
            canonical=meta.get(_META_CANONICAL) or node.content,
            aliases=_dedup_aliases(meta.get(_META_ALIASES, []) or [node.content]),
            category=meta.get(_META_CATEGORY),
            sightings=int(meta.get(_META_SIGHTINGS, 1) or 1),
            last_seen=node.memory_at or node.gmt_created,
        )

    def list_persons(self, *, min_sightings: int = 1) -> list[PersonEntity]:
        """列出已知他人实体（按 last_seen 新→旧）。

        ``min_sightings``：隐私/降噪门 —— 默认 1（全列）；传 2 滤掉只见一次的人。
        """
        people = [self._to_entity(n) for n in self._entity_nodes()]
        people = [p for p in people if p.sightings >= min_sightings]
        people.sort(key=lambda p: p.last_seen or datetime.min.replace(tzinfo=UTC), reverse=True)
        return people

    def person_timeline(self, name: str) -> list[MemoryNode]:
        """某人名下的全部交互事件节点，按 ``occurred_at`` 旧→新。

        ``name`` 可为规范名或任一别名；匹配不到 → 空列表（不抛）。
        """
        norm = _norm(name)
        if not norm:
            return []
        entity = self._find_entity(norm, [])
        if entity is None:
            return []
        canonical_norm = _norm(entity.canonical)
        events: list[MemoryNode] = []
        for node in self._mem.store.all_latest():
            if _TAG_EVENT not in (node.tags or "").split():
                continue
            meta = _meta_of(node)
            if _norm(meta.get(_META_CANONICAL, "")) == canonical_norm:
                events.append(node)
        events.sort(
            key=lambda n: (
                _parse_ts(n.occurred_at)
                or _aware(n.memory_at)
                or _aware(n.gmt_created)
                or datetime.min.replace(tzinfo=UTC)
            )
        )
        return events

    def build_person_context(self, name: str, *, max_events: int = 8) -> str:
        """产出某人交互时间线的摘要块（纯文本，供 prompt 注入）。

        **本层只产数据/纯文本，不拼信任指令** —— 上层负责把它包进 untrusted fence。
        措辞 app 无关。匹配不到该人 → 返回空串。

        块结构（每行一条，最近交互在前）::

            张三（colleague，别名：Zhang San；共 3 次交互）
            - 2026-06-20 14:30 在一次 meeting 场景
            - 2026-06-18 09:10 ...
        """
        norm = _norm(name)
        if not norm:
            return ""
        entity = self._find_entity(norm, [])
        if entity is None:
            return ""
        timeline = self.person_timeline(entity.canonical)

        descr: list[str] = []
        if entity.category:
            descr.append(entity.category)
        other_aliases = [a for a in entity.aliases if _norm(a) != _norm(entity.canonical)]
        if other_aliases:
            descr.append("别名：" + "、".join(other_aliases))
        descr.append(f"共 {entity.sightings} 次交互")
        header = entity.canonical + "（" + "，".join(descr) + "）"

        lines = [header]
        for node in reversed(timeline[-max_events:]):  # 最近在前
            when = _parse_ts(node.occurred_at) or node.memory_at or node.gmt_created
            stamp = when.strftime("%Y-%m-%d %H:%M") if when else "时间未知"
            summary = (node.content or "").strip() or "一次交互"
            lines.append(f"- {stamp} {summary}")
        if not timeline:
            lines.append("- （暂无已记录的交互细节）")
        return "\n".join(lines)


def _dedup_aliases(aliases: Iterable[str]) -> list[str]:
    """保序去重别名（按规范化键去重，保留首次出现的原始写法）。"""
    seen: set[str] = set()
    out: list[str] = []
    for a in aliases:
        text = (a or "").strip()
        key = _norm(text)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out

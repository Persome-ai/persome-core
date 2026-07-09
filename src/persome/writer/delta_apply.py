"""§4.2 确定性 apply —— 把 gated memory_delta 铸成真实点/边（零 LLM）。

memory_delta（§4.1）读全场多头提取 → gate_delta 规范化+校验 → 本模块把 clean
`{entities, relations, events}` 确定性落成图：

- **entities → 点**（kind-aware：person/org/project/artifact → `person-/org-/project-/tool-`
  file 前缀）。这是「点层稀」的直接修复——attention 式提取器读全场原文，比 classifier 的
  保守摘要分类捞出多得多的实体。`ended` → 回填点的 `valid_until`（§4.6 实体侧）。
- **relations → 边**（复用 `relation_extractor` 的 `_open_edges`/`_upsert_shadow`/`_edge_key`——
  单一实现，无分叉）。`ended` → `close_edge`（**§4.6 leg-a：delta 结束信号收口 valid_to**）。
- **events → Activity 点 + participates_in 边**（event:<hash> 终态点）。

纪律：判断已在 §4.1（LLM）+ §4.3（gate 漏斗）做完；本层零判断、纯确定性落库、幂等
（重跑同 delta → add_direct append 一条 md、边 reinforce no-op）、fail-open（任一 apply 失败
只记日志，绝不扰动 session 末链）。默认 OFF（`memory_delta.apply_enabled`）。
"""

from __future__ import annotations

import contextlib
import hashlib
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..evomem import relation_extractor as rex
from ..evomem.engine import EvoMemory
from ..evomem.models import MemoryLayer
from ..evomem.person_graph import _slug as _entity_slug
from ..logger import get
from ..store import entries as entries_store
from ..store import relation_edges as edges_store
from ..store.relation_edges import EntityKind, Predicate

logger = get("persome.writer.delta_apply")

SELF_IDENTITY = "self"
EVENT_PREFIX = "event:"

# kind → markdown file 前缀（与 dev_memory_graph 的 typed-point 反射一致：tool-*→artifact）
_KIND_PREFIX = {"person": "person", "org": "org", "project": "project", "artifact": "tool"}
_KIND_ENUM = {
    "person": EntityKind.PERSON,
    "org": EntityKind.ORG,
    "project": EntityKind.PROJECT,
    "artifact": EntityKind.ARTIFACT,
    "self": EntityKind.SELF,
    "event": EntityKind.EVENT,
}


@dataclass
class ApplyResult:
    entities_minted: int = 0
    entities_seen: int = 0
    assertions_minted: int = 0  # ② 事实层：新落的事实条目
    assertions_seen: int = 0  # 已存在（幂等跳过）
    edges_new: int = 0
    edges_reinforced: int = 0
    edges_closed: int = 0
    events_minted: int = 0
    floor_edges: int = 0  # ① engaged_with 关联地板边
    supersedes_applied: int = 0  # ⊖ 退役腿：delta 退掉的旧信念数（更新=加∧退）
    skipped_reason: str = ""
    errors: list[str] = field(default_factory=list)


def _canonical_of(who: dict[str, Any] | None) -> str | None:
    """gate 输出的 identity dict → 规范名字符串。self 保持 'self'。"""
    if not isinstance(who, dict):
        return None
    # gate 的 _canonicalize 落 {"ref": canonical} 或 {"new_entity": name}；两者都取值
    return who.get("ref") or who.get("new_entity") or who.get("canonical")


def _entity_kind_map(clean: dict) -> dict[str, str]:
    """canonical → kind，供边端点 kind 查询（entities 头是 kind 的唯一分型生产者，§4.1）。"""
    out: dict[str, str] = {}
    for e in clean.get("entities") or []:
        c = _canonical_of(e)
        if c and e.get("kind") in _KIND_PREFIX:
            out[c] = e["kind"]
    return out


def _find_entity_head(conn: sqlite3.Connection, file_name: str) -> str | None:
    """按 file_name 找当前活跃实体头 node_id；无 → None（该铸新点）。"""
    try:
        row = conn.execute(
            "SELECT node_id FROM evo_nodes WHERE file_name = ? AND is_latest = 1"
            " AND status = 'active' LIMIT 1",
            (file_name,),
        ).fetchone()
        return row[0] if row else None
    except Exception:  # noqa: BLE001 — 缺表/异常 fail-open：当作不存在，铸新点
        return None


def _apply_entities(conn: sqlite3.Connection, mem: EvoMemory, clean: dict, r: ApplyResult) -> None:
    now = datetime.now(UTC).isoformat()
    ended_files: list[str] = []
    for e in clean.get("entities") or []:
        try:
            if not isinstance(e, dict):
                continue
            kind = e.get("kind")
            canonical = _canonical_of(e)
            if not canonical or kind not in _KIND_PREFIX or canonical == SELF_IDENTITY:
                continue
            stem = f"{_KIND_PREFIX[kind]}-{_entity_slug(canonical)}"
            stored = f"{stem}.md"  # 引擎给 add_direct 的 file_name 补 .md，查询须用存储形式
            # 同 delta 内重复实体的去重靠这条重读：conn 是 autocommit（isolation_level=None），
            # add_direct 经引擎自身连接立即提交，故下一实体的 SELECT 看得见刚铸的头 → 不重复
            # （生产 512 实体文件实测 0 重复）。⚠ 若将来把 apply_delta 包进显式事务，conn 会冻结
            # 快照、这条重读失效 → 须改用本次调用内的 minted seen-set（同 _apply_floor 的做法）。
            head = _find_entity_head(conn, stored)
            if head is None:
                # 铸新点——attention 提取器捞出 classifier 漏掉的实体
                mem.add_direct(
                    canonical,
                    layer=MemoryLayer.L5_KNOWLEDGE,
                    file_name=stem,
                    tags="entity",
                )
                r.entities_minted += 1
            else:
                r.entities_seen += 1
            if e.get("ended"):
                ended_files.append(stored)
        except Exception as exc:  # noqa: BLE001 — 单实体失败不拖累其余
            r.errors.append(f"entity: {exc}")
    # ended → 回填 valid_until（§4.6 实体侧）。mint 走 EvoMemory 自己的连接，故先 commit
    # 传入 conn 刷新快照才看得见刚铸的头；only-if-null 幂等。
    if ended_files:
        _stamp_entities_valid_until(conn, ended_files, now)


def _stamp_entities_valid_until(conn: sqlite3.Connection, file_names: list[str], at: str) -> None:
    with contextlib.suppress(Exception):
        conn.commit()  # 结束当前读事务 → 下条 UPDATE 拿到能看见 mint 的新快照
        conn.executemany(
            "UPDATE evo_nodes SET valid_until = ? WHERE file_name = ? AND is_latest = 1"
            " AND valid_until IS NULL",
            [(at, fn) for fn in file_names],
        )
        conn.commit()


def _route_assertion_stem(
    conn: sqlite3.Connection, canonical: str, kinds: dict[str, str]
) -> str | None:
    """assertion 主体 canonical → 其实体文件 stem（person-/org-/project-/tool-slug）。

    优先本 delta 的 entities kind（§4.1 唯一分型生产者）；否则**领养**已存在的实体文件
    （backfill 已铸点，跨会话主体在别的 delta 定过 kind）；都没有 → None（不可路由，保守跳过，
    绝不臆断 kind）。
    """
    slug = _entity_slug(canonical)
    kind = kinds.get(canonical)
    if kind in _KIND_PREFIX:
        return f"{_KIND_PREFIX[kind]}-{slug}"
    for prefix in _KIND_PREFIX.values():
        if _find_entity_head(conn, f"{prefix}-{slug}.md") is not None:
            return f"{prefix}-{slug}"
    return None


def _assertion_exists(conn: sqlite3.Connection, stored: str, text: str) -> bool:
    """精确去重：该事实正文已在文件里 live → 跳过（幂等 replay / 重跑同 delta）。"""
    try:
        row = conn.execute(
            "SELECT 1 FROM evo_nodes WHERE file_name = ? AND content = ? AND is_latest = 1 LIMIT 1",
            (stored, text),
        ).fetchone()
        return row is not None
    except Exception:  # noqa: BLE001 — 缺表 fail-open：当作不存在
        return False


def _apply_assertions(
    conn: sqlite3.Connection, mem: EvoMemory, clean: dict, kinds: dict[str, str], r: ApplyResult
) -> None:
    """assertions → 实体文件里的事实条目（②事实层，schema 的料）。

    每条 `{subject, text, quote, confidence}`：subject 规范化 → 路由到该实体 kind 文件，
    `text` 作**事实条目** append（`add_direct`，tags=``fact`` + confidence，走既有 choke-point
    写口）。此前四头只接三头（entities/relations/events）、assertions 头抽出即弃 → 实体文件
    每个只 1 条点、够不到 schema 的 min_facts=4；补上后 schema 才有料可挖（spec 2026-07-04 §1）。
    判断已在 §4.1 quote-gated；本层零判断、幂等（同文件同 text 不重复）、fail-open。
    """
    for a in clean.get("assertions") or []:
        try:
            if not isinstance(a, dict):
                continue
            text = str(a.get("text") or "").strip()
            canonical = _canonical_of(a.get("subject"))
            if not text or not canonical or canonical == SELF_IDENTITY:
                continue
            stem = _route_assertion_stem(conn, canonical, kinds)
            if stem is None:
                continue  # 主体不可路由 → 保守跳过
            if _assertion_exists(conn, f"{stem}.md", text):
                r.assertions_seen += 1
                continue
            tags = "fact"
            conf = a.get("confidence")
            if isinstance(conf, (int, float)):
                tags += f" confidence:{float(conf):.2f}"
            mem.add_direct(text, layer=MemoryLayer.L5_KNOWLEDGE, file_name=stem, tags=tags)
            r.assertions_minted += 1
        except Exception as exc:  # noqa: BLE001 — 单条失败不拖累其余
            r.errors.append(f"assertion: {exc}")


def _apply_relations(
    conn: sqlite3.Connection, clean: dict, kinds: dict[str, str], r: ApplyResult
) -> None:
    seen = rex._open_edges(conn)  # noqa: SLF001 — 复用单一实现（无分叉，§5）
    tally = rex._Tally()  # noqa: SLF001
    now = datetime.now(UTC).isoformat()
    for rel in clean.get("relations") or []:
        try:
            if not isinstance(rel, dict):
                continue
            src = _canonical_of(rel.get("src"))
            dst = _canonical_of(rel.get("dst"))
            pred_raw = rel.get("predicate")
            if not src or not dst or pred_raw not in {p.value for p in Predicate}:
                continue
            predicate = Predicate(pred_raw)
            src_kind = _endpoint_kind(src, kinds)
            dst_kind = _endpoint_kind(dst, kinds)
            before = tally.new
            try:
                rex._upsert_shadow(  # noqa: SLF001 — 单一铸边实现
                    conn,
                    seen,
                    tally,
                    src=src,
                    dst=dst,
                    predicate=predicate,
                    confidence=float(rel.get("confidence", 0.5)),
                    quote=str(rel.get("quote") or ""),
                    label=rel.get("label"),
                    observations=1,
                    src_kind=src_kind,
                    dst_kind=dst_kind,
                    polarity=_norm_polarity(rel.get("polarity")),
                )
            except ValueError:
                # 非法端点/谓词组合 → 非 P0 关系，丢弃（add_edge 内建矩阵闸）
                continue
            if tally.new > before:
                r.edges_new += 1
            else:
                r.edges_reinforced += 1
            # ended → close_edge（§4.6 leg-a）
            if rel.get("ended"):
                key = rex._edge_key(src, dst, predicate.value)  # noqa: SLF001
                eid = seen.get(key)
                if eid and edges_store.close_edge(conn, edge_id=eid, at=now):
                    r.edges_closed += 1
        except Exception as exc:  # noqa: BLE001
            r.errors.append(f"relation: {exc}")


def _apply_events(
    conn: sqlite3.Connection, clean: dict, kinds: dict[str, str], r: ApplyResult
) -> None:
    """events → event:<hash> Activity 点 + participants participates_in 边。"""
    seen = rex._open_edges(conn)  # noqa: SLF001
    tally = rex._Tally()  # noqa: SLF001
    for ev in clean.get("events") or []:
        try:
            if not isinstance(ev, dict):
                continue
            title = str(ev.get("title") or "").strip()
            if not title:
                continue
            eid = EVENT_PREFIX + hashlib.sha1(title.encode("utf-8")).hexdigest()[:12]  # noqa: S324
            r.events_minted += 1
            for p in ev.get("participants") or []:
                pc = _canonical_of(p)
                if not pc:
                    continue
                try:
                    rex._upsert_shadow(  # noqa: SLF001
                        conn,
                        seen,
                        tally,
                        src=pc if pc != SELF_IDENTITY else SELF_IDENTITY,
                        dst=eid,
                        predicate=Predicate.PARTICIPATES_IN,
                        confidence=float(ev.get("confidence", 0.5)),
                        quote=str(ev.get("quote") or title),
                        label="event",
                        observations=1,
                        src_kind=_endpoint_kind(pc, kinds),
                        dst_kind=EntityKind.EVENT.value,
                    )
                except ValueError:
                    continue
        except Exception as exc:  # noqa: BLE001
            r.errors.append(f"event: {exc}")


def _apply_floor(
    conn: sqlite3.Connection, clean: dict, kinds: dict[str, str], r: ApplyResult
) -> None:
    """① 关联地板（attention 基底）：每个语境实体 → ``self engaged_with 它``。

    共现即建（实体出现在你 session 里 = 你关注了它，session 即证据，不需显式 quote）、
    确定性零 LLM、**kind 无关**（dst 全 kind 合法）——所以连通性永远完备，且 kind 判错
    也不掉孤儿。权重 = observations，跨 session reinforce 累加 = 参与频次 = attention 权重。
    """
    seen = rex._open_edges(conn)  # noqa: SLF001
    tally = rex._Tally()  # noqa: SLF001
    for e in clean.get("entities") or []:
        if not isinstance(e, dict):
            continue
        canonical = _canonical_of(e)
        if not canonical or canonical == SELF_IDENTITY:
            continue
        try:
            rex._upsert_shadow(  # noqa: SLF001
                conn,
                seen,
                tally,
                src=SELF_IDENTITY,
                dst=canonical,
                predicate=Predicate.ENGAGED_WITH,
                confidence=1.0,
                quote=str(e.get("quote") or ""),
                label="engaged",
                observations=1,
                src_kind=EntityKind.SELF.value,
                dst_kind=_endpoint_kind(canonical, kinds),
                additive=True,  # ① 地板 = 跨会话累加（=会话数=attention 权重）,非 MAX-of-1
            )
        except ValueError:
            continue  # 理论上 engaged_with 端点全合法；防御性
    r.floor_edges = tally.new + tally.reinforced


def _endpoint_kind(identity: str, kinds: dict[str, str]) -> str:
    if identity == SELF_IDENTITY:
        return EntityKind.SELF.value
    if identity.startswith(EVENT_PREFIX):
        return EntityKind.EVENT.value
    k = kinds.get(identity)
    return _KIND_ENUM[k].value if k in _KIND_ENUM else EntityKind.PERSON.value


def _apply_supersede(conn: sqlite3.Connection, clean: dict, r: ApplyResult) -> None:
    """⊖ 退役腿（2026-07-04 更新语义重构）：delta 不只会加，也会**退**——一个完整的更新 = 加∧退。
    每项 ``{file, entry_id, reason, replacement?}`` 走 choke-point：有 replacement → ``supersede_entry``
    （退旧+写新，一次原子更新），否则 ``mark_entry_deleted``（纯退役）。supersede-不删：markdown 划线、
    收据留、可 as-of 回看。观察 delta 目前不填这条腿（→天然仅定向更新用）；逐项 try 隔离、fail-open。"""
    for item in clean.get("supersede") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("file", "")).strip()
        eid = str(item.get("entry_id", "")).strip()
        if not name or not eid:
            continue
        try:
            repl = str(item.get("replacement", "")).strip()
            reason = str(item.get("reason", "") or "memory update")[:300]
            if repl:
                entries_store.supersede_entry(
                    conn, name=name, old_entry_id=eid, new_content=repl, reason=reason
                )
            else:
                entries_store.mark_entry_deleted(conn, name=name, entry_id=eid)
            r.supersedes_applied += 1
        except Exception as exc:  # noqa: BLE001 — one bad target never drops the rest
            r.errors.append(f"supersede {name}#{eid}: {exc}")


def apply_delta(
    conn: sqlite3.Connection,
    cfg: Any,
    clean: dict,
    *,
    memory: EvoMemory | None = None,
) -> ApplyResult:
    """确定性更新执行器。``clean`` = 一个 delta（gate_delta 的输出，或定向更新 update_memory 的输出）。
    加∧退：先退役旧信念（supersede 腿），再铸新点/边/事实。幂等、fail-open。"""
    r = ApplyResult()
    if not clean:
        r.skipped_reason = "empty"
        return r
    mem = memory or EvoMemory()
    kinds = _entity_kind_map(clean)
    _apply_supersede(conn, clean, r)  # ⊖ 先退（更新=加∧退；观察 delta 此腿空）
    _apply_entities(conn, mem, clean, r)
    # ② 事实层：assertions → 实体文件事实条目（喂 schema 的料）。gated（默认 OFF，spec
    # 2026-07-04 §4 红线③），实体先铸故排在 _apply_entities 之后、其余之前。
    if getattr(getattr(cfg, "memory_delta", None), "apply_assertions", False):
        _apply_assertions(conn, mem, clean, kinds, r)
    _apply_floor(conn, clean, kinds, r)  # ① 关联地板（连通保底）
    _apply_relations(conn, clean, kinds, r)  # ② 语义结构
    _apply_events(conn, clean, kinds, r)
    return r


def _norm_polarity(p: Any) -> str:
    v = str(p or "0")
    return v if v in ("+", "-", "0") else "0"

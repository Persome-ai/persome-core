"Evolutionary memory operations, reconciliation, and atomic chain updates."

from __future__ import annotations

import re
from datetime import UTC, datetime

from ..store import files as files_mod
from .chain import expand_evolution_chains
from .models import MemoryLayer, MemoryNode, ReconcileAction, ReconcileOp
from .reconciler import Reconciler
from .store import NodeStore

_CANDIDATE_FALLBACK_LIMIT = 20


_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fff]")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text or "")


def _new_id(now: datetime) -> str:

    from ..store.entries import make_id

    return make_id(now.astimezone().strftime("%Y-%m-%dT%H:%M"))


def _now() -> datetime:
    return datetime.now(UTC)


def _validated_file_name(file_name: str) -> str:
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
    ) -> None:

        self.user_id = user_id
        self.agent_id = agent_id
        self._reconciler = reconciler
        self._store = store or NodeStore(user_id=user_id, agent_id=agent_id)

    @property
    def store(self) -> NodeStore:
        return self._store

    def add(
        self,
        text: str,
        *,
        layer: MemoryLayer = MemoryLayer.L2_FACT,
        file_name: str = "",
        tags: str = "",
    ) -> list[str]:
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
        op = ReconcileOp(action=ReconcileAction.ADD, content=content, layer=layer)
        return self.apply_ops([op], layer=layer, file_name=file_name, tags=tags)[0]

    #

    def commit_node(self, node: MemoryNode) -> str:
        node.file_name = _validated_file_name(node.file_name)
        self._store.save(node)
        return node.node_id

    def commit_supersede(
        self, node: MemoryNode, *, old_id: str, old_valid_until: str | None = None
    ) -> str:
        node.file_name = _validated_file_name(node.file_name)
        self._store.save_and_supersede(node, old_id=old_id, old_valid_until=old_valid_until)
        return node.node_id

    def commit_retire(self, node_id: str, *, valid_until: str | None = None) -> None:
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
        op_layer = op.layer or layer
        if op.action is ReconcileAction.ADD:
            return self._save_head(op.content, op_layer, file_name=file_name, tags=tags)

        if op.action is ReconcileAction.SUPERSEDE and op.target_id is not None:
            node = self._make_node(
                op.content, op_layer, supersedes=[op.target_id], file_name=file_name, tags=tags
            )
            self._store.save_and_supersede(node, old_id=op.target_id)
            return node.node_id

        if op.action is ReconcileAction.UPDATE and op.target_id is not None:
            node = self._make_node(
                op.content, op_layer, file_name=file_name, tags=tags, refined_from=op.target_id
            )
            self._store.save_and_shadow(node, old_id=op.target_id)
            return node.node_id

        if op.action is ReconcileAction.DELETE and op.target_id is not None:
            self._store.shadow(op.target_id)
            return None

        if op.action is ReconcileAction.ABSTRACT and op.source_ids:
            #

            node = self._make_node(
                op.content,
                op_layer,
                file_name=file_name,
                tags=tags,
                abstracted_from=list(op.source_ids),
            )
            self._store.save_and_retire_sources(node, source_ids=op.source_ids)
            return node.node_id

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

    def search(self, query: str, top_k: int = 10) -> list[dict]:
        hits = self._recall(query, top_k=top_k)
        return expand_evolution_chains(self._store.get_by_ids, hits)

    def _lexical_recall(self, query: str, *, top_k: int) -> list[dict]:
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
        """Recall active nodes through the deterministic substring/token path."""
        return self._lexical_recall(query, top_k=top_k)

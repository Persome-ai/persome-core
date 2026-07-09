"""Entry-level operations: create file, append, supersede. Syncs FTS5 on every write."""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import frontmatter

from ..evomem import integrity as evo_integrity
from ..evomem import inversion as evo_inversion
from ..evomem import shadow as evo_shadow
from ..logger import get
from . import files as files_mod
from . import fts

logger = get("persome.store")


def _now_iso_minute() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M")


def make_id(timestamp: str) -> str:
    """YYYYMMDD-HHMM-<6-hex>.

    6 hex chars (24 bits) keeps collision probability <0.1% within a single
    minute even under heavy batched writes.
    """
    compact = timestamp.replace("-", "").replace(":", "").replace("T", "-")[:13]
    salt = hashlib.blake2s(os.urandom(8), digest_size=3).hexdigest()
    return f"{compact}-{salt}"


def _ensure_prefix(path_name: str) -> str:
    return files_mod.validate_prefix(path_name)


def _norm_occurred_at(occurred_at: str | None) -> str | None:
    """Normalize ``occurred_at`` to a whitespace-free ISO string, or ``None``.

    heading colon-tags 按空白分词解析（``store/files.py:_parse_entries``），含内部空白的
    值会被切断 → 增量路径存全值、rebuild 只剩首段，打破 ``entry_metadata`` ≡ rebuild 不变量
    （issue #434）。这里把常见的「日期␣时间」首个空格归一为 ``T``；若归一后仍含任何空白
    （多空格 / 时区后缀等异常形态），返回 ``None``（== 未标注），保证落盘的值必能字节级
    round-trip。**两个写口（markdown tag + ``set_entry_metadata``）都过这一关，值才一致。**
    """
    if not occurred_at or not occurred_at.strip():
        return None
    norm = occurred_at.strip().replace(" ", "T", 1)
    return norm if not any(ch.isspace() for ch in norm) else None


def _metadata_tags(confidence: str | None, conflicted: bool, occurred_at: str | None) -> list[str]:
    """Render meta-cognition fields as heading colon-tags (Hy-Memory migration).

    Values are normalized here so the markdown tag the incremental path writes is
    byte-identical to what a fresh ``rebuild_index`` re-parses — the precondition
    for the entry_metadata == rebuild invariant. An unknown confidence level or a
    falsy flag/value contributes no tag (== the all-default, row-less case).
    """
    out: list[str] = []
    if confidence:
        c = confidence.strip().lower()
        if c in fts.CONFIDENCE_LEVELS:
            out.append(f"confidence:{c}")
    if conflicted:
        out.append("conflicted")
    norm_occurred = _norm_occurred_at(occurred_at)
    if norm_occurred:
        out.append(f"occurred:{norm_occurred}")
    return out


def derived_append_rows(
    conn: sqlite3.Connection,
    *,
    entry_id: str,
    path_name: str,
    prefix: str,
    ts: str,
    tags_str: str,
    content: str,
    confidence: str | None = None,
    conflicted: bool = False,
    occurred_at: str | None = None,
) -> None:
    """一次 append 的派生表序列（entries/entry_temporal/entry_metadata）。

    **两种写权共用**（PR-6b）：markdown 主写模式由 :func:`append_entry` 在文件锁内
    调用；evomem 主写模式由 ``evomem/inversion.py`` 在真相写（evo_nodes）落定后
    调用——派生行的取值与顺序只存在一份，两条路径的 FTS 检索投影不可能各自漂移
    （§1.4「entries 表降级为检索投影」的 by-construction 等价保证）。
    """
    fts.insert_entry(
        conn,
        id=entry_id,
        path=path_name,
        prefix=prefix,
        timestamp=ts,
        tags=tags_str,
        content=content,
        superseded=0,
    )
    conn.execute(
        "INSERT OR IGNORE INTO entry_temporal(entry_id, valid_from) VALUES (?, ?)",
        (entry_id, ts),
    )
    # Meta-cognition derived row (Hy-Memory migration). Same writer as the
    # rebuild replay, so {incremental entry_metadata} ≡ {fresh rebuild}.
    fts.set_entry_metadata(
        conn,
        entry_id,
        confidence=confidence,
        conflicted=conflicted,
        occurred_at=occurred_at,
    )
    # Dense-retrieval enqueue (hybrid search Phase 1). No-op when hybrid is off; never embeds
    # inline (a daemon tick drains the queue) so capture is never blocked on a network call.
    from . import vectors as vectors_mod

    vectors_mod.maybe_enqueue(conn, entry_id, ts=ts)


def derived_supersede_rows(
    conn: sqlite3.Connection,
    *,
    old_entry_id: str,
    new_entry_id: str,
    path_name: str,
    prefix: str,
    ts: str,
    tags_str: str,
    content: str,
    confidence: str | None = None,
    conflicted: bool = False,
    occurred_at: str | None = None,
) -> None:
    """一次 supersede 的派生表序列（退役旧 + 落新 + 链接 + 检索统计承继）。

    与 :func:`derived_append_rows` 同款的两写权共用收口（PR-6b）。
    """
    fts.mark_superseded(conn, old_entry_id)
    conn.execute(
        "UPDATE entry_temporal SET valid_until=? WHERE entry_id=? AND valid_until IS NULL",
        (ts, old_entry_id),
    )
    fts.insert_entry(
        conn,
        id=new_entry_id,
        path=path_name,
        prefix=prefix,
        timestamp=ts,
        tags=tags_str,
        content=content,
        superseded=0,
    )
    conn.execute(
        "INSERT OR IGNORE INTO entry_temporal(entry_id, valid_from) VALUES (?, ?)",
        (new_entry_id, ts),
    )
    # Meta-cognition for the NEW head (the old entry keeps its own heading tags,
    # so its entry_metadata row survives a rebuild untouched).
    fts.set_entry_metadata(
        conn,
        new_entry_id,
        confidence=confidence,
        conflicted=conflicted,
        occurred_at=occurred_at,
    )
    # Carry the predecessor's retrieval history onto the new entry so a
    # supersede doesn't reset the load-bearing signal. ON CONFLICT DO NOTHING
    # because the new id is fresh — the guard is purely defensive.
    conn.execute(
        """
        INSERT INTO entry_retrieval_stats(entry_id, retrieval_count, last_retrieved_at)
        SELECT ?, retrieval_count, last_retrieved_at
          FROM entry_retrieval_stats WHERE entry_id = ?
        ON CONFLICT(entry_id) DO NOTHING
        """,
        (new_entry_id, old_entry_id),
    )
    # Dense layer: the old head is now superseded (dropped from dense search anyway), drop its
    # vector; the new head joins the embed queue. No-op when hybrid is off.
    from . import vectors as vectors_mod

    vectors_mod.evict(conn, old_entry_id)
    vectors_mod.maybe_enqueue(conn, new_entry_id, ts=ts)


def derived_retire_rows(conn: sqlite3.Connection, *, entry_id: str, ts: str) -> None:
    """一次孤儿退役（DELETE / ABSTRACT 源）的派生表序列。两写权共用（PR-6b）。"""
    fts.mark_superseded(conn, entry_id)
    conn.execute(
        "UPDATE entry_temporal SET valid_until=? WHERE entry_id=? AND valid_until IS NULL",
        (ts, entry_id),
    )
    from . import vectors as vectors_mod

    vectors_mod.evict(conn, entry_id)


def create_file(
    conn: sqlite3.Connection,
    *,
    name: str,
    description: str,
    tags: list[str],
    status: str = "active",
) -> Path:
    # Integrity freeze seam (evomem SSOT switch §3.3 #7): with the default
    # config the flag is never set, so this is a pure no-op flag check.
    evo_integrity.ensure_writes_allowed()
    # 写权反转 dispatch（PR-6b §4.4）：write_authority="evomem" 且目标文件属
    # evo_nodes 范围（非 event-*/子目录）时，本写口动词改由反转写路承接——真相落
    # evo_nodes、FTS 表降级为检索投影、markdown 由投影器再生成。默认
    # "markdown" = 纯 flag 检查，下方 legacy 路径与现状字节等价（P0）。
    if evo_inversion.routes_to_engine(name):
        return evo_inversion.create_file(
            conn, name=name, description=description, tags=tags, status=status
        )
    if not description.strip():
        raise ValueError("description is required")
    prefix = _ensure_prefix(name)
    path = files_mod.memory_path(name)
    # Lock around the exists-check + write so two concurrent classifiers
    # deciding to create the same file don't both pass the check and have
    # the second clobber the first's freshly written content.
    with files_mod.file_lock(path):
        if path.exists():
            raise FileExistsError(f"{path.name} already exists")

        # `status` lands in BOTH the frontmatter (the source of truth that
        # rebuild_index re-derives from) and the files-table row, so a file born
        # e.g. ``dormant`` survives a rebuild without drifting back to active.
        fm = files_mod.default_frontmatter(description=description, tags=tags, status=status)
        files_mod.write_file(path, fm, body="")
        fts.upsert_file(
            conn,
            fts.FileRow(
                path=path.name,
                prefix=prefix,
                description=description,
                tags=" ".join(tags),
                status=status,
                entry_count=0,
                created=fm["created"],
                updated=fm["updated"],
                needs_compact=0,
            ),
        )
    logger.info("created file: %s (status=%s)", path.name, status)
    return path


def set_file_status(conn: sqlite3.Connection, *, name: str, status: str) -> None:
    """Set a file's status in BOTH the markdown frontmatter and the files table.

    Frontmatter is the source of truth ``rebuild_index`` re-derives from, so a
    status written only to the DB (as ``index_md.auto_dormant`` does) is undone by
    the next rebuild. Writing both keeps it durable. No-op if the file is missing.
    Used to promote a ``dormant`` forming schema to ``active`` once it matures (and
    the inverse) without drift.
    """
    evo_integrity.ensure_writes_allowed()
    if evo_inversion.routes_to_engine(name):  # 写权反转 dispatch（PR-6b）
        return evo_inversion.set_file_status(conn, name=name, status=status)
    path = files_mod.memory_path(name)
    if not path.exists():
        return
    # `update_frontmatter` takes the file lock itself (non-reentrant), so don't
    # wrap it in another. The DB update is independent and doesn't need the lock.
    files_mod.update_frontmatter(path, {"status": status})
    conn.execute("UPDATE files SET status = ? WHERE path = ?", (status, path.name))
    conn.commit()
    logger.info("file status set: %s -> %s", path.name, status)


def append_entry(
    conn: sqlite3.Connection,
    *,
    name: str,
    content: str,
    tags: list[str],
    soft_limit_tokens: int | None = None,
    confidence: str | None = None,
    conflicted: bool = False,
    occurred_at: str | None = None,
) -> str:
    """Append a new entry, returning its id.

    ``confidence`` / ``conflicted`` / ``occurred_at`` are the meta-cognition layer
    (Hy-Memory migration). They ride the same heading-tag + parse path as
    ``refined-from`` — rendered as ``#confidence:<level>`` / ``#conflicted`` /
    ``#occurred:<iso>`` tags (markdown SSOT) and mirrored into the ``entry_metadata``
    derived table. All default → no tag, no row (byte-identical to before).
    """
    evo_integrity.ensure_writes_allowed()
    if evo_inversion.routes_to_engine(name):  # 写权反转 dispatch（PR-6b）
        return evo_inversion.append_entry(
            conn,
            name=name,
            content=content,
            tags=tags,
            soft_limit_tokens=soft_limit_tokens,
            confidence=confidence,
            conflicted=conflicted,
            occurred_at=occurred_at,
        )
    path = files_mod.memory_path(name)
    if not path.exists():
        raise FileNotFoundError(f"{path.name} does not exist; call create_file first")
    prefix = _ensure_prefix(name)

    # 规整一次，两个写口（markdown tag + set_entry_metadata）共用同一值，否则 DB 存原始
    # 空格值、markdown/rebuild 存归一值，反而把不变量打破在另一侧（issue #434）。
    occurred_at = _norm_occurred_at(occurred_at)

    ts = _now_iso_minute()
    entry_id = make_id(ts)
    all_tags = list(tags) + _metadata_tags(confidence, conflicted, occurred_at)
    heading = files_mod.render_heading(timestamp=ts, entry_id=entry_id, tags=all_tags)
    body = content.strip()

    # Lock the read-modify-write so a concurrent classifier appending to
    # the same file can't read the same base, append, and clobber this
    # write — both writes claim "+1 entry" but only one entry survives
    # while the FTS index keeps both, leaving file/index inconsistent.
    with files_mod.file_lock(path):
        post = frontmatter.load(path)
        current = post.content.rstrip()
        new_block = f"\n\n{heading}\n{body}\n" if current else f"{heading}\n{body}\n"
        post.content = current + new_block
        post.metadata["entry_count"] = int(post.metadata.get("entry_count", 0)) + 1
        post.metadata["updated"] = files_mod.today()

        # Soft limit check
        if soft_limit_tokens is not None:
            est_tokens = len(post.content) // 4
            if est_tokens > soft_limit_tokens and not post.metadata.get("needs_compact"):
                post.metadata["needs_compact"] = True
                logger.info(
                    "flagged %s for compact (est %d tokens > %d)",
                    path.name,
                    est_tokens,
                    soft_limit_tokens,
                )

        files_mod.atomic_write_text(path, frontmatter.dumps(post) + "\n")

        # Update FTS inside the lock too — a concurrent appender that
        # observes the file post-write must also observe the matching
        # FTS row, otherwise rebuild_index sees a row pointing at an
        # entry that "doesn't exist" until the second writer commits.
        derived_append_rows(
            conn,
            entry_id=entry_id,
            path_name=path.name,
            prefix=prefix,
            ts=ts,
            tags_str=" ".join(all_tags),
            content=body,
            confidence=confidence,
            conflicted=conflicted,
            occurred_at=occurred_at,
        )
        fts.upsert_file(
            conn,
            fts.FileRow(
                path=path.name,
                prefix=prefix,
                description=str(post.metadata.get("description", "")),
                tags=" ".join(post.metadata.get("tags", []) or []),
                status=str(post.metadata.get("status", "active")),
                entry_count=int(post.metadata.get("entry_count", 0)),
                created=str(post.metadata.get("created", "")),
                updated=str(post.metadata.get("updated", "")),
                needs_compact=1 if post.metadata.get("needs_compact") else 0,
            ),
        )
        # 影子写 hook（SSOT 切换 §4.2，PR-3）：锁尾增量镜像进 evo_nodes。绝不抛出、
        # 绝不回滚主写；关闭（[evomem] shadow_write_enabled=false）= 纯 flag 检查，
        # 主写路径行为与现状等价。挂在锁内使文件内容与刚写完的派生表状态一致。
        evo_shadow.after_write(conn, name=name, entry_ids=[entry_id])
    return entry_id


def _strike_entry_body(text: str, *, entry_id: str, body: str) -> str:
    """Wrap one entry's body in ``~~...~~`` ANCHORED to its ``{id: entry_id}`` marker.

    A content-blind ``text.replace(body, striked, 1)`` strikes the *first* matching
    body in the whole file — wrong when two entries share a byte-identical body
    (bug_009). This locates the entry's own body region first: from its unique
    ``{id: entry_id}`` heading down to the next entry heading (or EOF), and only
    replaces the body INSIDE that slice.

    No-op (returns ``text`` unchanged) when the entry id isn't found or its body is
    already struck. When ``body`` is empty (an empty-body entry being retired with
    no successor), a durable ``~~~~`` sentinel is inserted on its own line so a
    later ``rebuild_index`` still re-derives ``superseded=1`` (merged_bug_002a) —
    ``_body_is_striked("~~~~")`` is True.
    """
    # Find the heading for this exact entry id, anchored to the {id: ...} marker.
    target_match = None
    matches = list(files_mod.ENTRY_HEADING_RE.finditer(text))
    for i, m in enumerate(matches):
        if m.group("id") == entry_id:
            target_match = (i, m)
            break
    if target_match is None:
        return text
    idx, m = target_match
    region_start = m.end()
    region_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
    region = text[region_start:region_end]

    striked = "~~" + body.strip() + "~~"
    if body.strip():
        if body in region:
            new_region = region.replace(body, striked, 1)
        else:
            return text  # body not located in its own region (already rewritten) → no-op
    else:
        # Empty body: nothing to wrap. Insert a durable sentinel right after the
        # heading line so the retirement survives a rebuild. Idempotent: bail if a
        # strike sentinel already sits in the region.
        if _STRIKE_RE.search(region):
            return text
        new_region = "\n~~~~" + region
    return text[:region_start] + new_region + text[region_end:]


def supersede_entry(
    conn: sqlite3.Connection,
    *,
    name: str,
    old_entry_id: str,
    new_content: str,
    reason: str,
    tags: list[str] | None = None,
    refined_from: str | None = None,
    confidence: str | None = None,
    conflicted: bool = False,
    occurred_at: str | None = None,
) -> str:
    """Mark old entry superseded and append the new one. Returns new entry id.

    ``refined_from`` (EVO-02 双标签法): when set, the new entry additionally
    carries a ``#refined-from:{refined_from}`` provenance tag on its heading,
    marking this supersede as a *refinement* (sharpening) rather than a
    contradiction. The fold/chain mechanics are unchanged — the old entry still
    gets ``#superseded-by:{new}`` and is folded out, the new entry is the chain
    head — so a refinement now退役 the old version (对齐上游/engine.py) while a
    trail renderer can read the ``refined-from`` tag to distinguish ``← [精炼自]``
    from a contradiction's ``← [曾]``. ``None`` (every existing SUPERSEDE caller)
    → byte-identical to before: no extra tag is written.
    """
    evo_integrity.ensure_writes_allowed()
    if evo_inversion.routes_to_engine(name):  # 写权反转 dispatch（PR-6b）
        return evo_inversion.supersede_entry(
            conn,
            name=name,
            old_entry_id=old_entry_id,
            new_content=new_content,
            reason=reason,
            tags=tags,
            refined_from=refined_from,
            confidence=confidence,
            conflicted=conflicted,
            occurred_at=occurred_at,
        )
    path = files_mod.memory_path(name)
    if not path.exists():
        raise FileNotFoundError(path.name)

    # Same shape as append_entry: read-modify-write on a markdown file
    # plus an FTS update. Holding the lock across both halves keeps
    # readers from seeing a state where the file has the new entry but
    # FTS still doesn't (or vice versa).
    with files_mod.file_lock(path):
        parsed = files_mod.read_file(path)
        target = next((e for e in parsed.entries if e.id == old_entry_id), None)
        if target is None:
            raise ValueError(f"entry {old_entry_id} not found in {path.name}")

        # Build replacement heading and body in the file
        ts = _now_iso_minute()
        new_id = make_id(ts)

        # The new entry's base tag set is the caller's ``tags`` (or, falling back,
        # the old entry's tags). A refinement adds an orthogonal ``refined-from``
        # provenance tag on TOP of that base — additive only, so the supersede
        # chain mechanics (#superseded-by back-map, fold) are untouched.
        # 规整一次，markdown tag 与 set_entry_metadata 共用同一值（issue #434）。
        occurred_at = _norm_occurred_at(occurred_at)

        new_tags = list(tags or target.tags)
        if refined_from:
            new_tags.append(f"refined-from:{refined_from}")
        new_tags += _metadata_tags(confidence, conflicted, occurred_at)

        new_heading = files_mod.render_heading(timestamp=ts, entry_id=new_id, tags=new_tags)

        # Modify file text directly to preserve formatting
        text = path.read_text()
        # 1) append #superseded-by to old heading (only if not already present)
        old_heading = target.heading_line
        if f"superseded-by:{new_id}" not in old_heading:
            updated_heading = old_heading.rstrip() + f" #superseded-by:{new_id}"
            text = text.replace(old_heading, updated_heading, 1)
        # 2) wrap old body in ~~...~~ (only if not already) — anchored to the
        # entry's own {id} region so a duplicate body elsewhere isn't struck (bug_009).
        if target.body and not target.body.startswith("~~"):
            text = _strike_entry_body(text, entry_id=old_entry_id, body=target.body)

        # 3) Append the new entry at the end
        body = new_content.strip()
        new_block = (
            f"\n\n{new_heading}\n{body}\n<!-- supersedes: {old_entry_id}; reason: {reason} -->\n"
        )
        if not text.endswith("\n"):
            text += "\n"
        text += new_block

        # Fold the metadata bump (entry_count, updated) into a SINGLE write
        # via in-memory parse (frontmatter.loads), so the lock holds across
        # one atomic write rather than two writes with a reload between.
        post = frontmatter.loads(text)
        post.metadata["entry_count"] = int(post.metadata.get("entry_count", 0)) + 1
        post.metadata["updated"] = files_mod.today()
        files_mod.atomic_write_text(path, frontmatter.dumps(post) + "\n")

        # FTS + temporal + retrieval carry — the shared derived sequence
        # (live equivalent of rebuild walking the #superseded-by tag we just
        # wrote to markdown).
        prefix = _ensure_prefix(name)
        derived_supersede_rows(
            conn,
            old_entry_id=old_entry_id,
            new_entry_id=new_id,
            path_name=path.name,
            prefix=prefix,
            ts=ts,
            tags_str=" ".join(new_tags),
            content=body,
            confidence=confidence,
            conflicted=conflicted,
            occurred_at=occurred_at,
        )
        fts.upsert_file(
            conn,
            fts.FileRow(
                path=path.name,
                prefix=prefix,
                description=str(post.metadata.get("description", "")),
                tags=" ".join(post.metadata.get("tags", []) or []),
                status=str(post.metadata.get("status", "active")),
                entry_count=int(post.metadata.get("entry_count", 0)),
                created=str(post.metadata.get("created", "")),
                updated=str(post.metadata.get("updated", "")),
                needs_compact=1 if post.metadata.get("needs_compact") else 0,
            ),
        )
        # 影子写 hook（§4.2）：supersede 影响新旧两个节点——旧节点 shadow + 双向
        # 指针/refined_from 出处由共享映射从刚写下的 markdown tag 再生，与 backfill
        # 对既有链的判定一致。失败/滞后只记 miss，主写不受影响。
        evo_shadow.after_write(conn, name=name, entry_ids=[old_entry_id, new_id])
    return new_id


def mark_entry_deleted(conn: sqlite3.Connection, *, name: str, entry_id: str) -> None:
    """Logically retire an entry: strike its markdown body, mark FTS superseded.

    This is ``supersede_entry`` *without a replacement* — the DELETE landing
    (retire with no successor) and the ABSTRACT source-retire landing (each source
    after the synthesized entry is appended). The body is wrapped in ``~~...~~`` in the
    markdown file so the retirement is **markdown-durable**: ``rebuild_index``'s
    ``_body_is_striked`` re-detects the strike and re-derives ``superseded=1`` from
    markdown, the SSOT. A FTS-only ``mark_superseded`` would silently revive the
    entry on the next rebuild (markdown is the truth, the column is a projection).

    No ``#superseded-by`` heading tag is written (there is no successor id), so the
    entry stays off the evolution chain — it is just a retired leaf. Idempotent:
    re-striking an already-struck body is a no-op.

    NOTE (EVO-02 双标签法): UPDATE no longer routes through here. It now退役 the old
    version via ``supersede_entry(..., refined_from=old)`` — a real supersede chain
    head with a ``#refined-from`` provenance tag — rather than the orphan strike
    path, so the old version is folded out of recall (对齐上游/engine.py) and the
    refinement is distinguishable from a contradiction in the evolution trail.
    """
    evo_integrity.ensure_writes_allowed()
    if evo_inversion.routes_to_engine(name):  # 写权反转 dispatch（PR-6b）
        return evo_inversion.mark_entry_deleted(conn, name=name, entry_id=entry_id)
    path = files_mod.memory_path(name)
    if not path.exists():
        raise FileNotFoundError(path.name)
    with files_mod.file_lock(path):
        parsed = files_mod.read_file(path)
        target = next((e for e in parsed.entries if e.id == entry_id), None)
        if target is None:
            raise ValueError(f"entry {entry_id} not found in {path.name}")

        # Strike the old body in the markdown, ANCHORED to this entry's {id} region
        # (bug_009 — never strike a byte-identical body belonging to another entry).
        # An empty body has no markdown to wrap and (unlike supersede_entry) no
        # #superseded-by successor tag to fall back on, so the helper inserts a
        # durable ``~~~~`` sentinel — otherwise a rebuild would revive it
        # (merged_bug_002a). Bump the frontmatter ``updated`` and refresh the
        # files-table FileRow in the same atomic write (merged_bug_002b) so the
        # files row / list ordering don't drift from a fresh rebuild.
        if not target.body.startswith("~~"):
            text = path.read_text()
            text = _strike_entry_body(text, entry_id=entry_id, body=target.body)
            post = frontmatter.loads(text)
            post.metadata["updated"] = files_mod.today()
            files_mod.atomic_write_text(path, frontmatter.dumps(post) + "\n")
            prefix = _ensure_prefix(name)
            fts.upsert_file(
                conn,
                fts.FileRow(
                    path=path.name,
                    prefix=prefix,
                    description=str(post.metadata.get("description", "")),
                    tags=" ".join(post.metadata.get("tags", []) or []),
                    status=str(post.metadata.get("status", "active")),
                    entry_count=int(post.metadata.get("entry_count", 0)),
                    created=str(post.metadata.get("created", "")),
                    updated=str(post.metadata.get("updated", "")),
                    needs_compact=1 if post.metadata.get("needs_compact") else 0,
                ),
            )

        # FTS + temporal: mirror supersede_entry's retire half (no new entry).
        ts = _now_iso_minute()
        derived_retire_rows(conn, entry_id=entry_id, ts=ts)
        # 影子写 hook（§4.2）：DELETE 的影子 = 该节点按 markdown 终态重映射（孤儿
        # strike → status=shadow, is_latest=0，与 backfill 三态判定逐字一致——含
        # refined-from 强制活跃分支，保证「增量 == 全量重跑」不变式）。
        evo_shadow.after_write(conn, name=name, entry_ids=[entry_id])


def rebuild_index(conn: sqlite3.Connection) -> tuple[int, int]:
    """检索投影重建器（SSOT 切换设计 §2，PR-7 重定义）：从**当前写权的真相**重放
    entries（FTS）/ entry_metadata / files 检索投影。Returns (files, entries)。

    - ``write_authority="markdown"``（默认，§6 回滚杠杆）：markdown 是 SSOT——
      行为与历史 rebuild 一致：DELETE 后从 ``memory/*.md`` 全量重放三表
      （:func:`_rebuild_from_markdown`，原 from-markdown 全量逻辑）。
    - ``write_authority="evomem"``：evo_nodes 是 SSOT——entries/entry_metadata
      从 evo_nodes 投影（``superseded = 0 iff is_latest=1 AND status='active'``，
      §1.4/Q7 派生规则），files 行保留为文件级元数据真相只刷新 entry_count；
      **Q2 豁免类（event-*）与任何不在 evo_nodes 的 markdown 文件**（历史遗留 /
      legacy 直写口）仍从 markdown 直读——混合重建（:func:`_rebuild_from_evo_nodes`）。

    真相层自身的恢复不走这里：evo_nodes 损坏 → 从快照恢复（§3.2）或
    ``persome evomem-restore-from-markdown``（§3.4 有损灾难恢复，
    ``evomem/restore.py:import_from_markdown``）。
    """
    if evo_inversion.evomem_active():
        return _rebuild_from_evo_nodes(conn)
    return _rebuild_from_markdown(conn)


def _ingest_markdown_file(conn: sqlite3.Connection, path: Path, prefix: str) -> int:
    """单文件 markdown 直读重放（files 行 + entries + entry_metadata），返回条目数。"""
    parsed = files_mod.read_file(path)
    fts.upsert_file(
        conn,
        fts.FileRow(
            path=path.name,
            prefix=prefix,
            description=parsed.description,
            tags=" ".join(parsed.tags),
            status=parsed.status,
            entry_count=len(parsed.entries),
            created=parsed.created,
            updated=parsed.updated,
            needs_compact=1 if parsed.needs_compact else 0,
        ),
    )
    for e in parsed.entries:
        superseded = _superseded_from_tags(e)
        # Only strip the strike markers off entries we judged superseded —
        # a refined-from / live entry keeps its body verbatim so a legitimate
        # inline ``~~strike~~`` in the prose is preserved (EVO-02).
        content = _strip_strike(e.body) if superseded else e.body
        fts.insert_entry(
            conn,
            id=e.id,
            path=path.name,
            prefix=prefix,
            timestamp=e.timestamp,
            tags=" ".join(e.tags),
            content=content,
            superseded=superseded,
        )
        # Replay meta-cognition through the SAME writer the incremental path
        # uses, so a fresh rebuild reproduces entry_metadata row-for-row.
        fts.set_entry_metadata(
            conn,
            e.id,
            confidence=e.confidence,
            conflicted=e.conflicted,
            occurred_at=e.occurred_at,
        )
    return len(parsed.entries)


def _rebuild_from_markdown(conn: sqlite3.Connection) -> tuple[int, int]:
    """markdown 主写模式的全量重放（原 rebuild_index 逻辑）。幂等。"""
    conn.execute("DELETE FROM entries")
    conn.execute("DELETE FROM files")
    conn.execute("DELETE FROM entry_metadata")
    file_count = 0
    entry_count = 0
    for path in files_mod.list_memory_files():
        try:
            prefix = _ensure_prefix(path.name)
        except ValueError as exc:
            logger.warning("skipping %s: %s", path.name, exc)
            continue
        entry_count += _ingest_markdown_file(conn, path, prefix)
        file_count += 1
    return file_count, entry_count


def _rebuild_from_evo_nodes(conn: sqlite3.Connection) -> tuple[int, int]:
    """evomem 主写模式的混合重建（§2 表「rebuild_index 重定义」）。幂等。

    entries/entry_metadata 重放自 evo_nodes（真相侧，scope=default per Q4），
    heading 时间戳 / tag 渲染与 markdown 投影器同源（``projector._heading_ts`` /
    ``_render_tags``），``superseded`` 按 §1.4/Q7 派生。event-*（Q2 豁免，链真相
    从不进 evo_nodes）与任何无节点的 markdown 文件仍从 markdown 直读。files 行
    在本模式下是文件级元数据的真相（inversion 写口维护），**不**被 DELETE——
    仅刷新 entry_count；缺行的 evo 文件从盘上 frontmatter 兜底补行。
    """
    from ..evomem.models import MemoryStatus
    from ..evomem.store import _row_to_node
    from . import projector

    conn.execute("DELETE FROM entries")
    conn.execute("DELETE FROM entry_metadata")
    groups: dict[str, list] = {}
    for r in conn.execute(
        "SELECT * FROM evo_nodes WHERE user_id='default' AND agent_id='default' ORDER BY node_id"
    ).fetchall():
        node = _row_to_node(r)
        if not node.file_name:
            continue  # 未路由节点（如 run_system2 直写的 demo 节点）不投影
        groups.setdefault(node.file_name, []).append(node)

    file_count = 0
    entry_count = 0
    for file_name in sorted(groups):
        try:
            prefix = _ensure_prefix(file_name)
        except ValueError as exc:
            logger.warning("skipping evo file group %s: %s", file_name, exc)
            continue
        nodes = sorted(groups[file_name], key=lambda n: (projector._heading_ts(n), n.node_id))
        ts_by_id = {n.node_id: projector._heading_ts(n) for n in nodes}
        for n in nodes:
            superseded = 0 if (n.is_latest and n.status is MemoryStatus.ACTIVE) else 1
            fts.insert_entry(
                conn,
                id=n.node_id,
                path=file_name,
                prefix=prefix,
                timestamp=projector._heading_ts(n),
                tags=" ".join(projector._render_tags(n, prefix=prefix, ts_by_id=ts_by_id)),
                content=n.content,
                superseded=superseded,
            )
            fts.set_entry_metadata(
                conn,
                n.node_id,
                confidence=n.confidence,
                conflicted=n.conflicted,
                occurred_at=n.occurred_at,
            )
            entry_count += 1
        row = conn.execute("SELECT 1 FROM files WHERE path=?", (file_name,)).fetchone()
        if row is not None:
            conn.execute("UPDATE files SET entry_count=? WHERE path=?", (len(nodes), file_name))
        else:
            # files 行缺失（真相元数据丢了）：盘上投影的 frontmatter 兜底补行。
            path = files_mod.memory_path(file_name)
            if path.exists():
                parsed = files_mod.read_file(path)
                fts.upsert_file(
                    conn,
                    fts.FileRow(
                        path=file_name,
                        prefix=prefix,
                        description=parsed.description,
                        tags=" ".join(parsed.tags),
                        status=parsed.status,
                        entry_count=len(nodes),
                        created=parsed.created,
                        updated=parsed.updated,
                        needs_compact=1 if parsed.needs_compact else 0,
                    ),
                )
            else:
                fts.upsert_file(
                    conn,
                    fts.FileRow(
                        path=file_name,
                        prefix=prefix,
                        description="",
                        tags="",
                        status="active",
                        entry_count=len(nodes),
                        created="",
                        updated=files_mod.today(),
                        needs_compact=0,
                    ),
                )
        file_count += 1

    # Q2 豁免类与未入 evo_nodes 的文件：markdown 直读（与 legacy 重放同一形态）。
    for path in files_mod.list_memory_files():
        if path.name in groups:
            continue
        try:
            prefix = _ensure_prefix(path.name)
        except ValueError as exc:
            logger.warning("skipping %s: %s", path.name, exc)
            continue
        entry_count += _ingest_markdown_file(conn, path, prefix)
        file_count += 1

    # files 行收敛 (#578): this mode does NOT `DELETE FROM files` (files rows are
    # the file-level metadata truth). But a row that once had nodes, now has NONE
    # in evo_nodes (all moved out / file deleted) AND is not on disk falls into
    # neither `groups` nor `list_memory_files()` above — so its stale entry_count
    # never gets refreshed and the row lingers forever. Prune those orphan rows so
    # rebuild stays idempotent and converges to the truth state.
    valid_names = set(groups) | {p.name for p in files_mod.list_memory_files()}
    orphans = [
        r[0] for r in conn.execute("SELECT path FROM files").fetchall() if r[0] not in valid_names
    ]
    for name in orphans:
        conn.execute("DELETE FROM files WHERE path=?", (name,))
    if orphans:
        logger.info(
            "rebuild: pruned %d stale files row(s) (no evo_nodes + not on disk): %s",
            len(orphans),
            orphans,
        )
    return file_count, entry_count


_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)


def _body_is_striked(body: str) -> bool:
    stripped = body.strip()
    return stripped.startswith("~~") and stripped.endswith("~~")


def _superseded_from_tags(e: files_mod.ParsedEntry) -> int:
    """Three-way superseded judgment from an entry's markdown tags (EVO-02).

        if superseded-by:  1   (retired by a successor)
        elif refined-from: 0   (a refinement — forced live, even if struck)
        else:              1 if whole-body struck else 0   (orphan strike fallback)

    The ``refined-from → 0`` branch short-circuits BEFORE the strike fallback, so a
    refined-from entry whose body happens to be struck still reads live. Existing
    data carries no ``refined-from`` tag, so branch ② is dead code and this is
    byte-for-byte equivalent to the previous ``superseded_by or _body_is_striked``
    expression (the zero-regression guarantee).
    """
    if e.superseded_by:
        return 1
    if e.refined_from:
        return 0
    return 1 if _body_is_striked(e.body) else 0


def _strip_strike(body: str) -> str:
    return _STRIKE_RE.sub(r"\1", body)


def write_preset_files(conn: sqlite3.Connection) -> None:
    """Create user-profile.md and user-preferences.md if absent."""
    presets: dict[str, dict[str, Any]] = {
        "user-profile.md": {
            "description": (
                "User's identity, background, and long-term stable basic information "
                "(name, profession, languages, location, skill stack, etc.)"
            ),
            "tags": ["identity", "background"],
        },
        "user-preferences.md": {
            "description": (
                "User's preferences, habits, working style, and subjective tool choices"
            ),
            "tags": ["preferences"],
        },
    }
    for name, info in presets.items():
        if files_mod.memory_path(name).exists():
            continue
        with contextlib.suppress(FileExistsError):
            create_file(conn, name=name, description=info["description"], tags=info["tags"])

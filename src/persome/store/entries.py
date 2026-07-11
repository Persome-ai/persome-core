"""Entry-level operations: create file, append, supersede. Syncs FTS5 on every write."""

from __future__ import annotations

import contextlib
import hashlib
import html
import os
import re
import sqlite3
import time
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
    # Keep the established minute-local wire shape. Entry search has a broad
    # historical TEXT contract; changing it to aware ISO requires a dedicated
    # schema migration rather than mixing representations during hardening.
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


def _render_supersede_provenance(*, old_entry_id: str, reason: str) -> str:
    # Keep the structural marker on one valid HTML-comment line. In particular,
    # an untrusted ``-->`` in a model-generated reason must not close the marker
    # early and make rebuild treat its tail as searchable personal content.
    safe_reason = html.escape(reason.replace("\r", " ").replace("\n", " "), quote=False)
    safe_reason = safe_reason.replace("--", "&#45;&#45;")
    return f"<!-- supersedes: {old_entry_id}; reason: {safe_reason} -->"


def _insert_derived_entry(
    conn: sqlite3.Connection,
    *,
    entry_id: str,
    path_name: str,
    prefix: str,
    ts: str,
    tags_str: str,
    content: str,
) -> None:
    """Atomically claim an entry id and insert its FTS projection once.

    Markdown is written before the derived DB rows. If rebuild ingests that
    Markdown while the original writer is paused, the temporal PK claim makes
    the resumed writer observe and validate the existing projection instead of
    adding a duplicate row to the FTS5 table (whose ``id`` is not unique).
    """
    savepoint = "persome_derived_entry_insert"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        claimed = (
            conn.execute(
                "INSERT OR IGNORE INTO entry_temporal(entry_id, valid_from) VALUES (?, ?)",
                (entry_id, ts),
            ).rowcount
            == 1
        )
        candidate = (entry_id, path_name, prefix, ts, tags_str, content, 0)
        if claimed:
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
        else:
            temporal = conn.execute(
                "SELECT valid_from, valid_until FROM entry_temporal WHERE entry_id=?",
                (entry_id,),
            ).fetchone()
            if temporal is None or (temporal["valid_from"], temporal["valid_until"]) != (ts, None):
                raise sqlite3.IntegrityError(f"entry id collision for {entry_id!r}")
            rows = conn.execute(
                "SELECT id, path, prefix, timestamp, tags, content, superseded "
                "FROM entries WHERE id=?",
                (entry_id,),
            ).fetchall()
            existing = [
                (
                    row["id"],
                    row["path"],
                    row["prefix"],
                    row["timestamp"],
                    row["tags"],
                    row["content"],
                    int(row["superseded"] or 0),
                )
                for row in rows
            ]
            if not existing:
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
            elif existing != [candidate]:
                raise sqlite3.IntegrityError(f"entry id collision for {entry_id!r}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        with contextlib.suppress(sqlite3.Error):
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise


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
    _insert_derived_entry(
        conn,
        entry_id=entry_id,
        path_name=path_name,
        prefix=prefix,
        ts=ts,
        tags_str=tags_str,
        content=content,
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
    fts.mark_superseded(conn, old_entry_id)
    conn.execute(
        "UPDATE entry_temporal SET valid_until=? WHERE entry_id=? AND valid_until IS NULL",
        (ts, old_entry_id),
    )
    _insert_derived_entry(
        conn,
        entry_id=new_entry_id,
        path_name=path_name,
        prefix=prefix,
        ts=ts,
        tags_str=tags_str,
        content=content,
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
    if evo_inversion.routes_to_engine(name):
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
    if evo_inversion.routes_to_engine(name):
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

        evo_shadow.after_write(conn, name=name, entry_ids=[entry_id])
    return entry_id


def _append_entry_heading_tag(text: str, *, entry_id: str, tag: str) -> str:
    """Append ``tag`` to the exact entry heading identified by ``entry_id``."""
    rendered_tag = f"#{tag}"
    for match in files_mod.ENTRY_HEADING_RE.finditer(text):
        if match.group("id") != entry_id:
            continue
        if rendered_tag in (match.group("tags") or "").split():
            return text
        updated_heading = match.group(0).rstrip() + f" {rendered_tag}"
        return text[: match.start()] + updated_heading + text[match.end() :]
    return text


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
    evo_integrity.ensure_writes_allowed()
    if evo_inversion.routes_to_engine(name):
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
            text = _append_entry_heading_tag(
                text,
                entry_id=old_entry_id,
                tag=f"superseded-by:{new_id}",
            )
        # 2) wrap old body in ~~...~~ (only if not already) — anchored to the
        # entry's own {id} region so a duplicate body elsewhere isn't struck (bug_009).
        if target.body and not target.body.startswith("~~"):
            text = _strike_entry_body(text, entry_id=old_entry_id, body=target.body)

        # 3) Append the new entry at the end
        body = new_content.strip()
        provenance = _render_supersede_provenance(
            old_entry_id=old_entry_id,
            reason=reason,
        )
        projected_content = f"{body}\n{provenance}" if body else provenance
        new_block = f"\n\n{new_heading}\n{projected_content}\n"
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

        evo_shadow.after_write(conn, name=name, entry_ids=[old_entry_id, new_id])
    return new_id


def mark_entry_deleted(conn: sqlite3.Connection, *, name: str, entry_id: str) -> None:
    evo_integrity.ensure_writes_allowed()
    if evo_inversion.routes_to_engine(name):
        return evo_inversion.mark_entry_deleted(conn, name=name, entry_id=entry_id)
    path = files_mod.memory_path(name)
    if not path.exists():
        raise FileNotFoundError(path.name)
    with files_mod.file_lock(path):
        parsed = files_mod.read_file(path)
        target = next((e for e in parsed.entries if e.id == entry_id), None)
        if target is None:
            raise ValueError(f"entry {entry_id} not found in {path.name}")

        body_is_struck = _body_is_striked(target.body)
        explicit_until = _encoded_tag_value(target.tags, "valid-until:")
        explicit_status = _encoded_tag_value(target.tags, "status:")
        temporal_row = conn.execute(
            "SELECT valid_until FROM entry_temporal WHERE entry_id=?", (entry_id,)
        ).fetchone()
        stored_until = temporal_row["valid_until"] if temporal_row is not None else None
        # A repeated call on legacy struck Markdown should retain its original
        # retirement time. A first deletion gets a fresh timestamp even if a
        # stale side-table value somehow exists.
        ts = explicit_until or (stored_until if body_is_struck else None) or _now_iso_minute()
        temporal_tag = f"valid-until:{ts}"
        encode_temporal_tag = explicit_until is None and not target.superseded_by
        encode_shadow_status = (
            bool(target.refined_from)
            and not target.superseded_by
            and explicit_status not in {"shadow", "archived", "superseded"}
        )
        status_retires_refinement = (
            bool(target.refined_from)
            and not target.superseded_by
            and (encode_shadow_status or explicit_status in {"shadow", "archived", "superseded"})
        )
        strike_body = not body_is_struck and not status_retires_refinement
        updated_tags = list(target.tags)
        if encode_shadow_status:
            updated_tags.append("status:shadow")
        if encode_temporal_tag:
            updated_tags.append(temporal_tag)

        # Strike the old body in the markdown, ANCHORED to this entry's {id} region
        # (bug_009 — never strike a byte-identical body belonging to another entry).
        # An empty body has no markdown to wrap and (unlike supersede_entry) no
        # #superseded-by successor tag to fall back on, so the helper inserts a
        # durable ``~~~~`` sentinel — otherwise a rebuild would revive it
        # (merged_bug_002a). The exact retirement timestamp is encoded in the
        # same atomic Markdown write, closing the file/DB race and making a
        # fresh restore lossless. Bump ``updated`` and refresh the files row in
        # the same write (merged_bug_002b).
        if strike_body or encode_temporal_tag or encode_shadow_status:
            text = path.read_text()
            if encode_shadow_status:
                text = _append_entry_heading_tag(
                    text,
                    entry_id=entry_id,
                    tag="status:shadow",
                )
            if encode_temporal_tag:
                text = _append_entry_heading_tag(
                    text,
                    entry_id=entry_id,
                    tag=temporal_tag,
                )
            if strike_body:
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
        # Keep the encoded tag in the incremental projection too, so it is
        # already byte-equivalent to the next rebuild.
        conn.execute("UPDATE entries SET tags=? WHERE id=?", (" ".join(updated_tags), entry_id))
        derived_retire_rows(conn, entry_id=entry_id, ts=ts)

        evo_shadow.after_write(conn, name=name, entry_ids=[entry_id])


class _RebuildSourceChanged(RuntimeError):
    """A Markdown source changed between rebuild preflight and ingestion."""


def _retryable_rebuild_error(exc: BaseException) -> bool:
    if isinstance(exc, _RebuildSourceChanged):
        return True
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int) and (code & 0xFF) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return True
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def rebuild_index(conn: sqlite3.Connection) -> tuple[int, int]:
    max_attempts = 5
    for attempt in range(1, max_attempts + 1):
        try:
            return _rebuild_index_once(conn)
        except BaseException as exc:
            if attempt == max_attempts or not _retryable_rebuild_error(exc):
                raise
            logger.warning(
                "rebuild source changed or was busy; retrying (%d/%d)",
                attempt + 1,
                max_attempts,
            )
            time.sleep(min(0.05 * attempt, 0.2))
    raise AssertionError("unreachable")


def _rebuild_index_once(conn: sqlite3.Connection) -> tuple[int, int]:
    savepoint = "persome_rebuild_index"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        if evo_inversion.evomem_active():
            result = _rebuild_from_evo_nodes(conn)
        else:
            result = _rebuild_from_markdown(conn)
        # Rebuild is a reconciliation boundary. Derived rows for source entries
        # that no longer exist must not retain deleted personal text or vectors.
        conn.execute(
            "DELETE FROM entry_retrieval_stats WHERE entry_id NOT IN (SELECT id FROM entries)"
        )
        for table in ("entry_vectors", "vector_queue"):
            conn.execute(
                f"DELETE FROM {table} "
                "WHERE entry_id NOT IN (SELECT id FROM entries WHERE superseded=0)"
            )
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except BaseException:
        # ``cursor`` connections autocommit, so the savepoint is what keeps a
        # failed rebuild from leaving an empty or half-populated projection.
        with contextlib.suppress(sqlite3.Error):
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        with contextlib.suppress(sqlite3.Error):
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
    return result


def _encoded_tag_value(tags: list[str], prefix: str) -> str | None:
    """Return the last non-empty projection override for ``prefix``.

    Projected Markdown uses colon tags for temporal bounds that differ from
    the values implied by the entry heading / supersede chain. Match the
    projector's last-tag-wins behavior while treating an empty override as
    absent.
    """
    value: str | None = None
    for tag in tags:
        if tag.startswith(prefix):
            value = tag.split(":", 1)[1] or None
    return value


def _prior_valid_until(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        row["entry_id"]: row["valid_until"]
        for row in conn.execute(
            "SELECT entry_id, valid_until FROM entry_temporal WHERE valid_until IS NOT NULL"
        ).fetchall()
    }


def _temporal_source_state(
    entry: files_mod.ParsedEntry,
) -> tuple[str, str | None, int, str | None, str | None]:
    """Return the lightweight Markdown fields that determine temporal bounds."""
    return (
        entry.timestamp,
        entry.superseded_by,
        _superseded_from_tags(entry),
        _encoded_tag_value(entry.tags, "valid-from:"),
        _encoded_tag_value(entry.tags, "valid-until:"),
    )


def _markdown_temporal_bounds(
    sources: list[tuple[Path, str]],
    *,
    conn: sqlite3.Connection,
    external_timestamp_by_id: dict[str, str] | None = None,
) -> tuple[
    dict[str, tuple[str, str | None]],
    dict[str, tuple[str, str | None, int, str | None, str | None]],
]:
    """Derive temporal bounds from a complete set of Markdown sources.

    ``external_timestamp_by_id`` supplies canonical evomem nodes when this is
    used for direct-Markdown fallbacks (for example event logs). It also lets us
    reject an ID collision before destructive projection deletes.
    """
    timestamp_by_id = dict(external_timestamp_by_id or {})
    source_by_id = {entry_id: "the canonical evomem store" for entry_id in timestamp_by_id}
    source_state_by_id: dict[str, tuple[str, str | None, int, str | None, str | None]] = {}
    for path, _prefix in sources:
        parsed = files_mod.read_file(path)
        for entry in parsed.entries:
            previous_source = source_by_id.get(entry.id)
            if previous_source is not None:
                raise ValueError(
                    f"duplicate entry id {entry.id!r} in {previous_source!r} and {path.name!r}"
                )
            source_by_id[entry.id] = path.name
            timestamp_by_id[entry.id] = entry.timestamp
            source_state_by_id[entry.id] = _temporal_source_state(entry)

    # Delay the first side-table read until after the filesystem preflight, so
    # ordinary concurrent writes do not stale a long-lived WAL read snapshot.
    prior_valid_until = _prior_valid_until(conn)
    temporal_by_id: dict[str, tuple[str, str | None]] = {}
    for entry_id, state in source_state_by_id.items():
        timestamp, superseded_by, superseded, explicit_from, explicit_until = state
        valid_from = explicit_from or timestamp
        successor_until = timestamp_by_id.get(superseded_by) if superseded_by else None
        if explicit_until is not None:
            valid_until = explicit_until
        elif successor_until is not None:
            valid_until = successor_until
        elif superseded and entry_id in prior_valid_until:
            # Covers legacy orphan strikes and damaged/dangling chains. In
            # both cases the side table is the only remaining exact bound.
            valid_until = prior_valid_until[entry_id]
        elif superseded:
            valid_until = timestamp
            if superseded_by:
                logger.warning(
                    "rebuild: entry %s points to missing successor %s and has no "
                    "recoverable retirement time; using its heading timestamp",
                    entry_id,
                    superseded_by,
                )
            else:
                logger.warning(
                    "rebuild: entry %s has no recoverable retirement time; "
                    "using its heading timestamp",
                    entry_id,
                )
        else:
            valid_until = None
        temporal_by_id[entry_id] = (valid_from, valid_until)
    return temporal_by_id, source_state_by_id


def _ingest_markdown_file(
    conn: sqlite3.Connection,
    path: Path,
    prefix: str,
    *,
    temporal_by_id: dict[str, tuple[str, str | None]] | None = None,
    expected_source_state: (
        dict[str, tuple[str, str | None, int, str | None, str | None]] | None
    ) = None,
    supersedes_by_id: dict[str, set[str]] | None = None,
) -> int:
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
        if expected_source_state is not None:
            expected = expected_source_state.get(e.id)
            if expected is None or _temporal_source_state(e) != expected:
                raise _RebuildSourceChanged(f"Markdown source {path.name!r} changed during rebuild")
        superseded = _superseded_from_tags(e)
        # Only strip the strike markers off entries we judged superseded —
        # a refined-from / live entry keeps its body verbatim so a legitimate
        # inline ``~~strike~~`` in the prose is preserved (EVO-02).
        content = _fts_content_from_markdown_entry(
            e,
            superseded=superseded,
            supersedes=(supersedes_by_id or {}).get(e.id, set()),
        )
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
        valid_from, valid_until = (
            temporal_by_id[e.id]
            if temporal_by_id is not None
            else (e.timestamp, e.timestamp if superseded else None)
        )
        conn.execute(
            "INSERT INTO entry_temporal(entry_id, valid_from, valid_until) VALUES (?, ?, ?)",
            (e.id, valid_from, valid_until),
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
    # Parse every valid source before deleting the derived projection. Besides
    # avoiding a partial wipe on a read/duplicate failure, the global pass is
    # required because a superseded entry may point to a successor in another
    # Markdown file.
    memory_files = files_mod.list_memory_files()
    source_names = {path.name for path in memory_files}
    sources: list[tuple[Path, str]] = []
    for path in memory_files:
        try:
            prefix = _ensure_prefix(path.name)
        except ValueError as exc:
            logger.warning("skipping %s: %s", path.name, exc)
            continue
        sources.append((path, prefix))

    # Preserve the last known retirement bound only when Markdown cannot
    # reconstruct one. Resolvable chain entries never use this fallback: their
    # successor timestamp repairs even a previously corrupted side-table value.
    temporal_by_id, expected_source_state = _markdown_temporal_bounds(
        sources,
        conn=conn,
    )
    supersedes_by_id = _supersedes_by_id(expected_source_state)

    conn.execute("DELETE FROM entry_temporal")
    conn.execute("DELETE FROM entry_metadata")
    conn.execute("DELETE FROM entries")
    conn.execute("DELETE FROM files")
    entry_count = 0
    for path, prefix in sources:
        entry_count += _ingest_markdown_file(
            conn,
            path,
            prefix,
            temporal_by_id=temporal_by_id,
            expected_source_state=expected_source_state,
            supersedes_by_id=supersedes_by_id,
        )
    if entry_count != len(temporal_by_id):
        raise _RebuildSourceChanged("Markdown entries changed during rebuild")
    if {path.name for path in files_mod.list_memory_files()} != source_names:
        raise _RebuildSourceChanged("Markdown file set changed during rebuild")
    return len(sources), entry_count


def _rebuild_from_evo_nodes(conn: sqlite3.Connection) -> tuple[int, int]:
    from ..evomem.models import MemoryStatus
    from ..evomem.store import _row_to_node
    from . import projector

    groups: dict[str, list] = {}
    for r in conn.execute(
        "SELECT * FROM evo_nodes WHERE user_id='default' AND agent_id='default' ORDER BY node_id"
    ).fetchall():
        node = _row_to_node(r)
        if not node.file_name:
            continue
        groups.setdefault(node.file_name, []).append(node)

    group_prefixes: dict[str, str] = {}
    external_timestamp_by_id: dict[str, str] = {}
    for file_name, nodes in groups.items():
        try:
            group_prefixes[file_name] = _ensure_prefix(file_name)
        except ValueError as exc:
            logger.warning("skipping evo file group %s: %s", file_name, exc)
            continue
        for node in nodes:
            external_timestamp_by_id[node.node_id] = projector._heading_ts(node)

    memory_files = files_mod.list_memory_files()
    source_names = {path.name for path in memory_files}
    markdown_sources: list[tuple[Path, str]] = []
    for path in memory_files:
        if path.name in groups:
            continue
        try:
            prefix = _ensure_prefix(path.name)
        except ValueError as exc:
            logger.warning("skipping %s: %s", path.name, exc)
            continue
        markdown_sources.append((path, prefix))
    markdown_temporal, expected_source_state = _markdown_temporal_bounds(
        markdown_sources,
        conn=conn,
        external_timestamp_by_id=external_timestamp_by_id,
    )
    markdown_supersedes_by_id = _supersedes_by_id(expected_source_state)

    conn.execute("DELETE FROM entry_temporal")
    conn.execute("DELETE FROM entry_metadata")
    conn.execute("DELETE FROM entries")
    file_count = 0
    entry_count = 0
    for file_name in sorted(group_prefixes):
        prefix = group_prefixes[file_name]
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
                content=_fts_content_without_provenance(
                    n.content,
                    supersedes=set(n.supersedes),
                ),
                superseded=superseded,
            )
            conn.execute(
                "INSERT INTO entry_temporal(entry_id, valid_from, valid_until) VALUES (?, ?, ?)",
                (
                    n.node_id,
                    n.valid_from or projector._heading_ts(n),
                    n.valid_until,
                ),
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

    markdown_entry_count = 0
    for path, prefix in markdown_sources:
        count = _ingest_markdown_file(
            conn,
            path,
            prefix,
            temporal_by_id=markdown_temporal,
            expected_source_state=expected_source_state,
            supersedes_by_id=markdown_supersedes_by_id,
        )
        markdown_entry_count += count
        entry_count += count
        file_count += 1
    if markdown_entry_count != len(markdown_temporal):
        raise _RebuildSourceChanged("Markdown entries changed during rebuild")
    if {path.name for path in files_mod.list_memory_files()} != source_names:
        raise _RebuildSourceChanged("Markdown file set changed during rebuild")

    # the file-level metadata truth). But a row that once had nodes, now has NONE
    # in evo_nodes (all moved out / file deleted) AND is not on disk falls into
    # neither `groups` nor `list_memory_files()` above — so its stale entry_count
    # never gets refreshed and the row lingers forever. Prune those orphan rows so
    # rebuild stays idempotent and converges to the truth state.
    valid_names = set(groups) | source_names
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
_SUPERSEDES_COMMENT_RE = re.compile(
    r"(?:\n)?<!-- supersedes: (?P<old_id>[a-zA-Z0-9-]+); "
    r"reason: (?:(?!-->).)* -->\Z",
    re.DOTALL,
)


def _body_is_striked(body: str) -> bool:
    stripped = body.strip()
    return stripped.startswith("~~") and stripped.endswith("~~")


def _superseded_from_tags(e: files_mod.ParsedEntry) -> int:
    """Superseded judgment from an entry's Markdown projection (EVO-02).

        if explicit non-active status: 1   (retired refinement / archive)
        elif superseded-by:             1   (retired by a successor)
        elif refined-from:              0   (refinement forced live)
        else: whole-body strike         1   (orphan retirement fallback)

    A status override is the durable deletion signal for a refined head; without
    it, ``refined-from`` still short-circuits before the strike fallback so
    legitimate struck prose remains live.
    """
    status = _encoded_tag_value(e.tags, "status:")
    if status in {"shadow", "archived", "superseded"}:
        return 1
    if e.superseded_by:
        return 1
    if e.refined_from:
        return 0
    return 1 if _body_is_striked(e.body) else 0


def _strip_strike(body: str) -> str:
    if _body_is_striked(body):
        # Retirement wraps the complete body in one outer ``~~`` pair. Remove
        # only that pair so legitimate inline strike Markdown remains intact.
        stripped = body.strip()
        return stripped[2:-2]
    return body


def _content_from_markdown_entry(e: files_mod.ParsedEntry, *, superseded: int) -> str:
    status = _encoded_tag_value(e.tags, "status:")
    status_retired_refinement = (
        bool(e.refined_from)
        and not e.superseded_by
        and status in {"shadow", "archived", "superseded"}
    )
    if superseded and not status_retired_refinement:
        return _strip_strike(e.body)
    return e.body


def _fts_content_from_markdown_entry(
    e: files_mod.ParsedEntry,
    *,
    superseded: int,
    supersedes: set[str],
) -> str:
    content = _content_from_markdown_entry(e, superseded=superseded)
    return _fts_content_without_provenance(content, supersedes=supersedes)


def _fts_content_without_provenance(content: str, *, supersedes: set[str]) -> str:
    match = _SUPERSEDES_COMMENT_RE.search(content)
    if match is not None and match.group("old_id") in supersedes:
        return content[: match.start()]
    return content


def _supersedes_by_id(
    source_state_by_id: dict[str, tuple[str, str | None, int, str | None, str | None]],
) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for old_id, (
        _timestamp,
        successor,
        _superseded,
        _valid_from,
        _valid_until,
    ) in source_state_by_id.items():
        if successor:
            out.setdefault(successor, set()).add(old_id)
    return out


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

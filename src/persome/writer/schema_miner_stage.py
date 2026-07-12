"Runtime stage that mines and persists stable schema Faces."

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..config import Config
from ..evomem.schema_miner import SchemaMiner, SchemaResult
from ..logger import get
from ..store import entries as entries_mod
from ..store import files as files_mod
from ..store import schema_faces
from . import llm as llm_mod

logger = get("persome.writer")

# Prefixes whose entries are durable user *facts* — the raw material a schema is
# induced from. Aligned with the canonical fact set in ``retrieval/layered.py``.
# ``event-*`` (raw activity) and ``skill-*`` / ``schema-*`` (derived artefacts)
# are deliberately excluded: a schema must
# generalise grounded facts, not re-abstract other abstractions.
_FACT_PREFIXES: tuple[str, ...] = ("user", "project", "topic", "person", "org", "tool")

# Minimum facts in a cluster before it's worth inducing a schema (Hy-Memory's
# MIN_FACTS_FOR_INDUCTION). Fewer facts can't support a falsifiable generalisation.
_DEFAULT_MIN_FACTS = 4

# Confidence at/above which a freshly mined schema is born ``stable`` (and thus
# eligible for active model reads). Below it the schema is ``forming`` — it exists
# and is grep-able, but stays out of snapshots until more evidence promotes it.
_DEFAULT_STABLE_THRESHOLD = 0.6

# How many durable entries to pull per file when clustering. A generous cap — a
# single topic file rarely holds more facts than this, and the miner prompt is
# bounded by the model context anyway.
_MAX_FACTS_PER_FILE = 40

_INFERENCES_MARKER = "inferences:"


@dataclass
class FactBundle:
    """One cluster of related facts, tagged with the source it was drawn from.

    ``source_path`` is the memory filename the facts came from (e.g.
    ``project-x.md``); it derives the schema's stable slug so a re-mine updates
    one file rather than spawning new ones. ``facts`` are the entry bodies.
    """

    source_path: str
    facts: list[str]


@dataclass
class WrittenSchema:
    """One schema the stage induced and persisted this run."""

    path: str
    status: str
    confidence: float
    expected_inferences: list[str] = field(default_factory=list)
    updated_in_place: bool = False  # True when an existing schema was superseded


@dataclass
class SchemaRunResult:
    """Outcome of one :func:`mine_bundles_and_write` call."""

    written: list[WrittenSchema] = field(default_factory=list)
    skipped_small: int = 0  # bundles dropped for < min_facts
    skipped_empty: int = 0  # miner returned no usable proposition

    @property
    def written_count(self) -> int:
        return len(self.written)


# ── body rendering / parsing (shared with model.schema_reader) ───────────────


def render_schema_body(
    *,
    central_proposition: str,
    supporting_summary: str,
    expected_inferences: list[str],
) -> str:
    """Render a schema entry body for :mod:`persome.model.schema_reader`.

    Layout (design §3.3) — ``central``/``summary`` one-liners, then an
    ``inferences:`` marker followed by ``- `` bullets, one inference per line::

        central: The user consistently prefers minimal tooling.
        summary: They repeatedly choose uv and ruff over heavier frameworks.
        inferences:
        - They are likely to reject a large framework or SDK.
        - They will evaluate new tools partly by dependency size.
    """
    lines = [
        f"central: {central_proposition.strip()}",
        f"summary: {supporting_summary.strip()}",
        _INFERENCES_MARKER,
    ]
    lines.extend(f"- {inf.strip()}" for inf in expected_inferences if inf.strip())
    return "\n".join(lines)


def parse_expected_inferences(body: str) -> list[str]:
    """Inverse of :func:`render_schema_body` — pull the ``- `` inference bullets.

    Only the bullets *after* the ``inferences:`` marker are returned, so a stray
    ``- `` inside ``central``/``summary`` prose can't leak in. Tolerant of a body
    that has been through a markdown round-trip (leading/trailing whitespace).
    """
    out: list[str] = []
    in_block = False
    for raw in body.splitlines():
        line = raw.strip()
        if not in_block:
            if line.lower() == _INFERENCES_MARKER:
                in_block = True
            continue
        if line.startswith("- "):
            text = line[2:].strip()
            if text:
                out.append(text)
        elif line and not line.startswith("-"):
            # A non-bullet, non-empty line ends the inference block.
            break
    return out


def collect_fact_bundles(
    conn: sqlite3.Connection,
    *,
    min_facts: int = _DEFAULT_MIN_FACTS,
    from_evomem: bool = False,
) -> list[FactBundle]:
    """Assemble related fact sets by clustering durable entries per file.

    Each ``user-/project-/topic-/person-*.md`` file contributes one
    :class:`FactBundle` of its non-superseded entry bodies, tagged with the source
    path. Bundles with fewer than ``min_facts`` entries are dropped (an
    under-supported cluster yields noise, not a schema). Returns the bundles in a
    deterministic order (by file path) so a run is reproducible.

    ``from_evomem`` (Memory-rebuild, spec 2026-07-04 §1): read the **rebuild truth
    layer** ``evo_nodes`` (where delta-apply + assertions land) instead of the
    retiring ``entries`` FTS projection. Under ``markdown`` write authority
    ``add_direct`` writes evo_nodes only, so a schema mine off ``entries`` never
    sees the delta rebuild — this flag points the miner at the actual facts.
    """
    conn.row_factory = sqlite3.Row
    rows = None
    if from_evomem:
        like = " OR ".join(f"file_name LIKE '{p}-%'" for p in _FACT_PREFIXES)
        try:
            rows = conn.execute(
                f"SELECT file_name AS path, content FROM evo_nodes "
                f"WHERE is_latest = 1 AND status = 'active' AND ({like}) "
                "AND tags NOT LIKE '%person-event%' "
                "AND tags NOT LIKE '%person-entity%' "
                f"ORDER BY file_name, gmt_created"
            ).fetchall()
        except Exception:  # noqa: BLE001 — evo_nodes
            rows = None
    if rows is None:
        placeholders = ",".join("?" * len(_FACT_PREFIXES))
        rows = conn.execute(
            f"SELECT path, content FROM entries "
            f"WHERE prefix IN ({placeholders}) AND superseded = 0 "
            f"ORDER BY path, timestamp",
            _FACT_PREFIXES,
        ).fetchall()

    by_file: dict[str, list[str]] = {}
    for row in rows:
        body = (row["content"] or "").strip()
        if not body:
            continue
        by_file.setdefault(row["path"], []).append(body)

    bundles: list[FactBundle] = []
    for path in sorted(by_file):
        facts = by_file[path][:_MAX_FACTS_PER_FILE]
        if len(facts) >= min_facts:
            bundles.append(FactBundle(source_path=path, facts=facts))
    return bundles


# ── mining + landing (fork-independent: consumes pre-assembled bundles) ───────


def _build_llm_call(cfg: Config) -> Callable[[list[dict]], Any]:
    """Wrap acme's ``call_llm`` into the ``llm_call(messages) -> resp`` the miner wants."""

    def _call(messages: list[dict]) -> Any:
        return llm_mod.call_llm(cfg, "schema_miner", messages=messages, json_mode=True)

    return _call


def schema_name_for(source_path: str) -> str:
    """Map a source fact-file path to its stable schema filename.

    ``project-x.md`` → ``schema-project-x.md``. Deriving the slug from the source
    (not the LLM-written proposition, which can drift run to run) is what makes
    re-mining idempotent: the same cluster always lands in the same file.
    """
    stem = source_path.split("/")[-1].removesuffix(".md")
    return f"schema-{stem}.md"


def _status_for(confidence: float, stable_threshold: float) -> str:
    return "stable" if confidence >= stable_threshold else "forming"


def _latest_entry_id(name: str) -> str | None:
    """Return the id of ``name``'s most recent non-superseded entry, if any."""
    parsed = files_mod.read_file(files_mod.memory_path(name))
    live = [e for e in parsed.entries if not e.superseded_by]
    return live[-1].id if live else None


def _persist_schema(
    conn: sqlite3.Connection,
    source_path: str,
    result: SchemaResult,
    *,
    stable_threshold: float,
) -> WrittenSchema | None:
    """Land one mined schema into its source-derived ``schema-*.md`` file.

    Idempotent: if the schema file already has a live entry, that entry is
    **superseded** by the freshly mined one (re-mine = update in place); otherwise
    the file is created and the first entry appended. Returns ``None`` when the
    miner produced nothing usable (no central proposition) — we never write an
    empty schema rather than fabricating a pattern.
    """
    central = result.central_proposition.strip()
    if not central:
        return None

    status = _status_for(result.confidence, stable_threshold)
    name = schema_name_for(source_path)
    body = render_schema_body(
        central_proposition=central,
        supporting_summary=result.supporting_summary,
        expected_inferences=result.expected_inferences,
    )
    tags = ["schema", status, f"confidence:{result.confidence:.2f}"]
    # A still-``forming`` schema is born ``dormant`` so it stays out of default
    # ``list_memories`` and active model reads until it matures; a ``stable``
    # schema is ``active`` (design §5 / MCP-05, issue #440).
    file_status = "dormant" if status == "forming" else "active"

    path = files_mod.memory_path(name)
    updated_in_place = False
    if path.exists():
        # Re-mine: supersede the current head so the file holds one live schema,
        # the prior attitude preserved as a struck predecessor (auditable §2.4).
        old_id = _latest_entry_id(name)
        if old_id is not None:
            entries_mod.supersede_entry(
                conn,
                name=name,
                old_entry_id=old_id,
                new_content=body,
                reason="re-mined from refreshed fact cluster",
                tags=tags,
            )
            updated_in_place = True
        else:
            entries_mod.append_entry(conn, name=name, content=body, tags=tags)
        # Promote/demote visibility to match the re-mined maturity: a forming
        # schema that matured to stable flips dormant→active (and the inverse), in
        # both frontmatter + files table so it survives rebuild_index (issue #440).
        entries_mod.set_file_status(conn, name=name, status=file_status)
    else:
        entries_mod.create_file(
            conn,
            name=name,
            description=central[:120],
            tags=["schema", status],
            status=file_status,
        )
        entries_mod.append_entry(conn, name=name, content=body, tags=tags)

    logger.info(
        "schema mined: %s status=%s conf=%.2f%s",
        name,
        status,
        result.confidence,
        " (updated)" if updated_in_place else "",
    )
    return WrittenSchema(
        path=name,
        status=status,
        confidence=result.confidence,
        expected_inferences=list(result.expected_inferences),
        updated_in_place=updated_in_place,
    )


def _face_anchors(
    cfg: Config, source_path: str, signature: str, facts: list[str] | None = None
) -> list[str]:
    """Entity anchors for a mined face (§7-6 graph projection) — the hull
    vertices the view renders the face over. Three sources, unioned:

    - the source file's own entity (a Face mined from ``person-alex.md`` is
      about Alex; a ``user-*`` Face anchors to ``self``);
    - every roster identity named in the signature OR in the footprint fact
      BODIES when the schema is not explicitly about the owner. Owner-scoped
      schemas anchor to ``self`` instead of every collaborator mentioned in
      their evidence;
    - the same ``scan_mentions`` knife the read path uses (§4.3 one funnel).

    Best-effort: an empty list just means the face renders as a tower plate."""
    anchors: set[str] = set()
    try:
        stem = source_path.removesuffix(".md")
        # Anchor the schema to its SOURCE entity — the node it was mined from — so a
        # project/tool/topic schema sits ON its project/tool node instead of floating
        # (spec 2026-07-04 §face-anchor gap: only person-/org-/user- were anchored, so
        # every project-mined behaviour schema had 0 anchors → drew as a floating plate).
        # The stem-after-prefix is the graph node id for these kinds.
        for prefix in ("person-", "org-", "project-", "tool-", "topic-"):
            if stem.startswith(prefix):
                anchors.add(stem.removeprefix(prefix))

        # it is ABOUT the user, so it belongs on the USER hub (§1.5 same-component invariant).
        sig = (signature or "").strip()
        owner_scoped = (
            stem.startswith("user-")
            or sig.startswith("\u7528\u6237")
            or sig.startswith("\u8be5\u7528\u6237")
            or sig.lower().startswith(("user", "the user", "this person"))
        )
        if owner_scoped:
            anchors.add("self")
        from ..evomem import identity as identity_mod

        roster = identity_mod.load_roster(cfg)
        hay = "\n".join([signature, *(facts or [])])
        if not owner_scoped:
            anchors.update(identity_mod.scan_mentions(hay, roster))
    except Exception:  # noqa: BLE001 — anchors decorate, never block the mine
        logger.debug("face anchor derivation failed for %s", source_path, exc_info=True)
    return sorted(anchors)


def mine_bundles_and_write(
    cfg: Config,
    conn: sqlite3.Connection,
    fact_bundles: list[FactBundle],
    *,
    min_facts: int = _DEFAULT_MIN_FACTS,
    stable_threshold: float = _DEFAULT_STABLE_THRESHOLD,
    llm_call: Callable[[list[dict]], Any] | None = None,
) -> SchemaRunResult:
    """Feed each fact bundle to the miner and persist the resulting schemas.

    ``llm_call`` is injectable for tests (a fake that returns a canned response,
    as in ``test_schema_miner.py``); production passes ``None`` and the stage
    wires acme's ``call_llm`` for the ``schema_miner`` stage. Each bundle is
    independent — a miner failure on one bundle is logged and skipped, never
    aborting the rest (design §2.2: schema is a decoration, must not cascade).
    """
    miner = SchemaMiner(llm_call=llm_call if llm_call is not None else _build_llm_call(cfg))
    run = SchemaRunResult()

    for bundle in fact_bundles:
        if len(bundle.facts) < min_facts:
            run.skipped_small += 1
            continue
        try:
            result = miner.mine_schema(bundle.facts)
        except Exception:  # pragma: no cover - defensive; a bad bundle can't kill the run
            logger.exception("schema miner raised on %s; skipping", bundle.source_path)
            run.skipped_empty += 1
            continue
        if not result.success:
            run.skipped_empty += 1
            continue
        written = _persist_schema(
            conn, bundle.source_path, result, stable_threshold=stable_threshold
        )
        if written is None:
            run.skipped_empty += 1
        else:
            run.written.append(written)
            # §4.5 unified schema object: the mined contribution lands on the
            # schema_faces row too (signature route — the miner induced the
            # central proposition; the fact bundle is the footprint). Shadow-only
            # SQLite write, fail-open: a faces failure never blocks the mine.
            try:
                face_id = schema_faces.record_face(
                    conn,
                    source=schema_faces.PROVENANCE_MINED,
                    signature=result.central_proposition,
                    members=[schema_faces.member_key(f) for f in bundle.facts],
                    confidence=result.confidence,
                    anchors=_face_anchors(
                        cfg, bundle.source_path, result.central_proposition, bundle.facts
                    ),
                )
                schema_faces.maybe_promote(conn, face_id)
            except Exception:
                logger.exception("schema_faces record failed for %s", bundle.source_path)
    return run


def mine_schemas_for_user(
    cfg: Config,
    conn: sqlite3.Connection,
    *,
    min_facts: int = _DEFAULT_MIN_FACTS,
    stable_threshold: float = _DEFAULT_STABLE_THRESHOLD,
    llm_call: Callable[[list[dict]], Any] | None = None,
) -> SchemaRunResult:
    """Top-level, testable entry point: collect fact bundles, mine, and land them.

    This is the function a scheduled tick or manual model build calls. It
    wires :func:`collect_fact_bundles` (the per-file MVP clustering) into
    :func:`mine_bundles_and_write`. Wiring it into the daemon registry is a later
    step — keeping it a plain function here makes the whole chain unit-testable
    with an injected ``llm_call`` and no daemon.
    """

    from_evomem = bool(getattr(getattr(cfg, "memory_delta", None), "apply_enabled", False))
    bundles = collect_fact_bundles(conn, min_facts=min_facts, from_evomem=from_evomem)
    return mine_bundles_and_write(
        cfg,
        conn,
        bundles,
        min_facts=min_facts,
        stable_threshold=stable_threshold,
        llm_call=llm_call,
    )

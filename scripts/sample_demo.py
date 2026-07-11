"""Serve a disposable synthetic Persome model without reading personal data."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import tempfile
import threading
import webbrowser
from datetime import UTC, datetime, timedelta
from pathlib import Path

_SHOWCASE_FACES = (
    (
        "focused-planning",
        "focused planning",
        "Focused work begins with a small, inspectable plan.",
    ),
    (
        "deep-work",
        "deep work",
        "Protected focus blocks are reserved for difficult creative work.",
    ),
    (
        "build-verify",
        "implementation",
        "Implementation advances through short build-and-verify loops.",
    ),
    (
        "primary-sources",
        "source review",
        "Research claims are checked against primary evidence.",
    ),
    (
        "compare-options",
        "option comparison",
        "Competing approaches are compared before commitment.",
    ),
    (
        "explicit-uncertainty",
        "uncertainty handling",
        "Uncertain conclusions remain explicit and revisable.",
    ),
    (
        "architecture-review",
        "architecture review",
        "Architecture decisions are reviewed with collaborators.",
    ),
    (
        "clear-handoffs",
        "project handoffs",
        "Handoffs include context, ownership, and validation criteria.",
    ),
    (
        "durable-decisions",
        "decision recording",
        "Decisions are recorded close to the work they change.",
    ),
    (
        "local-context",
        "context handling",
        "Personal context stays local unless a trusted tool needs it.",
    ),
    (
        "auditable-agents",
        "agent review",
        "Agent outputs are grounded in inspectable evidence receipts.",
    ),
    (
        "model-correction",
        "model correction",
        "The personal model improves through correction, not silent overwrite.",
    ),
)

_SHOWCASE_VOLUMES = (
    "Focused craft connects planning, deep work, and iterative validation.",
    "Evidence-driven research combines source checks, alternatives, and uncertainty.",
    "Collaborative execution connects review, handoffs, and durable decisions.",
    "Trustworthy personal AI unifies local context, receipts, and correction.",
)


def _entry_time(entry_id: str) -> str:
    value = datetime.strptime(entry_id[:13], "%Y%m%d-%H%M").replace(tzinfo=UTC)
    return value.isoformat()


def _seed_showcase(
    conn,
    *,
    node_store,
    extra_face_members: dict[str, list[str]] | None = None,
) -> None:
    """Build dense, deterministic geometry for the public README screenshot."""
    from persome.evomem.models import MemoryLayer, MemoryNode
    from persome.store import entries, schema_faces

    extra_face_members = extra_face_members or {}

    def memory_node(entry_id: str, content: str, file_name: str, tags: list[str]) -> MemoryNode:
        timestamp = _entry_time(entry_id)
        return MemoryNode(
            node_id=entry_id,
            content=content,
            layer=MemoryLayer.L2_FACT,
            file_name=file_name,
            tags=" ".join(tags),
            occurred_at=timestamp,
            valid_from=timestamp,
            gmt_created=datetime.fromisoformat(timestamp),
        )

    face_ids: list[str] = []
    for face_index, (slug, activity, signature) in enumerate(_SHOWCASE_FACES):
        file_name = f"project-synthetic-{slug}.md"
        tags = ["synthetic", "showcase", slug]
        entries.create_file(
            conn,
            name=file_name,
            description=f"Synthetic {activity} observations for the public showcase.",
            tags=tags,
        )

        chain_heads: list[str] = []
        for chain_index in range(6):
            observation = chain_index + 1
            revisions = (
                f"Synthetic observation {observation}: {activity} begins with a visible checkpoint.",
                f"Synthetic observation {observation}: {activity} adds a written next step after the checkpoint.",
                f"Synthetic observation {observation}: {activity} consistently connects the checkpoint to a written next step and a verification cue.",
            )
            previous_id = entries.append_entry(
                conn,
                name=file_name,
                content=revisions[0],
                tags=tags,
            )
            node_store.save(memory_node(previous_id, revisions[0], file_name, tags))
            for revision_index, content in enumerate(revisions[1:], start=2):
                next_id = entries.supersede_entry(
                    conn,
                    name=file_name,
                    old_entry_id=previous_id,
                    new_content=content,
                    reason=f"Synthetic revision {revision_index} for the public showcase.",
                    tags=tags,
                )
                node_store.save_and_supersede(
                    memory_node(next_id, content, file_name, tags),
                    old_id=previous_id,
                    old_valid_until=_entry_time(next_id),
                )
                previous_id = next_id
            chain_heads.append(previous_id)

        for standalone_index in range(17):
            content = (
                f"Synthetic observation {standalone_index + 6}: {activity} favors a clear "
                f"checkpoint, bounded scope, and reviewable outcome {face_index + 1}."
            )
            entry_id = entries.append_entry(
                conn,
                name=file_name,
                content=content,
                tags=tags,
            )
            node_store.save(memory_node(entry_id, content, file_name, tags))

        face_members = [*chain_heads, *extra_face_members.get(slug, [])]
        face_id = schema_faces.record_face(
            conn,
            source=schema_faces.PROVENANCE_MINED,
            signature=signature,
            members=face_members,
            confidence=round(0.86 + (face_index % 4) * 0.02, 2),
            anchors=["self", slug],
        )
        schema_faces.record_face(
            conn,
            source=schema_faces.PROVENANCE_EMERGENT,
            signature=signature,
            members=face_members,
            confidence=round(0.92 + (face_index % 3) * 0.02, 2),
            anchors=["self", slug],
        )
        if not schema_faces.maybe_promote(conn, face_id):
            raise RuntimeError(f"showcase Face did not promote: {face_id}")
        face_ids.append(face_id)

    volume_ids: list[str] = []
    for volume_index, signature in enumerate(_SHOWCASE_VOLUMES):
        members = face_ids[volume_index * 3 : (volume_index + 1) * 3]
        volume_id = schema_faces.record_face(
            conn,
            source=schema_faces.PROVENANCE_MINED,
            signature=signature,
            members=members,
            confidence=round(0.9 + volume_index * 0.01, 2),
            level=2,
            anchors=["self", f"showcase-volume-{volume_index + 1}"],
        )
        schema_faces.record_face(
            conn,
            source=schema_faces.PROVENANCE_EMERGENT,
            signature=signature,
            members=members,
            confidence=round(0.95 + volume_index * 0.01, 2),
            level=2,
            anchors=["self", f"showcase-volume-{volume_index + 1}"],
        )
        if not schema_faces.maybe_promote(conn, volume_id):
            raise RuntimeError(f"showcase Volume did not promote: {volume_id}")
        volume_ids.append(volume_id)

    schema_faces.upsert_root(
        conn,
        signature=(
            "A focused systems builder who turns personal context into auditable, "
            "collaborative, and trustworthy work."
        ),
        members=volume_ids,
        anchors=["self", "local-first", "evidence", "collaboration"],
    )


def _seed_sample_at_root(root: Path, *, showcase: bool = False) -> dict:
    """Create deterministic searchable memory with ``PERSOME_ROOT`` already set."""
    from persome.evomem.models import MemoryLayer, MemoryNode, MemoryStatus
    from persome.evomem.store import NodeStore
    from persome.store import entries, fts, schema_faces
    from persome.store import relation_edges as edges

    original_make_id = entries.make_id
    original_now_iso_minute = entries._now_iso_minute
    original_schema_now = schema_faces._now
    fixed_ids = iter(
        [
            "20260601-0800-a1b2c3",
            "20260701-0800-d4e5f6",
            "20260615-0800-112233",
            "20260620-0800-445566",
        ]
    )
    fallback_id = 0

    def sample_id(timestamp: str) -> str:
        nonlocal fallback_id
        try:
            return next(fixed_ids)
        except StopIteration:
            fallback_id += 1
            compact = timestamp.replace("-", "").replace(":", "").replace("T", "-")[:13]
            return f"{compact}-f{fallback_id:05x}"

    def sample_times():
        yield from (
            "2026-06-01T08:00",
            "2026-07-01T08:00",
            "2026-06-15T08:00",
            "2026-06-20T08:00",
        )
        current = datetime(2026, 1, 5, 8, tzinfo=UTC)
        while True:
            yield current.strftime("%Y-%m-%dT%H:%M")
            current += timedelta(hours=6)

    fixed_times = sample_times()
    entries.make_id = sample_id
    entries._now_iso_minute = lambda: next(fixed_times)
    schema_faces._now = lambda: "2026-07-10T08:00:00+00:00"

    try:
        with fts.cursor() as conn:
            entries.create_file(
                conn,
                name="project-work.md",
                description="Synthetic work preferences for the public sample.",
                tags=["project", "synthetic"],
            )
            focus_v1 = entries.append_entry(
                conn,
                name="project-work.md",
                content="The user reserved mornings for focused writing.",
                tags=["work", "focus"],
            )
            focus_v2 = entries.supersede_entry(
                conn,
                name="project-work.md",
                old_entry_id=focus_v1,
                new_content="The user reserves mornings for focused writing and review.",
                reason="A later synthetic observation added review.",
                tags=["work", "focus", "writing"],
            )
            collaboration = entries.append_entry(
                conn,
                name="project-work.md",
                content="The user reviews architecture decisions with collaborators.",
                tags=["work", "collaboration"],
            )

            entries.create_file(
                conn,
                name="project-persome.md",
                description="Synthetic Persome project context for the public sample.",
                tags=["project", "synthetic"],
            )
            runtime = entries.append_entry(
                conn,
                name="project-persome.md",
                content="The user treats Persome as a local-first personal model runtime.",
                tags=["project", "persome", "runtime"],
            )

            node_store = NodeStore()
            focus_old = MemoryNode(
                node_id=focus_v1,
                content="The user reserved mornings for focused writing.",
                layer=MemoryLayer.L2_FACT,
                file_name="project-work.md",
                tags="work focus",
                valid_from="2026-06-01T08:00:00+00:00",
                gmt_created=datetime.fromisoformat("2026-06-01T08:00:00+00:00"),
            )
            node_store.save(focus_old)
            focus_new = MemoryNode(
                node_id=focus_v2,
                content="The user reserves mornings for focused writing and review.",
                layer=MemoryLayer.L2_FACT,
                file_name="project-work.md",
                tags="work focus writing",
                valid_from="2026-07-01T08:00:00+00:00",
                gmt_created=datetime.fromisoformat("2026-07-01T08:00:00+00:00"),
            )
            node_store.save_and_supersede(
                focus_new,
                old_id=focus_v1,
                old_valid_until="2026-07-01T08:00:00+00:00",
            )
            for node_id, content, file_name, tags, timestamp in (
                (
                    collaboration,
                    "The user reviews architecture decisions with collaborators.",
                    "project-work.md",
                    "work collaboration",
                    "2026-06-15T08:00:00+00:00",
                ),
                (
                    runtime,
                    "The user treats Persome as a local-first personal model runtime.",
                    "project-persome.md",
                    "project persome runtime",
                    "2026-06-20T08:00:00+00:00",
                ),
            ):
                node_store.save(
                    MemoryNode(
                        node_id=node_id,
                        content=content,
                        layer=MemoryLayer.L2_FACT,
                        file_name=file_name,
                        tags=tags,
                        valid_from=timestamp,
                        gmt_created=datetime.fromisoformat(timestamp),
                    )
                )

            edges.add_edge(
                conn,
                edge_id="relation-self-persome",
                src_identity="self",
                dst_identity="persome",
                predicate="participates_in",
                src_kind="self",
                dst_kind="project",
                provenance="inferred",
                confidence=0.9,
                label="maintains",
                quote="The user maintains the Persome runtime.",
                valid_from="2026-06-20T08:00:00+00:00",
                created_at="2026-06-20T08:00:00+00:00",
                status=MemoryStatus.ACTIVE,
                source_kind="session",
                source_id="event:session:synthetic-1",
                source_receipt="event:session:synthetic-1:fixtures/session-1.json",
            )

            if showcase:
                _seed_showcase(
                    conn,
                    node_store=node_store,
                    extra_face_members={
                        "focused-planning": [focus_v2],
                        "architecture-review": [collaboration],
                        "local-context": [runtime],
                    },
                )
            else:
                face_ids: list[str] = []
                for signature, members in (
                    ("Focused mornings are used for writing and review.", [focus_v2, runtime]),
                    (
                        "Architecture decisions are reviewed collaboratively.",
                        [collaboration, runtime],
                    ),
                ):
                    face_id = schema_faces.record_face(
                        conn,
                        source=schema_faces.PROVENANCE_MINED,
                        signature=signature,
                        members=members,
                        confidence=0.8,
                        anchors=["self", "persome"],
                    )
                    schema_faces.record_face(
                        conn,
                        source=schema_faces.PROVENANCE_EMERGENT,
                        signature=signature,
                        members=members,
                        confidence=0.9,
                        anchors=["self", "persome"],
                    )
                    if not schema_faces.maybe_promote(conn, face_id):
                        raise RuntimeError(f"sample Face did not promote: {face_id}")
                    face_ids.append(face_id)

                volume_signature = (
                    "The user combines focused authorship with collaborative runtime stewardship."
                )
                volume_id = schema_faces.record_face(
                    conn,
                    source=schema_faces.PROVENANCE_MINED,
                    signature=volume_signature,
                    members=face_ids,
                    confidence=0.85,
                    level=2,
                    anchors=["self", "persome"],
                )
                schema_faces.record_face(
                    conn,
                    source=schema_faces.PROVENANCE_EMERGENT,
                    signature=volume_signature,
                    members=face_ids,
                    confidence=0.9,
                    level=2,
                    anchors=["self", "persome"],
                )
                if not schema_faces.maybe_promote(conn, volume_id):
                    raise RuntimeError(f"sample Volume did not promote: {volume_id}")

                schema_faces.upsert_root(
                    conn,
                    signature=(
                        "A focused builder who turns personal context into an inspectable "
                        "local model."
                    ),
                    members=[volume_id],
                    anchors=["self", "persome"],
                )
            conn.commit()

            from persome.mcp.server import _read_receipt, _search
            from persome.model import build_snapshot

            search_result = _search(
                conn,
                query="When does the user prefer focused writing?",
                top_k=2,
            )
            top_id = search_result["results"][0]["id"]
            receipt = _read_receipt(conn, entry_id=top_id)
            snapshot = build_snapshot(
                conn,
                generated_at="2026-07-10T08:00:00+00:00",
                build_metadata={
                    "build_id": "public-showcase-v1" if showcase else "public-sample-v1",
                    "status": "complete",
                    "mode": "synthetic",
                    "trigger": "sample-demo",
                },
            )
        return {"search": search_result, "receipt": receipt, "snapshot": snapshot}
    finally:
        entries.make_id = original_make_id
        entries._now_iso_minute = original_now_iso_minute
        schema_faces._now = original_schema_now


def seed_sample(root: Path, *, showcase: bool = False) -> dict:
    """Create deterministic searchable memory and complete model geometry."""
    root.mkdir(parents=True, exist_ok=True)
    original_root = os.environ.get("PERSOME_ROOT")
    evomem_logger = logging.getLogger("persome.evomem")
    original_log_level = evomem_logger.level
    os.environ["PERSOME_ROOT"] = str(root)
    evomem_logger.setLevel(logging.ERROR)
    try:
        return _seed_sample_at_root(root, showcase=showcase)
    finally:
        evomem_logger.setLevel(original_log_level)
        if original_root is None:
            os.environ.pop("PERSOME_ROOT", None)
        else:
            os.environ["PERSOME_ROOT"] = original_root


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8743)
    parser.add_argument("--no-open", action="store_true", help="Do not open the browser.")
    parser.add_argument(
        "--showcase",
        action="store_true",
        help="Render the dense synthetic geometry used by the README hero image.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the real search, receipt, and snapshot payloads, then exit.",
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="persome-public-sample-") as temporary:
        root = Path(temporary)
        result = seed_sample(root, showcase=args.showcase)
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0

        os.environ["PERSOME_ROOT"] = str(root)
        logging.getLogger("persome.evomem").setLevel(logging.ERROR)
        from persome import config as config_mod
        from persome.mcp import server as mcp_server

        cfg = config_mod.load(root / "config.toml")
        cfg.mcp.host = "127.0.0.1"
        cfg.mcp.port = args.port
        url = f"http://127.0.0.1:{args.port}/model"
        print("Persome synthetic sample is isolated from ~/.persome.")
        print(f"Viewer: {url}")
        print(f"MCP:    http://127.0.0.1:{args.port}/mcp")
        print("Stop with Ctrl-C; the temporary sample data is then deleted.")
        if not args.no_open:
            threading.Timer(0.8, lambda: webbrowser.open(url)).start()
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(mcp_server.run_async(cfg, transport="streamable-http"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

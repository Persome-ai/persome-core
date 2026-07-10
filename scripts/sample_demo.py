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
from datetime import datetime
from pathlib import Path


def seed_sample(root: Path) -> dict:
    """Create deterministic searchable memory and complete model geometry."""
    os.environ["PERSOME_ROOT"] = str(root)
    logging.getLogger("persome.evomem").setLevel(logging.ERROR)

    from persome.evomem.models import MemoryLayer, MemoryNode, MemoryStatus
    from persome.evomem.store import NodeStore
    from persome.store import entries, fts, schema_faces
    from persome.store import relation_edges as edges

    root.mkdir(parents=True, exist_ok=True)
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
    fixed_times = iter(
        [
            "2026-06-01T08:00",
            "2026-07-01T08:00",
            "2026-06-15T08:00",
            "2026-06-20T08:00",
        ]
    )
    entries.make_id = lambda _timestamp: next(fixed_ids)
    entries._now_iso_minute = lambda: next(fixed_times)
    schema_faces._now = lambda: "2026-07-10T08:00:00+00:00"

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

        face_ids: list[str] = []
        for signature, members in (
            ("Focused mornings are used for writing and review.", [focus_v2, runtime]),
            ("Architecture decisions are reviewed collaboratively.", [collaboration, runtime]),
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
            signature="A focused builder who turns personal context into an inspectable local model.",
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
                "build_id": "public-sample-v1",
                "status": "complete",
                "mode": "synthetic",
                "trigger": "sample-demo",
            },
        )
    entries.make_id = original_make_id
    entries._now_iso_minute = original_now_iso_minute
    schema_faces._now = original_schema_now
    return {"search": search_result, "receipt": receipt, "snapshot": snapshot}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8743)
    parser.add_argument("--no-open", action="store_true", help="Do not open the browser.")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the real search, receipt, and snapshot payloads, then exit.",
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="persome-public-sample-") as temporary:
        root = Path(temporary)
        result = seed_sample(root)
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0

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

"""Fresh-root Runtime path: capture ingest through model build and export."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from persome import config as config_mod
from persome.api import build_api_app
from persome.api import routes as routes_mod
from persome.capture import scheduler
from persome.model import export_snapshot, run_model_build
from persome.session import store as session_store
from persome.session.manager import SessionManager
from persome.store import fts
from persome.timeline import tick as timeline_tick

FIXTURE = Path(__file__).parent / "fixtures" / "runtime_model" / "captures.json"
TZ = timezone(timedelta(hours=8))


def _tool_call(name: str, args: dict, call_id: str) -> SimpleNamespace:
    function = SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False))
    return SimpleNamespace(id=call_id, function=function)


def _response(*, tool_calls: list | None = None, payload: dict | None = None) -> SimpleNamespace:
    content = json.dumps(payload, ensure_ascii=False) if payload is not None else None
    message = SimpleNamespace(content=content, tool_calls=tool_calls or [])
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])


def _classifier_script() -> list[SimpleNamespace]:
    calls: list[tuple[str, dict]] = []
    projects = {
        "project-runtime.md": [
            "The runtime captures local screen context.",
            "The runtime stores auditable durable facts.",
            "The runtime exposes the personal model through MCP.",
            "The runtime keeps model data local by default.",
        ],
        "project-release.md": [
            "The runtime maps state formation to inspectable artifacts.",
            "The release uses synthetic verification fixtures.",
            "The model export carries provenance receipts.",
            "External clients consume a versioned snapshot contract.",
        ],
    }
    for path, facts in projects.items():
        calls.append(
            (
                "create",
                {
                    "path": path,
                    "description": f"Synthetic facts for {path}",
                    "tags": ["project", "runtime-fixture"],
                },
            )
        )
        calls.extend(
            ("append", {"path": path, "content": fact, "tags": ["fact", "synthetic"]})
            for fact in facts
        )
    calls.append(("commit", {"summary": "wrote synthetic runtime model facts"}))
    return [
        _response(tool_calls=[_tool_call(name, args, f"classifier-{index}")])
        for index, (name, args) in enumerate(calls)
    ]


def _schema_payload(central: str, inference: str) -> dict:
    return {
        "central_proposition": central,
        "supporting_summary": "Four independent synthetic facts support this pattern.",
        "expected_inferences": [inference],
        "confidence": 0.9,
    }


def _wire_session_manager(clock_state: list[datetime]) -> SessionManager:
    def on_start(session_id: str, start: datetime) -> None:
        with fts.cursor() as conn:
            session_store.insert(
                conn,
                session_store.SessionRow(id=session_id, start_time=start, status="active"),
            )

    def on_end(session_id: str, _start: datetime, end: datetime) -> None:
        with fts.cursor() as conn:
            session_store.mark_ended(conn, session_id, end)

    return SessionManager(
        on_session_start=on_start,
        on_session_end=on_end,
        clock=lambda: clock_state[0],
    )


def test_fresh_root_ingest_build_export_contract(ac_root, monkeypatch, fake_llm) -> None:
    captures = json.loads(FIXTURE.read_text(encoding="utf-8"))
    cfg = config_mod.load(ac_root / "config.toml")
    cfg.memory_delta.apply_enabled = False
    cfg.person_graph_enabled = False
    cfg.case_extraction_enabled = False
    cfg.relation_extraction_enabled = True
    cfg.search.hybrid_enabled = False
    cfg.timeline.cold_lookback_minutes = 5
    cfg.timeline.max_parallel_windows = 1

    clock_state = [datetime(2026, 7, 10, 9, 0, tzinfo=TZ)]
    manager = _wire_session_manager(clock_state)
    runner = scheduler._CaptureRunner(cfg.capture, provider=None, pre_capture_hook=manager.on_event)
    scheduler._set_active_runner(runner)
    routes_mod.set_config(cfg)
    try:
        client = TestClient(build_api_app(cfg))
        assert client.post("/captures/ingest", json=captures[0]).status_code == 200
        clock_state[0] = datetime(2026, 7, 10, 9, 2, tzinfo=TZ)
        assert client.post("/captures/ingest", json=captures[1]).status_code == 200
        assert manager.force_end(reason="runtime-fixture") is not None
    finally:
        scheduler._set_active_runner(None)
        routes_mod.set_config(None)

    monkeypatch.setattr(timeline_tick, "_now", lambda: datetime(2026, 7, 10, 9, 3, tzinfo=TZ))
    assert timeline_tick.tick_now(cfg) == 2

    fake_llm.add_script("classifier", _classifier_script())
    schema_responses = [
        _response(
            payload=_schema_payload(
                "Runtime work favors local and auditable systems.",
                "Future daemons will preserve local provenance.",
            )
        ),
        _response(
            payload=_schema_payload(
                "Release work favors reproducible artifacts.",
                "Future releases will ship replayable fixtures.",
            )
        ),
    ]
    fake_llm.add_script("schema_miner", [*schema_responses, *schema_responses])
    collision = {
        "detected": True,
        "central_proposition": "The user turns personal context into inspectable runtime artifacts.",
        "supporting_summary": "Runtime and release behavior share an auditability preference.",
        "expected_inferences": ["Future model changes will require receipts and replay."],
        "confidence": 0.92,
    }
    fake_llm.add_script(
        "cross_domain_sweeper", [_response(payload=collision), _response(payload=collision)]
    )
    apex = {"apex": "A focused builder who turns local personal context into reproducible models."}
    fake_llm.add_script("root_synthesis", [_response(payload=apex), _response(payload=apex)])

    moments = iter(
        [
            datetime(2026, 7, 10, 9, 4, tzinfo=UTC),
            datetime(2026, 7, 10, 9, 4, 2, tzinfo=UTC),
            datetime(2026, 7, 10, 9, 5, tzinfo=UTC),
            datetime(2026, 7, 10, 9, 5, 2, tzinfo=UTC),
        ]
    )
    first = run_model_build(cfg, trigger="runtime-fixture", now=lambda: next(moments))
    second = run_model_build(cfg, trigger="runtime-fixture", now=lambda: next(moments))
    assert first.status == "degraded"
    with fts.cursor() as conn:
        face_debug = [
            {
                "level": row["level"],
                "provenance": row["provenance"],
                "observations": row["observations"],
                "footprints": len(json.loads(row["footprints"])),
                "status": row["status"],
                "signature": row["signature"],
            }
            for row in conn.execute("SELECT * FROM schema_faces ORDER BY level, signature")
        ]
    assert second.stats["faces"] >= 2, json.dumps(
        {
            "stats": second.stats,
            "faces": face_debug,
            "schema_miner": second.stages["schema_miner"],
            "cross_domain_sweeper": second.stages["cross_domain_sweeper"],
            "root_synthesis": second.stages["root_synthesis"],
        },
        sort_keys=True,
    )
    assert second.stats["volumes"] >= 1, second.stats
    assert second.stats["roots"] == 1, second.stats
    assert second.status == "complete", {
        "degraded": second.manifest["degraded_stages"],
        "stats": second.stats,
        "stages": second.stages,
    }

    output = ac_root / "exports" / "runtime-model.json"
    with fts.cursor() as conn:
        exported = export_snapshot(
            conn,
            out_path=output,
            build_metadata=second.manifest,
            generated_at="2026-07-10T09:06:00+00:00",
        )
    snapshot = json.loads(exported.read_text(encoding="utf-8"))
    assert snapshot["stats"]["points"] >= 8
    assert snapshot["stats"]["evolution_lines"] + snapshot["stats"]["relation_lines"] >= 1
    assert snapshot["stats"]["faces"] >= 2
    assert snapshot["stats"]["volumes"] >= 1
    assert snapshot["stats"]["roots"] == 1
    assert snapshot["root"]["source_receipts"]
    assert all(face["source_receipts"] for face in snapshot["faces"])
    assert all(volume["source_receipts"] for volume in snapshot["volumes"])
    assert exported.stat().st_mode & 0o777 == 0o600

    client = TestClient(build_api_app(cfg))
    graph_response = client.get("/model/graph")
    assert graph_response.status_code == 200
    live = graph_response.json()["model"]
    assert live["points"]
    assert live["lines"]
    assert live["faces"]
    assert live["volumes"]
    assert live["root"] is not None
    assert live["root"]["source_receipts"]

    page = client.get("/model")
    assert page.status_code == 200
    assert "/model/assets/three.module.js" in page.text
    assert "cdn.jsdelivr.net" not in page.text

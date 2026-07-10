"Tests for test inversion stations."

from __future__ import annotations

from pathlib import Path

import pytest

from persome.store import fts

from .inversion_harness import assert_equivalent, run_in_both_modes


def test_station_chat_memory_extractor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from persome.chat import memory_extractor

    def _script() -> None:
        with fts.cursor() as conn:
            memory_extractor._write_memory(
                conn,
                {
                    "type": "preference",
                    "name": "Coffee",
                    "description": "drinks",
                    "content": "prefers oat-milk latte",
                },
            )

            memory_extractor._write_memory(
                conn,
                {
                    "type": "preference",
                    "name": "Coffee",
                    "description": "drinks",
                    "content": "prefers oat-milk latte",
                },
            )
            memory_extractor._write_memory(
                conn,
                {
                    "type": "project",
                    "name": "acme",
                    "description": "main repo",
                    "content": "works on acme-mono",
                },
            )

    snap_md, snap_evo = run_in_both_modes(monkeypatch, tmp_path, _script)
    assert len(snap_md.entries) == 2
    assert_equivalent(snap_md, snap_evo)


def test_station_chat_tool_handlers_set_user_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from persome.chat import tool_handlers

    def _script() -> None:
        assert tool_handlers.tool_set_user_name({"name": "Alice"}) == {"ok": True, "name": "Alice"}
        assert tool_handlers.tool_set_user_name({"name": "Bob"})["ok"]

    snap_md, snap_evo = run_in_both_modes(monkeypatch, tmp_path, _script)
    assert "user-profile.md" in snap_md.memory
    assert_equivalent(snap_md, snap_evo)


def test_station_writer_tools(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from persome.writer import tools

    def _script() -> None:
        with fts.cursor() as conn:
            state = tools.CommitState()
            assert tools.tool_create(
                conn, path="project-model.md", description="model file", tags=["proj"], state=state
            )["ok"]
            r1 = tools.tool_append(
                conn,
                path="project-model.md",
                content="observed fact",
                tags=["alpha"],
                soft_limit_tokens=20_000,
                state=state,
                confidence="high",
                occurred_at="2026-06-10T09:00",
            )
            assert r1["ok"]
            r2 = tools.tool_supersede(
                conn,
                path="project-model.md",
                old_entry_id=r1["id"],
                new_content="corrected fact",
                reason="model consolidation",
                tags=None,  # LLM may omit tags, so inherit them
                state=state,
            )
            assert r2["ok"]
            assert tools.tool_flag_compact(
                conn, path="project-model.md", reason="getting big", state=state
            )["ok"]

            assert "error" in tools.tool_append(
                conn,
                path="project-missing.md",
                content="x",
                tags=[],
                soft_limit_tokens=100,
                state=state,
            )

    snap_md, snap_evo = run_in_both_modes(monkeypatch, tmp_path, _script)
    assert snap_md.files[0][8] == 1
    assert "needs_compact: true" in snap_md.memory["project-model.md"]
    assert_equivalent(snap_md, snap_evo)


def test_station_schema_miner_stage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from persome.evomem.schema_miner import SchemaResult
    from persome.writer import schema_miner_stage as stage

    first = SchemaResult(
        success=True,
        central_proposition="\u504f\u597d\u6781\u7b80\u5de5\u5177",
        supporting_summary="\u591a\u6b21\u9009\u62e9 uv/ruff",
        expected_inferences=["\u4f1a\u62d2\u7edd\u91cd\u578b\u6846\u67b6"],
        confidence=0.55,
    )
    remined = SchemaResult(
        success=True,
        central_proposition="\u5f3a\u70c8\u504f\u597d\u6781\u7b80\u5de5\u5177",
        supporting_summary="\u518d\u6b21\u786e\u8ba4",
        expected_inferences=[
            "\u4f1a\u62d2\u7edd\u91cd\u578b\u6846\u67b6",
            "\u5148\u770b\u4f9d\u8d56\u4f53\u79ef",
        ],
        confidence=0.82,
    )

    def _script() -> None:
        with fts.cursor() as conn:
            w1 = stage._persist_schema(conn, "project-x.md", first, stable_threshold=0.7)
            assert w1 is not None and w1.status == "forming"

            w2 = stage._persist_schema(conn, "project-x.md", remined, stable_threshold=0.7)
            assert w2 is not None and w2.updated_in_place and w2.status == "stable"

    snap_md, snap_evo = run_in_both_modes(monkeypatch, tmp_path, _script)
    assert "schema-project-x.md" in snap_md.memory
    assert any(r[4] == "active" for r in snap_md.files)

    head = [r for r in snap_evo.evo_nodes if r[7] == 1]  # is_latest=1
    assert head and head[0][4] == "l6_schema" and head[0][20] == 0.82
    assert_equivalent(snap_md, snap_evo)


def test_station_cross_domain_sweeper(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from persome.writer import cross_domain_sweeper as sweeper

    a = sweeper._StableSchema(
        name="schema-project-a.md",
        source_path="project-a.md",
        central="\u53cd\u590d\u624b\u52a8\u91cd\u8bd5",
        inferences=["\u9047\u9519\u5148\u91cd\u8bd5"],
        confidence=0.8,
    )
    b = sweeper._StableSchema(
        name="schema-tool-b.md",
        source_path="tool-b.md",
        central="\u91cd\u8bd5\u662f\u9ed8\u8ba4\u52a8\u4f5c",
        inferences=["\u81ea\u52a8\u5316\u503e\u5411\u4f4e"],
        confidence=0.75,
    )
    collision = sweeper._Collision(
        detected=True,
        central_proposition="\u8de8\u573a\u666f\u7684\u300c\u91cd\u8bd5\u4f18\u5148\u300d\u884c\u4e3a\u6a21\u5f0f",
        supporting_summary="A/B \u4e24\u57df\u540c\u5f62",
        expected_inferences=[
            "\u65b0\u5de5\u5177\u4e0a\u624b\u4e5f\u4f1a\u5148\u624b\u52a8\u91cd\u8bd5"
        ],
        confidence=0.66,
    )
    better = sweeper._Collision(
        detected=True,
        central_proposition="\u8de8\u573a\u666f\u7684\u300c\u91cd\u8bd5\u4f18\u5148\u300d\u884c\u4e3a\u6a21\u5f0f\uff08\u5f3a\u5316\uff09",
        supporting_summary="\u518d\u6b21\u786e\u8ba4",
        expected_inferences=[
            "\u65b0\u5de5\u5177\u4e0a\u624b\u4e5f\u4f1a\u5148\u624b\u52a8\u91cd\u8bd5",
            "\u4e0d\u7231\u8bfb\u6587\u6863",
        ],
        confidence=0.81,
    )

    def _script() -> None:
        with fts.cursor() as conn:
            w1 = sweeper._persist_cross_schema(conn, a, b, collision, stable_threshold=0.7)
            assert w1 is not None and not w1.updated_in_place

            w2 = sweeper._persist_cross_schema(conn, a, b, better, stable_threshold=0.7)
            assert w2 is not None and w2.updated_in_place

    snap_md, snap_evo = run_in_both_modes(monkeypatch, tmp_path, _script)
    assert any(n.startswith("schema-xdomain-") for n in snap_md.memory)
    assert_equivalent(snap_md, snap_evo)

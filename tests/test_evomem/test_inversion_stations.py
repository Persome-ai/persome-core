"""写口逐站迁移的输出等价断言（PR-6b，设计稿 §4.4）。

每个写站点一个测试、一个独立 commit：以站点的**真实入口**驱动同一组写，断言
两种写权下 markdown 投影 byte-identical（仅 marker / #valid-until 两处已知
良性差异）+ FTS 五表逐行全等 + evo_nodes 真相态 == legacy+backfill 态。
harness 见 ``inversion_harness.py``。

站点清单（§1.3；Q2 豁免 ``writer/session_reducer.py`` 与 timeline 的 event 写）：
intent/sink、chat/memory_extractor、chat/tool_handlers、
writer/tools（classifier/pattern_detector/consolidator 共用的写工具层）、
writer/schema_miner_stage、writer/cross_domain_sweeper、intent/schema_feedback。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from persome.store import fts

from .inversion_harness import assert_equivalent, run_in_both_modes


@pytest.fixture(autouse=True)
def _quiet_alerts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("persome.events.publish", lambda *a, **k: None)


# ── 站点 1：intent/sink（add_direct 形态——append-only 投影，永不入链）────────


def test_station_intent_sink(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from persome.intent import sink
    from persome.intent.ontology import Intent

    def _script() -> None:
        with fts.cursor() as conn:
            sink.persist_intent(
                conn,
                Intent(
                    kind="calendar",
                    scope="session-1",
                    confidence=0.8,
                    rationale="r",
                    ts="2026-06-11T10:00",
                    payload={"when_text": "周五15:00"},
                ),
            )
            sink.persist_intent(
                conn,
                Intent(
                    kind="reminder",
                    scope="session-1",
                    confidence=0.7,
                    ts="2026-06-11T10:01",
                    payload={"when_text": "明天"},
                ),
            )
            # dedup：重复 intent 不再追加投影（两种写权同样跳过）
            sink.persist_intent(
                conn,
                Intent(
                    kind="calendar",
                    scope="session-1",
                    confidence=0.9,
                    ts="2026-06-11T10:02",
                    payload={"when_text": "周五15:00"},
                ),
            )

    snap_md, snap_evo = run_in_both_modes(monkeypatch, tmp_path, _script)
    assert any(n.startswith("intent-") for n in snap_md.memory)
    assert_equivalent(snap_md, snap_evo)


# ── 站点 2：chat/memory_extractor（create+append + dedup 守卫读投影）─────────


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
            # dedup 守卫：同内容第二次写应 no-op（守卫读的是投影文件）
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
    assert len(snap_md.entries) == 2  # dedup 真的挡住了第二次
    assert_equivalent(snap_md, snap_evo)


# ── 站点 3：chat/tool_handlers（set_user_name → user-profile 写）─────────────


def test_station_chat_tool_handlers_set_user_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from persome.chat import tool_handlers

    def _script() -> None:
        assert tool_handlers.tool_set_user_name({"name": "Alice"}) == {"ok": True, "name": "Alice"}
        assert tool_handlers.tool_set_user_name({"name": "Bob"})["ok"]  # 二写：append 路径

    snap_md, snap_evo = run_in_both_modes(monkeypatch, tmp_path, _script)
    assert "user-profile.md" in snap_md.memory
    assert_equivalent(snap_md, snap_evo)


# ── 站点 4：writer/tools（classifier/pattern_detector/consolidator 写层）─────


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
            # 错误面契约不变：缺文件返回 error dict 而非抛错
            assert "error" in tools.tool_append(
                conn,
                path="project-missing.md",
                content="x",
                tags=[],
                soft_limit_tokens=100,
                state=state,
            )

    snap_md, snap_evo = run_in_both_modes(monkeypatch, tmp_path, _script)
    assert snap_md.files[0][8] == 1  # needs_compact 在 files 行
    assert "needs_compact: true" in snap_md.memory["project-model.md"]
    assert_equivalent(snap_md, snap_evo)


# ── 站点 6：writer/schema_miner_stage（L6 schema：create + 原地 supersede）───


def test_station_schema_miner_stage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from persome.evomem.schema_miner import SchemaResult
    from persome.writer import schema_miner_stage as stage

    first = SchemaResult(
        success=True,
        central_proposition="偏好极简工具",
        supporting_summary="多次选择 uv/ruff",
        expected_inferences=["会拒绝重型框架"],
        confidence=0.55,
    )
    remined = SchemaResult(
        success=True,
        central_proposition="强烈偏好极简工具",
        supporting_summary="再次确认",
        expected_inferences=["会拒绝重型框架", "先看依赖体积"],
        confidence=0.82,
    )

    def _script() -> None:
        with fts.cursor() as conn:
            # 首挖：forming → 文件生而 dormant
            w1 = stage._persist_schema(conn, "project-x.md", first, stable_threshold=0.7)
            assert w1 is not None and w1.status == "forming"
            # 再挖：原地 supersede + dormant→active 翻转（set_file_status 路径）
            w2 = stage._persist_schema(conn, "project-x.md", remined, stable_threshold=0.7)
            assert w2 is not None and w2.updated_in_place and w2.status == "stable"

    snap_md, snap_evo = run_in_both_modes(monkeypatch, tmp_path, _script)
    assert "schema-project-x.md" in snap_md.memory
    assert any(r[4] == "active" for r in snap_md.files)  # dormant→active 已翻
    # L6 四元组三列落在真相侧（共享映射从 body/tag 解析）
    head = [r for r in snap_evo.evo_nodes if r[7] == 1]  # is_latest=1
    assert head and head[0][4] == "l6_schema" and head[0][20] == 0.82
    assert_equivalent(snap_md, snap_evo)


# ── 站点 7：writer/cross_domain_sweeper（xdomain schema：create + 原地 supersede）─


def test_station_cross_domain_sweeper(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from persome.writer import cross_domain_sweeper as sweeper

    a = sweeper._StableSchema(
        name="schema-project-a.md",
        source_path="project-a.md",
        central="反复手动重试",
        inferences=["遇错先重试"],
        confidence=0.8,
    )
    b = sweeper._StableSchema(
        name="schema-tool-b.md",
        source_path="tool-b.md",
        central="重试是默认动作",
        inferences=["自动化倾向低"],
        confidence=0.75,
    )
    collision = sweeper._Collision(
        detected=True,
        central_proposition="跨场景的「重试优先」行为模式",
        supporting_summary="A/B 两域同形",
        expected_inferences=["新工具上手也会先手动重试"],
        confidence=0.66,
    )
    better = sweeper._Collision(
        detected=True,
        central_proposition="跨场景的「重试优先」行为模式（强化）",
        supporting_summary="再次确认",
        expected_inferences=["新工具上手也会先手动重试", "不爱读文档"],
        confidence=0.81,
    )

    def _script() -> None:
        with fts.cursor() as conn:
            w1 = sweeper._persist_cross_schema(conn, a, b, collision, stable_threshold=0.7)
            assert w1 is not None and not w1.updated_in_place
            # 再扫同一无序对：幂等落同一文件，原地 supersede
            w2 = sweeper._persist_cross_schema(conn, a, b, better, stable_threshold=0.7)
            assert w2 is not None and w2.updated_in_place

    snap_md, snap_evo = run_in_both_modes(monkeypatch, tmp_path, _script)
    assert any(n.startswith("schema-xdomain-") for n in snap_md.memory)
    assert_equivalent(snap_md, snap_evo)


# ── 站点 8：intent/schema_feedback（确定性原地 supersede 调 confidence）──────


def test_station_schema_feedback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from persome.evomem.schema_miner import SchemaResult
    from persome.intent import schema_feedback
    from persome.writer import schema_miner_stage as stage

    seed = SchemaResult(
        success=True,
        central_proposition="偏好夜间工作",
        supporting_summary="多次深夜提交",
        expected_inferences=["早会容易迟到"],
        confidence=0.75,
    )

    def _script() -> None:
        with fts.cursor() as conn:
            stage._persist_schema(conn, "project-night.md", seed, stable_threshold=0.7)
            # dismiss 反馈：确定性拖低 confidence（原地 supersede，正文不变）
            adj = schema_feedback._adjust_schema(
                conn, "schema-project-night.md", delta=-0.1, feedback="dismissed"
            )
            assert adj is not None and adj.new_confidence < adj.old_confidence

    snap_md, snap_evo = run_in_both_modes(monkeypatch, tmp_path, _script)
    assert "schema-project-night.md" in snap_md.memory
    assert "confidence:0.65" in snap_md.memory["schema-project-night.md"]
    assert_equivalent(snap_md, snap_evo)

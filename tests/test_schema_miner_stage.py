"""D2 schema-mining stage: ``writer.schema_miner_stage``.

End-to-end of the mining half (acceptance ① / ②):
  - seed several fact entries into one durable file → clustering yields a bundle;
  - run mining with a *fake* ``llm_call`` (no network) returning a canned schema;
  - a ``schema-<slug>.md`` is written, the ``schema-`` prefix is accepted, the
    files table holds the row, and ``intent.schema_prior.active_schema_inferences``
    reads the schema's inferences back (the provider seam closes the loop).

The fake-llm injection mirrors ``tests/test_evomem/test_schema_miner.py``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from persome import config as config_mod
from persome.intent import schema_prior
from persome.store import entries as entries_mod
from persome.store import files as files_mod
from persome.store import fts
from persome.writer import schema_miner_stage as stage


def _resp(payload: dict):
    msg = SimpleNamespace(
        content="```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```",
        tool_calls=[],
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason="stop")],
        usage=SimpleNamespace(total_tokens=0),
    )


def _fake_llm(payload: dict):
    def call(_messages):
        return _resp(payload)

    return call


def _seed_facts(conn, *, name: str, facts: list[str]) -> None:
    entries_mod.create_file(conn, name=name, description=f"facts for {name}", tags=["t"])
    for f in facts:
        entries_mod.append_entry(conn, name=name, content=f, tags=["fact"])


_STABLE_PAYLOAD = {
    "central_proposition": "用户在工具选型上系统性偏好极简、低依赖方案",
    "supporting_summary": "多次选择 uv/ruff 而非重型框架，拒绝 litellm",
    "expected_inferences": [
        "会拒绝引入大型框架/重 SDK，倾向手搓等价实现",
        "评估新工具时优先看依赖体积与可审计性",
    ],
    "confidence": 0.85,
}


def test_mining_writes_stable_schema_and_files_row(ac_root):
    cfg = config_mod.load(ac_root / "config.toml")
    cfg.memory_delta.apply_enabled = False  # 测 entries 源的挖掘逻辑；apply_enabled=True 下 mine 读 evo_nodes（见 from_evomem 专测）
    with fts.cursor() as conn:
        _seed_facts(
            conn,
            name="project-tooling.md",
            facts=[
                "用 uv 管理依赖而非 pip",
                "用 ruff 取代 black+flake8",
                "拒绝 litellm，手写 Anthropic SDK 封装",
                "倾向命令行工具而非重型 IDE 插件",
            ],
        )

        result = stage.run_schema_mining(cfg, conn, llm_call=_fake_llm(_STABLE_PAYLOAD))

        # ① exactly one schema written, born stable (confidence 0.85 >= 0.6).
        assert result.written_count == 1
        written = result.written[0]
        assert written.status == "stable"
        assert written.path.startswith("schema-")

        # the schema- prefix is accepted by validate_prefix (else create_file
        # would have raised) and the files table carries the row.
        assert files_mod.validate_prefix(written.path) == "schema"
        row = fts.get_file(conn, written.path)
        assert row is not None
        assert row.prefix == "schema"
        assert row.entry_count == 1

        # ② the provider reads the stable schema's inferences back out.
        inferences = schema_prior.active_schema_inferences(conn)
    assert "会拒绝引入大型框架/重 SDK，倾向手搓等价实现" in inferences
    assert "评估新工具时优先看依赖体积与可审计性" in inferences


def test_low_confidence_schema_is_forming_and_not_injected(ac_root):
    """confidence < stable_threshold → forming → provider does not surface it."""
    cfg = config_mod.load(ac_root / "config.toml")
    cfg.memory_delta.apply_enabled = False  # 测 entries 源的挖掘逻辑；apply_enabled=True 下 mine 读 evo_nodes（见 from_evomem 专测）
    payload = {**_STABLE_PAYLOAD, "confidence": 0.3}
    with fts.cursor() as conn:
        _seed_facts(
            conn,
            name="topic-weak.md",
            facts=["事实一", "事实二", "事实三", "事实四"],
        )
        result = stage.run_schema_mining(cfg, conn, llm_call=_fake_llm(payload))
        assert result.written_count == 1
        assert result.written[0].status == "forming"
        # forming schemas are written (grep-able) but never injected as priors.
        assert schema_prior.active_schema_inferences(conn) == []


def test_forming_schema_is_born_dormant(ac_root):
    """A forming schema is born ``dormant`` → hidden from the default (non-dormant)
    listing, with the status in BOTH the files table and the frontmatter so a
    rebuild_index won't drift it back to active (issue #440)."""
    cfg = config_mod.load(ac_root / "config.toml")
    cfg.memory_delta.apply_enabled = False  # 测 entries 源的挖掘逻辑；apply_enabled=True 下 mine 读 evo_nodes（见 from_evomem 专测）
    payload = {**_STABLE_PAYLOAD, "confidence": 0.3}
    with fts.cursor() as conn:
        _seed_facts(conn, name="topic-forming.md", facts=["事实一", "事实二", "事实三", "事实四"])
        result = stage.run_schema_mining(cfg, conn, llm_call=_fake_llm(payload))
        path = result.written[0].path

        row = fts.get_file(conn, path)
        assert row is not None and row.status == "dormant"
        active = [f.path for f in fts.list_files(conn, include_dormant=False)]
        assert path not in active, "forming schema must be hidden from default listing"
        assert path in [f.path for f in fts.list_files(conn, include_dormant=True)]
        # frontmatter (the rebuild source of truth) carries it too.
        assert files_mod.read_file(files_mod.memory_path(path)).status == "dormant"


def test_remine_promotes_forming_schema_to_active(ac_root):
    """A dormant forming schema flips to ``active`` once re-mined as stable — in
    both frontmatter and the files table (issue #440)."""
    cfg = config_mod.load(ac_root / "config.toml")
    cfg.memory_delta.apply_enabled = False  # 测 entries 源的挖掘逻辑；apply_enabled=True 下 mine 读 evo_nodes（见 from_evomem 专测）
    facts = [
        "用 uv 管理依赖而非 pip",
        "用 ruff 取代 black+flake8",
        "拒绝 litellm，手写 Anthropic SDK 封装",
        "倾向命令行工具而非重型 IDE 插件",
    ]
    with fts.cursor() as conn:
        _seed_facts(conn, name="project-tooling.md", facts=facts)
        # ① first mine is weak → forming → dormant.
        r1 = stage.run_schema_mining(
            cfg, conn, llm_call=_fake_llm({**_STABLE_PAYLOAD, "confidence": 0.3})
        )
        path = r1.written[0].path
        assert fts.get_file(conn, path).status == "dormant"

        # ② re-mine the same source as stable → promoted to active in both places.
        r2 = stage.run_schema_mining(cfg, conn, llm_call=_fake_llm(_STABLE_PAYLOAD))
        assert r2.written[0].status == "stable"
        assert fts.get_file(conn, path).status == "active"
        assert files_mod.read_file(files_mod.memory_path(path)).status == "active"
        assert path in [f.path for f in fts.list_files(conn, include_dormant=False)]


def test_bundle_below_min_facts_is_skipped(ac_root):
    """A file with < min_facts entries produces no bundle → nothing mined."""
    cfg = config_mod.load(ac_root / "config.toml")
    cfg.memory_delta.apply_enabled = False  # 测 entries 源的挖掘逻辑；apply_enabled=True 下 mine 读 evo_nodes（见 from_evomem 专测）
    with fts.cursor() as conn:
        _seed_facts(conn, name="topic-thin.md", facts=["只有一条事实"])
        result = stage.run_schema_mining(cfg, conn, llm_call=_fake_llm(_STABLE_PAYLOAD))
    assert result.written_count == 0


def test_no_facts_writes_nothing(ac_root):
    cfg = config_mod.load(ac_root / "config.toml")
    cfg.memory_delta.apply_enabled = False  # 测 entries 源的挖掘逻辑；apply_enabled=True 下 mine 读 evo_nodes（见 from_evomem 专测）
    with fts.cursor() as conn:
        result = stage.run_schema_mining(cfg, conn, llm_call=_fake_llm(_STABLE_PAYLOAD))
        assert result.written_count == 0
        # ③ the P0 seam holds: no schema files → provider returns [].
        assert schema_prior.active_schema_inferences(conn) == []


def test_render_then_parse_roundtrips_inferences():
    body = stage.render_schema_body(
        central_proposition="命题",
        supporting_summary="摘要",
        expected_inferences=["推论一", "推论二"],
    )
    assert stage.parse_expected_inferences(body) == ["推论一", "推论二"]


def test_schema_name_derived_from_source_file():
    assert stage.schema_name_for("project-x.md") == "schema-project-x.md"
    assert stage.schema_name_for("topic-tooling.md") == "schema-topic-tooling.md"


def test_remine_updates_same_file_not_a_new_one(ac_root):
    """Idempotency: re-mining the same file cluster supersedes in place.

    The slug is source-derived, so two runs over ``project-tooling.md`` both land
    in ``schema-project-tooling.md`` — exactly one schema file, the old proposition
    superseded by the new (not two files accumulating).
    """
    cfg = config_mod.load(ac_root / "config.toml")
    cfg.memory_delta.apply_enabled = False  # 测 entries 源的挖掘逻辑；apply_enabled=True 下 mine 读 evo_nodes（见 from_evomem 专测）
    facts = [
        "用 uv 管理依赖而非 pip",
        "用 ruff 取代 black+flake8",
        "拒绝 litellm，手写 Anthropic SDK 封装",
        "倾向命令行工具而非重型 IDE 插件",
    ]
    with fts.cursor() as conn:
        _seed_facts(conn, name="project-tooling.md", facts=facts)

        first = stage.run_schema_mining(cfg, conn, llm_call=_fake_llm(_STABLE_PAYLOAD))
        assert first.written_count == 1
        assert first.written[0].path == "schema-project-tooling.md"
        assert first.written[0].updated_in_place is False

        # Second run over the same (unchanged) cluster: same file, superseded.
        updated_payload = {
            **_STABLE_PAYLOAD,
            "central_proposition": "用户对工具链的极简偏好进一步固化",
            "expected_inferences": ["新的推论：会主动删依赖"],
        }
        second = stage.run_schema_mining(cfg, conn, llm_call=_fake_llm(updated_payload))
        assert second.written_count == 1
        assert second.written[0].path == "schema-project-tooling.md"
        assert second.written[0].updated_in_place is True

        # Exactly one schema file on disk; provider returns the *new* inference,
        # not the stale one (the old entry was superseded).
        schema_files = [f for f in fts.list_files(conn) if f.path.startswith("schema-")]
        assert [f.path for f in schema_files] == ["schema-project-tooling.md"]
        inferences = schema_prior.active_schema_inferences(conn)
    assert "新的推论：会主动删依赖" in inferences
    assert "会拒绝引入大型框架/重 SDK，倾向手搓等价实现" not in inferences


def test_two_source_files_yield_two_schema_files(ac_root):
    cfg = config_mod.load(ac_root / "config.toml")
    cfg.memory_delta.apply_enabled = False  # 测 entries 源的挖掘逻辑；apply_enabled=True 下 mine 读 evo_nodes（见 from_evomem 专测）
    four = ["a", "b", "c", "d"]
    with fts.cursor() as conn:
        _seed_facts(conn, name="project-alpha.md", facts=four)
        _seed_facts(conn, name="topic-beta.md", facts=four)
        result = stage.run_schema_mining(cfg, conn, llm_call=_fake_llm(_STABLE_PAYLOAD))
        paths = sorted(w.path for w in result.written)
    assert paths == ["schema-project-alpha.md", "schema-topic-beta.md"]


def test_tool_and_org_prefixes_are_mined(ac_root):
    """bug_011: ``tool-`` and ``org-`` are canonical fact prefixes (see
    ``intent/recall.py``), so a ``tool-*.md`` / ``org-*.md`` cluster must mine a
    schema. They were previously absent from ``_FACT_PREFIXES`` and got dropped."""
    cfg = config_mod.load(ac_root / "config.toml")
    cfg.memory_delta.apply_enabled = False  # 测 entries 源的挖掘逻辑；apply_enabled=True 下 mine 读 evo_nodes（见 from_evomem 专测）
    four = ["事实一", "事实二", "事实三", "事实四"]
    with fts.cursor() as conn:
        _seed_facts(conn, name="tool-ripgrep.md", facts=four)
        _seed_facts(conn, name="org-acme.md", facts=four)
        result = stage.run_schema_mining(cfg, conn, llm_call=_fake_llm(_STABLE_PAYLOAD))
        paths = sorted(w.path for w in result.written)
    assert paths == ["schema-org-acme.md", "schema-tool-ripgrep.md"]


def test_collect_fact_bundles_from_evomem_reads_evo_nodes(ac_root):
    """from_evomem=True 读**重建层 evo_nodes**（delta+assertions 落的真 facts），而非退役中的
    entries 投影。markdown 权威下 add_direct 只写 evo_nodes，schema 若读 entries 会漏掉整个
    重建（spec 2026-07-04 §1「reader↔重建断层」的 schema 那一读路）。"""
    from persome.evomem.engine import EvoMemory
    from persome.writer import delta_apply

    cfg_da = SimpleNamespace(memory_delta=SimpleNamespace(apply_assertions=True))
    clean = {
        "entities": [{"ref": "张伟", "kind": "person", "ended": False, "quote": "x"}],
        "relations": [],
        "events": [],
        "assertions": [
            {"subject": {"ref": "张伟"}, "text": f"张伟事实{i}", "quote": "q", "confidence": 0.9}
            for i in range(4)
        ],
    }
    with fts.cursor() as conn:
        _seed_facts(conn, name="person-李四.md", facts=[f"李四事实{i}" for i in range(4)])  # legacy entries
        delta_apply.apply_delta(conn, cfg_da, clean, memory=EvoMemory())  # 重建 evo_nodes
        legacy = {b.source_path for b in stage.collect_fact_bundles(conn, from_evomem=False, min_facts=4)}
        evo = {b.source_path for b in stage.collect_fact_bundles(conn, from_evomem=True, min_facts=4)}
    # entries 路只见 legacy 的李四；evo_nodes 路只见重建的张伟——两条路各读各的层
    assert "person-李四.md" in legacy and "person-张伟.md" not in legacy
    assert "person-张伟.md" in evo and "person-李四.md" not in evo

"""D2 schema-mining stage: ``writer.schema_miner_stage``.

End-to-end of the mining half (acceptance ① / ②):
  - seed several fact entries into one durable file → clustering yields a bundle;
  - run mining with a *fake* ``llm_call`` (no network) returning a canned schema;
  - a ``schema-<slug>.md`` is written, the ``schema-`` prefix is accepted, the
    files table holds the row, and ``intent.schema_reader.active_schema_inferences``
    reads the schema's inferences back (the provider seam closes the loop).

The fake-llm injection mirrors ``tests/test_evomem/test_schema_miner.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from persome import config as config_mod
from persome.model import schema_reader
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
    "central_proposition": "\u7528\u6237\u5728\u5de5\u5177\u9009\u578b\u4e0a\u7cfb\u7edf\u6027\u504f\u597d\u6781\u7b80\u3001\u4f4e\u4f9d\u8d56\u65b9\u6848",
    "supporting_summary": "\u591a\u6b21\u9009\u62e9 uv/ruff \u800c\u975e\u91cd\u578b\u6846\u67b6\uff0c\u62d2\u7edd litellm",
    "expected_inferences": [
        "\u4f1a\u62d2\u7edd\u5f15\u5165\u5927\u578b\u6846\u67b6/\u91cd SDK\uff0c\u503e\u5411\u624b\u6413\u7b49\u4ef7\u5b9e\u73b0",
        "\u8bc4\u4f30\u65b0\u5de5\u5177\u65f6\u4f18\u5148\u770b\u4f9d\u8d56\u4f53\u79ef\u4e0e\u53ef\u5ba1\u8ba1\u6027",
    ],
    "confidence": 0.85,
}


def test_mining_writes_stable_schema_and_files_row(ac_root):
    cfg = config_mod.load(ac_root / "config.toml")
    cfg.memory_delta.apply_enabled = False
    with fts.cursor() as conn:
        _seed_facts(
            conn,
            name="project-tooling.md",
            facts=[
                "\u7528 uv \u7ba1\u7406\u4f9d\u8d56\u800c\u975e pip",
                "\u7528 ruff \u53d6\u4ee3 black+flake8",
                "\u62d2\u7edd litellm\uff0c\u624b\u5199 Anthropic SDK \u5c01\u88c5",
                "\u503e\u5411\u547d\u4ee4\u884c\u5de5\u5177\u800c\u975e\u91cd\u578b IDE \u63d2\u4ef6",
            ],
        )

        result = stage.mine_schemas_for_user(cfg, conn, llm_call=_fake_llm(_STABLE_PAYLOAD))

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
        inferences = schema_reader.active_schema_inferences(conn)
    assert (
        "\u4f1a\u62d2\u7edd\u5f15\u5165\u5927\u578b\u6846\u67b6/\u91cd SDK\uff0c\u503e\u5411\u624b\u6413\u7b49\u4ef7\u5b9e\u73b0"
        in inferences
    )
    assert (
        "\u8bc4\u4f30\u65b0\u5de5\u5177\u65f6\u4f18\u5148\u770b\u4f9d\u8d56\u4f53\u79ef\u4e0e\u53ef\u5ba1\u8ba1\u6027"
        in inferences
    )


def test_low_confidence_schema_is_forming_and_not_injected(ac_root):
    """confidence < stable_threshold → forming → provider does not surface it."""
    cfg = config_mod.load(ac_root / "config.toml")
    cfg.memory_delta.apply_enabled = False
    payload = {**_STABLE_PAYLOAD, "confidence": 0.3}
    with fts.cursor() as conn:
        _seed_facts(
            conn,
            name="topic-weak.md",
            facts=[
                "\u4e8b\u5b9e\u4e00",
                "\u4e8b\u5b9e\u4e8c",
                "\u4e8b\u5b9e\u4e09",
                "\u4e8b\u5b9e\u56db",
            ],
        )
        result = stage.mine_schemas_for_user(cfg, conn, llm_call=_fake_llm(payload))
        assert result.written_count == 1
        assert result.written[0].status == "forming"
        # forming schemas are written (grep-able) but never injected as priors.
        assert schema_reader.active_schema_inferences(conn) == []


def test_forming_schema_is_born_dormant(ac_root):
    """A forming schema is born ``dormant`` → hidden from the default (non-dormant)
    listing, with the status in BOTH the files table and the frontmatter so a
    rebuild_index won't drift it back to active (issue #440)."""
    cfg = config_mod.load(ac_root / "config.toml")
    cfg.memory_delta.apply_enabled = False
    payload = {**_STABLE_PAYLOAD, "confidence": 0.3}
    with fts.cursor() as conn:
        _seed_facts(
            conn,
            name="topic-forming.md",
            facts=[
                "\u4e8b\u5b9e\u4e00",
                "\u4e8b\u5b9e\u4e8c",
                "\u4e8b\u5b9e\u4e09",
                "\u4e8b\u5b9e\u56db",
            ],
        )
        result = stage.mine_schemas_for_user(cfg, conn, llm_call=_fake_llm(payload))
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
    cfg.memory_delta.apply_enabled = False
    facts = [
        "\u7528 uv \u7ba1\u7406\u4f9d\u8d56\u800c\u975e pip",
        "\u7528 ruff \u53d6\u4ee3 black+flake8",
        "\u62d2\u7edd litellm\uff0c\u624b\u5199 Anthropic SDK \u5c01\u88c5",
        "\u503e\u5411\u547d\u4ee4\u884c\u5de5\u5177\u800c\u975e\u91cd\u578b IDE \u63d2\u4ef6",
    ]
    with fts.cursor() as conn:
        _seed_facts(conn, name="project-tooling.md", facts=facts)
        # ① first mine is weak → forming → dormant.
        r1 = stage.mine_schemas_for_user(
            cfg, conn, llm_call=_fake_llm({**_STABLE_PAYLOAD, "confidence": 0.3})
        )
        path = r1.written[0].path
        assert fts.get_file(conn, path).status == "dormant"

        # ② re-mine the same source as stable → promoted to active in both places.
        r2 = stage.mine_schemas_for_user(cfg, conn, llm_call=_fake_llm(_STABLE_PAYLOAD))
        assert r2.written[0].status == "stable"
        assert fts.get_file(conn, path).status == "active"
        assert files_mod.read_file(files_mod.memory_path(path)).status == "active"
        assert path in [f.path for f in fts.list_files(conn, include_dormant=False)]


def test_bundle_below_min_facts_is_skipped(ac_root):
    """A file with < min_facts entries produces no bundle → nothing mined."""
    cfg = config_mod.load(ac_root / "config.toml")
    cfg.memory_delta.apply_enabled = False
    with fts.cursor() as conn:
        _seed_facts(conn, name="topic-thin.md", facts=["\u53ea\u6709\u4e00\u6761\u4e8b\u5b9e"])
        result = stage.mine_schemas_for_user(cfg, conn, llm_call=_fake_llm(_STABLE_PAYLOAD))
    assert result.written_count == 0


def test_no_facts_writes_nothing(ac_root):
    cfg = config_mod.load(ac_root / "config.toml")
    cfg.memory_delta.apply_enabled = False
    with fts.cursor() as conn:
        result = stage.mine_schemas_for_user(cfg, conn, llm_call=_fake_llm(_STABLE_PAYLOAD))
        assert result.written_count == 0
        # ③ the P0 seam holds: no schema files → provider returns [].
        assert schema_reader.active_schema_inferences(conn) == []


def test_render_then_parse_roundtrips_inferences():
    body = stage.render_schema_body(
        central_proposition="\u547d\u9898",
        supporting_summary="\u6458\u8981",
        expected_inferences=["\u63a8\u8bba\u4e00", "\u63a8\u8bba\u4e8c"],
    )
    assert stage.parse_expected_inferences(body) == ["\u63a8\u8bba\u4e00", "\u63a8\u8bba\u4e8c"]


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
    cfg.memory_delta.apply_enabled = False
    facts = [
        "\u7528 uv \u7ba1\u7406\u4f9d\u8d56\u800c\u975e pip",
        "\u7528 ruff \u53d6\u4ee3 black+flake8",
        "\u62d2\u7edd litellm\uff0c\u624b\u5199 Anthropic SDK \u5c01\u88c5",
        "\u503e\u5411\u547d\u4ee4\u884c\u5de5\u5177\u800c\u975e\u91cd\u578b IDE \u63d2\u4ef6",
    ]
    with fts.cursor() as conn:
        _seed_facts(conn, name="project-tooling.md", facts=facts)

        first = stage.mine_schemas_for_user(cfg, conn, llm_call=_fake_llm(_STABLE_PAYLOAD))
        assert first.written_count == 1
        assert first.written[0].path == "schema-project-tooling.md"
        assert first.written[0].updated_in_place is False

        # Second run over the same (unchanged) cluster: same file, superseded.
        updated_payload = {
            **_STABLE_PAYLOAD,
            "central_proposition": "\u7528\u6237\u5bf9\u5de5\u5177\u94fe\u7684\u6781\u7b80\u504f\u597d\u8fdb\u4e00\u6b65\u56fa\u5316",
            "expected_inferences": [
                "\u65b0\u7684\u63a8\u8bba\uff1a\u4f1a\u4e3b\u52a8\u5220\u4f9d\u8d56"
            ],
        }
        second = stage.mine_schemas_for_user(cfg, conn, llm_call=_fake_llm(updated_payload))
        assert second.written_count == 1
        assert second.written[0].path == "schema-project-tooling.md"
        assert second.written[0].updated_in_place is True

        # Exactly one schema file on disk; provider returns the *new* inference,
        # not the stale one (the old entry was superseded).
        schema_files = [f for f in fts.list_files(conn) if f.path.startswith("schema-")]
        assert [f.path for f in schema_files] == ["schema-project-tooling.md"]
        inferences = schema_reader.active_schema_inferences(conn)
    assert "\u65b0\u7684\u63a8\u8bba\uff1a\u4f1a\u4e3b\u52a8\u5220\u4f9d\u8d56" in inferences
    assert (
        "\u4f1a\u62d2\u7edd\u5f15\u5165\u5927\u578b\u6846\u67b6/\u91cd SDK\uff0c\u503e\u5411\u624b\u6413\u7b49\u4ef7\u5b9e\u73b0"
        not in inferences
    )


def test_two_source_files_yield_two_schema_files(ac_root):
    cfg = config_mod.load(ac_root / "config.toml")
    cfg.memory_delta.apply_enabled = False
    four = ["a", "b", "c", "d"]
    with fts.cursor() as conn:
        _seed_facts(conn, name="project-alpha.md", facts=four)
        _seed_facts(conn, name="topic-beta.md", facts=four)
        result = stage.mine_schemas_for_user(cfg, conn, llm_call=_fake_llm(_STABLE_PAYLOAD))
        paths = sorted(w.path for w in result.written)
    assert paths == ["schema-project-alpha.md", "schema-topic-beta.md"]


def test_tool_and_org_prefixes_are_mined(ac_root):
    """bug_011: ``tool-`` and ``org-`` are canonical fact prefixes (see
    ``intent/recall.py``), so a ``tool-*.md`` / ``org-*.md`` cluster must mine a
    schema. They were previously absent from ``_FACT_PREFIXES`` and got dropped."""
    cfg = config_mod.load(ac_root / "config.toml")
    cfg.memory_delta.apply_enabled = False
    four = ["\u4e8b\u5b9e\u4e00", "\u4e8b\u5b9e\u4e8c", "\u4e8b\u5b9e\u4e09", "\u4e8b\u5b9e\u56db"]
    with fts.cursor() as conn:
        _seed_facts(conn, name="tool-ripgrep.md", facts=four)
        _seed_facts(conn, name="org-acme.md", facts=four)
        result = stage.mine_schemas_for_user(cfg, conn, llm_call=_fake_llm(_STABLE_PAYLOAD))
        paths = sorted(w.path for w in result.written)
    assert paths == ["schema-org-acme.md", "schema-tool-ripgrep.md"]


def test_collect_fact_bundles_from_evomem_reads_evo_nodes(ac_root):
    from persome.evomem.engine import EvoMemory
    from persome.writer import delta_apply

    cfg_da = SimpleNamespace(memory_delta=SimpleNamespace(apply_assertions=True))
    clean = {
        "entities": [{"ref": "\u5f20\u4f1f", "kind": "person", "ended": False, "quote": "x"}],
        "relations": [],
        "events": [],
        "assertions": [
            {
                "subject": {"ref": "\u5f20\u4f1f"},
                "text": f"\u5f20\u4f1f\u4e8b\u5b9e{i}",
                "quote": "q",
                "confidence": 0.9,
            }
            for i in range(4)
        ],
    }
    with fts.cursor() as conn:
        _seed_facts(
            conn,
            name="person-\u674e\u56db.md",
            facts=[f"\u674e\u56db\u4e8b\u5b9e{i}" for i in range(4)],
        )  # legacy entries
        delta_apply.apply_delta(conn, cfg_da, clean, memory=EvoMemory())
        legacy = {
            b.source_path for b in stage.collect_fact_bundles(conn, from_evomem=False, min_facts=4)
        }
        evo = {
            b.source_path for b in stage.collect_fact_bundles(conn, from_evomem=True, min_facts=4)
        }

    assert "person-\u674e\u56db.md" in legacy and "person-\u5f20\u4f1f.md" not in legacy
    assert "person-\u5f20\u4f1f.md" in evo and "person-\u674e\u56db.md" not in evo


def test_evomem_schema_miner_excludes_derived_person_graph_nodes(ac_root):
    from persome.evomem.models import MemoryLayer, MemoryNode
    from persome.evomem.store import NodeStore

    store = NodeStore()
    for i in range(4):
        store.save(
            MemoryNode(
                node_id=f"person-event-{i}",
                content=f"Mixed session summary {i} mentioning Kevin and the owner",
                layer=MemoryLayer.L5_KNOWLEDGE,
                file_name="person-kevin.md",
                tags="person-event",
                memory_at=datetime(2026, 7, 12, 9, i, tzinfo=UTC),
            )
        )

    with fts.cursor() as conn:
        bundles = stage.collect_fact_bundles(conn, from_evomem=True, min_facts=1)

    assert all(bundle.source_path != "person-kevin.md" for bundle in bundles)

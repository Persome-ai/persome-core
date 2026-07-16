"""update_memory — the directed memory-update entry point (2026-07-04 spec).

Deterministic, mock-LLM. "Correcting a memory" is an UPDATE: a user statement (supervised
label) → the source entry it changes → one authority-specific supersede. Covers: retire via the
directed correction path (markdown strike = supersede-not-delete), replace (with replacement),
entity-op routing to retype, noop, dry-run, and the feedback log.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from persome.store import entries as E
from persome.store import files as files_mod
from persome.store import fts
from persome.writer import correct as C


def _cfg():
    return SimpleNamespace(
        memory_delta=SimpleNamespace(apply_assertions=False),
        search=SimpleNamespace(default_top_k=5),
    )


def _llm(payload: dict):
    def _call(_messages):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))
                )
            ]
        )

    return _call


def _seed_fact(conn, content: str, name: str = "user-profile.md") -> str:
    if not conn.execute("SELECT 1 FROM files WHERE path = ?", (name,)).fetchone():
        E.create_file(conn, name=name, description="identity", tags=["identity"])
    return E.append_entry(conn, name=name, content=content, tags=["identity"])


def _md_body(root, name: str) -> str:
    p = Path(root) / "memory" / name
    return p.read_text(encoding="utf-8") if p.exists() else ""


# ── the update is one authority-specific source supersede ────────────────────


def test_correction_supersedes_via_source_authority(ac_root):
    with fts.cursor() as conn:
        eid = _seed_fact(conn, "\u6843\u5b50 is the user's Feishu display name")
        res = C.update_memory(
            _cfg(),
            conn,
            "\u6843\u5b50\u4e0d\u662f\u6211\u7684\u540d\u5b57\uff0c\u662f Dev \u7fa4\u540c\u4e8b",
            source="user",
            llm_call=_llm(
                {
                    "supersede": [
                        {
                            "file": "user-profile.md",
                            "entry_id": eid,
                            "reason": "\u6843\u5b50\u662f\u540c\u4e8b",
                        }
                    ],
                    "entity_op": None,
                    "reason": "\u6843\u5b50\u975e\u7528\u6237",
                }
            ),
        )
    assert res.kind == "update" and res.ok
    assert any("superseded" in a for a in res.applied)
    # supersede-not-delete: markdown body is STRUCK (~~...~~), bytes survive as a receipt
    body = _md_body(ac_root, "user-profile.md")
    assert "~~\u6843\u5b50 is the user's Feishu display name~~" in body


def test_replace_writes_corrected_fact(ac_root):
    with fts.cursor() as conn:
        eid = _seed_fact(conn, "User lives in Beijing")
        res = C.update_memory(
            _cfg(),
            conn,
            "\u6211\u4f4f\u5728\u4e0a\u6d77\u4e0d\u662f\u5317\u4eac",
            llm_call=_llm(
                {
                    "supersede": [
                        {
                            "file": "user-profile.md",
                            "entry_id": eid,
                            "replacement": "User lives in Shanghai",
                            "reason": "moved",
                        }
                    ],
                    "entity_op": None,
                    "reason": "moved to Shanghai",
                }
            ),
        )
    assert res.ok
    body = _md_body(ac_root, "user-profile.md")
    assert "~~User lives in Beijing~~" in body  # old struck
    assert "User lives in Shanghai" in body  # new written


def test_multi_supersede_failure_is_explicit_and_has_no_side_effect(ac_root):
    with fts.cursor() as conn:
        eid = _seed_fact(conn, "Original fact")
        before = _md_body(ac_root, "user-profile.md")
        res = C.update_memory(
            _cfg(),
            conn,
            "both candidates are wrong",
            reforward=False,
            llm_call=_llm(
                {
                    "supersede": [
                        {"file": "user-profile.md", "entry_id": eid, "reason": "first"},
                        {
                            "file": "user-profile.md",
                            "entry_id": "missing",
                            "reason": "second",
                        },
                    ],
                    "entity_op": None,
                    "reason": "two targets",
                }
            ),
        )

    assert res.kind == "error" and not res.ok
    assert res.applied == []
    assert "exactly one" in res.reason
    assert _md_body(ac_root, "user-profile.md") == before


def test_evomem_authority_single_correction_uses_dedicated_source_path(ac_root):
    (ac_root / "config.toml").write_text('[evomem]\nwrite_authority = "evomem"\n', encoding="utf-8")
    with fts.cursor() as conn:
        eid = _seed_fact(conn, "Canonical evomem fact")
        res = C.update_memory(
            _cfg(),
            conn,
            "this canonical fact is wrong",
            reforward=False,
            llm_call=_llm(
                {
                    "supersede": [
                        {"file": "user-profile.md", "entry_id": eid, "reason": "corrected"}
                    ],
                    "entity_op": None,
                    "reason": "remove old fact",
                }
            ),
        )
        old = conn.execute(
            "SELECT status, is_latest, valid_until FROM evo_nodes WHERE node_id=?", (eid,)
        ).fetchone()

    assert res.kind == "update" and res.ok
    assert old is not None
    assert old["status"] == "shadow"
    assert old["is_latest"] == 0
    assert old["valid_until"] is not None


def test_markdown_projection_failure_is_committed_recovered_and_retry_idempotent(
    ac_root,
    monkeypatch,
):
    with fts.cursor() as conn:
        eid = _seed_fact(conn, "Old markdown fact")
        payload = {
            "supersede": [
                {
                    "file": "user-profile.md",
                    "entry_id": eid,
                    "replacement": "One corrected markdown fact",
                    "reason": "owner correction",
                }
            ],
            "entity_op": None,
            "reason": "owner correction",
        }

        monkeypatch.setattr(
            E,
            "derived_supersede_rows",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("projection boom")),
        )
        first = C.update_memory(
            _cfg(),
            conn,
            "correct the markdown fact",
            reforward=False,
            llm_call=_llm(payload),
        )
        second = C.update_memory(
            _cfg(),
            conn,
            "correct the markdown fact",
            reforward=False,
            llm_call=_llm(payload),
        )
        old = conn.execute("SELECT superseded FROM entries WHERE id=?", (eid,)).fetchone()
        successors = conn.execute(
            "SELECT id, content FROM entries WHERE path='user-profile.md' AND id<>?",
            (eid,),
        ).fetchall()

    body = _md_body(ac_root, "user-profile.md")
    assert first.ok and any("projection recovered" in row for row in first.applied)
    assert second.ok
    assert body.count("One corrected markdown fact") == 1
    assert old is not None and old["superseded"] == 1
    assert len(successors) == 1


def test_markdown_source_first_recovery_also_repairs_established_evomem_shadow(
    ac_root,
    monkeypatch,
):
    from persome.evomem import backfill

    with fts.cursor() as conn:
        eid = _seed_fact(conn, "Shadowed old fact")
    assert backfill.run_backfill().ok

    with fts.cursor() as conn:
        monkeypatch.setattr(
            E,
            "derived_supersede_rows",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("projection boom")),
        )
        result = C.update_memory(
            _cfg(),
            conn,
            "correct the shadowed fact",
            reforward=False,
            llm_call=_llm(
                {
                    "supersede": [
                        {
                            "file": "user-profile.md",
                            "entry_id": eid,
                            "replacement": "Shadowed new fact",
                            "reason": "owner correction",
                        }
                    ],
                    "entity_op": None,
                    "reason": "owner correction",
                }
            ),
        )
        old = conn.execute(
            "SELECT status, is_latest, superseded_by FROM evo_nodes WHERE node_id=?",
            (eid,),
        ).fetchone()
        heads = conn.execute(
            "SELECT node_id, content FROM evo_nodes "
            "WHERE file_name='user-profile.md' AND is_latest=1 AND status='active'"
        ).fetchall()

    assert result.ok and any("projection recovered" in row for row in result.applied)
    assert old is not None and old["status"] == "shadow" and old["is_latest"] == 0
    assert len(json.loads(old["superseded_by"])) == 1
    assert len(heads) == 1 and "Shadowed new fact" in heads[0]["content"]


def test_markdown_idempotent_retry_repairs_shadow_after_first_rebuild_failure(
    ac_root,
    monkeypatch,
):
    from persome.evomem import backfill

    with fts.cursor() as conn:
        eid = _seed_fact(conn, "Retry shadow old fact")
    assert backfill.run_backfill().ok

    real_rebuild = E.rebuild_index
    rebuild_calls = 0

    def fail_first_rebuild(*args, **kwargs):
        nonlocal rebuild_calls
        rebuild_calls += 1
        if rebuild_calls == 1:
            raise RuntimeError("rebuild boom")
        return real_rebuild(*args, **kwargs)

    monkeypatch.setattr(
        E,
        "derived_supersede_rows",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("projection boom")),
    )
    monkeypatch.setattr(E, "rebuild_index", fail_first_rebuild)
    payload = {
        "supersede": [
            {
                "file": "user-profile.md",
                "entry_id": eid,
                "replacement": "Retry shadow new fact",
                "reason": "owner correction",
            }
        ],
        "entity_op": None,
        "reason": "owner correction",
    }
    with fts.cursor() as conn:
        first = C.update_memory(
            _cfg(), conn, "retry shadow correction", reforward=False, llm_call=_llm(payload)
        )
        second = C.update_memory(
            _cfg(), conn, "retry shadow correction", reforward=False, llm_call=_llm(payload)
        )
        old = conn.execute(
            "SELECT status, is_latest, superseded_by FROM evo_nodes WHERE node_id=?",
            (eid,),
        ).fetchone()
        heads = conn.execute(
            "SELECT content FROM evo_nodes "
            "WHERE file_name='user-profile.md' AND is_latest=1 AND status='active'"
        ).fetchall()

    assert first.ok and any("projection degraded" in row for row in first.applied)
    assert second.ok
    assert old is not None and old["status"] == "shadow" and old["is_latest"] == 0
    assert len(json.loads(old["superseded_by"])) == 1
    assert len(heads) == 1 and "Retry shadow new fact" in heads[0]["content"]


def test_markdown_false_shadow_repair_is_reported_as_degraded(ac_root, monkeypatch):
    with fts.cursor() as conn:
        eid = _seed_fact(conn, "Old fact with unavailable shadow repair")
        monkeypatch.setattr(
            C.evo_shadow, "repair_after_markdown_commit", lambda *_args, **_kwargs: False
        )
        result = C.update_memory(
            _cfg(),
            conn,
            "correct the fact",
            reforward=False,
            llm_call=_llm(
                {
                    "supersede": [
                        {
                            "file": "user-profile.md",
                            "entry_id": eid,
                            "replacement": "New fact with unavailable shadow repair",
                            "reason": "owner correction",
                        }
                    ],
                    "entity_op": None,
                    "reason": "owner correction",
                }
            ),
        )

    assert result.ok
    assert any("authority projection degraded" in row for row in result.applied)


def test_markdown_committed_projection_degraded_is_not_reported_as_zero_apply(
    ac_root,
    monkeypatch,
):
    with fts.cursor() as conn:
        eid = _seed_fact(conn, "Old source fact")
        monkeypatch.setattr(
            E,
            "derived_supersede_rows",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("projection boom")),
        )
        monkeypatch.setattr(
            E,
            "rebuild_index",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("rebuild boom")),
        )
        result = C.update_memory(
            _cfg(),
            conn,
            "correct the source fact",
            reforward=False,
            llm_call=_llm(
                {
                    "supersede": [
                        {
                            "file": "user-profile.md",
                            "entry_id": eid,
                            "replacement": "Committed source fact",
                            "reason": "owner correction",
                        }
                    ],
                    "entity_op": None,
                    "reason": "owner correction",
                }
            ),
        )

    assert result.kind == "update" and result.ok
    assert any("projection degraded" in row for row in result.applied)
    assert "Committed source fact" in _md_body(ac_root, "user-profile.md")


def test_markdown_retry_with_different_replacement_conflicts_without_fork(ac_root):
    with fts.cursor() as conn:
        eid = _seed_fact(conn, "Original source fact")

        def run(replacement: str):
            return C.update_memory(
                _cfg(),
                conn,
                "correct the source fact",
                reforward=False,
                llm_call=_llm(
                    {
                        "supersede": [
                            {
                                "file": "user-profile.md",
                                "entry_id": eid,
                                "replacement": replacement,
                                "reason": "owner correction",
                            }
                        ],
                        "entity_op": None,
                        "reason": "owner correction",
                    }
                ),
            )

        first = run("First replacement")
        conflicting = run("Different replacement")

    body = _md_body(ac_root, "user-profile.md")
    assert first.ok
    assert conflicting.kind == "error" and not conflicting.ok
    assert body.count("First replacement") == 1
    assert "Different replacement" not in body


def test_correction_uses_one_authority_snapshot_when_config_changes_mid_write(
    ac_root,
    monkeypatch,
):
    with fts.cursor() as conn:
        eid = _seed_fact(conn, "Authority snapshot old fact")
        real_supersede = E.supersede_entry

        def switch_then_supersede(*args, **kwargs):
            (ac_root / "config.toml").write_text(
                '[evomem]\nwrite_authority = "evomem"\n',
                encoding="utf-8",
            )
            return real_supersede(*args, **kwargs)

        monkeypatch.setattr(E, "supersede_entry", switch_then_supersede)
        result = C.update_memory(
            _cfg(),
            conn,
            "change authority during correction",
            reforward=False,
            llm_call=_llm(
                {
                    "supersede": [
                        {
                            "file": "user-profile.md",
                            "entry_id": eid,
                            "replacement": "Authority snapshot new fact",
                            "reason": "owner correction",
                        }
                    ],
                    "entity_op": None,
                    "reason": "owner correction",
                }
            ),
        )
        old = conn.execute("SELECT superseded FROM entries WHERE id=?", (eid,)).fetchone()
        current = conn.execute(
            "SELECT content FROM entries WHERE path='user-profile.md' AND superseded=0"
        ).fetchall()

    assert result.ok
    assert old is not None and old["superseded"] == 1
    assert [row["content"] for row in current] == ["Authority snapshot new fact"]
    body = _md_body(ac_root, "user-profile.md")
    assert "~~Authority snapshot old fact~~" in body
    assert "Authority snapshot new fact" in body


def test_markdown_retire_projection_failure_and_retry_are_idempotent(ac_root, monkeypatch):
    with fts.cursor() as conn:
        eid = _seed_fact(conn, "Fact to retire")
        payload = {
            "supersede": [{"file": "user-profile.md", "entry_id": eid, "reason": "not true"}],
            "entity_op": None,
            "reason": "not true",
        }
        monkeypatch.setattr(
            E,
            "derived_retire_rows",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("projection boom")),
        )
        first = C.update_memory(
            _cfg(), conn, "retire the fact", reforward=False, llm_call=_llm(payload)
        )
        after_first = _md_body(ac_root, "user-profile.md")
        second = C.update_memory(
            _cfg(), conn, "retire the fact", reforward=False, llm_call=_llm(payload)
        )

    assert first.ok and second.ok
    assert any("projection recovered" in row for row in first.applied)
    assert _md_body(ac_root, "user-profile.md") == after_first


def test_evomem_projection_failure_is_committed_recovered_and_retry_has_one_head(
    ac_root,
    monkeypatch,
):
    (ac_root / "config.toml").write_text('[evomem]\nwrite_authority = "evomem"\n', encoding="utf-8")
    with fts.cursor() as conn:
        eid = _seed_fact(conn, "Old canonical fact")
        payload = {
            "supersede": [
                {
                    "file": "user-profile.md",
                    "entry_id": eid,
                    "replacement": "One canonical replacement",
                    "reason": "owner correction",
                }
            ],
            "entity_op": None,
            "reason": "owner correction",
        }
        monkeypatch.setattr(
            E,
            "derived_supersede_rows",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("projection boom")),
        )
        first = C.update_memory(
            _cfg(), conn, "correct canonical fact", reforward=False, llm_call=_llm(payload)
        )
        second = C.update_memory(
            _cfg(), conn, "correct canonical fact", reforward=False, llm_call=_llm(payload)
        )
        old = conn.execute(
            "SELECT superseded_by, status, is_latest FROM evo_nodes WHERE node_id=?",
            (eid,),
        ).fetchone()
        heads = conn.execute(
            "SELECT node_id, content FROM evo_nodes "
            "WHERE file_name='user-profile.md' AND is_latest=1 AND status='active'"
        ).fetchall()

    assert first.ok and any("projection recovered" in row for row in first.applied)
    assert second.ok
    assert old is not None and old["status"] == "shadow" and old["is_latest"] == 0
    assert len(json.loads(old["superseded_by"])) == 1
    assert len(heads) == 1
    assert "One canonical replacement" in heads[0]["content"]


def test_evomem_source_first_recovery_repairs_markdown_projection(ac_root, monkeypatch):
    (ac_root / "config.toml").write_text('[evomem]\nwrite_authority = "evomem"\n', encoding="utf-8")
    with fts.cursor() as conn:
        eid = _seed_fact(conn, "Old projected canonical fact")
        real_atomic_write = files_mod.atomic_write_text
        failed = False

        def fail_first_new_projection(path, text):
            nonlocal failed
            if "New projected canonical fact" in text and not failed:
                failed = True
                raise RuntimeError("projection boom")
            return real_atomic_write(path, text)

        monkeypatch.setattr(files_mod, "atomic_write_text", fail_first_new_projection)
        result = C.update_memory(
            _cfg(),
            conn,
            "correct projected canonical fact",
            reforward=False,
            llm_call=_llm(
                {
                    "supersede": [
                        {
                            "file": "user-profile.md",
                            "entry_id": eid,
                            "replacement": "New projected canonical fact",
                            "reason": "owner correction",
                        }
                    ],
                    "entity_op": None,
                    "reason": "owner correction",
                }
            ),
        )

    assert failed
    assert result.ok and any("projection recovered" in row for row in result.applied)
    body = _md_body(ac_root, "user-profile.md")
    assert "New projected canonical fact" in body
    assert "~~Old projected canonical fact~~" in body


def test_evomem_persistent_markdown_projection_failure_is_degraded_and_skips_reforward(
    ac_root,
    monkeypatch,
):
    (ac_root / "config.toml").write_text('[evomem]\nwrite_authority = "evomem"\n', encoding="utf-8")
    with fts.cursor() as conn:
        eid = _seed_fact(conn, "Old canonical fact with stale projection")
        monkeypatch.setattr(
            files_mod,
            "atomic_write_text",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("projection boom")),
        )
        reforwarded = False

        def unexpected_reforward(*_args, **_kwargs):
            nonlocal reforwarded
            reforwarded = True
            return []

        monkeypatch.setattr(C, "_reforward", unexpected_reforward)
        result = C.update_memory(
            _cfg(),
            conn,
            "correct canonical fact with stale projection",
            reforward=True,
            llm_call=_llm(
                {
                    "supersede": [
                        {
                            "file": "user-profile.md",
                            "entry_id": eid,
                            "replacement": "Committed canonical fact with stale projection",
                            "reason": "owner correction",
                        }
                    ],
                    "entity_op": None,
                    "reason": "owner correction",
                }
            ),
        )
        canonical = conn.execute(
            "SELECT content FROM evo_nodes "
            "WHERE file_name='user-profile.md' AND is_latest=1 AND status='active'"
        ).fetchone()

    assert result.ok and any("projection degraded" in row for row in result.applied)
    assert not reforwarded
    assert canonical is not None
    assert "Committed canonical fact with stale projection" in canonical["content"]
    assert "Committed canonical fact with stale projection" not in _md_body(
        ac_root, "user-profile.md"
    )


def test_evomem_retry_with_different_replacement_conflicts_without_fork(ac_root):
    (ac_root / "config.toml").write_text('[evomem]\nwrite_authority = "evomem"\n', encoding="utf-8")
    with fts.cursor() as conn:
        eid = _seed_fact(conn, "Original canonical source")

        def run(replacement: str):
            return C.update_memory(
                _cfg(),
                conn,
                "correct canonical source",
                reforward=False,
                llm_call=_llm(
                    {
                        "supersede": [
                            {
                                "file": "user-profile.md",
                                "entry_id": eid,
                                "replacement": replacement,
                                "reason": "owner correction",
                            }
                        ],
                        "entity_op": None,
                        "reason": "owner correction",
                    }
                ),
            )

        first = run("First canonical replacement")
        conflicting = run("Different canonical replacement")
        old = conn.execute(
            "SELECT superseded_by FROM evo_nodes WHERE node_id=?",
            (eid,),
        ).fetchone()
        heads = conn.execute(
            "SELECT content FROM evo_nodes "
            "WHERE file_name='user-profile.md' AND is_latest=1 AND status='active'"
        ).fetchall()

    assert first.ok
    assert conflicting.kind == "error" and not conflicting.ok
    assert old is not None and len(json.loads(old["superseded_by"])) == 1
    assert len(heads) == 1
    assert "First canonical replacement" in heads[0]["content"]
    assert "Different canonical replacement" not in heads[0]["content"]


def test_entity_op_routes_to_retype(ac_root, monkeypatch):
    calls = {}

    def fake_merge(name, keeper, cfg, *, memory=None):
        calls["merge"] = (name, keeper)
        return SimpleNamespace()

    monkeypatch.setattr("persome.evomem.retype.merge_alias", fake_merge)
    with fts.cursor() as conn:
        res = C.update_memory(
            _cfg(),
            conn,
            "\u5c0f\u5f20\u5c31\u662f\u5f20\u4e09",
            llm_call=_llm(
                {
                    "supersede": [],
                    "entity_op": {
                        "op": "merge",
                        "entity": "\u5c0f\u5f20",
                        "keeper": "\u5f20\u4e09",
                    },
                    "reason": "same person",
                }
            ),
        )
    assert res.ok and calls.get("merge") == ("\u5c0f\u5f20", "\u5f20\u4e09")


def test_entity_op_can_merge_identity_into_reserved_self(ac_root):
    with fts.cursor() as conn:
        res = C.update_memory(
            _cfg(),
            conn,
            "Casey-Example is my GitHub handle",
            llm_call=_llm(
                {
                    "supersede": [],
                    "entity_op": {"op": "merge_into_self", "entity": "Casey-Example"},
                    "reason": "owner handle",
                }
            ),
        )
        row = conn.execute(
            "SELECT status, decision_source FROM owner_aliases WHERE alias_key='casey-example'"
        ).fetchone()

    assert res.ok and "merged Casey-Example → self" in res.applied
    assert tuple(row) == ("active", "user")


def test_entity_op_can_reject_false_owner_alias(ac_root):
    with fts.cursor() as conn:
        res = C.update_memory(
            _cfg(),
            conn,
            "Kevin is my teammate, not me",
            llm_call=_llm(
                {
                    "supersede": [],
                    "entity_op": {"op": "reject_owner_alias", "entity": "Kevin"},
                    "reason": "collaborator",
                }
            ),
        )
        status = conn.execute(
            "SELECT status FROM owner_aliases WHERE alias_key='kevin'"
        ).fetchone()[0]

    assert res.ok and status == "rejected"


def test_owner_alias_correction_preserves_agent_provenance(ac_root):
    with fts.cursor() as conn:
        res = C.update_memory(
            _cfg(),
            conn,
            "Casey-Example is the owner's GitHub handle",
            source="agent",
            llm_call=_llm(
                {
                    "supersede": [],
                    "entity_op": {"op": "merge_into_self", "entity": "Casey-Example"},
                    "reason": "owner handle",
                }
            ),
        )
        source = conn.execute(
            "SELECT decision_source FROM owner_aliases WHERE alias_key='casey-example'"
        ).fetchone()[0]

    assert res.ok and source == "agent"


def test_noop_when_nothing_to_update(ac_root):
    with fts.cursor() as conn:
        res = C.update_memory(
            _cfg(),
            conn,
            "\u968f\u4fbf\u8bf4\u8bf4",
            llm_call=_llm(
                {"supersede": [], "entity_op": None, "reason": "\u65e0\u5bf9\u5e94\u6e90"}
            ),
        )
    assert res.kind == "noop" and not res.ok


def test_dry_run_previews_without_applying(ac_root):
    with fts.cursor() as conn:
        eid = _seed_fact(conn, "\u6843\u5b50 is the user's name")
        res = C.update_memory(
            _cfg(),
            conn,
            "\u6843\u5b50\u4e0d\u662f\u6211",
            dry_run=True,
            llm_call=_llm(
                {
                    "supersede": [{"file": "user-profile.md", "entry_id": eid}],
                    "entity_op": None,
                    "reason": "x",
                }
            ),
        )
        assert not res.ok and any("would retire" in a for a in res.applied)
        # NOT applied — body intact
    assert "~~" not in _md_body(ac_root, "user-profile.md")


def test_update_logged_as_feedback_signal(ac_root):
    with fts.cursor() as conn:
        eid = _seed_fact(conn, "wrong fact about the user")
        C.update_memory(
            _cfg(),
            conn,
            "\u90a3\u6761\u9519\u4e86",
            source="user",
            llm_call=_llm(
                {
                    "supersede": [{"file": "user-profile.md", "entry_id": eid, "reason": "r"}],
                    "entity_op": None,
                    "reason": "r",
                }
            ),
        )
    log = Path(ac_root) / "logs" / "memory-updates.jsonl"
    assert log.exists()
    assert log.stat().st_mode & 0o777 == 0o600
    row = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert row["source"] == "user" and row["kind"] == "update"

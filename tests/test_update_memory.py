"""update_memory — the directed memory-update entry point (2026-07-04 spec).

Deterministic, mock-LLM. "Correcting a memory" is an UPDATE: a user statement (supervised
label) → the delta it implies → applied through the SAME executor as observation
(``delta_apply`` ⊖ supersede leg). Covers: retire via the ⊖ leg (markdown strike =
supersede-not-delete), replace (with replacement), entity-op routing to retype, noop, dry-run,
and the feedback log.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from persome.store import entries as E
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


# ── the update is a supersede delta through the shared executor ───────────────


def test_correction_supersedes_via_delta_apply(ac_root):
    with fts.cursor() as conn:
        eid = _seed_fact(conn, "桃子 is the user's Feishu display name")
        res = C.update_memory(
            _cfg(),
            conn,
            "桃子不是我的名字，是 Dev 群同事",
            source="user",
            llm_call=_llm(
                {
                    "supersede": [
                        {"file": "user-profile.md", "entry_id": eid, "reason": "桃子是同事"}
                    ],
                    "entity_op": None,
                    "reason": "桃子非用户",
                }
            ),
        )
    assert res.kind == "update" and res.ok
    assert any("superseded" in a for a in res.applied)
    # supersede-not-delete: markdown body is STRUCK (~~...~~), bytes survive as a receipt
    body = _md_body(ac_root, "user-profile.md")
    assert "~~桃子 is the user's Feishu display name~~" in body


def test_replace_writes_corrected_fact(ac_root):
    with fts.cursor() as conn:
        eid = _seed_fact(conn, "User lives in Beijing")
        res = C.update_memory(
            _cfg(),
            conn,
            "我住在上海不是北京",
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
            "小张就是张三",
            llm_call=_llm(
                {
                    "supersede": [],
                    "entity_op": {"op": "merge", "entity": "小张", "keeper": "张三"},
                    "reason": "same person",
                }
            ),
        )
    assert res.ok and calls.get("merge") == ("小张", "张三")


def test_noop_when_nothing_to_update(ac_root):
    with fts.cursor() as conn:
        res = C.update_memory(
            _cfg(),
            conn,
            "随便说说",
            llm_call=_llm({"supersede": [], "entity_op": None, "reason": "无对应源"}),
        )
    assert res.kind == "noop" and not res.ok


def test_dry_run_previews_without_applying(ac_root):
    with fts.cursor() as conn:
        eid = _seed_fact(conn, "桃子 is the user's name")
        res = C.update_memory(
            _cfg(),
            conn,
            "桃子不是我",
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
            "那条错了",
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
    row = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert row["source"] == "user" and row["kind"] == "update"

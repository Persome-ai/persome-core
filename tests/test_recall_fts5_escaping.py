"""Recall hint → FTS5 MATCH 转义（生产实测 bug，PR-7 顺手修）。

实测形态：折叠查询的 hint 含撇号时 FTS5 语法错误被跳过
（``fold query failed for hint "User's": fts5: syntax error near "'"``）——
命中静默丢失。修复：``_fts5_match_expr`` 把 hint 按空白分词、每个 token 双引号
包裹（内部双引号翻倍）后再进 MATCH——引号串内一切字符均为字面量，普通 hint 的
匹配语义与裸词逐字相同。
"""

from __future__ import annotations

from persome.intent import recall
from persome.store import entries as entries_mod
from persome.store import fts


def _seed(conn) -> None:
    entries_mod.create_file(conn, name="user-preferences.md", description="p", tags=["t"])
    entries_mod.append_entry(
        conn, name="user-preferences.md", content="User's preference is dark roast", tags=["t"]
    )
    entries_mod.append_entry(
        conn, name="user-preferences.md", content="用户说 User's 偏好 是手冲咖啡", tags=["t"]
    )
    entries_mod.append_entry(
        conn, name="user-preferences.md", content='他说"好"，确认了 evening 方案', tags=["t"]
    )


# ── 单元：表达式形态 ──────────────────────────────────────────────────────────


def test_match_expr_quotes_every_token() -> None:
    assert recall._fts5_match_expr("User's") == '"User\'s"'
    assert recall._fts5_match_expr("foo bar") == '"foo" "bar"'
    # 内部双引号翻倍（FTS5 字符串转义规则）
    assert recall._fts5_match_expr('他说"好"') == '"他说""好"""'
    assert recall._fts5_match_expr("") == ""


# ── 行为：撇号 / 引号 / 中文+撇号混合 hint 不再被跳过 ─────────────────────────


def test_apostrophe_hint_matches_instead_of_erroring(ac_root, caplog) -> None:
    with fts.cursor() as conn:
        _seed(conn)
        with caplog.at_level("WARNING", logger="persome.intent.recall"):
            out = recall.assemble_background(
                conn, scope="", hints=["User's"], per_hint=10, fold_superseded=True
            )
    assert "dark roast" in out
    assert not any("query failed" in r.message for r in caplog.records)


def test_mixed_cjk_apostrophe_hint(ac_root, caplog) -> None:
    """中文+撇号混合 hint：整词当短语匹配，命中含同形词序的条目。"""
    with fts.cursor() as conn:
        _seed(conn)
        with caplog.at_level("WARNING", logger="persome.intent.recall"):
            out = recall.assemble_background(
                conn, scope="", hints=["User's 偏好"], per_hint=10, fold_superseded=True
            )
    assert "手冲咖啡" in out
    assert not any("query failed" in r.message for r in caplog.records)


def test_double_quote_hint_does_not_crash(ac_root, caplog) -> None:
    with fts.cursor() as conn:
        _seed(conn)
        with caplog.at_level("WARNING", logger="persome.intent.recall"):
            out = recall.assemble_background(
                conn, scope="", hints=['他说"好"'], per_hint=10, fold_superseded=True
            )
    assert "evening" in out
    assert not any("query failed" in r.message for r in caplog.records)


def test_plain_hint_output_unchanged(ac_root) -> None:
    """普通 hint 的输出与裸词形态逐字相同（引号串 ≡ 裸词的匹配语义）。"""
    with fts.cursor() as conn:
        _seed(conn)
        quoted = recall.assemble_background(conn, scope="", hints=["roast"], per_hint=10)
        # 直接用裸词跑底层查询对照（修复前的形态）
        rows = conn.execute(
            "SELECT path, content FROM entries WHERE entries MATCH ? ORDER BY rank LIMIT ?",
            ("roast", 10),
        ).fetchall()
    assert rows, "bareword baseline must hit"
    for r in rows:
        assert r["content"][:40] in quoted or r["content"][:300] in quoted

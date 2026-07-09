"""§1.2 维度判据的确定性接纳闸 — is this candidate a POINT?

The unified dimension criterion (product ruling 2026-07-03, spec §1.2): an
element's dimension IS the type of the minimal set of other elements its
complete description must reference; a candidate that no reference can pin to
a unique referent is not an element at all — it is a VALUE on some axis. For
points (∅ reference) the three conditions are 唯一指称 ∧ 状态自足 ∧ 可自定位.

``adjudicate`` operationalizes the deterministically-decidable slice as a
MECE decision chain — every candidate lands in exactly one verdict cell:

1. **alias** (是点，但不是新点): the normalized-alphanumeric form equals an
   existing canonical (``dev-群`` ≡ ``dev群``), or the candidate is an
   existing canonical wearing a decoration affix (``singularity-沈砚舟``).
2. **not_point** (不是元素——轴上的值): the name IS a class/generic (客户/
   团队/群聊/会议纪要…) or a role designation (…面试官/…负责人) — it denotes
   a set or a function, never a unique referent. The lexicon/patterns are a
   CURATED CLOSED SET grown by golden failures (data, not reactive prose).
3. **point** (三条判据全过): a unique-referent name WITH kind evidence —
   the ``kind_hint`` the caller extracted (delta's entities.kind head, a
   typed file prefix, a person-roster hit). State completeness = the kind
   axis has a definite value; self-locatability follows (position is a pure
   function of id+state, §7-6).
4. **defer** (当下不可判): unique-referent shaped but NO kind evidence yet —
   the honest gate action is to WAIT in shadow (§4.3 接纳闸: 推迟而非武断),
   never to force a cell.

Zero LLM, zero I/O — pure function over (candidate, roster, kind_hint) so the
golden gate can pin it deterministically. The LLM tier (delta's admission
principle in the prompt) handles what this slice honestly cannot: world-
knowledge uniqueness judgments.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# 泛称/类/形态 — 指称集合而非个体（curated closed set；每次 golden 失败补数据）
GENERIC_LEXICON = frozenset(
    {
        "客户",
        "团队",
        "大家",
        "群聊",
        "会议纪要",
        "同事",
        "会议",
        "联系人",
    }
)

# 角色指称 — 指称函数（谁当时占着这个角色）而非个体；消解出唯一自然人前不铸点
ROLE_PATTERNS = (
    re.compile(r".+面试官$"),
    re.compile(r".+负责人$"),
    re.compile(r".+接待员$"),
)

VALID_KINDS = frozenset({"person", "org", "project", "artifact"})

# decoration affix separators for the alias rule (rule 2)
_AFFIX_SEPARATORS = "-_·— ．."


@dataclass
class Verdict:
    verdict: str  # point | not_point | alias | defer
    kind: str | None = None  # for point
    alias_of: str | None = None  # for alias
    criterion: str = ""  # which §1.2 leg decided (trace, for the golden diff)


def _norm(name: str) -> str:
    return unicodedata.normalize("NFKC", name or "").strip().casefold()


def _alnum(name: str) -> str:
    return "".join(ch for ch in _norm(name) if ch.isalnum())


def adjudicate(
    candidate: str,
    *,
    roster: list[str] | None = None,
    kind_hint: str | None = None,
) -> Verdict:
    """One candidate through the §1.2 chain. ``roster`` = existing canonical
    identities (aliases resolved upstream by the identity funnel); ``kind_hint``
    = the caller's kind evidence (delta entities.kind / typed file prefix)."""
    name = (candidate or "").strip()
    if not name:
        return Verdict("not_point", criterion="唯一指称：空指称")

    known = [c for c in (roster or []) if c and c.strip()]
    n_cand = _alnum(name)

    # ── rule 1+2: alias（是点但不是新点）──
    for canonical in known:
        if _norm(canonical) == _norm(name):
            continue  # same identity, not an alias verdict — funnel's job
        if n_cand and n_cand == _alnum(canonical):
            return Verdict(
                "alias", alias_of=canonical, criterion="唯一指称：规范化同形（分隔符装饰）"
            )
        c_norm = _norm(canonical)
        cand_norm = _norm(name)
        if len(c_norm) >= 2 and cand_norm != c_norm:
            if (
                cand_norm.endswith(c_norm)
                and cand_norm[: -len(c_norm)].rstrip(_AFFIX_SEPARATORS) != cand_norm[: -len(c_norm)]
            ):
                return Verdict(
                    "alias", alias_of=canonical, criterion="唯一指称：既有个体加前缀装饰"
                )
            if (
                cand_norm.startswith(c_norm)
                and cand_norm[len(c_norm) :].lstrip(_AFFIX_SEPARATORS) != cand_norm[len(c_norm) :]
            ):
                return Verdict(
                    "alias", alias_of=canonical, criterion="唯一指称：既有个体加后缀装饰"
                )

    # ── rule 3: 类/角色/泛称 = 轴上的值 ──
    if _norm(name) in {_norm(g) for g in GENERIC_LEXICON}:
        return Verdict("not_point", criterion="唯一指称：泛称/类/形态指称集合")
    for pat in ROLE_PATTERNS:
        if pat.match(name):
            return Verdict("not_point", criterion="唯一指称：角色指称函数（未消解个体）")

    # ── rule 4: 状态自足 = kind 轴有定值（证据由调用方提供）──
    if kind_hint in VALID_KINDS:
        # 可自定位随之成立：π(x)=f(id,σ(x)) 单点可算（§7-6 布局是纯函数）
        return Verdict("point", kind=kind_hint, criterion="三判据全过")

    # ── rule 5: 唯一指称形似成立但无 kind 证据 → 推迟（§4.3 接纳闸）──
    return Verdict("defer", criterion="状态自足：kind 轴无定值，留 shadow 等证据")

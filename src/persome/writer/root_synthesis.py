"""root_synthesis — the level-3 apex 合成（2026-07-04 spec: Memory Root Apex）。

夜间（schema-tick 尾，face mining + cross-domain sweep 之后吃最新体）一趟有界 LLM：读
**活跃体（level-2）+ top-k 面（level-1）+ 耐久 profile 事实**，写出整张记忆图**唯一的、
≤1500-token 的 apex 叙事**——「这个人是谁·最要紧的是什么·当前在推进的大事」——句中挂
⟨体⟩ 把手供下钻。三道确定性闸后 `upsert_root` born-active（chain-supersede 旧 root）：

  1. **非空输入**：无体且无 profile → 不合成（无从压缩），保留旧 root。
  2. **token 封顶**：`_fit_budget` 句界硬截断到 ≤budget（确定性，无二次 LLM）。
  3. **提及子集反幻觉**：root 点名的 roster 实体 ⊆ 输入点名的实体（凭空冒出的人名 → 拒，保留旧 root）。
  4. **非空输出**：空/退化叙事 → 保留旧 root。

默认 ON（产品方 2026-07-04）；fail-open——任何异常只 log，绝不抛给 tick。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..evomem import identity
from ..evomem._json import parse_json_object
from ..evomem.models import MemoryStatus
from ..logger import get
from ..store import schema_faces
from ..writer import llm as llm_mod

logger = get("persome.writer.root_synthesis")

_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "root_synthesis.md"
_TOP_FACES = 12  # top-k 面 fed alongside the 体
_PROFILE_FILES = ("schema-user-profile.md", "user-profile.md", "user-preferences.md")


@dataclass(frozen=True)
class RootResult:
    face_id: str | None
    reason: str  # written | skip_empty_input | skip_empty_output | skip_hallucination | disabled | error


# ── token budget（确定性代理 + 句界截断闸）───────────────────────────────────


def estimate_tokens(text: str) -> int:
    """Deterministic token proxy: CJK ~1 tok/char, else ~1 tok/4 chars. Backstop for
    the budget gate — the prompt already targets the budget; this catches overflow."""
    cjk = sum(1 for c in text if "一" <= c <= "鿿")
    other = len(text) - cjk
    return cjk + (other + 3) // 4


_TRUNCATE_RECEIPT = " …⟨truncated⟩"


def fit_budget(text: str, budget: int) -> str:
    """Token-cap at a sentence boundary + a truncation receipt, honoring ``budget``
    INCLUDING the receipt (reserve its tokens up front so the result is ≤ budget).
    Deterministic (no second LLM call) so the offline daemon gate covers it."""
    text = text.strip()
    if estimate_tokens(text) <= budget:
        return text
    target = max(1, budget - estimate_tokens(_TRUNCATE_RECEIPT))  # leave room for the receipt
    lo, hi, best = 0, len(text), 0
    while lo <= hi:  # largest prefix within the reserved target
        mid = (lo + hi) // 2
        if estimate_tokens(text[:mid]) <= target:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    cut = text[:best]
    idx = max((cut.rfind(c) for c in "。！？!?\n"), default=-1)
    if idx > len(cut) * 0.5:  # prefer a clean sentence stop if it doesn't lose too much
        cut = cut[: idx + 1]
    return cut.rstrip() + _TRUNCATE_RECEIPT


# ── input gathering ──────────────────────────────────────────────────────────


def _profile_facts(cfg: Any) -> list[str]:
    """Durable identity/preference/project descriptions off the memory dir (best-effort,
    fail-open). Mirrors Swift MemoryDigest.profileFacts so both sides feed the apex the
    same durable spine."""
    try:
        from .. import paths

        mdir = paths.memory_dir()
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []
    try:
        names = {p.name for p in mdir.iterdir() if p.suffix == ".md"}
    except Exception:  # noqa: BLE001
        return []

    def _desc(name: str) -> str | None:
        try:
            text = (mdir / name).read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            return None
        # schema-*.md: the frontmatter ``description:`` is set once and NOT refreshed on re-mine —
        # it goes stale after a correction re-derives the schema. Read the LATEST LIVE ``central:``
        # (a re-mine appends a fresh central + strikes/supersedes the old) so the apex sees the
        # re-run forward pass, not a cached summary. This is the read-fresh half of the closed loop.
        if name.startswith("schema-"):
            central = None
            for line in text.splitlines():
                s = line.strip()
                if s.startswith("central:"):  # live one; struck versions start with '~~central:'
                    central = s.split(":", 1)[1].strip()[:300]
            if central:
                return central
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("description:"):
                return s.split(":", 1)[1].strip().strip("\"'")[:300] or None
        # fall back to first non-heading, non-frontmatter content line
        for line in text.splitlines():
            s = line.strip()
            if s and not s.startswith(("#", "---", "name:", "metadata:")):
                return s[:300]
        return None

    for name in _PROFILE_FILES:
        if name in names and (d := _desc(name)):
            out.append(d)
    for name in sorted(n for n in names if n.startswith("project-")):
        if d := _desc(name):
            out.append(f"项目 {name[8:-3]}：{d}")
    return out[:8]


def _active(conn: sqlite3.Connection, level: int, limit: int | None = None) -> list[sqlite3.Row]:
    schema_faces.ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    q = (
        "SELECT * FROM schema_faces WHERE level = ? AND status = ? AND valid_to IS NULL"
        " ORDER BY observations DESC, confidence DESC, face_id"
    )
    args: tuple = (level, MemoryStatus.ACTIVE.value)
    if limit is not None:
        q += " LIMIT ?"
        args += (limit,)
    return list(conn.execute(q, args))


def _build_user_prompt(bodies: list[sqlite3.Row], faces: list[sqlite3.Row], profile: list[str]) -> str:
    parts: list[str] = []
    if bodies:
        parts.append(
            "## 活跃体（level-2，跨域涌现的高层规律 — apex 的主料）\n"
            + "\n".join(f"- ⟨{b['signature']}⟩" for b in bodies)
        )
    if faces:
        parts.append(
            "## 活跃面（level-1，稳定行为规律 top-k）\n"
            + "\n".join(f"- {f['signature']}" for f in faces)
        )
    if profile:
        parts.append("## 耐久 profile（身份/偏好/项目）\n" + "\n".join(f"- {p}" for p in profile))
    parts.append(
        "请据以上材料，按系统提示写出这个人的**单一 apex 速写**（≤预算 token），"
        "句中对可下钻的体用 ⟨体签名⟩ 挂把手，输出 JSON。"
    )
    return "\n\n".join(parts)


def _content_of(resp: Any) -> str:
    try:
        return resp.choices[0].message.content or ""
    except (AttributeError, IndexError):
        return ""


def _load_prompt(budget: int) -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8").replace("{{BUDGET}}", str(budget))


def _build_llm_call(cfg: Any) -> Callable[[list[dict]], Any]:
    def _call(messages: list[dict]) -> Any:
        return llm_mod.call_llm(cfg, "root_synthesis", messages=messages, json_mode=True)

    return _call


# ── the pass ─────────────────────────────────────────────────────────────────


def synthesize_root(
    cfg: Any,
    conn: sqlite3.Connection,
    *,
    llm_call: Callable[[list[dict]], Any] | None = None,
    budget: int | None = None,
    roster: Any | None = None,
) -> RootResult:
    """Gather → LLM → 4 gates → upsert_root born-active. Injectable ``llm_call``/``roster``
    for tests. Returns a RootResult; NEVER raises (fail-open is the tick's contract, but we
    also guard here)."""
    budget = int(budget if budget is not None else getattr(cfg.schema, "root_token_budget", 1500))
    try:
        bodies = _active(conn, 2)
        faces = _active(conn, 1, _TOP_FACES)
        profile = _profile_facts(cfg)
        if not bodies and not profile:  # gate 1: nothing to compress → keep prior root
            return RootResult(None, "skip_empty_input")

        roster = roster if roster is not None else identity.load_roster(cfg)
        input_text = "\n".join(
            [b["signature"] for b in bodies] + [f["signature"] for f in faces] + profile
        )
        input_entities = set(identity.scan_mentions(input_text, roster))

        call = llm_call or _build_llm_call(cfg)
        messages = [
            {"role": "system", "content": _load_prompt(budget)},
            {"role": "user", "content": _build_user_prompt(bodies, faces, profile)},
        ]
        parsed = parse_json_object(_content_of(call(messages))) or {}
        apex = str(parsed.get("apex", "")).strip()
        if not apex:  # gate 4: empty/degenerate output → keep prior
            return RootResult(None, "skip_empty_output")

        apex = fit_budget(apex, budget)  # gate 2: token cap (deterministic)
        root_entities = set(identity.scan_mentions(apex, roster))
        if not root_entities.issubset(input_entities):  # gate 3: 提及子集反幻觉
            logger.warning(
                "root_synthesis: hallucinated entities %s not in input — keeping prior root",
                sorted(root_entities - input_entities),
            )
            return RootResult(None, "skip_hallucination")

        # anchors = the entities the apex names (progressive-disclosure handles) ∪ the 体 fused
        anchors = sorted(root_entities)
        members = [b["face_id"] for b in bodies]
        face_id = schema_faces.upsert_root(
            conn, signature=apex, members=members, anchors=anchors, confidence=1.0
        )
        logger.info(
            "root_synthesis: wrote root %s (%d tok, %d 体, %d anchors)",
            face_id,
            estimate_tokens(apex),
            len(members),
            len(anchors),
        )
        return RootResult(face_id, "written")
    except Exception:  # noqa: BLE001 — fail-open; a bad synthesis never kills the tick
        logger.exception("root_synthesis failed")
        return RootResult(None, "error")


def run_root_synthesis(cfg: Any, conn: sqlite3.Connection) -> RootResult:
    """Tick entry: gated on ``[schema] root_synthesis_enabled`` (default ON). Fail-open."""
    if not getattr(cfg.schema, "root_synthesis_enabled", True):
        return RootResult(None, "disabled")
    return synthesize_root(cfg, conn)

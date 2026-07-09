"""The SINGLE actuation safety gate — every act verb passes through `Gate.run`.

AX-first is what makes a *semantic* gate enforceable: an element carries its role + label, so the
gate can block side-effects (send / delete / pay …) until the user confirms, and verify the effect
afterwards from the AX diff. Coordinate clicking can't. Pure + injectable: the confirm step and the
actuator are seams, so the whole gate is offline-testable (the prod confirm is the daemon↔app SSE
round-trip; tests inject approve/deny).

Plan: docs/superpowers/plans/2026-06-25-persome-actuation-layer-plan.md §5.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# Verbs that mutate state → always gated.
SIDE_EFFECT_VERBS = {"setvalue"}
# Element labels that denote an irreversible / outward action → gated even for a plain press.
SIDE_EFFECT_LABEL_RE = re.compile(
    r"(send|submit|post|delete|remove|pay|purchase|buy|confirm|publish|share|"
    r"发送|发布|删除|提交|支付|购买|确认|分享)",
    re.IGNORECASE,
)

# Per-app control levels (bundle id → level). Default for unlisted apps is `full` (gated).
BLOCKED = "blocked"  # no actuation at all
READ_ONLY = "read_only"  # snapshot only
CLICK_ONLY = "click_only"  # press allowed, no setvalue
FULL = "full"  # all verbs, side-effects gated

APP_LEVELS: dict[str, str] = {
    "com.apple.Passwords": BLOCKED,
    "com.1password.1password": BLOCKED,
    "com.apple.systempreferences": BLOCKED,
    "com.apple.Safari": READ_ONLY,
    "com.google.Chrome": READ_ONLY,
    "com.microsoft.VSCode": CLICK_ONLY,
    "com.apple.Terminal": CLICK_ONLY,
}


def level_for(bundle_id: str | None) -> str:
    return APP_LEVELS.get(bundle_id or "", FULL)


@dataclass
class Decision:
    allowed: bool
    gated: bool  # requires user confirmation before performing
    reason: str = ""


def classify(*, verb: str, label: str, bundle_id: str | None) -> Decision:
    """Decide whether `verb` on an element labelled `label` in `bundle_id` is allowed / gated."""
    level = level_for(bundle_id)
    if level == BLOCKED:
        return Decision(False, False, "app blocked for actuation")
    if level == READ_ONLY and verb != "snapshot":
        return Decision(False, False, "app is read-only")
    if level == CLICK_ONLY and verb in SIDE_EFFECT_VERBS:
        return Decision(False, False, "app is click-only (no value entry)")
    gated = verb in SIDE_EFFECT_VERBS or bool(SIDE_EFFECT_LABEL_RE.search(label or ""))
    return Decision(True, gated, "side-effect" if gated else "")


def verify_from_diff(diff: list[dict[str, Any]]) -> bool:
    """A non-empty diff (something changed/appeared/disappeared) is evidence the action landed."""
    return any(d.get("change") in ("changed", "appeared", "disappeared") for d in diff or [])


# ── Freeform verbs (key / type / clickxy): no AX element, so no label to classify ──────────────
#
# The semantic label gate above can't see these — a coordinate click or a keystroke carries no role
# or label. So they're gated on three cheaper signals that need NO snapshot:
#   1. The agent's own `note` announcing a side-effect ("正在发送…" / "delete …") — app-agnostic.
#   2. Enter / Return inside a messaging / mail app (= send).
#   3. type / clickxy inside a messaging / mail app (could land in a compose box or hit Send).
# Plus the per-app levels (Passwords / System Settings → blocked; read-only apps → no freeform).

# Enter / Return = submit / send.
SUBMIT_KEY_RE = re.compile(r"\b(enter|return)\b", re.IGNORECASE)

# Messaging / mail surfaces (matched against the app NAME or bundle id the tool was given) where a
# stray type+enter or click could fire an irreversible send.
_COMMS_RE = re.compile(
    r"(wechat|微信|wecom|企业?微信|lark|飞书|feishu|mail|邮件|message|信息|imessage|slack|"
    r"telegram|whatsapp|discord|\bqq\b|com\.tencent|com\.bytedance\.lark|com\.electron\.lark|"
    r"com\.apple\.mail|com\.apple\.mobilesms|com\.tinyspeck)",
    re.IGNORECASE,
)

# Apps that are off-limits for any freeform actuation (matched on name or bundle id).
_BLOCKED_RE = re.compile(
    r"(password|1password|keychain|钥匙串|system settings|系统设置|systempreferences|系统偏好)",
    re.IGNORECASE,
)


def _is_comms(app: str) -> bool:
    return bool(_COMMS_RE.search(app or ""))


def classify_freeform(*, verb: str, keys: str = "", note: str = "", app: str = "") -> Decision:
    """Decide whether a freeform `verb` (key/type/clickxy) is allowed / needs confirmation.

    `app` is the name-or-bundle string the tool was handed; `note` is the agent's own one-line
    description of the step (a strong side-effect signal). No AX snapshot required.
    """
    if _BLOCKED_RE.search(app or ""):
        return Decision(False, False, "app blocked for actuation")
    # The agent itself announced a send/delete/pay — gate regardless of app.
    if SIDE_EFFECT_LABEL_RE.search(note or ""):
        return Decision(True, True, "step announces a side-effect")
    if verb == "key" and SUBMIT_KEY_RE.search(keys or "") and _is_comms(app):
        return Decision(True, True, "send (enter/return in a messaging app)")
    if verb in ("type", "clickxy") and _is_comms(app):
        return Decision(True, True, f"{verb} in a messaging app")
    return Decision(True, False, "")


# confirm(summary) -> bool : ask the user; True = approved. Prod wires the SSE round-trip.
ConfirmFn = Callable[[str], bool]
# perform(verb, element_id, text) -> dict : the actuator call; returns {ok, diff, ...}.
PerformFn = Callable[..., dict[str, Any]]


@dataclass
class Gate:
    confirm: ConfirmFn
    perform: PerformFn

    def run(
        self,
        *,
        verb: str,
        element_id: str,
        label: str,
        bundle_id: str | None,
        text: str | None = None,
    ) -> dict[str, Any]:
        """The one chokepoint: classify → (confirm if gated) → perform → verify."""
        d = classify(verb=verb, label=label, bundle_id=bundle_id)
        if not d.allowed:
            return {"ok": False, "error": "blocked", "reason": d.reason}
        if d.gated:
            summary = f"{verb} 『{label}』" + (f" = {text}" if text else "")
            if not self.confirm(summary):
                return {"ok": False, "error": "denied", "reason": "user declined"}
        result = self.perform(verb=verb, element_id=element_id, text=text)
        result["verified"] = verify_from_diff(result.get("diff", []))
        result["gated"] = d.gated
        return result

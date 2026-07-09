"""Communication tools — PRESENCE ONLY.

We record which chat / mail apps are installed (and how many mail accounts
exist), never message content. Knowing someone lives in Feishu + WeChat is a
strong locale/work signal; reading their messages is off-limits.
"""

from __future__ import annotations

from pathlib import Path

from .base import Signal, collector, home

# app display name -> bundle/container marker under ~/Library/Containers or /Applications
_CHAT_APPS = {
    "WeChat": "WeChat",
    "Feishu/Lark": "Feishu",
    "Lark": "Lark",
    "Slack": "Slack",
    "Telegram": "Telegram",
    "Discord": "Discord",
    "WhatsApp": "WhatsApp",
    "QQ": "QQ",
    "Signal": "Signal",
    "Zoom": "zoom.us",
}


def _app_present(marker: str) -> bool:
    for base in (Path("/Applications"), home() / "Applications"):
        if not base.is_dir():
            continue
        try:
            for entry in base.iterdir():
                if entry.suffix == ".app" and marker.lower() in entry.stem.lower():
                    return True
        except OSError:
            continue
    return False


def _mail_account_count() -> int:
    mail = home() / "Library" / "Mail"
    if not mail.is_dir():
        return 0
    count = 0
    try:
        for vdir in mail.glob("V*"):
            for acct in vdir.iterdir():
                if acct.is_dir() and ("@" in acct.name or acct.name.endswith(".mbox")):
                    count += 1
    except OSError:
        return 0
    return count


@collector("comms", "沟通工具", "comms")
def collect() -> list[Signal]:
    signals: list[Signal] = []

    present = [name for name, marker in _CHAT_APPS.items() if _app_present(marker)]
    if present:
        signals.append(Signal("聊天/会议", present))

    accounts = _mail_account_count()
    if accounts:
        signals.append(Signal("本地邮箱账户", accounts, "Apple Mail"))

    return signals

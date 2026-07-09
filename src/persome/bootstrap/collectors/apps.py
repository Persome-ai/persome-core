"""Installed applications and default browser — reveals the person's stack."""

from __future__ import annotations

from pathlib import Path

from .base import Signal, collector, home, read_text

# Curated buckets. We only surface apps that say something about the person;
# the long tail of system/utility apps is noise.
_NOTABLE: dict[str, list[str]] = {
    "编辑器/IDE": [
        "Visual Studio Code",
        "Cursor",
        "Xcode",
        "Zed",
        "Sublime Text",
        "PyCharm",
        "IntelliJ IDEA",
        "WebStorm",
        "GoLand",
        "Android Studio",
        "Nova",
        "Fleet",
    ],
    "终端": ["iTerm", "Warp", "Alacritty", "kitty", "WezTerm", "Ghostty", "Hyper"],
    "浏览器": [
        "Google Chrome",
        "Arc",
        "Safari",
        "Firefox",
        "Microsoft Edge",
        "Brave Browser",
        "Dia",
    ],
    "聊天/沟通": [
        "WeChat",
        "Feishu",
        "Lark",
        "Slack",
        "Discord",
        "Telegram",
        "WhatsApp",
        "QQ",
        "Zoom",
        "Microsoft Teams",
        "Signal",
    ],
    "设计": ["Figma", "Sketch", "Framer", "Pixelmator Pro", "Affinity Designer", "Adobe Photoshop"],
    "生产力/笔记": [
        "Notion",
        "Obsidian",
        "Bear",
        "Things3",
        "Things",
        "Craft",
        "Linear",
        "Raycast",
        "Alfred",
        "1Password",
    ],
    "AI": ["ChatGPT", "Claude", "Ollama", "LM Studio", "Doubao", "豆包", "Cherry Studio"],
    "数据/开发工具": [
        "Docker",
        "OrbStack",
        "TablePlus",
        "Postman",
        "Insomnia",
        "DBeaver",
        "Proxyman",
    ],
}


def _installed_app_names() -> set[str]:
    names: set[str] = set()
    for base in (Path("/Applications"), home() / "Applications"):
        if not base.is_dir():
            continue
        try:
            for entry in base.iterdir():
                if entry.suffix == ".app":
                    names.add(entry.stem)
        except OSError:
            continue
    return names


def _default_browser() -> str | None:
    # LaunchServices stores the https handler bundle id; map a few common ones.
    plist = (
        home()
        / "Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist"
    )
    text = read_text(plist, max_bytes=2_000_000) or ""
    bundle_map = {
        "com.google.chrome": "Google Chrome",
        "company.thebrowser.browser": "Arc",
        "com.apple.safari": "Safari",
        "org.mozilla.firefox": "Firefox",
        "com.microsoft.edgemac": "Microsoft Edge",
        "com.brave.browser": "Brave",
    }
    # The binary plist still contains readable bundle-id substrings near "https".
    if "https" in text.lower():
        for bid, friendly in bundle_map.items():
            if bid in text.lower():
                return friendly
    return None


@collector("apps", "已装应用", "apps")
def collect() -> list[Signal]:
    installed = _installed_app_names()
    if not installed:
        return []

    signals: list[Signal] = []
    # Case-insensitive membership.
    lower = {n.lower() for n in installed}
    for bucket, candidates in _NOTABLE.items():
        hits = [c for c in candidates if c.lower() in lower]
        if hits:
            signals.append(Signal(bucket, hits))

    signals.append(Signal("应用总数", len(installed), "/Applications + ~/Applications"))

    browser = _default_browser()
    if browser:
        signals.append(Signal("默认浏览器", browser))

    return signals

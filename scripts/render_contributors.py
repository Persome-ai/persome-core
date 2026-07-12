"""Render the compact README contributor cards from .all-contributorsrc."""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

START = "<!-- ALL-CONTRIBUTORS-LIST:START - Do not remove or modify this section -->"
END = "<!-- ALL-CONTRIBUTORS-LIST:END -->"

CONTRIBUTION_LABELS = {
    "a11y": ("♿", "Accessibility"),
    "bug": ("🐛", "Bug reports"),
    "code": ("💻", "Code"),
    "content": ("🖋️", "Content"),
    "data": ("🔣", "Data"),
    "design": ("🎨", "Design"),
    "doc": ("📖", "Documentation"),
    "growth": ("📈", "Growth"),
    "ideas": ("🤔", "Ideas and feedback"),
    "infra": ("🚇", "Infrastructure"),
    "maintenance": ("🚧", "Maintenance"),
    "mentoring": ("🧑‍🏫", "Mentoring"),
    "platform": ("📦", "Packaging"),
    "projectManagement": ("📆", "Project management"),
    "promotion": ("📣", "Promotion"),
    "question": ("💬", "Community support"),
    "research": ("🔬", "Research"),
    "review": ("👀", "Review"),
    "security": ("🛡️", "Security"),
    "test": ("⚠️", "Tests"),
    "tool": ("🔧", "Tools"),
    "translation": ("🌍", "Translation"),
    "tutorial": ("✅", "Tutorials"),
    "userTesting": ("📓", "User testing"),
}


def _label(contribution: str) -> str:
    icon, label = CONTRIBUTION_LABELS.get(
        contribution, ("✨", contribution.replace("_", " ").strip().title())
    )
    return f"{icon}&nbsp;{html.escape(label)}"


def _avatar_url(url: str, size: int) -> str:
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}size={size * 2}"


def render_contributors(config: dict[str, object]) -> str:
    contributors = config.get("contributors", [])
    if not isinstance(contributors, list):
        raise ValueError("contributors must be a list")

    per_line = int(config.get("contributorsPerLine", 3))
    image_size = int(config.get("imageSize", 56))
    if per_line < 1:
        raise ValueError("contributorsPerLine must be at least 1")
    if image_size < 1:
        raise ValueError("imageSize must be at least 1")

    cards: list[str] = []
    for raw in contributors:
        if not isinstance(raw, dict):
            raise ValueError("each contributor must be an object")
        name = html.escape(str(raw["name"]))
        profile = html.escape(str(raw["profile"]), quote=True)
        avatar = html.escape(_avatar_url(str(raw["avatar_url"]), image_size), quote=True)
        contributions = raw.get("contributions", [])
        if not isinstance(contributions, list):
            raise ValueError("contributions must be a list")
        labels = " &nbsp;·&nbsp; ".join(_label(str(item)) for item in contributions)
        if not labels:
            labels = "✨&nbsp;Contributor"
        cards.append(
            "\n".join(
                (
                    '      <td valign="middle">',
                    f'        <a href="{profile}"><img src="{avatar}" width="{image_size}" align="left" alt="{name}" /></a>',
                    f'        &nbsp;&nbsp;<strong><a href="{profile}">{name}</a></strong><br />',
                    f"        &nbsp;&nbsp;<sub>{labels}</sub>",
                    "      </td>",
                )
            )
        )

    rows = [
        "\n".join(("    <tr>", *cards[index : index + per_line], "    </tr>"))
        for index in range(0, len(cards), per_line)
    ]
    return "\n".join(("<table>", "  <tbody>", *rows, "  </tbody>", "</table>"))


def update_readme(readme: str, rendered: str) -> str:
    start = readme.find(START)
    end = readme.find(END)
    if start == -1 or end == -1 or end < start:
        raise ValueError("README contributor markers are missing or out of order")
    end += len(END)
    replacement = f"{START}\n{rendered}\n{END}"
    return f"{readme[:start]}{replacement}{readme[end:]}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if README is not up to date")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    config_path = root / ".all-contributorsrc"
    readme_path = root / "README.md"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    current = readme_path.read_text(encoding="utf-8")
    updated = update_readme(current, render_contributors(config))

    if args.check:
        if updated != current:
            print("README contributor cards are out of date.")
            return 1
        print("README contributor cards are up to date.")
        return 0

    readme_path.write_text(updated, encoding="utf-8")
    print("Rendered README contributor cards.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

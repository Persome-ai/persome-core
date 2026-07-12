from __future__ import annotations

import json
from pathlib import Path

from scripts.render_contributors import render_contributors, update_readme


def test_committed_contributor_cards_match_config() -> None:
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / ".all-contributorsrc").read_text(encoding="utf-8"))
    readme = (root / "README.md").read_text(encoding="utf-8")

    assert update_readme(readme, render_contributors(config)) == readme


def test_compact_cards_escape_profile_data_and_wrap_rows() -> None:
    contributor = {
        "login": "example",
        "name": "Example <Person>",
        "avatar_url": "https://example.com/avatar.png?version=1",
        "profile": 'https://example.com/?q="profile"',
        "contributions": ["code", "ideas"],
    }
    rendered = render_contributors(
        {
            "contributors": [contributor, contributor, contributor],
            "contributorsPerLine": 2,
            "imageSize": 56,
        }
    )

    assert rendered.count("<tr>") == 2
    assert '<table width="100%">' in rendered
    assert rendered.count('<td valign="middle" width="50.00%">') == 3
    assert "Example &lt;Person&gt;" in rendered
    assert "&quot;profile&quot;" in rendered
    assert "version=1&amp;size=112" in rendered
    assert "💻&nbsp;Code" in rendered
    assert "🤔&nbsp;Ideas and feedback" in rendered

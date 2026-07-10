# What

<!-- One or two sentences: what does this PR change? -->

# Why

<!-- The problem or need. Link issues: Fixes #123 -->

# How verified

<!-- Commands you ran + results. At minimum the offline gate:
PERSOME_LLM_MOCK=1 uv run pytest -m "not macos and not integration" -q
uv run ruff check . && uv run ruff format --check .
uv run python scripts/secret_scan.py
uv run python scripts/pii_scan.py
uv run python scripts/language_scan.py
-->

---

- [ ] Commits are signed off (`git commit -s`) — DCO required, see CONTRIBUTING.md
- [ ] No real names / emails / tokens in code or test fixtures (synthetic data only)
- [ ] Secret scan passes (`scripts/secret_scan.py`)
- [ ] Human-authored repository text is English (`scripts/language_scan.py` passes)

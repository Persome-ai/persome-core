# Contributing to Persome Runtime

## Dev setup

Requirements: macOS 13+ (for the full capture stack; the Python daemon and its
offline tests also run on Linux), Python 3.11, [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/Persome-ai/persome-core.git
cd persome-core
uv sync --all-extras
```

## Tests

The default gate is fully offline: no network, no API key. LLM calls are mocked
via `PERSOME_LLM_MOCK=1`.

```bash
# Offline unit gate (what CI runs; ~2 min)
PERSOME_LLM_MOCK=1 uv run pytest -m "not macos and not integration and not eval" -q

# Single file / test
PERSOME_LLM_MOCK=1 uv run pytest tests/test_recall.py -q
```

Marker meanings:

| marker | needs | run in CI? |
|---|---|---|
| (unmarked) | nothing (offline, mocked LLM) | yes |
| `macos` | real macOS AX permission / Swift capture helpers | no |
| `integration` | real LLM provider credentials | no |
| `eval` | LLM-judged eval harness (not pass/fail) | no |

## Lint

```bash
uv run ruff check .
```

Please don't run `ruff format` across files you didn't otherwise touch — the
tree has a pre-existing formatting baseline and wholesale reformatting destroys
`git blame`.

## PII gate (required)

No real names, emails, tokens, or captured personal content may enter the repo —
test data is synthetic, always. Before pushing:

```bash
uv run python scripts/pii_scan.py   # must exit 0
```

## Branches and pull requests

1. Fork (or branch) off `main`; name branches `feat/<scope>`, `fix/<scope>`,
   `docs/<scope>`, `chore/<scope>`.
2. Keep commits in Conventional Commit style: `type(scope): summary`.
3. Open the PR against `main` and fill in the template (What / Why / How
   verified).
4. CI must be green: offline test gate + PII scan on ubuntu and macos runners.

## DCO — sign your commits

This project uses the [Developer Certificate of Origin](https://developercertificate.org/).
Every commit must carry a `Signed-off-by` line matching the commit author:

```bash
git commit -s -m "fix(recall): handle empty FTS index"
```

which appends:

```
Signed-off-by: Your Name <you@example.com>
```

By signing off you certify you have the right to submit the contribution under
the project's Apache-2.0 license.

## License

Contributions are accepted under [Apache-2.0](LICENSE). This project derives in
part from Einsia/OpenChronicle (MIT); see `NOTICE` and `THIRD_PARTY_NOTICES`.

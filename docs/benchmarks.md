# Benchmark and launch evidence

This Runtime repository separates engineering verification from research
evaluation. Synthetic contract tests prove that code paths compose; they do not
prove that a model understands a real person or predicts future behavior.

## Public Runtime gates

| Question | Method | Pass criterion |
|---|---|---|
| Can a fresh store form the complete model contract? | `tests/test_runtime_model_e2e.py` | Point, Line, Face, Volume, Root, receipts, redacted export, and viewer routes all present after two deterministic builds |
| Can an agent retrieve and verify a fact? | `scripts/sample_demo.py` plus `scripts/verify_sample_mcp.py` | the real streamable HTTP transport lists the required tools, search returns a live entry, and `read_receipt` resolves the same ID/path/content |
| Does the offline Runtime remain stable? | `PERSOME_LLM_MOCK=1 uv run pytest -m "not macos and not integration"` | all selected tests pass without network or provider credentials |
| Does the package work outside the checkout? | build wheel, install in a fresh virtualenv, run CLI, inspect bundled resources | CLI starts; Swift sources, Three.js, and PP-OCRv6 weights exist in site-packages |
| Is committed content publication-safe? | secret, PII, and repository-language scans | zero findings outside the explicit OCR character-data exception |

## Experience targets

| Metric | Target | Interpretation |
|---|---|---|
| Median source install | at most 10 minutes | three clean installer runs on a supported Mac; report cache/network conditions |
| First useful recall | at most 10 minutes | valid capture plus configured semantic provider reaches searchable durable memory within two default five-minute flush windows |
| Viewer availability | immediate after daemon HTTP start | sparse geometry is allowed and must be labeled degraded |

The default cadence supports the recall target by construction, but environment,
provider latency, permissions, and evidence quality can delay real data.

## v0.2.2 launch observations

Measured on 2026-07-11 on an Apple Silicon Mac running macOS 26.3.1. The host
already had `uv` and its dependency cache; each run used a new
`PERSOME_INSTALL_HOME`, new virtualenv, new CLI directory, no provider key, and
the installer-managed Python 3.12 path.

| Observation | Runs | Result |
|---|---|---|
| Isolated source install | 11.716s, 11.896s, 11.926s | median 11.896s; passes the 10-minute target under recorded warm-cache conditions |
| Complete deterministic synthetic model | `tests/test_runtime_model_e2e.py` | 2.37s wall clock; Point through Root plus export/viewer assertions |
| Streamable HTTP MCP retrieval | 16 tools discovered; `search` then `read_receipt` | required model/correction tools present and receipt matched ID, path, and content |

These are launch-machine engineering observations, not population latency
percentiles. A cold network, first Python download, real provider latency, or
missing macOS permission can take longer. Real-person First Useful Recall has a
ten-minute operational target but is not represented as a measured benchmark.

## What is not measured here

The following belong in a separate `persome-bench` repository with explicit
datasets, consent, baselines, metrics, and licenses:

- personal-memory precision and recall;
- temporal fact freshness and contradiction resolution;
- next-state or next-action prediction;
- calibration and abstention;
- cross-user generalization;
- comparison with screenpipe, Mem0, or provider memory;
- real-person longitudinal utility.

Until those artifacts exist, Persome makes no paper-level superiority claim.
The README comparison describes product boundaries, not benchmark wins.

## Reproduce the synthetic proof

```bash
PERSOME_LLM_MOCK=1 uv run pytest tests/test_runtime_model_e2e.py -q
PERSOME_LLM_MOCK=1 uv run python scripts/sample_demo.py --no-open
# In another terminal:
uv run python scripts/verify_sample_mcp.py
uv build
```

The committed fixtures under `tests/fixtures/runtime_model/` contain no real
personal data. See [Runtime validation](../VALIDATION.md) for the complete gate.

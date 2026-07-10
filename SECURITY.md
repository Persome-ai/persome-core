# Security

## What this software does

persome-core is a macOS-only daemon that captures Accessibility tree events, with local screenshot OCR for AX-poor apps. It distills those captures into local Markdown memory files plus SQLite, so the daemon sees what you see.

## Data at rest

- All data lives under `~/.persome`.
- Memory is stored as Markdown plus SQLite.
- Secrets live in a 0600 env file at `~/.persome/env`.
- The capture buffer has a tiered retention policy.

## Network egress

persome-core has these egress paths. There is no telemetry and no
update phone-home; OCR is fully local (bundled 6.2 MB PP-OCRv6 weights).

Runtime egress:
1. The LLM endpoint you configure (`ANTHROPIC_BASE_URL`, default api.anthropic.com), when an LLM-dependent stage runs.
2. The embeddings endpoint you configure (`OPENAI_BASE_URL`), only when hybrid retrieval is enabled.

Opt-in / feature-gated (off unless you enable the feature or set its key):
3. DuckDuckGo search and arbitrary page fetch from `chat/tool_handlers.py`.
   These fire only when the optional chat agent invokes the corresponding tool.
4. The developer visualization pages (/dev/*) load charting libraries
   (three.js, echarts) from the jsdelivr CDN. These pages are dev-only.

Verify the full set yourself: grep the source for `httpx`, `requests`,
`aiohttp`, and `https://`. The list above is what that grep returns.

Bring your own key. No key ships with the code. Without a key, capture and BM25 retrieval still work, and LLM-dependent stages degrade cleanly.

## No computer use

The paper runtime has no click, type, takeover, or meeting-audio actuation path.
MCP write access is limited to explicit memory and model correction operations.

## Threat model

| Threat | In scope? | Mitigation |
| --- | --- | --- |
| Malicious local process reading `~/.persome` | No. A same-user local attacker owns the account. | Secrets live in a 0600 env file. Non-secret data under `~/.persome` uses default user-only home permissions. |
| Memory content exfiltration via the configured LLM endpoint | Yes. | You choose the LLM endpoint. Local capture and BM25 retrieval work without any key. |
| Prompt injection from captured screen content into connected agents | Yes. | Memory is data. Consuming agents must treat MCP results as untrusted input. |
| Supply chain | Yes. | Dependencies are pinned by the committed `uv.lock`; Python 3.11 is managed with uv. |
| Compromised or malicious MCP client | Partially. | The HTTP endpoint binds to 127.0.0.1 only and carries no additional authentication: any process running as your user can query it. Treat local MCP access as equivalent to local file access to `~/.persome`. |

## Reporting a vulnerability

Report vulnerabilities privately to a528895030@gmail.com.

Acknowledgement: within 72 hours.

Bug bounty: none at this time.

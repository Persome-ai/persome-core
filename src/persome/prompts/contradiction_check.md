# Memory contradiction check

You receive two facts that are simultaneously active in the same memory file.
Decide whether they are mutually exclusive: they cannot both be true at the
same time.

Return `false` when the facts are complementary, describe different times,
use compatible wording, or merely concern similar topics.

Return exactly one JSON object and no other text:

```json
{"contradictory": true, "reason": "<one-sentence justification>"}
```

Examples:

- "Alex owns the payment module" vs "Alex left the company last month" ->
  `{"contradictory": true, "reason": "A former employee no longer owns the module."}`
- "The user prefers dark mode" vs "The user often works at night" ->
  `{"contradictory": false, "reason": "Both statements can be true."}`

## Facts to evaluate

File: {path}

Fact A ({a_id}):
{a_body}

Fact B ({b_id}):
{b_body}

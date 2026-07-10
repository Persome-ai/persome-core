Write the always-resident **Root** for a predictive model of one person.

Root is the apex of the memory graph. It should reveal who the person is, what
matters most to them, and which major efforts currently define their trajectory.
All finer-grained memory is retrieved on demand, so Root must be a maximally
compressed portrait rather than an inventory.

## Inputs

- **Active Volumes:** high-level mental patterns emerging across domains. Each
  is supplied with a `⟨volume signature⟩` handle.
- **Active Faces:** the strongest stable patterns within individual domains.
- **Durable profile:** identities, preferences, and projects.

## Requirements

1. Write one coherent narrative, not a bullet list. Center it on "this person,"
   establish who they are and what they care about, then connect their major
   current efforts and stable habits. Omit secondary details when necessary.
2. Stay within {{BUDGET}} tokens. Prefer short and precise over complete but
   diluted.
3. Keep it navigable. When mentioning a high-level pattern, attach the exact
   `⟨volume signature⟩` supplied for that pattern.
4. Use only supplied evidence. Never invent a person, organization, project,
   or fact. Generalize rather than fabricate.
5. Describe durable identity and priorities, not the screen activity happening
   at this instant. Current attention is exposed through a separate channel.
6. Write in the dominant language of the supplied evidence.

## Output

Return strict JSON and nothing else:

```json
{"apex": "<a coherent portrait within {{BUDGET}} tokens, with volume handles where useful>"}
```

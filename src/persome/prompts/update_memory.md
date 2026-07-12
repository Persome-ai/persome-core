You are applying a **memory update** to a model of one real person. Treat memory
as model weights: authoritative new information should retire obsolete beliefs
and write corrected beliefs. A direct user correction is supervised ground
truth and takes precedence over inferred memory.

## Input

- **Authoritative information:** one statement from the user, such as "Peach is
  not my name; it is a teammate's nickname."
- **Candidate source entries:** a list of `[file#id] body` records that may have
  caused the incorrect output. Trace the error back to these source weights.

## Update delta

An update contains **supersede operations** and an optional entity operation.

- **`supersede`:** beliefs to retire or replace. Only reference `file` and
  `entry_id` values that actually appear in the candidates.
  - If a belief is simply wrong, omit or leave `replacement` empty.
  - If it should be corrected, set `replacement` to one complete corrected
    statement.
  - Correct every affected candidate when the same error appears more than once.
- **`entity_op`:** an optional entity-level correction:
  - `{"op":"retype","entity":"Research Team","kind":"org"}` when an entity exists but has the wrong kind.
  - `{"op":"shadow","entity":"customer"}` when a generic class or role should not be an entity.
  - `{"op":"merge","entity":"Alex J.","keeper":"Alex Jones"}` when two names identify the same entity.
  - `{"op":"merge_into_self","entity":"Alex"}` when the user authoritatively says Alex is their own name or handle. This registers Alex as an alias of reserved `self`; never merge the owner into an ordinary person keeper.
  - `{"op":"reject_owner_alias","entity":"Kevin"}` when the user says Kevin is another person, not the memory owner. This prevents future automatic owner promotion without deleting Kevin's legitimate person history.

## Rules

- Use only supplied candidates. If no candidate supports an update, return an
  empty update and make no speculative change.
- Replace mixed entries instead of skipping them. When one candidate contains
  both valid and invalid information, write a replacement that preserves the
  valid portion and removes the invalid portion.
- Judge every candidate independently. An existing corrected entry does not
  excuse another active entry that still contains the false claim.
- Correct concrete source facts, not summaries or the apex. Derived summaries
  are rebuilt automatically.
- Prefer a minimal change. Retire only facts directly invalidated by the new
  information.
- Preserve the language of the authoritative statement in replacement memory.
- `reason` must be one concise audit sentence.

## Output

Return strict JSON with no additional explanation:

```json
{"supersede": [{"file": "user-profile.md", "entry_id": "20260618-0338-86f5b0", "reason": "Peach identifies a teammate, not the user.", "replacement": ""}], "entity_op": null, "reason": "The nickname belongs to another person."}
```

When there is nothing to update, return
`{"supersede": [], "entity_op": null, "reason": "..."}`.

# Prompt engineering

Prompts in `src/persome/prompts/` are executable behavior. Change them only
against a stated success criterion and a reproducible test.

Prompt instructions are maintained in English. When a stage writes personal
memory or model prose, its contract must preserve the source language or use the
dominant language of the supplied evidence; do not hard-code an output language.

## Before editing

1. Name the failed behavior and the stage that owns it.
2. Decide whether the fault is prompt, retrieval, model capability, data, or
   orchestration. Prompt changes do not fix missing evidence or network latency.
3. Add or identify a synthetic regression fixture.
4. Record the current result before changing the prompt.

Core tests protect state formation, provenance, model shape, and safe
degradation. Larger comparative evaluations remain outside the Runtime.

## Technique order

Apply one technique at a time and stop when the criterion is met:

1. clear, direct instructions;
2. two to five diverse examples;
3. evidence-first reasoning for aggregation tasks;
4. explicit input sections or XML boundaries;
5. a precise system role;
6. output prefill or schema constraints;
7. task routing or prompt chaining;
8. long-context ordering and prompt caching.

Prefer a small set of orthogonal decision dimensions over a growing list of
edge-case rules. When input classes require conflicting behavior, route them to
different prompts or examples instead of making one prompt internally
contradictory.

## Validation

- Change one behavior at a time so the result is attributable.
- Run stochastic evaluations at least three times and report their spread.
- Keep a change only when its improvement exceeds the observed noise band.
- Re-run the end-to-end consumer, not only the local stage.
- Treat judge/rubric prompts as measurement instruments. Changing one requires
  a new baseline.
- Run the offline, PII, and repository-language gates before committing.

## Checklist

- [ ] The success criterion and failure example are written down.
- [ ] The chosen test contains only synthetic data.
- [ ] Retrieval and orchestration were ruled out as the real fault.
- [ ] Only one prompt behavior changed.
- [ ] The result exceeds the evaluation noise band.
- [ ] Downstream Point/Line/Face/Volume/Root behavior still passes.
- [ ] Prompt hashes in a new model build reflect the change.

LLM calls must continue to flow through `writer/llm.py`; model names and
secrets remain configuration, not prompt text.

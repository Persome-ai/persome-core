# Methodology: how to keep a code-fact atlas true

## Conclusion first

There is no standard tool that can read a mixed Python/Swift repository and
automatically produce a trustworthy map of its runtime algorithms, state
boundaries, data meaning, and failure semantics.

Tools can reliably extract *some* facts—imports, symbols, possible dataflow,
documentation, or paths actually executed in one run. None of those facts alone
answers “what does this stage mean in the system?” The engineering solution is
therefore layered evidence:

1. a maintainer-audited semantic stage registry;
2. generated static code facts and drift checks;
3. focused test links;
4. optional source/sink analysis for critical boundaries;
5. small, privacy-safe runtime traces for canonical journeys.

That is the design implemented by this atlas.

## What each map can and cannot prove

| Map | It can prove | It cannot prove |
|---|---|---|
| Static imports/symbols | A file, symbol, import, or potential dependency exists at this commit. | That the branch executed, the payload flowed, or the dependency owns the algorithm. |
| Maintainer-audited semantic Dataflow | The intended and source-checked trigger, I/O, transformation, state write, invariant, and failure policy of a stage. | That every depicted edge ran in a particular environment. |
| Test evidence | A controlled scenario exercised an assertion around the behavior. | Production frequency or every configuration/OS branch. |
| Runtime trace | This instrumented path executed for this version/config/scenario and took this long. | Untested branches, completeness, or semantic correctness. |
| API reference | Public symbols, signatures, types, and docstrings. | Cross-module runtime order or data meaning. |

The main atlas uses `S` (source), `T` (tests), and `M` (maintainer semantic
audit). `R` (runtime trace) is a future evidence layer, not silently implied.

## Tool research

### C4 and Structurizr: good abstraction, not automatic truth

The [C4 model](https://c4model.com/diagrams) is useful because it makes zoom
levels explicit: System Context, Container, Component, Code, plus dynamic and
deployment views. [Structurizr DSL](https://docs.structurizr.com/dsl) can keep a
single architecture model in Git and derive multiple navigable views from it.
Its UI can also attach [navigation and source URLs](https://docs.structurizr.com/ui/diagrams/navigation).

The limitation is decisive for this repository. Structurizr's automatic
[component finder](https://docs.structurizr.com/java/component) is Java-oriented,
and its own [introduction](https://docs.structurizr.com/java/component/introduction)
states that codebases are unique enough that generic identification rules cannot
be blindly applied. A dynamic view is still an author-specified ordered
relationship view, not a captured runtime trace.

Decision: use the C4 *idea* for levels, but keep Persome's semantic node registry
in a small repository-native TOML format. Add Structurizr later only if
cross-repository interactive zoom becomes valuable.

### Mermaid: best for reviewed Dataflow in Markdown

[Mermaid flowcharts](https://mermaid.js.org/syntax/flowchart.html) fit the
capture/model pipeline, and [Material for MkDocs supports Mermaid diagrams](https://squidfunk.github.io/mkdocs-material/reference/diagrams/)
inside normal Markdown. Mermaid also supports links/callbacks, but its
[security-level rules](https://mermaid.js.org/config/usage.html#securitylevel)
disable click behavior under strict mode, so interactive diagrams must be
treated as trusted source.

Limitations:

- Mermaid is diagram-as-code, not a semantic architecture model;
- it does not validate file paths, symbols, tests, or algorithm descriptions;
- a one-node-per-file graph becomes unreadable long before it becomes complete.

Decision: use Mermaid for a few human-scale, reviewed flows; keep exhaustive
files in a separate generated catalog; validate node source facts with the
atlas generator.

### Grimp, Import Linter, and pydeps: static Python dependency facts

[Grimp](https://grimp.readthedocs.io/en/stable/usage.html) builds a queryable
directed Python import graph and can expose import chains and line-level import
details. [Import Linter contracts](https://import-linter.readthedocs.io/en/latest/contract_types/)
can enforce forbidden dependencies, layer direction, independence, and acyclic
siblings; its [interactive UI](https://import-linter.readthedocs.io/en/latest/ui/)
can help explore violations. [pydeps](https://pydeps.readthedocs.io/en/latest/)
quickly renders Python dependency graphs and cycles.

Limitations:

- an import is not a function call, execution order, or data payload;
- dynamic loading and subprocess boundaries are incomplete;
- a Python graph cannot infer the Python-to-Swift process protocol;
- pydeps defaults such as limited dependency depth can make an apparently
  complete graph partial unless configured deliberately.

Decision: the v1 atlas uses the standard-library AST to avoid adding a runtime
dependency. Add Import Linter when the team is ready to enforce explicit
architecture contracts such as “no second session-modeling entrance” or “API
must not invent a parallel model writer.”

### CodeQL: useful for a few precise source/sink questions

CodeQL supports local/global control and dataflow analysis for both
[Python](https://codeql.github.com/docs/codeql-language-guides/analyzing-data-flow-in-python/)
and [Swift](https://codeql.github.com/docs/codeql-language-guides/analyzing-data-flow-in-swift/).
The official documentation notes that local flow is generally faster and more
precise, while global flow costs more time/memory and is less precise.

Good Persome questions would be:

- which capture-content sources can reach persistent file/SQLite sinks;
- which entrances can reach `writer/llm.py`;
- which functions can write `evo_nodes`, `relation_edges`, or Markdown;
- which API/MCP inputs can reach correction/write sinks;
- which Python sites launch or exchange data with Swift/OCR subprocesses.

Limitations:

- each database represents one language, so Python→Swift subprocess semantics
  require explicit modeling rather than appearing as one native flow;
- dynamic dispatch, aliases, library models, and subprocess protocols can create
  false positives or gaps;
- CodeQL answers a configured question; it does not generate the architecture
  narrative.

Decision: add 5–10 focused queries only after the semantic atlas stabilizes,
and store their results as evidence attachments rather than replacing the main
map.

### Doxygen, Sphinx, mkdocstrings/Griffe, and DocC: API reference only

Doxygen can render include, directory, and call/caller graphs, but its own
[diagram documentation](https://www.doxygen.nl/manual/diagrams.html) and
[call-graph command notes](https://www.doxygen.nl/manual/commands.html#cmdcallgraph)
make clear that parser limitations affect completeness. Its
[supported-language list](https://www.doxygen.nl/manual/starting.html) includes
Python but not Swift, so it is a poor unified choice here.

Sphinx [`autodoc`](https://www.sphinx-doc.org/en/master/usage/extensions/autodoc.html)
imports target modules and warns that import-time side effects execute. That is
an unnecessary risk around a macOS daemon, native helpers, and permission
probes.

For a future browsable API reference, a better split is:

- Python: [mkdocstrings-python](https://mkdocstrings.github.io/python/) with
  Griffe-based static collection;
- Swift: Apple's [Swift-DocC](https://www.swift.org/documentation/docc/).

Decision: keep API reference separate from the semantic Dataflow. The generated
module index already extracts safe AST/docstring/symbol facts without importing
Persome.

### OpenTelemetry: runtime proof, with a strict privacy ceiling

OpenTelemetry defines traces as parent/child spans representing an executed
operation graph. Its [trace model](https://opentelemetry.io/docs/specs/otel/overview/#tracing-signal)
and [Python instrumentation guide](https://opentelemetry.io/docs/languages/python/instrumentation/)
can support a future local canonical-journey harness.

Useful traces would be:

- AX event → committed capture → minute block;
- session end → reducer → trailing finalizer;
- explicit model build → Face/Volume/Root → snapshot;
- authenticated MCP request → retrieval/snapshot response;
- OCR subprocess success, timeout, restart, and AX-only degradation.

Persome's data makes trace attributes unusually sensitive. OpenTelemetry's
[sensitive-data guidance](https://opentelemetry.io/docs/security/handling-sensitive-data/)
explicitly says the implementer—not the framework—must identify/minimize or
remove sensitive data, including user behavior data.

Decision: if added, tracing must be development/test-only, local, default-off,
content-free, and limited to stage IDs, durations, counts, status, and synthetic
receipt IDs. The existing `src/persome/trace.py` request correlation ID is not a
span graph and must not be described as OpenTelemetry tracing.

### MkDocs/Material: useful presentation, optional in v1

[MkDocs configuration](https://www.mkdocs.org/user-guide/configuration/) and
[Material search](https://squidfunk.github.io/mkdocs-material/setup/setting-up-site-search/)
would provide navigation, search, Mermaid rendering, and static hosting without
changing the content model.

The repository already has Markdown/Jekyll documentation. Adding another site
toolchain before the semantic map is stable would create presentation work
without stronger evidence. The dependency-free `viewer.html` supplies local
search/drill-down now and can be hosted by any later docs site.

Decision: defer MkDocs migration. The atlas files are ordinary Markdown/JSON/HTML
and can move into it without rewriting semantic data.

## Why the implemented hybrid is intentionally small

The v1 implementation has three source layers:

```text
stages.toml                         human-reviewed meaning
      +
Python AST + git file inventory     mechanically extracted code facts
      +
viewer.template.html                presentation only
      ↓
dataflow-reference.md
module-index.md
repository-inventory.md
import-graph.mmd
atlas.json
viewer.html
```

The generator validates:

- unique lane/domain/stage IDs;
- every semantic node's source and test paths;
- every named symbol resolves to a real Python definition/module assignment or
  supported native/script declaration in one of the node's listed source files;
- stage-symbol links carry the resolved definition file and line number;
- every Python module matches exactly one code domain;
- current module docstrings, symbols/line numbers, internal imports, and direct
  test imports;
- the complete non-generated versioned or pending non-ignored file inventory;
- byte-for-byte drift of all committed generated artifacts, plus unexpected
  stale files left in the generator-owned output directory.

It deliberately does not infer semantic algorithms. Those are reviewable
inputs, not machine-generated confidence theater.

## Recommended next phases

### Phase 1 — landed here

- first-principles ontology;
- corrected semantic Dataflow;
- complete Python module and tracked-file mapping;
- interactive local explorer;
- code/docs drift register;
- standard-library generator and CI-style drift test.

### Phase 2 — architecture enforcement

- add Grimp/Import Linter;
- define a small number of high-value contracts after inspecting current
  cycles, rather than forcing an aspirational layer model immediately;
- export violations into the atlas as static evidence.

Candidate contracts:

1. no new production session-modeling entrance outside `writer/agent.py`;
2. no model-write dependency from `api/` or `mcp/` except explicit audited
   correction modules;
3. `model/snapshot.py` remains projection-only;
4. runtime paths remain centralized in `paths.py`;
5. stage LLM calls remain behind `writer/llm.py`.

### Phase 3 — targeted dataflow proof

- add a small CodeQL query pack for sensitive source/sink boundaries;
- model the Python subprocess launch points explicitly;
- record query revision and result at each release, without presenting static
  analysis as executed behavior.

### Phase 4 — canonical runtime journeys

- instrument synthetic/dev-only spans for a handful of scenarios;
- keep span attributes content-free and local;
- save trace fixtures next to the atlas with the exact commit/config/test;
- mark semantic edges `R` only when an executed fixture proves them.

### Phase 5 — hosted documentation, if needed

- adopt MkDocs Material or Structurizr only when cross-repo navigation and
  hosted search justify another toolchain;
- preserve `stages.toml` as the semantic source so presentation can change
  without rewriting architecture truth.

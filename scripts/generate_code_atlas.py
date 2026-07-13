#!/usr/bin/env python3
"""Generate and verify the Persome code-fact atlas.

The semantic stages in ``docs/code-atlas/stages.toml`` are curated because an
import graph cannot explain runtime data meaning. Everything that can be read
mechanically from the repository -- modules, docstrings, symbols, imports,
direct test imports, tracked files, and diagram source links -- is regenerated
from the current tree.

Run ``uv run python scripts/generate_code_atlas.py`` after an intentional
code-map change. CI uses ``--check`` to fail when committed atlas artifacts
drift.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import re
import subprocess
import sys
import tomllib
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ATLAS_DIR = ROOT / "docs" / "code-atlas"
CONFIG_PATH = ATLAS_DIR / "stages.toml"
TEMPLATE_PATH = ATLAS_DIR / "viewer.template.html"
GENERATED_DIR = ATLAS_DIR / "generated"

GENERATED_PATHS = {
    GENERATED_DIR / "dataflow-reference.md",
    GENERATED_DIR / "module-index.md",
    GENERATED_DIR / "repository-inventory.md",
    GENERATED_DIR / "import-graph.mmd",
    GENERATED_DIR / "atlas.json",
    ATLAS_DIR / "viewer.html",
}


class AtlasError(RuntimeError):
    """Raised when curated semantic facts no longer match the code tree."""


@dataclass(frozen=True)
class ModuleFact:
    path: str
    module: str
    domain: str
    status: str
    summary: str
    symbols: tuple[tuple[str, int], ...]
    imports: tuple[str, ...]
    tests: tuple[str, ...]
    stages: tuple[str, ...]


@dataclass(frozen=True)
class SymbolDefinition:
    name: str
    path: str
    line: int
    kind: str


StageSymbolIndex = dict[str, dict[str, tuple[SymbolDefinition, ...]]]


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _one_line(text: str, *, limit: int | None = None) -> str:
    value = re.sub(r"\s+", " ", text.strip())
    if limit is not None and len(value) > limit:
        return value[: max(1, limit - 1)].rstrip() + "…"
    return value


def _md(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _link(
    path: str,
    label: str | None = None,
    *,
    base: str = "../..",
    line: int | None = None,
) -> str:
    suffix = f"#L{line}" if line is not None else ""
    return f"[{label or path}]({base}/{path}{suffix})"


def _load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise AtlasError(f"missing semantic atlas: {_rel(CONFIG_PATH)}")
    with CONFIG_PATH.open("rb") as handle:
        config = tomllib.load(handle)
    if config.get("version") != 1:
        raise AtlasError("stages.toml must declare version = 1")
    return config


def _assignment_names(node: ast.Assign | ast.AnnAssign) -> list[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return [target.id for target in targets if isinstance(target, ast.Name)]


def _python_symbol_definitions(path: Path) -> list[SymbolDefinition]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        raise AtlasError(f"cannot parse {_rel(path)} while resolving stage symbols: {exc}") from exc

    definitions: list[SymbolDefinition] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            kind = "class"
        elif isinstance(node, ast.AsyncFunctionDef):
            kind = "async function"
        elif isinstance(node, ast.FunctionDef):
            kind = "function"
        else:
            continue
        definitions.append(
            SymbolDefinition(name=node.name, path=_rel(path), line=node.lineno, kind=kind)
        )

    # Module-level assignments are declarations too (for example the Typer
    # ``app`` surface). Deliberately ignore local variables: a stage symbol must
    # not pass validation merely because a function happens to reuse its name.
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            definitions.extend(
                SymbolDefinition(name=name, path=_rel(path), line=node.lineno, kind="assignment")
                for name in _assignment_names(node)
            )
    return definitions


_TEXT_DECLARATIONS: dict[str, re.Pattern[str]] = {
    ".swift": re.compile(
        r"\b(?P<kind>func|class|struct|enum|protocol|actor|typealias|let|var)\s+"
        r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\b"
    ),
    ".sh": re.compile(r"^(?:(?P<kind>function)\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\(\)"),
    ".js": re.compile(
        r"\b(?P<kind>function|class|const|let|var)\s+"
        r"(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)\b"
    ),
    ".mjs": re.compile(
        r"\b(?P<kind>function|class|const|let|var)\s+"
        r"(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)\b"
    ),
}


def _text_symbol_definitions(path: Path) -> list[SymbolDefinition]:
    pattern = _TEXT_DECLARATIONS.get(path.suffix.lower())
    if pattern is None:
        return []
    definitions: list[SymbolDefinition] = []
    in_block_comment = False
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line
        code_parts: list[str] = []
        while line:
            if in_block_comment:
                end = line.find("*/")
                if end < 0:
                    line = ""
                    continue
                line = line[end + 2 :]
                in_block_comment = False
            block = line.find("/*")
            inline = line.find("//")
            if inline >= 0 and (block < 0 or inline < block):
                code_parts.append(line[:inline])
                break
            if block < 0:
                code_parts.append(line)
                break
            code_parts.append(line[:block])
            line = line[block + 2 :]
            in_block_comment = True
        code = "".join(code_parts)
        if path.suffix.lower() == ".sh" and code.lstrip().startswith("#"):
            continue
        match = pattern.search(code)
        if match:
            definitions.append(
                SymbolDefinition(
                    name=match.group("name"),
                    path=_rel(path),
                    line=line_number,
                    kind=match.group("kind") or "function",
                )
            )
    return definitions


def _source_symbol_definitions(path: Path) -> list[SymbolDefinition]:
    if path.suffix.lower() == ".py":
        return _python_symbol_definitions(path)
    return _text_symbol_definitions(path)


def _validate_config(config: dict[str, Any]) -> StageSymbolIndex:
    lanes = config.get("lanes", [])
    stages = config.get("stages", [])
    edges = config.get("edges", [])
    domains = config.get("domains", [])
    statuses = config.get("statuses", [])
    if not lanes or not stages or not domains or not statuses:
        raise AtlasError("stages.toml must define lanes, domains, statuses, and stages")

    lane_ids = [lane["id"] for lane in lanes]
    stage_ids = [stage["id"] for stage in stages]
    domain_ids = [domain["id"] for domain in domains]
    status_ids = [status["id"] for status in statuses]
    for label, values in (
        ("lane", lane_ids),
        ("stage", stage_ids),
        ("domain", domain_ids),
        ("status", status_ids),
    ):
        duplicates = sorted(name for name, count in Counter(values).items() if count > 1)
        if duplicates:
            raise AtlasError(f"duplicate {label} ids: {', '.join(duplicates)}")
    defaults = [status["id"] for status in statuses if status.get("default")]
    if len(defaults) != 1:
        raise AtlasError(f"exactly one [[statuses]] entry must be default; found {defaults}")

    valid_kinds = {
        "source",
        "deterministic",
        "llm-proposal",
        "deterministic-gate",
        "store",
        "orchestration",
        "projection",
        "surface",
        "explicit-write",
    }
    declaration_cache: dict[str, list[SymbolDefinition]] = {}
    stage_symbols: StageSymbolIndex = {}
    for stage in stages:
        stage_id = stage["id"]
        if stage["lane"] not in lane_ids:
            raise AtlasError(f"stage {stage_id!r} uses unknown lane {stage['lane']!r}")
        if stage["kind"] not in valid_kinds:
            raise AtlasError(f"stage {stage_id!r} uses unknown kind {stage['kind']!r}")
        for field in (
            "label",
            "summary",
            "algorithm",
            "trigger",
            "inputs",
            "outputs",
            "state",
            "failure",
            "files",
            "symbols",
            "tests",
        ):
            if not stage.get(field):
                raise AtlasError(f"stage {stage_id!r} has no {field}")
        for path in (*stage["files"], *stage["tests"]):
            if not (ROOT / path).is_file():
                raise AtlasError(f"stage {stage_id!r} references missing file {path!r}")

        by_name: dict[str, list[SymbolDefinition]] = defaultdict(list)
        for path in stage["files"]:
            if path not in declaration_cache:
                declaration_cache[path] = _source_symbol_definitions(ROOT / path)
            for definition in declaration_cache[path]:
                by_name[definition.name].append(definition)
        resolved: dict[str, tuple[SymbolDefinition, ...]] = {}
        for symbol in stage["symbols"]:
            definitions = tuple(
                sorted(
                    by_name.get(symbol, []),
                    key=lambda item: (stage["files"].index(item.path), item.line),
                )
            )
            if not definitions:
                raise AtlasError(
                    f"stage {stage_id!r} references symbol {symbol!r} without a real "
                    f"definition/declaration in its source files"
                )
            resolved[symbol] = definitions
        stage_symbols[stage_id] = resolved

    for edge in edges:
        if edge["from"] not in stage_ids or edge["to"] not in stage_ids:
            raise AtlasError(f"edge references unknown stage: {edge}")
        if edge.get("kind", "data") not in {"data", "control", "feedback"}:
            raise AtlasError(f"edge has invalid kind: {edge}")
    return stage_symbols


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(ROOT / "src").with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _domain_for(path: str, domains: list[dict[str, Any]]) -> str:
    matches = [
        domain["id"]
        for domain in domains
        if any(fnmatch.fnmatch(path, pattern) for pattern in domain["globs"])
    ]
    if len(matches) != 1:
        raise AtlasError(
            f"{path} must match exactly one [[domains]] entry; matched {matches or 'none'}"
        )
    return matches[0]


def _status_for(path: str, statuses: list[dict[str, Any]]) -> str:
    default = next(status["id"] for status in statuses if status.get("default"))
    matches = [
        status["id"]
        for status in statuses
        if not status.get("default")
        and any(fnmatch.fnmatch(path, pattern) for pattern in status.get("globs", []))
    ]
    if len(matches) > 1:
        raise AtlasError(f"{path} matches multiple non-default [[statuses]] entries: {matches}")
    return matches[0] if matches else default


def _resolve_imports(
    tree: ast.Module,
    *,
    module: str,
    is_package: bool,
    known: set[str],
) -> tuple[str, ...]:
    found: set[str] = set()
    package_parts = module.split(".") if is_package else module.split(".")[:-1]

    def add(candidate: str) -> None:
        while candidate and candidate.startswith("persome"):
            if candidate in known and candidate != module:
                found.add(candidate)
                return
            if "." not in candidate:
                return
            candidate = candidate.rsplit(".", 1)[0]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                trim = max(0, node.level - 1)
                base_parts = (
                    package_parts[: len(package_parts) - trim] if trim else package_parts[:]
                )
                if node.module:
                    base_parts.extend(node.module.split("."))
                base = ".".join(base_parts)
            else:
                base = node.module or ""
            add(base)
            for alias in node.names:
                if alias.name != "*":
                    add(f"{base}.{alias.name}" if base else alias.name)
    return tuple(sorted(found))


def _test_import_index(known: set[str]) -> dict[str, set[str]]:
    index: dict[str, set[str]] = defaultdict(set)
    for path in sorted((ROOT / "tests").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                candidates = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                candidates = [node.module]
                candidates.extend(f"{node.module}.{alias.name}" for alias in node.names)
            else:
                continue
            for candidate in candidates:
                probe = candidate
                while probe.startswith("persome"):
                    if probe in known:
                        index[probe].add(_rel(path))
                        break
                    if "." not in probe:
                        break
                    probe = probe.rsplit(".", 1)[0]
    return index


def _module_facts(config: dict[str, Any]) -> list[ModuleFact]:
    paths = sorted((ROOT / "src" / "persome").rglob("*.py"))
    known = {_module_name(path) for path in paths}
    test_index = _test_import_index(known)
    stage_index: dict[str, list[str]] = defaultdict(list)
    for stage in config["stages"]:
        for source in stage["files"]:
            if source.endswith(".py") and source.startswith("src/persome/"):
                stage_index[source].append(stage["id"])

    facts: list[ModuleFact] = []
    for path in paths:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        module = _module_name(path)
        public = [
            (node.name, node.lineno)
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and not node.name.startswith("_")
        ]
        fallback = [
            (node.name, node.lineno)
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        symbols = tuple((public or fallback)[:8])
        doc = ast.get_docstring(tree) or "Package marker with no runtime behavior."
        summary = _one_line(doc.split("\n\n", 1)[0], limit=320)
        rel = _rel(path)
        tests = set(test_index.get(module, set()))
        basename = path.stem if path.stem != "__init__" else path.parent.name
        tests.update(
            _rel(test) for test in (ROOT / "tests").rglob(f"test_{basename}.py") if test.is_file()
        )
        facts.append(
            ModuleFact(
                path=rel,
                module=module,
                domain=_domain_for(rel, config["domains"]),
                status=_status_for(rel, config["statuses"]),
                summary=summary,
                symbols=symbols,
                imports=_resolve_imports(
                    tree,
                    module=module,
                    is_package=path.name == "__init__.py",
                    known=known,
                ),
                tests=tuple(sorted(tests)),
                stages=tuple(sorted(stage_index.get(rel, []))),
            )
        )
    return facts


def _repo_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    excluded_prefix = "docs/code-atlas/generated/"
    excluded_exact = {"docs/code-atlas/viewer.html"}
    return [
        path
        for path in result.stdout.splitlines()
        if path and not path.startswith(excluded_prefix) and path not in excluded_exact
    ]


def _file_role(path: str) -> str:
    if path.startswith("src/persome/") and path.endswith(".py"):
        return "runtime Python"
    if "/prompts/" in path or path.startswith("src/persome/prompts/"):
        return "LLM prompt"
    if path.startswith("tests/fixtures/"):
        return "synthetic test fixture"
    if path.startswith("tests/"):
        return "test evidence"
    if path.startswith("docs/") or path.endswith(".md"):
        return "documentation"
    if path.startswith("resources/") and path.endswith(".swift"):
        return "native Swift source"
    if path.startswith("resources/model_assets/"):
        return "viewer asset"
    if path.startswith("resources/"):
        return "native build resource"
    if path.startswith("scripts/"):
        return "maintenance script"
    if path.startswith(".github/workflows/"):
        return "CI/release workflow"
    if path.startswith(".github/"):
        return "repository governance"
    if path.startswith("ocr_models/"):
        return "vendored OCR model"
    if path.endswith((".toml", ".json", ".yml", ".yaml", ".lock", ".spec")):
        return "build/runtime contract"
    return "root/support file"


def _heading(path: Path) -> str:
    if not path.is_file():
        return "missing"
    suffix = path.suffix.lower()
    if suffix == ".py":
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            return _one_line(
                (ast.get_docstring(tree) or "Python module").split("\n\n", 1)[0], limit=180
            )
        except (OSError, SyntaxError, UnicodeDecodeError):
            return "Python module"
    if suffix in {".md", ".markdown"}:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines():
            if line.startswith("#"):
                return _one_line(line.lstrip("# "), limit=180)
        return "Markdown document or prompt"
    if suffix in {".swift", ".sh", ".js", ".mjs", ".css"}:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines()[:30]:
            value = line.strip().lstrip("#/ ").strip()
            if value and not value.startswith(("!", "import ", "from ", "@")):
                return _one_line(value, limit=180)
    return {
        ".json": "Versioned/generated JSON contract or fixture",
        ".toml": "TOML configuration or curated metadata",
        ".yml": "YAML automation/configuration",
        ".yaml": "YAML automation/configuration",
        ".lock": "Resolved dependency lock",
        ".onnx": "Vendored binary model weights",
    }.get(suffix, "Repository support artifact")


def _stage_json(
    config: dict[str, Any],
    modules: list[ModuleFact],
    stage_symbols: StageSymbolIndex,
) -> dict[str, Any]:
    domain_labels = {domain["id"]: domain["label"] for domain in config["domains"]}
    status_labels = {status["id"]: status["label"] for status in config["statuses"]}
    module_rows = [
        {
            "path": module.path,
            "module": module.module,
            "domain": module.domain,
            "domain_label": domain_labels[module.domain],
            "status": module.status,
            "status_label": status_labels[module.status],
            "summary": module.summary,
            "symbols": [{"name": name, "line": line} for name, line in module.symbols],
            "imports": list(module.imports),
            "tests": list(module.tests),
            "stages": list(module.stages),
        }
        for module in modules
    ]
    stage_rows: list[dict[str, Any]] = []
    for stage in config["stages"]:
        row = dict(stage)
        row["symbol_definitions"] = [
            {
                "name": symbol,
                "locations": [
                    {
                        "path": definition.path,
                        "line": definition.line,
                        "kind": definition.kind,
                    }
                    for definition in stage_symbols[stage["id"]][symbol]
                ],
            }
            for symbol in stage["symbols"]
        ]
        stage_rows.append(row)

    return {
        "schema_version": 1,
        "generated_from": (
            "docs/code-atlas/stages.toml + source declaration indexes + Python AST + git files"
        ),
        "lanes": config["lanes"],
        "domains": [
            {key: value for key, value in domain.items() if key != "globs"}
            for domain in config["domains"]
        ],
        "statuses": [
            {key: value for key, value in status.items() if key not in {"globs", "default"}}
            for status in config["statuses"]
        ],
        "stages": stage_rows,
        "edges": config.get("edges", []),
        "modules": module_rows,
    }


def _stage_symbol_links(
    stage: dict[str, Any],
    stage_symbols: StageSymbolIndex,
) -> str:
    rendered: list[str] = []
    for symbol in stage["symbols"]:
        definitions = stage_symbols[stage["id"]][symbol]
        if len(definitions) == 1:
            definition = definitions[0]
            rendered.append(
                _link(
                    definition.path,
                    f"`{symbol}`",
                    base="../../..",
                    line=definition.line,
                )
            )
            continue
        rendered.append(
            " / ".join(
                _link(
                    definition.path,
                    f"`{symbol}` in `{Path(definition.path).name}`",
                    base="../../..",
                    line=definition.line,
                )
                for definition in definitions
            )
        )
    return ", ".join(rendered)


def _render_dataflow(config: dict[str, Any], stage_symbols: StageSymbolIndex) -> str:
    lane_labels = {lane["id"]: lane["label"] for lane in config["lanes"]}
    kind_labels = {
        "source": "source fact",
        "deterministic": "deterministic",
        "llm-proposal": "LLM proposal",
        "deterministic-gate": "deterministic gate",
        "store": "durable store",
        "orchestration": "orchestration",
        "projection": "read-only projection",
        "surface": "access surface",
        "explicit-write": "explicit audited write",
    }
    lines = [
        "# Generated dataflow reference",
        "",
        "> Generated by `scripts/generate_code_atlas.py` from curated stage semantics and",
        "> validated source/test paths. Edit `docs/code-atlas/stages.toml`, not this file.",
        "",
    ]
    for lane in config["lanes"]:
        stages = sorted(
            (stage for stage in config["stages"] if stage["lane"] == lane["id"]),
            key=lambda stage: stage["order"],
        )
        if not stages:
            continue
        lines.extend([f"## {lane['label']}", "", lane["definition"], ""])
        for stage in stages:
            lines.extend(
                [
                    f"### `{stage['id']}` — {stage['label']}",
                    "",
                    f"- **Fact type:** {kind_labels[stage['kind']]}",
                    f"- **Why it exists:** {stage['summary']}",
                    f"- **Trigger:** {stage['trigger']}",
                    f"- **Input:** {'; '.join(stage['inputs'])}",
                    f"- **Output:** {'; '.join(stage['outputs'])}",
                    f"- **Core algorithm:** {stage['algorithm']}",
                    f"- **State / idempotency:** {stage['state']}",
                    f"- **Failure semantics:** {stage['failure']}",
                    "- **Source:** "
                    + "; ".join(_link(path, path, base="../../..") for path in stage["files"]),
                    f"- **Key symbols:** {_stage_symbol_links(stage, stage_symbols)}",
                    "- **Tests:** "
                    + "; ".join(_link(path, path, base="../../..") for path in stage["tests"]),
                    "",
                ]
            )
    lines.extend(
        ["## Edges", "", "| From | To | Kind | Payload | Condition |", "|---|---|---|---|---|"]
    )
    for edge in config.get("edges", []):
        lines.append(
            "| "
            + " | ".join(
                _md(value)
                for value in (
                    f"`{edge['from']}`",
                    f"`{edge['to']}`",
                    edge.get("kind", "data"),
                    edge["payload"],
                    edge["condition"],
                )
            )
            + " |"
        )
    lines.append("")
    lines.append(
        "Lane labels: " + ", ".join(f"`{key}` = {value}" for key, value in lane_labels.items())
    )
    lines.append("")
    return "\n".join(lines)


def _render_modules(config: dict[str, Any], modules: list[ModuleFact]) -> str:
    domain_labels = {domain["id"]: domain["label"] for domain in config["domains"]}
    status_labels = {status["id"]: status["label"] for status in config["statuses"]}
    lines = [
        "# Generated Python module index",
        "",
        "> Generated from every `src/persome/**/*.py` AST. Module docstrings are the role",
        "> descriptions; symbol line numbers, internal imports, stage membership, and direct",
        "> test imports come from the current tree. Edit source docstrings or",
        "> `docs/code-atlas/stages.toml`, then regenerate.",
        "",
        f"**Coverage:** {len(modules)} / {len(modules)} Python modules.",
        "",
    ]
    for domain in config["domains"]:
        subset = [module for module in modules if module.domain == domain["id"]]
        lines.extend(
            [
                f"## {domain_labels[domain['id']]}",
                "",
                domain["definition"],
                "",
                "| File | Operational status | Runtime role (module docstring) | Key symbols | Direct internal dependencies | Semantic stage(s) | Direct test evidence |",
                "|---|---|---|---|---|---|---|",
            ]
        )
        for module in subset:
            symbols = (
                "<br>".join(
                    f"[`{name}`](../../../{module.path}#L{line})" for name, line in module.symbols
                )
                or "package marker"
            )
            deps = "<br>".join(f"`{name}`" for name in module.imports[:8]) or "—"
            if len(module.imports) > 8:
                deps += f"<br>+{len(module.imports) - 8} more"
            stages = "<br>".join(f"`{name}`" for name in module.stages) or "supporting module"
            tests = (
                "<br>".join(
                    _link(path, Path(path).name, base="../../..") for path in module.tests[:6]
                )
                or "—"
            )
            if len(module.tests) > 6:
                tests += f"<br>+{len(module.tests) - 6} more"
            lines.append(
                "| "
                + " | ".join(
                    (
                        _link(module.path, module.path, base="../../.."),
                        status_labels[module.status],
                        _md(module.summary),
                        symbols,
                        deps,
                        stages,
                        tests,
                    )
                )
                + " |"
            )
        lines.append("")
    return "\n".join(lines)


def _render_inventory(files: list[str], modules: list[ModuleFact]) -> str:
    module_summaries = {module.path: module.summary for module in modules}
    groups: dict[str, list[str]] = defaultdict(list)
    for path in files:
        top = path.split("/", 1)[0] if "/" in path else "(root)"
        groups[top].append(path)
    lines = [
        "# Generated tracked-file inventory",
        "",
        "> Generated from `git ls-files --cached --others --exclude-standard`. Self-generated",
        "> atlas artifacts under",
        "> `docs/code-atlas/generated/` and `docs/code-atlas/viewer.html` are intentionally",
        "> excluded to avoid recursive drift; they are enumerated in the atlas README.",
        "",
        f"**Coverage:** {len(files)} versioned or pending non-generated files across "
        f"{len(groups)} top-level groups.",
        "",
    ]
    for group in sorted(groups, key=lambda value: (value != "(root)", value)):
        lines.extend(
            [
                f"## {group}",
                "",
                "| File | Class | Code-derived or file-derived purpose |",
                "|---|---|---|",
            ]
        )
        for path in sorted(groups[group]):
            purpose = module_summaries.get(path) or _heading(ROOT / path)
            lines.append(
                f"| {_link(path, path, base='../../..')} | {_file_role(path)} | {_md(purpose)} |"
            )
        lines.append("")
    return "\n".join(lines)


def _render_import_graph(config: dict[str, Any], modules: list[ModuleFact]) -> str:
    domain_by_module = {module.module: module.domain for module in modules}
    edges: Counter[tuple[str, str]] = Counter()
    for module in modules:
        for imported in module.imports:
            target = domain_by_module.get(imported)
            if target and target != module.domain:
                edges[(module.domain, target)] += 1
    labels = {domain["id"]: domain["label"] for domain in config["domains"]}
    lines = [
        "%% Generated package-domain import graph. Edge labels are distinct module-import pairs.",
        "flowchart LR",
    ]
    for domain in config["domains"]:
        lines.append(f'    {domain["id"].replace("-", "_")}["{labels[domain["id"]]}"]')
    for (source, target), count in sorted(edges.items()):
        lines.append(f'    {source.replace("-", "_")} -->|"{count}"| {target.replace("-", "_")}')
    lines.append("")
    return "\n".join(lines)


def _render_viewer(template: str, atlas: dict[str, Any]) -> str:
    marker = "__PERSOME_ATLAS_JSON__"
    if marker not in template:
        raise AtlasError(f"viewer template missing {marker}")
    encoded = json.dumps(atlas, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return template.replace(marker, encoded)


def _outputs(
    config: dict[str, Any],
    stage_symbols: StageSymbolIndex,
) -> dict[Path, str]:
    modules = _module_facts(config)
    files = _repo_files()
    atlas = _stage_json(config, modules, stage_symbols)
    if not TEMPLATE_PATH.exists():
        raise AtlasError(f"missing viewer template: {_rel(TEMPLATE_PATH)}")
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    return {
        GENERATED_DIR / "dataflow-reference.md": _render_dataflow(config, stage_symbols),
        GENERATED_DIR / "module-index.md": _render_modules(config, modules),
        GENERATED_DIR / "repository-inventory.md": _render_inventory(files, modules),
        GENERATED_DIR / "import-graph.mmd": _render_import_graph(config, modules),
        GENERATED_DIR / "atlas.json": json.dumps(atlas, ensure_ascii=False, indent=2) + "\n",
        ATLAS_DIR / "viewer.html": _render_viewer(template, atlas),
    }


def _unexpected_generated_paths() -> list[Path]:
    expected = {path for path in GENERATED_PATHS if path.is_relative_to(GENERATED_DIR)}
    actual = {path for path in GENERATED_DIR.rglob("*") if path.is_file()}
    return sorted(actual - expected)


def _check(outputs: dict[Path, str]) -> int:
    drift: list[str] = []
    for path, expected in outputs.items():
        actual = path.read_text(encoding="utf-8") if path.exists() else None
        if actual != expected:
            drift.append(_rel(path))
    stale = _unexpected_generated_paths()
    if drift or stale:
        print("code atlas drift detected:", file=sys.stderr)
        for path in drift:
            print(f"  - {path}", file=sys.stderr)
        for path in stale:
            print(f"  - {_rel(path)} (stale generated artifact)", file=sys.stderr)
        print("run: uv run python scripts/generate_code_atlas.py", file=sys.stderr)
        return 1
    print(f"code atlas is current ({len(outputs)} generated artifacts)")
    return 0


def _write(outputs: dict[Path, str]) -> int:
    if set(outputs) != GENERATED_PATHS:
        missing = sorted(_rel(path) for path in GENERATED_PATHS - set(outputs))
        extra = sorted(_rel(path) for path in set(outputs) - GENERATED_PATHS)
        raise AtlasError(f"generated output contract mismatch; missing={missing}, extra={extra}")
    stale = _unexpected_generated_paths()
    if stale:
        rendered = ", ".join(_rel(path) for path in stale)
        raise AtlasError(f"remove stale generated artifacts before regenerating: {rendered}")
    for path, content in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(f"wrote {_rel(path)}")
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if generated artifacts drift")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        config = _load_config()
        stage_symbols = _validate_config(config)
        outputs = _outputs(config, stage_symbols)
        return _check(outputs) if args.check else _write(outputs)
    except (AtlasError, OSError, subprocess.CalledProcessError, tomllib.TOMLDecodeError) as exc:
        print(f"code atlas error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

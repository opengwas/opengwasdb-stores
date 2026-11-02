"""The import graph between components, across both repositories (#418, #421).

The edges are not written in any model: they are read from the code. Every
tracked Python module a component claims is parsed, and each import that lands
in another component's module is one import of that edge. An import is counted
per (file, imported name), so one file importing three names of another
component is three imports.

An import inside the engine is `upward` when it goes to a component on a higher
layer than the importing one (the engine's own file declares the layers, top to
bottom); the same layer and lower layers are not. An import from this
repository into the engine is a `registry_to_engine` edge.

The same measurement is what this repository's baseline of engine imports is
held to (#421): `imports_of` yields every import once, `import_edges` counts
them between components, and `engine_imports` keeps the ones that reach the
engine. One pass over the sources, so the page's seam counts and the baseline
cannot disagree about what an import is.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from typing import Any, Mapping, Sequence

import yaml

from project_site.collect import architecture_model as model_rules
from project_site.collect.common import CollectError

Edge = dict[str, Any]


def module_name(repo: str, path: str) -> str | None:
    """The dotted module a Python file is: `opengwasdb/a/b.py` -> `opengwasdb.a.b`, `src/x/y.py` -> `x.y`."""
    if not path.endswith(".py"):
        return None
    if repo == "engine":
        relative = path
    elif path.startswith("src/"):
        relative = path[len("src/") :]
    else:
        return None
    return relative[: -len(".py")].replace("/", ".").removesuffix(".__init__")


def imported_names(source: str, module: str, is_package: bool, path: str = "?") -> set[str]:
    """Every absolute name a module imports; relative imports are resolved against `module`.

    A file that does not parse is an error naming it: dropping its imports would
    leave the graph looking complete.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise CollectError(f"{path} does not parse: {error}") from error
    package = module if is_package else module.rpartition(".")[0]
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".") if package else []
                parts = parts[: len(parts) - (node.level - 1)]
                base = ".".join([*parts, base] if base else parts)
            names.update(f"{base}.{alias.name}" if base else alias.name for alias in node.names)
    return names


def modules_of(trees: Mapping[str, Any]) -> dict[str, tuple[str, str]]:
    """Dotted module -> (repository, file), for every tracked file of both repositories."""
    return {
        module: (repo, path)
        for repo, tree in trees.items()
        for path in tree.files
        if (module := module_name(repo, path))
    }


def imports_of(
    trees: Mapping[str, Any], claims: Mapping[str, Sequence[tuple[str, str]]]
) -> list[dict[str, Any]]:
    """Every import a claimed file of either repository makes, one record per (file, imported name).

    `component` is the importing file's owner; `target` is the component the name
    resolves to, or `None` for a name no tracked module answers to. An unclaimed
    file is not a source here: it is a gap the ownership test fails on, so its
    imports are reported there rather than hidden in a graph.
    """
    # Every tracked module resolves to its own owner or to none: an unclaimed
    # module is a gap, and must not inherit the owner of the package around it.
    everything = modules_of(trees)
    module_owner: dict[str, str | None] = {
        module: model_rules.owner(claims[repo], path) for module, (repo, path) in everything.items()
    }
    sources: list[tuple[str, str, str]] = [
        (repo, path, module) for module, (repo, path) in everything.items() if module_owner[module]
    ]
    # A claimed script (`tools/`, `workflow/`) is not importable, but it imports:
    # it is a source of edges, never a target.
    importable = {path for _, path, _ in sources}
    for path in trees["registry"].files:
        if path.endswith(".py") and path not in importable and model_rules.owner(claims["registry"], path):
            sources.append(("registry", path, ""))

    def resolve(name: str) -> str | None:
        parts = name.split(".")
        while parts:
            if ".".join(parts) in module_owner:
                return module_owner[".".join(parts)]
            parts.pop()
        return None

    records: list[dict[str, Any]] = []
    for repo, tree in trees.items():
        mine = [(path, module) for r, path, module in sources if r == repo]
        text = tree.read([path for path, _ in mine])
        for path, module in mine:
            source = model_rules.owner(claims[repo], path)
            for name in imported_names(text.get(path, ""), module, path.endswith("__init__.py"), path):
                records.append(
                    {"repo": repo, "file": path, "component": source, "name": name, "target": resolve(name)}
                )
    return records


def is_engine_name(name: str) -> bool:
    """Whether an imported name is one of the engine's: its package, or a module or name under it.

    The rule needs no engine checkout and no engine model, which is what lets
    this repository's own CI hold its engine imports to a baseline on its own.
    `opengwasdbx` is a different package, not the engine's.
    """
    package = model_rules.ENGINE_PACKAGE
    return name == package or name.startswith(f"{package}.")


def engine_imports(imports: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """This repository's imports of engine names, sorted by (file, name), one entry per pair.

    `engine_component` is the engine component the name resolves to, and is
    `None` when the engine was not read: the measurement does not need it.
    """
    found = [
        {"file": record["file"], "name": record["name"], "engine_component": record["target"]}
        for record in imports
        if record["repo"] == "registry" and is_engine_name(record["name"])
    ]
    return sorted(found, key=lambda entry: (entry["file"], entry["name"]))


#: Where this repository commits the engine imports it lives with (#416 decision 14).
BASELINE_PATH = "docs/architecture/engine-imports-baseline.yaml"


def load_baseline(text: str, where: str = BASELINE_PATH) -> tuple[tuple[str, str], ...]:
    """A baseline file's `(file, name)` entries, sorted; a malformed or duplicated one is an error.

    A baseline that cannot be read must not pass for an empty one: an empty
    baseline would turn every listed import into a failure, and a silently
    truncated one would let a new import through.
    """
    document = yaml.safe_load(text)
    if not isinstance(document, dict) or not isinstance(document.get("imports"), list):
        raise CollectError(f"{where}: a baseline is a mapping with an `imports` list")
    entries: list[tuple[str, str]] = []
    for number, entry in enumerate(document["imports"]):
        if (
            not isinstance(entry, Mapping)
            or set(entry) != {"file", "name"}
            or not all(isinstance(entry[key], str) and entry[key] for key in ("file", "name"))
        ):
            raise CollectError(f"{where}: entry #{number} must be a mapping with only 'file' and 'name'")
        pair = (entry["file"], entry["name"])
        if pair in entries:
            raise CollectError(f"{where}: {pair[0]} imports {pair[1]} is listed twice")
        entries.append(pair)
    return tuple(sorted(entries))


def check_engine_imports(
    found: Sequence[Mapping[str, Any]], baseline: Sequence[tuple[str, str]]
) -> list[str]:
    """The lines CI fails on: an engine import the baseline does not list, and a listed one that is gone.

    The unit is the pair, so the same name imported by a second file is a new
    import, and an entry whose file still imports something else is stale. The
    engine component is named when the measurement carries it, which is when the
    engine was read.
    """
    listed = set(baseline)
    measured = {(entry["file"], entry["name"]) for entry in found}
    problems = [
        f"{entry['file']}: imports {entry['name']}"
        f"{' from the engine component ' + entry['engine_component'] if entry.get('engine_component') else ''}"
        f", which {BASELINE_PATH} does not list; a new engine import is a coupling nobody decided on,"
        " and that file only shrinks"
        for entry in found
        if (entry["file"], entry["name"]) not in listed
    ]
    problems += [
        f"{file}: imports {name}, listed in {BASELINE_PATH}, is gone; delete the entry"
        for file, name in baseline
        if (file, name) not in measured
    ]
    return problems


def import_edges(
    trees: Mapping[str, Any],
    claims: Mapping[str, Sequence[tuple[str, str]]],
    components: Sequence[Mapping[str, Any]],
    imports: Sequence[Mapping[str, Any]] | None = None,
) -> list[Edge]:
    """Component -> component edges with their import counts, sorted by (from, to).

    `imports` is the measurement, when the caller already made it: the collector
    measures once and hands the same records to the baseline it reports.
    """
    by_id = {c["id"]: c for c in components}
    records = imports_of(trees, claims) if imports is None else imports
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for record in records:
        source, target = record["component"], record["target"]
        if target and target != source:
            counts[(source, target)] += 1

    edges = []
    for (source, target), count in sorted(counts.items()):
        a, b = by_id[source], by_id[target]
        if a["repo"] == "engine" and b["repo"] == "engine":
            scope = "engine"
        elif a["repo"] == "registry" and b["repo"] == "engine":
            scope = "registry_to_engine"
        else:
            scope = "registry"
        upward = scope == "engine" and None not in (a["layer"], b["layer"]) and b["layer"] > a["layer"]
        edges.append({"from": source, "to": target, "imports": count, "scope": scope, "upward": upward})
    return edges


def summarise(edges: Sequence[Edge]) -> dict[str, int]:
    """The figures the pages state: upward edges and imports, and the registry's reach into the engine."""
    upward = [e for e in edges if e["upward"]]
    seam = [e for e in edges if e["scope"] == "registry_to_engine"]
    return {
        "edges": len(edges),
        "upward_edges": len(upward),
        "upward_imports": sum(e["imports"] for e in upward),
        "registry_to_engine_edges": len(seam),
        "registry_to_engine_imports": sum(e["imports"] for e in seam),
    }

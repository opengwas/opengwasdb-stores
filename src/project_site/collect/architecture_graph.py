"""The import graph between components, across both repositories (#418).

The edges are not written in any model: they are read from the code. Every
tracked Python module a component claims is parsed, and each import that lands
in another component's module is one import of that edge. An import is counted
per (file, imported name), so one file importing three names of another
component is three imports.

An import inside the engine is `upward` when it goes to a component on a higher
layer than the importing one (the engine's own file declares the layers, top to
bottom); the same layer and lower layers are not. An import from this
repository into the engine is a `registry_to_engine` edge.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from typing import Any, Mapping, Sequence

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


def import_edges(
    trees: Mapping[str, Any],
    claims: Mapping[str, Sequence[tuple[str, str]]],
    components: Sequence[Mapping[str, Any]],
) -> list[Edge]:
    """Component -> component edges with their import counts, sorted by (from, to)."""
    by_id = {c["id"]: c for c in components}
    module_owner: dict[str, str] = {}
    sources: list[tuple[str, str, str]] = []
    for module, (repo, path) in modules_of(trees).items():
        component = model_rules.owner(claims[repo], path)
        if component:
            module_owner[module] = component
            sources.append((repo, path, module))
    # A claimed script (`tools/`, `workflow/`) is not importable, but it imports:
    # it is a source of edges, never a target.
    importable = {path for _, path, _ in sources}
    for path in trees["registry"].files:
        if path.endswith(".py") and path not in importable and model_rules.owner(claims["registry"], path):
            sources.append(("registry", path, ""))

    def resolve(name: str) -> str | None:
        parts = name.split(".")
        while parts:
            hit = module_owner.get(".".join(parts))
            if hit:
                return hit
            parts.pop()
        return None

    counts: dict[tuple[str, str], int] = defaultdict(int)
    for repo, tree in trees.items():
        mine = [(path, module) for r, path, module in sources if r == repo]
        text = tree.read([path for path, _ in mine])
        for path, module in mine:
            source = model_rules.owner(claims[repo], path)
            for name in imported_names(text.get(path, ""), module, path.endswith("__init__.py"), path):
                target = resolve(name)
                if target and target != source:
                    counts[(source, target)] += 1

    edges = []
    for (source, target), imports in sorted(counts.items()):
        a, b = by_id[source], by_id[target]
        if a["repo"] == "engine" and b["repo"] == "engine":
            scope = "engine"
        elif a["repo"] == "registry" and b["repo"] == "engine":
            scope = "registry_to_engine"
        else:
            scope = "registry"
        upward = scope == "engine" and None not in (a["layer"], b["layer"]) and b["layer"] > a["layer"]
        edges.append({"from": source, "to": target, "imports": imports, "scope": scope, "upward": upward})
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

#!/usr/bin/env python3
"""Store Release Build imports nothing from Manifest Generation (#249, ADR 0035).

The two workflows meet only at the Release Bundle. Store Release Build is
`src/ogstores`, `workflow/Snakefile` and `workflow/release.py`; Manifest
Generation is the `manifest_generation` package (`src/manifest_generation`),
plus the legacy R generators under `resources/generators/` until they retire.
Store Release Build may not import either, or run them by module or by path.

Both sides may import the Release Bundle contract. Manifest Generation's side
of that is the `ogstores` modules it may import, listed in `CONTRACT`.

Run from the repository root:
    python3 tests/packaging/test_boundary.py
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# What a dependency on Manifest Generation looks like as an import.
GENERATION_MODULES = re.compile(r"^(manifest_generation|resources\.generators|resources)(\.|$)")
# ... and as a string handed to a subprocess: `-m manifest_generation...`, or
# a path into the package or the legacy generators.
GENERATION_PATHS = re.compile(r"manifest_generation|resources/generators")

# The `ogstores` modules Manifest Generation imports: the Release Bundle
# contract (`bundle`), Store id and Artifact Root paths (`paths`), and the
# registry index the emitter refreshes (`index`).
CONTRACT = {"ogstores", "ogstores.bundle", "ogstores.paths", "ogstores.index"}


def store_release_build_files() -> list[Path]:
    return sorted((REPO_ROOT / "src" / "ogstores").rglob("*.py")) + [
        REPO_ROOT / "workflow" / "release.py",
        REPO_ROOT / "workflow" / "Snakefile",
    ]


def imported_modules(source: str) -> list[str]:
    """Every module a Python source imports, by `import`, `from` or `import_module`."""
    modules: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.append(node.module)
            modules += [f"{node.module}.{alias.name}" for alias in node.names]
        elif (
            isinstance(node, ast.Call)
            and ast.unparse(node.func).split(".")[-1] in {"import_module", "__import__"}
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            modules.append(node.args[0].value)
    return modules


def snakefile_imports(source: str) -> list[str]:
    """The Snakefile is not plain Python, so its import lines are read as text."""
    modules: list[str] = []
    for line in source.splitlines():
        match = re.match(r"^\s*from\s+([\w.]+)\s+import\s+(.+)$", line)
        if match:
            modules.append(match.group(1))
            modules += [f"{match.group(1)}.{name.split(' as ')[0].strip(' ()')}" for name in match.group(2).split(",")]
            continue
        match = re.match(r"^\s*import\s+(.+)$", line)
        if match:
            modules += [name.split(" as ")[0].strip() for name in match.group(1).split(",")]
    return modules


def code_strings(source: str) -> list[str]:
    """String constants in Python code, docstrings excluded."""
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
    ]


def snakefile_code_lines(source: str) -> list[str]:
    return [line for line in source.splitlines() if not line.lstrip().startswith("#")]


class ScannerTest(unittest.TestCase):
    """The scan finds what it looks for, so a clean result means something."""

    def test_python_imports_of_generation_are_found(self) -> None:
        source = (
            "import manifest_generation.emit\n"
            "from resources.generators.lib import emit\n"
            "from resources import generators\n"
            "importlib.import_module('manifest_generation.generate_candidate')\n"
        )
        offending = [m for m in imported_modules(source) if GENERATION_MODULES.match(m)]
        self.assertEqual(
            offending,
            [
                "manifest_generation.emit",
                "resources.generators.lib",
                "resources.generators.lib.emit",
                "resources",
                "resources.generators",
                "manifest_generation.generate_candidate",
            ],
        )

    def test_ogstores_resources_is_not_mistaken_for_generation(self) -> None:
        modules = imported_modules("from ogstores import resources\nimport ogstores.resources\n")
        self.assertEqual([m for m in modules if GENERATION_MODULES.match(m)], [])

    def test_snakefile_imports_are_found(self) -> None:
        source = "from manifest_generation import emit\nfrom ogstores import bundle, paths\nimport os\n"
        self.assertEqual(
            snakefile_imports(source),
            ["manifest_generation", "manifest_generation.emit", "ogstores", "ogstores.bundle", "ogstores.paths", "os"],
        )

    def test_a_subprocess_into_generation_is_found(self) -> None:
        strings = code_strings('"""Docs may name manifest_generation."""\nrun(["python3", "-m", "manifest_generation.emit"])\n')
        self.assertEqual([s for s in strings if GENERATION_PATHS.search(s)], ["manifest_generation.emit"])


class StoreReleaseBuildBoundaryTest(unittest.TestCase):
    def test_manifest_generation_is_an_installed_package_of_this_checkout(self) -> None:
        import manifest_generation

        self.assertEqual(
            Path(manifest_generation.__file__).resolve().parent,
            REPO_ROOT / "src" / "manifest_generation",
        )

    def test_store_release_build_imports_nothing_from_manifest_generation(self) -> None:
        offenders = []
        for path in store_release_build_files():
            source = path.read_text(encoding="utf-8")
            modules = snakefile_imports(source) if path.name == "Snakefile" else imported_modules(source)
            relative = path.relative_to(REPO_ROOT)
            offenders += [f"{relative}: imports {m}" for m in modules if GENERATION_MODULES.match(m)]
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_store_release_build_runs_nothing_from_manifest_generation(self) -> None:
        offenders = []
        for path in store_release_build_files():
            source = path.read_text(encoding="utf-8")
            strings = snakefile_code_lines(source) if path.name == "Snakefile" else code_strings(source)
            relative = path.relative_to(REPO_ROOT)
            offenders += [f"{relative}: {s.strip()!r}" for s in strings if GENERATION_PATHS.search(s)]
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_manifest_generation_imports_only_the_contract_from_ogstores(self) -> None:
        offenders = []
        package = REPO_ROOT / "src" / "manifest_generation"
        self.assertTrue(package.is_dir(), f"{package} is missing")
        for path in sorted(package.rglob("*.py")):
            relative = path.relative_to(REPO_ROOT)
            for module in imported_modules(path.read_text(encoding="utf-8")):
                if module.split(".")[0] != "ogstores":
                    continue
                # `from ogstores import bundle` names the module ogstores.bundle;
                # `from ogstores.bundle import check` names a member of it.
                parent = module.rsplit(".", 1)[0]
                if module not in CONTRACT and (parent == "ogstores" or parent not in CONTRACT):
                    offenders.append(f"{relative}: imports {module}")
        self.assertEqual(offenders, [], "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()

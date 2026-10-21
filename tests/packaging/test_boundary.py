#!/usr/bin/env python3
"""Store Release Build imports nothing from Manifest Generation (#249, ADR 0035).

The two workflows meet only at the Release Bundle. Store Release Build is
`src/ogstores`, `workflow/Snakefile` and `workflow/release.py`; Manifest
Generation is the `manifest_generation` package (`src/manifest_generation`),
plus the legacy R generators under `resources/generators/` until they retire.
Store Release Build may not import either, or run them by module or by path.

Both sides may import the Release Bundle contract. The `ogstores` modules
Manifest Generation may import are listed in `CONTRACT_AND_SHARED_LAYOUT`: the
contract, and the registry layout it shares with Store Release Build.

Run from the repository root:
    python3 tests/packaging/test_boundary.py
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# What a dependency on Manifest Generation looks like as an import. `resources`
# covers `resources.generators` and anything else a generator might import.
GENERATION_MODULES = re.compile(r"^(manifest_generation|resources)(\.|$)")
# ... and as a string handed to a subprocess: `-m manifest_generation...`, or
# a path into the package or the legacy generators.
GENERATION_PATHS = re.compile(r"manifest_generation|resources/generators")

# The `ogstores` modules Manifest Generation may import: the contract and the
# registry layout it shares. ADR 0035 has each side depend only on the Release
# Bundle contract, which is `bundle`. The other two are not the contract but the
# registry layout both sides write into, and the dependency on them predates
# #249: `paths` is the Store id grammar and the Artifact Root / work-root
# resolution the emitter must agree with, and `index` regenerates stores.tsv
# and STORES.md after an emit. Adding a module here widens the boundary.
CONTRACT_AND_SHARED_LAYOUT = {"ogstores", "ogstores.bundle", "ogstores.paths", "ogstores.index"}


# The Analysis Catalog (#323, ADR 0039) reads Release Bundles through the contract
# and the registry layout, and nothing else of `ogstores`. Nothing imports it, so
# it can move to the API's repository.
CATALOG_PACKAGE = "analysis_catalog"
CATALOG_OGSTORES_SURFACE = {"ogstores", "ogstores.bundle", "ogstores.paths"}


def outside_catalog_surface(module: str) -> bool:
    """An `ogstores` import beyond `ogstores.bundle` and `ogstores.paths`.

    `from ogstores import bundle` names `ogstores.bundle`, and `from
    ogstores.bundle import check` names a member of it, as in
    `outside_shared_surface`; the catalog's surface is narrower.
    """
    if module.split(".")[0] != "ogstores" or module in CATALOG_OGSTORES_SURFACE:
        return False
    parent = module.rsplit(".", 1)[0]
    return parent == "ogstores" or parent not in CATALOG_OGSTORES_SURFACE


def imports_catalog(module: str) -> bool:
    return module.split(".")[0] == CATALOG_PACKAGE


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


def snakefile_includes(source: str) -> list[str]:
    """What a Snakefile pulls in: `include:` targets and `module` snakefiles."""
    return [
        match.group(1).strip()
        for line in snakefile_code_lines(source)
        if (match := re.match(r"^\s*(?:include|snakefile)\s*:\s*(.+)$", line))
    ]


def includes_generation(target: str) -> bool:
    return "generate.smk" in target or bool(GENERATION_PATHS.search(target))


def outside_shared_surface(module: str) -> bool:
    """An `ogstores` import beyond `CONTRACT_AND_SHARED_LAYOUT`.

    `from ogstores import bundle` names the module `ogstores.bundle`; `from
    ogstores.bundle import check` names a member of it, so a name is allowed
    when it, or its parent other than bare `ogstores`, is listed.
    """
    if module.split(".")[0] != "ogstores" or module in CONTRACT_AND_SHARED_LAYOUT:
        return False
    parent = module.rsplit(".", 1)[0]
    return parent == "ogstores" or parent not in CONTRACT_AND_SHARED_LAYOUT


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

    def test_a_snakefile_include_of_generation_is_found(self) -> None:
        source = (
            'include: "rules/common.smk"\n'
            'include: "generate.smk"\n'
            "# include: \"generate.smk\" (a comment)\n"
            "module gen:\n"
            '    snakefile: "../src/manifest_generation/rules.smk"\n'
        )
        self.assertEqual(
            [t for t in snakefile_includes(source) if includes_generation(t)],
            ['"generate.smk"', '"../src/manifest_generation/rules.smk"'],
        )

    def test_only_the_shared_ogstores_surface_is_allowed(self) -> None:
        allowed = ["ogstores", "ogstores.bundle", "ogstores.bundle.check", "ogstores.paths.artifact_root", "os"]
        refused = ["ogstores.plan", "ogstores.run", "ogstores.run.run_step", "ogstores.release"]
        self.assertEqual([m for m in allowed if outside_shared_surface(m)], [])
        self.assertEqual([m for m in refused if outside_shared_surface(m)], refused)

    def test_the_catalog_surface_is_narrower_than_the_shared_one(self) -> None:
        allowed = ["ogstores", "ogstores.bundle", "ogstores.bundle.check", "ogstores.paths.artifact_root", "os"]
        refused = ["ogstores.index", "ogstores.manifest", "ogstores.register", "ogstores.index.render_stores_tsv"]
        self.assertEqual([m for m in allowed if outside_catalog_surface(m)], [])
        self.assertEqual([m for m in refused if outside_catalog_surface(m)], refused)

    def test_an_import_of_the_catalog_is_found(self) -> None:
        modules = imported_modules("import analysis_catalog.build\nfrom analysis_catalog import publish\nimport analysis\n")
        self.assertEqual(
            [m for m in modules if imports_catalog(m)],
            ["analysis_catalog.build", "analysis_catalog", "analysis_catalog.publish"],
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

    def test_the_snakefile_includes_nothing_from_manifest_generation(self) -> None:
        snakefile = (REPO_ROOT / "workflow" / "Snakefile").read_text(encoding="utf-8")
        offenders = [t for t in snakefile_includes(snakefile) if includes_generation(t)]
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_store_release_build_reads_no_quality_check_results(self) -> None:
        """Only the contract validates `sidecars/quality_checks.tsv` (#285).

        Quality Checks decide membership before the bundle exists, and
        `analyses.tsv` already carries the decision, so Store Release Build has
        no reason to read their results.
        """
        contract = REPO_ROOT / "src" / "ogstores" / "bundle.py"
        offenders = [
            str(path.relative_to(REPO_ROOT))
            for path in store_release_build_files()
            if path != contract and "quality_checks" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_manifest_generation_imports_only_the_shared_ogstores_surface(self) -> None:
        offenders = []
        package = REPO_ROOT / "src" / "manifest_generation"
        self.assertTrue(package.is_dir(), f"{package} is missing")
        for path in sorted(package.rglob("*.py")):
            relative = path.relative_to(REPO_ROOT)
            for module in imported_modules(path.read_text(encoding="utf-8")):
                if outside_shared_surface(module):
                    offenders.append(f"{relative}: imports {module}")
        self.assertEqual(offenders, [], "\n".join(offenders))


class AnalysisCatalogBoundaryTest(unittest.TestCase):
    def test_analysis_catalog_is_an_installed_package_of_this_checkout(self) -> None:
        import analysis_catalog

        self.assertEqual(
            Path(analysis_catalog.__file__).resolve().parent,
            REPO_ROOT / "src" / CATALOG_PACKAGE,
        )

    def test_nothing_in_ogstores_or_manifest_generation_imports_the_catalog(self) -> None:
        offenders = []
        files = store_release_build_files() + sorted((REPO_ROOT / "src" / "manifest_generation").rglob("*.py"))
        for path in files:
            source = path.read_text(encoding="utf-8")
            modules = snakefile_imports(source) if path.name == "Snakefile" else imported_modules(source)
            relative = path.relative_to(REPO_ROOT)
            offenders += [f"{relative}: imports {m}" for m in modules if imports_catalog(m)]
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_nothing_in_ogstores_or_manifest_generation_runs_the_catalog(self) -> None:
        offenders = []
        files = store_release_build_files() + sorted((REPO_ROOT / "src" / "manifest_generation").rglob("*.py"))
        for path in files:
            source = path.read_text(encoding="utf-8")
            strings = snakefile_code_lines(source) if path.name == "Snakefile" else code_strings(source)
            relative = path.relative_to(REPO_ROOT)
            offenders += [f"{relative}: {s.strip()!r}" for s in strings if CATALOG_PACKAGE in s]
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_the_catalog_imports_only_the_contract_and_the_registry_layout_from_ogstores(self) -> None:
        package = REPO_ROOT / "src" / CATALOG_PACKAGE
        self.assertTrue(package.is_dir(), f"{package} is missing")
        offenders = []
        for path in sorted(package.rglob("*.py")):
            relative = path.relative_to(REPO_ROOT)
            for module in imported_modules(path.read_text(encoding="utf-8")):
                if outside_catalog_surface(module) or GENERATION_MODULES.match(module):
                    offenders.append(f"{relative}: imports {module}")
        self.assertEqual(offenders, [], "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()

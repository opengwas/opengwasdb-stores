#!/usr/bin/env python3
"""Nothing patches `sys.path` to import this repository's own code (#257, #249).

`ogstores`, `curation` and `manifest_generation` install editable into the
pixi environments (pyproject.toml), so no tracked Python file, and not the
Snakefile, inserts the repository root or its `src/` into `sys.path`.

Manifest Generation code and the tests go further (#249): they insert nothing
into `sys.path` at all, not even a script's own directory, and load no module
by file path (`spec_from_file_location`, `SourceFileLoader`, `runpy.run_path`).
They import modules. `EXEMPT_TESTS` lists the tests exempt from that, each
with its reason: each exercises a script that is not generation code and
cannot be imported as a module.

Run from the repository root:
    python3 tests/packaging/test_imports.py
"""

from __future__ import annotations

import ast
import re
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# What a repository-root or src/ insertion looks like in this code base: the
# root itself, a src directory, or src handed to a subprocess.
ROOT_OR_SRC = re.compile(
    r"""^(str\()?\s*(
        REPO_ROOT | ROOT | SRC_DIR | _?src
      | Path\(__file__\)\.resolve\(\)\.parents\[\d\]
      | .*["']src["'] .* | sys\.argv\[1\]
    )\s*\)?$""",
    re.VERBOSE,
)

# Deliberate: the benchmark report imports *another* checkout's opengwasdb.
ALLOWED: set[tuple[str, str]] = {
    ("tools/benchmarks/release-benchmark-report.py", "str(checkout)"),
    ("tools/benchmarks/release-benchmark-report.py", "str(src)"),
}

# Where the stricter #249 rule applies: Manifest Generation code (the package,
# and the R code that remains under resources/generators) and tests.
GENERATION_AND_TESTS = ("src/manifest_generation/", "resources/generators/", "tests/")

# Tests of scripts that are not generation code and are not importable modules
# (a hyphenated filename, or a directory that is not a package), with what each
# one exercises. Each may load that script by path or put its directory on
# sys.path.
EXEMPT_TESTS: dict[str, str] = {
    "tests/ld-panel-generation/run_tests.py": "LD-panel Reference Resource scripts (tools/ld-panel)",
    "tests/hapmap3-resource/test_hapmap3_resource.py": "the HapMap3 Reference Resource verifier (tools/reference-resources)",
    "tests/hm3-benchmark-report/test_release_benchmark_report.py": "release-benchmark-report.py, which imports another checkout",
    "tests/packaging/test_discovery.py": "the test runner, tests/run_all_tests.py",
    "tests/run_all_tests.py": "the test runner, which lists a suite's tests as running its script would, "
    "with the script's directory first on sys.path (#327)",
    "tests/packaging/test_tool_paths.py": "the moved tools' default paths (tools/, and the test runner)",
    "tests/finngen-r13-pipeline/test_pipeline_end_to_end.py":
        "the phase-b-candidate stage-test fixtures, whose directory is not a package",
    "tests/ukb-b-pipeline/test_pipeline_end_to_end.py":
        "the phase-b-candidate stage-test fixtures, whose directory is not a package",
    "tests/eqtlgen-pipeline/test_pipeline_end_to_end.py":
        "the phase-b-candidate stage-test fixtures, whose directory is not a package",
    "tests/prune-worktrees/test_prune_worktrees.py":
        "tools/prune_worktrees.py, a script, which three tests load to patch one step of a run",
    "tests/release-versioning/script_modules.py":
        "tools/check_changelog.py and tools/release.py, the release-versioning entry points (#386), "
        "which are scripts rather than importable modules",
    "tests/workflow/test_release_faults.py": "Store Release Build tests importing their sibling test module",
}

# This file spells out the patterns it looks for, so it does not scan itself.
THIS_FILE = "tests/packaging/test_imports.py"

# Loading a module from a file path rather than importing it.
FILE_PATH_LOADERS = {"spec_from_file_location", "SourceFileLoader", "run_path", "load_source"}

# `src/` handed to a subprocess through the environment instead.
PYTHONPATH_SRC = re.compile(r"PYTHONPATH.*(SRC_DIR|REPO_ROOT|['\"]src['\"])")
# Deliberate: the HM3 proof runs a separate capability runtime, an environment
# without this repository installed.
PYTHONPATH_ALLOWED: set[str] = {"tools/benchmarks/hm3-pipeline-proof.py"}


def path_insertions(source: str) -> list[str]:
    """The argument of every `sys.path.insert/append/extend(...)` call.

    Inside a `for` loop over a literal tuple or list, the loop's items are
    what is inserted, so each item is reported as well.
    """
    found = []
    tree = ast.parse(source)
    insert_or_append = r"sys\.path\.(insert|append|extend)"
    for loop in ast.walk(tree):
        if isinstance(loop, ast.For) and isinstance(loop.iter, (ast.Tuple, ast.List)):
            body = ast.unparse(ast.Module(body=loop.body, type_ignores=[]))
            if re.search(insert_or_append, body):
                found += [ast.unparse(item) for item in loop.iter.elts]
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"insert", "append", "extend"}
            and ast.unparse(node.func.value) == "sys.path"
            and node.args
        ):
            found.append(ast.unparse(node.args[-1]))
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            # Code handed to a subprocess as a string, e.g. `python -c "..."`.
            found += re.findall(r"sys\.path\.(?:insert\(\s*0\s*,|append\()\s*(.*)\)\s*;?\s*$", node.value, re.M)
    return found


def file_path_loads(source: str) -> list[str]:
    """Every use of a loader that imports a module from a file path."""
    found = []
    for node in ast.walk(ast.parse(source)):
        name = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else None
        if name in FILE_PATH_LOADERS:
            found.append(ast.unparse(node))
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module in {"importlib.util", "importlib.machinery", "runpy", "imp"}:
            found += [f"from {node.module} import {a.name}" for a in node.names if a.name in FILE_PATH_LOADERS]
    return found


def tracked_python(prefixes: tuple[str, ...] = ("",)) -> list[str]:
    """Tracked Python files under `prefixes`, this file excluded."""
    tracked = subprocess.run(
        ["git", "ls-files", "*.py"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    return [relative for relative in tracked if relative.startswith(prefixes) and relative != THIS_FILE]


class GenerationImportsModulesTest(unittest.TestCase):
    """#249: generation code and tests import modules; they never patch sys.path."""

    def test_the_scanners_find_what_they_look_for(self) -> None:
        source = (
            "import importlib.util, runpy, sys\n"
            "from importlib.machinery import SourceFileLoader\n"
            "sys.path.insert(0, str(HERE))\n"
            "spec = importlib.util.spec_from_file_location('x', path)\n"
            "runpy.run_path('script.py')\n"
        )
        self.assertEqual(path_insertions(source), ["str(HERE)"])
        self.assertEqual(
            sorted(file_path_loads(source)),
            sorted([
                "importlib.util.spec_from_file_location",
                "runpy.run_path",
                "from importlib.machinery import SourceFileLoader",
            ]),
        )

    def test_generation_code_and_tests_insert_nothing_into_sys_path(self) -> None:
        offenders = [
            f"{relative}: sys.path <- {argument.strip()}"
            for relative in tracked_python(GENERATION_AND_TESTS)
            if relative not in EXEMPT_TESTS
            for argument in path_insertions((REPO_ROOT / relative).read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_generation_code_and_tests_load_no_module_by_file_path(self) -> None:
        offenders = [
            f"{relative}: {use}"
            for relative in tracked_python(GENERATION_AND_TESTS)
            if relative not in EXEMPT_TESTS
            for use in file_path_loads((REPO_ROOT / relative).read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_every_exemption_is_still_needed(self) -> None:
        stale = [
            relative
            for relative in EXEMPT_TESTS
            if not (REPO_ROOT / relative).is_file()
            or not (
                path_insertions((REPO_ROOT / relative).read_text(encoding="utf-8"))
                or file_path_loads((REPO_ROOT / relative).read_text(encoding="utf-8"))
            )
        ]
        self.assertEqual(stale, [], "exempt but no longer loading by path: " + ", ".join(stale))


class NoPathInsertionTest(unittest.TestCase):
    def test_no_python_file_inserts_the_repository_root_or_src(self) -> None:
        offenders = []
        for relative in tracked_python():
            for argument in path_insertions((REPO_ROOT / relative).read_text(encoding="utf-8")):
                if ROOT_OR_SRC.match(argument.strip()) and (relative, argument.strip()) not in ALLOWED:
                    offenders.append(f"{relative}: sys.path <- {argument.strip()}")
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_no_python_file_hands_src_to_a_subprocess_through_pythonpath(self) -> None:
        offenders = [
            f"{relative}:{number}: {line.strip()}"
            for relative in tracked_python()
            if relative not in PYTHONPATH_ALLOWED
            for number, line in enumerate((REPO_ROOT / relative).read_text(encoding="utf-8").splitlines(), 1)
            if PYTHONPATH_SRC.search(line)
        ]
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_the_snakefile_does_not_patch_sys_path(self) -> None:
        snakefile = (REPO_ROOT / "workflow" / "Snakefile").read_text(encoding="utf-8")

        self.assertNotRegex(snakefile, r"sys\.path\.(insert|append)")


if __name__ == "__main__":
    unittest.main()

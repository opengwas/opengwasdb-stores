#!/usr/bin/env python3
"""Nothing patches `sys.path` to import this repository's own code (#257).

`ogstores`, `curation` and the generator library install editable into the
pixi environments (pyproject.toml), so no tracked Python file, and not the
Snakefile, inserts the repository root or its `src/` into `sys.path`. Adding a
script's own directory is a different thing, left for #249 to retire.

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
    ("resources/scripts/release-benchmark-report.py", "str(checkout)"),
    ("resources/scripts/release-benchmark-report.py", "str(src)"),
}

# `src/` handed to a subprocess through the environment instead.
PYTHONPATH_SRC = re.compile(r"PYTHONPATH.*(SRC_DIR|REPO_ROOT|['\"]src['\"])")
# Deliberate: the HM3 proof runs a separate capability runtime, an environment
# without this repository installed.
PYTHONPATH_ALLOWED: set[str] = {"resources/scripts/hm3-pipeline-proof.py"}


def path_insertions(source: str) -> list[str]:
    """The argument of every `sys.path.insert(...)`/`sys.path.append(...)` call.

    Inside a `for` loop over a literal tuple or list, the loop's items are
    what is inserted, so each item is reported as well.
    """
    found = []
    tree = ast.parse(source)
    for loop in ast.walk(tree):
        if isinstance(loop, ast.For) and isinstance(loop.iter, (ast.Tuple, ast.List)):
            body = ast.unparse(ast.Module(body=loop.body, type_ignores=[]))
            if re.search(r"sys\.path\.(insert|append)", body):
                found += [ast.unparse(item) for item in loop.iter.elts]
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"insert", "append"}
            and ast.unparse(node.func.value) == "sys.path"
            and node.args
        ):
            found.append(ast.unparse(node.args[-1]))
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            # Code handed to a subprocess as a string, e.g. `python -c "..."`.
            found += re.findall(r"sys\.path\.(?:insert\(\s*0\s*,|append\()\s*(.*)\)\s*;?\s*$", node.value, re.M)
    return found


class NoPathInsertionTest(unittest.TestCase):
    def test_no_python_file_inserts_the_repository_root_or_src(self) -> None:
        tracked = subprocess.run(
            ["git", "ls-files", "*.py"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        ).stdout.split()
        offenders = []
        for relative in tracked:
            if relative == "tests/packaging/test_imports.py":
                continue
            for argument in path_insertions((REPO_ROOT / relative).read_text(encoding="utf-8")):
                if ROOT_OR_SRC.match(argument.strip()) and (relative, argument.strip()) not in ALLOWED:
                    offenders.append(f"{relative}: sys.path <- {argument.strip()}")
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_no_python_file_hands_src_to_a_subprocess_through_pythonpath(self) -> None:
        tracked = subprocess.run(
            ["git", "ls-files", "*.py"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        ).stdout.split()
        offenders = [
            f"{relative}:{number}: {line.strip()}"
            for relative in tracked
            if relative not in PYTHONPATH_ALLOWED and relative != "tests/packaging/test_imports.py"
            for number, line in enumerate((REPO_ROOT / relative).read_text(encoding="utf-8").splitlines(), 1)
            if PYTHONPATH_SRC.search(line)
        ]
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_the_snakefile_does_not_patch_sys_path(self) -> None:
        snakefile = (REPO_ROOT / "workflow" / "Snakefile").read_text(encoding="utf-8")

        self.assertNotRegex(snakefile, r"sys\.path\.(insert|append)")


if __name__ == "__main__":
    unittest.main()

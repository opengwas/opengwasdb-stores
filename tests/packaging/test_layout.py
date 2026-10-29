#!/usr/bin/env python3
"""One home per kind of thing (#317, ADR 0038).

Python lives under `src/` (the packages), `tests/` (the suites), `tools/`
(scripts) or `workflow/` (the Snakemake wiring and release entry point), and
nowhere else: not at the repository root, and not as a namespace package such
as `rogue/module.py`. Only `src/` and `tests/` hold packages. `resources/`
holds inputs only, so it holds no code at all: the last R metadata resolvers
went with #350, and `resources/scripts/`, the miscellany this layout replaced,
does not come back.

Run from the repository root:
    python3 tests/packaging/test_layout.py
"""

from __future__ import annotations

import ast
import subprocess
import unittest
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[2]

# Where Python may live, and where of those a package may.
PYTHON_HOMES = ("src/", "tests/", "tools/", "workflow/")
PACKAGE_HOMES = ("src/", "tests/")

# What counts as code: a file with one of these suffixes (compared lower-case),
# a Snakefile, or anything that starts with a shebang line.
CODE_SUFFIXES = frozenset({".py", ".r", ".rmd", ".qmd", ".sh", ".smk", ".ipynb", ".js", ".pl"})


def tracked_files() -> list[str]:
    return subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.split("\0")[:-1]


def is_code(relative: str, head: bytes) -> bool:
    """Whether a file is code, from its name and its first bytes."""
    path = PurePosixPath(relative)
    return path.suffix.lower() in CODE_SUFFIXES or path.name == "Snakefile" or head.startswith(b"#!")


def head_of(relative: str) -> bytes:
    with (REPO_ROOT / relative).open("rb") as handle:
        return handle.read(2)


def python_outside_homes(files: list[str]) -> list[str]:
    """Python files outside `PYTHON_HOMES`: root modules and namespace packages included."""
    return sorted(relative for relative in files if relative.endswith(".py") and not relative.startswith(PYTHON_HOMES))


def packages_outside_homes(files: list[str]) -> list[str]:
    """Regular packages (an `__init__.py`) outside `src/` and `tests/`."""
    return sorted(
        str(PurePosixPath(relative).parent)
        for relative in files
        if PurePosixPath(relative).name == "__init__.py" and not relative.startswith(PACKAGE_HOMES)
    )


#: The `tempfile` factories: each writes where `dir=` says, and to the host's
#: `/tmp` without it. Everything this project writes lives under
#: `/data/opengwasdb` (CONTRIBUTING, Worktrees), so a scratch root is always
#: named -- under `/data` by default, or through the tree's own variable (#390).
TEMP_FACTORIES = frozenset({
    "TemporaryDirectory", "mkdtemp", "mkstemp", "NamedTemporaryFile", "TemporaryFile", "SpooledTemporaryFile",
})

#: Where the scratch rule is enforced: the site's and the yardstick's code, and
#: the scripts beside them (#390 review, item 4). A test may use `tempfile`
#: freely -- the runner sets the suite's own `$TMPDIR`.
SCRATCH_TREES = ("src/project_site/", "src/yardstick/", "tools/")


def _temp_factory(func: ast.expr) -> str | None:
    """The factory a call names, for `tempfile.mkdtemp(...)` and a bare `mkdtemp(...)` alike."""
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "tempfile":
        return func.attr if func.attr in TEMP_FACTORIES else None
    if isinstance(func, ast.Name) and func.id in TEMP_FACTORIES:
        return func.id
    return None


def missing_dir_calls(relative: str, source: str) -> list[str]:
    """Every `tempfile` scratch call in one file that names no `dir=`."""
    found = []
    for node in ast.walk(ast.parse(source, filename=relative)):
        if not isinstance(node, ast.Call):
            continue
        factory = _temp_factory(node.func)
        if factory and not any(keyword.arg == "dir" for keyword in node.keywords):
            found.append(f"{relative}:{node.lineno}: tempfile.{factory} without dir=")
    return found


def temp_calls_without_dir(files: list[str]) -> list[str]:
    """Every `tempfile` scratch call in `SCRATCH_TREES` that names no `dir=`."""
    found = []
    for relative in files:
        if not relative.endswith(".py") or not relative.startswith(SCRATCH_TREES):
            continue
        found += missing_dir_calls(relative, (REPO_ROOT / relative).read_text(encoding="utf-8"))
    return sorted(found)


def code_under_resources(files_and_heads: list[tuple[str, bytes]]) -> list[str]:
    return sorted(relative for relative, head in files_and_heads if relative.startswith("resources/") and is_code(relative, head))


class LayoutTest(unittest.TestCase):
    def setUp(self) -> None:
        self.files = [relative for relative in tracked_files() if (REPO_ROOT / relative).is_file()]

    def test_no_scratch_call_falls_back_to_the_hosts_tmp(self) -> None:
        self.assertEqual(
            temp_calls_without_dir(self.files),
            [],
            "name a scratch root under /data for every tempfile call (CONTRIBUTING, Worktrees)",
        )

    def test_the_scratch_scanner_finds_what_it_looks_for(self) -> None:
        self.assertEqual(
            missing_dir_calls("tools/planted.py", (
                "import tempfile\n"
                "from tempfile import mkdtemp\n"
                "a = tempfile.mkdtemp(prefix='x')\n"
                "b = tempfile.TemporaryDirectory(prefix='y')\n"
                "c = mkdtemp(prefix='z')\n"
                "d = tempfile.mkdtemp(prefix='ok', dir=root)\n"
                "e = tempfile.NamedTemporaryFile(delete=False)\n"
                "f = tempfile.TemporaryFile(dir=root)\n"
                "g = tempfile._get_candidate_names()\n"
            )),
            [
                "tools/planted.py:3: tempfile.mkdtemp without dir=",
                "tools/planted.py:4: tempfile.TemporaryDirectory without dir=",
                "tools/planted.py:5: tempfile.mkdtemp without dir=",
                "tools/planted.py:7: tempfile.NamedTemporaryFile without dir=",
            ],
        )

    def test_the_scanners_find_what_they_look_for(self) -> None:
        self.assertEqual(
            python_outside_homes([
                "rogue/module.py", "helper.py", "docs/reports/plot.py", "rogue/README.md",
                "src/curation/round.py", "tests/run_all_tests.py", "tools/env_check.py", "workflow/release.py",
            ]),
            ["docs/reports/plot.py", "helper.py", "rogue/module.py"],
        )
        self.assertEqual(
            packages_outside_homes([
                "curation/__init__.py", "tools/ld-panel/__init__.py", "workflow/__init__.py",
                "src/curation/__init__.py", "tests/__init__.py", "tests/curation/__init__.py",
            ]),
            ["curation", "tools/ld-panel", "workflow"],
        )
        self.assertEqual(
            code_under_resources([
                ("resources/scripts/check_bundles.py", b"#!"),
                ("resources/reference-resources/qc-panel-hg38/build_qc_panel.py", b"im"),
                ("resources/generators/x/generate.R", b"#!"),
                ("resources/scripts/report.qmd", b"--"),
                ("resources/scripts/run-me", b"#!"),
                ("resources/generators/x/config.yaml", b"# "),
                ("resources/inventories/x.tsv", b"so"),
                ("tools/env_check.py", b"#!"),
            ]),
            [
                "resources/generators/x/generate.R",
                "resources/reference-resources/qc-panel-hg38/build_qc_panel.py",
                "resources/scripts/check_bundles.py",
                "resources/scripts/report.qmd",
                "resources/scripts/run-me",
            ],
        )

    def test_no_python_outside_src_tests_tools_and_workflow(self) -> None:
        self.assertEqual(python_outside_homes(self.files), [])

    def test_no_python_package_outside_src_and_tests(self) -> None:
        self.assertEqual(packages_outside_homes(self.files), [])

    def test_resources_holds_no_code(self) -> None:
        code = code_under_resources([(r, head_of(r)) for r in self.files if r.startswith("resources/")])
        self.assertEqual(code, [], "resources/ is inputs only; code belongs in src/, tools/ or docs/reports/")

    def test_resources_scripts_is_gone(self) -> None:
        self.assertEqual([relative for relative in self.files if relative.startswith("resources/scripts/")], [])


if __name__ == "__main__":
    unittest.main()

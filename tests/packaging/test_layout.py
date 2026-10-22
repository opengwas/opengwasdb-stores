#!/usr/bin/env python3
"""One home per kind of thing (#317, ADR 0038).

Python lives under `src/` (the packages), `tests/` (the suites), `tools/`
(scripts) or `workflow/` (the Snakemake wiring and release entry point), and
nowhere else: not at the repository root, and not as a namespace package such
as `rogue/module.py`. Only `src/` and `tests/` hold packages. `resources/`
holds inputs only, so it holds no code, except two R metadata resolvers
under `resources/generators/lib/metadata_resolvers/` that curation's R-resolver
round-trip still sources. Each is listed in `LEGACY_R_GENERATORS` with the
ticket that retires it, and an entry whose file is gone fails, so the list
only shrinks. `resources/scripts/`, the miscellany this layout replaced, does
not come back.

Run from the repository root:
    python3 tests/packaging/test_layout.py
"""

from __future__ import annotations

import re
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

# The R code under resources/, and the ticket that retires each. Every
# generator's R path has retired (epic #282 stage 2), so what is left holds no
# generator: two metadata resolvers curation's R-resolver round-trip still
# sources, and the last shared helpers, which go with this cleanup.
EPIC_CLEANUP = "#282 (curation's R-resolver round-trip still sources these)"
LAST_R_GENERATOR = "#282 (gone with the last R generator)"
LEGACY_R_GENERATORS: dict[str, str] = {
    # These two lost their generator when ukb-b's R path retired with #289, but
    # the curation suites (tests/curation/test_promotion.py, test_e2e_pipeline.py)
    # still drive the R resolver through them to prove a promoted mapping row
    # round-trips: no ticket yet moves curation off the R resolver.
    "resources/generators/lib/metadata_resolvers/canonical_trait_table.R": EPIC_CLEANUP,
    "resources/generators/lib/metadata_resolvers/ontology_contract.R": EPIC_CLEANUP,
    "resources/generators/lib/build_environment.R": LAST_R_GENERATOR,
    "resources/generators/lib/indexed_subsets.R": LAST_R_GENERATOR,
    "resources/generators/lib/schema_validate.R": LAST_R_GENERATOR,
}


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


def code_under_resources(files_and_heads: list[tuple[str, bytes]]) -> list[str]:
    return sorted(relative for relative, head in files_and_heads if relative.startswith("resources/") and is_code(relative, head))


class LayoutTest(unittest.TestCase):
    def setUp(self) -> None:
        self.files = [relative for relative in tracked_files() if (REPO_ROOT / relative).is_file()]

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

    def test_resources_holds_no_code_but_the_listed_r_files(self) -> None:
        code = code_under_resources([(r, head_of(r)) for r in self.files if r.startswith("resources/")])
        unexpected = [relative for relative in code if relative not in LEGACY_R_GENERATORS]
        self.assertEqual(unexpected, [], "resources/ is inputs only; code belongs in src/, tools/ or docs/reports/")

    def test_every_legacy_r_generator_is_still_there_and_cites_its_ticket(self) -> None:
        retired = [relative for relative in LEGACY_R_GENERATORS if relative not in self.files]
        self.assertEqual(retired, [], "retired, so remove from LEGACY_R_GENERATORS")
        uncited = [relative for relative, ticket in LEGACY_R_GENERATORS.items() if not re.search(r"#\d+", ticket)]
        self.assertEqual(uncited, [])

    def test_resources_scripts_is_gone(self) -> None:
        self.assertEqual([relative for relative in self.files if relative.startswith("resources/scripts/")], [])


if __name__ == "__main__":
    unittest.main()

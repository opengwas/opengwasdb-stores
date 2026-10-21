#!/usr/bin/env python3
"""One home per kind of thing (#317, ADR 0038).

Importable Python lives under `src/` (the packages) or `tests/` (the suites),
nowhere else. `resources/` holds inputs only, so it holds no code, except the
legacy R generators that remain under `resources/generators/` until stage 2 of
epic #282 retires them. Each is listed in `LEGACY_R_GENERATORS` with the
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

# Where importable Python may live.
PYTHON_HOMES = ("src/", "tests/")

# What counts as code: a file with one of these suffixes (compared lower-case),
# a Snakefile, or anything that starts with a shebang line.
CODE_SUFFIXES = frozenset({".py", ".r", ".rmd", ".qmd", ".sh", ".smk", ".ipynb", ".js", ".pl"})

# The legacy R generators, and the stage-2 ticket of epic #282 that retires
# each. A shared helper retires with the last generator that sources it.
FINNGEN = "#288 (FinnGen onto the pipeline: AC1 deletes the R generator)"
UKB_B = "#289 (ukb-b onto the pipeline)"
BOTH = "#288 and #289, whichever retires the last R generator"
LEGACY_R_GENERATORS: dict[str, str] = {
    "resources/generators/finngen-r13/generate.R": FINNGEN,
    "resources/generators/lib/finngen_r13_pilot.R": FINNGEN,
    "resources/generators/lib/metadata_resolvers/finngen_manifest.R": FINNGEN,
    "resources/generators/lib/source-formats/finngen-r13-dense/generate.R": FINNGEN,
    "resources/generators/ukb-b/generate.R": UKB_B,
    "resources/generators/lib/opengwas_gwas_vcf_dense.R": UKB_B,
    "resources/generators/lib/metadata_resolvers/opengwas_api.R": UKB_B,
    "resources/generators/lib/metadata_resolvers/canonical_trait_table.R": UKB_B,
    "resources/generators/lib/metadata_resolvers/ontology_contract.R": UKB_B,
    "resources/generators/lib/source-formats/opengwas-gwas-vcf-dense/generate.R": UKB_B,
    "resources/generators/lib/build_environment.R": BOTH,
    "resources/generators/lib/emit_registry.R": BOTH,
    "resources/generators/lib/indexed_subsets.R": BOTH,
    "resources/generators/lib/schema_validate.R": BOTH,
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


def importable_outside_homes(files: list[str]) -> list[str]:
    """Python packages outside `src/` and `tests/`, and modules at the repository root."""
    return sorted(
        {
            str(PurePosixPath(relative).parent) if relative.endswith("__init__.py") else relative
            for relative in files
            if not relative.startswith(PYTHON_HOMES)
            and (PurePosixPath(relative).name == "__init__.py" or ("/" not in relative and relative.endswith(".py")))
        }
    )


def code_under_resources(files_and_heads: list[tuple[str, bytes]]) -> list[str]:
    return sorted(relative for relative, head in files_and_heads if relative.startswith("resources/") and is_code(relative, head))


class LayoutTest(unittest.TestCase):
    def setUp(self) -> None:
        self.files = [relative for relative in tracked_files() if (REPO_ROOT / relative).is_file()]

    def test_the_scanners_find_what_they_look_for(self) -> None:
        self.assertEqual(
            importable_outside_homes([
                "curation/__init__.py", "curation/round.py", "helper.py", "workflow/release.py",
                "src/curation/__init__.py", "tests/__init__.py", "tests/curation/__init__.py",
            ]),
            ["curation", "helper.py"],
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

    def test_no_importable_python_outside_src_and_tests(self) -> None:
        self.assertEqual(importable_outside_homes(self.files), [])

    def test_resources_holds_no_code_but_the_legacy_r_generators(self) -> None:
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

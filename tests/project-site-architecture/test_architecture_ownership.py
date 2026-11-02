#!/usr/bin/env python3
"""Who owns which file: the claims in `docs/architecture/components.yaml` (#418).

The functions under test are the ones the site's architecture collector and this
repository's own ownership check share, so the fixtures here are small literal
models and lists of tracked paths. The last test runs this repository's own model
against the files git tracks, which is the check that keeps the model true.

Run from the repository root:
    python3 tests/project-site-architecture/test_architecture_ownership.py
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

from project_site.collect import architecture_model as model

REPO = Path(__file__).resolve().parents[2]

COMPONENTS = [
    {"id": "orchestration", "paths": ["src/ogstores/", "workflow/Snakefile"]},
    {"id": "release", "paths": ["src/ogstores/release/", "tools/release.py"]},
    {"id": "docs", "paths": ["docs/_quarto.yml"]},
]
TRACKED = [
    "src/ogstores/bundle.py",
    "src/ogstores/release/plan.py",
    "tools/release.py",
    "workflow/Snakefile",
    "docs/_quarto.yml",
    "README.md",
]


class OwnershipTests(unittest.TestCase):
    def test_longest_claimed_prefix_wins(self) -> None:
        claims = model.claims_of(COMPONENTS)
        self.assertEqual(model.owner(claims, "src/ogstores/release/plan.py"), "release")
        self.assertEqual(model.owner(claims, "src/ogstores/bundle.py"), "orchestration")

    def test_a_file_claim_matches_only_that_file(self) -> None:
        claims = model.claims_of(COMPONENTS)
        self.assertEqual(model.owner(claims, "tools/release.py"), "release")
        self.assertIsNone(model.owner(claims, "tools/release.pyc"))
        self.assertIsNone(model.owner(claims, "src/ogstoresx/a.py"))

    def test_an_unclaimed_code_file_is_reported_by_path(self) -> None:
        claims = model.claims_of(COMPONENTS)
        tracked = [*TRACKED, "src/newpkg/thing.py", "tools/check.sh", "workflow/new.smk"]
        gaps = model.check_claims(claims, tracked, model.is_registry_code)
        self.assertEqual(gaps["unclaimed"], ["src/newpkg/thing.py", "tools/check.sh", "workflow/new.smk"])
        self.assertEqual(gaps["stale"], [])

    def test_files_that_are_not_code_need_no_claim(self) -> None:
        claims = model.claims_of(COMPONENTS)
        tracked = [*TRACKED, "src/ogstores/README.md", "workflow/profiles/slurm/config.yaml", "docs/x.md"]
        self.assertEqual(model.check_claims(claims, tracked, model.is_registry_code)["unclaimed"], [])

    def test_a_stale_claim_is_reported_with_its_component(self) -> None:
        claims = model.claims_of(COMPONENTS)
        tracked = [p for p in TRACKED if p not in ("tools/release.py", "src/ogstores/release/plan.py")]
        gaps = model.check_claims(claims, tracked, model.is_registry_code)
        self.assertEqual(
            gaps["stale"],
            [
                {"path": "src/ogstores/release/", "component": "release"},
                {"path": "tools/release.py", "component": "release"},
            ],
        )

    def test_engine_code_is_python_under_the_package(self) -> None:
        self.assertTrue(model.is_engine_code("opengwasdb/store/paths.py"))
        self.assertFalse(model.is_engine_code("opengwasdb/data.json"))
        self.assertFalse(model.is_engine_code("tests/test_x.py"))

    def test_a_nul_separated_list_returns_a_path_with_a_tab_or_newline_whole(self) -> None:
        listing = "src/a\tb.py\0src/c\nd.py\0src/e.py\0"
        self.assertEqual(model.split_nul(listing), ["src/a\tb.py", "src/c\nd.py", "src/e.py"])
        self.assertEqual(model.split_nul(""), [])

    def test_this_repositorys_model_claims_every_code_file_and_nothing_stale(self) -> None:
        tracked = model.split_nul(
            subprocess.run(["git", "-C", str(REPO), "ls-files", "-z"], capture_output=True, check=True).stdout.decode()
        )
        loaded = model.load_model((REPO / "docs/architecture/components.yaml").read_text(encoding="utf-8"))
        gaps = model.check_claims(model.claims_of(loaded["components"]), tracked, model.is_registry_code)
        self.assertEqual(gaps, {"unclaimed": [], "stale": []})


if __name__ == "__main__":
    unittest.main()

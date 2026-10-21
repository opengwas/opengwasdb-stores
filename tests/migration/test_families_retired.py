#!/usr/bin/env python3
"""One home for Release Bundles: `families/` and `.rebuild133/` are retired (#252).

Issue #252 retired the two directories that kept Release Bundles beside
`stores/`: the top-level `families/` archive in the pre-ADR-0022 family-first
schema, and the `.rebuild133/` leftover, which nothing read. What remains has
to be registered in `stores/` or gone, because as long as generators could keep
writing to a third place it kept coming back.

This suite pins that outcome, not the migration itself:

- no tracked file lives under either directory;
- no code, config or test reads either one, so nothing can start using them
  again. Decision records (ADRs) and dated historical documents are exempt:
  they describe the repository as it was, and the bundle documents keep the
  `generator.commands` and `notes` that record what actually ran;
- every bundle the confirmed triage kept is registered under its own OGS id,
  keeps its historical Release Status and Validation Record, passes the Release
  Bundle contract, and carries the membership of its `derived_from` parent.

Run from the repository root:

    python3 tests/migration/test_families_retired.py
"""

from __future__ import annotations

import csv
import re
import subprocess
import unittest
from pathlib import Path

import yaml

from ogstores import bundle as bundle_mod

REPO_ROOT = Path(__file__).resolve().parents[2]
STORES = REPO_ROOT / "stores"

RETIRED_DIRECTORIES: tuple[str, ...] = ("families", ".rebuild133")

# Every retired-directory reference allowed to stay. Each is a record of the
# past, not an instruction: the ADRs describe the family-first layout they
# superseded, and these dated audits are evidence of runs that have happened.
EXEMPT_PREFIXES: tuple[str, ...] = (
    "docs/adr/",
    "docs/release-seam-audit.md",
    "docs/software-management-audit.md",
    "docs/workflow-engine-comparison.md",
    "docs/ukb-b-restored-inventory-2026-10-11.md",
)

# Generated views of the bundles quote their `generator.commands` verbatim, so
# they repeat whatever path a recorded command ran on.
EXEMPT_GENERATED_VIEWS: frozenset[str] = frozenset({"stores.tsv", "STORES.md"})

# A frozen Source Inventory's evidence records the path a file was frozen at,
# and the path it was moved from when only its location changed.
EXEMPT_EVIDENCE_NAME = re.compile(
    r"\Aresources/inventories/[^/]+\.(?:meta\.yaml|receipt\.json)\Z"
)

# A `stores/<id>/release.yaml` is a Release Bundle's own record. Its
# `generator.commands` and `notes` say what ran, on what paths, and are never
# rewritten to match today's layout (issue #252 acceptance criteria, ADR 0014).
EXEMPT_BUNDLE_RECORD = re.compile(r"\Astores/OGS-\d{5}/release\.yaml\Z")

# Bundles the confirmed triage (issue #252, comment 21 Oct 2026) kept, with the
# historical Release Status and evidence each must still carry.
# (store_id, label, derived_from, status, layout, completion_state, n_analyses)
MIGRATED_BUNDLES: tuple[tuple[str, str, str | None, str, str, str, int], ...] = (
    ("OGS-00022", "r13-pilot-20-completed", "OGS-00003", "validated", "dense", "reference_completed", 20),
    ("OGS-00023", "eur-hybrid-pilot-10-completed", "OGS-00004", "validated", "hybrid", "reference_completed", 10),
    ("OGS-00024", "eur-hybrid-quant-pilot-10-completed", "OGS-00005", "validated", "hybrid", "reference_completed", 10),
    ("OGS-00025", "2023-chen-full-african", None, "built", "ragged", "observed_only", 1034),
    ("OGS-00026", "2023-chen-full-east-asian", None, "built", "ragged", "observed_only", 1038),
    ("OGS-00027", "2023-chen-full-south-asian", None, "built", "ragged", "observed_only", 971),
)


def tracked_files() -> list[str]:
    """Every path git tracks, relative to the repository root."""
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    return [name for name in completed.stdout.decode("utf-8").split("\0") if name]


# `families/` written as a path, `"families"` as a path component built with
# Path joining, and the bare `.rebuild133` name. A word such as `store_families`
# (a column) is not a reference to either directory.
REFERENCE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("families/", re.compile(r"(?<![A-Za-z0-9_])families/")),
    ('"families"', re.compile(r"[\"']families[\"']")),
    (".rebuild133", re.compile(r"\.rebuild133")),
)


def retired_reference(path: str) -> str | None:
    """The retired-directory reference `path` contains, if it is not exempt."""
    if any(path.startswith(prefix) for prefix in EXEMPT_PREFIXES):
        return None
    if path in EXEMPT_GENERATED_VIEWS or EXEMPT_EVIDENCE_NAME.match(path):
        return None
    if EXEMPT_BUNDLE_RECORD.match(path):
        return None
    try:
        text = (REPO_ROOT / path).read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None
    return next((label for label, pattern in REFERENCE_PATTERNS if pattern.search(text)), None)


class TestRetiredDirectoriesAreGone(unittest.TestCase):
    """The directories are deleted, and nothing puts them back."""

    def test_no_tracked_file_lives_under_a_retired_directory(self) -> None:
        offenders = [
            path
            for path in tracked_files()
            if any(path == name or path.startswith(f"{name}/") for name in RETIRED_DIRECTORIES)
        ]
        self.assertEqual(offenders, [], "tracked files under a retired bundle directory")

    def test_the_retired_directories_are_absent_from_the_worktree(self) -> None:
        for name in RETIRED_DIRECTORIES:
            self.assertFalse((REPO_ROOT / name).exists(), f"{name}/ still exists")

    def test_no_code_config_or_test_reads_a_retired_directory(self) -> None:
        offenders = {
            path: reference
            for path in tracked_files()
            if (reference := retired_reference(path)) is not None
        }
        self.assertEqual(
            offenders,
            {},
            "code, config or tests still reference a retired bundle directory; "
            "only ADRs, historical documents and bundle records may",
        )


class TestMigratedBundlesAreRegistered(unittest.TestCase):
    """Each kept bundle has one home, its own id, and its historical evidence."""

    def load(self, store_id: str) -> bundle_mod.Bundle:
        return bundle_mod.load(store_id, registry_root=STORES)

    def test_each_kept_bundle_is_registered_under_its_own_id(self) -> None:
        for store_id, label, derived_from, status, layout, completion_state, _ in MIGRATED_BUNDLES:
            with self.subTest(store_id=store_id):
                loaded = self.load(store_id)
                self.assertEqual(loaded.release.get("store_id"), store_id)
                self.assertEqual(loaded.release.get("label"), label)
                self.assertEqual(loaded.release.get("status"), status)
                self.assertEqual(loaded.release.get("derived_from"), derived_from)
                self.assertEqual(loaded.build.get("store_id"), store_id)
                self.assertEqual(loaded.build.get("layout"), layout)
                self.assertEqual(loaded.build.get("completion_state"), completion_state)

    def test_each_kept_bundle_passes_the_release_bundle_contract(self) -> None:
        for store_id, *_ in MIGRATED_BUNDLES:
            with self.subTest(store_id=store_id):
                self.assertEqual(bundle_mod.check(self.load(store_id), registry_root=STORES), [])

    def test_each_kept_bundle_keeps_its_validation_record(self) -> None:
        for store_id, _, _, status, *_ in MIGRATED_BUNDLES:
            with self.subTest(store_id=store_id):
                validation = self.load(store_id).validation
                self.assertIsInstance(validation, dict)
                self.assertTrue(validation.get("validator"), "validator evidence kept")
                self.assertIn("checks", validation)
                self.assertIn(status, {"built", "validated"})

    def test_derived_from_resolves_and_carries_the_parent_membership(self) -> None:
        for store_id, _, derived_from, *_ in MIGRATED_BUNDLES:
            if derived_from is None:
                continue  # The Chen ancestry slices are siblings, never children.
            with self.subTest(store_id=store_id):
                parent = self.load(derived_from)
                self.assertEqual(parent.release.get("store_id"), derived_from)
                child_ids, _ = bundle_mod.membership_ids(self.load(store_id))
                parent_ids, _ = bundle_mod.membership_ids(parent)
                missing = [analysis_id for analysis_id in parent_ids if analysis_id not in set(child_ids)]
                self.assertEqual(missing, [], "child must carry every parent Analysis")
                self.assertGreaterEqual(len(child_ids), len(parent_ids))

    def test_each_kept_bundle_is_in_the_generated_master_list(self) -> None:
        stores_tsv = (REPO_ROOT / "stores.tsv").read_text(encoding="utf-8")
        stores_md = (REPO_ROOT / "STORES.md").read_text(encoding="utf-8")
        for store_id, label, *_ in MIGRATED_BUNDLES:
            with self.subTest(store_id=store_id):
                self.assertIn(f"\n{store_id}\t", stores_tsv)
                self.assertIn(f"`{store_id}`", stores_md)
                self.assertIn(label, stores_tsv)
                link = STORES / "by-label" / label
                self.assertTrue(link.is_symlink(), f"by-label/{label} missing")
                self.assertEqual(
                    (link.parent / link.readlink()).resolve(),
                    (STORES / store_id).resolve(),
                )

    def test_migrated_membership_tables_are_unchanged_in_row_count(self) -> None:
        for store_id, _, _, _, _, _, expected in MIGRATED_BUNDLES:
            with self.subTest(store_id=store_id):
                with open(STORES / store_id / "analyses.tsv", newline="", encoding="utf-8") as fh:
                    rows = list(csv.reader(fh, delimiter="\t", quoting=csv.QUOTE_NONE))
                self.assertEqual(len(rows) - 1, expected)


class TestRetiredBundlesAreGone(unittest.TestCase):
    """The triage's retire list is not silently re-registered."""

    RETIRED_LABELS: tuple[str, ...] = (
        "dense-observed-vcf-c128-completed-issue34",
        "2018-ieu",
        "dense-observed-vcf-c128-resolved",
        "dense-observed-vcf-c128-rebuild117",
        "dense-observed-vcf-pilot-10",
        "2023-chen-pilot-80",
        "2018-sun-pilot-100",
    )

    def test_no_registered_bundle_carries_a_retired_label(self) -> None:
        labels = set()
        for root in sorted(STORES.iterdir()):
            if not root.is_dir() or not (root / "release.yaml").is_file():
                continue
            release = yaml.safe_load((root / "release.yaml").read_text(encoding="utf-8"))
            labels.add((release or {}).get("label"))
        for label in self.RETIRED_LABELS:
            self.assertNotIn(label, labels, f"retired bundle {label} is still registered")


if __name__ == "__main__":
    unittest.main()

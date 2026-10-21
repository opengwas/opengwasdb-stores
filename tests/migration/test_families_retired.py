#!/usr/bin/env python3
"""One home for Release Bundles: the retired archive and leftover are gone (#252).

Issue #252 retired the two directories that kept Release Bundles beside
`stores/`: the top-level family-first archive in the pre-ADR-0022 schema, and
the leftover rebuild directory, which nothing read. What remains has to be
registered in `stores/` or gone, because as long as generators could keep
writing to a third place it kept coming back.

This suite pins that outcome, not the migration itself:

- no tracked file lives under either directory;
- no code, config or test reads either one, so nothing can start using them
  again;
- every bundle the confirmed triage kept is registered under its own id, keeps
  its historical Release Status and Validation Record, and passes the Release
  Bundle contract;
- the retired shapes are refused rather than merged into one attribution.

What may still name a retired path is listed, field by field, below: decision
records and dated documents, a bundle's recorded `generator.commands`, the
generated column that copies them, and the one provenance field that records a
file's move. Every other field of those same files is live and is checked.

Run from the repository root:

    python3 tests/migration/test_families_retired.py
"""

from __future__ import annotations

import csv
import json
import re
import subprocess
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path

import yaml

from ogstores import bundle as bundle_mod

REPO_ROOT = Path(__file__).resolve().parents[2]
STORES = REPO_ROOT / "stores"

# The guard must name what it forbids, so both retired directories are spelled
# from their parts: a literal here would trip the very scan it runs.
RETIRED_DIRECTORIES: tuple[str, ...] = (
    "famil" + "ies",
    "." + "rebuild" + "133",
)

# dated records: a decision or an audit describes the repository as it was, so
# it may name the retired paths it is about.
HISTORICAL_PREFIXES: tuple[str, ...] = (
    "docs/adr/",
    "docs/release-seam-audit.md",
    "docs/software-management-audit.md",
    "docs/workflow-engine-comparison.md",
    "docs/ukb-b-restored-inventory-2026-10-11.md",
)

# Historical *fields*, not whole files. A structured record may name a retired
# path only in the field that records the past; anything else in the same file
# is live configuration and fails the scan. Keys are dotted paths into the
# document, and a list value keeps its parent key (`generator.commands`).
HISTORICAL_DOCUMENT_FIELDS: dict[str, tuple[str, ...]] = {
    # A Release Bundle records what ran. `generator.commands` is a log, never
    # replayed from the recipe, and is not rewritten to match today's layout.
    r"\Astores/OGS-\d{5}/release\.yaml\Z": ("generator.commands",),
    # A frozen Source Inventory's `path_update` records that its declared
    # Selection moved and why, so both its prose and its `previous_path` name
    # the retired path. Every other field of the record is live: `path` points
    # at the file today and must resolve.
    r"\Aresources/inventories/[^/]+\.meta\.yaml\Z": (
        "declared_selection.path_update",
    ),
}

# The generated master list carries a bundle's recorded command verbatim, in
# that one column; its `store_uri`, `label` and every other column are live.
HISTORICAL_TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "stores.tsv": ("generator_command",),
}

# Bundles the confirmed triage kept (owner decision, #252), with the historical
# Release Status and evidence each must still carry. The FinnGen completed
# pilot is not among them: its parent OGS-00003 now holds 10 of the 20 Analyses
# it was completed from, so the edge would complete the wrong membership.
# (store_id, label, derived_from, status, layout, completion_state, n_analyses)
MIGRATED_BUNDLES: tuple[tuple[str, str, str | None, str, str, str, int], ...] = (
    ("OGS-00022", "eur-hybrid-pilot-10-completed", "OGS-00004", "validated", "hybrid", "reference_completed", 10),
    ("OGS-00023", "eur-hybrid-quant-pilot-10-completed", "OGS-00005", "validated", "hybrid", "reference_completed", 10),
    ("OGS-00024", "2023-chen-full-african", None, "built", "ragged", "observed_only", 1034),
    ("OGS-00025", "2023-chen-full-east-asian", None, "built", "ragged", "observed_only", 1038),
    ("OGS-00026", "2023-chen-full-south-asian", None, "built", "ragged", "observed_only", 971),
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


def reference_patterns() -> tuple[tuple[str, re.Pattern[str]], ...]:
    """`<name>/` as a path, a quoted `<name>` path component, and a dotted name.

    A bare word is only a reference for the dot-named leftover (the rebuild
    directory):
    `families` on its own is also the ordinary English word, and `store_families`
    is a column name, so only the path-shaped forms mean the directory.
    """
    patterns: list[tuple[str, re.Pattern[str]], ...] = []
    for name in RETIRED_DIRECTORIES:
        patterns.append((f"{name}/", re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}/")))
        patterns.append((f'"{name}"', re.compile(rf"[\"']{re.escape(name)}[\"']")))
        if name.startswith("."):
            patterns.append((name, re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])")))
    return tuple(patterns)


REFERENCE_PATTERNS = reference_patterns()


def _references(text: str) -> list[str]:
    """Which retired-directory references `text` contains."""
    return [label for label, pattern in REFERENCE_PATTERNS if pattern.search(text)]


def _dotted_strings(document: object, prefix: str = "") -> Iterator[tuple[str, str]]:
    """`(dotted key, string value)` for every string in a YAML/JSON document."""
    if isinstance(document, dict):
        for key, value in document.items():
            yield from _dotted_strings(value, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(document, list):
        for item in document:
            yield from _dotted_strings(item, prefix)
    elif isinstance(document, str):
        yield prefix, document


def _structured(text: str, path: str) -> object | None:
    """The parsed document for a YAML/JSON record, or `None` for anything else."""
    try:
        if path.endswith((".yaml", ".yml")):
            return yaml.safe_load(text)
        if path.endswith(".json"):
            return json.loads(text)
    except (yaml.YAMLError, json.JSONDecodeError):
        return None
    return None


def _table_references(path: str, text: str) -> list[str]:
    """Retired references in the columns a table does not record as history."""
    allowed = HISTORICAL_TABLE_COLUMNS[path]
    rows = list(csv.reader(text.splitlines(), delimiter="\t", quoting=csv.QUOTE_NONE))
    if not rows:
        return []
    header, body = rows[0], rows[1:]
    found: list[str] = []
    for row in body:
        for column, cell in zip(header, row):
            if column not in allowed:
                found.extend(_references(cell))
    return found


def retired_references(path: str, text: str) -> list[str]:
    """Retired-directory references in one tracked file's text.

    Empty means the file is clean: it names neither directory, or it names one
    only in a field that records the past.
    """
    if any(path.startswith(prefix) for prefix in HISTORICAL_PREFIXES):
        return []
    if path in HISTORICAL_TABLE_COLUMNS:
        return _table_references(path, text)

    for pattern, allowed_fields in HISTORICAL_DOCUMENT_FIELDS.items():
        if not re.match(pattern, path):
            continue
        document = _structured(text, path)
        if document is None:
            return [f"unreadable record: {_references(text)}"]
        found: list[str] = []
        for key, value in _dotted_strings(document):
            if any(key == field or key.startswith(f"{field}.") for field in allowed_fields):
                continue
            found.extend(_references(value))
        return found

    return _references(text)


def retired_reference(path: str) -> list[str]:
    """The retired-directory references `path` makes."""
    try:
        text = (REPO_ROOT / path).read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    return retired_references(path, text)


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
            path: references
            for path in tracked_files()
            if (references := retired_reference(path))
        }
        self.assertEqual(
            offenders,
            {},
            "code, config or tests still reference a retired bundle directory. "
            "Only the historical fields listed in this module may.",
        )


class TestGuardControls(unittest.TestCase):
    """The guard sees a retired path in a live field and not in a historical one."""

    META = "resources/inventories/ukb-b-restored-2026-10-11.meta.yaml"
    RELEASE = "stores/OGS-00022/release.yaml"
    archive = RETIRED_DIRECTORIES[0]

    def test_a_live_inventory_field_naming_a_retired_path_is_caught(self) -> None:
        text = (
            "snapshot_id: ukb-b-restored-2026-10-11\n"
            "declared_selection:\n"
            f"  path: {self.archive}/ukb-b/releases/c128/analyses.tsv\n"
            "  sha256: 0ae5c2d7\n"
        )
        self.assertEqual(retired_references(self.META, text), [f"{self.archive}/"])

    def test_the_inventory_move_record_is_history(self) -> None:
        text = (
            "declared_selection:\n"
            "  path: resources/inventories/x.declared-selection.tsv\n"
            "  path_update:\n"
            f"    previous_path: /old/{self.archive}/ukb-b/releases/c128/analyses.tsv\n"
            f"    reason: moved out of the retired {self.archive}/ archive\n"
        )
        self.assertEqual(retired_references(self.META, text), [])

    def test_a_live_release_field_naming_a_retired_path_is_caught(self) -> None:
        text = (
            "store_id: OGS-00022\n"
            "generator:\n"
            f"  commands:\n    - Rscript generate.R --config={self.archive}/x.yaml\n"
            "description: moved\n"
            "notes: |\n"
            f"  The build read {self.archive}/legacy/analyses.tsv.\n"
        )
        self.assertEqual(retired_references(self.RELEASE, text), [f"{self.archive}/"])

    def test_the_recorded_generator_log_is_history(self) -> None:
        text = (
            "store_id: OGS-00022\n"
            "generator:\n"
            f"  commands:\n    - Rscript generate.R --config={self.archive}/x.yaml\n"
            "notes: migrated from the retired family-first layout\n"
        )
        self.assertEqual(retired_references(self.RELEASE, text), [])

    def test_a_generated_table_is_checked_column_by_column(self) -> None:
        header = "store_id\tstore_uri\tgenerator_command\n"
        historical = header + f"OGS-00022\t/data/opengwasdb/stores/OGS-00022/store.opengwasdb\tRscript x --config={self.archive}/y.yaml\n"
        self.assertEqual(retired_references("stores.tsv", historical), [])
        live = header + f"OGS-00022\t/data/opengwasdb/{self.archive}/OGS-00022/store.opengwasdb\tRscript x\n"
        self.assertEqual(retired_references("stores.tsv", live), [f"{self.archive}/"])


class TestRetiredShapesAreRefused(unittest.TestCase):
    """A Manifest outside a registered bundle is refused, not merged."""

    def test_two_retired_shapes_are_refused_rather_than_merged(self) -> None:
        from curation import gap_scan

        archive, chen, sun = RETIRED_DIRECTORIES[0], "chen", "sun"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifests = []
            for name in (chen, sun):
                bundle_dir = root / archive / name / "releases" / "shared"
                bundle_dir.mkdir(parents=True)
                manifest = bundle_dir / "analyses.tsv"
                manifest.write_text(
                    "analysis_id\tsource_label\ttrait_ontology_mapping_method\n"
                    "1\tX\tunmapped\n",
                    encoding="utf-8",
                )
                manifests.append(manifest)

            # Each used to attribute to `shared`, silently merging two Store
            # Releases into one family. Both are refused by name instead.
            for manifest in manifests:
                with self.subTest(manifest=str(manifest)):
                    with self.assertRaises(gap_scan.UnsupportedManifestLocationError) as caught:
                        gap_scan.derive_store_family(manifest)
                    self.assertIn("shared", str(caught.exception))
            with self.assertRaises(gap_scan.UnsupportedManifestLocationError):
                gap_scan.scan_manifests(manifests)


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

    def test_each_migration_note_names_its_own_bundle(self) -> None:
        """A note rewritten after a renumbering must not keep the previous id.

        The five kept bundles were renumbered once (#252 fix round 1), and every
        migration note then still named the next id along -- a reader following
        it would look for a bundle that does not exist, or describe the wrong
        one.
        """
        for store_id, _, derived_from, *_ in MIGRATED_BUNDLES:
            with self.subTest(store_id=store_id):
                notes = self.load(store_id).release.get("notes") or ""
                self.assertIn("Migration (#252)", notes)
                paragraph = notes.split("Migration (#252)", 1)[1].split("\n\n")[0]
                migrated_to = re.search(r"layout to\s+(OGS-\d{5})", paragraph)
                self.assertIsNotNone(migrated_to, paragraph)
                self.assertEqual(migrated_to.group(1), store_id)
                named = set(re.findall(r"OGS-\d{5}", paragraph))
                self.assertLessEqual(named, {store_id, derived_from} - {None}, paragraph)

    def test_every_generator_path_a_note_names_exists(self) -> None:
        """A note may not point at a directory that has since been retired.

        `#300` removed `resources/generators/metabolome-plasma-2023/`; a
        migration note that still sent a reader there was describing a tree
        that no longer exists.
        """
        for store_id, *_ in MIGRATED_BUNDLES:
            notes = self.load(store_id).release.get("notes") or ""
            for path in re.findall(r"resources/generators/[\w./-]+", notes):
                with self.subTest(store_id=store_id, path=path):
                    self.assertTrue((REPO_ROOT / path.rstrip(".")).exists(), path)

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
        # Retired rather than migrated: its parent OGS-00003 now holds 10 of the
        # 20 Analyses it was completed from (owner decision, fix round 1).
        "r13-pilot-20-completed",
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

    def test_the_finngen_completed_pilot_is_not_a_bundle_anywhere(self) -> None:
        for root in sorted(STORES.iterdir()):
            if not root.is_dir() or not (root / "release.yaml").is_file():
                continue
            release = yaml.safe_load((root / "release.yaml").read_text(encoding="utf-8")) or {}
            self.assertNotEqual(release.get("derived_from"), "OGS-00003", root.name)


if __name__ == "__main__":
    unittest.main()

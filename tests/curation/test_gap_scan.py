#!/usr/bin/env python3
"""Tests for curation.gap_scan (issue #163).

The user-visible contract under test is that the gap scan turns committed
Release Manifests into a correct, prioritised unmapped Trait work queue, and
that it never writes to or modifies a Manifest. The queue is the input to
Canonical Trait Mapping Table curation, so a label that is mis-normalised,
under-counted, or attributed to the wrong Store Family would send curation at
the wrong target.

Verifies:
- only `trait_ontology_mapping_method == unmapped` rows are queued;
- labels differing only by case or surrounding whitespace collapse and their
  occurrence counts sum, matching the canonical-table `trimws(tolower(x))` rule;
- Store Families are attributed from the bundle path and from `release.yaml`,
  sorted and comma-separated;
- the queue is ordered descending by occurrence count with an alphabetical
  tie-break;
- a Manifest with no unmapped rows (and one without the mapping column) yields
  no queue entries and the CLI exits 0;
- the CLI writes to stdout or `--output`, resolves bundle directories, and
  fails loudly with exit 1 on a missing Manifest.
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from curation import gap_scan
from curation.gap_scan import (
    MalformedRowError,
    MissingManifestError,
    MissingTraitLabelColumnError,
    QueueEntry,
    derive_store_family,
    format_queue_tsv,
    normalize_trait_label,
    scan_manifest,
    scan_manifests,
)

BASE_COLUMNS = ["analysis_id", "source_label", "trait_ontology_mapping_method"]


def write_tsv(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    """Write a plain tab-separated table with the given column order."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["\t".join(columns)]
    for row in rows:
        lines.append("\t".join(row.get(col, "") for col in columns))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_bundle(
    root: Path,
    *relative_dir: str,
    rows: list[dict[str, str]],
    columns: list[str] | None = None,
    release: dict | None = None,
) -> Path:
    """Create a bundle directory with an analyses.tsv and optional release.yaml."""
    bundle_dir = root.joinpath(*relative_dir)
    write_tsv(bundle_dir / "analyses.tsv", columns or BASE_COLUMNS, rows)
    if release is not None:
        import yaml

        (bundle_dir / "release.yaml").write_text(
            yaml.safe_dump(release), encoding="utf-8"
        )
    return bundle_dir / "analyses.tsv"


class TestNormalizeTraitLabel(unittest.TestCase):
    """The normalisation is exactly the canonical-table lookup's rule."""

    def test_trims_and_lowercases(self) -> None:
        self.assertEqual(normalize_trait_label("  Body Mass Index  "), "body mass index")

    def test_none_and_empty_become_empty(self) -> None:
        self.assertEqual(normalize_trait_label(None), "")
        self.assertEqual(normalize_trait_label("   "), "")

    def test_case_and_whitespace_variants_share_a_key(self) -> None:
        variants = ["Height", "height", "  HEIGHT ", "\theight\t"]
        keys = {normalize_trait_label(v) for v in variants}
        self.assertEqual(keys, {"height"})


class TestScanManifest(unittest.TestCase):
    """Per-Manifest selection, normalisation, and Store Family attribution."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_only_unmapped_rows_are_queued(self) -> None:
        rows = [
            {
                "analysis_id": "A1",
                "source_label": "Mapped by source",
                "trait_ontology_mapping_method": "source_provided",
            },
            {
                "analysis_id": "A2",
                "source_label": "Mapped by table",
                "trait_ontology_mapping_method": "canonical_table_lookup",
            },
            {
                "analysis_id": "A3",
                "source_label": "Queued",
                "trait_ontology_mapping_method": "unmapped",
            },
        ]
        manifest = make_bundle(self.td, "family-a", "releases", "rel-1", rows=rows)
        result = scan_manifest(manifest)

        self.assertEqual(list(result), ["queued"])
        self.assertEqual(result["queued"][0], 1)
        self.assertEqual(result["queued"][1], {"family-a"})

    def test_labels_collapse_across_case_and_whitespace(self) -> None:
        rows = [
            {"source_label": "Body mass index", "trait_ontology_mapping_method": "unmapped"},
            {"source_label": "  body mass index", "trait_ontology_mapping_method": "unmapped"},
            {"source_label": "BODY MASS INDEX  ", "trait_ontology_mapping_method": "unmapped"},
            {"source_label": "Height", "trait_ontology_mapping_method": "unmapped"},
        ]
        manifest = make_bundle(self.td, "family-a", "releases", "rel-1", rows=rows)
        result = scan_manifest(manifest)

        self.assertEqual(result["body mass index"][0], 3)
        self.assertEqual(result["height"][0], 1)

    def test_unmapped_rows_with_blank_label_are_not_queued(self) -> None:
        rows = [
            {"source_label": "", "trait_ontology_mapping_method": "unmapped"},
            {"source_label": "   ", "trait_ontology_mapping_method": "unmapped"},
            {"source_label": "Real", "trait_ontology_mapping_method": "unmapped"},
        ]
        manifest = make_bundle(self.td, "family-a", "releases", "rel-1", rows=rows)
        result = scan_manifest(manifest)

        self.assertEqual(list(result), ["real"])

    def test_store_family_derived_from_releases_path(self) -> None:
        manifest = make_bundle(
            self.td, "gwas-catalog-eur-hybrid", "releases", "eur-hybrid-pilot-10",
            rows=[{"source_label": "X", "trait_ontology_mapping_method": "unmapped"}],
        )
        self.assertEqual(derive_store_family(manifest), "gwas-catalog-eur-hybrid")

    def test_store_family_prefers_release_yaml_metadata(self) -> None:
        manifest = make_bundle(
            self.td, "family-a", "releases", "rel-1",
            rows=[{"source_label": "X", "trait_ontology_mapping_method": "unmapped"}],
            release={"store_family_id": "declared-family"},
        )
        self.assertEqual(derive_store_family(manifest), "declared-family")

    def test_store_family_falls_back_to_bundle_directory(self) -> None:
        manifest = make_bundle(
            self.td, "OGS-00099",
            rows=[{"source_label": "X", "trait_ontology_mapping_method": "unmapped"}],
        )
        self.assertEqual(derive_store_family(manifest), "OGS-00099")

    def test_missing_mapping_column_is_skipped_not_an_error(self) -> None:
        manifest = make_bundle(
            self.td, "legacy",
            columns=["analysis_id", "source_label"],
            rows=[{"analysis_id": "A1", "source_label": "No mapping here"}],
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = scan_manifest(manifest)
        self.assertEqual(result, {})
        self.assertIn("no trait_ontology_mapping_method column", stderr.getvalue())

    def test_missing_trait_label_column_raises(self) -> None:
        manifest = make_bundle(
            self.td, "broken",
            columns=["analysis_id", "trait_ontology_mapping_method"],
            rows=[{"analysis_id": "A1", "trait_ontology_mapping_method": "unmapped"}],
        )
        with self.assertRaises(MissingTraitLabelColumnError):
            scan_manifest(manifest)

    def test_ragged_row_raises(self) -> None:
        manifest = self.td / "ragged" / "analyses.tsv"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(
            "analysis_id\tsource_label\ttrait_ontology_mapping_method\n"
            "A1\tShort\n",
            encoding="utf-8",
        )
        with self.assertRaises(MalformedRowError):
            scan_manifest(manifest)

    def test_missing_manifest_raises(self) -> None:
        with self.assertRaises(MissingManifestError):
            scan_manifest(self.td / "does-not-exist")

    def test_directory_without_analyses_tsv_raises(self) -> None:
        empty = self.td / "empty-bundle"
        empty.mkdir()
        with self.assertRaises(MissingManifestError):
            scan_manifest(empty)


class TestScanManifests(unittest.TestCase):
    """Cross-Manifest aggregation, ordering, and empty-queue handling."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_counts_sum_and_families_union_across_manifests(self) -> None:
        m1 = make_bundle(
            self.td, "family-a", "releases", "rel-1",
            rows=[
                {"source_label": "Shared trait", "trait_ontology_mapping_method": "unmapped"},
                {"source_label": "SHARED TRAIT ", "trait_ontology_mapping_method": "unmapped"},
                {"source_label": "Only A", "trait_ontology_mapping_method": "unmapped"},
            ],
        )
        m2 = make_bundle(
            self.td, "family-b", "releases", "rel-2",
            rows=[
                {"source_label": "shared trait", "trait_ontology_mapping_method": "unmapped"},
                {"source_label": "Only B", "trait_ontology_mapping_method": "unmapped"},
            ],
        )

        entries = scan_manifests([m1, m2])
        by_label = {e.trait_label: e for e in entries}

        self.assertEqual(by_label["shared trait"].occurrence_count, 3)
        self.assertEqual(by_label["shared trait"].store_families, ("family-a", "family-b"))
        self.assertEqual(by_label["only a"].occurrence_count, 1)
        self.assertEqual(by_label["only b"].occurrence_count, 1)

    def test_queue_sorted_descending_then_alphabetical(self) -> None:
        m1 = make_bundle(
            self.td, "family-a", "releases", "rel-1",
            rows=[
                {"source_label": "Alpha", "trait_ontology_mapping_method": "unmapped"},
                {"source_label": "Alpha", "trait_ontology_mapping_method": "unmapped"},
                {"source_label": "Beta", "trait_ontology_mapping_method": "unmapped"},
                {"source_label": "Zeta", "trait_ontology_mapping_method": "unmapped"},
                {"source_label": "Gamma", "trait_ontology_mapping_method": "unmapped"},
            ],
        )
        entries = scan_manifests([m1])
        self.assertEqual(
            [e.trait_label for e in entries],
            ["alpha", "beta", "gamma", "zeta"],
        )
        self.assertEqual(entries[0].occurrence_count, 2)

    def test_no_unmapped_rows_yields_empty_queue(self) -> None:
        manifest = make_bundle(
            self.td, "gwas-ssf-ragged", "releases", "rel",
            rows=[
                {"source_label": "Mapped", "trait_ontology_mapping_method": "source_provided"},
                {"source_label": "Looked up",
                 "trait_ontology_mapping_method": "canonical_table_lookup"},
            ],
        )
        self.assertEqual(scan_manifests([manifest]), [])

    def test_duplicate_labels_in_one_manifest_count_each_row(self) -> None:
        manifest = make_bundle(
            self.td, "family-a", "releases", "rel-1",
            rows=[
                {"source_label": "Same", "trait_ontology_mapping_method": "unmapped"},
                {"source_label": "same", "trait_ontology_mapping_method": "unmapped"},
            ],
        )
        entries = scan_manifests([manifest])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].occurrence_count, 2)

    def test_store_families_are_sorted(self) -> None:
        m1 = make_bundle(
            self.td, "zeta", "releases", "rel-1",
            rows=[{"source_label": "X", "trait_ontology_mapping_method": "unmapped"}],
        )
        m2 = make_bundle(
            self.td, "alpha", "releases", "rel-2",
            rows=[{"source_label": "X", "trait_ontology_mapping_method": "unmapped"}],
        )
        entries = scan_manifests([m1, m2])
        self.assertEqual(entries[0].store_families, ("alpha", "zeta"))


class TestFormatQueueTsv(unittest.TestCase):
    """The rendered TSV is machine-readable and always carries the header."""

    def test_header_only_for_empty_queue(self) -> None:
        self.assertEqual(
            format_queue_tsv([]),
            "trait_label\toccurrence_count\tstore_families\n",
        )

    def test_entry_row(self) -> None:
        entry = QueueEntry("body mass index", 4, ("family-a", "family-b"))
        self.assertEqual(
            format_queue_tsv([entry]),
            "trait_label\toccurrence_count\tstore_families\n"
            "body mass index\t4\tfamily-a,family-b\n",
        )


class TestCli(unittest.TestCase):
    """The command's stdout/--output behaviour and exit codes."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)
        self.manifest = make_bundle(
            self.td, "family-a", "releases", "rel-1",
            rows=[
                {"source_label": "Trait one", "trait_ontology_mapping_method": "unmapped"},
                {"source_label": "Trait one", "trait_ontology_mapping_method": "unmapped"},
                {"source_label": "Trait two", "trait_ontology_mapping_method": "unmapped"},
                {"source_label": "Mapped", "trait_ontology_mapping_method": "source_provided"},
            ],
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = gap_scan.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_writes_queue_to_stdout_and_exits_zero(self) -> None:
        code, out, err = self.run_cli([str(self.manifest)])
        self.assertEqual(code, 0)
        self.assertEqual(
            out,
            "trait_label\toccurrence_count\tstore_families\n"
            "trait one\t2\tfamily-a\n"
            "trait two\t1\tfamily-a\n",
        )
        self.assertEqual(err, "")

    def test_writes_queue_to_output_file_and_leaves_stdout_empty(self) -> None:
        output = self.td / "queue" / "unmapped.tsv"
        code, out, _ = self.run_cli([str(self.manifest), "--output", str(output)])
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertTrue(output.is_file())
        self.assertIn("trait one\t2\tfamily-a", output.read_text(encoding="utf-8"))

    def test_directory_argument_resolves_analyses_tsv(self) -> None:
        bundle_dir = self.manifest.parent
        code, out, _ = self.run_cli([str(bundle_dir)])
        self.assertEqual(code, 0)
        self.assertIn("trait one\t2", out)

    def test_empty_queue_is_success_with_header_only(self) -> None:
        clean = make_bundle(
            self.td, "gwas-ssf-ragged", "releases", "rel",
            rows=[{"source_label": "Mapped", "trait_ontology_mapping_method": "source_provided"}],
        )
        code, out, _ = self.run_cli([str(clean)])
        self.assertEqual(code, 0)
        self.assertEqual(out, "trait_label\toccurrence_count\tstore_families\n")

    def test_missing_manifest_exits_one(self) -> None:
        code, out, err = self.run_cli([str(self.td / "nope")])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("gap-scan: error:", err)

    def test_missing_required_column_exits_one(self) -> None:
        broken = self.td / "broken" / "analyses.tsv"
        write_tsv(
            broken,
            ["analysis_id", "trait_ontology_mapping_method"],
            [{"analysis_id": "A1", "trait_ontology_mapping_method": "unmapped"}],
        )
        code, out, err = self.run_cli([str(broken)])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("gap-scan: error:", err)


if __name__ == "__main__":
    unittest.main()

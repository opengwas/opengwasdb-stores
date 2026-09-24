#!/usr/bin/env python3
"""Tests for curation.harvest (issue #165).

The user-visible contract under test is that the harvest turns committed
Release Manifests into a correct ground-truth validation set of source-provided
``(trait_label -> ontology_id)`` pairs, each placed in a stratum and flagged
for obsolescence, without modifying a Manifest, a generator, or a bundle.

A wrong validation set is silently misleading: a pair that is not really
source-provided poisons the ground truth, a mis-stated stratum makes the
stratified recall report lie, and a retired term left unflagged is scored as a
retrieval failure that never was one.

Verifies:
- only `trait_ontology_mapping_method == source_provided` rows are harvested;
- pairs are unique on (trait_label, ontology_id, ontology_label, stratum) and
  their Store Families are unioned;
- stratum categorisation follows the documented precedence (MONDO/OBA prefixes,
  analyte families, measurement labels, disease families, else other);
- obsolete terms are flagged from the pinned index and from the source
  `obsolete_` label convention, never guessed from absence;
- the TSV carries the six documented columns and boolean strings;
- the CLI resolves files and bundle directories, writes to `--output` or
  stdout, and fails loudly on a missing Manifest.
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

from curation import harvest
from curation.gap_scan import MissingManifestError
from curation.harvest import (
    STRATUM_ANALYTE_MEASUREMENT,
    STRATUM_DISEASE,
    STRATUM_OTHER,
    HarvestEntry,
    categorize_stratum,
    detect_obsolete,
    format_harvest_tsv,
    harvest as harvest_pairs,
    scan_manifest,
)
from curation.ontology import build_index_from_obo

FIXTURE_OBO = """
format-version: 1.2
ontology: efo

[Term]
id: EFO:0004340
name: body mass index
def: "A measurement of body mass index." []
is_a: EFO:0004338 ! body weights and measures

[Term]
id: EFO:0004338
name: body weights and measures

[Term]
id: EFO:0004784
name: self reported educational attainment

[Term]
id: EFO:9999001
name: legacy obsolete trait
is_obsolete: true
replaced_by: EFO:0004340
"""

BASE_COLUMNS = [
    "analysis_id",
    "source_label",
    "trait_ontology_id",
    "trait_ontology_label",
    "trait_ontology_mapping_method",
]


def write_tsv(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["\t".join(columns)]
    lines.extend("\t".join(row.get(col, "") for col in columns) for row in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_bundle(
    root: Path,
    *relative_dir: str,
    rows: list[dict[str, str]],
    columns: list[str] | None = None,
) -> Path:
    bundle_dir = root.joinpath(*relative_dir)
    write_tsv(bundle_dir / "analyses.tsv", columns or BASE_COLUMNS, rows)
    return bundle_dir / "analyses.tsv"


def _fixture_index():
    """Build the hermetic fixture index from an in-memory OBO document."""
    import tempfile as _tempfile

    tmp = _tempfile.NamedTemporaryFile("w", suffix=".obo", delete=False, encoding="utf-8")
    try:
        tmp.write(FIXTURE_OBO)
        tmp.close()
        return build_index_from_obo(Path(tmp.name), "efo/vfixture")
    finally:
        Path(tmp.name).unlink(missing_ok=True)


def parse_tsv(text: str) -> tuple[list[str], list[dict[str, str]]]:
    lines = text.splitlines()
    header = lines[0].split("\t")
    rows = [dict(zip(header, line.split("\t"))) for line in lines[1:] if line]
    return header, rows


class TestCategorizeStratum(unittest.TestCase):
    """The documented stratum precedence."""

    def test_mondo_is_disease(self) -> None:
        self.assertEqual(categorize_stratum("MONDO:0005301", ("anything",)), STRATUM_DISEASE)

    def test_oba_is_analyte_measurement(self) -> None:
        self.assertEqual(
            categorize_stratum("OBA:2050131", ("gwas-catalog-eur-hybrid",)),
            STRATUM_ANALYTE_MEASUREMENT,
        )

    def test_analyte_family_is_measurement(self) -> None:
        self.assertEqual(
            categorize_stratum("EFO:0021984", ("pqtl-interval-2018",)),
            STRATUM_ANALYTE_MEASUREMENT,
        )
        self.assertEqual(
            categorize_stratum("EFO:0010469", ("metabolome-plasma-2023",)),
            STRATUM_ANALYTE_MEASUREMENT,
        )

    def test_measurement_label_without_family_is_measurement(self) -> None:
        self.assertEqual(
            categorize_stratum("EFO:0007793", ("unknown",), ontology_label="leptin measurement"),
            STRATUM_ANALYTE_MEASUREMENT,
        )

    def test_disease_family_efo_trait_is_disease(self) -> None:
        self.assertEqual(
            categorize_stratum("EFO:0000768", ("gwas-catalog-eur-hybrid",), ontology_label="idiopathic pulmonary fibrosis"),
            STRATUM_DISEASE,
        )

    def test_unclassifiable_is_other(self) -> None:
        self.assertEqual(
            categorize_stratum("EFO:0004784", ("unknown",), ontology_label="self reported educational attainment"),
            STRATUM_OTHER,
        )


class TestDetectObsolete(unittest.TestCase):
    """Obsolete detection uses the pinned index, then the label convention."""

    def setUp(self) -> None:
        self.index_by_id = _fixture_index().by_id()

    def test_index_obsolete_flag_is_used(self) -> None:
        self.assertTrue(detect_obsolete("EFO:9999001", "legacy obsolete trait", self.index_by_id))

    def test_index_live_term_is_not_obsolete(self) -> None:
        self.assertFalse(detect_obsolete("EFO:0004340", "body mass index", self.index_by_id))

    def test_absent_term_with_obsolete_label_is_flagged(self) -> None:
        self.assertTrue(
            detect_obsolete("EFO:0021984", "obsolete_[PDK1] measurement", self.index_by_id)
        )

    def test_absent_term_with_normal_label_is_not_flagged(self) -> None:
        self.assertFalse(detect_obsolete("EFO:0802230", "PDK2 measurement", self.index_by_id))

    def test_no_index_still_uses_label_convention(self) -> None:
        self.assertTrue(detect_obsolete("EFO:1", "obsolete_thing", None))
        self.assertFalse(detect_obsolete("EFO:2", "live thing", None))


class TestScanManifest(unittest.TestCase):
    """Per-Manifest selection and field extraction."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_only_source_provided_rows_are_harvested(self) -> None:
        rows = [
            {"source_label": "Mapped by source", "trait_ontology_id": "EFO:1",
             "trait_ontology_label": "one", "trait_ontology_mapping_method": "source_provided"},
            {"source_label": "Mapped by table", "trait_ontology_id": "EFO:2",
             "trait_ontology_label": "two", "trait_ontology_mapping_method": "canonical_table_lookup"},
            {"source_label": "Unmapped", "trait_ontology_id": "",
             "trait_ontology_label": "", "trait_ontology_mapping_method": "unmapped"},
        ]
        manifest = make_bundle(self.td, "metabolome-plasma-2023", "releases", "rel", rows=rows)
        entries = scan_manifest(manifest)
        self.assertEqual([entry.trait_label for entry in entries], ["Mapped by source"])
        self.assertEqual(entries[0].ontology_id, "EFO:1")
        self.assertEqual(entries[0].store_families, ("metabolome-plasma-2023",))

    def test_source_provided_row_without_id_is_skipped(self) -> None:
        rows = [
            {"source_label": "No id", "trait_ontology_id": "",
             "trait_ontology_mapping_method": "source_provided"},
            {"source_label": "Has id", "trait_ontology_id": "EFO:1",
             "trait_ontology_label": "one", "trait_ontology_mapping_method": "source_provided"},
        ]
        manifest = make_bundle(self.td, "family", "releases", "rel", rows=rows)
        entries = scan_manifest(manifest)
        self.assertEqual([entry.trait_label for entry in entries], ["Has id"])

    def test_missing_mapping_column_is_skipped_with_warning(self) -> None:
        manifest = make_bundle(
            self.td, "legacy",
            columns=["analysis_id", "source_label", "trait_ontology_id"],
            rows=[{"analysis_id": "A1", "source_label": "X", "trait_ontology_id": "EFO:1"}],
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            entries = scan_manifest(manifest)
        self.assertEqual(entries, [])
        self.assertIn("no trait_ontology_mapping_method column", stderr.getvalue())

    def test_missing_ontology_id_column_is_skipped_with_warning(self) -> None:
        manifest = make_bundle(
            self.td, "legacy",
            columns=["analysis_id", "source_label", "trait_ontology_mapping_method"],
            rows=[{"analysis_id": "A1", "source_label": "X",
                   "trait_ontology_mapping_method": "source_provided"}],
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            entries = scan_manifest(manifest)
        self.assertEqual(entries, [])
        self.assertIn("no trait_ontology_id column", stderr.getvalue())

    def test_missing_manifest_raises(self) -> None:
        with self.assertRaises(MissingManifestError):
            scan_manifest(self.td / "nope")


class TestHarvest(unittest.TestCase):
    """Cross-Manifest uniqueness, family union, and ordering."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_pairs_are_unique_and_families_union(self) -> None:
        m1 = make_bundle(
            self.td, "metabolome-plasma-2023", "releases", "rel-1",
            rows=[
                {"source_label": "Carnitine levels", "trait_ontology_id": "EFO:0010469",
                 "trait_ontology_label": "carnitine measurement",
                 "trait_ontology_mapping_method": "source_provided"},
                {"source_label": "Carnitine levels", "trait_ontology_id": "EFO:0010469",
                 "trait_ontology_label": "carnitine measurement",
                 "trait_ontology_mapping_method": "source_provided"},
            ],
        )
        m2 = make_bundle(
            self.td, "pqtl-interval-2018", "releases", "rel-2",
            rows=[
                {"source_label": "Carnitine levels", "trait_ontology_id": "EFO:0010469",
                 "trait_ontology_label": "carnitine measurement",
                 "trait_ontology_mapping_method": "source_provided"},
            ],
        )
        entries = harvest_pairs([m1, m2])
        self.assertEqual(len(entries), 1)
        self.assertEqual(
            entries[0].store_families, ("metabolome-plasma-2023", "pqtl-interval-2018")
        )

    def test_same_label_different_id_is_two_pairs(self) -> None:
        manifest = make_bundle(
            self.td, "gwas-catalog-eur-hybrid", "releases", "rel",
            rows=[
                {"source_label": "Parkinson's disease", "trait_ontology_id": "MONDO:0005180",
                 "trait_ontology_label": "Parkinson disease",
                 "trait_ontology_mapping_method": "source_provided"},
                {"source_label": "Parkinson's disease", "trait_ontology_id": "EFO:0002508",
                 "trait_ontology_label": "Parkinson disease",
                 "trait_ontology_mapping_method": "source_provided"},
            ],
        )
        entries = harvest_pairs([manifest])
        self.assertEqual({entry.ontology_id for entry in entries}, {"MONDO:0005180", "EFO:0002508"})

    def test_obsolete_flag_is_or_across_occurrences(self) -> None:
        manifest = make_bundle(
            self.td, "pqtl-interval-2018", "releases", "rel",
            rows=[
                {"source_label": "Legacy trait", "trait_ontology_id": "EFO:9999001",
                 "trait_ontology_label": "legacy obsolete trait",
                 "trait_ontology_mapping_method": "source_provided"},
                {"source_label": "Legacy trait", "trait_ontology_id": "EFO:9999001",
                 "trait_ontology_label": "legacy obsolete trait",
                 "trait_ontology_mapping_method": "source_provided"},
            ],
        )
        index = _fixture_index()
        entries = harvest_pairs([manifest], index)
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].is_obsolete)

    def test_ordered_by_stratum_then_label(self) -> None:
        hybrid = make_bundle(
            self.td, "gwas-catalog-eur-hybrid", "releases", "rel",
            rows=[
                {"source_label": "Zeta disease", "trait_ontology_id": "MONDO:1",
                 "trait_ontology_label": "zeta", "trait_ontology_mapping_method": "source_provided"},
                {"source_label": "Alpha measurement", "trait_ontology_id": "OBA:1",
                 "trait_ontology_label": "alpha", "trait_ontology_mapping_method": "source_provided"},
            ],
        )
        other = make_bundle(
            self.td, "misc-family", "releases", "rel",
            rows=[
                {"source_label": "Other trait", "trait_ontology_id": "EFO:9",
                 "trait_ontology_label": "other", "trait_ontology_mapping_method": "source_provided"},
            ],
        )
        entries = harvest_pairs([hybrid, other])
        self.assertEqual(
            [entry.stratum for entry in entries],
            [STRATUM_ANALYTE_MEASUREMENT, STRATUM_DISEASE, STRATUM_OTHER],
        )


class TestFormatHarvestTsv(unittest.TestCase):
    """The rendered table carries the documented columns and booleans."""

    def test_header_only(self) -> None:
        self.assertEqual(format_harvest_tsv([]), "\t".join(harvest.OUTPUT_COLUMNS) + "\n")

    def test_row_rendering(self) -> None:
        entry = HarvestEntry(
            trait_label="Carnitine levels",
            ontology_id="EFO:0010469",
            ontology_label="carnitine measurement",
            stratum=STRATUM_ANALYTE_MEASUREMENT,
            store_families=("metabolome-plasma-2023",),
            is_obsolete=False,
        )
        header, rows = parse_tsv(format_harvest_tsv([entry]))
        self.assertEqual(header, list(harvest.OUTPUT_COLUMNS))
        self.assertEqual(rows[0]["trait_label"], "Carnitine levels")
        self.assertEqual(rows[0]["stratum"], STRATUM_ANALYTE_MEASUREMENT)
        self.assertEqual(rows[0]["store_families"], "metabolome-plasma-2023")
        self.assertEqual(rows[0]["is_obsolete"], "false")


class TestCli(unittest.TestCase):
    """The command resolves manifests, flags obsolete terms, and writes a table."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)
        self.obo_path = self.td / "fixture.obo"
        self.obo_path.write_text(FIXTURE_OBO, encoding="utf-8")
        self.index_path = self.td / "index.json"
        from curation.ontology import write_index

        write_index(build_index_from_obo(self.obo_path, "efo/vfixture"), self.index_path)
        self.manifest = make_bundle(
            self.td, "metabolome-plasma-2023", "releases", "rel",
            rows=[
                {"source_label": "Carnitine levels", "trait_ontology_id": "EFO:0010469",
                 "trait_ontology_label": "carnitine measurement",
                 "trait_ontology_mapping_method": "source_provided"},
                {"source_label": "Legacy trait", "trait_ontology_id": "EFO:9999001",
                 "trait_ontology_label": "legacy obsolete trait",
                 "trait_ontology_mapping_method": "source_provided"},
            ],
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = harvest.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_writes_to_output_and_flags_obsolete(self) -> None:
        output = self.td / "out" / "validation.tsv"
        code, out, err = self.run_cli(
            [str(self.manifest), "--index", str(self.index_path), "--output", str(output)]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "")
        header, rows = parse_tsv(output.read_text(encoding="utf-8"))
        self.assertEqual(header, list(harvest.OUTPUT_COLUMNS))
        by_id = {row["ontology_id"]: row for row in rows}
        self.assertEqual(by_id["EFO:9999001"]["is_obsolete"], "true")
        self.assertEqual(by_id["EFO:0010469"]["is_obsolete"], "false")

    def test_accepts_bundle_directory_and_writes_stdout(self) -> None:
        code, out, err = self.run_cli([str(self.manifest.parent)])
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("\t".join(harvest.OUTPUT_COLUMNS)))

    def test_missing_manifest_exits_one(self) -> None:
        code, out, err = self.run_cli([str(self.td / "nope")])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("harvest: error:", err)


if __name__ == "__main__":
    unittest.main()

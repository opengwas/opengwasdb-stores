#!/usr/bin/env python3
"""The FinnGen R13 metadata adapter: provider manifest -> Source Metadata Table (#288).

The provider manifest is five rows of the real ``finngen_R13_manifest.tsv``
shape (``phenocode``, ``phenotype``, ``category``, ``num_cases``,
``num_controls``, ``path_https``), chosen for the endpoint classes the R
resolver distinguished: the inverse-rank-normalised quantitative endpoints
(``category`` is ``Quantitative endpoints``, whose ``num_cases`` is the total N
and whose ``num_controls`` is the source's ``0``), ordinary case-control
endpoints, and the two rows that contradict themselves and must be refused
rather than dropped.

Run from the repository root:
    python3 tests/finngen-r13-pipeline/test_source_metadata_adapter.py
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import csv

from manifest_generation.providers.finngen.source_metadata_adapter import (
    AdapterError,
    convert_manifest,
)
from manifest_generation.source_metadata_table import (
    SourceMetadataTableError,
    load_source_metadata_table,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "finngen_R13_manifest.tsv"

COLUMNS = (
    "source_analysis_id",
    "analysis_id",
    "analysis_group_id",
    "source_trait_label",
    "source_trait_id",
    "source_ancestry_label",
    "source_superpopulation",
    "study_design",
    "sample_size",
    "n_cases",
    "n_controls",
)


class FinnGenSourceMetadataAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def convert(self, source: Path = FIXTURE):
        out = self.tmp / "source-metadata.tsv"
        convert_manifest(source, out)
        return load_source_metadata_table(out)

    def manifest(self, rows: list[dict[str, str]]) -> Path:
        """Write a provider manifest with ``rows`` over the fixture's columns."""
        path = self.tmp / "manifest.tsv"
        columns = ["phenocode", "phenotype", "category", "num_cases", "num_controls",
                   "path_bucket", "path_https"]
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, delimiter="\t", lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        return path

    def test_fixture_exercises_its_endpoint_classes(self) -> None:
        text = FIXTURE.read_text(encoding="utf-8")
        self.assertEqual(text.count("Quantitative endpoints"), 3)
        self.assertIn("Quantitative endpoints \t", text)  # an upstream padded category
        self.assertIn("Arthropathies \t", text)  # an upstream padded trait label

    def test_every_endpoint_becomes_one_canonical_row_in_manifest_order(self) -> None:
        table = self.convert()
        self.assertEqual(
            table.analysis_ids,
            ("BMI_IRN", "HEIGHT_IRN", "AB1_ACTINOMYCOSIS", "M13_ARTHROPATHIES", "Q_RNS_CTRL"),
        )
        self.assertNotIn("phenocode", table.columns)

    def test_a_quantitative_endpoint_carries_its_total_never_case_counts(self) -> None:
        row = self.convert().row("BMI_IRN")
        self.assertEqual(
            {column: row[column] for column in COLUMNS},
            {
                "source_analysis_id": "BMI_IRN",
                "analysis_id": "finngen-r13-BMI_IRN",
                "analysis_group_id": "Quantitative endpoints",
                "source_trait_label": "Body mass index, inverse-rank normalized",
                "source_trait_id": "BMI_IRN",
                "source_ancestry_label": "Finnish",
                "source_superpopulation": "EUR",
                "study_design": "quantitative",
                "sample_size": "362216",
                "n_cases": "",
                "n_controls": "",
            },
        )

    def test_a_case_control_endpoint_carries_both_counts_and_their_sum(self) -> None:
        row = self.convert().row("AB1_ACTINOMYCOSIS")
        self.assertEqual(
            {column: row[column] for column in COLUMNS},
            {
                "source_analysis_id": "AB1_ACTINOMYCOSIS",
                "analysis_id": "finngen-r13-AB1_ACTINOMYCOSIS",
                "analysis_group_id": "I Certain infectious and parasitic diseases (AB1_)",
                "source_trait_label": "Actinomycosis",
                "source_trait_id": "AB1_ACTINOMYCOSIS",
                "source_ancestry_label": "Finnish",
                "source_superpopulation": "EUR",
                "study_design": "case-control",
                "sample_size": "433889",
                "n_cases": "132",
                "n_controls": "433757",
            },
        )

    def test_a_quantitative_endpoint_ignores_upstream_controls_and_padded_category(self) -> None:
        table = self.convert()
        for analysis_id in ("HEIGHT_IRN", "Q_RNS_CTRL"):
            with self.subTest(analysis_id=analysis_id):
                row = table.row(analysis_id)
                self.assertEqual(row["study_design"], "quantitative")
                self.assertEqual(row["n_cases"], "")
                self.assertEqual(row["n_controls"], "")

    def test_padded_text_is_trimmed(self) -> None:
        row = self.convert().row("M13_ARTHROPATHIES")
        self.assertEqual(row["source_trait_label"], "Arthropathies")

    def test_a_quantitative_endpoint_without_a_case_total_is_refused(self) -> None:
        path = self.manifest([{
            "phenocode": "BMI_IRN", "phenotype": "Body mass index",
            "category": "Quantitative endpoints", "num_cases": "0", "num_controls": "0",
            "path_bucket": "gs://bucket/x.gz", "path_https": "https://example.invalid/x.gz",
        }])
        with self.assertRaises(AdapterError) as raised:
            self.convert(path)
        self.assertIn("BMI_IRN", str(raised.exception))
        self.assertFalse((self.tmp / "source-metadata.tsv").exists())

    def test_a_case_control_endpoint_without_positive_counts_is_refused(self) -> None:
        for num_cases, num_controls in (("100", "0"), ("0", "100")):
            with self.subTest(num_cases=num_cases, num_controls=num_controls):
                path = self.manifest([{
                    "phenocode": "M13_ARTHROPATHIES", "phenotype": "Arthropathies",
                    "category": "XIII Diseases", "num_cases": num_cases,
                    "num_controls": num_controls,
                    "path_bucket": "gs://bucket/x.gz", "path_https": "https://example.invalid/x.gz",
                }])
                with self.assertRaises(AdapterError) as raised:
                    self.convert(path)
                self.assertIn("M13_ARTHROPATHIES", str(raised.exception))

    def test_a_manifest_missing_a_column_is_refused_by_name(self) -> None:
        path = self.tmp / "manifest.tsv"
        path.write_text("phenocode\tphenotype\tcategory\nBMI_IRN\tBody mass index\tQuantitative endpoints\n",
                        encoding="utf-8")
        with self.assertRaises(AdapterError) as raised:
            self.convert(path)
        self.assertIn("num_cases", str(raised.exception))

    def test_the_written_table_is_what_the_canonical_loader_accepts(self) -> None:
        """The gate is the real loader, and it is a gate.

        The written table loads with exactly the canonical columns the adapter
        writes, and a copy whose ``study_design`` leaves the vocabulary is
        refused by that same loader -- so the load is not the file agreeing with
        itself.
        """
        out = self.tmp / "source-metadata.tsv"
        convert_manifest(FIXTURE, out)
        self.assertEqual(
            load_source_metadata_table(out).columns,
            (
                "source_analysis_id", "source_trait_label", "source_trait_id",
                "source_ancestry_label", "source_superpopulation", "study_design",
                "sample_size", "n_cases", "n_controls",
                "analysis_id", "analysis_group_id",
            ),
        )

        corrupted = self.tmp / "corrupted.tsv"
        corrupted.write_text(
            out.read_text(encoding="utf-8").replace("\tcase-control\t", "\tbinary\t", 1),
            encoding="utf-8",
        )
        with self.assertRaises(SourceMetadataTableError) as raised:
            load_source_metadata_table(corrupted)
        self.assertIn("study_design", str(raised.exception))

    def test_an_untranslated_ancestry_label_is_refused_rather_than_left_blank(self) -> None:
        """`Finnish` is in the tracked map; a map without it must refuse, not blank."""
        resource = (self.tmp / "resources" / "reference-resources" / "ukb-ancestry-mixture-hg38")
        resource.mkdir(parents=True)
        (resource / "source_label_map.tsv").write_text(
            "source_label\tsuper_population\nEuropean\tEUR\n", encoding="utf-8"
        )
        (resource / "source_label_imprecise.tsv").write_text(
            "source_label\treason\n", encoding="utf-8"
        )
        with self.assertRaises(AdapterError) as raised:
            convert_manifest(FIXTURE, self.tmp / "source-metadata.tsv", repo_root=self.tmp)
        self.assertIn("Finnish", str(raised.exception))
        self.assertIn("map it or list it as imprecise", str(raised.exception))
        self.assertFalse((self.tmp / "source-metadata.tsv").exists())

    def test_every_row_carries_the_namespaced_store_id_and_its_category(self) -> None:
        """The Store's identity is FinnGen's: ``finngen-r13-<phenocode>``, grouped
        by the manifest's own ``category`` (#288). Both are literal here because
        OGS-00017's committed bundle names exactly these values."""
        table = self.convert()
        self.assertEqual(
            [(row["analysis_id"], row["analysis_group_id"]) for row in table.rows],
            [
                ("finngen-r13-BMI_IRN", "Quantitative endpoints"),
                ("finngen-r13-HEIGHT_IRN", "Quantitative endpoints"),
                ("finngen-r13-AB1_ACTINOMYCOSIS",
                 "I Certain infectious and parasitic diseases (AB1_)"),
                ("finngen-r13-M13_ARTHROPATHIES",
                 "XIII Diseases of the musculoskeletal system and connective tissue (M13_)"),
                ("finngen-r13-Q_RNS_CTRL", "Quantitative endpoints"),
            ],
        )

    def test_a_declared_id_that_collides_with_a_source_id_is_refused(self) -> None:
        """Uniqueness is the *effective* Store id's, not the declared one's.

        The first row declares ``SRC_B``; the second declares nothing, so its
        Store id falls back to its own source id ``SRC_B``. Both would render as
        ``SRC_B``, which ``bundle.check()`` refuses (#288).
        """
        out = self.tmp / "source-metadata.tsv"
        out.write_text(
            "source_analysis_id\tanalysis_id\tanalysis_group_id\tstudy_design\n"
            "SRC_A\tSRC_B\tGroup\tcase-control\n"
            "SRC_B\t\tGroup\tcase-control\n",
            encoding="utf-8",
        )
        with self.assertRaises(SourceMetadataTableError) as raised:
            load_source_metadata_table(out)
        self.assertIn("SRC_B", str(raised.exception))

    def test_a_repeated_store_analysis_id_is_refused(self) -> None:
        """The Store's ids must be unique even when two source ids differ."""
        out = self.tmp / "source-metadata.tsv"
        out.write_text(
            "source_analysis_id\tanalysis_id\tanalysis_group_id\tstudy_design\n"
            "AB1_X\tfinngen-r13-SAME\tGroup\tcase-control\n"
            "AB1_Y\tfinngen-r13-SAME\tGroup\tcase-control\n",
            encoding="utf-8",
        )
        with self.assertRaises(SourceMetadataTableError) as raised:
            load_source_metadata_table(out)
        self.assertIn("finngen-r13-SAME", str(raised.exception))


if __name__ == "__main__":
    unittest.main()

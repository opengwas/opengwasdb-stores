#!/usr/bin/env python3
"""The ukb-b Source Metadata Table adapter: declared selection + provider JSONs (#289).

The seam is the canonical table: a fixture of the declared selection and the
provider JSON sidecars beside the VCFs gives the rows the shared freeze reads,
as literal values. The expected values are the provider JSONs' own (a
case-control trait with its counts, a quantitative trait with its total, the
provider's ``European`` label and its ``EUR`` translation), never the adapter's
arithmetic.

Run from the repository root:
    python3 tests/ukb-b-pipeline/test_source_metadata_adapter.py
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from manifest_generation.providers.ukb_b import source_metadata_adapter as adapter
from manifest_generation.source_inventory import InventoryError
from manifest_generation.source_metadata_table import load_source_metadata_table

from ukb_b_fixture import ANALYSES, build_fixture, write_tsv

EXPECTED_ROWS = [
    {
        "source_analysis_id": "ukb-b-10001",
        "source_trait_label": "Operative procedures - secondary OPCS: Z84.6 Knee joint",
        "source_trait_id": "ukb-b-10001",
        "source_ancestry_label": "European",
        "source_superpopulation": "EUR",
        "study_design": "case-control",
        "sample_size": "463010",
        "n_cases": "6661",
        "n_controls": "456349",
    },
    {
        "source_analysis_id": "ukb-b-10054",
        "source_trait_label": "Cheese consumers",
        "source_trait_id": "ukb-b-10054",
        "source_ancestry_label": "European",
        "source_superpopulation": "EUR",
        "study_design": "case-control",
        "sample_size": "64949",
        "n_cases": "29408",
        "n_controls": "35541",
    },
    {
        "source_analysis_id": "ukb-b-10787",
        "source_trait_label": "Standing height",
        "source_trait_id": "ukb-b-10787",
        "source_ancestry_label": "European",
        "source_superpopulation": "EUR",
        "study_design": "quantitative",
        "sample_size": "461950",
        "n_cases": "",
        "n_controls": "",
    },
]


class AdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ukb-b-adapter-")
        self.root = Path(self._tmp.name)
        self.fixture = build_fixture(self.root)
        self.out = self.root / "table.tsv"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def convert(self) -> None:
        adapter.convert(
            self.fixture.declared_selection, self.fixture.source_dir, self.out
        )

    # -- the seam ---------------------------------------------------------

    def test_the_table_holds_the_provider_s_own_values(self) -> None:
        self.convert()
        table = load_source_metadata_table(self.out)
        self.assertEqual([dict(row) for row in table.rows], EXPECTED_ROWS)

    def test_a_quantitative_trait_carries_no_case_counts(self) -> None:
        # The provider declares no usable ncase/ncontrol pair for it, and a zero
        # where counts do not apply is the absence-vs-zero confusion the bundle
        # contract refuses (#133).
        self.convert()
        rows = {row["source_analysis_id"]: row for row in load_source_metadata_table(self.out).rows}
        self.assertEqual(rows["ukb-b-10787"]["n_cases"], "")
        self.assertEqual(rows["ukb-b-10787"]["n_controls"], "")

    def test_the_adapter_writes_no_effect_scale_and_no_collection_wide_fact(self) -> None:
        # The release config's method tiers and defaults own these (#287).
        self.convert()
        header = self.out.read_text(encoding="utf-8").splitlines()[0].split("\t")
        for column in (
            "declared_effect_scale",
            "stored_effect_scale",
            "original_sd_method",
            "sample_size_kind",
            "source_genome_build",
            "license",
            "consortium",
            "first_author",
            "analysis_group_id",
        ):
            self.assertNotIn(column, header)

    def test_the_command_line_writes_the_same_table(self) -> None:
        self.assertEqual(
            adapter.main(
                [
                    "--declared-selection",
                    str(self.fixture.declared_selection),
                    "--source-dir",
                    str(self.fixture.source_dir),
                    "--out",
                    str(self.out),
                ]
            ),
            0,
        )
        self.assertEqual(
            [dict(row) for row in load_source_metadata_table(self.out).rows], EXPECTED_ROWS
        )

    # -- refusals ---------------------------------------------------------

    def assertRefused(self, *fragments: str) -> None:
        with self.assertRaises(InventoryError) as caught:
            self.convert()
        for fragment in fragments:
            self.assertIn(fragment, str(caught.exception))

    def test_a_declared_analysis_with_no_provider_json_is_refused_by_name(self) -> None:
        (self.fixture.source_dir / "ukb-b-10054.json").unlink()
        self.assertRefused("ukb-b-10054", "provider JSON not found")

    def test_a_provider_json_for_another_id_is_refused(self) -> None:
        path = self.fixture.source_dir / "ukb-b-10054.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["id"] = "UKB-b-99999"
        path.write_text(json.dumps(document), encoding="utf-8")
        self.assertRefused("ukb-b-10054", "does not match the frozen analysis id")

    def test_a_provider_json_for_another_batch_is_refused(self) -> None:
        path = self.fixture.source_dir / "ukb-b-10054.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["batch"] = "ieu-a"
        path.write_text(json.dumps(document), encoding="utf-8")
        self.assertRefused("ukb-b-10054", "batch")

    def test_a_provider_json_with_no_sample_size_is_refused(self) -> None:
        path = self.fixture.source_dir / "ukb-b-10054.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document.update({"sample_size": None, "ncase": "NA", "ncontrol": "NA"})
        path.write_text(json.dumps(document), encoding="utf-8")
        self.assertRefused("ukb-b-10054", "resolves no study design or sample size")

    def test_an_untranslatable_source_ancestry_label_is_refused_by_name(self) -> None:
        path = self.fixture.source_dir / "ukb-b-10054.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["population"] = "Martian"
        path.write_text(json.dumps(document), encoding="utf-8")
        self.assertRefused("ukb-b-10054", "Martian")

    def test_a_declared_label_that_disagrees_with_the_provider_is_refused(self) -> None:
        rows = [
            {"analysis_id": analysis_id, "source_analysis_id": analysis_id, "source_label": "Other"}
            for analysis_id, _document, _body in ANALYSES
        ]
        write_tsv(self.fixture.declared_selection, adapter.ADAPTER_INPUT_COLUMNS, rows)
        self.assertRefused("ukb-b-10001", "declared Source Trait Label")

    def test_a_duplicate_declared_analysis_is_refused(self) -> None:
        rows = [
            {"analysis_id": analysis_id, "source_analysis_id": analysis_id, "source_label": trait}
            for analysis_id, document, _body in ANALYSES
            for trait in (str(document["trait"]),)
        ]
        rows.append(dict(rows[0]))
        write_tsv(self.fixture.declared_selection, adapter.ADAPTER_INPUT_COLUMNS, rows)
        self.assertRefused("duplicate declared analysis_id", "ukb-b-10001")

    def test_an_unwritable_table_is_not_left_behind(self) -> None:
        (self.fixture.source_dir / "ukb-b-10054.json").unlink()
        with self.assertRaises(InventoryError):
            self.convert()
        self.assertFalse(self.out.exists(), "a refused conversion leaves no table for freeze")


if __name__ == "__main__":
    unittest.main()

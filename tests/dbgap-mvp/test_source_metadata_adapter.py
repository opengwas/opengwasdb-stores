#!/usr/bin/env python3
"""The dbGaP MVP metadata adapter: mapping + ``*.dbGaP.metadata.txt`` -> Source Metadata Table (#290).

MVP ``phs002453`` is one Source Collection under the dbGaP Source Provider. Its
adapter converts the acquisition's Analysis/candidate binding (``mapping.tsv``)
and the deposit's own metadata files into the canonical Source Metadata Table
the shared freeze reads. The fixture's expected rows are literal, so a change
to the canonical columns or to a translated value has to be reviewed here.

Run from the repository root:
    python3 tests/dbgap-mvp/test_source_metadata_adapter.py
"""
from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

import yaml

from manifest_generation.freeze import freeze
from manifest_generation.providers.dbgap.mvp.source_metadata_adapter import (
    AdapterError,
    convert_mapping,
)
from manifest_generation.selection import parse_selection
from manifest_generation.source_metadata_table import load_source_metadata_table

MAPPING_COLUMNS = (
    "analysis_id", "member", "tar", "ancestry", "study_design", "dbgap_trait_type",
    "sample_size", "n_cases", "n_controls", "dbgap_trait_code", "analyzed_variable",
    "candidate_trait", "candidate_store_key", "match_basis", "trait_check",
)

INNER = "MVP_R4.1000G_AGR.GIA.Fixture"

#: analysis_id, ancestry, trait code, analyzed variable, trait type, N, cases, controls.
FIXTURE = (
    ("GCST90000001", "EUR", "A1C_Min_INT", "hemoglobin A1c (HbA1c, minimum, inv-norm transformed)", "quantitative", 338640, 0, 0),
    ("GCST90000002", "AFR", "A1C_Min_INT", "hemoglobin A1c (HbA1c, minimum, inv-norm transformed)", "quantitative", 98772, 0, 0),
    ("GCST90000003", "AMR", "Phe_394", "Rheumatic disease of the heart valves", "case-control", 438595, 5583, 433012),
    ("GCST90000004", "EAS", "Phe_394", "Rheumatic disease of the heart valves", "case-control", 3547, 1500, 2047),
    ("GCST90000005", "META", "Albumin_Mean_INT", "albumin (mean, inv-norm transformed)", "quantitative", 542276, 0, 0),
)

EXPECTED = {
    "GCST90000001": {
        "source_analysis_id": "GCST90000001",
        "source_trait_label": "hemoglobin A1c (HbA1c, minimum, inv-norm transformed)",
        "source_trait_id": "A1C_Min_INT",
        "source_ancestry_label": "European",
        "source_superpopulation": "EUR",
        "study_design": "quantitative",
        "sample_size": "338640",
        "publication_pmid": "39024449",
        "store_key": "dense__pmid-39024449__European",
    },
    "GCST90000003": {
        "source_analysis_id": "GCST90000003",
        "source_trait_label": "Rheumatic disease of the heart valves",
        "source_trait_id": "Phe_394",
        "source_ancestry_label": "Hispanic or Latin American",
        "source_superpopulation": "AMR",
        "study_design": "case-control",
        "sample_size": "438595",
        "n_cases": "5583",
        "n_controls": "433012",
        "publication_pmid": "39024449",
        "store_key": "dense__pmid-39024449__Hispanic-or-Latin-American",
    },
    "GCST90000005": {
        "source_analysis_id": "GCST90000005",
        "source_trait_label": "albumin (mean, inv-norm transformed)",
        "source_trait_id": "Albumin_Mean_INT",
        "source_ancestry_label": "META",
        # META is deliberately imprecise: it maps to no single super-population.
        "source_superpopulation": "",
        "study_design": "quantitative",
        "sample_size": "542276",
        "publication_pmid": "39024449",
        "store_key": "dense__pmid-39024449__Multi-ancestry",
    },
}


def build_fixture(root: Path) -> tuple[Path, Path]:
    extracted = root / "GIA" / "extracted" / INNER
    extracted.mkdir(parents=True)
    mapping_rows: list[dict[str, str]] = []
    for analysis_id, ancestry, trait_code, variable, design, size, cases, controls in FIXTURE:
        stem = f"MVP_R4.1000G_AGR.{trait_code}.{ancestry}.GIA"
        member = f"{INNER}/{stem}.dbGaP.txt.gz"
        sample = f"Total Sample Size={size}"
        if design == "case-control":
            sample += f"; Cases={cases}; Controls={controls}"
        (extracted / f"{stem}.dbGaP.metadata.txt").write_text(
            "Info\tDescription\n"
            f"Title of analysis\t{trait_code}.{ancestry}.GIA\n"
            f"Analyzed variable\t{variable}\n"
            f"Phenotypic trait type\t{'Binary Trait' if design == 'case-control' else 'Quantitative Trait'}\n"
            f"Sample size\t{sample}\n",
            encoding="utf-8",
        )
        mapping_rows.append({
            "analysis_id": analysis_id,
            "member": member,
            "tar": "phs002453.fixture.tar",
            "ancestry": ancestry,
            "study_design": design,
            "dbgap_trait_type": "binary" if design == "case-control" else "quantitative",
            "sample_size": str(size),
            "n_cases": str(cases),
            "n_controls": str(controls),
            "dbgap_trait_code": trait_code,
            "analyzed_variable": variable,
            "candidate_trait": variable.title(),
            "candidate_store_key": "dense__pmid-39024449__European",
            "match_basis": "unique-key",
            "trait_check": "exact",
        })
    mapping = root / "mapping.tsv"
    with mapping.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(MAPPING_COLUMNS), delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(mapping_rows)
    return mapping, root / "GIA" / "extracted"


class SourceMetadataAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="dbgap-adapter-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "root"
        self.mapping, self.extracted = build_fixture(self.root)
        self.out = self.root / "source-metadata-table.tsv"

    def convert(self) -> None:
        convert_mapping(self.mapping, self.extracted, self.out)

    def test_adapter_writes_the_canonical_rows_with_literal_values(self) -> None:
        self.convert()
        table = load_source_metadata_table(self.out)
        self.assertEqual(len(table.rows), 5)
        for analysis_id, expected in EXPECTED.items():
            row = table.row(analysis_id)
            for column, value in expected.items():
                with self.subTest(analysis_id=analysis_id, column=column):
                    self.assertEqual(row[column], value)
            # A quantitative Analysis records absence, never the mapping's 0/0.
            if analysis_id != "GCST90000003":
                self.assertEqual(row["n_cases"], "")
                self.assertEqual(row["n_controls"], "")
        # Every row carries the collection-wide publication; the genome build is
        # the release config's default, so the adapter does not write it.
        self.assertEqual({row["publication_pmid"] for row in table.rows}, {"39024449"})
        self.assertEqual({row.get("source_genome_build", "") for row in table.rows}, {""})

    def test_meta_is_imprecise_without_a_reason_of_its_own(self) -> None:
        self.convert()
        table = load_source_metadata_table(self.out)
        meta = [row for row in table.rows if row["source_ancestry_label"] == "META"]
        self.assertEqual(len(meta), 1)
        self.assertEqual(meta[0]["source_superpopulation"], "")

    def test_a_metadata_value_that_disagrees_with_the_mapping_is_refused(self) -> None:
        path = self.extracted / INNER / "MVP_R4.1000G_AGR.A1C_Min_INT.EUR.GIA.dbGaP.metadata.txt"
        path.write_text(
            path.read_text(encoding="utf-8").replace("Total Sample Size=338640", "Total Sample Size=1"),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(AdapterError, "sample size"):
            convert_mapping(self.mapping, self.extracted, self.out)

    def edit_metadata(self, analysis_id: str, old: str, new: str) -> None:
        """Rewrite one fixture metadata value, finding its file by analysis id."""
        for path in (self.extracted / INNER).glob("*.dbGaP.metadata.txt"):
            text = path.read_text(encoding="utf-8")
            if old in text:
                path.write_text(text.replace(old, new), encoding="utf-8")
                return
        raise AssertionError(f"no metadata file holds {old!r} for {analysis_id}")

    def edit_mapping(self, analysis_id: str, column: str, value: str) -> None:
        with self.mapping.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            rows = list(reader)
        (row,) = [row for row in rows if row["analysis_id"] == analysis_id]
        row[column] = value
        with self.mapping.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]), delimiter="\t", lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)

    def test_case_control_metadata_without_counts_is_refused(self) -> None:
        """A missing case count is absence, never a fabricated zero (#290 review)."""
        self.edit_metadata(
            "GCST90000003",
            "Sample size\tTotal Sample Size=438595; Cases=5583; Controls=433012",
            "Sample size\tTotal Sample Size=438595",
        )
        with self.assertRaisesRegex(AdapterError, "records no case/control counts"):
            self.convert()
        self.assertFalse(self.out.exists(), "a refused conversion writes no table")

    def test_a_quantitative_metadata_count_is_refused(self) -> None:
        self.edit_metadata(
            "GCST90000001",
            "Sample size\tTotal Sample Size=338640",
            "Sample size\tTotal Sample Size=338640; Cases=10; Controls=338630",
        )
        with self.assertRaisesRegex(AdapterError, "quantitative"):
            self.convert()

    def test_a_quantitative_mapping_count_is_refused(self) -> None:
        self.edit_mapping("GCST90000001", "n_cases", "10")
        with self.assertRaisesRegex(AdapterError, "quantitative"):
            self.convert()

    def test_a_case_control_count_disagreement_is_refused(self) -> None:
        self.edit_mapping("GCST90000003", "n_cases", "5584")
        with self.assertRaisesRegex(AdapterError, "case count 5583"):
            self.convert()

    def test_a_case_control_mapping_that_records_no_counts_is_refused(self) -> None:
        """A case-control mapping's 0 is not a count it can corroborate (#290 review)."""
        self.edit_mapping("GCST90000003", "n_cases", "0")
        with self.assertRaisesRegex(AdapterError, "mapping records no case/control counts"):
            self.convert()

    def test_a_member_named_with_an_unknown_ancestry_is_refused(self) -> None:
        rows = []
        with self.mapping.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            rows = list(reader)
        rows[0]["member"] = rows[0]["member"].replace(".EUR.GIA.dbGaP", ".XYZ.GIA.dbGaP")
        with self.mapping.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]), delimiter="\t", lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        with self.assertRaisesRegex(AdapterError, "ancestry"):
            self.convert()

    def test_the_five_selections_are_disjoint_and_select_their_bucket(self) -> None:
        self.convert()
        table = load_source_metadata_table(self.out)
        chosen: list[str] = []
        for store_key, count in (
            ("dense__pmid-39024449__European", 1),
            ("dense__pmid-39024449__African", 1),
            ("dense__pmid-39024449__Hispanic-or-Latin-American", 1),
            ("dense__pmid-39024449__East-Asian", 1),
            ("dense__pmid-39024449__Multi-ancestry", 1),
        ):
            selection = parse_selection({"where": {"store_key": store_key}})
            from manifest_generation.selection import select
            selected = select(table, selection)
            self.assertEqual(len(selected), count, store_key)
            chosen.extend(row["source_analysis_id"] for row in selected)
        self.assertEqual(len(chosen), len(set(chosen)))
        self.assertEqual(set(chosen), set(table.analysis_ids))

    def test_the_frozen_inventory_uses_the_shared_connector(self) -> None:
        """A fixture Acquisition Manifest of one row freezes against the adapter's table."""
        from manifest_generation.connector_tsv import write_connector
        from manifest_generation.acquisition_manifest import ACQUISITION_MANIFEST

        self.convert()
        data = self.root / "GCST90000001.tsv.gz"
        data.write_bytes(b"fixture")
        sidecar = self.root / "GCST90000001.tsv.gz-meta.yaml"
        sidecar.write_text(yaml.safe_dump({"genome_assembly": "GRCh38"}), encoding="utf-8")
        manifest = self.root / "acquisition-manifest.tsv"
        import hashlib

        write_connector(
            manifest,
            ACQUISITION_MANIFEST,
            ACQUISITION_MANIFEST.columns,
            [{
                "source_analysis_id": "GCST90000001",
                "status": "ok",
                "location": str(data),
                "bytes": str(data.stat().st_size),
                "sha256": hashlib.sha256(data.read_bytes()).hexdigest(),
                "sidecar_location": str(sidecar),
                "sidecar_bytes": str(sidecar.stat().st_size),
            }],
        )
        frozen = freeze(
            snapshot_id="fixture",
            source_collection_id="dbgap-phs002453",
            metadata_table=self.out,
            acquisition_manifest=manifest,
            selection=parse_selection({"where": {"store_key": "dense__pmid-39024449__European"}}),
        )
        self.assertEqual([row.analysis_id for row in frozen.rows], ["GCST90000001"])


if __name__ == "__main__":
    unittest.main()

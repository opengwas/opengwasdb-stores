#!/usr/bin/env python3
"""Loading a Source Metadata Table (issue #283, ADR 0036).

A Source Metadata Table is the per-Analysis source metadata a Source Collection
supplies in one canonical set of columns. Its rows are the intended membership,
so the loader is a gate: a valid table loads, and an unknown column, a missing
required value or a value outside the contract's vocabulary is refused rather
than carried into a frozen Source Inventory.

Run from the repository root:
    python3 tests/freeze/test_source_metadata_table.py
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from manifest_generation.connector_tsv import FreezeError, write_connector
from manifest_generation.source_inventory import InventoryError, sha256_file
from manifest_generation.source_metadata_table import (
    SOURCE_METADATA_TABLE,
    SourceMetadataTableError,
    load_source_metadata_table,
)
from ogstores.bundle import SUPERPOPULATIONS

VALID_HEADER = (
    "source_analysis_id",
    "source_trait_label",
    "source_trait_ontology_id",
    "source_trait_ontology_label",
    "source_ancestry_label",
    "source_superpopulation",
    "study_design",
    "declared_effect_scale",
    "sample_size",
    "n_cases",
    "n_controls",
    "source_genome_build",
    "publication_pmid",
    "first_author",
    "license",
    "store_key",
)

VALID_ROWS = (
    {
        "source_analysis_id": "GCST90000001",
        "source_trait_label": "Height",
        "source_trait_ontology_id": "http://www.ebi.ac.uk/efo/EFO_0004339",
        "source_trait_ontology_label": "body height",
        "source_ancestry_label": "European",
        "source_superpopulation": "EUR",
        "study_design": "quantitative",
        "declared_effect_scale": "sd",
        "sample_size": "1000",
        "n_cases": "",
        "n_controls": "",
        "source_genome_build": "GRCh38",
        "publication_pmid": "123",
        "first_author": "Doe J",
        "license": "EBI GWAS Catalog terms of use",
        "store_key": "hybrid__European",
    },
    {
        "source_analysis_id": "GCST90000002",
        "source_trait_label": "Type 2 diabetes",
        "source_trait_ontology_id": "",
        "source_trait_ontology_label": "",
        # A Source Ancestry Label the tracked map cannot translate: the label is
        # provenance and stays, the super-population is simply absent.
        "source_ancestry_label": "Multiple/Mixed",
        "source_superpopulation": "",
        "study_design": "case-control",
        "declared_effect_scale": "",
        "sample_size": "3000",
        "n_cases": "1000",
        "n_controls": "2000",
        "source_genome_build": "GRCh38",
        "publication_pmid": "456",
        "first_author": "Roe R",
        "license": "EBI GWAS Catalog terms of use",
        "store_key": "hybrid__Multiple-Mixed",
    },
)


def write_table(directory: Path, rows, header=VALID_HEADER, name: str = "table.tsv") -> Path:
    path = directory / name
    lines = ["\t".join(header)]
    for row in rows:
        lines.append("\t".join(row.get(column, "") for column in header))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def with_value(column: str, value: str, index: int = 0):
    rows = [dict(row) for row in VALID_ROWS]
    rows[index][column] = value
    return rows


class SourceMetadataTableTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def assertRefused(self, path: Path, *fragments: str) -> None:
        with self.assertRaises(SourceMetadataTableError) as caught:
            load_source_metadata_table(path)
        message = str(caught.exception)
        for fragment in fragments:
            self.assertIn(fragment, message)

    # -- a valid table ------------------------------------------------------

    def test_valid_table_loads_with_its_identity(self) -> None:
        path = write_table(self.tmp, VALID_ROWS)
        table = load_source_metadata_table(path)
        self.assertEqual(table.path, path)
        self.assertEqual(table.sha256, sha256_file(path))
        self.assertEqual(table.bytes, path.stat().st_size)
        self.assertEqual(table.columns, VALID_HEADER)
        self.assertEqual(table.analysis_ids, ("GCST90000001", "GCST90000002"))
        self.assertEqual(table.row("GCST90000002")["n_cases"], "1000")
        # Values are carried verbatim: no number is reformatted.
        self.assertEqual(table.row("GCST90000001")["sample_size"], "1000")

    def test_only_the_required_columns_are_needed(self) -> None:
        path = write_table(
            self.tmp, [{"source_analysis_id": "A1"}], header=("source_analysis_id",)
        )
        table = load_source_metadata_table(path)
        self.assertEqual(table.analysis_ids, ("A1",))

    def test_target_columns_are_canonical(self) -> None:
        """Sun 2018's protein targets: SomaScan SeqId, gene symbol, Ensembl gene, GRCh38 span."""
        header = (
            "source_analysis_id", "source_trait_label", "target_assay_id", "target_symbol",
            "target_ensembl_gene_id", "target_chromosome", "target_start", "target_end",
            "tissue", "context",
        )
        row = {
            "source_analysis_id": "GCST005806",
            "source_trait_label": "PDK1 levels",
            "target_assay_id": "5227-60",
            "target_symbol": "PDK1",
            "target_ensembl_gene_id": "ENSG00000152256",
            "target_chromosome": "2",
            "target_start": "172555373",
            "target_end": "172628598",
            "tissue": "plasma",
            "context": "SomaScan",
        }
        table = load_source_metadata_table(write_table(self.tmp, [row], header=header))
        self.assertEqual(table.row("GCST005806")["target_symbol"], "PDK1")

    def test_the_writer_writes_canonical_order_and_the_reader_reads_it_back(self) -> None:
        path = self.tmp / "written.tsv"
        write_connector(path, SOURCE_METADATA_TABLE, ("study_design", "source_analysis_id"),
                        [{"source_analysis_id": "A1", "study_design": "quantitative"}])
        self.assertEqual(path.read_text(encoding="utf-8"), "source_analysis_id\tstudy_design\nA1\tquantitative\n")
        self.assertEqual(load_source_metadata_table(path).analysis_ids, ("A1",))

    def test_the_writer_refuses_a_missing_required_or_unknown_column(self) -> None:
        with self.assertRaises(SourceMetadataTableError):
            write_connector(self.tmp / "w.tsv", SOURCE_METADATA_TABLE, ("study_design",), [])
        with self.assertRaises(SourceMetadataTableError):
            write_connector(self.tmp / "w.tsv", SOURCE_METADATA_TABLE, ("source_analysis_id", "STUDY"), [])

    # -- refusals: shape ----------------------------------------------------

    def test_unknown_column_is_refused(self) -> None:
        header = VALID_HEADER + ("STUDY.ACCESSION",)
        self.assertRefused(write_table(self.tmp, VALID_ROWS, header=header), "unknown column", "STUDY.ACCESSION")

    def test_missing_required_column_is_refused(self) -> None:
        header = tuple(column for column in VALID_HEADER if column != "source_analysis_id")
        self.assertRefused(write_table(self.tmp, VALID_ROWS, header=header), "source_analysis_id")

    def test_missing_required_value_is_refused(self) -> None:
        self.assertRefused(
            write_table(self.tmp, with_value("source_analysis_id", "", index=1)),
            "source_analysis_id", "line 3",
        )

    def test_duplicate_analysis_is_refused(self) -> None:
        rows = [dict(VALID_ROWS[0]), dict(VALID_ROWS[0])]
        self.assertRefused(write_table(self.tmp, rows), "duplicate", "GCST90000001")

    def test_duplicate_header_column_is_refused(self) -> None:
        header = VALID_HEADER + ("store_key",)
        self.assertRefused(write_table(self.tmp, VALID_ROWS, header=header), "store_key")

    def test_empty_table_is_refused(self) -> None:
        self.assertRefused(write_table(self.tmp, []), "no rows")

    def test_padded_value_is_refused(self) -> None:
        """An adapter writes the table; a padded cell is an adapter defect, not data to trim."""
        self.assertRefused(write_table(self.tmp, with_value("source_trait_label", "Height ")), "whitespace")

    def test_ragged_row_is_refused(self) -> None:
        path = write_table(self.tmp, VALID_ROWS)
        path.write_text(path.read_text(encoding="utf-8") + "GCST3\tToo short\n", encoding="utf-8")
        self.assertRefused(path, "line 4")

    def test_missing_file_is_refused(self) -> None:
        self.assertRefused(self.tmp / "absent.tsv", "not found")

    def test_refusal_is_a_freeze_error(self) -> None:
        """A table refusal stops a freeze, and the inventory commands report it."""
        self.assertTrue(issubclass(SourceMetadataTableError, FreezeError))
        self.assertTrue(issubclass(FreezeError, InventoryError))

    # -- refusals: vocabulary -----------------------------------------------

    def test_superpopulation_uses_the_contract_vocabulary(self) -> None:
        self.assertEqual(SUPERPOPULATIONS, ("AFR", "AMR", "EAS", "EUR", "MID", "NAF", "SAS"))
        for code in SUPERPOPULATIONS:
            load_source_metadata_table(write_table(self.tmp, with_value("source_superpopulation", code)))
        self.assertRefused(
            write_table(self.tmp, with_value("source_superpopulation", "European")),
            "source_superpopulation", "European",
        )

    def test_declared_effect_scale_uses_the_contract_vocabulary(self) -> None:
        for scale in ("sd", "log_or", "log_hazard"):
            load_source_metadata_table(write_table(self.tmp, with_value("declared_effect_scale", scale)))
        self.assertRefused(
            write_table(self.tmp, with_value("declared_effect_scale", "logOR")),
            "declared_effect_scale", "logOR",
        )

    def test_study_design_vocabulary(self) -> None:
        self.assertRefused(
            write_table(self.tmp, with_value("study_design", "binary")), "study_design", "binary"
        )

    def test_genome_build_vocabulary(self) -> None:
        self.assertRefused(
            write_table(self.tmp, with_value("source_genome_build", "hg38")), "source_genome_build", "hg38"
        )

    def test_numbers_must_be_numbers(self) -> None:
        self.assertRefused(write_table(self.tmp, with_value("sample_size", "NA")), "sample_size", "NA")
        self.assertRefused(write_table(self.tmp, with_value("publication_pmid", "PMID123")), "publication_pmid")

    def test_case_counts_only_for_case_control(self) -> None:
        """Zero is a real count: writing it where counts do not apply is refused (#133)."""
        self.assertRefused(write_table(self.tmp, with_value("n_cases", "0")), "n_cases", "case-control")

    def test_ontology_label_needs_its_identifier(self) -> None:
        self.assertRefused(
            write_table(self.tmp, with_value("source_trait_ontology_label", "diabetes", index=1)),
            "source_trait_ontology_label",
        )

    def test_target_coordinates_are_checked(self) -> None:
        header = ("source_analysis_id", "target_chromosome", "target_start", "target_end", "target_ensembl_gene_id")
        good = {"source_analysis_id": "A1", "target_chromosome": "X", "target_start": "10",
                "target_end": "20", "target_ensembl_gene_id": "ENSG00000152256"}
        load_source_metadata_table(write_table(self.tmp, [good], header=header))
        for column, value, fragment in (
            ("target_chromosome", "chr2", "target_chromosome"),
            ("target_start", "30", "target_start"),
            ("target_end", "", "target_end"),
            ("target_ensembl_gene_id", "PDK1", "target_ensembl_gene_id"),
        ):
            with self.subTest(column=column):
                bad = dict(good, **{column: value})
                self.assertRefused(write_table(self.tmp, [bad], header=header), fragment)

    def test_target_annotation_records_how_the_target_was_resolved(self) -> None:
        """The MHC flag, the resolution method, the rows it came from, and an aggregate assay (#300).

        GCST90240123 measures the seven-member 14-3-3 family under one SeqId,
        whose coordinates SomaScan.db maps to YWHAH: an aggregate.
        """
        header = (
            "source_analysis_id", "target_assay_id", "target_symbol", "target_ensembl_gene_id",
            "target_chromosome", "target_start", "target_end", "target_mhc", "target_is_aggregate",
            "target_resolution_method", "target_rows",
        )
        good = {
            "source_analysis_id": "GCST90240123", "target_assay_id": "4707-50", "target_symbol": "YWHAH",
            "target_ensembl_gene_id": "ENSG00000128245", "target_chromosome": "22",
            "target_start": "31944465", "target_end": "31957603", "target_mhc": "false",
            "target_is_aggregate": "true", "target_resolution_method": "somascan_db_seqid",
            "target_rows": "1",
        }
        table = load_source_metadata_table(write_table(self.tmp, [good], header=header))
        self.assertEqual(table.row("GCST90240123")["target_is_aggregate"], "true")
        for column, value, fragment in (
            ("target_mhc", "FALSE", "target_mhc"),
            ("target_is_aggregate", "1", "target_is_aggregate"),
            ("target_rows", "0", "target_rows"),
            ("target_assay_id", "", "target_assay_id"),
        ):
            with self.subTest(column=column):
                bad = dict(good, **{column: value})
                self.assertRefused(write_table(self.tmp, [bad], header=header), fragment)

    def test_every_problem_is_reported_at_once(self) -> None:
        rows = with_value("study_design", "binary")
        rows[1]["source_superpopulation"] = "Europe"
        self.assertRefused(write_table(self.tmp, rows), "study_design", "source_superpopulation")


if __name__ == "__main__":
    unittest.main(verbosity=2)

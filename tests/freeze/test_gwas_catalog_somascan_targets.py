#!/usr/bin/env python3
"""SomaScan target annotation for the GWAS Catalog Source Metadata Table (#300).

Sun 2018's pQTL Analyses each measure a SomaScan assay. The tracked target
table (``resources/reference-resources/somascan/sun-2018-analysis-targets.tsv``)
resolves each assay's gene and GRCh38 span, and SomaLogic's own table says
whether an assay measures several targets. The adapter adds that annotation to
the GWAS Catalog's Source Metadata Table, as its target columns, for the
Analyses the target table resolves -- the collection a Sun-style release
selects from.

The fixture is the target table's own rows for five Analyses; every expected
row is worked out by hand from them, as the R generator read them.

Run from the repository root:
    python3 tests/freeze/test_gwas_catalog_somascan_targets.py
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from manifest_generation import REPO_ROOT
from manifest_generation.connector_tsv import write_connector
from manifest_generation.providers.gwas_catalog.somascan_targets import annotate_targets
from manifest_generation.source_metadata_table import SOURCE_METADATA_TABLE, load_source_metadata_table

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "somascan"
SOMASCAN_TARGETS = REPO_ROOT / "resources/reference-resources/somascan/somascan-targets.tsv"
TARGET_COLUMNS = (
    "target_assay_id", "target_symbol", "target_ensembl_gene_id", "target_chromosome", "target_start",
    "target_end", "target_mhc", "target_is_aggregate", "target_resolution_method", "target_rows",
)


class SomascanTargetAnnotationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.table = self.tmp / "gwas-catalog-ssf.source-metadata.tsv"
        write_connector(
            self.table,
            SOURCE_METADATA_TABLE,
            ("source_analysis_id", "source_trait_label", "study_design", "sample_size", "store_key"),
            [
                {"source_analysis_id": analysis_id, "source_trait_label": label,
                 "study_design": "quantitative", "sample_size": "3301",
                 "store_key": "ragged__pmid-29875488__European"}
                for analysis_id, label in (
                    ("GCST005806", "PDK1 levels"),
                    ("GCST90199621", "Carnitine levels"),  # not a SomaScan Analysis
                    ("GCST90240123", "14-3-3 protein family levels"),
                    ("GCST90240202", "CD38 levels"),
                    ("GCST90240264", "AMP Kinase (alpha1beta1gamma1) levels"),
                    ("GCST90240366", "B melanoma antigen 3 levels"),
                )
            ],
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_the_target_table_annotates_the_analyses_it_resolves(self) -> None:
        out = self.tmp / "sun-2018.source-metadata.tsv"
        annotate_targets(self.table, FIXTURES / "sun-2018-analysis-targets.tsv", SOMASCAN_TARGETS, out)
        table = load_source_metadata_table(out)
        self.assertEqual(
            {row["source_analysis_id"]: tuple(row.get(column, "") for column in TARGET_COLUMNS)
             for row in table.rows},
            {
                # One target: its gene and span.
                "GCST005806": ("5227-60", "PDK1", "ENSG00000152256", "2", "172555373", "172628598",
                               "false", "false", "somascan_db_seqid", "1"),
                # The label lists seven members: an aggregate, whose coordinates
                # SomaScan.db maps to YWHAH.
                "GCST90240123": ("4707-50", "YWHAH", "ENSG00000128245", "22", "31944465", "31957603",
                                 "false", "true", "somascan_db_seqid", "1"),
                # Resolved by gene name through two current SeqIds: still one
                # gene. The assay is the source's own SeqId.
                "GCST90240202": ("11505-1", "CD38", "ENSG00000004468", "4", "15778275", "15853232",
                                 "false", "false", "somascan_db_gene_name", "2"),
                # Three genes on two chromosomes: no single span; SomaLogic flags
                # the assay as multiple.
                "GCST90240264": ("5183-53", "", "", "", "", "", "false", "true", "somascan_db_seqid", "27"),
                # Unresolved: kept, with no gene, span or rows.
                "GCST90240366": ("6442-6", "", "", "", "", "", "", "false", "unresolved", ""),
            },
        )
        # The table's own columns are carried verbatim.
        self.assertEqual(table.row("GCST005806")["source_trait_label"], "PDK1 levels")


if __name__ == "__main__":
    unittest.main()

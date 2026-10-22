#!/usr/bin/env python3
"""The eQTLGen metadata adapter: the BESD ``.epi`` -> Source Metadata Table (#291).

eQTLGen's file set holds one Analysis per probe and carries no study metadata,
so the adapter writes one row per ``.epi`` probe: the Analysis id
(``<probe>::<tissue>``, the id ``build-ragged-besd`` gives it), the probe it
reads, the probe's position as its target, and the study-level facts the
operator states (tissue and sample size) on every row. The expected rows are
literals, not the adapter's own output.

Run from the repository root:
    python3 tests/eqtlgen-pipeline/test_source_metadata_adapter.py
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from manifest_generation.providers.eqtlgen.source_metadata_adapter import (
    AdapterError,
    convert_epi,
)
from manifest_generation.source_metadata_table import load_source_metadata_table

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "eqtlgen-fixture.epi"

EXPECTED_HEADER = (
    "source_analysis_id", "source_trait_label", "study_design", "sample_size",
    "target_ensembl_gene_id", "target_chromosome", "target_start", "target_end",
    "tissue", "source_probe_id",
)

EXPECTED = [
    {
        "source_analysis_id": "ENSG00000175445::whole_blood",
        "source_probe_id": "ENSG00000175445",
        "source_trait_label": "ENSG00000175445",
        "study_design": "quantitative",
        "sample_size": "31684",
        "tissue": "whole_blood",
        "target_ensembl_gene_id": "ENSG00000175445",
        "target_chromosome": "8",
        "target_start": "19791998",
        "target_end": "19791998",
    },
    {
        "source_analysis_id": "ENSG00000134243::whole_blood",
        "source_probe_id": "ENSG00000134243",
        "source_trait_label": "ENSG00000134243",
        "study_design": "quantitative",
        "sample_size": "31684",
        "tissue": "whole_blood",
        "target_ensembl_gene_id": "ENSG00000134243",
        "target_chromosome": "1",
        "target_start": "109896382",
        "target_end": "109896382",
    },
    {
        "source_analysis_id": "ENSG00000232810::whole_blood",
        "source_probe_id": "ENSG00000232810",
        "source_trait_label": "ENSG00000232810",
        "study_design": "quantitative",
        "sample_size": "31684",
        "tissue": "whole_blood",
        "target_ensembl_gene_id": "ENSG00000232810",
        "target_chromosome": "6",
        "target_start": "31544728",
        "target_end": "31544728",
    },
]


class EqtlgenSourceMetadataAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.out = self.tmp / "source-metadata.tsv"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_every_probe_becomes_one_canonical_row(self) -> None:
        convert_epi(FIXTURE, self.out, tissue="whole_blood", sample_size=31684)
        table = load_source_metadata_table(self.out)
        self.assertEqual(table.columns, EXPECTED_HEADER)
        self.assertEqual(
            [dict(table.row(analysis_id)) for analysis_id in table.analysis_ids], EXPECTED
        )

    def test_a_probe_on_a_chromosome_the_table_does_not_know_is_refused_by_name(self) -> None:
        epi = self.tmp / "odd.epi"
        epi.write_text("23\tENSG00000000001\t0\t5\tENSG00000000001\tN\n", encoding="utf-8")
        with self.assertRaises(AdapterError) as caught:
            convert_epi(epi, self.out, tissue="whole_blood", sample_size=31684)
        self.assertIn("ENSG00000000001", str(caught.exception))
        self.assertIn("chromosome '23'", str(caught.exception))
        self.assertFalse(self.out.exists())

    def test_a_repeated_probe_and_a_short_row_are_refused(self) -> None:
        epi = self.tmp / "bad.epi"
        epi.write_text(
            "1\tENSG00000000001\t0\t5\tENSG00000000001\tN\n"
            "1\tENSG00000000001\t0\t9\tENSG00000000001\tN\n"
            "2\tENSG00000000002\t0\n",
            encoding="utf-8",
        )
        with self.assertRaises(AdapterError) as caught:
            convert_epi(epi, self.out, tissue="whole_blood", sample_size=31684)
        message = str(caught.exception)
        self.assertIn("ENSG00000000001", message)
        self.assertIn("line 3", message)
        self.assertFalse(self.out.exists())


if __name__ == "__main__":
    unittest.main()

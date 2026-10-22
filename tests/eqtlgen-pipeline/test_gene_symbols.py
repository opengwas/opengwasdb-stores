#!/usr/bin/env python3
"""The eQTLGen gene-symbol map: probes -> GeneSymbol, from the published cis file (#314).

The adapter's Analysis Labels come from eQTLGen's own published ``Gene`` and
``GeneSymbol`` columns, which the ``.epi`` does not carry. This module streams the
published (gzipped) file once and writes the small map as a connector input with
its lineage: the URL, the downloaded file's sha256 and byte count, and the date.
The fixture is a gzip written here and read through a ``file://`` URL, so the
expected map, counts and checksum are literals and an independent hash.

Run from the repository root:
    python3 tests/eqtlgen-pipeline/test_gene_symbols.py
"""

from __future__ import annotations

import gzip
import hashlib
import tempfile
import unittest
from pathlib import Path

import yaml

from manifest_generation.providers.eqtlgen.gene_symbols import GeneSymbolError, build_map

HEADER = "Pvalue\tSNP\tSNPChr\tSNPPos\tAssessedAllele\tOtherAllele\tZscore\tGene\tGeneSymbol\tGeneChr\tGenePos\tNrCohorts\tNrSamples\tFDR\tBonferroniP\n"


def row(gene: str, symbol: str) -> str:
    return f"1E-5\trs1\t1\t100\tA\tG\t4.5\t{gene}\t{symbol}\t1\t90\t30\t30000\t0.0\t0.1\n"


class GeneSymbolTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.out = self.tmp / "gene-symbols.tsv"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def published(self, *rows: str) -> str:
        path = self.tmp / "published.txt.gz"
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            fh.write(HEADER + "".join(rows))
        self.gz = path
        return path.as_uri()

    def test_the_map_has_each_probes_symbol_and_the_lineage_of_the_file(self) -> None:
        url = self.published(
            row("ENSG00000232810", "TNF"), row("ENSG00000232810", "TNF"),   # repeated, consistent
            row("ENSG00000134243", "SORT1"),
            row("ENSG00000999999", "NOT_A_PROBE"),                            # not one of ours
            row("ENSG00000000007", ""),                                       # no symbol published
        )
        probes = ["ENSG00000232810", "ENSG00000134243", "ENSG00000000007", "ENSG00000000008"]
        counts = build_map(url, probes, self.out, today="2026-10-22")
        self.assertEqual(
            self.out.read_text(encoding="utf-8"),
            "gene\tgene_symbol\nENSG00000134243\tSORT1\nENSG00000232810\tTNF\n",
        )
        self.assertEqual(counts, {"probes": 4, "with_symbol": 2, "without_symbol": 2})
        meta = yaml.safe_load(Path(f"{self.out}.meta.yaml").read_text(encoding="utf-8"))
        self.assertEqual(meta["url"], url)
        self.assertEqual(meta["sha256"], hashlib.sha256(self.gz.read_bytes()).hexdigest())
        self.assertEqual(meta["bytes"], self.gz.stat().st_size)
        self.assertEqual(meta["downloaded"], "2026-10-22")
        self.assertEqual(meta["probes"], 4)
        self.assertEqual(meta["with_symbol"], 2)
        self.assertEqual(meta["without_symbol"], 2)

    def test_a_gene_with_two_symbols_is_refused_by_name_and_nothing_is_written(self) -> None:
        url = self.published(row("ENSG00000232810", "TNF"), row("ENSG00000232810", "LTA"))
        with self.assertRaises(GeneSymbolError) as caught:
            build_map(url, ["ENSG00000232810"], self.out, today="2026-10-22")
        for word in ("ENSG00000232810", "TNF", "LTA"):
            self.assertIn(word, str(caught.exception))
        self.assertFalse(self.out.exists())

    def test_a_published_file_without_the_columns_is_refused(self) -> None:
        path = self.tmp / "bad.txt.gz"
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            fh.write("A\tB\n1\t2\n")
        with self.assertRaises(GeneSymbolError) as caught:
            build_map(path.as_uri(), ["ENSG00000232810"], self.out, today="2026-10-22")
        self.assertIn("GeneSymbol", str(caught.exception))
        self.assertFalse(self.out.exists())


if __name__ == "__main__":
    unittest.main()

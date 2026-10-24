#!/usr/bin/env python3
"""The eQTLGen per-pair sample-size table: published file + BESD -> table (#302, ADR 0067).

opengwasdb's ``build-ragged-besd --sample-size-table`` takes one row per stored
(probe, SNP) pair, ``probe_id``, ``snp_id`` and ``n``, grouped by probe in ``.epi``
order. eQTLGen's published file carries ``NrSamples`` per (Gene, SNP) in no such
order, so the converter joins it to the BESD's own pairs. The fixture is a tiny
published file in an order unrelated to the ``.epi`` and a BESD written here, so
the expected rows, counts and checksums are literals and an independent hash,
not the converter's own output.

Run from the repository root:
    python3 tests/eqtlgen-pipeline/test_sample_size_table.py
"""

from __future__ import annotations

import gzip
import hashlib
import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np
import yaml

from manifest_generation.providers.eqtlgen.sample_size_table import SampleSizeTableError, build_table

URL = "https://example.org/published.txt.gz"
HEADER = "Pvalue\tSNP\tSNPChr\tSNPPos\tAssessedAllele\tOtherAllele\tZscore\tGene\tGeneSymbol\tGeneChr\tGenePos\tNrCohorts\tNrSamples\tFDR\tBonferroniP\n"
EPI = (
    "8\tENSG00000175445\t0\t19791998\tENSG00000175445\tN\n"
    "1\tENSG00000134243\t0\t109896382\tENSG00000134243\tN\n"
    "6\tENSG00000232810\t0\t31544728\tENSG00000232810\tN\n"
)
SNPS = ["rs0", "rs1", "rs2", "rs3", "rs4"]
#: Each probe's stored pairs, as .esi row indices in BESD order.
PAIRS = {
    "ENSG00000175445": [0, 1, 2],
    "ENSG00000134243": [0, 1, 3],
    "ENSG00000232810": [2, 0],
}


def write_besd(prefix: Path) -> None:
    probes = [line.split("\t")[1] for line in EPI.splitlines()]
    Path(f"{prefix}.epi").write_text(EPI, encoding="utf-8")
    Path(f"{prefix}.esi").write_text(
        "".join(f"1\t{name}\t0\t{1000 * (i + 1)}\tA\tG\t0.3\n" for i, name in enumerate(SNPS)), encoding="utf-8"
    )
    cols = np.zeros(2 * len(probes) + 1, dtype=np.int64)
    rowid, values, offset = [], [], 0
    for n, probe in enumerate(probes):
        rows = np.array(PAIRS[probe], dtype=np.uint32)
        cols[2 * n], cols[2 * n + 1] = offset, offset + len(rows)
        rowid += [rows, rows]
        values.append(np.full(2 * len(rows), 0.5, dtype=np.float32))
        offset += 2 * len(rows)
    cols[-1] = offset
    with open(f"{prefix}.besd", "wb") as fh:
        fh.write(struct.pack("<I", 3) + b"\x00" * 60 + struct.pack("<Q", offset))
        fh.write(cols.tobytes() + np.concatenate(rowid).tobytes() + np.concatenate(values).tobytes())


def published_row(gene: str, snp: str, n: str) -> str:
    return f"1E-5\t{snp}\t1\t100\tA\tG\t4.5\t{gene}\tSYM\t1\t90\t30\t{n}\t0.0\t0.1\n"


#: The published rows in no .epi order: the third probe first, a published-only pair
#: of a probe the file set holds (ENSG00000175445, rs4), a probe it does not hold
#: (ENSG00000999999), and no row for the BESD pair (ENSG00000134243, rs3).
PUBLISHED = [
    ("ENSG00000232810", "rs0", "500"),
    ("ENSG00000999999", "rs1", "7"),
    ("ENSG00000134243", "rs1", "31000"),
    ("ENSG00000175445", "rs2", "18000"),
    ("ENSG00000232810", "rs2", "31684"),
    ("ENSG00000175445", "rs4", "9"),
    ("ENSG00000134243", "rs0", "12000"),
    ("ENSG00000175445", "rs0", "1000"),
    ("ENSG00000175445", "rs1", "20000"),
]

EXPECTED_TABLE = (
    "probe_id\tsnp_id\tn\n"
    "ENSG00000175445\trs0\t1000\n"
    "ENSG00000175445\trs1\t20000\n"
    "ENSG00000175445\trs2\t18000\n"
    "ENSG00000134243\trs0\t12000\n"
    "ENSG00000134243\trs1\t31000\n"
    "ENSG00000232810\trs2\t31684\n"
    "ENSG00000232810\trs0\t500\n"
)


class SampleSizeTableTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.prefix = self.tmp / "fixture"
        write_besd(self.prefix)
        self.out = self.tmp / "fixture.sample-size.tsv.gz"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def publish(self, rows=PUBLISHED, header=HEADER) -> Path:
        path = self.tmp / "published.txt.gz"
        path.write_bytes(gzip.compress((header + "".join(published_row(*r) for r in rows)).encode()))
        return path

    def build(self, published: Path | None = None) -> dict:
        return build_table(published or self.publish(), self.prefix, self.out, source_url=URL)

    def table_text(self) -> str:
        return gzip.decompress(self.out.read_bytes()).decode()

    def test_the_table_is_one_row_per_matched_pair_grouped_by_probe_in_epi_order(self) -> None:
        self.build()
        self.assertEqual(self.table_text(), EXPECTED_TABLE)

    def test_the_lineage_names_the_source_the_file_set_and_the_row_counts(self) -> None:
        published = self.publish()
        self.build(published)
        lineage = yaml.safe_load(Path(f"{self.out}.provenance.yaml").read_text(encoding="utf-8"))
        self.assertEqual(lineage["source_url"], URL)
        self.assertEqual(lineage["source_sha256"], hashlib.sha256(published.read_bytes()).hexdigest())
        self.assertEqual(lineage["source_bytes"], published.stat().st_size)
        for member in ("besd", "epi", "esi"):
            self.assertEqual(
                lineage["besd"][member], hashlib.sha256(Path(f"{self.prefix}.{member}").read_bytes()).hexdigest()
            )
        self.assertEqual(lineage["table_sha256"], hashlib.sha256(self.out.read_bytes()).hexdigest())
        self.assertEqual(
            lineage["counts"],
            {
                "besd_pairs": 8,
                "published_rows": 9,
                "matched": 7,
                "unmatched": 1,
                "published_only": 2,
            },
        )

    def test_a_gene_and_snp_published_twice_is_refused_by_name(self) -> None:
        rows = PUBLISHED + [("ENSG00000134243", "rs1", "31001")]
        with self.assertRaises(SampleSizeTableError) as caught:
            self.build(self.publish(rows))
        self.assertIn("ENSG00000134243", str(caught.exception))
        self.assertIn("rs1", str(caught.exception))
        self.assertFalse(self.out.exists())

    def test_an_n_that_is_not_a_whole_number_is_refused_by_name(self) -> None:
        for bad in ("NA", "0", "31684.5", ""):
            rows = PUBLISHED + [("ENSG00000134243", "rs2", bad)]
            with self.subTest(n=bad), self.assertRaises(SampleSizeTableError) as caught:
                self.build(self.publish(rows))
            self.assertIn("ENSG00000134243", str(caught.exception))
            self.assertIn("rs2", str(caught.exception))
            self.assertFalse(self.out.exists())

    def test_a_published_file_without_the_columns_is_refused(self) -> None:
        with self.assertRaises(SampleSizeTableError) as caught:
            self.build(self.publish(header="Gene\tSNP\tZscore\n"))
        self.assertIn("NrSamples", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

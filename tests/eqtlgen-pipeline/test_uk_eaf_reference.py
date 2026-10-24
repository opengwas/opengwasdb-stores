#!/usr/bin/env python3
"""The "United Kingdom" EAF table derived from ukb-ancestry-mixture-hg38 (#282, ADR 0067).

``--eaf-reference`` takes ``alid`` and ``eaf``; the mixture reference holds one
oriented column per fine group. The builder copies the named column unchanged and
records the source's sha256. The fixture is a three-row mixture file written here,
so the expected rows and digest are literals and an independent hash.

Run from the repository root:
    python3 tests/eqtlgen-pipeline/test_uk_eaf_reference.py
"""

from __future__ import annotations

import gzip
import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "reference-resources" / "ukb-ancestry-mixture-hg38-uk-eaf" / "build_uk_eaf.py"
MIXTURE = (
    "alid\tchromosome\tposition\teffect_allele\tother_allele\trsid\tFinland\tUnited Kingdom\tIreland\n"
    "1:100:A:G\t1\t100\tA\tG\trs1\t0.10\t0.25\t0.30\n"
    "1:200:C:T\t1\t200\tC\tT\trs2\t0.90\t0.75\t0.70\n"
    "2:300:A:C\t2\t300\tA\tC\trs3\t0.50\t\t0.50\n"
)


class UkEafReferenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.freqs = self.tmp / "ref_freqs.tsv.gz"
        self.freqs.write_bytes(gzip.compress(MIXTURE.encode()))
        self.out = self.tmp / "eaf.tsv.gz"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_tool(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-I", str(TOOL), "--freqs", str(self.freqs), "--out", str(self.out), *extra],
            capture_output=True, text=True,
        )

    def test_the_united_kingdom_column_becomes_eaf_and_a_blank_row_is_dropped(self) -> None:
        done = self.run_tool()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(
            gzip.decompress(self.out.read_bytes()).decode(), "alid\teaf\n1:100:A:G\t0.25\n1:200:C:T\t0.75\n"
        )
        meta = yaml.safe_load(Path(f"{self.out}.meta.yaml").read_text(encoding="utf-8"))
        self.assertEqual(meta["source_sha256"], hashlib.sha256(self.freqs.read_bytes()).hexdigest())
        self.assertEqual(meta["source_resource_id"], "ukb-ancestry-mixture-hg38")
        self.assertEqual((meta["rows_read"], meta["rows_blank"], meta["rows_written"]), (3, 1, 2))
        self.assertEqual(meta["table_sha256"], hashlib.sha256(self.out.read_bytes()).hexdigest())

    def test_an_existing_output_is_never_overwritten(self) -> None:
        self.out.write_bytes(b"keep")
        done = self.run_tool()
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(self.out.read_bytes(), b"keep")

    def test_a_column_the_mixture_lacks_is_refused_by_name(self) -> None:
        done = self.run_tool("--column", "Atlantis")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("Atlantis", done.stderr)
        self.assertFalse(self.out.exists())


if __name__ == "__main__":
    unittest.main()

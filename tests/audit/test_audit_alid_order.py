#!/usr/bin/env python3
"""The audit's allele-order rule for a stored ALID, by format (#381, opengwasdb ADR 0079).

Below format 0.2.2 every ALID spells its alleles sorted, an indel's included
(OGS-00004 is 0.1.0 and OGS-00011 is 0.2.0), so a `1:100:AT:A` row there is not
canonical and must raise `non_canonical_alid_rows`. From 0.2.2 an indel spells
its reference allele first, so a deletion (`1:827587:GGGGCC:G`) has an effect
allele that sorts after its other allele and is canonical. The tests run the
scan the audit runs (`_scan_variant_table`), not only the helper under it.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import pysam

from ogstores import audit


def write_table(path: Path, rows: list[tuple[str, str, str]]) -> Path:
    """A Store Variant Table of `(position, effect, other)` rows on chromosome 1."""
    with pysam.BGZFile(str(path), "wb") as handle:  # type: ignore[call-arg]
        for index, (position, effect, other) in enumerate(rows):
            line = "\t".join(["1", position, str(index), effect, other, f"1:{position}:{effect}:{other}", ".", "."])
            handle.write((line + "\n").encode())
    return path


class TestAlleleOrder(unittest.TestCase):
    def test_a_sorted_pair_is_in_order_in_every_format(self) -> None:
        for reference_first in (False, True):
            for effect, other in (("A", "G"), ("AC", "GT")):
                self.assertTrue(audit.alleles_in_alid_order(effect, other, indels_reference_first=reference_first))
            for effect, other in (("G", "A"), ("T", "T"), ("GT", "AC")):
                self.assertFalse(audit.alleles_in_alid_order(effect, other, indels_reference_first=reference_first))

    def test_an_indel_is_sorted_below_0_2_2_and_reference_first_from_it(self) -> None:
        for effect, other in (("AT", "A"), ("GGGGCC", "G")):  # a deletion: effect allele sorts last
            self.assertFalse(audit.alleles_in_alid_order(effect, other, indels_reference_first=False), (effect, other))
            self.assertTrue(audit.alleles_in_alid_order(effect, other, indels_reference_first=True), (effect, other))
        for effect, other in (("A", "AT"), ("G", "GGGGCC")):  # an insertion is sorted either way
            self.assertTrue(audit.alleles_in_alid_order(effect, other, indels_reference_first=False))
            self.assertTrue(audit.alleles_in_alid_order(effect, other, indels_reference_first=True))
        self.assertFalse(audit.alleles_in_alid_order("AT", "AT", indels_reference_first=True))

    def test_the_format_that_spells_indels_reference_first(self) -> None:
        expected = {"0.1.0": False, "0.2.0": False, "0.2.1": False, "0.2.2": True, "0.2.10": True, "0.3.0": True}
        for version, reference_first in expected.items():
            self.assertIs(audit.indels_spelt_reference_first(version), reference_first, version)


class TestScanOfTheVariantTable(unittest.TestCase):
    ROWS = [("100", "A", "G"), ("200", "AT", "A"), ("300", "G", "A"), ("400", "A", "AT")]

    def scan(self, *, reference_first: bool) -> audit._IdentityScan:
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            table = write_table(Path(tmp) / "variants.tsv.gz", self.ROWS)
            return audit._scan_variant_table(
                table, n_axis=len(self.ROWS), reference=None, indels_reference_first=reference_first
            )

    def test_below_0_2_2_a_sorted_format_deletion_row_is_non_canonical(self) -> None:
        """`1:200:AT:A` and the unsorted SNV `1:300:G:A` are both reported."""
        self.assertEqual(self.scan(reference_first=False).non_canonical_rows, 2)

    def test_from_0_2_2_only_the_unsorted_snv_is_non_canonical(self) -> None:
        self.assertEqual(self.scan(reference_first=True).non_canonical_rows, 1)


if __name__ == "__main__":
    unittest.main()

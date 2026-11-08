#!/usr/bin/env python3
"""A `swap_alleles` rescue rewrites the statistics, never the allele labels (#455).

opengwasdb#381 reads `other_allele` as the reference of a GWAS-SSF indel, so a
rescued copy has to keep both allele columns exactly as the raw file has them.
The rescue then expresses the statistics on the other allele:

* `beta`, `z`, `Stat` are negated, `Direction` has its signs exchanged;
* `odds_ratio` is inverted and `ci_lower`/`ci_upper` are inverted and swapped;
* `effect_allele_frequency` is complemented, `MinFreq`/`MaxFreq` complemented
  and exchanged;
* `hm_code` and every other column are byte-identical.

A missing value (`NA`, empty) stays what it was. `flip_frequency` is untouched.
"""
from __future__ import annotations

import gzip
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from manifest_generation.providers.gwas_catalog.create_rescued_gwas_files import rescue_file

BETA_HDR = ["chromosome", "base_pair_location", "effect_allele", "other_allele", "beta",
            "standard_error", "effect_allele_frequency", "p_value", "rsid", "hm_code", "variant_id"]
BETA_ROWS = [
    # SNV, indel (insertion), indel (deletion), missing beta / EAF, zero beta
    ["1", "100", "A", "G", "0.25", "0.1", "0.3", "0.01", "rs1", "11", "1_100_G_A"],
    ["1", "200", "CTA", "C", "-0.5", "0.2", "0.15", "0.5", "rs2", "10", "1_200_C_CTA"],
    ["1", "300", "G", "GTT", "1e-05", "0.2", "0.999", "0.5", "NA", "11", "1_300_GTT_G"],
    ["1", "400", "T", "C", "NA", "0.2", "", "0.5", "rs4", "12", "1_400_C_T"],
    ["1", "500", "T", "C", "0", "0.2", "NA", "0.5", "rs5", "13", "1_500_C_T"],
]
OR_HDR = ["chromosome", "base_pair_location", "effect_allele", "other_allele", "beta",
          "standard_error", "effect_allele_frequency", "p_value", "rsid", "ci_upper",
          "ci_lower", "n", "hm_coordinate_conversion", "odds_ratio", "hm_code", "variant_id"]
OR_ROWS = [
    ["1", "100", "A", "G", "0.5", "0.1", "0.3", "0.01", "rs1", "2.0", "1.25", "9", "rs", "1.6", "11", "1_100_G_A"],
    ["1", "200", "CTA", "C", "-0.5", "0.2", "0.15", "0.5", "rs2", "0.8", "0.5", "9", "lo", "0.625", "10", "1_200_C_CTA"],
    ["1", "300", "T", "C", "NA", "0.2", "0.2", "0.5", "rs3", "NA", "", "9", "rs", "NA", "11", "1_300_C_T"],
]
ALL_HDR = ["chromosome", "base_pair_location", "effect_allele", "other_allele", "beta",
           "standard_error", "effect_allele_frequency", "p_value", "z", "Stat", "Var",
           "FreqSE", "MinFreq", "MaxFreq", "Direction", "hm_code"]
ALL_ROWS = [
    ["1", "1", "A", "G", "0.1", "0.1", "0.3", "0.5", "1.5", "47.5", "772.3", "0.02", "0.25", "0.4", "?+-", "11"],
    ["1", "2", "A", "G", "0.1", "0.1", "0.3", "0.5", "NA", "", "5", "NA", "NA", "0.4", "???+", "11"],
]


def write_gz(path: Path, hdr: list[str], rows: list[list[str]], newline="\n") -> None:
    with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
        for r in [hdr, *rows]:
            fh.write("\t".join(r) + newline)


def read_gz(path: Path) -> tuple[list[str], list[list[str]]]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        lines = [ln.rstrip("\n").split("\t") for ln in fh]
    return lines[0], lines[1:]


class SwapAllelesKeepsLabels(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def rescue(self, hdr, rows, recommendation="swap_alleles", newline="\n"):
        raw, out = self.tmp / "raw.tsv.gz", self.tmp / "out.tsv.gz"
        write_gz(raw, hdr, rows, newline)
        rescue_file(raw, out, recommendation)
        got_hdr, got_rows = read_gz(out)
        self.assertEqual(got_hdr, hdr)
        self.assertEqual(len(got_rows), len(rows))
        return got_rows

    def col(self, hdr, rows, name):
        return [r[hdr.index(name)] for r in rows]

    def test_allele_columns_equal_the_raw_files_on_snvs_and_indels(self):
        got = self.rescue(BETA_HDR, BETA_ROWS)
        for name in ("effect_allele", "other_allele"):
            self.assertEqual(self.col(BETA_HDR, got, name), self.col(BETA_HDR, BETA_ROWS, name))

    def test_beta_is_negated_and_missing_stays_missing(self):
        got = self.rescue(BETA_HDR, BETA_ROWS)
        self.assertEqual(self.col(BETA_HDR, got, "beta"), ["-0.25", "0.5", "-1e-05", "NA", "0"])

    def test_eaf_is_complemented_and_missing_stays_missing(self):
        got = self.rescue(BETA_HDR, BETA_ROWS)
        eaf = self.col(BETA_HDR, got, "effect_allele_frequency")
        self.assertEqual([Decimal(x) for x in eaf[:3]], [Decimal("0.7"), Decimal("0.85"), Decimal("0.001")])
        self.assertEqual(eaf[3:], ["", "NA"])

    def test_odds_ratio_is_inverted_and_its_interval_inverted_and_swapped(self):
        got = self.rescue(OR_HDR, OR_ROWS)
        g = lambda n: self.col(OR_HDR, got, n)
        self.assertEqual([float(x) for x in g("odds_ratio")[:2]], [1 / 1.6, 1 / 0.625])
        # new upper bound is 1 / old lower bound; new lower is 1 / old upper
        self.assertEqual([float(x) for x in g("ci_upper")[:2]], [1 / 1.25, 1 / 0.5])
        self.assertEqual([float(x) for x in g("ci_lower")[:2]], [1 / 2.0, 1 / 0.8])
        # missing stays missing, bounds still exchanged: old upper NA, old lower ""
        self.assertEqual((g("ci_lower")[2], g("ci_upper")[2]), ("NA", ""))
        self.assertEqual((g("odds_ratio")[2], g("beta")[2]), ("NA", "NA"))
        self.assertEqual(g("beta")[:2], ["-0.5", "0.5"])

    def test_signed_and_frequency_companions(self):
        got = self.rescue(ALL_HDR, ALL_ROWS)
        g = lambda n: self.col(ALL_HDR, got, n)
        self.assertEqual(g("z"), ["-1.5", "NA"])
        self.assertEqual(g("Stat"), ["-47.5", ""])
        self.assertEqual(g("Direction"), ["?-+", "???-"])
        # new MinFreq = 1 - old MaxFreq, new MaxFreq = 1 - old MinFreq
        self.assertEqual(Decimal(g("MinFreq")[0]), Decimal("0.6"))
        self.assertEqual(Decimal(g("MaxFreq")[0]), Decimal("0.75"))
        self.assertEqual(g("MinFreq")[1], "0.6")  # from the old MaxFreq 0.4
        self.assertEqual(g("FreqSE"), ["0.02", "NA"])
        self.assertEqual(g("MaxFreq")[1], "NA")   # old MinFreq missing

    def test_hm_code_and_unlisted_columns_are_byte_identical(self):
        changed = {"beta", "effect_allele_frequency", "z", "Stat", "Direction", "MinFreq", "MaxFreq"}
        for hdr, rows in ((BETA_HDR, BETA_ROWS), (ALL_HDR, ALL_ROWS)):
            got = self.rescue(hdr, rows)
            for name in hdr:
                if name not in changed:
                    self.assertEqual(self.col(hdr, got, name), self.col(hdr, rows, name), name)
        got = self.rescue(OR_HDR, OR_ROWS)
        for name in set(OR_HDR) - {"beta", "effect_allele_frequency", "odds_ratio", "ci_upper", "ci_lower"}:
            self.assertEqual(self.col(OR_HDR, got, name), self.col(OR_HDR, OR_ROWS, name), name)

    def test_flip_frequency_is_unchanged(self):
        got = self.rescue(BETA_HDR, BETA_ROWS, "flip_frequency")
        for name in BETA_HDR:
            if name != "effect_allele_frequency":
                self.assertEqual(self.col(BETA_HDR, got, name), self.col(BETA_HDR, BETA_ROWS, name))
        self.assertEqual(self.col(BETA_HDR, got, "effect_allele_frequency"),
                         ["0.7", "0.85", "0.001", "", "NA"])

    def test_an_unreadable_statistic_stops_the_rescue(self):
        bad = [list(BETA_ROWS[0])]
        bad[0][BETA_HDR.index("effect_allele_frequency")] = "oops"
        with self.assertRaises(ValueError):
            self.rescue(BETA_HDR, bad)


if __name__ == "__main__":
    unittest.main()

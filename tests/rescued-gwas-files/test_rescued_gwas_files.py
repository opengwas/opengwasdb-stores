#!/usr/bin/env python3
"""A `swap_alleles` rescue rewrites the statistics, never the allele labels (#455).

opengwasdb#381 reads `other_allele` as the reference of a GWAS-SSF indel, so a
rescued copy has to keep both allele columns exactly as the raw file has them.
The rescue then expresses the statistics on the other allele:

* `beta` is negated;
* `odds_ratio` is inverted and `ci_lower`/`ci_upper` are inverted and swapped;
* `effect_allele_frequency` is complemented;
* `hm_code`, the unharmonised study columns (`MinFreq`, `MaxFreq`, `Direction`,
  `FreqSE`) and every other column are byte-identical;
* a column the rescue has not classified refuses the file.

A missing value (`NA`, empty) stays what it was. `flip_frequency` is untouched.
"""
from __future__ import annotations

import csv
import gzip
import sys
import tempfile
import unittest
from decimal import Decimal
from fractions import Fraction
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
# Study-only columns the harmoniser never flipped stay exactly as raw (#455 review).
STUDY_HDR = ["chromosome", "base_pair_location", "effect_allele", "other_allele", "beta",
             "standard_error", "effect_allele_frequency", "p_value", "FreqSE", "MinFreq",
             "MaxFreq", "Direction", "n", "hm_code"]
STUDY_ROWS = [
    ["1", "1", "A", "G", "0.1", "0.1", "0.3", "0.5", "0.02", "0.25", "0.4", "?+-", "9", "11"],
    ["1", "2", "A", "G", "0.1", "0.1", "0.3", "0.5", "NA", "NA", "0.4", "???+", "9", "10"],
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

    def rescue(self, hdr, rows, recommendation="swap_alleles"):
        raw, out = self.tmp / "raw.tsv.gz", self.tmp / "out.tsv.gz"
        write_gz(raw, hdr, rows)
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

    def test_study_only_columns_stay_exactly_as_raw(self):
        """MinFreq/MaxFreq/Direction follow the study's allele, not the harmonised one."""
        got = self.rescue(STUDY_HDR, STUDY_ROWS)
        for name in ("FreqSE", "MinFreq", "MaxFreq", "Direction", "n", "hm_code"):
            self.assertEqual(self.col(STUDY_HDR, got, name), self.col(STUDY_HDR, STUDY_ROWS, name), name)
        self.assertEqual(self.col(STUDY_HDR, got, "beta"), ["-0.1", "-0.1"])

    def test_hm_code_and_unlisted_columns_are_byte_identical(self):
        for hdr, rows, changed in (
            (BETA_HDR, BETA_ROWS, {"beta", "effect_allele_frequency"}),
            (STUDY_HDR, STUDY_ROWS, {"beta", "effect_allele_frequency"}),
            (OR_HDR, OR_ROWS, {"beta", "effect_allele_frequency", "odds_ratio", "ci_upper", "ci_lower"}),
        ):
            got = self.rescue(hdr, rows)
            for name in hdr:
                if name not in changed:
                    self.assertEqual(self.col(hdr, got, name), self.col(hdr, rows, name), name)

    def test_complement_is_exact_for_any_digits(self):
        row = list(BETA_ROWS[0])
        row[BETA_HDR.index("effect_allele_frequency")] = "1e-30"
        got = self.rescue(BETA_HDR, [row])
        self.assertEqual(Fraction(self.col(BETA_HDR, got, "effect_allele_frequency")[0]),
                         1 - Fraction("1e-30"))
        row[BETA_HDR.index("effect_allele_frequency")] = "0.123456789012345678901234567890123"
        got = self.rescue(BETA_HDR, [row])
        self.assertEqual(Fraction(self.col(BETA_HDR, got, "effect_allele_frequency")[0]),
                         1 - Fraction("0.123456789012345678901234567890123"))

    def test_flip_frequency_is_unchanged(self):
        got = self.rescue(BETA_HDR, BETA_ROWS, "flip_frequency")
        for name in BETA_HDR:
            if name != "effect_allele_frequency":
                self.assertEqual(self.col(BETA_HDR, got, name), self.col(BETA_HDR, BETA_ROWS, name))
        self.assertEqual(self.col(BETA_HDR, got, "effect_allele_frequency"),
                         ["0.7", "0.85", "0.001", "", "NA"])

    def refuses(self, hdr, rows, **edit):
        rows = [list(r) for r in rows[:1]]
        for name, value in edit.items():
            rows[0][hdr.index(name)] = value
        with self.assertRaises(ValueError):
            self.rescue(hdr, rows)

    def test_an_unreadable_statistic_stops_the_rescue(self):
        self.refuses(BETA_HDR, BETA_ROWS, effect_allele_frequency="oops")
        self.refuses(BETA_HDR, BETA_ROWS, beta="oops")

    def test_inf_and_nan_stop_the_rescue(self):
        for bad in ("inf", "-inf", "NaN", "nan"):
            self.refuses(BETA_HDR, BETA_ROWS, beta=bad)
            self.refuses(BETA_HDR, BETA_ROWS, effect_allele_frequency=bad)
            self.refuses(OR_HDR, OR_ROWS, odds_ratio=bad)

    def test_a_frequency_outside_zero_one_stops_the_rescue(self):
        self.refuses(BETA_HDR, BETA_ROWS, effect_allele_frequency="1.2")
        self.refuses(BETA_HDR, BETA_ROWS, effect_allele_frequency="-0.1")

    def test_a_non_positive_odds_ratio_or_bound_stops_the_rescue(self):
        for bad in ("0", "-1.5"):
            self.refuses(OR_HDR, OR_ROWS, odds_ratio=bad)
            self.refuses(OR_HDR, OR_ROWS, ci_lower=bad)

    def test_a_wrong_field_count_stops_the_rescue(self):
        raw, out = self.tmp / "raw.tsv.gz", self.tmp / "out.tsv.gz"
        write_gz(raw, BETA_HDR, [BETA_ROWS[0][:-1]])
        with self.assertRaises(ValueError):
            rescue_file(raw, out, "swap_alleles")

    def test_a_file_with_ci_but_no_odds_ratio_is_refused(self):
        hdr = [h for h in OR_HDR if h != "odds_ratio"]
        rows = [[v for h, v in zip(OR_HDR, r) if h != "odds_ratio"] for r in OR_ROWS]
        with self.assertRaises(ValueError):
            self.rescue(hdr, rows)

    def test_a_column_in_neither_list_is_refused(self):
        """A column the rescue has not classified might depend on the effect allele."""
        for extra in ("zscore", "hm_beta", "OR", "z", "Stat", "ref_allele"):
            hdr = BETA_HDR + [extra]
            rows = [r + ["1"] for r in BETA_ROWS]
            with self.assertRaises(ValueError, msg=extra):
                self.rescue(hdr, rows)

    def test_an_excluded_study_is_never_regenerated_and_is_marked_in_the_manifest(self):
        """GCST90275159/60 (dummy beta and SE) are excluded for good (owner, 10 Nov 2026)."""
        from concurrent.futures import ThreadPoolExecutor
        from unittest import mock

        from manifest_generation.providers.gwas_catalog import create_rescued_gwas_files as mod

        self.assertEqual(mod.EXCLUDED_SWAP_STUDIES, {"GCST90275159", "GCST90275160"})
        t = self.tmp
        summary, readiness, base = t / "summary.tsv", t / "readiness.tsv", t / "base.tsv"
        ids = {"GCST90275159": "swap_alleles", "GCST90275160": "swap_alleles", "GCSTAAA": "swap_alleles", "GCSTBBB": "flip_frequency"}
        summary.write_text("analysis_id\tgate_reason\trecommendation\n" + "".join(
            f"{k}\teaf_orientation\t{v}\n" for k, v in ids.items()))
        readiness.write_text("analysis_id\tpublication_pmid\ttrait\tstudy_design\tsample_size\tdata_url\tyaml_url\tdata_file\tyaml_file\n" + "".join(
            f"{k}\t1\tT\tquantitative\t5\tu\ty\t/raw/{k}.tsv.gz\t/raw/{k}.yaml\n" for k in ids))
        cols = mod.MANIFEST_COLUMNS
        with open(base, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, delimiter="\t")
            w.writeheader()
            for k in ids:
                w.writerow({c: {"analysis_id": k, "status": "already_present", "data_file": f"/derived/{k}.tsv.gz",
                                "sha256": "old", "error": "rescued via swap_alleles"}.get(c, "") for c in cols})
        regenerated = []

        def fake(row, rec, write_base):
            regenerated.append(row["analysis_id"])
            return {c: {"analysis_id": row["analysis_id"], "status": "already_present", "sha256": "new",
                        "data_file": f"/derived/{row['analysis_id']}.tsv.gz", "error": f"rescued via {rec}"}.get(c, "") for c in cols}

        out = t / "out.tsv"
        for only in (["--only", "swap_alleles"], []):
            regenerated.clear()
            with mock.patch.object(mod, "SUMMARY_TSV", summary), mock.patch.object(mod, "SOURCE_READINESS_TSV", readiness), \
                 mock.patch.object(mod, "ProcessPoolExecutor", ThreadPoolExecutor), mock.patch.object(mod, "process_study", fake):
                mod.main(["--derived-base", str(t / "scratch"), "--manifest-out", str(out), "--base-manifest", str(base), *only])
            self.assertNotIn("GCST90275159", regenerated)
            self.assertNotIn("GCST90275160", regenerated)
            self.assertIn("GCSTAAA", regenerated)
            with open(out, newline="") as f:
                rows = {r["analysis_id"]: r for r in csv.DictReader(f, delimiter="\t")}
            self.assertEqual(set(rows), set(ids))
            for k in ("GCST90275159", "GCST90275160"):
                self.assertEqual(rows[k]["status"], "excluded")
                self.assertIn("dummy", rows[k]["error"])
                self.assertNotIn("rescued", rows[k]["error"])
                for c in ("data_file", "yaml_file", "sha256", "data_bytes", "yaml_bytes"):
                    self.assertEqual(rows[k][c], "")
            self.assertEqual(rows["GCSTAAA"]["status"], "already_present")

    def test_derived_base_without_manifest_out_is_refused(self):
        """A scratch run must not record its checksums in the live manifest.

        Everything the run could write is pointed into the temporary directory
        and the per-study work is stubbed, so a failure of this test cannot touch
        the real derived tree.
        """
        from unittest import mock

        from manifest_generation.providers.gwas_catalog import create_rescued_gwas_files as mod

        live = self.tmp / "live"
        calls = []
        with mock.patch.object(mod, "DERIVED_BASE", live), \
             mock.patch.object(mod, "MANIFEST_OUT", live / "manifest.tsv"), \
             mock.patch.object(mod, "ProcessPoolExecutor", side_effect=lambda *a, **k: calls.append(1)), \
             self.assertRaises(SystemExit) as raised:
            mod.main(["--derived-base", str(self.tmp / "scratch")])
        self.assertNotEqual(raised.exception.code, 0)
        self.assertEqual(calls, [])
        self.assertFalse((live / "manifest.tsv").exists())


if __name__ == "__main__":
    unittest.main()

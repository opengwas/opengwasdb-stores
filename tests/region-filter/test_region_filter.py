#!/usr/bin/env python3
"""The shared region filter: a release's region policy applied to one source file (#300).

Acquisition filters each file of a region-filtered release to the region policy
its release config declares, and always keeps the QC-panel positions, so
Analysis Resolution can still measure ancestry and phenotype SD. The policy is
the one the R ``gwas-ssf-ragged`` generator applied: a cis flank around the
Analysis's target (``cis_plus_signals``), a flank around every significant
hit outside it, and suggestive leads at least a minimum distance apart. The
filter keeps whole source rows, verbatim.

Every expected row here is worked out by hand from the fixture positions.

Run from the repository root:
    python3 tests/region-filter/test_region_filter.py
"""

from __future__ import annotations

import csv
import gzip
import tempfile
import unittest
from pathlib import Path

from manifest_generation import REPO_ROOT
from manifest_generation.region_filter import (
    QcPanel,
    RegionPolicy,
    TargetSpan,
    filter_file,
    load_qc_panel,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
QC_PANEL = REPO_ROOT / "resources/reference-resources/qc-panel-hg38/qc_panel.tsv"

HEADER = (
    "chromosome", "base_pair_location", "effect_allele", "other_allele", "beta",
    "standard_error", "effect_allele_frequency", "p_value", "variant_id", "rsid",
)

SIGNALS_ONLY = RegionPolicy(
    region_policy_id="sig-trans-1mb_suggestive-leads",
    association_coverage="signals_only",
    trans_flank_bp=1_000_000,
    significant_p=5e-8,
    suggestive_p=1e-5,
    suggestive_lead_min_distance_bp=100_000,
)
CIS_PLUS_SIGNALS = RegionPolicy(
    region_policy_id="cis-1mb_sig-trans-1mb_suggestive-leads",
    association_coverage="cis_plus_signals",
    trans_flank_bp=1_000_000,
    significant_p=5e-8,
    suggestive_p=1e-5,
    suggestive_lead_min_distance_bp=100_000,
    cis_flank_bp=1_000_000,
)
NO_PANEL = QcPanel(resource_id="fixture-panel", sha256="0" * 64, positions=frozenset())


def row(chromosome: str, position: int, p_value: str, rsid: str = "") -> str:
    rsid = rsid or f"rs{chromosome}_{position}"
    return "\t".join((chromosome, str(position), "A", "G", "0.1", "0.02", "0.3", p_value, rsid, rsid))


class RegionFilterTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_filter(self, rows, policy=SIGNALS_ONLY, panel=NO_PANEL, target=None):
        source = self.tmp / "source.h.tsv.gz"
        with gzip.open(source, "wt", encoding="utf-8") as fh:
            fh.write("\t".join(HEADER) + "\n" + "".join(line + "\n" for line in rows))
        destination = self.tmp / "filtered.tsv.gz"
        summary = filter_file(source, destination, policy, panel, target=target)
        with gzip.open(destination, "rt", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(lines[0], "\t".join(HEADER))
        return summary, lines[1:]

    def kept(self, lines) -> list[tuple[str, int]]:
        return [(line.split("\t")[0], int(line.split("\t")[1])) for line in lines]

    def test_the_cis_flank_keeps_every_row_within_the_flank_of_the_target(self) -> None:
        # Target chr2:3,000,000-3,050,000, flank 1 Mb: chr2:2,000,000-4,050,000, ends included.
        rows = [
            row("2", 1_999_999, "0.5"),
            row("2", 2_000_000, "0.5"),
            row("2", 3_020_000, "0.9"),
            row("2", 4_050_000, "0.5"),
            row("2", 4_050_001, "0.5"),
            row("3", 3_020_000, "0.5"),
        ]
        summary, lines = self.run_filter(
            rows, CIS_PLUS_SIGNALS, target=TargetSpan("2", 3_000_000, 3_050_000)
        )
        self.assertEqual(self.kept(lines), [("2", 2_000_000), ("2", 3_020_000), ("2", 4_050_000)])
        self.assertEqual((summary.input_rows, summary.retained_rows, summary.cis_rows), (6, 3, 3))
        # Kept rows are the source's own lines, byte for byte.
        self.assertEqual(lines[1], rows[2])

    def test_a_significant_hit_keeps_its_trans_flank(self) -> None:
        # Hit chr1:5,000,000 at p 1e-9, flank 1 Mb: chr1:4,000,000-6,000,000, ends included.
        # p exactly 5e-8 is significant (<=).
        rows = [
            row("1", 3_999_999, "0.5"),
            row("1", 4_000_000, "0.5"),
            row("1", 5_000_000, "1e-9"),
            row("1", 6_000_000, "0.5"),
            row("1", 6_000_001, "0.5"),
            row("7", 20_000_000, "5e-8"),
            row("7", 20_999_999, "0.5"),
            row("7", 21_000_001, "0.5"),
        ]
        summary, lines = self.run_filter(rows)
        self.assertEqual(
            self.kept(lines),
            [("1", 4_000_000), ("1", 5_000_000), ("1", 6_000_000), ("7", 20_000_000), ("7", 20_999_999)],
        )
        self.assertEqual(
            (summary.significant_trans_regions, summary.significant_trans_rows, summary.retained_rows),
            (2, 5, 5),
        )

    def test_a_significant_hit_inside_the_cis_window_opens_no_trans_flank(self) -> None:
        # Target chr2:3,000,000-3,000,100 with a 1 Mb cis flank: 2,000,000-4,000,100.
        # The hit at 3,900,000 is cis, so 4,000,101-4,900,000 is not kept.
        rows = [row("2", 3_900_000, "1e-20"), row("2", 4_500_000, "0.5")]
        summary, lines = self.run_filter(
            rows, CIS_PLUS_SIGNALS, target=TargetSpan("2", 3_000_000, 3_000_100)
        )
        self.assertEqual(self.kept(lines), [("2", 3_900_000)])
        self.assertEqual((summary.cis_rows, summary.significant_trans_regions), (1, 0))

    def test_suggestive_leads_are_the_strongest_rows_at_least_the_minimum_distance_apart(self) -> None:
        # chr4, strongest first: 300,000 (1e-7) is a lead; 380,000 (2e-7) is 80 kb
        # from it, so not; 200,000 (3e-6) is exactly 100 kb away, which is not
        # more than the minimum, so not; 500,000 and 500,500 tie at 4e-6 and the
        # earlier row wins; 700,000 (1.1e-5) is above the suggestive threshold.
        # chr9: 2,500,000 (6e-8) is suggestive but inside the flank of the
        # significant hit at 2,000,000, so it is a trans row, not a lead.
        rows = [
            row("4", 200_000, "3e-6"),
            row("4", 300_000, "1e-7"),
            row("4", 380_000, "2e-7"),
            row("4", 500_500, "4e-6", rsid="rs_first_tie"),
            row("4", 500_000, "4e-6", rsid="rs_second_tie"),
            row("4", 700_000, "1.1e-5"),
            row("9", 2_000_000, "1e-10"),
            row("9", 2_500_000, "6e-8"),
        ]
        summary, lines = self.run_filter(rows)
        self.assertEqual(
            self.kept(lines),
            [("4", 300_000), ("4", 500_500), ("9", 2_000_000), ("9", 2_500_000)],
        )
        self.assertEqual((summary.suggestive_leads, summary.significant_trans_rows), (2, 2))

    def test_qc_panel_positions_are_kept_whatever_their_p_value(self) -> None:
        # The panel names chr1:15,903 and chr22:500; the source spells chr1 as
        # "chr1". Nothing here is a signal, so the panel alone keeps rows.
        panel = QcPanel("qc-panel-hg38", "f" * 64, frozenset({("1", 15_903), ("22", 500)}))
        rows = [
            row("chr1", 15_903, "0.99"),
            row("chr1", 15_904, "0.99"),
            row("22", 500, "0.4"),
            row("21", 500, "0.4"),
        ]
        summary, lines = self.run_filter(rows, panel=panel)
        self.assertEqual(self.kept(lines), [("chr1", 15_903), ("22", 500)])
        self.assertEqual((summary.qc_panel_rows, summary.retained_rows), (2, 2))

    def test_trans_flanks_merge_when_they_overlap_or_touch(self) -> None:
        # Flanks of 1 Mb. chr5: hits 1,500,000 apart overlap -> one region.
        # chr6: hits 2,000,000 apart touch (the second starts where the first
        # ends) -> one region. chr8: hits 2,000,002 apart leave a one-base gap
        # at 4,000,001 -> two regions, and the gap row is not kept. chr10: hits
        # 2,000,001 apart give adjacent flanks, which R counts as two regions
        # (it merges only a span starting at or before the current end).
        rows = [
            row("5", 3_000_000, "1e-9"),
            row("5", 4_500_000, "1e-9"),
            row("6", 3_000_000, "1e-9"),
            row("6", 5_000_000, "1e-9"),
            row("8", 3_000_000, "1e-9"),
            row("8", 4_000_000, "0.5"),
            row("8", 4_000_001, "0.5"),
            row("8", 5_000_002, "1e-9"),
            row("10", 3_000_000, "1e-9"),
            row("10", 5_000_001, "1e-9"),
        ]
        summary, lines = self.run_filter(rows)
        self.assertEqual(summary.significant_trans_regions, 6)
        self.assertNotIn(("8", 4_000_001), self.kept(lines))
        self.assertEqual(summary.retained_rows, 9)

    def test_the_filter_keeps_exactly_the_rows_r_kept_from_a_chen_original(self) -> None:
        """Parity with the R ``gwas-ssf-ragged`` filter on real source rows.

        The fixture is 54 verbatim rows of chromosome 11 of GCST90199622's
        harmonised original (Chen 2023, OGS-00006): every row with
        ``p_value <= 1e-5`` -- one significant hit, and suggestive rows with
        tied p-values 6 to 21 kb apart -- so its regions are the full file's,
        plus 10 rows R kept, 10 it dropped and 6 at QC-panel positions.
        ``r_kept.tsv`` is the 35 of them in R's filtered file for that
        Analysis, whose sha256 is the one OGS-00006's ``filter_summary.tsv``
        recorded (/data/opengwasdb/raw/ebi-chen-10/filtered/).
        """
        fixture = FIXTURES / "chen-GCST90199622-chr11"
        panel = load_qc_panel(QC_PANEL, "qc-panel-hg38")
        destination = self.tmp / "GCST90199622.filtered.tsv.gz"
        summary = filter_file(fixture / "source.h.tsv.gz", destination, SIGNALS_ONLY, panel)
        with gzip.open(destination, "rt", encoding="utf-8") as fh:
            ours = list(csv.DictReader(fh, delimiter="\t"))
        with (fixture / "r_kept.tsv").open(encoding="utf-8") as fh:
            r_kept = list(csv.DictReader(fh, delimiter="\t"))

        def keys(rows):
            return [
                (row["chromosome"], int(row["base_pair_location"]), row["effect_allele"],
                 row["other_allele"], row["rsid"])
                for row in rows
            ]

        self.assertEqual(len(r_kept), 35)
        self.assertEqual(keys(ours), keys(r_kept))
        self.assertEqual(summary.retained_rows, 35)

    def test_rows_without_a_position_or_p_value_are_dropped(self) -> None:
        panel = QcPanel("qc-panel-hg38", "f" * 64, frozenset({("1", 100)}))
        rows = [row("1", 100, "NA"), row("1", 100, ""), row("1", 100, "0.5")]
        summary, lines = self.run_filter(rows, panel=panel)
        self.assertEqual((summary.input_rows, lines), (1, [rows[2]]))


if __name__ == "__main__":
    unittest.main()

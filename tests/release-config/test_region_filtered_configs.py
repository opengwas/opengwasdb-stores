#!/usr/bin/env python3
"""Chen 2023 and Sun 2018 need only a release config (#300, epic #282).

The two region-filtered releases the retired R generator built, OGS-00006 and
OGS-00007, are each a release config over the GWAS Catalog's connectors. Each
config loads, declares its region policy, and its Selection takes exactly the
Analyses its committed bundle holds -- 1,400 for Chen, 10 for Sun -- from the
Source Metadata Table it names, in-process.

Run from the repository root:
    python3 tests/release-config/test_region_filtered_configs.py
"""

from __future__ import annotations

import csv
import unittest

from manifest_generation import REPO_ROOT
from manifest_generation.freeze import load_frozen_connectors
from manifest_generation.release_config import load_release_config
from manifest_generation.selection import select
from manifest_generation.source_metadata_table import load_source_metadata_table

RAGGED = REPO_ROOT / "resources/generators/gwas-catalog-ragged"


def bundle_analyses(store_id: str) -> list[str]:
    with (REPO_ROOT / "stores" / store_id / "analyses.tsv").open(encoding="utf-8") as fh:
        return [row["analysis_id"] for row in csv.DictReader(fh, delimiter="\t")]


class RegionFilteredConfigTests(unittest.TestCase):
    def selected(self, config) -> list[str]:
        """The Selection over the collection's table where it is on this host, else the frozen one."""
        table_path = config.connectors.source_metadata_table
        if table_path.is_file():
            table = load_source_metadata_table(table_path)
        else:
            table, _manifest = load_frozen_connectors(config.inventory_provenance_path)
        return [row["source_analysis_id"] for row in select(table, config.selection)]

    def test_chen_selects_ogs_00006s_1400_analyses_and_keeps_signals_only(self) -> None:
        config = load_release_config(RAGGED / "release-chen-2023-european.yaml", REPO_ROOT)
        self.assertEqual(config.region_policy.association_coverage, "signals_only")
        self.assertIsNone(config.region_policy.cis_flank_bp)
        selected = self.selected(config)
        self.assertEqual(len(selected), 1400)
        self.assertEqual(sorted(selected), sorted(bundle_analyses("OGS-00006")))

    def test_sun_selects_ogs_00007s_10_analyses_with_a_cis_window(self) -> None:
        config = load_release_config(RAGGED / "release-sun-2018-pilot-10.yaml", REPO_ROOT)
        self.assertEqual(config.region_policy.association_coverage, "cis_plus_signals")
        self.assertEqual(config.region_policy.cis_flank_bp, 1_000_000)
        self.assertTrue(config.trust_source_ancestry_without_af)
        self.assertEqual(config.maf_threshold, "0")
        self.assertEqual(config.method_tiers["quantitative"].original_sd_method, "declared_standardised")
        selected = self.selected(config)
        self.assertEqual(selected, bundle_analyses("OGS-00007"))


if __name__ == "__main__":
    unittest.main()

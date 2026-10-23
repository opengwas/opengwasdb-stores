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
from dataclasses import replace

from manifest_generation import REPO_ROOT
from manifest_generation.freeze import load_frozen_connectors
from manifest_generation.region_filtered_acquisition import frozen_evidence, region_lineage_problems
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
        self.assertEqual(config.region_policy.association_coverage, "cis_and_signals")
        self.assertEqual(config.region_policy.cis_flank_bp, 1_000_000)
        self.assertTrue(config.trust_source_ancestry_without_af)
        self.assertEqual(config.maf_threshold, "0")
        self.assertEqual(config.method_tiers["quantitative"].original_sd_method, "declared_standardised")
        selected = self.selected(config)
        self.assertEqual(selected, bundle_analyses("OGS-00007"))

    def test_the_frozen_lineages_retired_spelling_is_the_configs_coverage(self) -> None:
        """Sun's frozen lineage says `cis_plus_signals`; the config says `cis_and_signals` (#341).

        A frozen Acquisition Manifest is checksummed evidence and is never
        rewritten, so preflight's "filtered to exactly this policy" comparison
        reads the retired spelling as the current one. The same lineage read as
        another coverage is still refused, so the alias is not a wildcard.
        """
        config = load_release_config(RAGGED / "release-sun-2018-pilot-10.yaml", REPO_ROOT)
        self.assertEqual(config.region_policy.association_coverage, "cis_and_signals")
        provenance = config.inventory_provenance_path
        table, manifest = load_frozen_connectors(provenance)
        evidence = frozen_evidence(provenance)
        self.assertEqual(region_lineage_problems(config, table, manifest, evidence), [])

        other = replace(
            config,
            region_policy=replace(
                config.region_policy, association_coverage="signals_only", cis_flank_bp=None
            ),
        )
        problems = region_lineage_problems(other, table, manifest, evidence)
        self.assertTrue(
            any("association_coverage" in problem and "signals_only" in problem for problem in problems),
            problems,
        )

    def test_every_frozen_file_was_filtered_to_its_configs_policy_and_qc_panel(self) -> None:
        """ADR 0036 (#300): a region-filtered release's files keep the QC panel, without exception.

        Held to the check preflight and rendering apply (#282 review): each
        file's policy, QC panel and, for Sun's cis policy, target span are the
        config's, and both evidence files are bound. Chen's are its committed
        bundle's own sidecars, so its legacy acquisition passes as it is.
        """
        for name in ("release-chen-2023-european.yaml", "release-sun-2018-pilot-10.yaml"):
            with self.subTest(config=name):
                config = load_release_config(RAGGED / name, REPO_ROOT)
                provenance = config.inventory_provenance_path
                table, manifest = load_frozen_connectors(provenance)
                self.assertEqual(
                    region_lineage_problems(config, table, manifest, frozen_evidence(provenance)), []
                )


if __name__ == "__main__":
    unittest.main()

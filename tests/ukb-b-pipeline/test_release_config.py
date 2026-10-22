#!/usr/bin/env python3
"""The ukb-b release configs: they load, and their Selection is OGS-00009 (#289).

The seam is the release config itself. ``release-full.yaml`` loads, and its
Selection selects the 2,514 Analyses of the committed frozen Source Inventory
in-process; its method tiers reproduce every declaration the accepted
``stores/OGS-00009/analyses.tsv`` carries, the 1,846 case-control Analyses
included; and its Build Recipe shape, options, ``post`` and ``hm3`` Indexed
Variant Subset are the ones that bundle's ``build.yaml`` declares. The ten-Analysis
probe config loads too, and selects exactly the ten #219 probed.

Expected values are read from the accepted bundle and the frozen inventory --
never from the config under test -- so this suite would fail if either drifted.

Run from the repository root:
    python3 tests/ukb-b-pipeline/test_release_config.py
"""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

import yaml

from manifest_generation import REPO_ROOT
from manifest_generation.freeze import load_frozen_connectors
from manifest_generation.release_config import load_release_config
from manifest_generation.selection import select
from manifest_generation.source_inventory import read_inventory

FULL_CONFIG = REPO_ROOT / "resources" / "generators" / "ukb-b" / "release-full.yaml"
PROBE_CONFIG = REPO_ROOT / "resources" / "generators" / "ukb-b" / "release-probe-10.yaml"
ACCEPTED_BUILD = REPO_ROOT / "stores" / "OGS-00009" / "build.yaml"
ACCEPTED_ANALYSES = REPO_ROOT / "stores" / "OGS-00009" / "analyses.tsv"

PROBE_IDS = [
    "ukb-b-10001",
    "ukb-b-10002",
    "ukb-b-10003",
    "ukb-b-10008",
    "ukb-b-10011",
    "ukb-b-1004",
    "ukb-b-10054",
    "ukb-b-10787",
    "ukb-b-11842",
    "ukb-b-19953",
]


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


class FullReleaseConfigTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_release_config(FULL_CONFIG, REPO_ROOT)
        cls.table, cls.manifest = load_frozen_connectors(cls.config.inventory_provenance_path)
        cls.accepted = read_tsv(ACCEPTED_ANALYSES)

    def test_the_selection_selects_the_frozen_membership(self) -> None:
        selected = select(self.table, self.config.selection)
        self.assertEqual(len(selected), 2514)
        self.assertEqual(len(read_inventory(self.config.inventory_path)), 2514)
        self.assertEqual(
            sorted(row["source_analysis_id"] for row in selected),
            sorted(row["source_analysis_id"] for row in self.accepted),
        )

    def test_the_frozen_files_are_the_accepted_ones(self) -> None:
        # Sizes, not a re-read of 425 GB: the inventory's recorded bytes are the
        # accepted bundle's recorded bytes, so this snapshot is that membership.
        inventory = {row.analysis_id: row for row in read_inventory(self.config.inventory_path)}
        accepted = {row["source_analysis_id"]: row for row in self.accepted}
        self.assertEqual(set(inventory), set(accepted))
        mismatched = [
            analysis_id
            for analysis_id, row in inventory.items()
            if row.data_file != accepted[analysis_id]["source_file"]
            or row.sha256 != accepted[analysis_id]["checksum"]
            or row.data_bytes != str(accepted[analysis_id]["size_bytes"])
        ]
        self.assertEqual(mismatched, [])

    def test_the_method_tiers_reproduce_every_accepted_declaration(self) -> None:
        # Every accepted row's Study Design comes from the frozen Source Metadata
        # Table, which is what the tier keys on; the four declarations must then
        # be the accepted ones. The 1,846 case-control rows are sd too.
        designs = {
            row["source_analysis_id"]: row["study_design"] for row in self.table.rows
        }
        by_design: dict[str, int] = {}
        for row in self.accepted:
            design = designs[row["source_analysis_id"]]
            tier = self.config.method_tiers[design]
            self.assertEqual(
                (
                    row["stored_effect_scale"],
                    row["original_effect_scale"],
                    row["original_sd_method"],
                    row["sample_size_kind"],
                ),
                (
                    tier.stored_effect_scale,
                    tier.original_effect_scale,
                    tier.original_sd_method,
                    tier.sample_size_kind,
                ),
                f"{row['source_analysis_id']} ({design})",
            )
            by_design[design] = by_design.get(design, 0) + 1
        self.assertEqual(by_design, {"case-control": 1846, "quantitative": 668})

    def test_the_build_recipe_is_the_accepted_one(self) -> None:
        accepted = yaml.safe_load(ACCEPTED_BUILD.read_text(encoding="utf-8"))
        self.assertEqual(self.config.build.command, accepted["build"]["command"])
        self.assertEqual(self.config.build.options, accepted["build"]["options"])
        self.assertEqual(self.config.build.post, accepted["post"])
        self.assertEqual(self.config.build.layout, accepted["layout"])
        self.assertEqual(
            [dict(entry) for entry in self.config.indexed_subsets or []],
            accepted["indexed_subsets"],
        )

    def test_the_release_is_a_european_dense_release_of_the_gwas_vcf_collection(self) -> None:
        self.assertEqual(self.config.source_collection_id, "opengwas-gwas-vcf")
        self.assertEqual(self.config.build.layout, "dense")
        self.assertEqual(self.config.build.options["source-reader-capability"], "opengwasdb.gwas-vcf")
        self.assertEqual(self.config.build.options["source-assembly"], "hg19")
        self.assertEqual(self.config.selection_superpopulation, "EUR")
        self.assertEqual(self.config.selection.expect_rows, 2514)
        self.assertEqual(self.config.method_tiers["case-control"].sample_size_kind, "case_control")
        # No reference-AF fallback is declared: every tier declares its scale.
        self.assertEqual(self.config.effect_scale_reference_resources, ())


class ProbeConfigTest(unittest.TestCase):
    def test_the_probe_selects_its_ten_and_nothing_else(self) -> None:
        config = load_release_config(PROBE_CONFIG, REPO_ROOT)
        table, _manifest = load_frozen_connectors(config.inventory_provenance_path)
        selected = [row["source_analysis_id"] for row in select(table, config.selection)]
        self.assertEqual(sorted(selected), sorted(PROBE_IDS))
        self.assertEqual(len(read_inventory(config.inventory_path)), 10)
        self.assertEqual(config.selection.expect_rows, 10)
        self.assertEqual(config.label, "ukb-b-probe-10")

    def test_the_probe_resolves_into_its_own_work_root(self) -> None:
        config = load_release_config(PROBE_CONFIG, REPO_ROOT)
        full = load_release_config(FULL_CONFIG, REPO_ROOT)
        self.assertNotEqual(config.work_root, full.work_root)
        self.assertNotEqual(config.inventory_snapshot_id, full.inventory_snapshot_id)


class TempRegistryLayoutTest(unittest.TestCase):
    def test_the_config_points_at_tracked_connectors_and_inventories(self) -> None:
        # A committed config must be replayable from a checkout: its connectors
        # are tracked, and its frozen snapshot sits with the other inventories.
        config = load_release_config(FULL_CONFIG, REPO_ROOT)
        self.assertIsNotNone(config.connectors)
        for path in (config.connectors.source_metadata_table, config.connectors.acquisition_manifest):
            self.assertTrue(path.is_file(), path)
            self.assertTrue(path.is_relative_to(REPO_ROOT), path)
        self.assertTrue(config.inventory_path.is_relative_to(REPO_ROOT))
        self.assertTrue(config.inventory_provenance_path.is_file())


if __name__ == "__main__":
    unittest.main()

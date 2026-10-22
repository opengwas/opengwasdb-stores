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
import json
import unittest
from pathlib import Path

import yaml

from manifest_generation import REPO_ROOT
from manifest_generation.candidate_workflow import (
    declared_setting_checksums,
    load_candidate_configuration,
    render_build_yaml,
    resolver_settings,
)
from manifest_generation.freeze import load_frozen_connectors
from manifest_generation.release_config import load_release_config
from manifest_generation.selection import select
from manifest_generation.source_inventory import read_inventory, sha256_file

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


def option(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


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

    def test_the_rendered_recipe_is_the_accepted_build_yaml(self) -> None:
        # The strongest form of "reproduces OGS-00009's complete recipe": the
        # pipeline's own rendering of this config, for that Store id, is the
        # committed build.yaml -- both blocks, the hm3 subset, the post steps and
        # the hg19 source assembly, document for document.
        candidate = load_candidate_configuration(FULL_CONFIG, REPO_ROOT)
        self.assertEqual(
            render_build_yaml("OGS-00009", candidate),
            yaml.safe_load(ACCEPTED_BUILD.read_text(encoding="utf-8")),
        )

    def test_the_cross_build_lift_chain_is_declared_and_passed(self) -> None:
        # The VCFs are GRCh37 and the ancestry mixture is GRCh38, so the resolver
        # is passed the declared chain (#292); without it a cross-build source is
        # refused. Its declared checksum joins the settings digest. Composing the
        # settings reads no host file, so this passes on a runner with no /data.
        self.assertEqual(self.config.liftover_chain_resource_id, "liftover-hg19-to-hg38")
        chain = self.config.reference_resources["liftover-hg19-to-hg38"]
        self.assertEqual(chain.kind, "liftover_chain")
        self.assertEqual(
            str(chain.location), "/data/opengwasdb/reference/liftover/hg19ToHg38.over.chain.gz"
        )
        self.assertIn("liftover-hg19-to-hg38", self.config.required_resource_ids)
        candidate = load_candidate_configuration(FULL_CONFIG, REPO_ROOT)
        settings = resolver_settings(candidate)
        self.assertEqual(option(settings, "--chain-file"), str(chain.location))
        self.assertEqual(option(settings, "--reference-build"), "GRCh38")
        # What the settings digest folds in is the declaration's checksum, which
        # is the one fact about the chain composing a run's key needs.
        declaration = yaml.safe_load(
            (REPO_ROOT / "resources/reference-resources/liftover-hg19-to-hg38/resource.yaml").read_text()
        )
        self.assertEqual(
            declared_setting_checksums(candidate), {str(chain.location): declaration["sha256"]}
        )

    def test_the_completion_reference_is_ogs_00009_s(self) -> None:
        completion = self.config.completion
        self.assertIsNotNone(completion)
        self.assertEqual(completion.command, "complete-dense")
        self.assertEqual(completion.options, {"n-workers": 32})
        self.assertEqual(
            dict(completion.reference),
            {
                "resource_id": "ukb-hg38-eur-ld",
                "qualification": "qualified",
                "activation": "active",
            },
        )
        # The panel, its ancestry and its declared status are the Reference
        # Resource declaration's, read through the config's entry for it.
        self.assertEqual(completion.panel.location, "/data/opengwasdb/reference/ld")
        self.assertEqual(completion.panel.ancestry, "EUR")
        self.assertEqual(completion.panel.status, "available")
        self.assertIn("ukb-hg38-eur-ld", self.config.required_resource_ids)
        self.assertEqual(self.config.build.completion_state, "reference_completed")

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

    def test_the_probe_declares_the_lift_chain(self) -> None:
        # The probe's subject is ancestry measured on a GRCh37 source against the
        # GRCh38 reference, which needs the chain as much as the full release.
        config = load_release_config(PROBE_CONFIG, REPO_ROOT)
        self.assertEqual(config.liftover_chain_resource_id, "liftover-hg19-to-hg38")

    def test_the_probe_resolves_into_its_own_work_root(self) -> None:
        config = load_release_config(PROBE_CONFIG, REPO_ROOT)
        full = load_release_config(FULL_CONFIG, REPO_ROOT)
        self.assertNotEqual(config.work_root, full.work_root)
        self.assertNotEqual(config.inventory_snapshot_id, full.inventory_snapshot_id)


class ReceiptsTest(unittest.TestCase):
    """The committed receipts are #219's, by the hash its committed receipt records."""

    def test_the_committed_checksum_receipt_is_the_recorded_one(self) -> None:
        receipt = json.loads(
            (REPO_ROOT / "resources" / "inventories" / "ukb-b-restored-2026-10-11.receipt.json").read_text(
                encoding="utf-8"
            )
        )
        committed = REPO_ROOT / "resources" / "generators" / "ukb-b" / "receipts" / (
            "ukb-b-restored-2026-10-11.checksums.json"
        )
        self.assertEqual(sha256_file(committed), receipt["checksum_receipt_sha256"])
        self.assertEqual(
            len(json.loads(committed.read_text(encoding="utf-8"))["entries"]), 2514
        )

    def test_the_committed_lineage_names_the_committed_receipts(self) -> None:
        lineage = yaml.safe_load(
            (
                REPO_ROOT
                / "resources"
                / "generators"
                / "ukb-b"
                / "connectors"
                / "acquisition-manifest.meta.yaml"
            ).read_text(encoding="utf-8")
        )
        for name in ("checksums", "integrity", "verification"):
            recorded = lineage["evidence"][name]
            path = REPO_ROOT / recorded["path"]
            self.assertTrue(path.is_file(), recorded["path"])
            self.assertEqual(sha256_file(path), recorded["sha256"], recorded["path"])
        self.assertEqual(lineage["readiness_counts"], {"ok": 2514})


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

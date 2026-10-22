#!/usr/bin/env python3
"""The FinnGen R13 release config loads, and its Selection selects the release (#288).

The connector files and the frozen Source Inventory this config names are the
committed ones under `resources/inventories/`, frozen from
`/data/opengwasdb/raw/finngen-r13/connectors/2026-10-21/` -- the endpoint
metadata table the FinnGen adapter wrote and the Acquisition Manifest the
acquisition script wrote. The test is in-process: the schema gate, the
Selection, the frozen provenance and the pipeline's reading of the config.

Run from the repository root:
    python3 tests/finngen-r13-pipeline/test_release_config.py
"""

from __future__ import annotations

import hashlib
import importlib.resources
import unittest
from collections import Counter
from pathlib import Path

import yaml

from manifest_generation import REPO_ROOT
from manifest_generation.candidate_workflow import (
    load_candidate_configuration,
    render_build_yaml,
    render_column_maps,
)
from manifest_generation.release_config import load_release_config
from manifest_generation.selection import select
from manifest_generation.source_metadata_table import load_source_metadata_table

CONFIG = REPO_ROOT / "resources/generators/finngen-r13/release-r13-full.yaml"
PROBE_CONFIG = REPO_ROOT / "resources/generators/finngen-r13/release-r13-probe-10.yaml"
SNAPSHOT_ID = "finngen-r13-full-2026-10-22"
PROBE_SNAPSHOT_ID = "finngen-r13-probe-10-2026-10-23"
#: The built-in Column Map this collection is read through (opengwasdb#281, #292).
BUILTIN_MAP = "finngen-r13"
#: The frozen Source Metadata Table, which the config's provenance names.
FROZEN_TABLE = REPO_ROOT / "resources/inventories" / f"{SNAPSHOT_ID}.source-metadata.tsv"
PROVENANCE = REPO_ROOT / "resources/inventories" / f"{SNAPSHOT_ID}.meta.yaml"
#: The 10-Analysis probe's own frozen Source Metadata Table.
PROBE_FROZEN_TABLE = (
    REPO_ROOT / "resources/inventories" / f"{PROBE_SNAPSHOT_ID}.source-metadata.tsv"
)
#: The probe's ten: seven case-control endpoints and all three IRN quantitative ones.
PROBE_ANALYSES = [
    "AB1_ACTINOMYCOSIS",
    "ALCODELIRIUM",
    "CD2_INSITU_BREAST",
    "K11_IBD_STRICT",
    "M13_ARTHROPATHIES",
    "RX_STATIN",
    "T2D",
    "BMI_IRN",
    "HEIGHT_IRN",
    "WEIGHT_IRN",
]


def packaged_map_sha256(name: str) -> str:
    """The built-in map's checksum, from the bytes the pinned package ships."""
    data = importlib.resources.files("opengwasdb.readers").joinpath(
        f"column_maps/{name}.toml"
    ).read_bytes()
    return hashlib.sha256(data).hexdigest()


class FinnGenProbeReleaseConfigTest(unittest.TestCase):
    """The 10-Analysis probe config: the full release's shape, a probe's Selection."""

    def setUp(self) -> None:
        self.config = load_release_config(PROBE_CONFIG, REPO_ROOT)

    def test_it_selects_ten_mixed_design_analyses_of_the_frozen_table(self) -> None:
        table = load_source_metadata_table(PROBE_FROZEN_TABLE)
        selected = select(table, self.config.selection)
        # The table's own order is the provider manifest's; the Selection's list
        # is a declaration, not an ordering.
        self.assertEqual(
            [row["source_analysis_id"] for row in selected],
            [
                row["source_analysis_id"]
                for row in table.rows
                if row["source_analysis_id"] in set(PROBE_ANALYSES)
            ],
        )
        self.assertEqual(
            {row["source_analysis_id"] for row in selected}, set(PROBE_ANALYSES)
        )
        self.assertEqual(self.config.selection.as_dict(),
                         {"analyses": PROBE_ANALYSES, "expect_rows": 10})
        self.assertEqual(
            Counter(row["study_design"] for row in selected),
            {"case-control": 7, "quantitative": 3},
        )
        # The full release's rows, connectors and tiers: only the Selection and
        # the snapshot differ, so the probe measures the release's own bundle.
        full = load_candidate_configuration(CONFIG, REPO_ROOT)
        probe = load_candidate_configuration(PROBE_CONFIG, REPO_ROOT)
        self.assertEqual(probe.reader_capability, full.reader_capability)
        self.assertEqual(probe.base.column_map_builtin, full.base.column_map_builtin)
        self.assertEqual(probe.defaults, full.defaults)
        self.assertEqual(
            render_build_yaml("OGS-99288", probe)["column_map"],
            render_build_yaml("OGS-00017", full)["column_map"],
        )


class FinnGenReleaseConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_release_config(CONFIG, REPO_ROOT)

    def test_it_loads_with_the_releases_identity_and_collection(self) -> None:
        self.assertEqual(self.config.label, "r13-full")
        self.assertEqual(self.config.access_posture, "public")
        self.assertEqual(self.config.source_collection_id, "finngen-r13")
        self.assertEqual(self.config.inventory_snapshot_id, SNAPSHOT_ID)
        self.assertEqual(self.config.inventory_directory, REPO_ROOT / "resources/inventories")

    def test_its_selection_is_every_endpoint_of_the_frozen_table(self) -> None:
        table = load_source_metadata_table(FROZEN_TABLE)
        selected = select(table, self.config.selection)
        self.assertEqual(len(selected), 2_754)
        self.assertEqual(
            Counter(row["study_design"] for row in selected),
            {"case-control": 2_751, "quantitative": 3},
        )
        self.assertEqual(
            {row["source_analysis_id"] for row in selected if row["study_design"] == "quantitative"},
            {"BMI_IRN", "HEIGHT_IRN", "WEIGHT_IRN"},
        )
        # The Selection grammar has no prefix form: every row, nothing dropped.
        self.assertEqual(self.config.selection.as_dict(), {"all": True})

    def test_the_frozen_provenance_is_this_collections_and_this_selection(self) -> None:
        provenance = yaml.safe_load(PROVENANCE.read_text(encoding="utf-8"))
        self.assertEqual(provenance["frozen_by"], "manifest_generation.freeze")
        self.assertEqual(provenance["source_collection_id"], "finngen-r13")
        self.assertEqual(provenance["selection"], {"all": True})
        self.assertEqual(provenance["rows"], 2_754)
        self.assertEqual(provenance["ready_rows"], 2_754)
        self.assertEqual(provenance["readiness_counts"], {"already_present": 2_754})
        self.assertEqual(
            provenance["study_design_counts"], {"case-control": 2_751, "quantitative": 3}
        )
        # No unrecorded file, no duplicate content: intent and acquisition match.
        self.assertEqual(provenance["duplicate_content_groups"], [])
        self.assertEqual(provenance["acquisition_manifest"]["unselected_entries"], 0)
        self.assertIsNotNone(provenance["acquisition_manifest"]["original"]["pass_lineage"])

    def test_the_pipeline_reads_finngens_rows_through_the_built_in_column_map(self) -> None:
        """Only the Column Map reader streams metrics; the FinnGen reader cannot (#288)."""
        config = load_candidate_configuration(CONFIG, REPO_ROOT)
        self.assertEqual(config.reader_capability, "opengwasdb.column-map")
        self.assertEqual(config.base.column_map_builtin, BUILTIN_MAP)
        self.assertIsNone(config.base.column_map_path)
        # The bundle binds it by name and the package's checksum, and carries no
        # file: the bytes are the pinned OpenGWASDB package's own.
        build = render_build_yaml("OGS-00017", config)
        self.assertEqual(
            build["column_map"],
            {"name": BUILTIN_MAP, "sha256": packaged_map_sha256(BUILTIN_MAP)},
        )
        self.assertEqual(build["build"]["options"]["source-reader-capability"],
                         "opengwasdb.column-map")
        self.assertEqual(render_column_maps(config), {})

    def test_the_pipeline_reads_finngens_method_tiers(self) -> None:
        candidate = load_candidate_configuration(CONFIG, REPO_ROOT)
        self.assertEqual(candidate.target_ancestry, "EUR")
        self.assertEqual(candidate.layout, "dense")
        self.assertEqual(candidate.build_command, "build-dense-vcf")
        self.assertEqual(
            {
                design: (
                    tier.stored_effect_scale, tier.original_effect_scale,
                    tier.original_sd_method, tier.sample_size_kind,
                )
                for design, tier in candidate.base.method_tiers.items()
            },
            {
                # The IRN quantitative endpoints are standardised by construction,
                # so the tier declares the scale rather than estimating a SD.
                "quantitative": ("sd", "sd", "declared_standardised", "total"),
                "case-control": ("log_or", "log_or", "binary_trait", "case_control"),
            },
        )
        self.assertEqual(
            [entry["name"] for entry in candidate.indexed_subsets or ()], ["hm3"]
        )
        # No Store Composition Rule and no MAF floor: OGS-00017 is one Store.
        self.assertFalse(candidate.require_maf_filtered)
        self.assertIsNone(candidate.maf_threshold)

    def test_the_collection_wide_defaults_are_finnGens(self) -> None:
        candidate = load_candidate_configuration(CONFIG, REPO_ROOT)
        self.assertEqual(candidate.defaults["source_genome_build"], "GRCh38")
        self.assertEqual(candidate.defaults["license"], "FinnGen public data")
        self.assertEqual(candidate.defaults["consortium"], "FinnGen")
        self.assertEqual(candidate.defaults["sample_size_scope"], "analysis_level")

    def test_the_connectors_are_the_read_only_files_the_freeze_froze(self) -> None:
        provenance = yaml.safe_load(PROVENANCE.read_text(encoding="utf-8"))
        self.assertEqual(
            self.config.connectors.source_metadata_table,
            Path(provenance["source_metadata_table"]["original"]["path"]),
        )
        self.assertEqual(
            self.config.connectors.acquisition_manifest,
            Path(provenance["acquisition_manifest"]["original"]["path"]),
        )


if __name__ == "__main__":
    unittest.main()

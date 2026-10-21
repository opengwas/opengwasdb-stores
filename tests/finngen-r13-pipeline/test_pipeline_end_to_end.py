#!/usr/bin/env python3
"""Three FinnGen-shaped Analyses through the shared pipeline, end to end (#288).

The source connectors are a FinnGen endpoint manifest's shape: a Source
Metadata Table the FinnGen adapter writes (case-control endpoints with counts,
``Finnish`` as the source ancestry label, GRCh38) and an Acquisition Manifest
the FinnGen acquisition script writes (one provider file per endpoint, no
per-Analysis metadata sidecar). They go through the one entry point --
``prepare``, ``resolve`` against the stage tests' fake resolver, ``render`` and
``emit`` -- into a temporary registry, so this suite pins only that the
pipeline needs nothing FinnGen-specific beyond its connectors and its release
config.

The fixture machinery is the phase-b-candidate suite's, loaded by path because
its directory is not an importable package (see ``tests/packaging/test_imports``):
its fake resolver, its CLI invocation and its temporary-directory conventions.

Run from the repository root:
    python3 tests/finngen-r13-pipeline/test_pipeline_end_to_end.py
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from ogstores import bundle as bundle_module

REPO_ROOT = Path(__file__).resolve().parents[2]
STAGE_TEST = REPO_ROOT / "tests" / "phase-b-candidate" / "test_phase_b_candidate.py"

STORE_ID = "OGS-99188"
SNAPSHOT_ID = "finngen-r13-fixture-2026-10-21"
TRAIT_TABLE_RESOURCE_ID = "canonical-trait-mapping-efo"
#: The one fixture row shape: a case-control FinnGen endpoint.
ANALYSES = [
    # (phenocode, phenotype, n_cases, n_controls)
    ("AB1_ACTINOMYCOSIS", "Actinomycosis", "132", "433757"),
    ("M13_ARTHROPATHIES", "Arthropathies", "500", "400000"),
    ("RX_STATIN", "Statin purchases", "200000", "200000"),
]
#: The provider manifest's ``category``, which the adapter carries as the Store's
#: ``analysis_group_id`` (#288).
CATEGORIES = {
    "AB1_ACTINOMYCOSIS": "I Certain infectious and parasitic diseases (AB1_)",
    "M13_ARTHROPATHIES": "XIII Diseases of the musculoskeletal system and connective tissue (M13_)",
    "RX_STATIN": "XIX Injury, poisoning and certain other consequences of external causes (S19_)",
}
TRAIT_TABLE_TEXT = (
    "trait_label\ttrait_ontology_id\ttrait_ontology_label\n"
    "weight\tEFO:0004338\tbody weight\n"
)


def _stage_module():
    """The phase-b-candidate stage test, loaded for its fixture machinery."""
    spec = importlib.util.spec_from_file_location("p288_stage_fixture", STAGE_TEST)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve their annotations through sys.modules during exec.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FinnGenEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.stage = _stage_module()

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="finngen-e2e-")
        self.tmp = Path(self._tmp.name)
        self.fixture = self.build_fixture()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- the fixture ------------------------------------------------------

    def build_fixture(self):
        """Write the two connector files, the reference resources and a release config."""
        stage = self.stage
        root = self.tmp
        reference = root / "reference"
        reference.mkdir(parents=True)
        (reference / "ref_freqs.tsv.gz").write_bytes(b"fixture reference\n")
        (reference / "ancestry_groups.tsv").write_text(
            "fine_group\tsuper_population\nEUR_A\tEUR\n", encoding="utf-8"
        )
        trait_table_dir = root / "resources" / "reference-resources" / TRAIT_TABLE_RESOURCE_ID
        trait_table_dir.mkdir(parents=True)
        (trait_table_dir / "resource.yaml").write_text(
            f"resource_id: {TRAIT_TABLE_RESOURCE_ID}\nkind: trait_ontology_mapping\nversion: 1\n"
            f"location: resources/reference-resources/{TRAIT_TABLE_RESOURCE_ID}/mapping.tsv\n",
            encoding="utf-8",
        )
        (trait_table_dir / "mapping.tsv").write_text(TRAIT_TABLE_TEXT, encoding="utf-8")

        source_dir = root / "source"
        source_dir.mkdir()
        acquisition_rows = []
        table_rows = []
        for phenocode, phenotype, n_cases, n_controls in ANALYSES:
            body = f"FinnGen fixture rows for {phenocode}\n".encode()
            path = source_dir / f"finngen_R13_{phenocode}.gz"
            path.write_bytes(body)
            acquisition_rows.append({
                "source_analysis_id": phenocode,
                "status": "already_present",
                "location": str(path),
                "bytes": str(len(body)),
                "sha256": hashlib.sha256(body).hexdigest(),
                "source_url": f"https://storage.googleapis.com/finngen-public-data-r13/summary_stats/"
                              f"finngen_R13_{phenocode}.gz",
            })
            table_rows.append({
                "source_analysis_id": phenocode,
                "analysis_id": f"finngen-r13-{phenocode}",
                "analysis_group_id": CATEGORIES[phenocode],
                "source_trait_label": phenotype,
                "source_trait_id": phenocode,
                "source_ancestry_label": "Finnish",
                "source_superpopulation": "EUR",
                "study_design": "case-control",
                "sample_size": str(int(n_cases) + int(n_controls)),
                "n_cases": n_cases,
                "n_controls": n_controls,
            })
        table_header = [
            "source_analysis_id", "analysis_id", "analysis_group_id",
            "source_trait_label", "source_trait_id",
            "source_ancestry_label", "source_superpopulation", "study_design",
            "sample_size", "n_cases", "n_controls",
        ]
        acquisition_header = [
            "source_analysis_id", "status", "location", "bytes", "sha256", "source_url",
        ]
        table_path = root / "finngen-r13.source-metadata.tsv"
        stage._write_tsv(table_path, table_header, table_rows)
        acquisition_path = root / "finngen-r13-full.acquisition-manifest.tsv"
        stage._write_tsv(acquisition_path, acquisition_header, acquisition_rows)

        inventory_dir = root / "inventory"
        inventory_dir.mkdir()
        work_root = root / "work"
        registry_root = root / "stores"
        registry_root.mkdir()
        config = {
            "release_config_version": 1,
            "label": "r13-fixture",
            "access_posture": "public",
            "description": "three FinnGen-shaped fixture endpoints",
            "source_collection": {
                "source_collection_id": "finngen-r13",
                "connectors": {
                    "source_metadata_table": str(table_path),
                    "acquisition_manifest": str(acquisition_path),
                },
                "inventory": {"snapshot_id": SNAPSHOT_ID, "directory": str(inventory_dir)},
            },
            "selection": {"all": True},
            "build": {
                "layout": "dense",
                "completion_state": "observed_only",
                "command": "build-dense-vcf",
                "options": {
                    "source-reader-capability": "opengwasdb.finngen-r13",
                    "source-assembly": "hg38",
                },
                "post": {"top_hits": True, "overview": True},
            },
            "defaults": {
                "source_genome_build": "GRCh38",
                "license": "FinnGen public data",
                "consortium": "FinnGen",
                "sample_size_scope": "analysis_level",
                "by_study_design": {
                    "quantitative": {
                        "stored_effect_scale": "sd",
                        "original_effect_scale": "sd",
                        "original_sd_method": "declared_standardised",
                        "sample_size_kind": "total",
                    },
                    "case-control": {
                        "stored_effect_scale": "log_or",
                        "original_effect_scale": "log_or",
                        "original_sd_method": "binary_trait",
                        "sample_size_kind": "case_control",
                    },
                },
            },
            "release_policy": {"selection_superpopulation": "EUR"},
            "reference_resources": [
                {
                    "resource_id": "fixture-ancestry-mixture",
                    "kind": "ancestry_mixture",
                    "location": str(reference / "ref_freqs.tsv.gz"),
                    "fine_group_map": str(reference / "ancestry_groups.tsv"),
                },
                {
                    "resource_id": TRAIT_TABLE_RESOURCE_ID,
                    "kind": "trait_ontology_mapping",
                    "location": str(trait_table_dir),
                },
            ],
            "ancestry_assignment": {
                "reference_resource_id": "fixture-ancestry-mixture",
                "maf_floor": 0.01,
                "extraction_panel": None,
                "gates": {"tau": 0.50, "delta": 0.20, "n_min": 5000, "residual_max": 0.06},
            },
            "effect_scale_validation": {"reference_resources": []},
            "trait_ontology_mapping": {"reference_resource_id": TRAIT_TABLE_RESOURCE_ID},
            "runtime": {"cores": 1, "min_free_gb": 0},
            "output": {"work_root": str(work_root)},
        }
        config_path = root / "release-r13-fixture.yaml"
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        outcomes_path = root / "outcomes.json"
        outcomes_path.write_text(json.dumps({}), encoding="utf-8")
        return stage.Fixture(
            root=root,
            config_path=config_path,
            inventory_path=inventory_dir / f"{SNAPSHOT_ID}.tsv",
            provenance_path=inventory_dir / f"{SNAPSHOT_ID}.meta.yaml",
            source_metadata_table_path=table_path,
            acquisition_manifest_path=acquisition_path,
            outcomes_path=outcomes_path,
            registry_root=registry_root,
            work_root=work_root,
            trait_table_dir=trait_table_dir,
            resolution_root=root / "resolution",
        )

    # -- the run ----------------------------------------------------------

    def test_three_finnGen_analyses_go_through_prepare_resolve_render_and_emit(self) -> None:
        result = self.stage._run_cli(self.fixture, "--stage", "all", store_id=STORE_ID)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        run_root = self.fixture.work_root / STORE_ID

        # prepare: the resolver manifest is FinnGen's reader, tier and counts.
        manifest = self.stage._read_tsv(run_root / "resolver" / "analyses.tsv")
        self.assertEqual([row["analysis_id"] for row in manifest],
                         [phenocode for phenocode, *_ in ANALYSES])
        for row in manifest:
            self.assertEqual(row["source_reader_capability"], "opengwasdb.finngen-r13")
            self.assertEqual(row["stored_effect_scale"], "log_or")
            self.assertEqual(row["original_sd_method"], "binary_trait")
        self.assertEqual(
            [row["sample_size"] for row in manifest], ["433889", "400500", "400000"]
        )
        # resolve: the fake resolver's receipt names every record it kept.
        receipt = json.loads((run_root / "resolver" / "resolution_receipt.json").read_text())
        self.assertEqual(sorted(receipt["records"]), [phenocode for phenocode, *_ in ANALYSES])

        bundle_dir = self.fixture.registry_root / STORE_ID
        self.assertTrue((bundle_dir / "release.yaml").is_file())
        release = yaml.safe_load((bundle_dir / "release.yaml").read_text())
        self.assertEqual(release["status"], "candidate")
        self.assertEqual(release["label"], "r13-fixture")

        analyses = self.stage._read_tsv(bundle_dir / "analyses.tsv")
        self.assertEqual([row["source_analysis_id"] for row in analyses],
                         [phenocode for phenocode, *_ in ANALYSES])
        # The Store's Analysis identity is the connector's, not the resolver's
        # key: a namespaced id and the provider manifest's category (#288).
        self.assertEqual(
            [row["analysis_id"] for row in analyses],
            [f"finngen-r13-{phenocode}" for phenocode, *_ in ANALYSES],
        )
        self.assertEqual(
            [row["analysis_group_id"] for row in analyses],
            [CATEGORIES[phenocode] for phenocode, *_ in ANALYSES],
        )
        for row in analyses:
            self.assertEqual(row["exclude_from_build"], "")
            self.assertEqual(row["source_reader_capability"], "opengwasdb.finngen-r13")
            self.assertEqual(row["stored_effect_scale"], "log_or")
            self.assertEqual(row["original_sd_method"], "binary_trait")
            self.assertEqual(row["sample_size_kind"], "case_control")
            self.assertEqual(row["source_ancestry_label"], "Finnish")
            self.assertEqual(row["source_genome_build"], "GRCh38")
            self.assertEqual(row["license"], "FinnGen public data")
            self.assertEqual(row["consortium"], "FinnGen")
            self.assertEqual(row["trait_ontology_mapping_method"], "unmapped")
            self.assertEqual(row["source_file"],
                             str(self.fixture.root / "source" /
                                 f"finngen_R13_{row['source_analysis_id']}.gz"))
        self.assertEqual([(row["n_cases"], row["n_controls"]) for row in analyses],
                         [("132", "433757"), ("500", "400000"), ("200000", "200000")])

        self.assertEqual(
            bundle_module.check(
                bundle_module.load(STORE_ID, registry_root=self.fixture.registry_root),
                registry_root=self.fixture.registry_root,
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()

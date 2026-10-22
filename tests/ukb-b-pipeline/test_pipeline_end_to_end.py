#!/usr/bin/env python3
"""Three ukb-b-shaped Analyses through the shared pipeline, end to end (#289).

The connectors are ukb-b's shape: the Source Metadata Table the ukb-b adapter
writes (case-control traits with their counts, quantitative traits with their
totals, the provider's ``European`` label, GRCh37) and the Acquisition Manifest
the ukb-b writer derives from the recorded identity receipts (one VCF per
Analysis, its provider JSON as the sidecar). They go through the one entry
point -- ``prepare``, ``resolve`` against the stage tests' fake resolver,
``render`` and ``emit`` -- into a temporary registry, so this suite pins only
that the pipeline needs nothing ukb-b-specific beyond its connectors and its
release config.

What it pins about ukb-b's release shape: the resolver is given the GWAS-VCF
reader and the hg19 source assembly; both method tiers declare the SD scale
(``sd`` / ``declared_standardised``), so a case-control Analysis is included on
a declared scale rather than excluded for want of a phenotype SD; and the
emitted bundle passes ``bundle.check()``.

The fixture machinery is the phase-b-candidate suite's, loaded by path because
its directory is not an importable package (see ``tests/packaging/test_imports``):
its fake resolver, its CLI invocation and its temporary-directory conventions.

Run from the repository root:
    python3 tests/ukb-b-pipeline/test_pipeline_end_to_end.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from ogstores import bundle as bundle_module

from ukb_b_fixture import ANALYSES, UkbFixture, build_fixture

REPO_ROOT = Path(__file__).resolve().parents[2]
STAGE_TEST = REPO_ROOT / "tests" / "phase-b-candidate" / "test_phase_b_candidate.py"

STORE_ID = "OGS-99189"
#: The fixture's case-control Analyses, which the SD tier includes on its
#: declared scale; the third is quantitative.
CASE_CONTROL = ("ukb-b-10001", "ukb-b-10054")


def _stage_module():
    """The phase-b-candidate stage test, loaded for its fixture machinery."""
    spec = importlib.util.spec_from_file_location("p289_stage_fixture", STAGE_TEST)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve their annotations through sys.modules during exec.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class UkbPipelineEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.stage = _stage_module()

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ukb-b-e2e-")
        self.tmp = Path(self._tmp.name)
        self.ukb: UkbFixture = build_fixture(self.tmp)
        self.fixture = self.stage.Fixture(
            root=self.ukb.root,
            config_path=self.ukb.config_path,
            inventory_path=self.ukb.inventory_dir / f"{self.ukb.snapshot_id}.tsv",
            provenance_path=self.ukb.inventory_dir / f"{self.ukb.snapshot_id}.meta.yaml",
            source_metadata_table_path=self.ukb.source_metadata_table,
            acquisition_manifest_path=self.ukb.acquisition_manifest,
            outcomes_path=self.ukb.outcomes_path,
            registry_root=self.ukb.registry_root,
            work_root=self.ukb.work_root,
            trait_table_dir=self.ukb.trait_table_dir,
            resolution_root=self.ukb.resolution_root,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_three_ukb_analyses_go_through_prepare_resolve_render_and_emit(self) -> None:
        result = self.stage._run_cli(self.fixture, "--stage", "all", store_id=STORE_ID)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        run_root = self.ukb.work_root / STORE_ID

        # prepare: the resolver manifest is ukb-b's reader, assembly and tier.
        manifest = self.stage._read_tsv(run_root / "resolver" / "analyses.tsv")
        self.assertEqual(
            [row["analysis_id"] for row in manifest], [analysis_id for analysis_id, _d, _b in ANALYSES]
        )
        for row in manifest:
            self.assertEqual(row["source_reader_capability"], "opengwasdb.gwas-vcf")
            self.assertEqual(row["source_genome_build"], "GRCh37")
            self.assertEqual(row["stored_effect_scale"], "sd")
            self.assertEqual(row["original_sd_method"], "declared_standardised")

        # resolve: the fake resolver's receipt names every record it kept.
        receipt = json.loads((run_root / "resolver" / "resolution_receipt.json").read_text())
        self.assertEqual(
            sorted(receipt["records"]), sorted(analysis_id for analysis_id, _d, _b in ANALYSES)
        )

        bundle_dir = self.ukb.registry_root / STORE_ID
        release = yaml.safe_load((bundle_dir / "release.yaml").read_text())
        self.assertEqual(release["status"], "candidate")
        self.assertEqual(release["label"], "ukb-b-fixture")

        analyses = self.stage._read_tsv(bundle_dir / "analyses.tsv")
        self.assertEqual(
            [row["source_analysis_id"] for row in analyses],
            [analysis_id for analysis_id, _d, _b in ANALYSES],
        )
        by_id = {row["source_analysis_id"]: row for row in analyses}
        for row in analyses:
            self.assertEqual(row["exclude_from_build"], "")
            self.assertEqual(row["stored_effect_scale"], "sd")
            self.assertEqual(row["original_effect_scale"], "sd")
            self.assertEqual(row["original_sd_method"], "declared_standardised")
            self.assertEqual(row["original_sd"], "")
            self.assertEqual(row["source_genome_build"], "GRCh37")
            self.assertEqual(row["license"], "UK Biobank terms of access")
            self.assertEqual(row["consortium"], "MRC-IEU")
            self.assertEqual(row["analysis_group_id"], "ukb-b")
            self.assertEqual(
                row["source_file"], str(self.ukb.source_dir / f"{row['source_analysis_id']}.vcf.gz")
            )

        # Trait Ontology Mapping is the shared step's (#286): the fixture's
        # canonical table maps one of the three source labels and leaves the
        # other two unmapped.
        self.assertEqual(
            {analysis_id: row["trait_ontology_mapping_method"] for analysis_id, row in by_id.items()},
            {
                "ukb-b-10001": "unmapped",
                "ukb-b-10054": "unmapped",
                "ukb-b-10787": "canonical_table_lookup",
            },
        )

        # The 1,846 case-control Analyses of OGS-00009 carry sample_size_kind
        # `case_control` and their counts; this is what the pipeline emits for a
        # case-control Analysis whose tier declares the SD scale. #289's report
        # records the difference, so a change here is a decision, not a drift.
        self.assertEqual(by_id["ukb-b-10001"]["sample_size_kind"], "case_control")
        self.assertEqual(by_id["ukb-b-10001"]["n_cases"], "")
        self.assertEqual(by_id["ukb-b-10001"]["n_controls"], "")
        self.assertEqual(by_id["ukb-b-10001"]["sample_size"], "463010")
        self.assertEqual(by_id["ukb-b-10787"]["sample_size_kind"], "total")
        self.assertEqual(by_id["ukb-b-10787"]["sample_size"], "461950")

        self.assertEqual(
            bundle_module.check(
                bundle_module.load(STORE_ID, registry_root=self.ukb.registry_root),
                registry_root=self.ukb.registry_root,
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()

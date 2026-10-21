#!/usr/bin/env python3
"""A region-filtered release carries its filtering's plan and counts (#300).

Issue #300: ``sparse_regions.tsv`` and ``filter_summary.tsv`` are carried as
today. Acquisition records them beside the Acquisition Manifest and checksums
them in its run record (``<manifest-stem>.meta.yaml``), which freeze checksums
into the inventory's provenance. The candidate carries the Selection's rows of
each, verbatim and in file order, into ``sidecars/``, after checking both
checksums; a file changed since is refused. A release whose acquisition
recorded none carries neither.

Run from the repository root:
    python3 tests/phase-b-candidate/test_acquisition_evidence.py
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import yaml

from manifest_generation.candidate_workflow import (
    CandidateError,
    acquisition_evidence,
    apply_release_policy,
    build_candidate_tables,
    load_candidate_configuration,
    render_validation_yaml,
)
from manifest_generation.source_inventory import sha256_file

from test_phase_b_candidate import (  # sibling suite's fixture helpers
    REPO_ROOT,
    FixtureBuilder,
    _synthetic_analysis_metadata,
    _synthetic_inventory_row,
    _synthetic_resolver_record,
)

SUMMARY = (
    "analysis_id\tstatus\tretained_rows\tchecksum\n"
    "GCST1\tok\t23593\t659bd0b8\n"
    "GCST2\tok\t15089\t2c1f4e0a\n"
)
REGIONS = (
    "analysis_id\tregion_id\tregion_kind\tchromosome\tstart\tend\tlead_variant_id\n"
    "GCST1\tGCST1__cis__001\tcis\t2\t171555373\t173628598\t\n"
    "GCST2\tGCST2__cis__001\tcis\t17\t49094737\t51120276\t\n"
    "GCST1\tGCST1__suggestive__001\tsuggestive_trans\t3\t5000\t5000\trs1\n"
)


class AcquisitionEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="acquisition-evidence-")
        self.tmp = Path(self._tmp.name)
        self.summary = self.tmp / "manifest.filter-summary.tsv"
        self.regions = self.tmp / "manifest.sparse-regions.tsv"
        self.summary.write_text(SUMMARY, encoding="utf-8")
        self.regions.write_text(REGIONS, encoding="utf-8")
        self.run = self.tmp / "manifest.meta.yaml"
        self.run.write_text(yaml.safe_dump({"evidence": {
            "filter_summary": {"path": str(self.summary), "sha256": sha256_file(self.summary)},
            "sparse_regions": {"path": str(self.regions), "sha256": sha256_file(self.regions)},
        }}), encoding="utf-8")
        self.provenance = self.write_provenance({"path": str(self.run), "sha256": sha256_file(self.run)})

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_provenance(self, pass_lineage) -> Path:
        path = self.tmp / "snapshot.meta.yaml"
        path.write_text(yaml.safe_dump(
            {"acquisition_manifest": {"original": {"pass_lineage": pass_lineage}}}
        ), encoding="utf-8")
        return path

    def test_the_selections_rows_are_carried_verbatim(self) -> None:
        self.assertEqual(
            acquisition_evidence(self.provenance, ["GCST1"]),
            {
                "filter_summary.tsv": "analysis_id\tstatus\tretained_rows\tchecksum\n"
                                      "GCST1\tok\t23593\t659bd0b8\n",
                "sparse_regions.tsv": "analysis_id\tregion_id\tregion_kind\tchromosome\tstart\tend"
                                      "\tlead_variant_id\n"
                                      "GCST1\tGCST1__cis__001\tcis\t2\t171555373\t173628598\t\n"
                                      "GCST1\tGCST1__suggestive__001\tsuggestive_trans\t3\t5000\t5000\trs1\n",
            },
        )

    def test_the_bundle_carries_and_reports_them(self) -> None:
        config = load_candidate_configuration(FixtureBuilder(self.tmp / "fixture").build().config_path, REPO_ROOT)
        row = replace(_synthetic_inventory_row(), analysis_id="GCST1")
        outcomes = apply_release_policy(
            [row], config, {"GCST1": replace(_synthetic_analysis_metadata(), analysis_id="GCST1")},
            {"GCST1": dict(_synthetic_resolver_record(), analysis_id="GCST1")},
        )
        tables = build_candidate_tables(
            inventory_rows=[row], outcomes=outcomes, config=config, index_summary={},
            acquisition_evidence=acquisition_evidence(self.provenance, ["GCST1"]),
        )
        self.assertEqual(sorted(tables.acquisition_evidence), ["filter_summary.tsv", "sparse_regions.tsv"])
        reports = render_validation_yaml(
            tables=tables, index_summary={}, validated_at="2026-10-21T00:00:00Z", validator_name="x",
            trait_ontology_mapping={},
        )["reports"]
        self.assertEqual(
            (reports["filter_summary"], reports["sparse_regions"]),
            ("sidecars/filter_summary.tsv", "sidecars/sparse_regions.tsv"),
        )

    def test_evidence_changed_since_the_freeze_is_refused(self) -> None:
        self.regions.write_text(REGIONS.replace("rs1", "rs2"), encoding="utf-8")
        with self.assertRaises(CandidateError) as caught:
            acquisition_evidence(self.provenance, ["GCST1"])
        self.assertIn(str(self.regions), str(caught.exception))

    def test_a_selected_analysis_acquisition_did_not_summarise_is_refused(self) -> None:
        with self.assertRaises(CandidateError) as caught:
            acquisition_evidence(self.provenance, ["GCST1", "GCST3"])
        self.assertIn("GCST3", str(caught.exception))

    def test_a_release_whose_acquisition_recorded_none_carries_none(self) -> None:
        self.assertEqual(acquisition_evidence(self.write_provenance(None), ["GCST1"]), {})


if __name__ == "__main__":
    unittest.main()

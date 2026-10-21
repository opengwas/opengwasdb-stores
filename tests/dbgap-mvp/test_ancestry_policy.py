#!/usr/bin/env python3
"""The MVP's two ancestry-policy decisions (#290), at the release-policy seam.

A Selection that names no single super-population -- ``multi-ancestry`` --
never assigns Assigned Ancestry: the fit is measured and kept, and no Analysis
is excluded for ancestry. Separately, a release may opt in to keeping an
Analysis the ancestry gates left *unassigned* as a recorded warning, while a
measured different super-population still excludes.

Both are declared in a release config and applied by the Quality Checks, so
this suite decides fixture Analyses through ``apply_release_policy`` and reads
the outcome and its checks; it needs no resolver and no association data.

Run from the repository root:
    python3 tests/dbgap-mvp/test_ancestry_policy.py
"""
from __future__ import annotations

import copy
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import yaml

# The sibling end-to-end suite builds the MVP-shaped fixture this policy runs on.
import test_end_to_end as end_to_end

from manifest_generation import REPO_ROOT
from manifest_generation.candidate_workflow import (
    AnalysisMetadata,
    apply_release_policy,
    load_candidate_configuration,
)
from manifest_generation.quality_checks import Result
from manifest_generation.release_config import ReleaseConfigError, load_release_config
from manifest_generation.source_inventory import SourceInventoryRow

GENERATORS = REPO_ROOT / "resources/generators/dbgap-mvp"


def _row(analysis_id: str, design: str = "case-control") -> SourceInventoryRow:
    return SourceInventoryRow(
        analysis_id=analysis_id,
        publication_pmid="39024449",
        trait="a fixture trait",
        study_design=design,
        sample_size="1000",
        readiness_status="ok",
        data_url="https://ftp.ncbi.nlm.nih.gov/dbgap/studies/phs002453/analyses/GIA/",
        yaml_url="",
        data_file=f"/mirror/{analysis_id}.tsv.gz",
        yaml_file="",
        data_bytes="1000",
        yaml_bytes="",
        sha256="",
        error="",
    )


def _metadata(
    analysis_id: str, *, superpopulation: str = "", label: str = "European"
) -> AnalysisMetadata:
    return AnalysisMetadata(
        analysis_id=analysis_id,
        source_label="a fixture trait",
        source_trait_ontology_id="",
        source_trait_ontology_label="",
        publication_pmid="39024449",
        first_author="",
        n_cases="100",
        n_controls="900",
        sample_size="1000",
        source_ancestry_label=label,
        source_superpopulation=superpopulation,
    )


def _record(analysis_id: str, *, assigned: str = "", gate_reason: str = "ok") -> dict:
    """A case-control Resolution Record whose ancestry result the caller sets."""
    return {
        "analysis_id": analysis_id,
        "status": "success",
        "ancestry": {
            "assigned_ancestry": assigned or None,
            "gate_reason": gate_reason,
            "eaf_orientation": "passed",
            "af_overlap": 50_000,
        },
        "diagnostics": {"build_eligible_rows": 1000, "canonical_rows_observed": 1000},
        "phenotype_sd": {
            "status": "skipped",
            "reason": "non_quantitative_effect_scale",
            "estimate": None,
        },
    }


class PolicyTestCase(unittest.TestCase):
    """A configuration built from the end-to-end fixture's document, re-policied."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="dbgap-ancestry-")
        self.root = Path(self._tmp.name)
        self.config_path = end_to_end.build(self.root)
        self.document = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def configuration(self, **policy):
        document = copy.deepcopy(self.document)
        document["release_policy"] = {
            "selection_superpopulation": "EUR",
            **{key: value for key, value in policy.items() if key != "selection_superpopulation"},
        }
        if "selection_superpopulation" in policy:
            document["release_policy"]["selection_superpopulation"] = policy[
                "selection_superpopulation"
            ]
        path = self.root / "policy.yaml"
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        return load_candidate_configuration(path, REPO_ROOT)

    def decide(self, config, record: dict, *, superpopulation: str = ""):
        analysis_id = str(record["analysis_id"])
        (outcome,) = apply_release_policy(
            [_row(analysis_id)],
            config,
            {analysis_id: _metadata(analysis_id, superpopulation=superpopulation)},
            {analysis_id: record},
        )
        findings = {finding.name: finding for finding in outcome.check_results}
        return outcome, findings


class MultiAncestrySelectionTests(PolicyTestCase):
    def test_a_multi_ancestry_selection_never_assigns_ancestry(self) -> None:
        """The measured fit is kept; the emitted ancestry stays blank, method unassigned."""
        config = self.configuration(selection_superpopulation="multi-ancestry")
        outcome, findings = self.decide(config, _record("GCST1", assigned="EUR"))
        self.assertTrue(outcome.included, outcome.exclusion_reason)
        self.assertEqual(outcome.assigned_ancestry, "")
        self.assertEqual(outcome.ancestry_assignment_method, "unassigned")
        self.assertIs(findings["assigned_ancestry"].result, Result.NOT_APPLICABLE)
        # The fit is still measured and recorded, not discarded.
        self.assertIn("assigned_ancestry=EUR", findings["assigned_ancestry"].measured)

    def test_a_multi_ancestry_selection_excludes_nothing_for_ancestry(self) -> None:
        """A different measured super-population, and no measurement, both stay in."""
        config = self.configuration(selection_superpopulation="multi-ancestry")
        for record in (
            _record("GCST1", assigned="AFR"),
            _record("GCST2", assigned="", gate_reason="margin"),
        ):
            with self.subTest(gate_reason=record["ancestry"]["gate_reason"]):
                outcome, findings = self.decide(config, record)
                self.assertTrue(outcome.included, outcome.exclusion_reason)
                self.assertEqual(outcome.assigned_ancestry, "")
                self.assertEqual(outcome.ancestry_assignment_method, "unassigned")
                self.assertIs(findings["assigned_ancestry"].result, Result.NOT_APPLICABLE)

    def test_a_multi_ancestry_selection_is_the_shipped_meta_config(self) -> None:
        config = load_candidate_configuration(GENERATORS / "release-meta.yaml", REPO_ROOT)
        self.assertEqual(config.target_ancestry, "multi-ancestry")
        self.assertEqual(config.base.selection.expect_rows, 1691)
        self.assertEqual(
            config.base.selection.as_dict()["where"]["store_key"],
            "dense__pmid-39024449__Multi-ancestry",
        )


class KeepUnassignedAncestryTests(PolicyTestCase):
    def test_an_unassigned_analysis_is_kept_as_a_recorded_warning(self) -> None:
        config = self.configuration(keep_unassigned_ancestry_as_warning=True)
        outcome, findings = self.decide(
            config, _record("GCST1", assigned="", gate_reason="margin")
        )
        self.assertTrue(outcome.included, outcome.exclusion_reason)
        self.assertEqual(outcome.assigned_ancestry, "")
        self.assertEqual(outcome.ancestry_assignment_method, "unassigned")
        self.assertIs(findings["assigned_ancestry"].result, Result.WARNING)
        self.assertIn("margin", findings["assigned_ancestry"].measured)

    def test_a_measured_different_superpopulation_still_excludes_with_the_opt_in(self) -> None:
        config = self.configuration(keep_unassigned_ancestry_as_warning=True)
        outcome, findings = self.decide(config, _record("GCST1", assigned="AFR"))
        self.assertFalse(outcome.included)
        self.assertEqual(outcome.exclusion_reason, "ancestry_not_eur")
        self.assertIs(findings["assigned_ancestry"].result, Result.FAILED)

    def test_without_the_opt_in_an_unassigned_analysis_still_excludes(self) -> None:
        config = self.configuration()
        outcome, findings = self.decide(
            config, _record("GCST1", assigned="", gate_reason="proportion")
        )
        self.assertFalse(outcome.included)
        self.assertEqual(outcome.exclusion_reason, "ancestry_unassigned")
        self.assertIs(findings["assigned_ancestry"].result, Result.FAILED)

    def test_the_shipped_amr_config_opts_in(self) -> None:
        config = load_candidate_configuration(GENERATORS / "release-amr.yaml", REPO_ROOT)
        self.assertTrue(config.keep_unassigned_ancestry_as_warning)
        self.assertEqual(config.target_ancestry, "AMR")


class PolicyGrammarTests(PolicyTestCase):
    def test_the_multi_ancestry_spelling_is_accepted_and_nothing_else_is(self) -> None:
        for spelling in ("multi-ancestry", "META", "Multiple"):
            with self.subTest(spelling=spelling):
                document = copy.deepcopy(self.document)
                document["release_policy"]["selection_superpopulation"] = spelling
                path = self.root / f"{spelling.replace('/', '-')}.yaml"
                path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
                if spelling == "multi-ancestry":
                    self.assertEqual(
                        load_release_config(path, REPO_ROOT).selection_superpopulation,
                        "multi-ancestry",
                    )
                else:
                    with self.assertRaisesRegex(ReleaseConfigError, "multi-ancestry"):
                        load_release_config(path, REPO_ROOT)

    def test_the_keep_opt_in_is_a_boolean(self) -> None:
        document = copy.deepcopy(self.document)
        document["release_policy"]["keep_unassigned_ancestry_as_warning"] = "yes"
        path = self.root / "bad-opt-in.yaml"
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        with self.assertRaisesRegex(ReleaseConfigError, "keep_unassigned_ancestry_as_warning"):
            load_release_config(path, REPO_ROOT)


if __name__ == "__main__":
    unittest.main()

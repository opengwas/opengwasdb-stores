#!/usr/bin/env python3
"""A declared effect scale is not a phenotype SD to estimate (#300).

A release whose quantitative tier declares its scale (``declared_standardised``,
Sun 2018's SomaScan pQTLs) asks the resolver for no estimate, and the resolver
says so: ``phenotype_sd`` is ``skipped`` with reason ``scale_already_declared``.
The ``phenotype_sd`` Quality Check is then ``not_applicable``, and the Analysis
is included with ``original_sd`` blank, as OGS-00007 carries it.

Run from the repository root:
    python3 tests/phase-b-candidate/test_declared_scale.py
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from manifest_generation.candidate_workflow import apply_release_policy, load_candidate_configuration
from manifest_generation.quality_checks import Result
from manifest_generation.source_inventory import MethodTier

from test_phase_b_candidate import (  # sibling suite's fixture helpers
    REPO_ROOT,
    FixtureBuilder,
    _synthetic_analysis_metadata,
    _synthetic_inventory_row,
    _synthetic_resolver_record,
)

DECLARED = MethodTier(
    study_design="quantitative",
    stored_effect_scale="sd",
    original_effect_scale="sd",
    original_sd_method="declared_standardised",
    sample_size_kind="total",
)


class DeclaredScaleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="declared-scale-")
        config = load_candidate_configuration(FixtureBuilder(Path(self._tmp.name)).build().config_path, REPO_ROOT)
        self.config = replace(config, base=replace(config.base, method_tiers={"quantitative": DECLARED}))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_declared_scale_is_not_applicable_and_the_analysis_is_included(self) -> None:
        row = _synthetic_inventory_row()
        record = _synthetic_resolver_record(
            phenotype_sd_status="skipped", phenotype_sd_reason="scale_already_declared"
        )
        (outcome,) = apply_release_policy(
            [row], self.config, {row.analysis_id: _synthetic_analysis_metadata()}, {row.analysis_id: record}
        )
        finding = {finding.name: finding for finding in outcome.check_results}["phenotype_sd"]
        self.assertIs(finding.result, Result.NOT_APPLICABLE)
        self.assertEqual(finding.measured, "status=skipped reason=scale_already_declared")
        self.assertTrue(outcome.included, outcome.exclusion_detail)
        self.assertEqual((outcome.original_sd, outcome.original_sd_method), ("", "declared_standardised"))


if __name__ == "__main__":
    unittest.main()

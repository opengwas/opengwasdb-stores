#!/usr/bin/env python3
"""Hermetic tests for Quality Checks (issue #285, ADR 0036).

A Quality Check tests one measurement of an Analysis against a threshold, with
its consequence (an exclusion or a warning) fixed when the check is declared.
These tests pin five seams:

1. the check registry: every reason that was an exclusion before #285 and is a
   measurement is declared once, with its kind and default threshold;
2. the release policy over records gives the outcomes and exclusion rows it gave
   before #285, pinned as literals from a known-good run (not recomputed);
3. a warning check registered from a test alone reaches ``validation.yaml`` and
   the Quality Check sidecar, with no change to the policy code;
4. a release override is recorded in the resolution receipt, and changing it
   makes the receipt stale;
5. the sidecar has one row per Analysis with its measured values, passing and
   excluded alike.

Run from the repository root:
    python3 tests/phase-b-candidate/test_quality_checks.py
"""

from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import yaml

from unittest import mock

from manifest_generation import quality_checks
from manifest_generation.candidate_workflow import (
    POLICY_EXCLUSION_CATEGORIES,
    RECEIPT_FILENAME,
    CandidateError,
    apply_release_policy,
    build_candidate_tables,
    compute_resolution_contract,
    controlled_exclusion_categories,
    derive_resolver_manifest,
    load_candidate_configuration,
    read_candidate_metadata,
    render_validation_yaml,
    verify_records,
)
from manifest_generation.quality_checks import Kind, Result, Stage, record_section
from manifest_generation.source_inventory import PreflightConfigError, read_inventory

from test_phase_b_candidate import (  # sibling suite's fixture helpers
    REPO_ROOT,
    STORE_ID,
    FixtureBuilder,
    _read_tsv,
    _run_cli,
    _synthetic_candidate_metadata,
    _synthetic_inventory_row,
    _synthetic_resolver_record,
)


# ---------------------------------------------------------------------------
# A known-good scenario set covering every pre-#285 exclusion path
# ---------------------------------------------------------------------------


def _record(analysis_id: str, **kwargs) -> dict:
    record = _synthetic_resolver_record(**kwargs)
    record["analysis_id"] = analysis_id
    return record


def _scenarios():
    """(inventory rows, metadata, records, placeholder listing) for every path.

    Each Analysis exercises one branch of the pre-#285 release policy, and a few
    exercise two at once so the precedence between them is pinned too.
    """
    rows = []
    metadata = {}
    records = {}

    def add(number, *, design="quantitative", sample_size="5000", n_cases="",
            n_controls="", record=None):
        analysis_id = f"GCST9001{number:04d}"
        rows.append(
            replace(
                _synthetic_inventory_row(study_design=design, sample_size=sample_size),
                analysis_id=analysis_id,
                data_file=f"/mirror/{analysis_id}.h.tsv.gz",
            )
        )
        metadata[analysis_id] = replace(
            _synthetic_candidate_metadata(
                sample_size=sample_size, n_cases=n_cases, n_controls=n_controls
            ),
            analysis_id=analysis_id,
        )
        if record is not None:
            records[analysis_id] = record(analysis_id)
        return analysis_id

    def diagnostics(**values):
        def build(analysis_id):
            record = _record(analysis_id)
            record["diagnostics"].update(values)
            return record
        return build

    def sd(status, reason=None, *, sd_value=None, estimate=True):
        def build(analysis_id):
            record = _record(
                analysis_id, phenotype_sd_status=status, phenotype_sd_reason=reason
            )
            if sd_value is not None:
                record["phenotype_sd"]["estimate"]["sd"] = sd_value
            if not estimate:
                record["phenotype_sd"]["estimate"] = None
            return record
        return build

    def both(first, second):
        def build(analysis_id):
            record = first(analysis_id)
            extra = second(analysis_id)
            record["ancestry"] = extra["ancestry"]
            return record
        return build

    def orientation(**kwargs):
        return lambda analysis_id: _record(analysis_id, **kwargs)

    add(1, record=_record)  # included quantitative
    add(2, design="case-control", n_cases="400", n_controls="600", record=_record)
    add(3, sample_size="0", record=_record)  # missing_sample_size
    add(4)  # no resolver record
    add(5, record=lambda aid: _record(aid, status="controlled_failure"))
    add(6, record=diagnostics(rows_read=100, canonical_rows_retained=0, build_eligible_rows=0))
    add(7, record=both(
        diagnostics(rows_read=100, canonical_rows_retained=100, build_eligible_rows=0),
        orientation(eaf_orientation="failed", gate_reason="eaf_orientation",
                    assigned_ancestry=None, eaf_orientation_r=-0.9),
    ))
    placeholder = add(8, record=_record)
    placeholder_and_orientation = add(9, record=orientation(
        eaf_orientation="failed", gate_reason="ok", assigned_ancestry="EUR",
        eaf_orientation_r=-0.2,
    ))
    add(10, record=orientation(eaf_orientation="failed", gate_reason="ok",
                               assigned_ancestry="EUR", eaf_orientation_r=-0.3))
    add(11, record=orientation(eaf_orientation="", gate_reason="eaf_orientation",
                               assigned_ancestry=None, eaf_orientation_r=-0.97))
    add(12, record=orientation(gate_reason="overlap", assigned_ancestry=None))
    add(13, record=orientation(assigned_ancestry="EAS"))
    add(14, record=sd("estimated", sd_value=0.0))
    add(15, record=sd("skipped", "no_reference_resource_for_ancestry", estimate=False))
    add(16, record=sd("skipped", None, estimate=False))
    add(17, record=sd("skipped", "no_usable_sample_size", estimate=False))
    add(18, record=sd("skipped", "some_new_reason", estimate=False))
    add(19, record=sd("unavailable", "no_qualifying_evidence", estimate=False))
    add(20, record=sd("unavailable", None, estimate=False))
    add(21, record=sd("", None, estimate=False))
    add(22, record=sd("estimated", estimate=False))
    add(23, design="case-control", record=_record)  # no counts
    add(24, design="case-control", n_cases="400", n_controls="600",
        record=orientation(eaf_orientation="failed", gate_reason="ok",
                           assigned_ancestry="EUR", eaf_orientation_r=-0.4))
    add(25, design="case-control", n_cases="400", n_controls="600",
        record=orientation(assigned_ancestry="EAS"))
    add(26, record=orientation(eaf_orientation="unverified", gate_reason="ok",
                               assigned_ancestry="EUR", eaf_orientation_r=float("nan")))
    add(27, record=sd("unavailable", "no_reference_resource_for_ancestry", estimate=False))
    add(28, record=diagnostics(build_eligible_rows=7))  # few rows, but above the default minimum
    add(29, record=sd("estimated", sd_value=2.5))
    add(30, record=orientation(assigned_ancestry="XYZ"))  # not a super-population code

    listing = {
        placeholder: "3 of 100 rows carry the 2.2e-308 placeholder",
        placeholder_and_orientation: "1 of 100 rows carry the 2.2e-308 placeholder",
    }
    return rows, metadata, records, listing


#: sidecars/exclusions.tsv for the scenario set, from the pre-#285 policy: derived by
#: running the base branch's code (feat/249-manifest-generation-package), never this one.
PINNED_EXCLUSIONS_TSV = """\
analysis_id	source_analysis_id	study_design	category	reason	detail	resolver_status	exclude_from_build
GCST90010003	GCST90010003	quantitative	metadata	missing_sample_size	resolved sample_size is empty or non-positive	success	true
GCST90010004	GCST90010004	quantitative	resolution	resolution_failed	no resolver record	missing	true
GCST90010005	GCST90010005	quantitative	resolution	resolution_failed	controlled failure	controlled_failure	true
GCST90010006	GCST90010006	quantitative	effect_scale	no_build_eligible_rows	resolver found no row with a finite effect and positive standard error (rows_read=100, canonical_rows_retained=0)	success	true
GCST90010007	GCST90010007	quantitative	effect_scale	no_build_eligible_rows	resolver found no row with a finite effect and positive standard error (rows_read=100, canonical_rows_retained=100)	success	true
GCST90010008	GCST90010008	quantitative	effect_scale	effect_placeholder_rows	3 of 100 rows carry the 2.2e-308 placeholder	success	true
GCST90010009	GCST90010009	quantitative	effect_scale	effect_placeholder_rows	1 of 100 rows carry the 2.2e-308 placeholder	success	true
GCST90010010	GCST90010010	quantitative	orientation	orientation_failure	eaf_orientation=failed gate_reason=ok r=-0.3	success	true
GCST90010011	GCST90010011	quantitative	orientation	orientation_failure	eaf_orientation=unrecorded gate_reason=eaf_orientation r=-0.97	success	true
GCST90010012	GCST90010012	quantitative	ancestry	ancestry_unassigned	gate_reason=overlap	success	true
GCST90010013	GCST90010013	quantitative	ancestry	ancestry_not_eur	assigned_ancestry=EAS (release target EUR)	success	true
GCST90010014	GCST90010014	quantitative	effect_scale	sd_no_qualifying_evidence	estimated phenotype SD 0.0 is not a positive finite number	success	true
GCST90010015	GCST90010015	quantitative	effect_scale	sd_no_reference_resource_for_ancestry	no_reference_resource_for_ancestry	success	true
GCST90010016	GCST90010016	quantitative	effect_scale	sd_no_reference_resource_for_ancestry	quantitative SD estimation was skipped	success	true
GCST90010017	GCST90010017	quantitative	effect_scale	sd_no_usable_sample_size	no_usable_sample_size	success	true
GCST90010018	GCST90010018	quantitative	effect_scale	sd_failed	some_new_reason	success	true
GCST90010019	GCST90010019	quantitative	effect_scale	sd_no_qualifying_evidence	no_qualifying_evidence	success	true
GCST90010020	GCST90010020	quantitative	effect_scale	sd_failed	no phenotype SD could be estimated	success	true
GCST90010021	GCST90010021	quantitative	effect_scale	sd_failed	phenotype_sd status='missing' reason='none'	success	true
GCST90010022	GCST90010022	quantitative	effect_scale	sd_failed	phenotype_sd status='estimated' reason='none'	success	true
GCST90010023	GCST90010023	case-control	metadata	missing_case_control_counts	n_cases=empty n_controls=empty	success	true
GCST90010024	GCST90010024	case-control	orientation	orientation_failure	eaf_orientation=failed gate_reason=ok r=-0.4	success	true
GCST90010025	GCST90010025	case-control	ancestry	ancestry_not_eur	assigned_ancestry=EAS (release target EUR)	success	true
GCST90010027	GCST90010027	quantitative	effect_scale	sd_no_reference_resource_for_ancestry	no_reference_resource_for_ancestry	success	true
GCST90010030	GCST90010030	quantitative	ancestry	ancestry_unassigned	assigned_ancestry 'XYZ' is not a super-population code	success	true
"""

#: (analysis_id, exclude_from_build, assigned_ancestry, ancestry_assignment_method,
#: original_sd, original_sd_method, inclusion_reason) from the pre-#285 policy.
PINNED_ANALYSES = """\
GCST90010001		EUR	af_assigned	1	estimated_from_source_maf	selected_ready_source
GCST90010002		EUR	af_assigned		binary_trait	selected_ready_source
GCST90010003	true		unassigned		estimated_from_source_maf	excluded: missing_sample_size: resolved sample_size is empty or non-positive
GCST90010004	true		unassigned		estimated_from_source_maf	excluded: resolution_failed: no resolver record
GCST90010005	true		unassigned		estimated_from_source_maf	excluded: resolution_failed: controlled failure
GCST90010006	true		unassigned		estimated_from_source_maf	excluded: no_build_eligible_rows: resolver found no row with a finite effect and positive standard error (rows_read=100, canonical_rows_retained=0)
GCST90010007	true		unassigned		estimated_from_source_maf	excluded: no_build_eligible_rows: resolver found no row with a finite effect and positive standard error (rows_read=100, canonical_rows_retained=100)
GCST90010008	true		unassigned		estimated_from_source_maf	excluded: effect_placeholder_rows: 3 of 100 rows carry the 2.2e-308 placeholder
GCST90010009	true		unassigned		estimated_from_source_maf	excluded: effect_placeholder_rows: 1 of 100 rows carry the 2.2e-308 placeholder
GCST90010010	true	EUR	unassigned		estimated_from_source_maf	excluded: orientation_failure: eaf_orientation=failed gate_reason=ok r=-0.3
GCST90010011	true		unassigned		estimated_from_source_maf	excluded: orientation_failure: eaf_orientation=unrecorded gate_reason=eaf_orientation r=-0.97
GCST90010012	true		unassigned		estimated_from_source_maf	excluded: ancestry_unassigned: gate_reason=overlap
GCST90010013	true	EAS	af_assigned		estimated_from_source_maf	excluded: ancestry_not_eur: assigned_ancestry=EAS (release target EUR)
GCST90010014	true	EUR	af_assigned		estimated_from_source_maf	excluded: sd_no_qualifying_evidence: estimated phenotype SD 0.0 is not a positive finite number
GCST90010015	true	EUR	af_assigned		estimated_from_source_maf	excluded: sd_no_reference_resource_for_ancestry: no_reference_resource_for_ancestry
GCST90010016	true	EUR	af_assigned		estimated_from_source_maf	excluded: sd_no_reference_resource_for_ancestry: quantitative SD estimation was skipped
GCST90010017	true	EUR	af_assigned		estimated_from_source_maf	excluded: sd_no_usable_sample_size: no_usable_sample_size
GCST90010018	true	EUR	af_assigned		estimated_from_source_maf	excluded: sd_failed: some_new_reason
GCST90010019	true	EUR	af_assigned		estimated_from_source_maf	excluded: sd_no_qualifying_evidence: no_qualifying_evidence
GCST90010020	true	EUR	af_assigned		estimated_from_source_maf	excluded: sd_failed: no phenotype SD could be estimated
GCST90010021	true	EUR	af_assigned		estimated_from_source_maf	excluded: sd_failed: phenotype_sd status='missing' reason='none'
GCST90010022	true	EUR	af_assigned		estimated_from_source_maf	excluded: sd_failed: phenotype_sd status='estimated' reason='none'
GCST90010023	true	EUR	af_assigned		binary_trait	excluded: missing_case_control_counts: n_cases=empty n_controls=empty
GCST90010024	true	EUR	unassigned		binary_trait	excluded: orientation_failure: eaf_orientation=failed gate_reason=ok r=-0.4
GCST90010025	true	EAS	af_assigned		binary_trait	excluded: ancestry_not_eur: assigned_ancestry=EAS (release target EUR)
GCST90010026		EUR	af_assigned	1	estimated_from_source_maf	selected_ready_source
GCST90010027	true	EUR	af_assigned		estimated_from_source_maf	excluded: sd_no_reference_resource_for_ancestry: no_reference_resource_for_ancestry
GCST90010028		EUR	af_assigned	1	estimated_from_source_maf	selected_ready_source
GCST90010029		EUR	af_assigned	2.5	estimated_from_source_maf	selected_ready_source
GCST90010030	true		unassigned		estimated_from_source_maf	excluded: ancestry_unassigned: assigned_ancestry 'XYZ' is not a super-population code
"""

PINNED_ANALYSES_COLUMNS = (
    "analysis_id",
    "exclude_from_build",
    "assigned_ancestry",
    "ancestry_assignment_method",
    "original_sd",
    "original_sd_method",
    "inclusion_reason",
)


class QualityCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="quality-checks-")
        self.fixture = FixtureBuilder(Path(self._tmp.name)).build()
        self.config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _policy(self, config=None):
        rows, metadata, records, listing = _scenarios()
        config = replace(config or self.config, effect_placeholder_exclusions=listing)
        outcomes = apply_release_policy(rows, config, metadata, records)
        tables = build_candidate_tables(
            inventory_rows=rows, outcomes=outcomes, config=config, index_summary={}
        )
        return rows, outcomes, tables

    # -- seam 2: the policy over records is unchanged -------------------------

    def test_policy_over_records_reproduces_the_pre_285_exclusions(self) -> None:
        _, _, tables = self._policy()
        self.assertEqual(tables.exclusions_tsv, PINNED_EXCLUSIONS_TSV)

    def test_policy_over_records_reproduces_the_pre_285_analyses_rows(self) -> None:
        _, _, tables = self._policy()
        rows = csv.DictReader(io.StringIO(tables.analyses_tsv), delimiter="\t")
        projected = "".join(
            "\t".join(row[name] for name in PINNED_ANALYSES_COLUMNS) + "\n" for row in rows
        )
        self.assertEqual(projected, PINNED_ANALYSES)

    # -- seam 1: the registry ---------------------------------------------------

    def test_every_measured_exclusion_reason_is_declared_once(self) -> None:
        declared = {}
        for check in quality_checks.checks():
            for reason in check.reasons:
                self.assertNotIn(reason, declared, f"{reason} declared twice")
                declared[reason] = check
        expected = {
            "no_build_eligible_rows": ("build_eligible_rows", 1),
            "effect_placeholder_rows": ("effect_placeholder_rows", None),
            "orientation_failure": ("eaf_orientation", None),
            "ancestry_unassigned": ("assigned_ancestry", None),
            "ancestry_not_eur": ("assigned_ancestry", None),
            "sd_no_reference_resource_for_ancestry": ("phenotype_sd", None),
            "sd_no_qualifying_evidence": ("phenotype_sd", None),
            "sd_no_usable_sample_size": ("phenotype_sd", None),
            "sd_failed": ("phenotype_sd", None),
        }
        self.assertEqual(set(declared), set(expected))
        for reason, (name, default) in expected.items():
            check = declared[reason]
            self.assertEqual(check.name, name, reason)
            self.assertIs(check.kind, Kind.EXCLUSION, reason)
            self.assertEqual(check.default_threshold, default, reason)
        # What is not a check stays release policy, and the controlled
        # vocabulary is exactly the registry's reasons plus the policy's.
        self.assertEqual(
            set(POLICY_EXCLUSION_CATEGORIES),
            {"resolution_failed", "missing_sample_size", "missing_case_control_counts",
             "not_maf_filtered"},
        )
        self.assertFalse(set(POLICY_EXCLUSION_CATEGORIES) & set(declared))
        self.assertEqual(
            controlled_exclusion_categories(),
            {**POLICY_EXCLUSION_CATEGORIES, **quality_checks.exclusion_categories()},
        )
        self.assertEqual(
            {reason: declared[reason].category for reason in declared},
            quality_checks.exclusion_categories(),
        )

    def test_registry_refuses_a_duplicate_name_or_reason_and_a_bad_declaration(self) -> None:
        def evaluate(subject, threshold):
            return quality_checks.Finding(Result.PASSED)

        with self.assertRaises(quality_checks.QualityCheckError):
            quality_checks.register(
                quality_checks.QualityCheck(
                    name="phenotype_sd", kind=Kind.WARNING, stage=Stage.RESOLVED,
                    measures="x", evaluate=evaluate,
                )
            )
        with self.assertRaises(quality_checks.QualityCheckError):
            quality_checks.register(
                quality_checks.QualityCheck(
                    name="another", kind=Kind.EXCLUSION, stage=Stage.RESOLVED,
                    measures="x", evaluate=evaluate, reasons=("sd_failed",),
                    category="effect_scale",
                )
            )
        with self.assertRaises(quality_checks.QualityCheckError):
            quality_checks.QualityCheck(
                name="bad_kind", kind="exclude", stage=Stage.RESOLVED, measures="x",
                evaluate=evaluate,
            )
        with self.assertRaises(quality_checks.QualityCheckError):
            # An exclusion must name the reason it writes and its category.
            quality_checks.QualityCheck(
                name="no_reason", kind=Kind.EXCLUSION, stage=Stage.RESOLVED,
                measures="x", evaluate=evaluate,
            )
        with self.assertRaises(quality_checks.QualityCheckError):
            quality_checks.unregister("never_registered")
        self.assertNotIn("another", [check.name for check in quality_checks.checks()])

    # -- seam 3: a warning check needs no policy change -------------------------

    @staticmethod
    def _made_up_warning():
        """Warn when a record read fewer rows than a threshold (test-only check)."""

        def evaluate(subject, minimum):
            if not subject.resolved:
                return quality_checks.Finding(Result.UNMEASURED)
            rows_read = record_section(subject.record, "diagnostics").get("rows_read")
            if type(rows_read) is not int:
                return quality_checks.Finding(Result.UNMEASURED)
            if rows_read >= minimum:
                return quality_checks.Finding(Result.PASSED, measured=str(rows_read))
            return quality_checks.Finding(
                Result.FAILED,
                measured=str(rows_read),
                detail=f"rows_read={rows_read} < {minimum}",
            )

        return quality_checks.QualityCheck(
            name="made_up_rows_read",
            kind=Kind.WARNING,
            stage=Stage.RESOLVED,
            measures="diagnostics.rows_read, the rows the resolver read",
            evaluate=evaluate,
            default_threshold=150,
            parse_threshold=int,
        )

    def test_a_warning_check_reaches_validation_and_the_sidecar_without_policy_changes(
        self,
    ) -> None:
        _, before, before_tables = self._policy()
        with quality_checks.registered(self._made_up_warning()):
            _, outcomes, tables = self._policy()
            validation = render_validation_yaml(
                tables=tables, index_summary={}, validated_at="now", validator_name="test"
            )
        self.assertNotIn("made_up_rows_read", [check.name for check in quality_checks.checks()])

        # A warning keeps every Analysis where it was.
        self.assertEqual(
            [(o.analysis_id, o.included, o.exclusion_reason) for o in outcomes],
            [(o.analysis_id, o.included, o.exclusion_reason) for o in before],
        )
        self.assertEqual(tables.exclusions_tsv, before_tables.exclusions_tsv)
        self.assertEqual(tables.analyses_tsv, before_tables.analyses_tsv)

        summary = validation["quality_checks"]["made_up_rows_read"]
        self.assertEqual(summary["kind"], "warning")
        self.assertEqual(summary["threshold"], 150)
        self.assertFalse(summary["overridden"])
        # Every resolved synthetic record read 100 rows, below the made-up 150.
        included = sum(1 for outcome in outcomes if outcome.included)
        self.assertEqual(summary["warned"], included)
        self.assertTrue(
            any("made_up_rows_read" in warning for warning in validation["warnings"]),
            validation["warnings"],
        )
        self.assertNotIn(
            "made_up_rows_read",
            " ".join(render_validation_yaml(
                tables=before_tables, index_summary={}, validated_at="now",
                validator_name="test",
            )["warnings"]),
        )

        rows = {
            row["analysis_id"]: row
            for row in csv.DictReader(io.StringIO(tables.quality_checks_tsv), delimiter="\t")
        }
        self.assertEqual(rows["GCST90010001"]["made_up_rows_read"], "failed")
        self.assertEqual(rows["GCST90010001"]["made_up_rows_read_measured"], "100")
        self.assertEqual(rows["GCST90010001"]["exclude_from_build"], "")
        self.assertEqual(rows["GCST90010004"]["made_up_rows_read"], "unmeasured")

    # -- seam 5: the sidecar ----------------------------------------------------

    def test_the_sidecar_records_every_analysis_passing_or_excluded(self) -> None:
        rows, outcomes, tables = self._policy()
        reader = csv.DictReader(io.StringIO(tables.quality_checks_tsv), delimiter="\t")
        self.assertEqual(
            reader.fieldnames,
            [
                "analysis_id", "exclude_from_build", "exclusion_reason",
                "build_eligible_rows", "build_eligible_rows_measured",
                "effect_placeholder_rows", "effect_placeholder_rows_measured",
                "eaf_orientation", "eaf_orientation_measured",
                "assigned_ancestry", "assigned_ancestry_measured",
                "phenotype_sd", "phenotype_sd_measured",
            ],
        )
        sidecar = {row["analysis_id"]: row for row in reader}
        self.assertEqual(list(sidecar), [row.analysis_id for row in rows])

        # An included Analysis: every check measured and passed.
        included = sidecar["GCST90010001"]
        self.assertEqual(included["exclude_from_build"], "")
        self.assertEqual(included["exclusion_reason"], "")
        self.assertEqual(included["build_eligible_rows"], "passed")
        self.assertEqual(included["build_eligible_rows_measured"], "100")
        self.assertEqual(included["eaf_orientation"], "passed")
        self.assertEqual(
            included["eaf_orientation_measured"], "eaf_orientation=passed gate_reason=ok r=0.99"
        )
        self.assertEqual(included["assigned_ancestry"], "passed")
        self.assertEqual(included["assigned_ancestry_measured"], "assigned_ancestry=EUR gate_reason=ok")
        self.assertEqual(included["phenotype_sd"], "passed")
        self.assertEqual(included["effect_placeholder_rows"], "passed")
        self.assertEqual(included["effect_placeholder_rows_measured"], "not_listed")
        # `unverified` is no verdict on orientation, so the check passes.
        self.assertEqual(sidecar["GCST90010026"]["eaf_orientation"], "passed")

        # Excluded by a check: it failed, and the deciding reason is beside it.
        zero = sidecar["GCST90010006"]
        self.assertEqual(zero["exclude_from_build"], "true")
        self.assertEqual(zero["exclusion_reason"], "no_build_eligible_rows")
        self.assertEqual(zero["build_eligible_rows"], "failed")
        self.assertEqual(zero["build_eligible_rows_measured"], "0")

        # Excluded earlier by another check, yet its other measurements are kept:
        # GCST90010007 has zero rows *and* a failed orientation.
        both = sidecar["GCST90010007"]
        self.assertEqual(both["exclusion_reason"], "no_build_eligible_rows")
        self.assertEqual(both["eaf_orientation"], "failed")

        # The Assigned Ancestry check: a non-target and an unassigned Analysis.
        not_eur = sidecar["GCST90010013"]
        self.assertEqual(not_eur["exclusion_reason"], "ancestry_not_eur")
        self.assertEqual(not_eur["assigned_ancestry"], "failed")
        self.assertEqual(not_eur["eaf_orientation"], "passed")
        self.assertEqual(not_eur["phenotype_sd"], "passed")
        self.assertEqual(sidecar["GCST90010012"]["assigned_ancestry"], "failed")
        self.assertEqual(
            sidecar["GCST90010012"]["assigned_ancestry_measured"],
            "assigned_ancestry=none gate_reason=overlap",
        )

        # Excluded by release policy that is not a check: measurements still kept.
        counts = sidecar["GCST90010023"]
        self.assertEqual(counts["exclusion_reason"], "missing_case_control_counts")
        self.assertEqual(counts["assigned_ancestry"], "passed")

        # A case-control Analysis has no phenotype SD to test.
        self.assertEqual(sidecar["GCST90010002"]["phenotype_sd"], "not_applicable")
        self.assertEqual(sidecar["GCST90010002"]["phenotype_sd_measured"], "")

        # No Resolution Record: every record measurement is unmeasured.
        missing = sidecar["GCST90010004"]
        self.assertEqual(missing["exclusion_reason"], "resolution_failed")
        for name in ("build_eligible_rows", "eaf_orientation", "assigned_ancestry", "phenotype_sd"):
            self.assertEqual(missing[name], "unmeasured", name)
            self.assertEqual(missing[f"{name}_measured"], "", name)

        listed = sidecar["GCST90010008"]
        self.assertEqual(listed["effect_placeholder_rows"], "failed")
        self.assertEqual(
            listed["effect_placeholder_rows_measured"],
            "3 of 100 rows carry the 2.2e-308 placeholder",
        )
        sd_zero = sidecar["GCST90010014"]
        self.assertEqual(sd_zero["phenotype_sd"], "failed")
        self.assertEqual(sd_zero["phenotype_sd_measured"], "status=estimated reason=none sd=0.0")

        validation = render_validation_yaml(
            tables=tables, index_summary={}, validated_at="now", validator_name="test"
        )
        self.assertEqual(validation["reports"]["quality_checks"], "sidecars/quality_checks.tsv")
        build_eligible = validation["quality_checks"]["build_eligible_rows"]
        self.assertEqual(
            {key: value for key, value in build_eligible.items() if key != "measures"},
            {
                "kind": "exclusion",
                "stage": "resolved",
                "threshold": 1,
                "default_threshold": 1,
                "overridden": False,
                # Two Analyses have no successful record; two have zero rows.
                "results": {
                    "passed": 26, "failed": 2, "not_applicable": 0, "unmeasured": 2,
                    "invalid": 0,
                },
                "excluded": 2,
            },
        )
        self.assertIn("build_eligible_rows", build_eligible["measures"])
        self.assertEqual(validation["quality_checks"]["phenotype_sd"]["excluded"], 10)
        self.assertEqual(validation["quality_checks"]["assigned_ancestry"]["excluded"], 4)
        self.assertIn(
            "reviewed", validation["quality_checks"]["effect_placeholder_rows"]["measures"]
        )

    # -- a measurement the policy cannot use is never a silent pass --------------

    def _decide_one(self, record, *, sample_size="5000", design="quantitative", config=None):
        row = _synthetic_inventory_row(study_design=design, sample_size=sample_size)
        metadata = _synthetic_candidate_metadata(
            sample_size=sample_size,
            n_cases="400" if design == "case-control" else "",
            n_controls="600" if design == "case-control" else "",
        )
        record["analysis_id"] = row.analysis_id
        (outcome,) = apply_release_policy(
            [row], config or self.config, {row.analysis_id: metadata}, {row.analysis_id: record}
        )
        return outcome

    @staticmethod
    def _finding(outcome, name):
        (finding,) = [f for f in outcome.check_results if f.name == name]
        return finding

    def test_an_invalid_measurement_is_recorded_where_it_decides_nothing(self) -> None:
        # Unknown orientation outcome on an Analysis already excluded earlier.
        unknown = _synthetic_resolver_record(eaf_orientation="ok")
        outcome = self._decide_one(unknown, sample_size="0")
        self.assertEqual(outcome.exclusion_reason, "missing_sample_size")
        finding = self._finding(outcome, "eaf_orientation")
        self.assertIs(finding.result, Result.INVALID)
        self.assertIn("unknown eaf_orientation 'ok'", finding.detail)

        # A non-numeric SD on an Analysis the Assigned Ancestry check excluded.
        not_numeric = _synthetic_resolver_record(assigned_ancestry="EAS")
        not_numeric["phenotype_sd"]["estimate"]["sd"] = "n/a"
        outcome = self._decide_one(not_numeric)
        self.assertEqual(outcome.exclusion_reason, "ancestry_not_eur")
        finding = self._finding(outcome, "phenotype_sd")
        self.assertIs(finding.result, Result.INVALID)
        self.assertIn("'n/a' is not a number", finding.detail)

        rows = {
            row["analysis_id"]: row
            for row in csv.DictReader(
                io.StringIO(
                    build_candidate_tables(
                        inventory_rows=[outcome.row], outcomes=[outcome],
                        config=self.config, index_summary={},
                    ).quality_checks_tsv
                ),
                delimiter="\t",
            )
        }
        self.assertEqual(rows[outcome.analysis_id]["phenotype_sd"], "invalid")

    def test_an_invalid_measurement_raises_where_it_would_decide(self) -> None:
        with self.assertRaises(CandidateError) as raised:
            self._decide_one(_synthetic_resolver_record(eaf_orientation="ok"))
        self.assertIn("unknown eaf_orientation", str(raised.exception))
        not_numeric = _synthetic_resolver_record()
        not_numeric["phenotype_sd"]["estimate"]["sd"] = "n/a"
        with self.assertRaises(CandidateError) as raised:
            self._decide_one(not_numeric)
        self.assertIn("'n/a' is not a number", str(raised.exception))

    def test_an_unmeasured_exclusion_refuses_to_include_the_analysis(self) -> None:
        legacy = _synthetic_resolver_record(eaf_orientation="")
        with self.assertRaises(CandidateError) as raised:
            self._decide_one(legacy)
        self.assertIn("GCST90000001", str(raised.exception))
        self.assertIn("eaf_orientation is unmeasured", str(raised.exception))
        # The same gap on an Analysis excluded for another reason is only recorded.
        outcome = self._decide_one(_synthetic_resolver_record(eaf_orientation=""), sample_size="0")
        self.assertIs(self._finding(outcome, "eaf_orientation").result, Result.UNMEASURED)

    def test_build_eligible_rows_passes_and_fails_under_an_override(self) -> None:
        config = replace(self.config, quality_check_overrides={"build_eligible_rows": 50})
        enough = _synthetic_resolver_record()
        enough["diagnostics"].update(build_eligible_rows=50, canonical_rows_retained=80)
        outcome = self._decide_one(enough, config=config)
        self.assertTrue(outcome.included, outcome.exclusion_reason)
        self.assertIs(self._finding(outcome, "build_eligible_rows").result, Result.PASSED)

        few = _synthetic_resolver_record()
        few["diagnostics"].update(build_eligible_rows=10, canonical_rows_retained=80)
        outcome = self._decide_one(few, config=config)
        self.assertFalse(outcome.included)
        self.assertEqual(outcome.exclusion_reason, "no_build_eligible_rows")
        self.assertEqual(
            outcome.exclusion_detail,
            "resolver found 10 row(s) with a finite effect and positive standard error, "
            "fewer than the release minimum of 50 (rows_read=100, canonical_rows_retained=80)",
        )
        # The same record passes on the shared default of 1.
        self.assertTrue(self._decide_one(few).included)

    # -- seam 4: thresholds are recorded and bind the receipt ---------------------

    def _write_overrides(self, overrides) -> None:
        document = yaml.safe_load(self.fixture.config_path.read_text())
        if overrides is None:
            document.pop("quality_checks", None)
        else:
            document["quality_checks"] = overrides
        self.fixture.config_path.write_text(yaml.safe_dump(document, sort_keys=False))

    def test_the_contract_records_the_effective_thresholds_defaults_included(self) -> None:
        self.assertEqual(self.config.quality_check_overrides, {})
        self.assertEqual(
            compute_resolution_contract(self.config)["quality_checks"],
            {"thresholds": {"build_eligible_rows": 1}, "overrides": {}},
        )

    def test_override_parsing_refuses_unknown_thresholdless_and_invalid_values(self) -> None:
        for overrides, message in (
            ({"no_such_check": 1}, "no_such_check"),
            ({"eaf_orientation": 1}, "has no threshold"),
            ({"build_eligible_rows": 0}, "build_eligible_rows"),
            ({"build_eligible_rows": "many"}, "build_eligible_rows"),
            ({"build_eligible_rows": True}, "build_eligible_rows"),
            (["build_eligible_rows"], "mapping"),
        ):
            with self.subTest(overrides=overrides):
                self._write_overrides(overrides)
                with self.assertRaises(PreflightConfigError) as raised:
                    load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
                self.assertIn(message, str(raised.exception))

    def _verify(self) -> list[str]:
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        rows = read_inventory(self.fixture.inventory_path)
        metadata = read_candidate_metadata(
            self.fixture.candidates_path, [row.analysis_id for row in rows if row.ready]
        )
        run_root = self.fixture.work_root / STORE_ID / "resolver"
        _, failures = verify_records(
            derive_resolver_manifest(rows, config, metadata),
            run_root / "records",
            config=config,
            manifest_path=run_root / "analyses.tsv",
            receipt_path=run_root / RECEIPT_FILENAME,
        )
        return failures

    def test_a_release_override_is_recorded_and_changing_it_makes_the_receipt_stale(
        self,
    ) -> None:
        self._write_overrides({"build_eligible_rows": 2})
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        self.assertEqual(config.quality_check_overrides, {"build_eligible_rows": 2})

        first = _run_cli(self.fixture)
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        receipt_path = self.fixture.work_root / STORE_ID / "resolver" / RECEIPT_FILENAME
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(
            receipt["contract"]["quality_checks"],
            {"thresholds": {"build_eligible_rows": 2}, "overrides": {"build_eligible_rows": 2}},
        )
        bundle = self.fixture.registry_root / STORE_ID
        validation = yaml.safe_load((bundle / "validation.yaml").read_text())
        summary = validation["quality_checks"]["build_eligible_rows"]
        self.assertEqual((summary["threshold"], summary["default_threshold"]), (2, 1))
        self.assertTrue(summary["overridden"])
        sidecar = _read_tsv(bundle / "sidecars/quality_checks.tsv")
        self.assertEqual(
            {row["build_eligible_rows_measured"] for row in sidecar if row["exclude_from_build"] != "true"},
            {"2000"},
        )
        self.assertEqual(self._verify(), [])

        before = (bundle / "sidecars/quality_checks.tsv").read_bytes()
        self._write_overrides({"build_eligible_rows": 3})
        self.assertTrue(any("quality_checks" in failure for failure in self._verify()))
        stale = _run_cli(self.fixture, "--stage", "emit")
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("resolution contract changed", stale.stderr)
        self.assertIn("quality_checks", stale.stderr)
        self.assertEqual((bundle / "sidecars/quality_checks.tsv").read_bytes(), before)

        # Removing the override is a change too: the receipt bound one.
        self._write_overrides(None)
        stale = _run_cli(self.fixture, "--stage", "emit")
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("quality_checks", stale.stderr)

    def test_changing_a_shared_default_makes_the_receipt_stale(self) -> None:
        first = _run_cli(self.fixture)
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        self.assertEqual(self._verify(), [])
        registry = quality_checks._REGISTRY
        changed = replace(registry["build_eligible_rows"], default_threshold=2)
        # A simulated code change to the shared default, in place in the registry.
        with mock.patch.dict(registry, {"build_eligible_rows": changed}):
            failures = self._verify()
        self.assertTrue(
            any("quality_checks" in failure and "resolution must be re-run" in failure
                for failure in failures),
            failures,
        )
        self.assertEqual(self._verify(), [])


if __name__ == "__main__":
    unittest.main()

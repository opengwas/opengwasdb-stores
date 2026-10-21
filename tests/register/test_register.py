#!/usr/bin/env python3
"""Tests for ogstores.register: validation.yaml assembly, argv drift, and registration safety (Issue #117).

Verifies the central contracts of ADR 0022 and ADR 0023:
1. `validation.yaml` is written atomically only by `register`; failed runs leave any previous
   `validation.yaml` completely intact.
2. Observed measurements (format_version, n_variants, n_analyses, n_associations, store_bytes,
   build_elapsed_s, validate_status) are harvested from step records.
3. Argv drift check fails and names the exact difference when executed argv differs from plan.
   Staging normalization applies (store.opengwasdb.partial -> store.opengwasdb).
4. `complete-dense-resume` is accepted as a legitimate divergence for planned `complete-dense`
   and recorded as `resumed: true` in validation.yaml.
5. Strict seam compliance: `register` opens NO Store and re-runs NO validation (proven via tripwires).
6. Atomic publication: invokes `run.publish_store()` upon successful registration.
7. Only this run's findings (#195): the verdict, checks, warnings and errors come from
   `opengwasdb validate --format json`; nothing is carried from the previous record.
8. A completion phase deletes the Store it replaces, only over a passing Observed-View Check,
   and archives its records and the check's verdict; a forced run archives the Store (#309).
"""

from __future__ import annotations

import builtins
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from typing import Any

import yaml


from ogstores import bundle, manifest, paths, register, run
from ogstores.bundle import Bundle
from ogstores.plan import OBSERVED_VIEW_CHECK_STEP, Step, plan
from ogstores.register import (
    ArgvDriftError,
    MissingRecordError,
    RegisterError,
    StepFailedError,
    check_argv_drift,
    harvest_observed_measurements,
    normalize_executed_argv_for_staging,
    register_release,
)


def observed_view_verdict(*, passed: bool) -> str:
    """An Observed-View Check verdict as `opengwasdb check-observed-view` prints it: indented JSON."""
    failures = [] if passed else ["z_mismatch"]
    verdict = {"seed": 20261021, "totals": {"z_mismatch": 0 if passed else 3}, "failures": failures, "passed": passed}
    return json.dumps(verdict, indent=2) + "\n"


def create_test_bundle_and_records(
    temp_dir: Path,
    store_id: str = "OGS-00042",
    *,
    layout: str = "dense",
    completion_state: str = "observed_only",
    command: str = "build-dense-vcf",
    derived_from: str | None = None,
    options: dict[str, Any] | None = None,
    post: dict[str, Any] | None = None,
    variant_reference: object = None,
    custom_records: dict[str, dict[str, Any]] | None = None,
) -> tuple[Bundle, Path, Path]:
    """Helper to create a test bundle with staged artifact directories and valid execution records."""
    stores_root = temp_dir / "stores"
    artifact_root = temp_dir / "artifacts"
    store_bundle_dir = stores_root / store_id
    store_bundle_dir.mkdir(parents=True, exist_ok=True)
    records_dir = artifact_root / store_id / "records"
    records_dir.mkdir(parents=True, exist_ok=True)

    rel_dict = {
        "store_id": store_id,
        "label": f"test-{store_id}",
        "status": "candidate",
        "source_collection_id": "test-collection",
        "association_coverage": "full_gwas",
        "derived_from": derived_from,
        "created_at": "2026-08-18T08:51:59Z",
        "description": "Test release",
        "source_snapshot_id": "test-snapshot",
        "release_kind": "pilot",
        "generator": {"command": "test-gen"},
    }

    default_post = (
        {"top_hits": False, "rho": False, "overview": True, "validate": True}
        if completion_state == "reference_completed"
        else {"top_hits": True, "rho": False, "overview": True, "validate": True}
    )
    bld_dict: dict[str, Any] = {
        "store_id": store_id,
        "layout": layout,
        "completion_state": completion_state,
        "post": post or default_post,
        "artifacts": {"root": str(artifact_root)},
    }
    if completion_state == "reference_completed":
        bld_dict["complete"] = {"command": command, "options": options or {"ancestry": "EUR"}}
        if derived_from is None:
            # One id, published twice: the build phase comes first (ADR 0037).
            bld_dict["build"] = {"command": "build-dense-vcf", "options": {"source-assembly": "hg38"}}
    else:
        bld_dict["build"] = {"command": command, "options": options or {"source-assembly": "hg38"}}
    if variant_reference is not None:
        block_key = "complete" if completion_state == "reference_completed" else "build"
        bld_dict[block_key]["variant_reference"] = variant_reference

    with open(store_bundle_dir / "release.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(rel_dict, f)
    with open(store_bundle_dir / "build.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(bld_dict, f)

    analyses_p = store_bundle_dir / "analyses.tsv"
    analyses_p.write_text("analysis_id\tsource_file\nana1\t/path/to/file1.vcf.gz\nana2\t/path/to/file2.vcf.gz\n")

    b = bundle.load(store_id, registry_root=stores_root)
    planned_steps = plan(b, artifact_root=artifact_root)

    # Create staging .partial directory so publication works
    partial_p = paths.partial_store_path(store_id, root=artifact_root)
    partial_p.mkdir(parents=True, exist_ok=True)

    # Populate valid execution records for all planned steps
    for step in planned_steps:
        if custom_records and step.name in custom_records:
            rec_data = custom_records[step.name]
        else:
            # As staging runs it: every Store token but the published Store a
            # completion step reads becomes the staged path (ADR 0037).
            executed_argv = [
                token
                if index == step.published_store_arg
                else token.replace(str(paths.store_path(store_id, root=artifact_root)), str(partial_p))
                for index, token in enumerate(step.argv)
            ]
            if step.name == "variant-reference" and step.outputs:
                staged = str(run.variant_reference_partial_path(step.outputs[0]))
                executed_argv = [
                    staged if token == str(step.outputs[0]) else token
                    for token in executed_argv
                ]
            stdout_payload = ""
            if step.name in ("build", "complete"):
                stdout_payload = json.dumps({"n_variants": 1000, "n_analyses": 2, "format_version": "1.0"}) + "\n"
            elif step.name in ("validate", "completion-validate"):
                # The shape `opengwasdb validate --format json` prints (#195).
                stdout_payload = json.dumps({"errors": [], "ok": True, "warnings": []}) + "\n"
            elif step.name == OBSERVED_VIEW_CHECK_STEP:
                stdout_payload = observed_view_verdict(passed=True)

            rec_data = {
                "step": step.name,
                "store_id": store_id,
                "exit_code": 0,
                "success": True,
                "start_time": "2026-09-14T12:00:00Z",
                "end_time": "2026-09-14T12:00:05Z",
                "elapsed_seconds": 5.0,
                "argv": executed_argv,
                "planned_argv": list(step.argv),
                "inputs": [str(p) for p in step.inputs],
                "outputs": [str(p) for p in step.outputs],
                "opengwasdb_rev": "a9e8bc825af692cdb5bb7cba39d56d11654fcebe",
                "opengwasdb_version": "0.3.0",
                "opengwasdb_executable": "/fake/bin/opengwasdb",
                "stdout": stdout_payload,
                "stderr": "",
                "record_path": str(paths.record_path(store_id, step.name, root=artifact_root)),
            }
        rec_file = paths.record_path(store_id, step.name, root=artifact_root)
        run._write_record_atomically(rec_data, rec_file)

    return b, stores_root, artifact_root


class TestRegisterExecutionAndSafety(unittest.TestCase):
    """Test register_release assembly, atomic write safety, and observed measurements."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_validation_yaml_written_only_by_register_atomic_safety(self) -> None:
        """validation.yaml is written atomically only on register success; failed run leaves previous intact."""
        b, stores_root, artifact_root = create_test_bundle_and_records(self.td, "OGS-00042")
        val_yaml_p = b.root / "validation.yaml"

        # Pre-populate an existing validation.yaml
        initial_val_data = {
            "status": "passed_with_warnings",
            "validated_at": "2026-08-01T10:00:00Z",
            "custom_sentinel_key": "must_be_preserved_on_failure",
            "checks": {"schema": "passed", "files": "passed"},
        }
        with open(val_yaml_p, "w", encoding="utf-8") as f:
            yaml.safe_dump(initial_val_data, f)

        # 1. Simulate failure: corrupt a step record (make it fail)
        build_rec_p = paths.record_path("OGS-00042", "build", root=artifact_root)
        corrupt_rec = json.loads(build_rec_p.read_text(encoding="utf-8"))
        corrupt_rec["exit_code"] = 1
        corrupt_rec["success"] = False
        run._write_record_atomically(corrupt_rec, build_rec_p)

        with self.assertRaises(StepFailedError):
            register_release(b, registry_root=stores_root, artifact_root=artifact_root)

        # Assert existing validation.yaml was completely untouched
        current_data = yaml.safe_load(val_yaml_p.read_text(encoding="utf-8"))
        self.assertEqual(current_data["custom_sentinel_key"], "must_be_preserved_on_failure")
        self.assertEqual(current_data["status"], "passed_with_warnings")

        # 2. Fix the step record and execute register_release successfully
        corrupt_rec["exit_code"] = 0
        corrupt_rec["success"] = True
        run._write_record_atomically(corrupt_rec, build_rec_p)

        val_result = register_release(b, registry_root=stores_root, artifact_root=artifact_root)
        self.assertEqual(val_result["status"], "passed")

        # Verify validation.yaml was written atomically and contains merged data
        updated_data = yaml.safe_load(val_yaml_p.read_text(encoding="utf-8"))
        self.assertEqual(updated_data["status"], "passed")
        self.assertEqual(updated_data["observed"]["validate_status"], "passed")
        self.assertEqual(updated_data["observed"]["format_version"], "1.0")

        # Verify records/register.json was produced
        reg_rec_p = paths.record_path("OGS-00042", "register", root=artifact_root)
        self.assertTrue(reg_rec_p.is_file())
        reg_json = json.loads(reg_rec_p.read_text(encoding="utf-8"))
        self.assertEqual(reg_json["step"], "register")
        self.assertTrue(reg_json["success"])

    def test_n_analyses_fallback_counts_built_manifest_not_audit_table(self) -> None:
        """The n_analyses fallback counts the derived manifest, never the bundle's excluded rows (ADR 0025)."""
        b, stores_root, artifact_root = create_test_bundle_and_records(self.td, "OGS-00049")

        # The bundle keeps an excluded audit row; the derived manifest drops it.
        (b.root / "analyses.tsv").write_text(
            "analysis_id\tsource_file\texclude_from_build\n"
            "ana1\t/path/to/file1.vcf.gz\t\n"
            "EXCLUDED\t/path/to/excluded.vcf.gz\ttrue\n"
            "ana2\t/path/to/file2.vcf.gz\t\n",
            encoding="utf-8",
        )
        manifest.materialise_build_manifest(
            b.analyses_path,
            paths.build_manifest_path("OGS-00049", root=artifact_root),
            paths.build_manifest_sidecar_path("OGS-00049", root=artifact_root),
        )

        # Strip n_analyses from every record's stdout so the manifest fallback runs.
        for step_name in ("build", "validate"):
            rec_p = paths.record_path("OGS-00049", step_name, root=artifact_root)
            rec = json.loads(rec_p.read_text(encoding="utf-8"))
            payload = json.loads(rec["stdout"]) if rec.get("stdout", "").strip() else {}
            payload.pop("n_analyses", None)
            rec["stdout"] = json.dumps(payload) + "\n"
            run._write_record_atomically(rec, rec_p)

        val_data = register_release(b, registry_root=stores_root, artifact_root=artifact_root)
        self.assertEqual(val_data["observed"]["n_analyses"], 2)

    def test_observed_measurements_harvested_from_records(self) -> None:
        """Measurements come from the build record; validate's JSON carries only its verdict (#195)."""
        b, stores_root, artifact_root = create_test_bundle_and_records(self.td, "OGS-00043")

        build_rec_p = paths.record_path("OGS-00043", "build", root=artifact_root)
        build_rec = json.loads(build_rec_p.read_text(encoding="utf-8"))
        build_rec["stdout"] = json.dumps({
            "format_version": "1.0",
            "n_variants": 12500,
            "n_analyses": 50,
            "n_associations": 625000,
        }) + "\n"
        build_rec["elapsed_seconds"] = 10.5
        run._write_record_atomically(build_rec, build_rec_p)

        val_data = register_release(b, registry_root=stores_root, artifact_root=artifact_root)
        obs = val_data["observed"]

        self.assertEqual(obs["format_version"], "1.0")
        self.assertEqual(obs["n_variants"], 12500)
        self.assertEqual(obs["n_analyses"], 50)
        self.assertEqual(obs["n_associations"], 625000)
        self.assertIsNone(obs["store_bytes"], "no step reports a Store size")
        self.assertEqual(obs["validate_status"], "passed")
        self.assertGreater(obs["build_elapsed_s"], 10.0)

    def test_written_record_matches_the_documented_register_shape(self) -> None:
        """The written validation.yaml carries exactly the canonical register keys (issue #135).

        The committed-record suite asserts each `stores/OGS-*/validation.yaml`
        against these same constants, so the shape `register` produces and the
        documented format cannot drift apart.
        """
        b, stores_root, artifact_root = create_test_bundle_and_records(self.td, "OGS-00051")
        register_release(b, registry_root=stores_root, artifact_root=artifact_root)

        written = yaml.safe_load((b.root / "validation.yaml").read_text(encoding="utf-8"))
        self.assertEqual(set(written["observed"]), set(register.OBSERVED_FIELDS))
        self.assertEqual(set(written["build_environment"]), set(register.BUILD_ENVIRONMENT_FIELDS))
        self.assertEqual(written["validator"]["name"], register.VALIDATOR_NAME)


def set_validate_output(store_id: str, artifact_root: Path, stdout: str, stderr: str = "") -> None:
    """Replace what the validate step's record says `opengwasdb validate` printed."""
    rec_p = paths.record_path(store_id, "validate", root=artifact_root)
    rec = json.loads(rec_p.read_text(encoding="utf-8"))
    rec["stdout"] = stdout
    rec["stderr"] = stderr
    run._write_record_atomically(rec, rec_p)


class TestRegisterRecordsOnlyThisRun(unittest.TestCase):
    """A Validation Record holds this run's findings and nothing from the one it replaces (#195)."""

    WARNING = (
        "analysis 'GCST003898' stores EAF whose orientation is unverified: "
        "only 120 variants overlap the reference, fewer than 500"
    )

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_stale_record(self, b: Bundle) -> tuple[Bundle, dict[str, Any]]:
        """OGS-00005's committed shape: a previous build's checks, reports and 30 warnings.

        Returns the bundle reloaded, as the workflow loads it, so `register`
        sees the record it is replacing.
        """
        stale = {
            "status": "passed_with_warnings",
            "validated_at": "2026-08-31T20:01:07Z",
            # Migrated to the register shape by #135, so it names the register's validator.
            "validator": {"name": register.VALIDATOR_NAME, "version": "opengwasdb@6ef919e"},
            "checks": {
                "schema": "passed",
                "files": "passed_with_warnings",
                "reader_smoke_test": "passed_with_warnings",
                "effect_scale": "passed",
                "sd_estimation": "passed",
            },
            "reports": {"build_report": "sidecars/build_report.tsv"},
            "warnings": [
                f"GCST{n:06d}: sample_size_kind present in the manifest but missing from "
                "the built store's analyses.tsv"
                for n in range(30)
            ],
            "errors": [],
        }
        (b.root / "validation.yaml").write_text(yaml.safe_dump(stale), encoding="utf-8")
        return bundle.load(b.store_id, registry_root=b.root.parent), stale

    def test_stale_findings_are_not_carried_into_the_new_record(self) -> None:
        """Only the new run's verdict, warning and errors appear; the previous record's are gone."""
        b, stores_root, artifact_root = create_test_bundle_and_records(self.td, "OGS-00052")
        b, _stale = self.write_stale_record(b)
        set_validate_output(
            b.store_id,
            artifact_root,
            json.dumps({"errors": [], "ok": True, "warnings": [self.WARNING]}) + "\n",
        )

        register_release(b, registry_root=stores_root, artifact_root=artifact_root)

        written = yaml.safe_load((b.root / "validation.yaml").read_text(encoding="utf-8"))
        self.assertEqual(written["warnings"], [self.WARNING])
        self.assertEqual(written["checks"], {"store": "passed_with_warnings"})
        self.assertNotIn("reports", written)
        self.assertNotIn("acceptance", written, "a previous build's findings are not acceptance evidence")
        self.assertEqual(written["errors"], [])
        self.assertEqual(written["status"], "passed_with_warnings")
        self.assertEqual(written["observed"]["validate_status"], "passed_with_warnings")

    def test_a_clean_validate_records_no_findings(self) -> None:
        """No warning from validate means `passed` with empty findings, not fabricated checks."""
        b, stores_root, artifact_root = create_test_bundle_and_records(self.td, "OGS-00053")
        b, _stale = self.write_stale_record(b)

        written = register_release(b, registry_root=stores_root, artifact_root=artifact_root)

        self.assertEqual(written["checks"], {"store": "passed"})
        self.assertEqual(written["warnings"], [])
        self.assertEqual(written["errors"], [])
        self.assertNotIn("reports", written)
        self.assertEqual(written["status"], "passed")

    def test_an_unreported_format_version_is_recorded_as_null(self) -> None:
        """No step printed a format version, so the record says null rather than "1.0" (#195)."""
        b, stores_root, artifact_root = create_test_bundle_and_records(self.td, "OGS-00056")
        rec_p = paths.record_path(b.store_id, "build", root=artifact_root)
        rec = json.loads(rec_p.read_text(encoding="utf-8"))
        rec["stdout"] = json.dumps({"n_variants": 1000, "n_analyses": 2}) + "\n"
        run._write_record_atomically(rec, rec_p)

        written = register_release(b, registry_root=stores_root, artifact_root=artifact_root, publish=False)

        self.assertIsNone(written["observed"]["format_version"])

    def test_a_validate_record_without_a_json_verdict_is_refused(self) -> None:
        """Text output is not searched for the word `warning`; register fails and writes nothing."""
        b, stores_root, artifact_root = create_test_bundle_and_records(self.td, "OGS-00054")
        b, stale = self.write_stale_record(b)
        set_validate_output(b.store_id, artifact_root, "valid\n", f"warning: {self.WARNING}\n")

        with self.assertRaises(register.ValidateVerdictError) as ctx:
            register_release(b, registry_root=stores_root, artifact_root=artifact_root)

        self.assertIn("--format json", str(ctx.exception))
        self.assertEqual(
            yaml.safe_load((b.root / "validation.yaml").read_text(encoding="utf-8")),
            stale,
        )
        self.assertFalse(paths.record_path(b.store_id, "register", root=artifact_root).exists())


CANDIDATE_RECORD: dict[str, Any] = {
    "status": "passed_with_warnings",
    "validated_at": "2026-09-30T00:25:26Z",
    "validator": {"name": "resources/generators/gwas-catalog-eur-hybrid/generate_candidate.py", "version": None},
    "observed": {"format_version": None, "validate_status": None},
    "checks": {"schema": "passed", "files": "passed", "ancestry": "passed_with_warnings", "sd_estimation": "passed_with_warnings"},
    "reports": {"ancestry": "sidecars/ancestry.tsv", "sd_estimation": "sidecars/sd_estimation.tsv"},
    # OGS-00011's generator also writes this top-level block of Phase B evidence.
    "reference_overlap": {
        "low_overlap_threshold": 0.05,
        "included_measured": 3317,
        "ancestry_low_overlap_analyses": ["GCST003898", "GCST005898"],
    },
    # And its shared-Store MAF-filtered admission summary (issue #203).
    "store_composition": {
        "require_maf_filtered": True,
        "maf_floor": "0.005",
        "unfiltered_included": 0,
        "unfiltered_included_off_reference_rows": 0,
        "unfiltered_routed": 148,
        "unfiltered_routed_off_reference_rows": 176733851,
    },
    "warnings": [
        "1162 Analysis/Analyses excluded by ancestry policy (unassigned, non-target, or orientation failure); see sidecars/exclusions.tsv",
        "173 included Analysis/Analyses have a high-dispersion SD estimate; see sidecars/sd_estimation.tsv",
    ],
    "errors": [],
}


def validate_record(stdout: str, exit_code: int = 0, success: bool | None = None) -> dict[str, Any]:
    """A validate step record as run.py writes it, with what the CLI printed."""
    return {
        "step": "validate",
        "exit_code": exit_code,
        "success": (exit_code == 0) if success is None else success,
        "stdout": stdout,
        "stderr": "",
        "record_path": "/artifacts/OGS-00063/records/validate.json",
    }


def verdict_json(ok: Any, errors: Any, warnings: Any, **extra: Any) -> str:
    """What `opengwasdb validate --format json` prints: one sorted object and a newline."""
    return json.dumps({"ok": ok, "errors": errors, "warnings": warnings, **extra}, sort_keys=True) + "\n"


class TestValidateVerdictContract(unittest.TestCase):
    """`validate_verdict` accepts exactly the pinned CLI's JSON contract and nothing else (#195)."""

    def test_passing_warning_and_failing_verdicts(self) -> None:
        cases = {
            "passing": (validate_record(verdict_json(True, [], [])), ("passed", [], [])),
            "warning": (
                validate_record(verdict_json(True, [], ["EAF orientation unverified"])),
                ("passed_with_warnings", ["EAF orientation unverified"], []),
            ),
            "failing": (
                validate_record(verdict_json(False, ["tabix cannot fetch 23:1:A:G"], ["w"]), exit_code=1),
                ("failed", ["w"], ["tabix cannot fetch 23:1:A:G"]),
            ),
        }
        for name, (record, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(register.validate_verdict(record), expected)

    def test_no_validate_step_is_not_run(self) -> None:
        self.assertEqual(register.validate_verdict(None), ("not_run", [], []))

    def test_malformed_output_is_refused(self) -> None:
        one = verdict_json(True, [], []).strip()
        cases = {
            "text output": "valid\n",
            "empty output": "",
            "two objects": one + "\n" + one + "\n",
            "object then text": one + "\nvalid\n",
            "text then object": "valid\n" + one + "\n",
            "a JSON array": "[]\n",
            "missing warnings": json.dumps({"ok": True, "errors": []}) + "\n",
            "an extra key": verdict_json(True, [], [], format_version="0.1.0"),
            "ok as a string": verdict_json("true", [], []),
            "ok as a number": verdict_json(1, [], []),
            "errors as a string": verdict_json(True, "", []),
            "warnings as a dict": verdict_json(True, [], {}),
            "a non-string warning": verdict_json(True, [], [1]),
            "a null error": verdict_json(False, [None], [], ),
        }
        for name, stdout in cases.items():
            with self.subTest(name):
                exit_code = 1 if name == "a null error" else 0
                with self.assertRaises(register.ValidateVerdictError):
                    register.validate_verdict(validate_record(stdout, exit_code=exit_code))

    def test_contradictory_records_are_refused(self) -> None:
        cases = {
            "ok with an error": validate_record(verdict_json(True, ["fatal"], [])),
            "not ok without an error": validate_record(verdict_json(False, [], []), exit_code=1),
            "ok but exit 1": validate_record(verdict_json(True, [], []), exit_code=1, success=False),
            "not ok but exit 0": validate_record(verdict_json(False, ["fatal"], []), exit_code=0, success=False),
            "ok but not a success": validate_record(verdict_json(True, [], []), exit_code=0, success=False),
            "not ok but a success": validate_record(verdict_json(False, ["fatal"], []), exit_code=1, success=True),
        }
        for name, record in cases.items():
            with self.subTest(name):
                with self.assertRaises(register.ValidateVerdictError):
                    register.validate_verdict(record)


    def test_record_fields_of_the_wrong_type_are_refused(self) -> None:
        """exit_code must be an int (not a bool or float) and success a bool, as run.py writes them."""
        ok = verdict_json(True, [], [])
        failed = verdict_json(False, ["fatal"], [])
        cases = {
            "exit_code false": dict(validate_record(ok), exit_code=False),
            "exit_code true": dict(validate_record(failed, exit_code=1), exit_code=True),
            "exit_code 0.0": dict(validate_record(ok), exit_code=0.0),
            "exit_code 1.0": dict(validate_record(failed, exit_code=1), exit_code=1.0),
            "exit_code '0'": dict(validate_record(ok), exit_code="0"),
            "exit_code missing": {k: v for k, v in validate_record(ok).items() if k != "exit_code"},
            "success 1": dict(validate_record(ok), success=1),
            "success 0": dict(validate_record(failed, exit_code=1), success=0),
            "success missing": {k: v for k, v in validate_record(ok).items() if k != "success"},
            "stdout bytes": dict(validate_record(ok), stdout=ok.encode()),
        }
        for name, record in cases.items():
            with self.subTest(name):
                with self.assertRaises(register.ValidateVerdictError):
                    register.validate_verdict(record)


class TestRegisterRefusesAContradictoryVerdict(unittest.TestCase):
    """A contradictory validate record stops registration before anything is written (#195)."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_ok_with_errors_is_not_registered_as_passed(self) -> None:
        b, stores_root, artifact_root = create_test_bundle_and_records(self.td, "OGS-00063")
        set_validate_output(b.store_id, artifact_root, verdict_json(True, ["fatal"], []))

        with self.assertRaises(register.ValidateVerdictError):
            register_release(b, registry_root=stores_root, artifact_root=artifact_root)

        self.assertFalse((b.root / "validation.yaml").exists())
        self.assertFalse(paths.store_path(b.store_id, root=artifact_root).exists())
        self.assertFalse(paths.record_path(b.store_id, "register", root=artifact_root).exists())


class TestRegisterKeepsPhaseBAcceptanceApart(unittest.TestCase):
    """Phase B acceptance evidence is kept in its own dated block, never in the run's findings (#195)."""

    COMMIT = "1" * 40

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def register_over(self, store_id: str, previous: dict[str, Any], validate_warnings: list[str]) -> dict[str, Any]:
        b, stores_root, artifact_root = create_test_bundle_and_records(self.td, store_id)
        (b.root / "validation.yaml").write_text(yaml.safe_dump(previous), encoding="utf-8")
        b = bundle.load(store_id, registry_root=stores_root)
        set_validate_output(
            store_id,
            artifact_root,
            json.dumps({"errors": [], "ok": True, "warnings": validate_warnings}) + "\n",
        )
        register_release(
            b,
            registry_root=stores_root,
            artifact_root=artifact_root,
            publish=False,
            acceptance_commit=self.COMMIT,
        )
        return yaml.safe_load((b.root / "validation.yaml").read_text(encoding="utf-8"))

    def test_candidate_evidence_appears_only_under_acceptance(self) -> None:
        """A candidate's checks, warnings and reports move verbatim into `acceptance`, dated."""
        written = self.register_over("OGS-00057", CANDIDATE_RECORD, [])

        self.assertEqual(
            written["acceptance"],
            {
                "recorded_at": CANDIDATE_RECORD["validated_at"],
                "commit": self.COMMIT,
                "checks": CANDIDATE_RECORD["checks"],
                "warnings": CANDIDATE_RECORD["warnings"],
                "reports": CANDIDATE_RECORD["reports"],
                "reference_overlap": CANDIDATE_RECORD["reference_overlap"],
                "store_composition": CANDIDATE_RECORD["store_composition"],
            },
        )
        self.assertNotIn("reference_overlap", written, "Phase B evidence stays inside the block")
        self.assertNotIn(
            "store_composition", written, "Phase B evidence stays inside the block"
        )
        for warning in CANDIDATE_RECORD["warnings"]:
            self.assertNotIn(warning, written["warnings"])
        self.assertEqual(written["warnings"], [])
        self.assertEqual(written["checks"], {"store": "passed"})
        self.assertEqual(written["status"], "passed", "Phase B's passed_with_warnings must not feed the verdict")
        self.assertNotIn("reports", written)

    def test_any_kind_of_generator_evidence_survives_registration(self) -> None:
        """`register` names no generator block: a new kind of evidence is carried as it is (#250)."""
        novel = {"novel_evidence": {"measured": 3, "notes": ["new kind of check"]}}
        dbgap_shaped = {
            "acceptance": {"human_acceptance": "pending", "membership_frozen": True},
        }
        generator_run = {"build_environment": {"repo_commit": "0" * 40}}
        written = self.register_over(
            "OGS-00061", {**CANDIDATE_RECORD, **novel, **dbgap_shaped, **generator_run}, []
        )

        self.assertEqual(written["acceptance"]["novel_evidence"], novel["novel_evidence"])
        self.assertEqual(written["acceptance"]["acceptance"], dbgap_shaped["acceptance"])
        self.assertNotIn("novel_evidence", written, "generator evidence stays inside the block")
        for envelope in ("status", "validated_at", "validator", "build_environment", "observed", "errors"):
            self.assertNotIn(envelope, written["acceptance"])

    def test_a_candidate_block_named_like_the_dating_is_refused(self) -> None:
        """Carrying it would overwrite the date or commit the block is tied to."""
        for name in ("recorded_at", "commit"):
            with self.subTest(name=name), self.assertRaises(register.RegisterError):
                self.register_over("OGS-00061", {**CANDIDATE_RECORD, name: "from the generator"}, [])

    def test_acceptance_never_mixes_with_this_runs_warnings(self) -> None:
        """With validate warnings too, each list holds only its own source's warnings."""
        run_warning = "analysis 'GCST003898' stores EAF whose orientation is unverified"
        written = self.register_over("OGS-00058", CANDIDATE_RECORD, [run_warning])

        self.assertEqual(written["warnings"], [run_warning])
        self.assertEqual(written["acceptance"]["warnings"], CANDIDATE_RECORD["warnings"])
        self.assertEqual(written["status"], "passed_with_warnings")

    def test_reregistration_carries_the_acceptance_block_verbatim(self) -> None:
        """A register-written record's `acceptance` describes the same accepted bundle, so it is kept."""
        first = self.register_over("OGS-00059", CANDIDATE_RECORD, ["first run's warning"])
        second = self.register_over("OGS-00059", first, [])

        self.assertEqual(second["acceptance"], first["acceptance"])
        self.assertEqual(second["warnings"], [])

    def test_a_register_record_without_acceptance_gives_none(self) -> None:
        """A migrated record mixes Phase B and old build findings; nothing is extracted from it."""
        legacy = {
            "status": "passed_with_warnings",
            "validator": {"name": register.VALIDATOR_NAME, "version": "opengwasdb@6ef919e"},
            "checks": {"schema": "passed", "files": "passed_with_warnings", "sd_estimation": "passed"},
            "warnings": ["GCST002047: sample_size_kind present in the manifest but missing from the built store's analyses.tsv"],
        }
        written = self.register_over("OGS-00060", legacy, [])
        self.assertNotIn("acceptance", written)


class TestForcedRegistration(unittest.TestCase):
    """A forced registration archives the replaced release and says where (#195)."""

    STAMP = "20261007T090000Z"

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def published(self, store_id: str) -> tuple[Bundle, Path, Path, bytes]:
        """A bundle whose release is published, with a new build staged beside it."""
        b, stores_root, artifact_root = create_test_bundle_and_records(self.td, store_id)
        store_p = paths.store_path(store_id, root=artifact_root)
        store_p.mkdir()
        (store_p / "version.txt").write_text("old", encoding="utf-8")
        (paths.partial_store_path(store_id, root=artifact_root) / "version.txt").write_text("new", encoding="utf-8")
        old_record = yaml.safe_dump({"status": "passed", "validator": {"name": register.VALIDATOR_NAME}})
        (b.root / "validation.yaml").write_text(old_record, encoding="utf-8")
        return bundle.load(store_id, registry_root=stores_root), stores_root, artifact_root, old_record.encode()

    def test_forced_registration_archives_the_replaced_release(self) -> None:
        b, stores_root, artifact_root, old_record = self.published("OGS-00061")
        snapshot = paths.force_snapshot_path(b.store_id, self.STAMP, root=artifact_root)
        snapshot.mkdir()
        (snapshot / "build.json").write_text("old build record", encoding="utf-8")

        written = register_release(b, registry_root=stores_root, artifact_root=artifact_root, force=True)

        archive = paths.replaced_dir(b.store_id, self.STAMP, root=artifact_root)
        store_p = paths.store_path(b.store_id, root=artifact_root)
        self.assertEqual((store_p / "version.txt").read_text(), "new")
        self.assertEqual((archive / "store.opengwasdb" / "version.txt").read_text(), "old")
        self.assertEqual((archive / "records" / "build.json").read_text(), "old build record")
        self.assertEqual((archive / "validation.yaml").read_bytes(), old_record)
        self.assertFalse(snapshot.exists())
        self.assertEqual(written["replaced"]["archive"], str(archive))
        register_rec = json.loads(paths.record_path(b.store_id, "register", root=artifact_root).read_text())
        self.assertEqual(register_rec["replaced_archive"], str(archive))

    def test_unforced_registration_over_a_published_store_is_refused(self) -> None:
        b, stores_root, artifact_root, old_record = self.published("OGS-00062")

        with self.assertRaises(run.StoreExistsError):
            register_release(b, registry_root=stores_root, artifact_root=artifact_root)

        self.assertEqual((b.root / "validation.yaml").read_bytes(), old_record)
        self.assertEqual((paths.store_path(b.store_id, root=artifact_root) / "version.txt").read_text(), "old")
        self.assertFalse((paths.store_dir(b.store_id, root=artifact_root) / "replaced").exists())


KILL_REGISTER_SCRIPT = """
import sys
from ogstores import bundle, register
stores_root, artifact_root, store_id, force, phase = sys.argv[1:6]
b = bundle.load(store_id, registry_root=stores_root)
register.register_release(b, registry_root=stores_root, artifact_root=artifact_root, force=force == "1", phase=phase)
"""


class TestPublicationTransaction(unittest.TestCase):
    """A publication killed or failing at any boundary is finished by the next run (#195)."""

    STAMP = "20261007T090000Z"
    # The boundaries only one kind of replacement crosses: a forced run
    # archives the Store it replaces, and a completion phase deletes it (#309).
    # Both cross every other boundary, so one added to the code is killed in
    # both kinds until it is named here, and fails in the kind that never
    # reaches it.
    ARCHIVE_ONLY: frozenset[str] = frozenset({"old-store-archived"})
    DELETE_ONLY: frozenset[str] = frozenset({"old-store-deleting", "old-store-deleted"})

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def replacements_crossing(self, boundary: str) -> list[bool]:
        """Each kind of replacement that crosses `boundary`, as `completion`: forced (False) or completion phase (True)."""
        return [
            completion
            for completion in (False, True)
            if boundary not in (self.ARCHIVE_ONLY if completion else self.DELETE_ONLY)
        ]

    def staged_replacement(self, store_id: str, completion: bool = False) -> tuple[Bundle, Path, Path, bytes]:
        """A published release, a validated replacement staged beside it, and the forced run's snapshot.

        With `completion`, the release completes its own Store instead: its
        build phase has published the Observed-Only Store, arrays and all, and
        its completion phase has staged the completed one, with no snapshot
        (ADR 0037).
        """
        if completion:
            b, stores_root, artifact_root = create_test_bundle_and_records(
                self.td / store_id,
                store_id,
                completion_state="reference_completed",
                command="complete-dense",
                options={"ld-panel": "/data/ld", "ancestry": "EUR"},
            )
            partial_p = paths.partial_store_path(store_id, root=artifact_root)
            (partial_p / "version.txt").write_text("old", encoding="utf-8")
            (partial_p / "z" / "c").mkdir(parents=True)
            (partial_p / "z" / "c" / "0.0").write_bytes(b"old chunk")
            register_release(b, registry_root=stores_root, artifact_root=artifact_root)
            partial_p.mkdir()
            (partial_p / "version.txt").write_text("new", encoding="utf-8")
            return bundle.load(store_id, registry_root=stores_root), stores_root, artifact_root, (b.root / "validation.yaml").read_bytes()
        b, stores_root, artifact_root = create_test_bundle_and_records(self.td / store_id, store_id)
        store_p = paths.store_path(store_id, root=artifact_root)
        store_p.mkdir()
        (store_p / "version.txt").write_text("old", encoding="utf-8")
        (paths.partial_store_path(store_id, root=artifact_root) / "version.txt").write_text("new", encoding="utf-8")
        old_record = yaml.safe_dump({"status": "passed", "validator": {"name": register.VALIDATOR_NAME}})
        (b.root / "validation.yaml").write_text(old_record, encoding="utf-8")
        snapshot = paths.force_snapshot_path(store_id, self.STAMP, root=artifact_root)
        snapshot.mkdir()
        (snapshot / "build.json").write_text("old build record", encoding="utf-8")
        return bundle.load(store_id, registry_root=stores_root), stores_root, artifact_root, old_record.encode()

    def assert_replaced(self, b: Bundle, artifact_root: Path, old_record: bytes, completion: bool = False) -> None:
        """The new release is published, the old one archived whole, and nothing is left pending.

        With `completion`, the old Store is deleted instead: its archive holds
        its register record, its Validation Record and the Observed-View
        Check's verdict, and no arrays (#309).
        """
        sid = b.store_id
        self.assertEqual((paths.store_path(sid, root=artifact_root) / "version.txt").read_text(), "new")
        if completion:
            archives = sorted((paths.store_dir(sid, root=artifact_root) / "replaced").iterdir())
            self.assertEqual(len(archives), 1, archives)
            archive = archives[0]
            self.assertEqual(sorted(p.name for p in archive.iterdir()), ["records", "validation.yaml"])
            kept = sorted(["register.json", f"{OBSERVED_VIEW_CHECK_STEP}.json"])
            self.assertEqual(sorted(p.name for p in (archive / "records").iterdir()), kept)
            for name in kept:
                record = paths.records_dir(sid, root=artifact_root) / name
                self.assertEqual((archive / "records" / name).read_bytes(), record.read_bytes(), name)
        else:
            archive = paths.replaced_dir(sid, self.STAMP, root=artifact_root)
            self.assertEqual((archive / "store.opengwasdb" / "version.txt").read_text(), "old")
            self.assertEqual((archive / "records" / "build.json").read_text(), "old build record")
        self.assertEqual((archive / "validation.yaml").read_bytes(), old_record)
        written = yaml.safe_load((b.root / "validation.yaml").read_text(encoding="utf-8"))
        self.assertEqual(written["replaced"]["archive"], str(archive))
        self.assertEqual(written["status"], "passed")
        register_step = "completion-register" if completion else "register"
        register_rec = json.loads(paths.record_path(sid, register_step, root=artifact_root).read_text())
        self.assertEqual(register_rec["replaced_archive"], str(archive))
        for leftover in (
            paths.publication_marker(sid, root=artifact_root),
            paths.force_snapshot_path(sid, self.STAMP, root=artifact_root),
            paths.backup_store_path(sid, root=artifact_root),
            paths.partial_store_path(sid, root=artifact_root),
        ):
            self.assertFalse(leftover.exists(), leftover)

    def kill_register_at(
        self, boundary: str, b: Bundle, stores_root: Path, artifact_root: Path, force: bool = True, completion: bool = False
    ) -> int:
        """Register `b`, or its completion phase with `completion`, killed at `boundary`."""
        env = dict(os.environ)
        env[run.KILL_AT_ENV] = boundary
        phase = "completion" if completion else "build"
        return subprocess.run(
            [sys.executable, "-c", KILL_REGISTER_SCRIPT, str(stores_root), str(artifact_root), b.store_id, "1" if force else "0", phase],
            env=env,
            capture_output=True,
            text=True,
        ).returncode

    def test_a_kill_after_the_marker_is_completed_by_the_next_run(self) -> None:
        """At every boundary from the marker on, a SIGKILL-like exit is finished by complete_publication.

        A forced replacement, which archives the old Store, and a completion
        phase's, which deletes it (#309), are each killed at every boundary
        they cross. The run after the one that finishes finds nothing to do.
        """
        self.assertLessEqual(self.ARCHIVE_ONLY | self.DELETE_ONLY, set(register.PUBLICATION_BOUNDARIES))
        for boundary in register.PUBLICATION_BOUNDARIES[1:]:
            for completion in self.replacements_crossing(boundary):
                with self.subTest(boundary=boundary, completion=completion):
                    index = register.PUBLICATION_BOUNDARIES.index(boundary)
                    b, stores_root, artifact_root, old_record = self.staged_replacement(
                        f"OGS-{(600 if completion else 100) + index:05d}", completion
                    )
                    killed = self.kill_register_at(
                        boundary, b, stores_root, artifact_root, force=not completion, completion=completion
                    )
                    self.assertEqual(killed, 137)
                    if boundary != "marker-removed":
                        self.assertTrue(paths.publication_marker(b.store_id, root=artifact_root).exists())
                    if boundary == "old-store-deleting":
                        # Killed mid-deletion: the old Store is half gone.
                        (half_deleted,) = (paths.store_dir(b.store_id, root=artifact_root) / "replaced").glob("*/*")
                        self.assertEqual(sorted(p.name for p in half_deleted.iterdir()), ["z"])

                    register.complete_publication(b.store_id, artifact_root)

                    self.assert_replaced(b, artifact_root, old_record, completion)
                    self.assertIsNone(register.complete_publication(b.store_id, artifact_root))

    def test_a_raise_at_any_boundary_is_completed(self) -> None:
        """An exception at every boundary from the marker on leaves a state complete_publication finishes."""
        for boundary in register.PUBLICATION_BOUNDARIES[1:-1]:
            for completion in self.replacements_crossing(boundary):
                with self.subTest(boundary=boundary, completion=completion):
                    index = register.PUBLICATION_BOUNDARIES.index(boundary)
                    b, stores_root, artifact_root, old_record = self.staged_replacement(
                        f"OGS-{(700 if completion else 200) + index:05d}", completion
                    )

                    def fail_at(name: str, _boundary: str = boundary) -> None:
                        if name == _boundary:
                            raise OSError(f"injected failure at {name}")

                    with patch.object(run, "fault_boundary", side_effect=fail_at):
                        with self.assertRaises(OSError):
                            register_release(
                                b,
                                registry_root=stores_root,
                                artifact_root=artifact_root,
                                force=not completion,
                                phase="completion" if completion else "build",
                            )
                    self.assertTrue(paths.publication_marker(b.store_id, root=artifact_root).exists())

                    register.complete_publication(b.store_id, artifact_root)

                    self.assert_replaced(b, artifact_root, old_record, completion)

    def test_a_kill_before_the_marker_leaves_the_old_release_and_its_snapshot(self) -> None:
        """Before the marker nothing is published: the old Store and record stand, and the snapshot blocks."""
        b, stores_root, artifact_root, old_record = self.staged_replacement("OGS-00300")
        self.assertEqual(self.kill_register_at("publication-started", b, stores_root, artifact_root), 137)

        self.assertIsNone(register.complete_publication(b.store_id, artifact_root))
        self.assertEqual((paths.store_path(b.store_id, root=artifact_root) / "version.txt").read_text(), "old")
        self.assertEqual((b.root / "validation.yaml").read_bytes(), old_record)
        self.assertFalse(paths.replaced_dir(b.store_id, self.STAMP, root=artifact_root).exists())
        with self.assertRaises(run.ForceSnapshotPendingError):
            run.refuse_pending_force_snapshots([(artifact_root, b.store_id)])

    def test_a_first_publication_killed_mid_way_is_completed(self) -> None:
        """A new release killed between its marker and its record is published by the next run."""
        for boundary in ("marker-written", "store-published", "record-written"):
            with self.subTest(boundary=boundary):
                store_id = f"OGS-{400 + ('marker-written', 'store-published', 'record-written').index(boundary):05d}"
                b, stores_root, artifact_root = create_test_bundle_and_records(self.td / store_id, store_id)
                (paths.partial_store_path(store_id, root=artifact_root) / "version.txt").write_text("new")
                self.assertEqual(self.kill_register_at(boundary, b, stores_root, artifact_root, force=False), 137)

                register.complete_publication(store_id, artifact_root)

                self.assertEqual((paths.store_path(store_id, root=artifact_root) / "version.txt").read_text(), "new")
                self.assertNotIn("replaced", yaml.safe_load((b.root / "validation.yaml").read_text()))
                self.assertTrue(paths.record_path(store_id, "register", root=artifact_root).is_file())
                self.assertFalse(paths.publication_marker(store_id, root=artifact_root).exists())
                self.assertFalse(paths.partial_store_path(store_id, root=artifact_root).exists())

    def test_under_the_entry_point_the_marker_outlives_the_register_job(self) -> None:
        """finalize=False: everything is published and written, and the marker is left for the entry point."""
        for completion in (False, True):
            with self.subTest(completion=completion):
                b, stores_root, artifact_root, old_record = self.staged_replacement(
                    "OGS-00505" if completion else "OGS-00502", completion
                )

                register_release(
                    b,
                    registry_root=stores_root,
                    artifact_root=artifact_root,
                    force=not completion,
                    finalize=False,
                    phase="completion" if completion else "build",
                )

                marker = paths.publication_marker(b.store_id, root=artifact_root)
                self.assertTrue(marker.exists())
                register.complete_publication(b.store_id, artifact_root, finalize=False)
                self.assertTrue(marker.exists(), "completing again without finalize keeps the marker")
                register.remove_publication_marker(b.store_id, artifact_root)
                self.assert_replaced(b, artifact_root, old_record, completion)

    def test_completing_an_already_written_publication_leaves_its_files_untouched(self) -> None:
        """A second `complete_publication` writes neither file: their mtimes stay put (#303).

        The entry point finishes a release's deferred publication after
        Snakemake's last job, and that pass rewrites `validation.yaml` and
        `records/register.json`. Bumping their mtimes makes the record look
        newer than the job that consumed it, so the next run reschedules that
        job, and #195 then refuses a run over an already published Store. The
        bytes are already there; writing them again buys nothing.
        """
        b, stores_root, artifact_root, _ = self.staged_replacement("OGS-00503")
        register_release(b, registry_root=stores_root, artifact_root=artifact_root, force=True, finalize=False)

        validation_p = b.root / "validation.yaml"
        register_p = paths.record_path(b.store_id, "register", root=artifact_root)
        before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in (validation_p, register_p)}

        # The marker still stands, so this is the entry point's pass after the
        # register job, made again on a publication that is already written.
        register.complete_publication(b.store_id, artifact_root, finalize=False)

        for p, (mtime, payload) in before.items():
            self.assertEqual(p.read_bytes(), payload, f"{p.name} holds different bytes")
            self.assertEqual(p.stat().st_mtime_ns, mtime, f"{p.name} was rewritten by an identical write")

    def test_an_impossible_state_is_raised_and_the_marker_kept(self) -> None:
        b, stores_root, artifact_root, _ = self.staged_replacement("OGS-00500")
        self.assertEqual(self.kill_register_at("store-set-aside", b, stores_root, artifact_root), 137)
        shutil.rmtree(paths.partial_store_path(b.store_id, root=artifact_root))

        with self.assertRaises(register.PublicationError):
            register.complete_publication(b.store_id, artifact_root)
        self.assertTrue(paths.publication_marker(b.store_id, root=artifact_root).exists())
        self.assertTrue(paths.backup_store_path(b.store_id, root=artifact_root).exists())

    def test_register_refuses_while_a_publication_is_pending(self) -> None:
        b, stores_root, artifact_root, _ = self.staged_replacement("OGS-00501")
        self.assertEqual(self.kill_register_at("store-published", b, stores_root, artifact_root), 137)

        with self.assertRaises(run.PublicationPendingError):
            register_release(b, registry_root=stores_root, artifact_root=artifact_root, force=True)


class TestCompletionPhaseRegistration(unittest.TestCase):
    """A release that completes its own Store publishes it twice under one id (ADR 0037).

    The completion phase's register replaces the Observed-Only Store its build
    phase published. It needs no `force` and takes no records snapshot. It
    deletes the replaced Store, and archives its register record, its
    Validation Record and the Observed-View Check's verdict under a
    `replaced/<stamp>/` of its own (#309).
    """

    STORE_ID = "OGS-00071"
    STAMP = "20261021T120000Z"

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def built_and_completed(self, td: Path | None = None) -> tuple[Bundle, Path, Path]:
        """Every step record of both phases, with the completed Store staged and nothing published."""
        b, stores_root, artifact_root = create_test_bundle_and_records(
            td or self.td,
            self.STORE_ID,
            completion_state="reference_completed",
            command="complete-dense",
            options={"ld-panel": "/data/ld", "ancestry": "EUR"},
        )
        return b, stores_root, artifact_root

    def publish_build_phase(self, b: Bundle, stores_root: Path, artifact_root: Path) -> bytes:
        """The build phase's register publishes the Observed-Only Store; then completion stages its own."""
        partial = paths.partial_store_path(b.store_id, root=artifact_root)
        (partial / "version.txt").write_text("observed", encoding="utf-8")
        (partial / "z" / "c").mkdir(parents=True)
        (partial / "z" / "c" / "0.0").write_bytes(b"observed chunk")
        register_release(b, registry_root=stores_root, artifact_root=artifact_root)
        partial.mkdir()
        (partial / "version.txt").write_text("completed", encoding="utf-8")
        return (b.root / "validation.yaml").read_bytes()

    def test_the_completion_phase_replaces_the_store_without_force_and_deletes_it(self) -> None:
        b, stores_root, artifact_root = self.built_and_completed()
        build_record = self.publish_build_phase(b, stores_root, artifact_root)
        register_p = paths.record_path(b.store_id, "register", root=artifact_root)
        build_register = register_p.read_bytes()
        check_p = paths.record_path(b.store_id, OBSERVED_VIEW_CHECK_STEP, root=artifact_root)
        # A forced run's snapshot belongs to its build phase; completion takes none.
        snapshot = paths.force_snapshot_path(b.store_id, "20261021T110000Z", root=artifact_root)
        snapshot.mkdir()

        loaded = bundle.load(b.store_id, registry_root=stores_root)
        with patch.object(run, "utc_stamp", return_value=self.STAMP):
            written = register_release(loaded, registry_root=stores_root, artifact_root=artifact_root, phase="completion")

        archive = paths.replaced_dir(b.store_id, self.STAMP, root=artifact_root)
        store_p = paths.store_path(b.store_id, root=artifact_root)
        self.assertEqual((store_p / "version.txt").read_text(), "completed")
        # The Observed-Only Store's arrays are gone; its records and the check's verdict are kept.
        self.assertEqual(sorted(p.name for p in archive.iterdir()), ["records", "validation.yaml"])
        self.assertEqual(
            sorted(p.name for p in (archive / "records").iterdir()),
            [f"{OBSERVED_VIEW_CHECK_STEP}.json", "register.json"],
        )
        self.assertEqual((archive / "validation.yaml").read_bytes(), build_record)
        self.assertEqual((archive / "records" / "register.json").read_bytes(), build_register)
        self.assertEqual((archive / "records" / check_p.name).read_bytes(), check_p.read_bytes())
        self.assertEqual(register_p.read_bytes(), build_register, "the build phase's register record stays")
        self.assertEqual(
            sorted(p.name for p in paths.store_dir(b.store_id, root=artifact_root).iterdir()),
            sorted(["records", "replaced", "store.opengwasdb", snapshot.name]),
        )
        self.assertTrue(snapshot.is_dir(), "the completion phase never takes or consumes a snapshot")

        completion_register = json.loads(
            paths.record_path(b.store_id, "completion-register", root=artifact_root).read_text()
        )
        self.assertEqual(completion_register["step"], "completion-register")
        self.assertEqual(completion_register["replaced_archive"], str(archive))
        self.assertEqual(written["replaced"]["archive"], str(archive))
        self.assertEqual(yaml.safe_load((b.root / "validation.yaml").read_text())["replaced"]["archive"], str(archive))
        self.assertFalse(paths.publication_marker(b.store_id, root=artifact_root).exists())
        self.assertFalse(paths.partial_store_path(b.store_id, root=artifact_root).exists())

    def assert_completion_refused(self, check_stdout: str | None, reason: str) -> None:
        """With this check record stdout (None: no record), the completion phase changes nothing (#309)."""
        b, stores_root, artifact_root = self.built_and_completed(Path(tempfile.mkdtemp(dir=self.td)))
        self.publish_build_phase(b, stores_root, artifact_root)
        check_p = paths.record_path(b.store_id, OBSERVED_VIEW_CHECK_STEP, root=artifact_root)
        if check_stdout is None:
            check_p.unlink()
        else:
            check_record = json.loads(check_p.read_text())
            check_record["stdout"] = check_stdout
            check_p.write_text(json.dumps(check_record))

        def on_disk() -> dict[Path, tuple[int, bytes]]:
            trees = (paths.store_dir(b.store_id, root=artifact_root), b.root)
            return {
                p: (p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else b"")
                for tree in trees
                for p in tree.rglob("*")
            }

        before = on_disk()
        with self.assertRaises(RegisterError) as ctx:
            register_release(
                bundle.load(b.store_id, registry_root=stores_root),
                registry_root=stores_root,
                artifact_root=artifact_root,
                phase="completion",
            )
        self.assertIn(str(check_p), str(ctx.exception))
        self.assertIn(reason, str(ctx.exception))
        self.assertEqual(on_disk(), before)
        self.assertFalse(paths.publication_marker(b.store_id, root=artifact_root).exists())

    def test_a_failed_observed_view_check_is_refused_before_anything_changes(self) -> None:
        self.assert_completion_refused(observed_view_verdict(passed=False), "passed: false")

    def test_an_unreadable_observed_view_verdict_is_refused_before_anything_changes(self) -> None:
        for stdout, reason in (
            ("", "no verdict"),
            ("error: refused: not a Dense Store\n", "no verdict"),
            ('{"passed": true', "no verdict"),
            ('{"passed": "true", "failures": []}', 'passed: "true"'),
            ('{"passed": null, "failures": []}', "passed: null"),
            ('{"failures": []}', "no `passed`"),
            ('[{"passed": true}]', "no verdict"),
        ):
            with self.subTest(stdout=stdout):
                self.assert_completion_refused(stdout, reason)

    def test_a_missing_observed_view_check_record_is_refused_before_anything_changes(self) -> None:
        self.assert_completion_refused(None, "missing")

    def test_the_first_verdict_of_a_doubled_stdout_is_the_one_read(self) -> None:
        """`run.execute_step` can capture a step's output twice; the first object is the verdict."""
        self.assert_completion_refused(observed_view_verdict(passed=False) + observed_view_verdict(passed=True), "passed: false")

    def test_a_forced_runs_completion_phase_archives_the_store_it_replaces(self) -> None:
        """Forced, the completion phase archives as every forced replacement does, not deletes (#309)."""
        b, stores_root, artifact_root = self.built_and_completed()
        build_record = self.publish_build_phase(b, stores_root, artifact_root)
        build_register = paths.record_path(b.store_id, "register", root=artifact_root).read_bytes()

        with patch.object(run, "utc_stamp", return_value=self.STAMP):
            register_release(
                bundle.load(b.store_id, registry_root=stores_root),
                registry_root=stores_root,
                artifact_root=artifact_root,
                force=True,
                phase="completion",
            )

        archive = paths.replaced_dir(b.store_id, self.STAMP, root=artifact_root)
        self.assertEqual((paths.store_path(b.store_id, root=artifact_root) / "version.txt").read_text(), "completed")
        self.assertEqual(sorted(p.name for p in archive.iterdir()), ["records", "store.opengwasdb", "validation.yaml"])
        self.assertEqual((archive / "store.opengwasdb" / "version.txt").read_text(), "observed")
        self.assertEqual((archive / "store.opengwasdb" / "z" / "c" / "0.0").read_bytes(), b"observed chunk")
        self.assertEqual(sorted(p.name for p in (archive / "records").iterdir()), ["register.json"])
        self.assertEqual((archive / "records" / "register.json").read_bytes(), build_register)
        self.assertEqual((archive / "validation.yaml").read_bytes(), build_record)

    def test_the_completion_phase_of_a_forced_entry_point_run_is_forced(self) -> None:
        """Forced is read from the build phase's register record: that phase used up the run's snapshot (#309).

        The entry point forces a release through its records snapshot, and the
        forced build phase's publication moves that snapshot into its archive,
        `replaced/<run stamp>/`. Its register record names that archive.
        """
        run_stamp, earlier_stamp = "20261021T130000Z", "20261021T100000Z"
        b, stores_root, artifact_root = self.built_and_completed()
        self.assertFalse(register.forced_completion(run_stamp, b.store_id, artifact_root), "nothing is published")
        self.publish_build_phase(b, stores_root, artifact_root)
        self.assertFalse(register.forced_completion(run_stamp, b.store_id, artifact_root), "an unforced build phase")

        # A forced run of the published release: its build phase replaces the Store.
        partial = paths.partial_store_path(b.store_id, root=artifact_root)
        (partial / "version.txt").write_text("observed again", encoding="utf-8")
        paths.force_snapshot_path(b.store_id, run_stamp, root=artifact_root).mkdir()
        register_release(
            bundle.load(b.store_id, registry_root=stores_root),
            registry_root=stores_root,
            artifact_root=artifact_root,
            force=True,
        )

        self.assertTrue(register.forced_completion(run_stamp, b.store_id, artifact_root))
        self.assertFalse(register.forced_completion(earlier_stamp, b.store_id, artifact_root), "another run")
        self.assertFalse(register.forced_completion(None, b.store_id, artifact_root), "outside the entry point")

    def test_unreadable_force_provenance_is_refused_and_nothing_is_deleted(self) -> None:
        """A build register record with no `replaced_archive`, or one neither text nor null, is refused (#309).

        Read as unforced, it would let a forced run's completion phase delete
        the Store it must archive. `force` is computed as the Snakefile's
        `completion_register` computes it.
        """
        for case, value in (("missing", None), ("a number", 42), ("a list", ["replaced"])):
            with self.subTest(case=case):
                b, stores_root, artifact_root = self.built_and_completed(Path(tempfile.mkdtemp(dir=self.td)))
                self.publish_build_phase(b, stores_root, artifact_root)
                register_p = paths.record_path(b.store_id, "register", root=artifact_root)
                record = json.loads(register_p.read_text())
                if case == "missing":
                    del record["replaced_archive"]
                else:
                    record["replaced_archive"] = value
                register_p.write_text(json.dumps(record))

                with self.assertRaises(RegisterError) as ctx:
                    register_release(
                        bundle.load(b.store_id, registry_root=stores_root),
                        registry_root=stores_root,
                        artifact_root=artifact_root,
                        force=register.forced_completion("20261021T130000Z", b.store_id, artifact_root),
                        phase="completion",
                    )
                self.assertIn(str(register_p), str(ctx.exception))
                store_p = paths.store_path(b.store_id, root=artifact_root)
                self.assertEqual((store_p / "z" / "c" / "0.0").read_bytes(), b"observed chunk")
                self.assertTrue(paths.partial_store_path(b.store_id, root=artifact_root).is_dir())
                self.assertFalse((paths.store_dir(b.store_id, root=artifact_root) / "replaced").exists())
                self.assertFalse(paths.publication_marker(b.store_id, root=artifact_root).exists())

    def test_a_marker_written_before_deletion_existed_still_archives(self) -> None:
        """A pending marker without `replaced_store`, as a crash before #309 left it, archives the Store."""
        b, stores_root, artifact_root = self.built_and_completed()
        build_record = self.publish_build_phase(b, stores_root, artifact_root)

        def crash_at(name: str) -> None:
            if name == "store-published":
                raise OSError("injected crash")

        with patch.object(run, "utc_stamp", return_value=self.STAMP), patch.object(run, "fault_boundary", side_effect=crash_at):
            with self.assertRaises(OSError):
                register_release(
                    bundle.load(b.store_id, registry_root=stores_root),
                    registry_root=stores_root,
                    artifact_root=artifact_root,
                    phase="completion",
                )
        marker_p = paths.publication_marker(b.store_id, root=artifact_root)
        marker = json.loads(marker_p.read_text())
        self.assertEqual(marker.pop("replaced_store"), "delete")
        marker_p.write_text(json.dumps(marker))

        register.complete_publication(b.store_id, artifact_root)

        archive = paths.replaced_dir(b.store_id, self.STAMP, root=artifact_root)
        self.assertEqual((archive / "store.opengwasdb" / "version.txt").read_text(), "observed")
        self.assertEqual(sorted(p.name for p in (archive / "records").iterdir()), ["register.json"])
        self.assertEqual((archive / "validation.yaml").read_bytes(), build_record)
        self.assertEqual((paths.store_path(b.store_id, root=artifact_root) / "version.txt").read_text(), "completed")
        self.assertFalse(marker_p.exists())

    def test_a_completion_that_read_its_staged_store_is_drift(self) -> None:
        """The published Store `complete` reads must be in its record as planned, never staged.

        Normalising every `.partial` token back would turn a completion that
        read its own staged Store into its plan's argv, a wrong answer that
        looks right. Only the destination is staged (ADR 0037).
        """
        b, stores_root, artifact_root = self.built_and_completed()
        complete = next(s for s in plan(b, artifact_root=artifact_root) if s.name == "complete")
        record = json.loads(paths.record_path(b.store_id, "complete", root=artifact_root).read_text())
        partial = str(paths.partial_store_path(b.store_id, root=artifact_root))
        self.assertEqual(check_argv_drift(complete, record, b.store_id, artifact_root=artifact_root), (True, False))

        record["argv"][complete.published_store_arg] = partial
        with self.assertRaises(ArgvDriftError) as ctx:
            check_argv_drift(complete, record, b.store_id, artifact_root=artifact_root)
        self.assertIn("published Store", str(ctx.exception))

    def test_the_completed_record_times_every_step_of_the_release(self) -> None:
        """`build_elapsed_s` sums every step's elapsed seconds, the build phase's too.

        Each fixture record took 5 s: `build`, `overview` and `validate`, then
        `complete`, `completion-overview`, `completion-validate` and
        `completion-observed-view-check`.
        """
        b, stores_root, artifact_root = self.built_and_completed()
        self.publish_build_phase(b, stores_root, artifact_root)
        built = yaml.safe_load((b.root / "validation.yaml").read_text())
        self.assertEqual(built["observed"]["build_elapsed_s"], 15.0)

        written = register_release(
            bundle.load(b.store_id, registry_root=stores_root),
            registry_root=stores_root,
            artifact_root=artifact_root,
            phase="completion",
        )
        self.assertEqual(written["observed"]["build_elapsed_s"], 35.0)

    def test_a_replacement_in_the_same_second_takes_its_own_stamp(self) -> None:
        """A forced run's build phase may archive at the same second; completion's stamp is still new."""
        b, stores_root, artifact_root = self.built_and_completed()
        self.publish_build_phase(b, stores_root, artifact_root)
        taken = paths.replaced_dir(b.store_id, self.STAMP, root=artifact_root)
        taken.mkdir(parents=True)

        with patch.object(run, "utc_stamp", return_value=self.STAMP):
            register_release(
                bundle.load(b.store_id, registry_root=stores_root),
                registry_root=stores_root,
                artifact_root=artifact_root,
                phase="completion",
            )

        self.assertEqual(sorted(p.name for p in taken.iterdir()), [])
        archive = paths.replaced_dir(b.store_id, f"{self.STAMP}-2", root=artifact_root)
        self.assertEqual(sorted(p.name for p in archive.iterdir()), ["records", "validation.yaml"])


class TestCommittedRevision(unittest.TestCase):
    """`committed_revision` names the commit a file came from, or None when it is not that commit."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    def test_revision_of_committed_modified_and_untracked_files(self) -> None:
        record = self.repo / "validation.yaml"
        self.assertIsNone(register.committed_revision(record), "not a git repository")

        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "test")
        record.write_text("status: passed\n", encoding="utf-8")
        self.assertIsNone(register.committed_revision(record), "untracked")

        self.git("add", "validation.yaml")
        self.git("commit", "-q", "-m", "candidate")
        self.assertEqual(register.committed_revision(record), self.git("rev-parse", "HEAD"))

        record.write_text("status: failed\n", encoding="utf-8")
        self.assertIsNone(register.committed_revision(record), "modified since its commit")


class TestArgvDriftAndResumptionDivergence(unittest.TestCase):
    """Argv drift detection, staging normalization, and complete-dense-resume divergence."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_argv_drift_check_fails_and_names_difference(self) -> None:
        """Executed argv differing from planned argv raises ArgvDriftError and names difference."""
        b, stores_root, artifact_root = create_test_bundle_and_records(self.td, "OGS-00044")

        # Mutate build record's executed argv to introduce drift (e.g. changed worker count)
        build_rec_p = paths.record_path("OGS-00044", "build", root=artifact_root)
        rec = json.loads(build_rec_p.read_text(encoding="utf-8"))
        rec["argv"].append("--unexpected-flag")
        run._write_record_atomically(rec, build_rec_p)

        with self.assertRaises(ArgvDriftError) as ctx:
            register_release(b, registry_root=stores_root, artifact_root=artifact_root)

        err_msg = str(ctx.exception)
        self.assertIn("build", err_msg)
        self.assertIn("OGS-00044", err_msg)
        self.assertIn("--unexpected-flag", err_msg)
        self.assertIn("Planned:", err_msg)
        self.assertIn("Executed:", err_msg)

    def test_complete_dense_resume_accepted_and_recorded_as_resumed_true(self) -> None:
        """complete-dense-resume is accepted as legitimate divergence for complete-dense and marked resumed: true."""
        b, stores_root, artifact_root = create_test_bundle_and_records(
            self.td,
            "OGS-00045",
            completion_state="reference_completed",
            command="complete-dense",
            derived_from="OGS-00042",
        )

        # Update complete record's argv to complete-dense-resume with checkpoint path
        comp_rec_p = paths.record_path("OGS-00045", "complete", root=artifact_root)
        rec = json.loads(comp_rec_p.read_text(encoding="utf-8"))
        ckpt_path = str(paths.partial_store_path("OGS-00045", root=artifact_root).parent / ".store.opengwasdb.partial.checkpoint")
        rec["argv"] = ["opengwasdb", "complete-dense-resume", ckpt_path]
        run._write_record_atomically(rec, comp_rec_p)

        val_data = register_release(b, registry_root=stores_root, artifact_root=artifact_root)
        self.assertTrue(val_data["observed"].get("resumed"), "Expected observed.resumed to be True")

    def test_complete_dense_without_resume_has_no_resumed_flag(self) -> None:
        """complete-dense run without resume registers normally without resumed: true."""
        b, stores_root, artifact_root = create_test_bundle_and_records(
            self.td,
            "OGS-00046",
            completion_state="reference_completed",
            command="complete-dense",
            derived_from="OGS-00042",
        )

        val_data = register_release(b, registry_root=stores_root, artifact_root=artifact_root)
        self.assertNotIn("resumed", val_data["observed"])

    def test_missing_step_record_raises_missing_record_error(self) -> None:
        """Missing required step record raises MissingRecordError naming the step."""
        b, stores_root, artifact_root = create_test_bundle_and_records(self.td, "OGS-00047")

        # Delete overview.json record
        overview_rec_p = paths.record_path("OGS-00047", "overview", root=artifact_root)
        overview_rec_p.unlink()

        with self.assertRaises(MissingRecordError) as ctx:
            register_release(b, registry_root=stores_root, artifact_root=artifact_root)

        self.assertIn("overview", str(ctx.exception))
        self.assertIn("OGS-00047", str(ctx.exception))


class TestRegisterStrictSeamAndTripwires(unittest.TestCase):
    """Assert register opens NO Store files, runs NO subprocesses, and re-runs NO validation."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_tripwires_register_opens_no_store_and_reruns_no_validation(self) -> None:
        """Strict seam proof (ADR 0023): register opens no Store internals and spawns no subprocesses."""
        b, stores_root, artifact_root = create_test_bundle_and_records(self.td, "OGS-00048")
        target_store_dir = paths.store_path("OGS-00048", root=artifact_root)
        partial_store_dir = paths.partial_store_path("OGS-00048", root=artifact_root)

        # Place fake files inside partial store directory
        (partial_store_dir / "manifest.json").write_text('{"store_id": "test"}')
        (partial_store_dir / "analyses.tsv").write_text("analysis_id\n")

        real_open = builtins.open
        store_read_attempts: list[str] = []

        def guarded_open(file: Any, *args: Any, **kwargs: Any) -> Any:
            path_str = str(file)
            # Tripwire: flag any attempt to read inside store.opengwasdb or store.opengwasdb.partial
            if (
                (str(partial_store_dir) in path_str and not path_str.endswith(".checkpoint"))
                or (str(target_store_dir) in path_str and not path_str.endswith(".checkpoint"))
            ):
                mode = args[0] if args else kwargs.get("mode", "r")
                if "r" in mode:
                    store_read_attempts.append(path_str)
                    raise AssertionError(f"Tripwire: register illegally opened Store file {path_str} in mode {mode!r}")
            return real_open(file, *args, **kwargs)

        subprocess_attempts: list[list[str]] = []

        def guarded_popen(*args: Any, **kwargs: Any) -> Any:
            cmd = args[0] if args else kwargs.get("args")
            subprocess_attempts.append(list(cmd))
            raise AssertionError(f"Tripwire: register illegally invoked subprocess: {cmd}")

        original_popen = subprocess.Popen
        try:
            builtins.open = guarded_open  # type: ignore[assignment]
            subprocess.Popen = guarded_popen  # type: ignore[assignment]

            # Execute registration
            val_data = register_release(b, registry_root=stores_root, artifact_root=artifact_root, publish=True)
            self.assertEqual(val_data["status"], "passed")
        finally:
            builtins.open = real_open  # type: ignore[assignment]
            subprocess.Popen = original_popen  # type: ignore[assignment]

        self.assertEqual(store_read_attempts, [], "Register attempted to read Store internal files")
        self.assertEqual(subprocess_attempts, [], "Register attempted to spawn subprocesses")


class TestVariantReferenceRegistration(unittest.TestCase):
    """Register validates the variant-reference step argv with staged-path normalization (#145/#147)."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _declaring_bundle(self) -> tuple[Bundle, Path, Path, str]:
        ref = str(self.td / "refs" / "panel.tsv.gz")
        b, stores_root, artifact_root = create_test_bundle_and_records(
            self.td,
            "OGS-00042",
            options={"variant-reference": ref, "source-assembly": "hg38"},
            variant_reference={"output": ref, "options": {"n-workers": 4}},
        )
        return b, stores_root, artifact_root, ref

    def test_normalization_maps_staged_output_path_back(self) -> None:
        """The staged `--output-path` normalizes to the declared destination."""
        target = Path("/refs/panel.tsv.gz")
        planned = Step(
            name="variant-reference",
            argv=["opengwasdb", "extract-variant-reference", "/m.tsv", "--output-path", str(target)],
            inputs=[],
            outputs=[target],
        )
        staged = str(run.variant_reference_partial_path(target))
        executed = ["opengwasdb", "extract-variant-reference", "/m.tsv", "--output-path", staged]
        self.assertEqual(
            register.normalize_executed_argv_for_variant_reference(executed, planned),
            planned.argv,
        )

    def test_planned_extraction_record_registers_without_drift(self) -> None:
        """A staged extraction record registers cleanly against the planned argv."""
        b, stores_root, artifact_root, ref = self._declaring_bundle()
        rec = run.load_record(b.store_id, "variant-reference", root=artifact_root)
        self.assertIsNotNone(rec)
        self.assertIn(str(run.variant_reference_partial_path(Path(ref))), rec["argv"])

        val = register_release(b, registry_root=stores_root, artifact_root=artifact_root, publish=False)
        self.assertEqual(val["status"], "passed")

    def test_skipped_extraction_record_registers_without_drift(self) -> None:
        """A skipped record carries the planned argv and registers cleanly."""
        b, stores_root, artifact_root, _ref = self._declaring_bundle()
        rec_p = paths.record_path(b.store_id, "variant-reference", root=artifact_root)
        rec = json.loads(rec_p.read_text(encoding="utf-8"))
        rec["argv"] = list(rec["planned_argv"])
        rec["skipped"] = True
        rec["skip_reason"] = "provided"
        rec["elapsed_seconds"] = 0.0
        run._write_record_atomically(rec, rec_p)

        val = register_release(b, registry_root=stores_root, artifact_root=artifact_root, publish=False)
        self.assertEqual(val["status"], "passed")

    def test_drifted_extraction_record_raises(self) -> None:
        """A changed extract flag is caught as argv drift."""
        b, stores_root, artifact_root, _ref = self._declaring_bundle()
        rec_p = paths.record_path(b.store_id, "variant-reference", root=artifact_root)
        rec = json.loads(rec_p.read_text(encoding="utf-8"))
        idx = rec["argv"].index("--n-workers")
        rec["argv"][idx + 1] = "8"
        run._write_record_atomically(rec, rec_p)

        with self.assertRaises(ArgvDriftError):
            register_release(b, registry_root=stores_root, artifact_root=artifact_root, publish=False)

    def test_observed_records_extracted_when_the_pre_stage_ran(self) -> None:
        """A declared pre-stage that executed records variant_reference=extracted."""
        b, stores_root, artifact_root, _ref = self._declaring_bundle()
        val = register_release(b, registry_root=stores_root, artifact_root=artifact_root, publish=False)
        self.assertEqual(val["observed"]["variant_reference"], "extracted")

        written = yaml.safe_load((b.root / "validation.yaml").read_text(encoding="utf-8"))
        self.assertEqual(written["observed"]["variant_reference"], "extracted")

    def test_observed_records_provided_when_the_pre_stage_was_skipped(self) -> None:
        """A skipped pre-stage records variant_reference=provided."""
        b, stores_root, artifact_root, _ref = self._declaring_bundle()
        rec_p = paths.record_path(b.store_id, "variant-reference", root=artifact_root)
        rec = json.loads(rec_p.read_text(encoding="utf-8"))
        rec["argv"] = list(rec["planned_argv"])
        rec["skipped"] = True
        rec["skip_reason"] = "provided"
        run._write_record_atomically(rec, rec_p)

        val = register_release(b, registry_root=stores_root, artifact_root=artifact_root, publish=False)
        self.assertEqual(val["observed"]["variant_reference"], "provided")

        written = yaml.safe_load((b.root / "validation.yaml").read_text(encoding="utf-8"))
        self.assertEqual(written["observed"]["variant_reference"], "provided")

    def test_observed_records_provided_when_only_the_build_option_names_a_panel(self) -> None:
        """A build option naming a panel with no declared pre-stage (OGS-00004/5) is provided."""
        ref = str(self.td / "refs" / "shared-panel.txt")
        b, stores_root, artifact_root = create_test_bundle_and_records(
            self.td,
            "OGS-00043",
            options={"variant-reference": ref, "source-assembly": "hg38"},
        )
        self.assertNotIn("variant-reference", [s.name for s in plan(b, artifact_root=artifact_root)])

        val = register_release(b, registry_root=stores_root, artifact_root=artifact_root, publish=False)
        self.assertEqual(val["observed"]["variant_reference"], "provided")

    def test_observed_records_no_variant_reference_key_when_unused(self) -> None:
        """A release that uses no variant reference carries no provenance key."""
        b, stores_root, artifact_root = create_test_bundle_and_records(self.td, "OGS-00044")
        val = register_release(b, registry_root=stores_root, artifact_root=artifact_root, publish=False)
        self.assertNotIn("variant_reference", val["observed"])


if __name__ == "__main__":
    unittest.main()

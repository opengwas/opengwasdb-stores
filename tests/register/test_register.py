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

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
SRC_DIR: Path = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from ogstores import bundle, manifest, paths, register, run
from ogstores.bundle import Bundle
from ogstores.plan import Step, plan
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
            executed_argv = [
                token.replace(str(paths.store_path(store_id, root=artifact_root)), str(partial_p))
                for token in step.argv
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
            elif step.name == "validate":
                # The shape `opengwasdb validate --format json` prints (#195).
                stdout_payload = json.dumps({"errors": [], "ok": True, "warnings": []}) + "\n"

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
            },
        )
        self.assertNotIn("reference_overlap", written, "Phase B evidence stays inside the block")
        for warning in CANDIDATE_RECORD["warnings"]:
            self.assertNotIn(warning, written["warnings"])
        self.assertEqual(written["warnings"], [])
        self.assertEqual(written["checks"], {"store": "passed"})
        self.assertEqual(written["status"], "passed", "Phase B's passed_with_warnings must not feed the verdict")
        self.assertNotIn("reports", written)

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
sys.path.insert(0, sys.argv[1])
from ogstores import bundle, register
stores_root, artifact_root, store_id, force = sys.argv[2:6]
b = bundle.load(store_id, registry_root=stores_root)
register.register_release(b, registry_root=stores_root, artifact_root=artifact_root, force=force == "1")
"""


class TestPublicationTransaction(unittest.TestCase):
    """A publication killed or failing at any boundary is finished by the next run (#195)."""

    STAMP = "20261007T090000Z"

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def staged_replacement(self, store_id: str) -> tuple[Bundle, Path, Path, bytes]:
        """A published release, a validated replacement staged beside it, and the forced run's snapshot."""
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

    def assert_replaced(self, b: Bundle, artifact_root: Path, old_record: bytes) -> None:
        """The new release is published, the old one archived whole, and nothing is left pending."""
        sid = b.store_id
        archive = paths.replaced_dir(sid, self.STAMP, root=artifact_root)
        self.assertEqual((paths.store_path(sid, root=artifact_root) / "version.txt").read_text(), "new")
        self.assertEqual((archive / "store.opengwasdb" / "version.txt").read_text(), "old")
        self.assertEqual((archive / "records" / "build.json").read_text(), "old build record")
        self.assertEqual((archive / "validation.yaml").read_bytes(), old_record)
        written = yaml.safe_load((b.root / "validation.yaml").read_text(encoding="utf-8"))
        self.assertEqual(written["replaced"]["archive"], str(archive))
        self.assertEqual(written["status"], "passed")
        register_rec = json.loads(paths.record_path(sid, "register", root=artifact_root).read_text())
        self.assertEqual(register_rec["replaced_archive"], str(archive))
        for leftover in (
            paths.publication_marker(sid, root=artifact_root),
            paths.force_snapshot_path(sid, self.STAMP, root=artifact_root),
            paths.backup_store_path(sid, root=artifact_root),
            paths.partial_store_path(sid, root=artifact_root),
        ):
            self.assertFalse(leftover.exists(), leftover)

    def kill_register_at(self, boundary: str, b: Bundle, stores_root: Path, artifact_root: Path, force: bool = True) -> int:
        env = dict(os.environ)
        env[run.KILL_AT_ENV] = boundary
        return subprocess.run(
            [sys.executable, "-c", KILL_REGISTER_SCRIPT, str(SRC_DIR), str(stores_root), str(artifact_root), b.store_id, "1" if force else "0"],
            env=env,
            capture_output=True,
            text=True,
        ).returncode

    def test_a_kill_after_the_marker_is_completed_by_the_next_run(self) -> None:
        """At every boundary from the marker on, a SIGKILL-like exit is finished by complete_publication."""
        for boundary in register.PUBLICATION_BOUNDARIES[1:]:
            with self.subTest(boundary=boundary):
                b, stores_root, artifact_root, old_record = self.staged_replacement(f"OGS-{100 + register.PUBLICATION_BOUNDARIES.index(boundary):05d}")
                self.assertEqual(self.kill_register_at(boundary, b, stores_root, artifact_root), 137)
                if boundary != "marker-removed":
                    self.assertTrue(paths.publication_marker(b.store_id, root=artifact_root).exists())

                register.complete_publication(b.store_id, artifact_root)

                self.assert_replaced(b, artifact_root, old_record)

    def test_a_raise_at_any_boundary_is_completed(self) -> None:
        """An exception at every boundary from the marker on leaves a state complete_publication finishes."""
        for boundary in register.PUBLICATION_BOUNDARIES[1:-1]:
            with self.subTest(boundary=boundary):
                b, stores_root, artifact_root, old_record = self.staged_replacement(f"OGS-{200 + register.PUBLICATION_BOUNDARIES.index(boundary):05d}")

                def fail_at(name: str, _boundary: str = boundary) -> None:
                    if name == _boundary:
                        raise OSError(f"injected failure at {name}")

                with patch.object(run, "fault_boundary", side_effect=fail_at):
                    with self.assertRaises(OSError):
                        register_release(b, registry_root=stores_root, artifact_root=artifact_root, force=True)
                self.assertTrue(paths.publication_marker(b.store_id, root=artifact_root).exists())

                register.complete_publication(b.store_id, artifact_root)

                self.assert_replaced(b, artifact_root, old_record)

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

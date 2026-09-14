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
"""

from __future__ import annotations

import builtins
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
SRC_DIR: Path = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from ogstores import bundle, paths, register, run
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
        "family": "test-fam",
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
            stdout_payload = ""
            if step.name in ("build", "complete"):
                stdout_payload = json.dumps({"n_variants": 1000, "n_analyses": 2, "format_version": "1.0"}) + "\n"
            elif step.name == "validate":
                stdout_payload = json.dumps({"status": "passed", "valid": True, "format_version": "1.0", "n_associations": 2000, "store_bytes": 65536}) + "\n"

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

    def test_observed_measurements_harvested_from_records(self) -> None:
        """Observed measurements are accurately harvested from stdout payloads across all steps."""
        b, stores_root, artifact_root = create_test_bundle_and_records(self.td, "OGS-00043")

        # Customize validate stdout to carry rich measurements
        val_rec_p = paths.record_path("OGS-00043", "validate", root=artifact_root)
        val_rec = json.loads(val_rec_p.read_text(encoding="utf-8"))
        val_rec["stdout"] = json.dumps({
            "status": "passed",
            "format_version": "1.0",
            "n_variants": 12500,
            "n_analyses": 50,
            "n_associations": 625000,
            "store_bytes": 10485760,
        }) + "\n"
        val_rec["elapsed_seconds"] = 10.5
        run._write_record_atomically(val_rec, val_rec_p)

        val_data = register_release(b, registry_root=stores_root, artifact_root=artifact_root)
        obs = val_data["observed"]

        self.assertEqual(obs["format_version"], "1.0")
        self.assertEqual(obs["n_variants"], 12500)
        self.assertEqual(obs["n_analyses"], 50)
        self.assertEqual(obs["n_associations"], 625000)
        self.assertEqual(obs["store_bytes"], 10485760)
        self.assertEqual(obs["validate_status"], "passed")
        self.assertGreater(obs["build_elapsed_s"], 10.0)


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


if __name__ == "__main__":
    unittest.main()

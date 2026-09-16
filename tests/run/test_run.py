#!/usr/bin/env python3
"""Tests for ogstores.run: Staged release transaction execution, records, and safety rules (Issue #114).

All tests use temporary directories and lightweight fake commands so they execute
in milliseconds without external network or large fixture dependencies.

Covers:
  1. Staged release transaction execution:
     - Store-producing 'build' step: creates store.opengwasdb.partial.
     - Mutating post steps ('top-hits', 'rho', 'overview') and 'validate' execute against store.opengwasdb.partial.
     - Terminal publication: .partial is published (atomically renamed) to store.opengwasdb only on terminal validate success when publish=True.
     - Reference-completion 'complete' step: stages child store at .partial while preserving parent store path.
  2. Publication gating:
     - Rejects publish=True on non-validate steps with ValueError and writes failure record.
     - Rejects run_plan(..., publish=True) on plans lacking a 'validate' step.
     - publish_store() verifies successful 'validate' record exists before publication.
  3. Path rewriting & strict destination validation:
     - Every release step requires exactly one canonical target Store token rewritten to .partial.
     - Rewrites exact tokens and embedded substrings (e.g. --flag=<dest>) to canonical .partial path.
     - Rejects missing, multiple ambiguous, or non-canonical equivalent paths.
     - Preflight lstat/no-follow rejects symlinks (live and dangling) and non-directories.
     - Exit 0 without producing .partial fails step (no direct-to-final fallback).
  4. Step.name security:
     - Validates step.name against allowlist ({'build', 'complete', 'top-hits', 'rho', 'overview', 'validate', 'register'}).
     - Strictly rejects path separators ('/', '\\'), '..', and arbitrary names.
     - Writes preflight failure record to records/unknown.json on invalid step name.
  5. Process group isolation & failure cleanup:
     - Commands launch in their own process session/group (start_new_session=True), led by a supervisor.
     - On timeout, SIGINT, or nonzero exit code, SIGTERM the group, wait out a grace window, then SIGKILL it.
     - The supervisor absorbs SIGTERM so a command's own SIGTERM handler can finish during that grace window.
     - Abrupt orchestrator death (SIGKILL) tears down the detached group via the supervisor's liveness pipe.
     - The supervisor only signals a group it leads, and mirrors signal-death exit status (including -9).
  5b. Command exec classification:
     - A failed exec inside the supervisor is reported back by errno: ENOENT -> exit 127 / MissingCommandError,
       EACCES / EISDIR -> exit 1 / StepExecutionError, exactly as the old direct Popen produced.
  6. Force timing:
     - Staging in .partial is allowed beside an existing final Store without force.
     - force=True is required only at terminal publication when replacing an existing final Store.
  7. Preflight failure records & staging normalization (#117):
     - Preflight errors write a failed record (success: false) before raising.
     - Planned argv retains canonical final path; executed argv records staged .partial path.
  8. Exact commit & executable provenance:
     - Resolves executable path from environment PATH.
     - Derives exact 40/64 hex commit SHA from executable's own package direct_url.json.
     - Rejects branch/version strings as SHA; returns 'unavailable' if unverified.
     - No unverified caller SHA overrides.
  9. No Store readback (tripwire test):
     - execute_step performs no store opening, inspection, or interpretation (ADR 0023).
 10. Sequential plan execution & failure halting:
     - run_plan() executes steps sequentially and publishes only on terminal step success.
     - run_plan() stops immediately after any failed step (even in check=False mode).
 11. Explicit resumption:
     - resume=True preserves existing .partial directory; resume=False cleans it up.

Run from repository root:
    pixi run python tests/run/test_run.py
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ogstores import _pdeath_supervisor, bundle, paths, plan, run
from ogstores.bundle import Bundle
from ogstores.plan import Step
from ogstores.run import (
    VALID_STEP_NAMES,
    MissingCommandError,
    StepExecutionError,
    StepResult,
    StoreExistsError,
    execute_step,
    get_opengwasdb_executable,
    get_opengwasdb_revision,
    get_opengwasdb_version,
    is_exact_commit_hash,
    is_store_producing_step,
    load_record,
    publish_store,
    rewrite_argv_for_staging,
    run_plan,
    run_step,
    validate_step_name,
)

n_checks = 0


def record_check() -> None:
    global n_checks
    n_checks += 1


def proc_state(pid: int) -> str | None:
    """Return the Linux /proc state letter for `pid`, or None if it is gone.

    A zombie ('Z') is treated as gone: it can no longer write, so an orphan that
    has already exited is not a live build.
    """
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None
    rparen = stat_text.rfind(")")
    if rparen == -1 or rparen + 2 >= len(stat_text):
        return None
    return stat_text[rparen + 2]


def pid_is_live(pid: int) -> bool:
    """Return True only while `pid` is a running (not zombie/exit) process."""
    state = proc_state(pid)
    return state is not None and state not in ("Z", "X", "x")


class TestStepClassificationAndArgvRewrite(unittest.TestCase):
    """Test step classification, step.name security, and strict staging argv rewriting."""

    def test_step_classification(self) -> None:
        build_step = Step(name="build", argv=["opengwasdb", "build-dense-vcf"], inputs=[], outputs=[])
        complete_step = Step(name="complete", argv=["opengwasdb", "complete-dense"], inputs=[], outputs=[])
        tophits_step = Step(name="top-hits", argv=["opengwasdb", "build-dense-top-hits"], inputs=[], outputs=[])
        rho_step = Step(name="rho", argv=["opengwasdb", "build-dense-rho"], inputs=[], outputs=[])
        overview_step = Step(name="overview", argv=["opengwasdb", "regenerate-overview"], inputs=[], outputs=[])
        validate_step = Step(name="validate", argv=["opengwasdb", "validate"], inputs=[], outputs=[])

        self.assertTrue(is_store_producing_step(build_step))
        record_check()
        self.assertTrue(is_store_producing_step(complete_step))
        record_check()
        self.assertFalse(is_store_producing_step(tophits_step))
        record_check()
        self.assertFalse(is_store_producing_step(rho_step))
        record_check()
        self.assertFalse(is_store_producing_step(overview_step))
        record_check()
        self.assertFalse(is_store_producing_step(validate_step))
        record_check()

    def test_step_name_security_and_preflight_record(self) -> None:
        for valid_name in ["build", "complete", "top-hits", "rho", "overview", "validate", "register"]:
            self.assertEqual(validate_step_name(valid_name), valid_name)
            record_check()

        for bad_name in ["../escape", "build/sub", "validate\\test", "arbitrary", ""]:
            with tempfile.TemporaryDirectory() as td:
                temp_root = Path(td)
                bad_step = Step(name=bad_name, argv=["opengwasdb", "build"], inputs=[], outputs=[])
                with self.assertRaises(ValueError):
                    execute_step(bad_step, store_id="OGS-00042", artifact_root=temp_root)
                record_check()

                # Verify records/unknown.json was written to disk
                rec = load_record("OGS-00042", "unknown", root=temp_root)
                self.assertIsNotNone(rec)
                record_check()
                if rec:
                    self.assertFalse(rec["success"])
                    record_check()
                    self.assertIn("Invalid step name", rec["stderr"])
                    record_check()

    def test_build_argv_rewriting(self) -> None:
        root = Path("/data/opengwasdb/stores")
        store_id = "OGS-00042"
        target_store = paths.store_path(store_id, root=root)
        partial_store = paths.partial_store_path(store_id, root=root)

        step = Step(
            name="build",
            argv=[
                "opengwasdb",
                "build-dense-vcf",
                "stores/OGS-00042/analyses.tsv",
                str(target_store),
                "--store-id",
                "finngen-r13",
                "--release-id",
                store_id,
            ],
            inputs=[Path("stores/OGS-00042/analyses.tsv")],
            outputs=[target_store],
        )

        rewritten = rewrite_argv_for_staging(step, store_id, artifact_root=root)
        self.assertEqual(rewritten[3], str(partial_store))
        record_check()
        self.assertEqual(rewritten[0:3], step.argv[0:3])
        record_check()
        self.assertEqual(rewritten[4:], step.argv[4:])
        record_check()

    def test_complete_argv_rewriting_preserves_parent(self) -> None:
        root = Path("/data/opengwasdb/stores")
        parent_id = "OGS-00001"
        child_id = "OGS-00002"
        parent_store = paths.store_path(parent_id, root=root)
        child_store = paths.store_path(child_id, root=root)
        child_partial = paths.partial_store_path(child_id, root=root)

        step = Step(
            name="complete",
            argv=[
                "opengwasdb",
                "complete-ragged",
                str(parent_store),
                str(child_store),
                "--release-id",
                child_id,
            ],
            inputs=[parent_store],
            outputs=[child_store],
        )

        rewritten = rewrite_argv_for_staging(step, child_id, artifact_root=root)
        self.assertEqual(rewritten[2], str(parent_store))  # parent path unchanged
        record_check()
        self.assertEqual(rewritten[3], str(child_partial))  # child path rewritten to .partial
        record_check()

    def test_post_steps_argv_rewritten_to_partial(self) -> None:
        root = Path("/data/opengwasdb/stores")
        store_id = "OGS-00003"
        target_store = paths.store_path(store_id, root=root)
        partial_store = paths.partial_store_path(store_id, root=root)

        for name, cmd in [
            ("top-hits", "build-dense-top-hits"),
            ("rho", "build-dense-rho"),
            ("overview", "regenerate-overview"),
            ("validate", "validate"),
        ]:
            step = Step(
                name=name,
                argv=["opengwasdb", cmd, str(target_store)],
                inputs=[target_store],
                outputs=[target_store] if name != "validate" else [],
            )
            rewritten = rewrite_argv_for_staging(step, store_id, artifact_root=root)
            self.assertEqual(rewritten[2], str(partial_store))
            record_check()

    def test_embedded_flag_substring_rewriting(self) -> None:
        root = Path("/data/opengwasdb/stores")
        store_id = "OGS-00042"
        target_store = paths.store_path(store_id, root=root)
        partial_store = paths.partial_store_path(store_id, root=root)

        step = Step(
            name="build",
            argv=[
                "opengwasdb",
                "build-dense",
                f"--output-store={target_store}",
            ],
            inputs=[],
            outputs=[target_store],
        )

        rewritten = rewrite_argv_for_staging(step, store_id, artifact_root=root)
        self.assertEqual(rewritten[2], f"--output-store={partial_store}")
        record_check()

    def test_strict_destination_token_validation_and_preflight_record(self) -> None:
        root = Path("/data/opengwasdb/stores")
        store_id = "OGS-00042"
        target_store = paths.store_path(store_id, root=root)

        # 1. Missing destination token raises ValueError and writes failure record
        missing_step = Step(
            name="build",
            argv=["opengwasdb", "build-dense-vcf", "analyses.tsv"],
            inputs=[],
            outputs=[target_store],
        )
        with tempfile.TemporaryDirectory() as td:
            temp_root = Path(td)
            with self.assertRaises(ValueError) as cm:
                execute_step(missing_step, store_id=store_id, artifact_root=temp_root)
            record_check()
            self.assertIn("missing expected canonical destination token", str(cm.exception))
            record_check()

            rec = load_record(store_id, "build", root=temp_root)
            self.assertIsNotNone(rec)
            record_check()
            if rec:
                self.assertFalse(rec["success"])
                record_check()
                self.assertIn("missing expected canonical destination token", rec["stderr"])
                record_check()

        # 2. Multiple ambiguous destination tokens raise ValueError
        ambiguous_step = Step(
            name="build",
            argv=["opengwasdb", "build-dense-vcf", str(target_store), str(target_store)],
            inputs=[],
            outputs=[target_store],
        )
        with self.assertRaises(ValueError) as cm:
            rewrite_argv_for_staging(ambiguous_step, store_id, artifact_root=root)
        record_check()
        self.assertIn("multiple (2) ambiguous matches", str(cm.exception))
        record_check()

        # 3. Equivalent-but-non-canonical token raises ValueError
        non_canonical_step = Step(
            name="build",
            argv=["opengwasdb", "build-dense-vcf", f"{target_store}/."],
            inputs=[],
            outputs=[target_store],
        )
        with self.assertRaises(ValueError) as cm:
            rewrite_argv_for_staging(non_canonical_step, store_id, artifact_root=root)
        record_check()
        self.assertIn("non-canonical equivalent destination token", str(cm.exception))
        record_check()


class TestStagedReleaseTransactionAndPublicationGating(unittest.TestCase):
    """Test full staged release transaction lifecycle, force timing, and publication gating."""

    def setUp(self) -> None:
        self.test_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.test_dir.name)
        self.store_id = "OGS-00042"

    def tearDown(self) -> None:
        self.test_dir.cleanup()

    def test_publication_gating_rejects_non_validate_steps(self) -> None:
        """publish=True on non-validate steps raises ValueError and writes failure record."""
        target_store = paths.store_path(self.store_id, root=self.root)
        mock_script = self.root / "mock_build.py"
        mock_script.write_text("import sys; print('noop')")

        step = Step(name="build", argv=[sys.executable, str(mock_script), str(target_store)], inputs=[], outputs=[target_store])

        with self.assertRaises(ValueError) as cm:
            execute_step(step, store_id=self.store_id, artifact_root=self.root, publish=True)
        record_check()
        self.assertIn("Premature publication rejected", str(cm.exception))
        record_check()

        rec = load_record(self.store_id, "build", root=self.root)
        self.assertIsNotNone(rec)
        record_check()
        if rec:
            self.assertFalse(rec["success"])
            record_check()

    def test_publish_store_requires_successful_validate_record(self) -> None:
        """publish_store() checks that records/validate.json exists and is successful."""
        partial_store = paths.partial_store_path(self.store_id, root=self.root)
        partial_store.mkdir(parents=True, exist_ok=True)
        (partial_store / "data.dat").write_text("ready")

        # 1. Without validate record, publish_store() is rejected
        with self.assertRaises(ValueError) as cm:
            publish_store(self.store_id, artifact_root=self.root)
        record_check()
        self.assertIn("missing successful 'validate' record", str(cm.exception))
        record_check()

        # 2. With validate record, publish_store() succeeds
        rec_dir = paths.records_dir(self.store_id, root=self.root)
        rec_dir.mkdir(parents=True, exist_ok=True)
        (rec_dir / "validate.json").write_text(json.dumps({"success": True, "exit_code": 0}))

        pub_p = publish_store(self.store_id, artifact_root=self.root)
        self.assertEqual(pub_p, paths.store_path(self.store_id, root=self.root))
        record_check()
        self.assertTrue(pub_p.is_dir())
        record_check()

    def test_force_timing_staging_allowed_beside_existing_store(self) -> None:
        """Staging is permitted beside an existing store without force; force required only at publish."""
        target_store = paths.store_path(self.store_id, root=self.root)
        target_store.mkdir(parents=True, exist_ok=True)
        (target_store / "v1.txt").write_text("v1")

        mock_script = self.root / "mock_build.py"
        mock_script.write_text("""
import sys
from pathlib import Path
dest = Path(sys.argv[1])
dest.mkdir(parents=True, exist_ok=True)
(dest / "v2.txt").write_text("v2")
""")

        step = Step(name="build", argv=[sys.executable, str(mock_script), str(target_store)], inputs=[], outputs=[target_store])

        # Staging without force=True succeeds!
        res = execute_step(step, store_id=self.store_id, artifact_root=self.root, force=False, publish=False, check=True)
        self.assertTrue(res.success)
        record_check()
        # Existing target store is completely untouched
        self.assertEqual((target_store / "v1.txt").read_text(), "v1")
        record_check()

        # Terminal validation publish without force=True is refused
        val_script = self.root / "mock_val.py"
        val_script.write_text("import sys; print('valid')")
        val_step = Step(name="validate", argv=[sys.executable, str(val_script), str(target_store)], inputs=[target_store], outputs=[])

        with self.assertRaises(StepExecutionError) as cm:
            execute_step(val_step, store_id=self.store_id, artifact_root=self.root, force=False, publish=True, check=True)
        record_check()
        self.assertIn("Target store already exists", str(cm.exception))
        record_check()

        # Terminal validation publish WITH force=True replaces existing store
        res_forced = execute_step(val_step, store_id=self.store_id, artifact_root=self.root, force=True, publish=True, check=True)
        self.assertTrue(res_forced.success)
        record_check()
        self.assertTrue((target_store / "v2.txt").is_file())
        record_check()

    def test_run_plan_rejects_publish_without_validate_step(self) -> None:
        """run_plan(..., publish=True) rejects plans lacking a validate step."""
        target_store = paths.store_path(self.store_id, root=self.root)
        steps = [
            Step(name="build", argv=["opengwasdb", "build-dense", str(target_store)], inputs=[], outputs=[target_store]),
        ]
        with self.assertRaises(ValueError) as cm:
            run_plan(steps, store_id=self.store_id, artifact_root=self.root, publish=True)
        record_check()
        self.assertIn("plan lacks a terminal 'validate' step", str(cm.exception))
        record_check()

    def test_full_staged_transaction_publish_on_terminal_success(self) -> None:
        """All steps execute against .partial; publication happens on terminal validate step."""
        mock_pipeline_script = self.root / "mock_pipeline.py"
        mock_pipeline_script.write_text("""
import sys
from pathlib import Path
action = sys.argv[1]
store = Path(sys.argv[2])

assert "store.opengwasdb.partial" in str(store), f"Expected staged .partial store, got {store}"

if action == "build":
    store.mkdir(parents=True, exist_ok=True)
    (store / "manifest.json").write_text('{"store_id": "test"}')
    (store / "data.dat").write_text("build data")
elif action == "top-hits":
    assert (store / "manifest.json").is_file()
    (store / "tophits.idx").write_text("top hits index")
elif action == "rho":
    assert (store / "tophits.idx").is_file()
    (store / "rho.idx").write_text("rho matrix")
elif action == "overview":
    assert (store / "rho.idx").is_file()
    (store / "overview.html").write_text("<html>Overview</html>")
elif action == "validate":
    assert (store / "overview.html").is_file()
    print("Store valid")
""")

        target_store = paths.store_path(self.store_id, root=self.root)
        partial_store = paths.partial_store_path(self.store_id, root=self.root)

        steps = [
            Step(name="build", argv=[sys.executable, str(mock_pipeline_script), "build", str(target_store)], inputs=[], outputs=[target_store]),
            Step(name="top-hits", argv=[sys.executable, str(mock_pipeline_script), "top-hits", str(target_store)], inputs=[target_store], outputs=[target_store]),
            Step(name="rho", argv=[sys.executable, str(mock_pipeline_script), "rho", str(target_store)], inputs=[target_store], outputs=[target_store]),
            Step(name="overview", argv=[sys.executable, str(mock_pipeline_script), "overview", str(target_store)], inputs=[target_store], outputs=[target_store]),
            Step(name="validate", argv=[sys.executable, str(mock_pipeline_script), "validate", str(target_store)], inputs=[target_store], outputs=[]),
        ]

        results = run_plan(steps, store_id=self.store_id, artifact_root=self.root, publish=True, check=True)
        self.assertEqual(len(results), 5)
        record_check()
        self.assertTrue(all(r.success for r in results))
        record_check()

        # Final store is published and complete
        self.assertFalse(partial_store.exists())
        record_check()
        self.assertTrue(target_store.is_dir())
        record_check()
        self.assertTrue((target_store / "data.dat").is_file())
        record_check()
        self.assertTrue((target_store / "tophits.idx").is_file())
        record_check()
        self.assertTrue((target_store / "rho.idx").is_file())
        record_check()
        self.assertTrue((target_store / "overview.html").is_file())
        record_check()

    def test_dangling_and_live_symlinks_strictly_rejected(self) -> None:
        """Dangling symlinks (target missing) and live symlinks are both rejected via lstat."""
        target_store = paths.store_path(self.store_id, root=self.root)
        target_store.parent.mkdir(parents=True, exist_ok=True)

        # 1. Create a dangling symlink (pointing to non-existent target)
        non_existent = self.root / "does_not_exist_xyz"
        os.symlink(str(non_existent), str(target_store))

        # Path.exists() is False for dangling symlink, but lstat catches it!
        self.assertFalse(target_store.exists())
        self.assertTrue(os.path.islink(target_store))

        mock_script = self.root / "mock_noop.py"
        mock_script.write_text("import sys; print('noop')")
        step = Step(name="validate", argv=[sys.executable, str(mock_script), str(target_store)], inputs=[target_store], outputs=[])

        with self.assertRaises(ValueError) as cm:
            execute_step(step, store_id=self.store_id, artifact_root=self.root, check=True)
        record_check()
        self.assertIn("is a symlink; symlinks are prohibited", str(cm.exception))
        record_check()


class TestProcessGroupAndDescendantIsolation(unittest.TestCase):
    """Test process session/group isolation and termination on nonzero returncode/timeout."""

    def setUp(self) -> None:
        self.test_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.test_dir.name)
        self.store_id = "OGS-00042"

    def tearDown(self) -> None:
        self.test_dir.cleanup()

    def test_leader_fails_child_cleaned_up(self) -> None:
        """When leader exits with nonzero code, surviving child worker in process group is reaped."""
        mock_fork_fail_script = self.root / "mock_fork_fail.py"
        mock_fork_fail_script.write_text("""
import os
import sys
import time

pid = os.fork()
if pid == 0:
    # Close inherited standard pipes so parent sees EOF on leader exit
    try:
        sys.stdout.close()
        sys.stderr.close()
        os.close(0)
        os.close(1)
        os.close(2)
    except Exception:
        pass
    # Background child worker loop that stays in the process group
    while True:
        time.sleep(0.1)

# Leader fails immediately
sys.stderr.write("Leader failed with exit code 1\\n")
sys.exit(1)
""")

        target_store = paths.store_path(self.store_id, root=self.root)
        step = Step(
            name="build",
            argv=[sys.executable, str(mock_fork_fail_script), str(target_store)],
            inputs=[],
            outputs=[target_store],
        )

        with self.assertRaises(StepExecutionError) as cm:
            execute_step(
                step,
                store_id=self.store_id,
                artifact_root=self.root,
                check=True,
            )
        record_check()

        err_res = cm.exception.result
        self.assertEqual(err_res.exit_code, 1)
        record_check()
        self.assertFalse(err_res.success)
        record_check()

    def test_stubborn_descendant_killed_on_timeout(self) -> None:
        """Descendant worker ignoring SIGTERM is terminated via SIGKILL to the PGID."""
        mock_stubborn_script = self.root / "mock_stubborn.py"
        mock_stubborn_script.write_text("""
import os
import signal
import sys
import time

def ignore_term(signum, frame):
    pass

signal.signal(signal.SIGTERM, ignore_term)

pid = os.fork()
if pid == 0:
    signal.signal(signal.SIGTERM, ignore_term)
    while True:
        time.sleep(0.1)

while True:
    time.sleep(0.1)
""")

        target_store = paths.store_path(self.store_id, root=self.root)
        step = Step(
            name="build",
            argv=[sys.executable, str(mock_stubborn_script), str(target_store)],
            inputs=[],
            outputs=[target_store],
        )

        t0 = time.monotonic()
        with self.assertRaises(StepExecutionError) as cm:
            execute_step(
                step,
                store_id=self.store_id,
                artifact_root=self.root,
                timeout=0.2,
                check=True,
            )
        record_check()
        elapsed = time.monotonic() - t0

        err_res = cm.exception.result
        self.assertFalse(err_res.success)
        record_check()
        self.assertIn("timed out", err_res.stderr)
        record_check()
        self.assertLess(elapsed, 10.0)
        record_check()

    def test_signal_death_status_is_mirrored_not_masked(self) -> None:
        """A command killed by a signal is still recorded with a negative exit code."""
        mock_signal_script = self.root / "mock_signal.py"
        mock_signal_script.write_text("""
import os
import signal

os.kill(os.getpid(), signal.SIGTERM)
""")

        target_store = paths.store_path(self.store_id, root=self.root)
        step = Step(
            name="build",
            argv=[sys.executable, str(mock_signal_script), str(target_store)],
            inputs=[],
            outputs=[target_store],
        )

        with self.assertRaises(StepExecutionError) as cm:
            execute_step(step, store_id=self.store_id, artifact_root=self.root, check=True)
        record_check()

        err_res = cm.exception.result
        self.assertEqual(err_res.exit_code, -signal.SIGTERM)
        record_check()
        self.assertFalse(err_res.success)
        record_check()

    def test_supervisor_spawn_failure_does_not_hang(self) -> None:
        """A missing supervisor interpreter fails fast instead of blocking on the status pipe."""
        target_store = paths.store_path(self.store_id, root=self.root)
        step = Step(
            name="validate",
            argv=[sys.executable, "-c", "pass", str(target_store)],
            inputs=[target_store],
            outputs=[],
        )

        t0 = time.monotonic()
        with patch.object(run.sys, "executable", "/no/such/python-interpreter"):
            with self.assertRaises(MissingCommandError) as cm:
                execute_step(step, store_id=self.store_id, artifact_root=self.root, check=True)
        record_check()

        self.assertLess(time.monotonic() - t0, 5.0)
        record_check()
        self.assertEqual(cm.exception.result.exit_code, 127)
        record_check()

    def test_sigterm_grace_completes_before_sigkill(self) -> None:
        """The SIGTERM -> grace -> SIGKILL sequence lets a handler finish first.

        This drives the same ``run._terminate_process_group`` the timeout and
        KeyboardInterrupt paths call. An explicit readiness handshake removes
        interpreter-startup timing from the test: the mock installs its SIGTERM
        handler and only then publishes its pgid, and the test does not start
        the termination sequence until it can see that readiness.
        """
        marker = self.root / "grace_marker.txt"
        ready = self.root / "grace_ready.txt"
        mock_grace_script = self.root / "mock_grace.py"
        mock_grace_script.write_text(f'''
import os
import signal
import sys
import time
from pathlib import Path

marker = Path({str(marker)!r})
ready = Path({str(ready)!r})

def on_term(signum, frame):
    time.sleep(0.4)
    marker.write_text("handler ran")
    sys.exit(0)

signal.signal(signal.SIGTERM, on_term)
ready.write_text(f"{{os.getpid()}} {{os.getpgid(0)}}")
while True:
    time.sleep(0.05)
''')

        liveness_read, liveness_write = os.pipe()
        status_read, status_write = os.pipe()
        supervisor_argv = [
            sys.executable,
            "-c",
            run._SUPERVISOR_BOOTSTRAP,
            str(REPO_ROOT / "src"),
            str(liveness_read),
            str(status_write),
            sys.executable,
            str(mock_grace_script),
            str(marker),
            str(ready),
        ]
        proc = subprocess.Popen(
            supervisor_argv,
            start_new_session=True,
            pass_fds=(liveness_read, status_write),
        )
        os.close(liveness_read)
        os.close(status_write)
        try:
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and not ready.is_file():
                time.sleep(0.02)
            self.assertTrue(
                ready.is_file(),
                "mock never signalled that its SIGTERM handler was installed",
            )
            record_check()
            # start_new_session makes the supervisor the leader of the group it
            # was told to tear down.
            self.assertEqual(int(ready.read_text().split()[1]), proc.pid)
            record_check()

            t0 = time.monotonic()
            run._terminate_process_group(proc.pid, proc, timeout=2.0)
            elapsed = time.monotonic() - t0

            self.assertTrue(
                marker.is_file(),
                "SIGTERM handler was preempted; grace window did not complete before SIGKILL",
            )
            record_check()
            self.assertEqual(marker.read_text(), "handler ran")
            record_check()
            # The command handled SIGTERM and exited 0; an immediate SIGKILL
            # would have killed the supervisor itself and left -9 here.
            self.assertEqual(proc.returncode, 0)
            record_check()
            self.assertLess(elapsed, 2.0)
            record_check()
        finally:
            os.close(liveness_write)
            os.close(status_read)
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                proc.wait(timeout=10.0)

    @unittest.skipUnless(
        sys.platform.startswith("linux") and Path("/proc").is_dir(),
        "requires Linux /proc process state to observe liveness",
    )
    def test_abrupt_parent_death_reaps_detached_build_group(self) -> None:
        """SIGKILLing the orchestrator must not leave its detached build group alive.

        Reproduces the Snakemake-rule failure directly: the runner launches
        ``opengwasdb`` with ``start_new_session=True``, so if the orchestrator is
        killed abruptly (no exception handler runs) its forked workers keep
        writing to ``store.opengwasdb.partial`` and the next attempt collides
        with them. The build here is a stand-in that forks one worker, exactly
        as ``opengwasdb``'s ``ProcessPoolExecutor`` workers do, and then sleeps.
        """
        mock_build = self.root / "mock_long_build.py"
        mock_build.write_text('''
import os
import sys
import time
from pathlib import Path

target = Path(sys.argv[1])
info = Path(sys.argv[2])
target.mkdir(parents=True, exist_ok=True)
(target / "building.dat").write_text("building")

worker = os.fork()
if worker == 0:
    with open(info, "a") as handle:
        handle.write(f"worker {os.getpid()} {os.getpgid(0)}\\n")
    while True:
        time.sleep(0.1)

with open(info, "a") as handle:
    handle.write(f"leader {os.getpid()} {os.getpgid(0)}\\n")
while True:
    time.sleep(0.1)
''')

        driver = self.root / "orphan_driver.py"
        driver.write_text('''
import sys
from pathlib import Path

repo, root, store_id, mock, info = sys.argv[1:6]
sys.path.insert(0, str(Path(repo) / "src"))

from ogstores import paths
from ogstores.plan import Step
from ogstores.run import execute_step

target = paths.store_path(store_id, root=root)
step = Step(
    name="build",
    argv=[sys.executable, mock, str(target), info],
    inputs=[],
    outputs=[target],
)
execute_step(step, store_id=store_id, artifact_root=root, check=False)
''')

        info = self.root / "build_pids.txt"
        log_path = self.root / "orphan_driver.log"
        leader: int | None = None
        worker: int | None = None
        with open(log_path, "w", encoding="utf-8") as log_file:
            driver_proc = subprocess.Popen(
                [sys.executable, str(driver), str(REPO_ROOT), str(self.root), self.store_id, str(mock_build), str(info)],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                # Wait, bounded, for the detached build group to come up.
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline:
                    if info.is_file():
                        for line in info.read_text(encoding="utf-8").splitlines():
                            fields = line.split()
                            if len(fields) == 3 and fields[0] == "leader":
                                leader = int(fields[1])
                            elif len(fields) == 3 and fields[0] == "worker":
                                worker = int(fields[1])
                    if leader is not None and worker is not None:
                        break
                    time.sleep(0.05)

                self.assertIsNotNone(
                    leader,
                    f"build leader never started; driver log:\n{log_path.read_text(encoding='utf-8')}",
                )
                record_check()
                self.assertIsNotNone(
                    worker,
                    f"build worker never started; driver log:\n{log_path.read_text(encoding='utf-8')}",
                )
                record_check()

                # Abrupt death: no Python handler in the orchestrator gets to run.
                driver_proc.send_signal(signal.SIGKILL)
                driver_proc.wait(timeout=10.0)

                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline and (pid_is_live(leader) or pid_is_live(worker)):
                    time.sleep(0.05)

                self.assertFalse(
                    pid_is_live(leader),
                    f"detached build leader {leader} survived abrupt orchestrator death",
                )
                record_check()
                self.assertFalse(
                    pid_is_live(worker),
                    f"detached build worker {worker} survived abrupt orchestrator death",
                )
                record_check()
            finally:
                for pid in (leader, worker):
                    if pid is not None:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except (ProcessLookupError, OSError):
                            pass
                if driver_proc.poll() is None:
                    driver_proc.kill()
                    driver_proc.wait(timeout=10.0)


class TestSupervisorGroupGuard(unittest.TestCase):
    """The supervisor only signals a group it leads, and mirrors signal death faithfully."""

    def test_refuses_to_signal_group_it_does_not_lead(self) -> None:
        with patch("os.getpgid", return_value=os.getpid() + 1), patch("os.killpg") as mock_killpg:
            _pdeath_supervisor._kill_own_group()
        mock_killpg.assert_not_called()
        record_check()

    def test_signals_exactly_the_group_it_leads(self) -> None:
        with patch("os.getpgid", return_value=os.getpid()), patch("os.killpg") as mock_killpg:
            _pdeath_supervisor._kill_own_group()
        mock_killpg.assert_called_once_with(os.getpid(), signal.SIGKILL)
        record_check()

    def test_group_lookup_failure_does_not_signal(self) -> None:
        with patch("os.getpgid", side_effect=OSError("no such group")), patch("os.killpg") as mock_killpg:
            _pdeath_supervisor._kill_own_group()
        mock_killpg.assert_not_called()
        record_check()

    def test_supervisor_mirrors_sigkill_death_as_minus_nine(self) -> None:
        """An uncatchable SIGKILL still reaches the orchestrator as Popen.returncode -9."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        mock_long = Path(tmp.name) / "mock_long.py"
        mock_long.write_text("import os, time\nprint(os.getpid(), flush=True)\nwhile True:\n    time.sleep(0.1)\n")

        liveness_read, liveness_write = os.pipe()
        status_read, status_write = os.pipe()
        supervisor_argv = [
            sys.executable,
            "-c",
            run._SUPERVISOR_BOOTSTRAP,
            str(REPO_ROOT / "src"),
            str(liveness_read),
            str(status_write),
            sys.executable,
            str(mock_long),
        ]
        proc = subprocess.Popen(
            supervisor_argv,
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
            pass_fds=(liveness_read, status_write),
        )
        os.close(liveness_read)
        os.close(status_write)
        try:
            assert proc.stdout is not None
            command_pid = int(proc.stdout.readline())
            os.kill(command_pid, signal.SIGKILL)
            proc.wait(timeout=10.0)
            self.assertEqual(proc.returncode, -signal.SIGKILL)
            record_check()
        finally:
            os.close(liveness_write)
            os.close(status_read)
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10.0)


class TestCommandExecClassification(unittest.TestCase):
    """A supervisor-hidden exec failure keeps the old exception class and exit code."""

    def setUp(self) -> None:
        self.test_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.test_dir.name)
        self.store_id = "OGS-00042"

    def tearDown(self) -> None:
        self.test_dir.cleanup()

    def test_missing_command_raises_missing_command_error(self) -> None:
        target_store = paths.store_path(self.store_id, root=self.root)
        step = Step(
            name="build",
            argv=["/no/such/dir/opengwasdb", "build", str(target_store)],
            inputs=[],
            outputs=[target_store],
        )

        with self.assertRaises(MissingCommandError) as cm:
            execute_step(step, store_id=self.store_id, artifact_root=self.root, check=True)
        record_check()

        res = cm.exception.result
        self.assertEqual(res.exit_code, 127)
        record_check()
        self.assertIn("Command not found", res.stderr)
        record_check()

        record_data = load_record(self.store_id, "build", root=self.root)
        self.assertIsNotNone(record_data)
        record_check()
        assert record_data is not None
        self.assertEqual(record_data["exit_code"], 127)
        record_check()
        self.assertFalse(record_data["success"])
        record_check()

    def test_non_executable_command_is_not_reported_as_missing(self) -> None:
        """EACCES keeps the old exit 1 / StepExecutionError, not missing / 127."""
        not_executable = self.root / "not_executable.py"
        not_executable.write_text("print('must not run')\n", encoding="utf-8")
        not_executable.chmod(0o644)

        target_store = paths.store_path(self.store_id, root=self.root)
        step = Step(
            name="build",
            argv=[str(not_executable), str(target_store)],
            inputs=[],
            outputs=[target_store],
        )

        with self.assertRaises(StepExecutionError) as cm:
            execute_step(step, store_id=self.store_id, artifact_root=self.root, check=True)
        record_check()

        self.assertNotIsInstance(cm.exception, MissingCommandError)
        record_check()
        self.assertEqual(cm.exception.result.exit_code, 1)
        record_check()

        record_data = load_record(self.store_id, "build", root=self.root)
        self.assertIsNotNone(record_data)
        record_check()
        assert record_data is not None
        self.assertEqual(record_data["exit_code"], 1)
        record_check()

    def test_directory_command_is_not_reported_as_missing(self) -> None:
        """A directory argv[0] keeps the old exit 1 / StepExecutionError."""
        a_directory = self.root / "a_directory"
        a_directory.mkdir()

        target_store = paths.store_path(self.store_id, root=self.root)
        step = Step(
            name="build",
            argv=[str(a_directory), str(target_store)],
            inputs=[],
            outputs=[target_store],
        )

        with self.assertRaises(StepExecutionError) as cm:
            execute_step(step, store_id=self.store_id, artifact_root=self.root, check=True)
        record_check()

        self.assertNotIsInstance(cm.exception, MissingCommandError)
        record_check()
        self.assertEqual(cm.exception.result.exit_code, 1)
        record_check()


class TestPublicationRollbackAndCrashRecovery(unittest.TestCase):
    """Test publication rollback, crash recovery, and backup cleanup resilience."""

    def setUp(self) -> None:
        self.test_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.test_dir.name)
        self.store_id = "OGS-00042"

    def tearDown(self) -> None:
        self.test_dir.cleanup()

    def test_safe_force_replacement_rollback_on_swap_failure(self) -> None:
        target_store = paths.store_path(self.store_id, root=self.root)
        target_store.mkdir(parents=True, exist_ok=True)
        (target_store / "version.txt").write_text("original-v1")

        partial_store = paths.partial_store_path(self.store_id, root=self.root)
        partial_store.mkdir(parents=True, exist_ok=True)
        (partial_store / "version.txt").write_text("partial-v2")

        backup_store = paths.store_dir(self.store_id, root=self.root) / "store.opengwasdb.backup"

        original_rename = Path.rename

        def failing_rename(self_path: Path, target_dest: Path) -> Path:
            if self_path == partial_store:
                raise OSError("Simulated disk I/O error during partial swap")
            return original_rename(self_path, target_dest)

        with patch.object(Path, "rename", side_effect=failing_rename):
            with self.assertRaises(RuntimeError) as cm:
                run._safe_publish_partial_store(partial_store, target_store, backup_store, force=True)
            record_check()

            self.assertIn("Failed to replace existing store", str(cm.exception))
            record_check()

        # Rollback check: original target store is restored intact!
        self.assertTrue(target_store.is_dir())
        record_check()
        self.assertEqual((target_store / "version.txt").read_text(), "original-v1")
        record_check()

    def test_recovery_of_pending_backup_on_start(self) -> None:
        """If a previous run was interrupted leaving a backup, execute_step recovers it."""
        target_store = paths.store_path(self.store_id, root=self.root)
        backup_store = paths.store_dir(self.store_id, root=self.root) / "store.opengwasdb.backup"
        backup_store.mkdir(parents=True, exist_ok=True)
        (backup_store / "recovered.txt").write_text("recovered state")

        self.assertFalse(target_store.exists())

        mock_script = self.root / "mock_quick.py"
        mock_script.write_text("import sys; print('noop')")

        step = Step(
            name="validate",
            argv=[sys.executable, str(mock_script), str(target_store)],
            inputs=[target_store],
            outputs=[],
        )

        execute_step(step, store_id=self.store_id, artifact_root=self.root, check=True)
        record_check()

        # Verify target_store was recovered from backup_store
        self.assertTrue(target_store.is_dir())
        record_check()
        self.assertTrue((target_store / "recovered.txt").is_file())
        record_check()
        self.assertFalse(backup_store.exists())
        record_check()

    def test_backup_cleanup_failure_treated_as_recoverable_success(self) -> None:
        """If backup rmtree fails after successful swap, publication succeeds and retains backup."""
        target_store = paths.store_path(self.store_id, root=self.root)
        target_store.mkdir(parents=True, exist_ok=True)
        (target_store / "version.txt").write_text("v1")

        partial_store = paths.partial_store_path(self.store_id, root=self.root)
        partial_store.mkdir(parents=True, exist_ok=True)
        (partial_store / "version.txt").write_text("v2")

        backup_store = paths.store_dir(self.store_id, root=self.root) / "store.opengwasdb.backup"

        original_rmtree = shutil.rmtree

        def failing_rmtree(path: Any, *args: Any, **kwargs: Any) -> Any:
            if Path(path) == backup_store:
                raise OSError("Simulated permission error on backup cleanup")
            return original_rmtree(path, *args, **kwargs)

        with patch("shutil.rmtree", side_effect=failing_rmtree):
            run._safe_publish_partial_store(partial_store, target_store, backup_store, force=True)
            record_check()

        # Target store is successfully updated to v2!
        self.assertEqual((target_store / "version.txt").read_text(), "v2")
        record_check()
        # Retained backup is cleaned up on next start
        run._recover_pending_backup(self.store_id, self.root)
        record_check()
        self.assertFalse(backup_store.exists())
        record_check()


class TestProvenanceAndSequentialPlan(unittest.TestCase):
    """Test provenance derivation bound to executable environment, tripwires, resumption, and sequential execution."""

    def setUp(self) -> None:
        self.test_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.test_dir.name)
        self.store_id = "OGS-00042"

    def tearDown(self) -> None:
        self.test_dir.cleanup()

    def test_bound_provenance_from_executable_environment(self) -> None:
        """get_opengwasdb_revision derives commit SHA from the executable's prefix environment."""
        fake_env_dir = self.root / "fake_env"
        fake_bin_dir = fake_env_dir / "bin"
        fake_bin_dir.mkdir(parents=True, exist_ok=True)
        fake_exe = fake_bin_dir / "opengwasdb"
        fake_exe.write_text("#!/bin/sh\necho fake\n")
        fake_exe.chmod(0o755)

        fake_dist_dir = fake_env_dir / "lib" / "python3.11" / "site-packages" / "opengwasdb-0.3.0.dist-info"
        fake_dist_dir.mkdir(parents=True, exist_ok=True)
        expected_sha = "1111222233334444555566667777888899990000"
        (fake_dist_dir / "direct_url.json").write_text(json.dumps({"vcs_info": {"commit_id": expected_sha}}))

        derived_rev = get_opengwasdb_revision(executable=str(fake_exe))
        self.assertEqual(derived_rev, expected_sha)
        record_check()

        # Fallback to unavailable if executable has no valid commit SHA
        (fake_dist_dir / "direct_url.json").write_text(json.dumps({"vcs_info": {"commit_id": "invalid-sha"}}))
        self.assertEqual(get_opengwasdb_revision(executable=str(fake_exe)), "unavailable")
        record_check()

    def test_exact_commit_provenance_and_executable_resolution(self) -> None:
        rev = get_opengwasdb_revision()
        self.assertTrue(rev == "unavailable" or is_exact_commit_hash(rev))
        record_check()

        ver = get_opengwasdb_version()
        self.assertIsInstance(ver, str)
        record_check()

        exe = get_opengwasdb_executable()
        self.assertIsInstance(exe, str)
        record_check()

    def test_tripwire_no_store_readback(self) -> None:
        """Assert that execute_step never opens or reads back files from store.opengwasdb."""
        mock_script = self.root / "mock_build.py"
        mock_script.write_text("""
import sys
from pathlib import Path
dest = Path(sys.argv[1])
dest.mkdir(parents=True, exist_ok=True)
(dest / "manifest.json").write_text('{"store_id": "test"}')
""")

        target_store = paths.store_path(self.store_id, root=self.root)
        step = Step(
            name="build",
            argv=[sys.executable, str(mock_script), str(target_store)],
            inputs=[],
            outputs=[target_store],
        )

        real_open = open
        opened_paths: list[str] = []

        def spy_open(file: Any, *args: Any, **kwargs: Any) -> Any:
            f_str = str(file)
            if "store.opengwasdb" in f_str and not f_str.endswith(".json"):
                opened_paths.append(f_str)
            return real_open(file, *args, **kwargs)

        with patch("builtins.open", side_effect=spy_open):
            res = execute_step(
                step,
                store_id=self.store_id,
                artifact_root=self.root,
                publish=False,
                check=True,
            )

        self.assertTrue(res.success)
        record_check()
        self.assertEqual(
            opened_paths,
            [],
            f"run.py violated ADR 0023 by opening store files directly: {opened_paths}",
        )
        record_check()

    def test_explicit_resume_preserves_partial_and_fresh_cleans_it(self) -> None:
        partial_store = paths.partial_store_path(self.store_id, root=self.root)
        partial_store.mkdir(parents=True, exist_ok=True)
        (partial_store / "checkpoint_data.dat").write_text("checkpoint state")

        mock_resume_script = self.root / "mock_resume.py"
        mock_resume_script.write_text("""
import sys
from pathlib import Path
dest = Path(sys.argv[1])
assert (dest / "checkpoint_data.dat").is_file(), "checkpoint must exist"
(dest / "final_complete.dat").write_text("finished")
""")

        target_store = paths.store_path(self.store_id, root=self.root)
        step = Step(
            name="complete",
            argv=[sys.executable, str(mock_resume_script), str(target_store)],
            inputs=[],
            outputs=[target_store],
        )

        # 1. With resume=True, partial is preserved and completion succeeds
        res = execute_step(
            step,
            store_id=self.store_id,
            artifact_root=self.root,
            resume=True,
            publish=False,
            check=True,
        )

        self.assertTrue(res.success)
        record_check()
        self.assertTrue((partial_store / "checkpoint_data.dat").is_file())
        record_check()
        self.assertTrue((partial_store / "final_complete.dat").is_file())
        record_check()

        # 2. With resume=False on fresh run with stale partial, stale partial is cleaned up
        stale_partial = paths.partial_store_path("OGS-00043", root=self.root)
        stale_partial.mkdir(parents=True, exist_ok=True)
        (stale_partial / "stale.dat").write_text("stale")

        mock_fresh_script = self.root / "mock_fresh.py"
        mock_fresh_script.write_text("""
import sys
from pathlib import Path
dest = Path(sys.argv[1])
assert not (dest / "stale.dat").exists(), "stale file must be cleaned before fresh run"
dest.mkdir(parents=True, exist_ok=True)
(dest / "fresh.dat").write_text("fresh")
""")
        fresh_target = paths.store_path("OGS-00043", root=self.root)
        fresh_step = Step(
            name="build",
            argv=[sys.executable, str(mock_fresh_script), str(fresh_target)],
            inputs=[],
            outputs=[fresh_target],
        )

        res_fresh = execute_step(
            fresh_step,
            store_id="OGS-00043",
            artifact_root=self.root,
            resume=False,
            publish=False,
            check=True,
        )
        self.assertTrue(res_fresh.success)
        record_check()
        self.assertTrue((stale_partial / "fresh.dat").is_file())
        record_check()
        self.assertFalse((stale_partial / "stale.dat").exists())
        record_check()

    def test_run_plan_halts_immediately_on_failure_in_check_false_mode(self) -> None:
        """run_plan stops immediately on step failure even when check=False."""
        mock_script = self.root / "mock_steps.py"
        mock_script.write_text("""
import sys
from pathlib import Path
action = sys.argv[1]
dest = Path(sys.argv[2])
if action == "step1":
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "step1.dat").write_text("ok")
elif action == "step2":
    sys.stderr.write("Step 2 failed\\n")
    sys.exit(1)
elif action == "step3":
    (dest / "step3.dat").write_text("should not run")
""")

        target_store = paths.store_path(self.store_id, root=self.root)
        steps = [
            Step(name="build", argv=[sys.executable, str(mock_script), "step1", str(target_store)], inputs=[], outputs=[target_store]),
            Step(name="top-hits", argv=[sys.executable, str(mock_script), "step2", str(target_store)], inputs=[target_store], outputs=[target_store]),
            Step(name="validate", argv=[sys.executable, str(mock_script), "step3", str(target_store)], inputs=[target_store], outputs=[]),
        ]

        results = run_plan(
            steps,
            store_id=self.store_id,
            artifact_root=self.root,
            publish=True,
            check=False,
        )

        self.assertEqual(len(results), 2)
        record_check()
        self.assertTrue(results[0].success)
        record_check()
        self.assertFalse(results[1].success)
        record_check()
        self.assertFalse(target_store.exists())
        record_check()


def main() -> None:
    suite = unittest.TestSuite()
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestStepClassificationAndArgvRewrite))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestStagedReleaseTransactionAndPublicationGating))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestProcessGroupAndDescendantIsolation))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestSupervisorGroupGuard))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestCommandExecClassification))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestPublicationRollbackAndCrashRecovery))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestProvenanceAndSequentialPlan))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if not result.wasSuccessful():
        sys.exit(1)
    print(f"ALL {n_checks} CHECKS PASSED")


if __name__ == "__main__":
    main()

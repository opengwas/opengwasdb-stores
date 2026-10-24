#!/usr/bin/env python3
"""The stores lane of the #326 SE re-encode command (`ogstores.se_reencode`).

Covers the orchestration the issue assigns to this repository, with fixture
artifact roots and real `flock` contention:

* dry-run is strictly read-only and reports the plan plus the core revision;
* a successful apply writes `records/se-reencode.json` with the exact core
  revision, start/end UTC, per-array bytes/saving/equality/chunks, and the
  Dense or nested Hybrid group path;
* a second run is a no-op and does not rewrite a successful record;
* a *held* flock is refused with its holder, while a stale lock-file text
  remnant is allowed;
* `publication.json`, `store.opengwasdb.partial` and completion markers are
  refused before any write;
* a failed apply leaves no success record;
* the worker cap at 16 is enforced;
* an unregistered Store is refused.

The core primitive itself (`plan_se_reencode` / `reencode_se_codecs`) is
covered by the opengwasdb repository; here it is patched so these tests
exercise this repository's coordination and record contract, not a real
format-0.2.0 Store.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]

from ogstores import release, runtime, se_reencode  # noqa: E402

STORE_ID = "OGS-00090"

n_checks = 0


def record_check() -> None:
    global n_checks
    n_checks += 1


def plan(
    *,
    group: str = "data.zarr",
    array: str = "se",
    dtype: str = "int8",
    current_typesize: int = 4,
    expected_typesize: int = 1,
    before_bytes: int = 1000,
    estimated_after_bytes: int = 600,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        group=group,
        array=array,
        dtype=dtype,
        current_typesize=current_typesize,
        expected_typesize=expected_typesize,
        before_bytes=before_bytes,
        estimated_after_bytes=estimated_after_bytes,
        estimated_saving_bytes=before_bytes - estimated_after_bytes,
    )


def result(
    *,
    group: str = "data.zarr",
    array: str = "se",
    before_bytes: int = 1000,
    after_bytes: int = 600,
    equality: bool = True,
    chunks: int = 7,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        group=group,
        array=array,
        before_bytes=before_bytes,
        after_bytes=after_bytes,
        equality=equality,
        chunks=chunks,
    )


class SeReencodeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ogstores_se_reencode_"))
        self.registry = self.tmp / "stores"
        self.artifacts = self.tmp / "artifacts"
        self.registry.mkdir()
        self.artifacts.mkdir()
        bundle_root = self.registry / STORE_ID
        bundle_root.mkdir()
        (bundle_root / "release.yaml").write_text(
            f"store_id: {STORE_ID}\nstatus: accepted\n", encoding="utf-8"
        )
        store_dir = self.artifacts / STORE_ID / "store.opengwasdb"
        store_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = se_reencode.main(list(argv))
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue(), err.getvalue()

    def record_path(self) -> Path:
        return self.artifacts / STORE_ID / "records" / "se-reencode.json"

    def base_argv(self) -> list[str]:
        return [
            STORE_ID,
            "--artifact-root",
            str(self.artifacts),
            "--registry-root",
            str(self.registry),
        ]

    def snapshot(self, root: Path) -> set[str]:
        return {str(p.relative_to(root)) for p in root.rglob("*")}

    # --- dry run ----------------------------------------------------------

    def test_dry_run_is_read_only_and_reports_the_plan(self) -> None:
        """A dry run writes nothing and reports target array, typesizes, bytes and the revision."""
        before = self.snapshot(self.artifacts)
        with mock.patch.object(
            se_reencode, "_core_plan", return_value=[plan()]
        ) as core_plan:
            code, out, err = self.run_main(*(self.base_argv() + ["--dry-run"]))
        record_check()
        self.assertEqual(code, 0, out + err)
        self.assertIn("se-reencode dry run for OGS-00090", out)
        self.assertIn("data.zarr/se", out)
        self.assertIn("typesize 4 -> 1", out)
        self.assertIn("before=1000 bytes", out)
        self.assertIn("estimated_after=600 bytes", out)
        self.assertIn("estimated_saving=400 bytes", out)
        self.assertIn(f"core revision: {runtime.PINNED_OPENGWASDB_REVISION}", out)
        self.assertEqual(self.snapshot(self.artifacts), before, "dry run wrote files")
        self.assertEqual(core_plan.call_count, 1)

    def test_dry_run_of_an_already_correct_store_reports_nothing_to_do(self) -> None:
        with mock.patch.object(se_reencode, "_core_plan", return_value=[]):
            code, out, err = self.run_main(*(self.base_argv() + ["--dry-run"]))
        record_check()
        self.assertEqual(code, 0, out + err)
        self.assertIn("nothing to re-encode; the Store is already correct", out)

    def test_dry_run_refuses_a_held_lock_without_writing(self) -> None:
        """A dry run is read-only, so it reports a live lock instead of taking it."""
        live = release.ReleaseLocks(self.artifacts)
        live.acquire(STORE_ID)
        self.addCleanup(live.release_all)
        before = self.snapshot(self.artifacts)
        code, out, err = self.run_main(*(self.base_argv() + ["--dry-run"]))
        record_check()
        self.assertEqual(code, 1, out + err)
        self.assertIn("in progress", err)
        self.assertEqual(self.snapshot(self.artifacts), before, "dry run wrote files")

    # --- successful apply and record -------------------------------------

    def test_successful_apply_writes_the_exact_core_revision_record(self) -> None:
        """The record holds the exact core revision, times and the per-array facts."""
        with mock.patch.object(se_reencode, "_core_apply", return_value=[result()]), \
             mock.patch.object(se_reencode, "_core_plan", return_value=[]):
            code, out, err = self.run_main(*self.base_argv())
        record_check()
        self.assertEqual(code, 0, out + err)
        self.assertIn("re-encoded 1 array(s) of OGS-00090", out)
        record = json.loads(self.record_path().read_text(encoding="utf-8"))
        self.assertEqual(record["store_id"], STORE_ID)
        self.assertEqual(record["step"], "se-reencode")
        self.assertEqual(record["opengwasdb_revision"], runtime.PINNED_OPENGWASDB_REVISION)
        self.assertEqual(len(record["opengwasdb_revision"]), 40)
        self.assertEqual(record["opengwasdb_version"], "0.3.0")
        self.assertRegex(record["start_time"], r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}.*Z")
        self.assertRegex(record["end_time"], r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}.*Z")
        self.assertIs(record["complete"], True)
        self.assertEqual(record["arrays"], [
            {
                "group": "data.zarr",
                "array": "se",
                "before_bytes": 1000,
                "after_bytes": 600,
                "saved_bytes": 400,
                "decoded_equality": True,
                "chunks": 7,
            }
        ])

    def test_record_reports_a_nested_hybrid_group_path(self) -> None:
        """The record keeps the core's `dense/data.zarr` label for a Hybrid Store."""
        hybrid = result(group="dense/data.zarr", array="se", before_bytes=5, after_bytes=3)
        with mock.patch.object(se_reencode, "_core_apply", return_value=[hybrid]), \
             mock.patch.object(se_reencode, "_core_plan", return_value=[]):
            code, out, err = self.run_main(*self.base_argv())
        record_check()
        self.assertEqual(code, 0, out + err)
        record = json.loads(self.record_path().read_text(encoding="utf-8"))
        self.assertEqual(record["arrays"][0]["group"], "dense/data.zarr")
        self.assertEqual(record["arrays"][0]["saved_bytes"], 2)

    def test_a_partial_apply_is_recorded_truthfully(self) -> None:
        """A run that leaves an array still wrong records `complete: false`."""
        with mock.patch.object(se_reencode, "_core_apply", return_value=[result()]), \
             mock.patch.object(se_reencode, "_core_plan", return_value=[plan(array="se_exception_index")]):
            code, out, err = self.run_main(*self.base_argv())
        record_check()
        self.assertEqual(code, 0, out + err)
        self.assertIn("partial", out)
        record = json.loads(self.record_path().read_text(encoding="utf-8"))
        self.assertIs(record["complete"], False)

    # --- idempotency ------------------------------------------------------

    def test_second_invocation_is_a_noop_and_does_not_rewrite_the_record(self) -> None:
        """An already-correct Store is reported as such and its record is left alone."""
        with mock.patch.object(se_reencode, "_core_apply", return_value=[result()]), \
             mock.patch.object(se_reencode, "_core_plan", return_value=[]):
            code, out, err = self.run_main(*self.base_argv())
        self.assertEqual(code, 0, out + err)
        before = self.record_path().read_text(encoding="utf-8")

        with mock.patch.object(se_reencode, "_core_apply", return_value=[]) as core_apply:
            code, out, err = self.run_main(*self.base_argv())
        record_check()
        self.assertEqual(code, 0, out + err)
        self.assertIn("already correct", out)
        self.assertEqual(core_apply.call_count, 1)
        self.assertEqual(self.record_path().read_text(encoding="utf-8"), before)

    # --- locks ------------------------------------------------------------

    def test_held_lock_refusal_reports_the_holder(self) -> None:
        """A live run's flock is refused with its holder, before the core is called."""
        live = release.ReleaseLocks(self.artifacts)
        live.acquire(STORE_ID)
        self.addCleanup(live.release_all)
        with mock.patch.object(se_reencode, "_core_apply") as core_apply:
            code, out, err = self.run_main(*self.base_argv())
        record_check()
        self.assertEqual(code, 1, out + err)
        self.assertIn("in progress", err)
        self.assertIn(STORE_ID, err)
        self.assertIn(f"pid {os.getpid()}", err)
        core_apply.assert_not_called()
        self.assertFalse(self.record_path().exists())

    def test_stale_lock_file_text_is_allowed(self) -> None:
        """A lock file whose flock is free is a text remnant, not a live run."""
        lock = self.artifacts / ".release-locks" / f"{STORE_ID}.lock"
        lock.parent.mkdir(parents=True)
        lock.write_text("pid 99999999, started 20261023T230247Z\n", encoding="utf-8")
        with mock.patch.object(se_reencode, "_core_apply", return_value=[result()]), \
             mock.patch.object(se_reencode, "_core_plan", return_value=[]):
            code, out, err = self.run_main(*self.base_argv())
        record_check()
        self.assertEqual(code, 0, out + err)
        self.assertTrue(self.record_path().exists())

    # --- pre-write refusals ----------------------------------------------

    def test_publication_marker_is_refused_before_any_write(self) -> None:
        (self.artifacts / STORE_ID / "publication.json").write_text("{}", encoding="utf-8")
        before = self.snapshot(self.artifacts)
        with mock.patch.object(se_reencode, "_core_apply") as core_apply:
            code, out, err = self.run_main(*self.base_argv())
        record_check()
        self.assertEqual(code, 1, out + err)
        self.assertIn("publication.json", err)
        core_apply.assert_not_called()
        self.assertFalse(self.record_path().exists())
        self.assertEqual(self.snapshot(self.artifacts), before)

    def test_partial_store_is_refused_before_any_write(self) -> None:
        (self.artifacts / STORE_ID / "store.opengwasdb.partial").mkdir()
        with mock.patch.object(se_reencode, "_core_apply") as core_apply:
            code, out, err = self.run_main(*self.base_argv())
        record_check()
        self.assertEqual(code, 1, out + err)
        self.assertIn("store.opengwasdb.partial", err)
        core_apply.assert_not_called()

    def test_completion_markers_are_refused_before_any_write(self) -> None:
        markers = [
            self.artifacts / STORE_ID / "work" / "complete.started.json",
            self.artifacts / STORE_ID / ".store.opengwasdb.partial.checkpoint",
        ]
        for marker in markers:
            with self.subTest(marker=marker.name):
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text("{}", encoding="utf-8")
                with mock.patch.object(se_reencode, "_core_apply") as core_apply:
                    code, out, err = self.run_main(*self.base_argv())
                record_check()
                self.assertEqual(code, 1, out + err)
                self.assertIn(marker.name, err)
                core_apply.assert_not_called()
                self.assertFalse(self.record_path().exists())
                marker.unlink()

    # --- failure and validation ------------------------------------------

    def test_failed_apply_leaves_no_success_record(self) -> None:
        with mock.patch.object(se_reencode, "_core_apply", side_effect=RuntimeError("boom")):
            code, out, err = self.run_main(*self.base_argv())
        record_check()
        self.assertEqual(code, 1, out + err)
        self.assertIn("no success record was written", err)
        self.assertFalse(self.record_path().exists())

    def test_worker_cap_is_enforced_at_sixteen(self) -> None:
        for workers in ("0", "17", "64"):
            with self.subTest(workers=workers):
                code, out, err = self.run_main(*(self.base_argv() + ["--workers", workers]))
                record_check()
                self.assertEqual(code, 2, out + err)
                self.assertIn("between 1 and 16", err)
        with mock.patch.object(se_reencode, "_core_apply", return_value=[result()]), \
             mock.patch.object(se_reencode, "_core_plan", return_value=[]):
            code, out, err = self.run_main(*(self.base_argv() + ["--workers", "16"]))
        record_check()
        self.assertEqual(code, 0, out + err)

    def test_unregistered_store_is_refused(self) -> None:
        code, out, err = self.run_main("OGS-00091", "--artifact-root", str(self.artifacts), "--registry-root", str(self.registry))
        record_check()
        self.assertEqual(code, 1, out + err)
        self.assertIn("not a registered Release Bundle", err)

    def test_missing_published_store_is_refused(self) -> None:
        shutil.rmtree(self.artifacts / STORE_ID / "store.opengwasdb")
        code, out, err = self.run_main(*self.base_argv())
        record_check()
        self.assertEqual(code, 1, out + err)
        self.assertIn("no published Store", err)


def main() -> None:
    suite = unittest.TestSuite()
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(SeReencodeTestCase))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if not result.wasSuccessful():
        sys.exit(1)
    print(f"ALL {n_checks} CHECKS PASSED")


if __name__ == "__main__":
    main()

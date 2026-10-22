#!/usr/bin/env python3
"""`tools/relocate_store.py` on synthetic releases: its refusals, and an interrupted move resumed (#331).

The whole-stack seams, a fixture release moved and released again at its new
root, are the workflow suite's (`TestWorkflowEndToEndRelocation`). Here each
release is six records and an empty Store directory, written as `run` writes
them, so no Store is built. `opengwasdb` is a stub on PATH that reports the
Store id a test chooses.

Run from the repository root:
    python3 tests/relocate-store/test_relocate_store.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from ogstores import paths, release, run

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
RELOCATE_STORE: Path = REPO_ROOT / "tools" / "relocate_store.py"
STORE_ID = "OGS-00099"
STEPS: tuple[str, ...] = ("build", "indexed-subset-hm3", "overview", "register", "top-hits", "validate")

# The tool's test-only fault: it raises before the replacement this names.
FAIL_AT_ENV = "OGSTORES_TEST_RELOCATION_FAIL_AT"
TIMES: tuple[str, ...] = ("started_at", "completed_at", "resumed_at")


def write_release(root: Path, store_id: str = STORE_ID) -> Path:
    """A published release under `root`: an empty Store, a build manifest, and one record per step."""
    store = paths.store_path(store_id, root=root)
    staged = paths.partial_store_path(store_id, root=root)
    manifest = paths.build_manifest_path(store_id, root=root)
    store.mkdir(parents=True)
    manifest.parent.mkdir()
    manifest.write_text("analysis_id\na\n", encoding="utf-8")
    paths.records_dir(store_id, root=root).mkdir()
    for n, step in enumerate(STEPS):
        registering = step == "register"
        record = {
            "step": step,
            "store_id": store_id,
            "exit_code": 0,
            "success": True,
            "argv": [] if registering else ["opengwasdb", step, str(staged)],
            "planned_argv": [] if registering else ["opengwasdb", step, str(store)],
            "inputs": [str(paths.record_path(store_id, s, root=root)) for s in STEPS if s != step][:2]
            if registering else [str(manifest if step == "build" else store)],
            "outputs": ["/registry/stores/OGS-00099/validation.yaml"] if registering else [str(store)],
            "stdout": "" if registering else f"wrote {staged}\n",
            "stderr": "",
            "record_path": str(paths.record_path(store_id, step, root=root)),
            "published_store": str(store) if registering else None,
        }
        record_p = paths.record_path(store_id, step, root=root)
        record_p.write_text(run.record_text(record), encoding="utf-8")
        os.utime(record_p, ns=(1_800_000_000_000_000_000 + n, 1_800_000_000_000_000_000 + n * 10**9))
    return paths.store_dir(store_id, root=root)


def records_of(release_dir: Path) -> dict[str, tuple[int, bytes]]:
    return {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in sorted((release_dir / "records").glob("*.json"))}


def tree_state(root: Path) -> dict[str, tuple[int, bytes] | None]:
    """Every path under `root`: a file's mtime and bytes, None for a directory."""
    return {
        str(p.relative_to(root)): None if p.is_dir() else (p.lstat().st_mtime_ns, p.read_bytes())
        for p in sorted(root.rglob("*"))
    }


def receipt_of(release_dir: Path) -> dict[str, Any]:
    receipts = sorted((release_dir / "relocations").glob("*.json"))
    assert len(receipts) == 1, receipts
    return json.loads(receipts[0].read_text(encoding="utf-8"))


class RelocateStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)
        self.old_root, self.new_root = self.td / "built", self.td / "published"
        self.new_root.mkdir()
        self.old_dir = write_release(self.old_root)
        self.new_dir = paths.store_dir(STORE_ID, root=self.new_root)
        self.stub_bin = self.td / "bin"
        self.stub_bin.mkdir()
        self.reported_id(STORE_ID)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def reported_id(self, store_id: str) -> None:
        """Make the `opengwasdb` on the tool's PATH report `store_id` for any Store."""
        stub = self.stub_bin / "opengwasdb"
        stub.write_text(f"#!/bin/sh\necho '{json.dumps({'store_id': store_id})}'\n", encoding="utf-8")
        stub.chmod(0o755)

    def relocate(self, *words: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(RELOCATE_STORE), STORE_ID, "--from", str(self.old_root), "--to", str(self.new_root), *words],
            cwd=REPO_ROOT, capture_output=True, text=True,
            env={**os.environ, "PATH": f"{self.stub_bin}{os.pathsep}{os.environ['PATH']}", **(env or {})},
        )

    def assert_refused(self, *named: str) -> str:
        """The tool refuses, naming each of `named`, and nothing under the test's directory changes."""
        before = tree_state(self.td)
        refused = self.relocate()
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        for name in named:
            self.assertIn(name, refused.stderr)
        self.assertIn("Nothing was moved", refused.stderr)
        self.assertEqual(tree_state(self.td), before)
        return refused.stderr


class RefusalTest(RelocateStoreTestCase):
    """Each refusal the whole-stack tests do not reach, with nothing moved or written."""

    def test_a_run_holding_the_release_lock_at_either_root(self) -> None:
        for root in (self.old_root, self.new_root):
            with self.subTest(root=str(root)):
                locks = release.ReleaseLocks(root)
                locks.acquire(STORE_ID)
                try:
                    self.assert_refused(str(paths.release_lock_path(STORE_ID, root=root)), f"pid {os.getpid()}")
                finally:
                    locks.release_all()

    def test_a_store_that_reports_another_id(self) -> None:
        self.reported_id("OGS-00016")
        self.assert_refused("store_id 'OGS-00016', not OGS-00099", str(paths.store_path(STORE_ID, root=self.old_root)))

    def test_a_record_that_would_not_be_written_back_byte_for_byte(self) -> None:
        record_p = paths.record_path(STORE_ID, "overview", root=self.old_root)
        record_p.write_text(json.dumps(json.loads(record_p.read_text(encoding="utf-8"))), encoding="utf-8")
        self.assert_refused("records/overview.json would not be written back byte for byte", "+++")

    def test_the_old_root_in_a_field_the_tool_does_not_rewrite(self) -> None:
        record_p = paths.record_path(STORE_ID, "register", root=self.old_root)
        record = json.loads(record_p.read_text(encoding="utf-8"))
        record["replaced_archive"] = str(paths.replaced_dir(STORE_ID, "20261022T000000Z", root=self.old_root))
        record_p.write_text(run.record_text(record), encoding="utf-8")
        self.assert_refused(f"records/register.json: replaced_archive still names {self.old_dir}")


class InterruptedRelocationTest(RelocateStoreTestCase):
    """A failure after the move leaves a started receipt, and `--resume` finishes what an uninterrupted run does."""

    def test_a_failed_replacement_is_finished_by_resume_as_an_uninterrupted_run(self) -> None:
        pristine = self.td / "pristine"
        shutil.copytree(self.old_dir, pristine)  # keeps mtimes
        failed = self.relocate(env={FAIL_AT_ENV: "4"})

        self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
        receipt_p = next((self.new_dir / "relocations").glob("*.json"))
        self.assertIn(str(receipt_p), failed.stderr)
        self.assertIn("--resume", failed.stderr)
        self.assertFalse(self.old_dir.exists())
        started = json.loads(receipt_p.read_text(encoding="utf-8"))
        self.assertEqual(started["status"], "started")
        records = {entry["path"]: entry for entry in started["records"]}
        halfway = {
            name: hashlib.sha256(data).hexdigest() for name, (_, data) in records_of(self.new_dir).items()
        }
        self.assertEqual(
            [name for name in halfway if halfway[name] == records[f"records/{name}"]["sha256_after"]],
            ["build.json", "indexed-subset-hm3.json", "overview.json"],
        )
        self.assertEqual(
            [name for name in halfway if halfway[name] == records[f"records/{name}"]["sha256_before"]],
            ["register.json", "top-hits.json", "validate.json"],
        )
        self.assertIn("--resume", self.assert_refused(str(receipt_p)))

        resumed = self.relocate("--resume")

        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        interrupted_records, interrupted_receipt = records_of(self.new_dir), receipt_of(self.new_dir)
        self.assertEqual(interrupted_receipt["status"], "complete")

        # The same release moved without interruption, from the same root to the same root.
        shutil.rmtree(self.new_dir)
        shutil.copytree(pristine, self.old_dir)
        uninterrupted = self.relocate()
        self.assertEqual(uninterrupted.returncode, 0, uninterrupted.stdout + uninterrupted.stderr)

        self.assertEqual(interrupted_records, records_of(self.new_dir))
        untimed = lambda receipt: {key: value for key, value in receipt.items() if key not in TIMES}  # noqa: E731
        self.assertEqual(untimed(interrupted_receipt), untimed(receipt_of(self.new_dir)))
        self.assertEqual(len(interrupted_receipt["resumed_at"]), 1)


if __name__ == "__main__":
    unittest.main()

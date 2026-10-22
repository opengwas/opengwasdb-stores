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
import stat
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ogstores import paths, release, run

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
RELOCATE_STORE: Path = REPO_ROOT / "tools" / "relocate_store.py"
STORE_ID = "OGS-00099"
STEPS: tuple[str, ...] = ("build", "indexed-subset-hm3", "overview", "register", "top-hits", "validate")
# Records with a mode other than the default, which a move must keep: `build` is
# rewritten before the test fault, `top-hits` only when the move is resumed.
PRIVATE_RECORDS: frozenset[str] = frozenset({"build", "top-hits"})

# The tool's test-only fault: it raises before the replacement this numbers,
# while it first writes the receipt (`receipt`), before it renames (`rename`)
# or before it marks the receipt complete (`complete`).
FAIL_AT_ENV = "OGSTORES_TEST_RELOCATION_FAIL_AT"
REFUSED, UNFINISHED = 2, 1


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
        record_p.chmod(0o600 if step in PRIVATE_RECORDS else 0o644)
        os.utime(record_p, ns=(1_800_000_000_000_000_000 + n, 1_800_000_000_000_000_000 + n * 10**9))
    return paths.store_dir(store_id, root=root)


def records_of(store_dir: Path) -> dict[str, tuple[int, int, bytes]]:
    """Each record's mtime, mode and bytes."""
    return {
        p.name: (p.stat().st_mtime_ns, stat.S_IMODE(p.stat().st_mode), p.read_bytes())
        for p in sorted((store_dir / "records").glob("*.json"))
    }


def tree_state(root: Path) -> dict[str, tuple[int, int, bytes] | None]:
    """Every path under `root`: a file's mtime, mode and bytes, None for a directory.

    The roots' `.release-locks/` are left out: taking a release's lock makes
    its lock file, which stays, as every `pixi run release` leaves it.
    """
    return {
        str(p.relative_to(root)): None if p.is_dir() else (p.lstat().st_mtime_ns, p.lstat().st_mode, p.read_bytes())
        for p in sorted(root.rglob("*"))
        if paths.RELEASE_LOCKS_DIRNAME not in p.relative_to(root).parts
    }


def receipt_of(store_dir: Path) -> dict[str, Any]:
    receipts = sorted((store_dir / "relocations").glob("*.json"))
    assert len(receipts) == 1, receipts
    return json.loads(receipts[0].read_text(encoding="utf-8"))


def untimed(receipt: dict[str, Any]) -> dict[str, Any]:
    """A receipt without the times that differ between two runs of the same move."""
    return {key: value for key, value in receipt.items() if key not in ("started_at", "completed_at", "resumed_at")}


class RelocateStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)
        self.stub_bin = self.td / "bin"
        self.stub_bin.mkdir()
        self.reported_id(STORE_ID)
        self.old_root, self.new_root = self.release_in(self.td / "release")
        self.old_dir = paths.store_dir(STORE_ID, root=self.old_root)
        self.new_dir = paths.store_dir(STORE_ID, root=self.new_root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def release_in(self, into: Path) -> tuple[Path, Path]:
        """A release built under `into`, and an empty root beside it to move it to."""
        old_root, new_root = into / "built", into / "published"
        write_release(old_root)
        new_root.mkdir()
        return old_root, new_root

    def reported_id(self, store_id: str) -> None:
        """Make the `opengwasdb` on the tool's PATH report `store_id` for any Store."""
        stub = self.stub_bin / "opengwasdb"
        stub.write_text(f"#!/bin/sh\necho '{json.dumps({'store_id': store_id})}'\n", encoding="utf-8")
        stub.chmod(0o755)

    def relocate(
        self, *words: str, roots: tuple[Path, Path] | None = None, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        old_root, new_root = roots or (self.old_root, self.new_root)
        return subprocess.run(
            [sys.executable, str(RELOCATE_STORE), STORE_ID, "--from", str(old_root), "--to", str(new_root), *words],
            cwd=REPO_ROOT, capture_output=True, text=True,
            env={**os.environ, "PATH": f"{self.stub_bin}{os.pathsep}{os.environ['PATH']}", **(env or {})},
        )

    def assert_refused(
        self, *named: str, words: tuple[str, ...] = (), roots: tuple[Path, Path] | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        """The tool refuses, naming each of `named`, and nothing under the test's directory changes."""
        before = tree_state(self.td)
        refused = self.relocate(*words, roots=roots, env=env)
        self.assertEqual(refused.returncode, REFUSED, refused.stdout + refused.stderr)
        for name in named:
            self.assertIn(name, refused.stderr)
        self.assertEqual(tree_state(self.td), before)
        return refused.stderr

    def interrupt(self, roots: tuple[Path, Path] | None = None) -> Path:
        """Fail a move before its fourth record replacement; the receipt it leaves."""
        old_root, new_root = roots or (self.old_root, self.new_root)
        failed = self.relocate(roots=(old_root, new_root), env={FAIL_AT_ENV: "4"})
        self.assertEqual(failed.returncode, UNFINISHED, failed.stdout + failed.stderr)
        (receipt_p,) = (paths.store_dir(STORE_ID, root=new_root) / "relocations").glob("*.json")
        return receipt_p


class RefusalTest(RelocateStoreTestCase):
    """Each refusal the whole-stack tests do not reach, with nothing moved or written."""

    def assert_nothing_moved(self, *named: str, env: dict[str, str] | None = None) -> None:
        self.assertIn("Nothing was moved or written", self.assert_refused(*named, env=env))

    def test_a_run_holding_the_release_lock_at_either_root(self) -> None:
        for root in (self.old_root, self.new_root):
            with self.subTest(root=str(root)):
                locks = release.ReleaseLocks(root)
                locks.acquire(STORE_ID)
                try:
                    self.assert_nothing_moved(str(paths.release_lock_path(STORE_ID, root=root)), f"pid {os.getpid()}")
                finally:
                    locks.release_all()

    def test_a_store_that_reports_another_id(self) -> None:
        self.reported_id("OGS-00016")
        self.assert_nothing_moved("store_id 'OGS-00016', not OGS-00099", str(paths.store_path(STORE_ID, root=self.old_root)))

    def test_a_record_that_would_not_be_written_back_byte_for_byte(self) -> None:
        record_p = paths.record_path(STORE_ID, "overview", root=self.old_root)
        record_p.write_text(json.dumps(json.loads(record_p.read_text(encoding="utf-8"))), encoding="utf-8")
        self.assert_nothing_moved("records/overview.json would not be written back byte for byte", "+++")

    def test_the_old_root_in_a_field_the_tool_does_not_rewrite(self) -> None:
        record_p = paths.record_path(STORE_ID, "register", root=self.old_root)
        record = json.loads(record_p.read_text(encoding="utf-8"))
        record["replaced_archive"] = str(paths.replaced_dir(STORE_ID, "20261022T000000Z", root=self.old_root))
        record_p.write_text(run.record_text(record), encoding="utf-8")
        self.assert_nothing_moved(f"records/register.json: replaced_archive still names {self.old_dir}")

    def test_a_test_fault_that_names_no_replacement(self) -> None:
        self.assert_nothing_moved(f"{FAIL_AT_ENV}='fourth'", env={FAIL_AT_ENV: "fourth"})

    def test_a_receipt_that_cannot_be_written_is_removed_with_its_directory(self) -> None:
        stderr = self.assert_refused("could not be written", str(self.old_dir / "relocations"), env={FAIL_AT_ENV: "receipt"})
        self.assertIn("Nothing was moved or written", stderr)
        self.assertNotIn("--resume", stderr)

    def test_a_receipt_that_cannot_be_written_leaves_an_earlier_receipt_alone(self) -> None:
        """A move straight back, within the second, whose receipt fails: the first move's receipt stays."""
        self.assertEqual(self.relocate().returncode, 0)
        (earlier,) = (self.new_dir / "relocations").glob("*.json")
        self.assert_refused("could not be written", roots=(self.new_root, self.old_root), env={FAIL_AT_ENV: "receipt"})
        self.assertEqual(sorted((self.new_dir / "relocations").glob("*.json")), [earlier])


class InterruptedRelocationTest(RelocateStoreTestCase):
    """A failure after the move leaves a started receipt, and `--resume` finishes what an uninterrupted run does."""

    # Where each test fault stops a move: how many of its six records are
    # rewritten, and whether the Store Release's directory has been renamed.
    FAULTS: dict[str, tuple[int, bool]] = {"rename": (0, False), "4": (3, True), "complete": (6, True)}

    def test_an_interrupted_move_is_finished_by_resume_as_an_uninterrupted_run(self) -> None:
        pristine = self.td / "pristine"
        shutil.copytree(self.old_dir, pristine)  # keeps modes and mtimes
        uninterrupted = self.relocate()
        self.assertEqual(uninterrupted.returncode, 0, uninterrupted.stdout + uninterrupted.stderr)
        expected_records, expected_receipt = records_of(self.new_dir), receipt_of(self.new_dir)
        for step in PRIVATE_RECORDS:
            self.assertEqual(expected_records[f"{step}.json"][1], 0o600, step)
        resume_command = f"--from {self.old_root} --to {self.new_root} --resume"

        for fault, (rewritten, renamed) in self.FAULTS.items():
            with self.subTest(fault=fault):
                shutil.rmtree(self.new_dir)
                shutil.copytree(pristine, self.old_dir)
                failed = self.relocate(env={FAIL_AT_ENV: fault})

                self.assertEqual(failed.returncode, UNFINISHED, failed.stdout + failed.stderr)
                here = self.new_dir if renamed else self.old_dir
                self.assertEqual(self.old_dir.exists(), not renamed)
                (receipt_p,) = (here / "relocations").glob("*.json")
                self.assertIn(str(receipt_p), failed.stderr)
                self.assertIn(resume_command, failed.stderr)
                self.assertNotIn("Nothing was moved", failed.stderr)
                started = json.loads(receipt_p.read_text(encoding="utf-8"))
                self.assertEqual(started["status"], "started")
                planned = {entry["path"]: entry["sha256_after"] for entry in started["records"]}
                now = {f"records/{name}": hashlib.sha256(data).hexdigest() for name, (_, _, data) in records_of(here).items()}
                self.assertEqual(sum(now[name] == planned[name] for name in planned), rewritten)
                # What a replacement that failed part-way leaves beside the records.
                leftover = here / "records" / ".register.json.relocating.4242"
                leftover.write_text("{", encoding="utf-8")
                rerun = self.assert_refused(str(receipt_p), resume_command)
                self.assertNotIn("Nothing was moved", rerun)

                resumed = self.relocate("--resume")

                self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
                self.assertFalse(self.old_dir.exists())
                self.assertFalse((self.new_dir / "records" / leftover.name).exists())
                self.assertEqual(records_of(self.new_dir), expected_records)
                receipt = receipt_of(self.new_dir)
                self.assertEqual(receipt["status"], "complete")
                self.assertEqual(len(receipt["resumed_at"]), 1)
                self.assertEqual(untimed(receipt), untimed(expected_receipt))

    def test_a_plain_run_names_the_resume_that_would_finish_a_move_or_why_none_would(self) -> None:
        receipt_p = self.interrupt()
        elsewhere = self.td / "elsewhere"
        elsewhere.mkdir()
        # Run from where the release now is: the command names the receipt's roots.
        self.assert_refused(
            str(receipt_p), f"--from {self.old_root} --to {self.new_root} --resume", roots=(self.new_root, elsewhere)
        )
        record_p = paths.record_path(STORE_ID, "validate", root=self.new_root)
        record = json.loads(record_p.read_text(encoding="utf-8"))
        record["stderr"] = "rerun after the move\n"
        record_p.write_text(run.record_text(record), encoding="utf-8")

        stderr = self.assert_refused(str(receipt_p), "records/validate.json is neither the record")

        self.assertNotIn("--resume", stderr)

    def test_resume_refuses_with_nothing_changed(self) -> None:
        """Every check is made before `--resume` moves or writes anything, and a refusal never asks for `--resume`."""

        def neither_hash(old_root: Path, new_root: Path, receipt_p: Path) -> tuple[Path, Path, str]:
            record_p = paths.record_path(STORE_ID, "validate", root=new_root)
            record = json.loads(record_p.read_text(encoding="utf-8"))
            record["stderr"] = "rerun after the move\n"
            record_p.write_text(run.record_text(record), encoding="utf-8")
            return old_root, new_root, "records/validate.json is neither the record"

        def another_move(old_root: Path, new_root: Path, receipt_p: Path) -> tuple[Path, Path, str]:
            elsewhere = old_root.parent / "elsewhere"
            elsewhere.mkdir()
            return elsewhere, new_root, f"moves {STORE_ID} from {old_root}"

        def both_directories(old_root: Path, new_root: Path, receipt_p: Path) -> tuple[Path, Path, str]:
            paths.store_dir(STORE_ID, root=old_root).mkdir()
            return old_root, new_root, "both"

        def two_receipts(old_root: Path, new_root: Path, receipt_p: Path) -> tuple[Path, Path, str]:
            shutil.copy2(receipt_p, receipt_p.with_name("20000101T000000Z.json"))
            return old_root, new_root, "2 unfinished moves"

        def record_added(old_root: Path, new_root: Path, receipt_p: Path) -> tuple[Path, Path, str]:
            shutil.copy2(
                paths.record_path(STORE_ID, "validate", root=new_root), paths.record_path(STORE_ID, "rho", root=new_root)
            )
            return old_root, new_root, "records/rho.json"

        def record_deleted(old_root: Path, new_root: Path, receipt_p: Path) -> tuple[Path, Path, str]:
            paths.record_path(STORE_ID, "validate", root=new_root).unlink()
            return old_root, new_root, "records/validate.json"

        def pending_publication(old_root: Path, new_root: Path, receipt_p: Path) -> tuple[Path, Path, str]:
            paths.publication_marker(STORE_ID, root=new_root).write_text("{}\n", encoding="utf-8")
            return old_root, new_root, str(paths.publication_marker(STORE_ID, root=new_root))

        def staged_store(old_root: Path, new_root: Path, receipt_p: Path) -> tuple[Path, Path, str]:
            paths.partial_store_path(STORE_ID, root=new_root).mkdir()
            return old_root, new_root, str(paths.partial_store_path(STORE_ID, root=new_root))

        def unreadable_record(old_root: Path, new_root: Path, receipt_p: Path) -> tuple[Path, Path, str]:
            record_p = paths.record_path(STORE_ID, "validate", root=new_root)
            record_p.unlink()
            record_p.mkdir()
            return old_root, new_root, "records/validate.json cannot be read"

        def receipt_without_resumes(old_root: Path, new_root: Path, receipt_p: Path) -> tuple[Path, Path, str]:
            document = json.loads(receipt_p.read_text(encoding="utf-8"))
            document["resumed_at"] = None
            receipt_p.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
            return old_root, new_root, "resumed_at"

        def mistyped_root(old_root: Path, new_root: Path, receipt_p: Path) -> tuple[Path, Path, str]:
            typo = new_root.with_name("publishd")
            return old_root, typo, f"{typo} is not a directory"

        cases: tuple[Callable[[Path, Path, Path], tuple[Path, Path, str]], ...] = (
            neither_hash, another_move, both_directories, two_receipts, record_added, record_deleted,
            pending_publication, staged_store, unreadable_record, receipt_without_resumes, mistyped_root,
        )
        for case, arrange in enumerate(cases):
            with self.subTest(arrange.__name__):
                roots = self.release_in(self.td / f"case-{case}")
                receipt_p = self.interrupt(roots)
                old_root, new_root, named = arrange(*roots, receipt_p)

                stderr = self.assert_refused(named, words=("--resume",), roots=(old_root, new_root))

                self.assertNotIn("--resume", stderr)
                self.assertNotIn("Nothing was moved", stderr)
                for root in {old_root, new_root} - set(roots):
                    # Checked before any lock: no lock file under a root the move does not name.
                    self.assertFalse((root / paths.RELEASE_LOCKS_DIRNAME).exists(), root)

        with self.subTest("no unfinished move"):
            stderr = self.assert_refused("no unfinished move", words=("--resume",))
            self.assertNotIn("--resume", stderr)


if __name__ == "__main__":
    unittest.main()

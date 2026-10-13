#!/usr/bin/env python3
"""A whole-stack crash of a forced release at each kind of boundary, through the entry point (#195).

At each boundary below, a forced `workflow/release.py` run is killed. The kill
is SIGKILL to its whole process group: the entry point, Snakemake and the
register job die together, as they would on a host crash, and no hook,
`finally` or settlement runs. The next run through the same supported entry
point must then recover:

- after the publication marker exists (every boundary from `marker-written` on,
  `marker-removed` included), the next run finishes the publication, clears
  Snakemake's mark that the killed register job's output is incomplete, and
  then has nothing left to do;
- before it (`snapshots-taken`, `publication-started`), nothing was published:
  the old Store and Validation Record stand, the leftover snapshot makes the
  next run refuse (the decision of 7 Oct 2026). Once an operator resolves it
  through the entry point (`--resolve-snapshot delete` before any job ran,
  `restore` after) and clears Snakemake's stale lock, a forced run replaces the
  release.

Every entry-point boundary is crashed here. The register job's boundaries are
crashed here by kind, because each costs a whole release: the register suite
(tests/register) already kills the publication transaction at every one of them
and completes it, in seconds. What this suite adds is Snakemake's view of the
killed job, and that differs between them only in whether the publication
marker exists and whether the job's output, `records/register.json`, has been
written. One boundary of each kind is crashed here.

Each boundary has its own directories, so Snakemake's metadata for one cannot
make another look up to date.

Run from the repository root:
    pixi run -e dev python3 tests/workflow/test_release_faults.py
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import yaml

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ogstores import paths, register, release, run  # noqa: E402
from test_workflow import (  # noqa: E402
    SNAKEFILE_PATH,
    create_dense_fixture_store,
    find_snakemake_cmd,
    release_argv,
    run_release,
)

STORE_ID = "OGS-00099"

# One register-job boundary of each kind Snakemake can see; the register suite
# crashes the transaction at all of them.
REGISTER_JOB_KINDS: dict[str, str] = {
    "publication-started": "no marker yet",
    "store-published": "marker, register.json not yet written",
    "register-record-written": "marker and register.json",
}

# The boundaries crashed here, in the order a forced release crosses them:
# every entry-point boundary, and the register job's by kind.
BOUNDARIES: tuple[str, ...] = (
    "snapshots-taken",
    "publication-started",
    "store-published",
    "register-record-written",
    "before-settle",
    "metadata-cleaned",
    "marker-removed",
)
BEFORE_PUBLICATION: frozenset[str] = frozenset({"snapshots-taken", "publication-started"})


def records_of(records_dir: Path) -> dict[str, tuple[int, bytes]]:
    return {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in sorted(records_dir.iterdir())}


_TEMPLATE_DIR: tempfile.TemporaryDirectory | None = None


def setUpModule() -> None:
    """Publish the fixture release once, through the entry point, as every case's starting point.

    Once for the module, not per class: a publication is a whole release.
    """
    global _TEMPLATE_DIR
    _TEMPLATE_DIR = tempfile.TemporaryDirectory()
    root = Path(_TEMPLATE_DIR.name)
    stores, artifacts = root / "stores", root / "artifacts"
    stores.mkdir()
    artifacts.mkdir()
    create_dense_fixture_store(
        stores,
        store_id=STORE_ID,
        artifact_root=artifacts,
        post_top_hits=False,
        post_overview=False,
    )
    PublishedReleaseFixture.template_stores = stores
    PublishedReleaseFixture.template_artifacts = artifacts
    PublishedReleaseFixture.first = run_release([STORE_ID], registry_root=stores, artifact_root=artifacts)


def tearDownModule() -> None:
    if _TEMPLATE_DIR is not None:
        _TEMPLATE_DIR.cleanup()


class PublishedReleaseFixture(unittest.TestCase):
    """One release published through the entry point (`setUpModule`), copied fresh for each case."""

    template_stores: Path
    template_artifacts: Path
    first: subprocess.CompletedProcess[str]

    def assert_first_publication(self) -> None:
        self.assertEqual(self.first.returncode, 0, self.first.stdout + self.first.stderr)
        self.assertIn(f"finished the publication of {STORE_ID}", self.first.stdout)
        self.assertFalse(paths.publication_marker(STORE_ID, root=self.template_artifacts).exists())

    def published_copy(self, aged: bool = True) -> tuple[Path, Path]:
        """A fresh copy of the published release, aged (by default) so the bundle is newer than its records."""
        td = Path(tempfile.mkdtemp(prefix="release_fault_"))
        self.addCleanup(shutil.rmtree, td, True)
        stores, artifacts = td / "stores", td / "artifacts"
        shutil.copytree(self.template_stores, stores)
        shutil.copytree(self.template_artifacts, artifacts)
        if not aged:
            return stores, artifacts
        day_ns = 86_400 * 10**9
        for p in paths.store_dir(STORE_ID, root=artifacts).rglob("*"):
            if p.is_file():
                st = p.stat()
                os.utime(p, ns=(st.st_atime_ns - day_ns, st.st_mtime_ns - day_ns))
        return stores, artifacts

    def assert_replaced(
        self,
        stores: Path,
        artifacts: Path,
        old_manifest: bytes,
        old_records: dict[str, tuple[int, bytes]],
        old_validation: bytes,
    ) -> None:
        """The new release is published, the old one archived whole, and nothing is pending."""
        store_dir = paths.store_dir(STORE_ID, root=artifacts)
        archives = sorted((store_dir / "replaced").iterdir())
        self.assertEqual(len(archives), 1, archives)
        archive = archives[0]
        self.assertEqual((archive / "store.opengwasdb" / "manifest.json").read_bytes(), old_manifest)
        self.assertEqual(records_of(archive / "records"), old_records)
        self.assertEqual((archive / "validation.yaml").read_bytes(), old_validation)
        self.assertNotEqual(
            (paths.store_path(STORE_ID, root=artifacts) / "manifest.json").read_bytes(), old_manifest
        )
        written = yaml.safe_load((stores / STORE_ID / "validation.yaml").read_text(encoding="utf-8"))
        self.assertEqual(written["replaced"]["archive"], str(archive))
        register_rec = json.loads(paths.record_path(STORE_ID, "register", root=artifacts).read_text())
        self.assertEqual(register_rec["replaced_archive"], str(archive))
        for leftover in (
            paths.publication_marker(STORE_ID, root=artifacts),
            paths.backup_store_path(STORE_ID, root=artifacts),
            paths.partial_store_path(STORE_ID, root=artifacts),
        ):
            self.assertFalse(leftover.exists(), leftover)
        self.assertEqual(run.pending_force_snapshots(STORE_ID, artifacts), [])
        self.assertEqual(sorted(store_dir.glob(".records.before-force-*")), [])


class TestEveryBoundaryThroughTheEntryPoint(PublishedReleaseFixture):
    """SIGKILL the whole stack at each boundary; the next entry-point run recovers."""

    def test_the_first_publication_went_through_the_entry_point(self) -> None:
        self.assert_first_publication()

    def test_every_entry_point_boundary_and_each_kind_of_register_boundary_is_crashed_here(self) -> None:
        """An entry-point boundary added without a case here, or a stale name, fails this test.

        A register-job boundary added to the code is crashed by the register
        suite, which iterates `register.PUBLICATION_BOUNDARIES`. The kinds
        crashed here must still sit either side of the marker and of the
        register record being written.
        """
        self.assertLessEqual(set(release.ENTRY_POINT_BOUNDARIES), set(BOUNDARIES))
        self.assertLessEqual(
            set(BOUNDARIES), set(release.ENTRY_POINT_BOUNDARIES) | set(register.PUBLICATION_BOUNDARIES)
        )
        self.assertLessEqual(set(REGISTER_JOB_KINDS), set(BOUNDARIES))
        order = register.PUBLICATION_BOUNDARIES.index
        self.assertLess(order("publication-started"), order("marker-written"))
        self.assertLess(order("marker-written"), order("store-published"))
        self.assertLess(order("store-published"), order("register-record-written"))
        self.assertLess(order("register-record-written"), order("marker-removed"))

    def check_boundary(self, boundary: str) -> None:
        stores, artifacts = self.published_copy()
        store_p = paths.store_path(STORE_ID, root=artifacts)
        records = paths.records_dir(STORE_ID, root=artifacts)
        validation_p = stores / STORE_ID / "validation.yaml"
        old_manifest = (store_p / "manifest.json").read_bytes()
        old_records = records_of(records)
        old_validation = validation_p.read_bytes()

        def release_run(force: bool = False, env: dict[str, str] | None = None, new_session: bool = False):
            return run_release(
                [STORE_ID],
                registry_root=stores,
                artifact_root=artifacts,
                config={"force": "1"} if force else None,
                env=env,
                new_session=new_session,
            )

        killed = release_run(force=True, env={run.KILL_GROUP_AT_ENV: boundary}, new_session=True)
        self.assertEqual(killed.returncode, -9, f"no kill at {boundary}:\n{killed.stdout}\n{killed.stderr}")

        if boundary in BEFORE_PUBLICATION:
            # Nothing was published: the old Store and record stand, and the snapshot blocks.
            self.assertEqual((store_p / "manifest.json").read_bytes(), old_manifest)
            self.assertEqual(validation_p.read_bytes(), old_validation)
            self.assertFalse(paths.publication_marker(STORE_ID, root=artifacts).exists())
            snapshots = run.pending_force_snapshots(STORE_ID, artifacts)
            self.assertEqual(len(snapshots), 1, snapshots)
            for force in (False, True):
                refused = release_run(force=force)
                self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
                self.assertIn(str(snapshots[0]), refused.stderr)
                self.assertIn("restore it", refused.stderr)
                self.assertIn("delete it", refused.stderr)

            # The operator resolves the snapshot as the refusal says: before any
            # job ran, records/ still describes the Store, so the snapshot is
            # deleted; once jobs had rewritten it, the snapshot is restored.
            action = "delete" if boundary == "snapshots-taken" else "restore"
            resolved = run_release(
                [STORE_ID, "--resolve-snapshot", action], registry_root=stores, artifact_root=artifacts
            )
            self.assertEqual(resolved.returncode, 0, resolved.stdout + resolved.stderr)
            self.assertEqual(records_of(records), old_records)
            self.assertEqual(run.pending_force_snapshots(STORE_ID, artifacts), [])
            # Then clears Snakemake's stale lock, as the refusal and the specification say.
            unlock = subprocess.run(
                find_snakemake_cmd()
                + ["--snakefile", str(SNAKEFILE_PATH), "--unlock", "--config",
                   f"registry_root={stores}", f"artifact_root={artifacts}"],
                cwd=REPO_ROOT, capture_output=True, text=True,
            )
            self.assertEqual(unlock.returncode, 0, unlock.stdout + unlock.stderr)
            res = release_run(force=True)
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        else:
            res = release_run()
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            self.assertIn("nothing to be done", res.stdout)
            if boundary != "marker-removed":
                self.assertIn(f"completed the interrupted publication of {STORE_ID}", res.stdout)

        self.assert_replaced(stores, artifacts, old_manifest, old_records, old_validation)
        again = release_run()
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("nothing to be done", again.stdout)


class TestLiveAndInterruptedRuns(PublishedReleaseFixture):
    """A live run is never taken for a dead one, and an interrupted run settles (#195, review round 3)."""

    def start_paused_forced_run(self, stores: Path, artifacts: Path) -> tuple[subprocess.Popen, Path, Path]:
        """Start a forced run in its own session, paused once its build job has started."""
        pause = stores.parent / "paused"
        log = stores.parent / "forced.log"
        handle = log.open("w")
        self.addCleanup(handle.close)
        env = {**os.environ, run.PAUSE_AT_ENV: "build-started", run.PAUSE_FILE_ENV: str(pause)}
        proc = subprocess.Popen(
            release_argv([STORE_ID], registry_root=stores, artifact_root=artifacts, config={"force": "1"}),
            cwd=REPO_ROOT, stdout=handle, stderr=subprocess.STDOUT, env=env, start_new_session=True,
        )
        self.addCleanup(lambda: proc.poll() is None and os.killpg(proc.pid, signal.SIGKILL))
        deadline = time.monotonic() + 300
        while not pause.exists():
            if proc.poll() is not None:
                self.fail(f"the forced run ended before its build started:\n{log.read_text()}")
            if time.monotonic() > deadline:
                self.fail(f"the forced run never reached its build:\n{log.read_text()}")
            time.sleep(0.2)
        return proc, pause, log

    def test_a_live_run_is_refused_not_mistaken_for_a_leftover(self) -> None:
        """While a forced run builds, a second run, a forced one and --resolve-snapshot all refuse; it then completes."""
        stores, artifacts = self.published_copy()
        store_p = paths.store_path(STORE_ID, root=artifacts)
        old_manifest = (store_p / "manifest.json").read_bytes()
        old_records = records_of(paths.records_dir(STORE_ID, root=artifacts))
        old_validation = (stores / STORE_ID / "validation.yaml").read_bytes()
        proc, pause, log = self.start_paused_forced_run(stores, artifacts)
        snapshots = run.pending_force_snapshots(STORE_ID, artifacts)
        self.assertEqual(len(snapshots), 1)

        for words, config in (([STORE_ID], None), ([STORE_ID], {"force": "1"}),
                              ([STORE_ID, "--resolve-snapshot", "restore"], None),
                              ([STORE_ID, "--resolve-snapshot", "delete"], None)):
            with self.subTest(words=words, config=config):
                res = run_release(words, registry_root=stores, artifact_root=artifacts, config=config)
                self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
                self.assertIn(f"a run of {STORE_ID} is in progress (pid {proc.pid},", res.stderr)
                self.assertNotIn("stopped before it began to publish", res.stderr)
        self.assertEqual(records_of(snapshots[0]), old_records, "the live run's snapshot is untouched")

        dry = run_release([STORE_ID, "--dry-run"], registry_root=stores, artifact_root=artifacts)
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertIn(f"a run of {STORE_ID} is in progress (pid {proc.pid},", dry.stdout)

        pause.unlink()
        self.assertEqual(proc.wait(timeout=600), 0, log.read_text())
        self.assert_replaced(stores, artifacts, old_manifest, old_records, old_validation)

    def test_recovery_never_completes_a_live_runs_publication(self) -> None:
        """A marker under another run's lock is that run's; recovery from any other run leaves it alone."""
        stores, artifacts = self.published_copy()
        marker = paths.publication_marker(STORE_ID, root=artifacts)
        marker.write_text('{"not": "for recovery to read"}', encoding="utf-8")
        live = release.ReleaseLocks(artifacts)
        live.acquire(STORE_ID)
        self.addCleanup(live.release_all)

        mine = release.ReleaseLocks(artifacts)
        self.addCleanup(mine.release_all)
        messages = release.recover_releases(
            release.parse_invocation(["--config", f"registry_root={stores}", f"artifact_root={artifacts}"]),
            stores, artifacts, mine,
        )

        self.assertEqual(messages, [])
        self.assertEqual(marker.read_text(encoding="utf-8"), '{"not": "for recovery to read"}')
        self.assertFalse(mine.holds(STORE_ID))

    def test_a_dry_run_during_a_live_publication_says_in_progress(self) -> None:
        """A marker under a live run's lock is that run's publication, not one a crash interrupted (round 4)."""
        stores, artifacts = self.published_copy(aged=False)
        paths.publication_marker(STORE_ID, root=artifacts).write_text("{}", encoding="utf-8")
        live = release.ReleaseLocks(artifacts)
        live.acquire(STORE_ID)
        self.addCleanup(live.release_all)

        dry = run_release([STORE_ID, "--dry-run"], registry_root=stores, artifact_root=artifacts)

        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertIn(f"a run of {STORE_ID} is in progress (pid {os.getpid()},", dry.stdout)
        self.assertNotIn("completes the interrupted publication", dry.stdout)

    def check_interrupt(self, deliver: str, signum: int, *, force_again: bool = False) -> None:
        stores, artifacts = self.published_copy()
        store_p = paths.store_path(STORE_ID, root=artifacts)
        records = paths.records_dir(STORE_ID, root=artifacts)
        old_manifest = (store_p / "manifest.json").read_bytes()
        old_records = records_of(records)
        validation_p = stores / STORE_ID / "validation.yaml"
        old_validation = validation_p.read_bytes()
        proc, pause, log = self.start_paused_forced_run(stores, artifacts)
        self.assertFalse(paths.record_path(STORE_ID, "build", root=artifacts).exists(), "mid-build")

        if deliver == "group":
            os.killpg(proc.pid, signum)  # a terminal's Ctrl-C reaches the foreground group
        elif deliver == "pid":
            os.kill(proc.pid, signum)  # `kill <pid>` reaches the entry point alone
        else:
            # A scheduler's limit reaches every process of the job: the entry point's
            # group and Snakemake's own session, found through the paused build job.
            job_pid = int(pause.read_text(encoding="utf-8").strip())
            os.killpg(os.getpgid(job_pid), signum)
            os.killpg(proc.pid, signum)
        returncode = proc.wait(timeout=600)

        self.assertEqual(returncode, 128 + signum, log.read_text())
        self.assertIn(f"restored the records of {STORE_ID}", log.read_text())
        self.assertEqual(records_of(records), old_records, "records restored byte-for-byte")
        self.assertEqual((store_p / "manifest.json").read_bytes(), old_manifest)
        self.assertEqual(validation_p.read_bytes(), old_validation)
        self.assertEqual(run.pending_force_snapshots(STORE_ID, artifacts), [])
        self.assertFalse(paths.publication_marker(STORE_ID, root=artifacts).exists())
        self.assertFalse((paths.store_dir(STORE_ID, root=artifacts) / "replaced").exists())

        # Nothing is left to recover or resolve: the release is exactly as it was
        # before the forced run, so an unforced run meets the ordinary refusal, with
        # no IncompleteFilesException and no leftover snapshot.
        res = run_release([STORE_ID], registry_root=stores, artifact_root=artifacts)
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("already published", res.stderr)
        self.assertNotIn("IncompleteFilesException", res.stdout + res.stderr)
        self.assertNotIn("stopped before it began to publish", res.stderr)
        if not force_again:
            return
        forced = run_release([STORE_ID], registry_root=stores, artifact_root=artifacts, config={"force": "1"})
        self.assertEqual(forced.returncode, 0, forced.stdout + forced.stderr)
        self.assert_replaced(stores, artifacts, old_manifest, old_records, old_validation)

    def test_ctrl_c_mid_build_restores_the_records(self) -> None:
        """SIGINT to the foreground group (Ctrl-C) mid-build: exit 130, records restored."""
        self.check_interrupt("group", signal.SIGINT)

    def test_sigterm_to_the_entry_point_mid_build_restores_the_records(self) -> None:
        """SIGTERM to the entry point alone (`kill`) mid-build: exit 143, records restored."""
        self.check_interrupt("pid", signal.SIGTERM)

    def test_sigterm_to_every_process_mid_build_restores_the_records(self) -> None:
        """SIGTERM to the entry point and Snakemake's session (a scheduler limit) mid-build: exit 143.

        This delivery reaches Snakemake itself, so it is the one that also proves
        a forced run then replaces the release: each forced run is a whole release.
        """
        self.check_interrupt("all", signal.SIGTERM, force_again=True)


def _case(boundary: str):
    def test(self: TestEveryBoundaryThroughTheEntryPoint) -> None:
        self.check_boundary(boundary)

    test.__doc__ = f"A whole-stack SIGKILL at {boundary!r}; the next entry-point run recovers."
    return test


for _boundary in BOUNDARIES:
    setattr(
        TestEveryBoundaryThroughTheEntryPoint,
        f"test_kill_at_{BOUNDARIES.index(_boundary):02d}_{_boundary.replace('-', '_')}",
        _case(_boundary),
    )


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Adding a published Store's missing post steps in place, with no rebuild (#528, ADR 0044).

These tests need no engine. `opengwasdb` is a stub install on PATH, pinned by
patching the locked commit, that answers `info` and `validate --format json`
from files each fixture Store carries and adds a marker file for
`build-dense-rho`, `clump` and `regenerate-overview`. The published release is
written from the recipe's own plan: its records, its register record and its
Validation Record, as `register` writes them. Store contents are the engine's to
test; the one real-engine run of this mode is `tests/clumps-pipeline`'s
`AddedInPlaceTest`.

The fixture's recipe declares `overview` and an `hm3` Indexed Variant Subset,
as every hosted Dense recipe does, and then gains `post.rho`, `post.clumps` and
its LD Store mapping. They hold the mode to the issue and its review:

1. The plan is the missing steps, the regenerated `overview`, `validate` and
   the register; one run adds them, the old Store goes under `replaced/<UTC>/`,
   and the new Validation Record records `has_rho`, `has_clumps` and the
   lineage. A second run, `release all` and a direct Snakemake dry run all do
   nothing, though the `hm3` record is now older than the `clump` record before it.
2. "Up to date" needs current evidence: a missing `validate` record, or a
   `clump` record planned with another LD Store mapping, is planned again.
3. A failure before the marker (a failing step; SIGTERM, repeated during the
   rollback) leaves the published Store, `records/` and `validation.yaml`
   byte-identical; an interruption after it is reported as pending and the next
   run finishes it.
4. A 0.2.x Store, a changed Store-writing step, a filesystem with no reflink and
   a symlinked Store are refused before anything is staged; a rewritten Store is
   held to ADR 0043's deviation rule.

Run from the repository root:
    pixi run -e dev test --suite post-additions
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "workflow"))

from test_workflow import create_dense_fixture_store  # noqa: E402

from ogstores import (  # noqa: E402
    bundle,
    engine,
    manifest,
    paths,
    post_additions,
    register,
    release,
    run,
    staged_publication,
)
from ogstores.plan import plan  # noqa: E402

STORE_ID = "OGS-99528"
ENGINE_COMMIT = "5" * 40
RELEASE_ID = "rel-1"
N_LOCI = 3

STUB = """#!{python}
import json, sys
from pathlib import Path
known = ("info", "validate", "build-dense-rho", "clump", "regenerate-overview", "recount-top-hits")
command = sys.argv[1]
if sys.argv[2:] == ["--help"]:
    sys.exit(0 if command in known else 2)
store = Path(sys.argv[2])
if (store / f"stub-fail-{{command}}").exists():
    print(f"{{command}} failed on purpose", file=sys.stderr)
    sys.exit(1)
if command == "info":
    print((store / "stub-info.json").read_text().strip())
elif command == "validate":
    payload = (store / "stub-validate.json").read_text().strip()
    print(payload)
    sys.exit(0 if json.loads(payload)["ok"] else 1)
elif command == "build-dense-rho":
    (store / "rho").write_text("rho\\n")
elif command == "clump":
    (store / "clumps").write_text(" ".join(sys.argv[3:]) + "\\n")
    print(json.dumps({{"n_loci_5e8": {n_loci}, "n_unclumped_5e8": 0}}))
elif command == "recount-top-hits":
    (store / "recount").write_text("after-rho" if (store / "rho").exists() else "before-rho")
elif command == "regenerate-overview":
    (store / "overview.html").write_text("rho" if (store / "rho").exists() else "no rho")
else:
    sys.exit(2)
"""


def finding(rule: str, severity: str, message: str) -> dict[str, str]:
    return {"message": message, "rule": rule, "severity": severity, "target": "."}


def verdict(*findings: dict[str, str]) -> dict[str, Any]:
    errors = [f["message"] for f in findings if f["severity"] == "error"]
    warnings = [f["message"] for f in findings if f["severity"] == "warning"]
    return {"errors": errors, "findings": list(findings), "ok": not errors, "warnings": warnings}


NATIVE_WARNING = finding("eaf_orientation.unverified", "warning", "analysis 'a' stores EAF whose orientation is unverified")
DEVIATION_WARNING = finding("variant_table.source_alid", "warning", (
    "no row carries a source_alid [a deviation recorded by ogdb rewrite-release --convert-legacy, "
    "which converted this release from format_version 0.2.0 (release obs); a rebuild from source fixes it]"
))


def tree(root: Path) -> dict[str, tuple[int, bytes]]:
    return {str(p.relative_to(root)): (p.stat().st_mtime_ns, p.read_bytes()) for p in sorted(root.rglob("*")) if p.is_file()}


class Fixture:
    """One published Dense release with no Rho Matrix or Clumps, whose recipe then gains them."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.registry, self.root = tmp / "stores", tmp / "artifacts"
        self.store = paths.store_path(STORE_ID, root=self.root)
        self.store_dir = paths.store_dir(STORE_ID, root=self.root)
        self.records = paths.records_dir(STORE_ID, root=self.root)
        self.validation_yaml = self.registry / STORE_ID / "validation.yaml"
        self.ld_store = tmp / "ld-stores" / "EUR"
        self._engine()
        self._published()

    def _engine(self) -> None:
        bin_dir = self.tmp / "engine" / "bin"
        bin_dir.mkdir(parents=True)
        exe = bin_dir / "opengwasdb"
        exe.write_text(STUB.format(python=sys.executable, n_loci=N_LOCI))
        exe.chmod(0o755)
        dist = self.tmp / "engine" / "lib" / "python3.12" / "site-packages" / "opengwasdb-0.20.0.dist-info"
        dist.mkdir(parents=True)
        (dist / "direct_url.json").write_text(json.dumps(
            {"url": "https://github.com/opengwas/opengwasdb", "vcs_info": {"vcs": "git", "commit_id": ENGINE_COMMIT}}
        ))
        (dist / "METADATA").write_text("Name: opengwasdb\nVersion: 0.20.0\n")
        self.path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"

    def _published(self) -> None:
        variant_list = self.tmp / "hm3.alid.txt"
        variant_list.write_text("1:100:A:G\n")
        create_dense_fixture_store(self.registry, STORE_ID, artifact_root=self.root, post_overview=True, trial=True)
        self.edit_recipe(lambda doc: doc.update({"indexed_subsets": [{
            "name": "hm3", "resource_id": "hapmap3-hg38", "variant_list": str(variant_list),
            "reference_assembly": "GRCh38",
        }]}))
        b = bundle.load(STORE_ID, registry_root=self.registry)
        manifest.materialise_build_manifest(
            b.analyses_path,
            paths.build_manifest_path(STORE_ID, root=self.root),
            paths.build_manifest_sidecar_path(STORE_ID, root=self.root),
        )
        self.store.mkdir(parents=True)
        (self.store / "data").write_text("published\n")
        (self.store / "overview.html").write_text("no rho")
        self.write_info("0.3.0")
        self.write_verdict(verdict())
        self.records.mkdir(parents=True)
        steps = plan(b, artifact_root=self.root)
        for step in steps:
            self.write_record(step.name, step.argv)
        run.write_record_atomically({
            "step": "register", "store_id": STORE_ID, "exit_code": 0, "success": True,
            "inputs": [str(paths.record_path(STORE_ID, s.name, root=self.root)) for s in steps],
            "outputs": [str(self.validation_yaml)], "published_store": str(self.store),
            "replaced_archive": None, "record_path": str(paths.record_path(STORE_ID, "register", root=self.root)),
            "opengwasdb_rev": ENGINE_COMMIT, "opengwasdb_version": "0.20.0", "opengwasdb_executable": "opengwasdb",
        }, paths.record_path(STORE_ID, "register", root=self.root))
        self.validation_yaml.write_text(yaml.safe_dump({
            "status": "passed", "validated_at": "2026-11-20T10:00:00Z",
            "validator": {"name": register.VALIDATOR_NAME, "version": f"opengwasdb@{ENGINE_COMMIT}"},
            "build_environment": {
                "opengwasdb_version": "0.20.0", "opengwasdb_commit": ENGINE_COMMIT, "python_version": "3.12",
                "platform": "linux",
                "engines": [{"opengwasdb_commit": ENGINE_COMMIT, "opengwasdb_version": "0.20.0",
                             "steps": [s.name for s in steps]}],
            },
            "observed": {"format_version": "0.3.0", "n_analyses": 1, "has_hm3": True, "has_rho": False,
                         "has_clumps": None, "build_elapsed_s": 100.0, "validate_status": "passed",
                         "completion_state": "observed_only"},
            "checks": {"store": "passed"}, "warnings": [], "errors": [],
        }, sort_keys=False))
        # The recipe now declares the Rho Matrix and the Clumps (#520 stream D).
        self.edit_recipe(lambda doc: doc["post"].update(
            {"rho": True, "clumps": True, "ld_stores": {"EUR": str(self.ld_store)}}
        ))

    def write_record(self, name: str, planned: list[str], **extra: Any) -> None:
        run.write_record_atomically({
            "step": name, "store_id": STORE_ID, "exit_code": 0, "success": True, "elapsed_seconds": 10.0,
            "argv": list(planned), "planned_argv": list(planned), "stdout": "", "stderr": "",
            "opengwasdb_rev": ENGINE_COMMIT, "opengwasdb_version": "0.20.0", "opengwasdb_executable": "opengwasdb",
            "record_path": str(paths.record_path(STORE_ID, name, root=self.root)), **extra,
        }, paths.record_path(STORE_ID, name, root=self.root))

    def write_info(self, format_version: str) -> None:
        (self.store / "stub-info.json").write_text(json.dumps({
            "format_version": format_version, "release_id": RELEASE_ID, "store_id": STORE_ID,
        }))

    def write_verdict(self, payload: dict[str, Any]) -> None:
        (self.store / "stub-validate.json").write_text(json.dumps(payload))

    def edit_recipe(self, edit) -> None:
        build_p = self.registry / STORE_ID / "build.yaml"
        document = yaml.safe_load(build_p.read_text(encoding="utf-8"))
        edit(document)
        build_p.write_text(yaml.safe_dump(document), encoding="utf-8")

    def edit_register(self, edit) -> None:
        register_p = paths.record_path(STORE_ID, "register", root=self.root)
        record = json.loads(register_p.read_text(encoding="utf-8"))
        edit(record)
        run.write_record_atomically(record, register_p)

    def bundle(self) -> bundle.Bundle:
        return bundle.load(STORE_ID, registry_root=self.registry)

    def assess(self) -> post_additions.Assessment | None:
        return post_additions.assess(self.bundle(), self.root)

    def state(self) -> dict[str, Any]:
        """The published Store, records/ and validation.yaml, byte for byte, and what lies beside them."""
        return {
            "store": tree(self.store),
            "records": tree(self.records),
            "validation.yaml": self.validation_yaml.read_bytes(),
            "beside": sorted(p.name for p in self.store_dir.iterdir() if p.name not in ("work", "tmp")),
        }


class PostAdditionsCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR")))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.fx = Fixture(self.tmp)
        snakemake_dir = self.tmp / "snakemake"
        snakemake_dir.mkdir()
        for patcher in (
            mock.patch.dict(os.environ, {"PATH": self.fx.path, release.SNAKEMAKE_DIRECTORY_ENV: str(snakemake_dir)}),
            mock.patch.object(engine, "locked_commit", return_value=ENGINE_COMMIT),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def release(self, *words: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        argv = [*(words or (STORE_ID,)), "--config", f"registry_root={self.fx.registry}", f"artifact_root={self.fx.root}"]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = release.main(argv)
        return code, out.getvalue(), err.getvalue()

    def add(self) -> dict[str, Any] | None:
        """One addition through the module, Snakemake's metadata left alone."""
        assessment = self.fx.assess()
        assert assessment is not None
        return post_additions.add_post_steps(assessment, registry_root=self.fx.registry)

    def assert_untouched(self, before: dict[str, Any]) -> None:
        self.assertEqual(self.fx.state(), before)
        self.assertFalse(os.path.lexists(paths.partial_store_path(STORE_ID, root=self.fx.root)))
        self.assertEqual(run.pending_force_snapshots(STORE_ID, self.fx.root), [])
        self.assertFalse(paths.publication_marker(STORE_ID, root=self.fx.root).exists())

    def assert_refused(self, *phrases: str) -> None:
        before = self.fx.state()
        with self.assertRaises(post_additions.PostAdditionRefused) as raised:
            self.add()
        for phrase in phrases:
            self.assertIn(phrase, str(raised.exception))
        self.assert_untouched(before)


class TestAdding(PostAdditionsCase):
    def test_the_plan_is_the_missing_steps_the_overview_validate_and_register(self) -> None:
        before = self.fx.state()

        code, out, err = self.release(STORE_ID, "--dry-run")

        self.assertEqual(code, 0, out + err)
        planned = [line.split(":", 1)[0].strip() for line in out.splitlines() if line.startswith("  ")]
        self.assertEqual(planned, ["rho", "clump", "overview", "validate", "register"], out)
        self.assertIn(f"--ld-store EUR={self.fx.ld_store}", out)
        self.assert_untouched(before)

    def test_one_run_adds_them_and_nothing_reschedules_them(self) -> None:
        old_validation = self.fx.validation_yaml.read_text(encoding="utf-8")
        hm3 = paths.record_path(STORE_ID, "indexed-subset-hm3", root=self.fx.root)
        hm3_before = (hm3.stat().st_mtime_ns, hm3.read_bytes())

        code, out, err = self.release()

        self.assertEqual(code, 0, out + err)
        written = yaml.safe_load(self.fx.validation_yaml.read_text(encoding="utf-8"))
        observed = written["observed"]
        self.assertEqual((observed["has_rho"], observed["has_clumps"], observed["n_loci_5e8"]), (True, True, N_LOCI))
        ran = sum(
            run.read_record(paths.record_path(STORE_ID, s, root=self.fx.root))["elapsed_seconds"]
            for s in ("rho", "clump", "overview", "validate")
        )
        self.assertAlmostEqual(observed["build_elapsed_s"], round(100.0 - 10.0 - 10.0 + ran, 3), places=3)
        self.assertEqual(written[post_additions.BLOCK][0]["steps"], ["rho", "clump", "overview"])
        # The declared overview is regenerated, and reports the Rho Matrix; the subset is not rebuilt.
        self.assertEqual((self.fx.store / "overview.html").read_text(), "rho")
        self.assertEqual((hm3.stat().st_mtime_ns, hm3.read_bytes()), hm3_before)
        archive = Path(written["replaced"]["archive"])
        self.assertEqual((archive / "store.opengwasdb" / "overview.html").read_text(), "no rho")
        self.assertEqual((archive / "validation.yaml").read_text(encoding="utf-8"), old_validation)
        register_record = run.read_record(paths.record_path(STORE_ID, "register", root=self.fx.root))
        self.assertEqual(
            [Path(p).stem for p in register_record["inputs"]],
            ["build", "indexed-subset-hm3", "rho", "clump", "overview", "validate"],
        )
        self.assertTrue(post_additions.current(self.fx.bundle(), self.fx.root))

        before = self.fx.state()
        for words in ((STORE_ID,), ("all",)):
            code, out, err = self.release(*words)
            self.assertEqual((code, "nothing to be done" in out), (0, True), out + err)
        self.assertEqual(self.snakemake_jobs(), [])
        self.assert_untouched(before)

    def snakemake_jobs(self) -> list[str]:
        """The steps a direct `snakemake --dry-run` of the release schedules."""
        dry = subprocess.run(
            [sys.executable, "-m", "snakemake", "--snakefile", str(release.SNAKEFILE), "--dry-run", STORE_ID,
             "--config", f"registry_root={self.fx.registry}", f"artifact_root={self.fx.root}"],
            cwd=self.tmp / "snakemake", capture_output=True, text=True,
        )
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        return [job[2] or "" for job in release.scheduled_jobs(dry.stdout + dry.stderr)]

    def test_a_forced_run_of_a_settled_release_schedules_the_whole_build(self) -> None:
        self.add()

        code, out, err = self.release(STORE_ID, "--dry-run", "--config", "force=1")

        self.assertEqual(code, 0, out + err)
        steps = {job[2] for job in release.scheduled_jobs(out + err)}
        self.assertLessEqual({"build", "rho", "clump", "indexed-subset-hm3", "overview", "validate"}, steps, out)

    def test_a_newer_build_manifest_still_schedules_the_build(self) -> None:
        self.add()
        manifest_p = paths.build_manifest_path(STORE_ID, root=self.fx.root)
        later = paths.record_path(STORE_ID, "register", root=self.fx.root).stat().st_mtime + 60
        os.utime(manifest_p, (later, later))

        self.assertIn("build", self.snakemake_jobs())
        # Unforced, the published-Store guard refuses it, as it always has; the dry run says so
        # before the runtime checks, which this stub engine would fail first.
        for words in ((STORE_ID,), ("all",)):
            code, out, err = self.release(*words, "--dry-run")
            self.assertEqual(code, 0, out + err)
            self.assertIn("the real run would refuse", out)
            self.assertIn("already published", out)

    def test_a_drifted_or_failed_record_is_refused_not_up_to_date(self) -> None:
        self.add()
        self.fx.edit_recipe(lambda doc: doc["build"]["options"].update({"source-assembly": "hg19"}))
        for words in ((STORE_ID,), ("all",)):
            code, out, err = self.release(*words)
            self.assertEqual(code, 1, out + err)
            self.assertIn("`--source-assembly hg38` became `--source-assembly hg19`", err)

        self.fx.edit_recipe(lambda doc: doc["build"]["options"].update({"source-assembly": "hg38"}))
        self.fx.edit_register(lambda r: r.update({"success": False, "exit_code": 1}))
        code, out, err = self.release()
        self.assertEqual(code, 1, out + err)
        self.assertIn("records no successful registration", err)
        self.assertFalse(post_additions.current(self.fx.bundle(), self.fx.root))

    def test_up_to_date_needs_current_evidence(self) -> None:
        self.add()
        self.assertTrue(self.fx.assess().up_to_date)

        paths.record_path(STORE_ID, "validate", root=self.fx.root).unlink()
        self.assertEqual([s.name for s in self.fx.assess().steps], ["validate"])
        self.assertFalse(post_additions.current(self.fx.bundle(), self.fx.root))
        self.add()
        self.assertTrue(self.fx.assess().up_to_date)

        self.fx.edit_recipe(lambda doc: doc["post"]["ld_stores"].update({"EUR": str(self.tmp / "other-ld")}))
        self.assertEqual([s.name for s in self.fx.assess().steps], ["clump", "overview", "validate"])
        self.assertFalse(post_additions.current(self.fx.bundle(), self.fx.root))

    def test_a_missing_overview_record_is_added(self) -> None:
        self.fx.edit_recipe(lambda doc: doc["post"].update({"rho": False, "clumps": False}))
        paths.record_path(STORE_ID, "overview", root=self.fx.root).unlink()

        self.assertEqual([s.name for s in self.fx.assess().steps], ["overview", "validate"])

    def test_a_release_still_to_complete_its_store_is_left_to_completion(self) -> None:
        """Completion comes first: a recipe that also gains `complete` is the own-id completion's (ADR 0037)."""
        self.fx.edit_recipe(lambda doc: doc.update(
            {"completion_state": "reference_completed", "complete": {"command": "complete-dense"}}
        ))
        self.assertIsNone(self.fx.assess())


class TestResolverMismatch(PostAdditionsCase):
    """The resolver-mismatch rule is `staged_publication.republication`'s, as `register`'s is for a run (#520)."""

    def test_the_verdict_agrees_with_register(self) -> None:
        """One warning naming both engines, `status` raised, `checks.store` validate's own (`register`'s shape)."""
        record = yaml.safe_load(self.fx.validation_yaml.read_text())
        record["acceptance"] = {"resolver_engine": {"opengwasdb_commit": "6" * 40, "opengwasdb_version": "0.19.0"}}
        self.fx.validation_yaml.write_text(yaml.safe_dump(record, sort_keys=False))

        written = self.add()

        assert written is not None
        self.assertEqual(len(written["warnings"]), 1, written["warnings"])
        self.assertIn("6" * 40, written["warnings"][0])
        self.assertIn(ENGINE_COMMIT, written["warnings"][0])
        self.assertEqual(written["status"], "passed_with_warnings")
        self.assertEqual(written["checks"], {"store": "passed"}, "validate's own verdict is unchanged")
        self.assertEqual(written["observed"]["validate_status"], "passed")


class TestRecountTopHits(PostAdditionsCase):
    """A completed Hybrid's additions begin with `recount-top-hits` (opengwasdb#509, #532, ADR 0044)."""

    def completed_hybrid(self) -> None:
        """The fixture's Store stands for a completed Hybrid; `plan()` and the stub engine are the same."""
        patcher = mock.patch.object(post_additions, "_completed_hybrid", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_plan_recounts_before_rho_clump_and_overview(self) -> None:
        self.completed_hybrid()
        before = self.fx.state()

        code, out, err = self.release(STORE_ID, "--dry-run")

        self.assertEqual(code, 0, out + err)
        planned = [line.split(":", 1)[0].strip() for line in out.splitlines() if line.startswith("  ")]
        self.assertEqual(planned, ["recount-top-hits", "rho", "clump", "overview", "validate", "register"], out)
        self.assertIn(f"recount-top-hits: opengwasdb recount-top-hits {self.fx.store}", out)
        self.assert_untouched(before)

    def test_a_run_recounts_first_and_records_it(self) -> None:
        self.completed_hybrid()

        written = self.add()

        assert written is not None
        self.assertEqual((self.fx.store / "recount").read_text(), "before-rho")
        self.assertEqual(written[post_additions.BLOCK][0]["steps"], ["recount-top-hits", "rho", "clump", "overview"])
        record = run.read_record(paths.record_path(STORE_ID, "recount-top-hits", root=self.fx.root))
        self.assertEqual(record["exit_code"], 0)
        register_record = run.read_record(paths.record_path(STORE_ID, "register", root=self.fx.root))
        self.assertEqual(
            [Path(p).stem for p in register_record["inputs"]],
            ["build", "indexed-subset-hm3", "recount-top-hits", "rho", "clump", "overview", "validate"],
        )
        # Its seconds are in the elapsed sum, like any step this run ran.
        ran = sum(
            run.read_record(paths.record_path(STORE_ID, s, root=self.fx.root))["elapsed_seconds"]
            for s in ("recount-top-hits", "rho", "clump", "overview", "validate")
        )
        self.assertAlmostEqual(written["observed"]["build_elapsed_s"], round(100.0 - 10.0 - 10.0 + ran, 3), places=3)
        # The route does not reschedule it, and a fresh plan never holds it.
        self.assertTrue(post_additions.current(self.fx.bundle(), self.fx.root))
        self.assertNotIn("recount-top-hits", [s.name for s in plan(self.fx.bundle(), artifact_root=self.fx.root)])

    def test_a_failing_recount_publishes_nothing(self) -> None:
        self.completed_hybrid()
        before = self.fx.state()
        # The staged copy is a reflink of the published Store, so the marker reaches it from there.
        (self.fx.store / "stub-fail-recount-top-hits").write_text("")
        before = self.fx.state()
        self.assert_refused("recount-top-hits failed on purpose")

    def test_nothing_is_recounted_for_a_release_that_is_not_a_completed_hybrid(self) -> None:
        planned = [s.name for s in self.fx.assess().steps]
        self.assertNotIn("recount-top-hits", planned)

    def test_the_trigger_is_a_completed_hybrid_and_the_completion_phase_names_it(self) -> None:
        from types import SimpleNamespace

        def release(layout: str, completion_state: str, own: bool) -> Any:
            return SimpleNamespace(
                store_id=STORE_ID, layout=layout, completion_state=completion_state, completes_own_store=own
            )

        hybrid = release("hybrid", "reference_completed", True)
        step = post_additions._recount_step(hybrid, "completion", self.fx.root)
        assert step is not None
        self.assertEqual((step.name, step.phase), ("completion-recount-top-hits", "completion"))
        self.assertEqual(step.argv, ["opengwasdb", "recount-top-hits", str(self.fx.store)])
        # A completed Hybrid that published once, a two-id child, is the build phase's.
        child = post_additions._recount_step(release("hybrid", "reference_completed", False), "build", self.fx.root)
        assert child is not None
        self.assertEqual(child.name, "recount-top-hits")
        for other in (
            (release("hybrid", "observed_only", False), "build"),
            (release("dense", "reference_completed", True), "completion"),
            (release("ragged", "reference_completed", False), "build"),
        ):
            self.assertIsNone(post_additions._recount_step(other[0], other[1], self.fx.root))
        # The Observed-Only phase of a Hybrid that completes its own Store has nothing to recount.
        self.assertIsNone(post_additions._recount_step(hybrid, "build", self.fx.root))


class TestFailures(PostAdditionsCase):
    def test_a_failing_step_leaves_everything_as_it_was(self) -> None:
        (self.fx.store / "stub-fail-clump").write_text("")
        self.assert_refused("clump failed on purpose", "Nothing was published")
        kept = list(paths.work_dir(STORE_ID, root=self.fx.root).glob("post-additions-refused-*-clump.json"))
        self.assertEqual(len(kept), 1)

    def test_a_validation_error_names_its_errors(self) -> None:
        self.fx.write_verdict(verdict(finding("clumps.reclump", "error", "lead 3 is not the lead")))
        self.assert_refused("lead 3 is not the lead")

    def test_sigterm_and_a_second_during_the_rollback_leave_everything_as_it_was(self) -> None:
        before = self.fx.state()
        real_boundary, real_restore = run.fault_boundary, run.restore_force_snapshot

        def boundary(name: str) -> None:
            if name == "clump-started":
                os.kill(os.getpid(), signal.SIGTERM)
            real_boundary(name)

        def restore(snapshot: Path) -> Path:
            os.kill(os.getpid(), signal.SIGTERM)  # ignored while the rollback runs
            return real_restore(snapshot)

        with (
            mock.patch.object(run, "fault_boundary", side_effect=boundary),
            mock.patch.object(run, "restore_force_snapshot", side_effect=restore),
            self.assertRaises(staged_publication.Interrupted) as raised,
        ):
            self.add()

        self.assertEqual(raised.exception.state, staged_publication.ROLLED_BACK)
        self.assert_untouched(before)

    def test_an_interruption_after_the_marker_is_reported_pending_and_finished_next(self) -> None:
        real = register.complete_publication
        calls: list[Path] = []

        def interrupted(store_id: str, root: Path, **finish: Any) -> Any:
            # The addition's own finish, the first call with a marker to carry out.
            if not calls and paths.publication_marker(store_id, root=root).exists():
                calls.append(Path(root))
                raise KeyboardInterrupt("signal 15")
            return real(store_id, root, **finish)

        with mock.patch.object(register, "complete_publication", side_effect=interrupted):
            code, out, err = self.release()

        self.assertEqual(code, 130, out + err)
        self.assertIn(f"the publication of {STORE_ID} had begun", err)
        self.assertTrue(paths.publication_marker(STORE_ID, root=self.fx.root).exists())
        self.assert_recovered()

    def test_an_interrupted_metadata_cleanup_keeps_the_marker_and_is_replayed(self) -> None:
        with mock.patch.object(release, "_cleanup_metadata", side_effect=KeyboardInterrupt("signal 15")):
            code, out, err = self.release()

        self.assertEqual(code, 130, out + err)
        self.assertIn("had begun", err)
        self.assertTrue(paths.publication_marker(STORE_ID, root=self.fx.root).exists())
        self.assert_recovered()

    def test_an_interruption_after_the_marker_is_removed_reports_it_published(self) -> None:
        real = register.remove_publication_marker

        def removed(store_id: str, root: Path) -> None:
            real(store_id, root)
            raise KeyboardInterrupt("signal 15")

        with (
            mock.patch.object(release, "_cleanup_metadata"),
            mock.patch.object(register, "remove_publication_marker", side_effect=removed),
        ):
            code, out, err = self.release()

        self.assertEqual(code, 130, out + err)
        self.assertIn(f"the publication of {STORE_ID} had finished", err)
        self.assertTrue(post_additions.current(self.fx.bundle(), self.fx.root))

    def assert_recovered(self) -> None:
        """The next run finishes the publication, clearing the metadata of every record the addition's register lists."""
        with mock.patch.object(release, "_cleanup_metadata") as cleanup:
            code, out, err = self.release()

        self.assertEqual(code, 0, out + err)
        self.assertIn("completed the interrupted publication", out)
        self.assertFalse(paths.publication_marker(STORE_ID, root=self.fx.root).exists())
        cleared = {Path(f).stem for call in cleanup.call_args_list for f in call.args[1]}
        self.assertLessEqual({"register", "build", "indexed-subset-hm3", "rho", "clump", "overview", "validate"}, cleared)
        self.assertEqual((self.fx.store / "overview.html").read_text(), "rho")
        self.assertTrue(post_additions.current(self.fx.bundle(), self.fx.root))


class TestRefusals(PostAdditionsCase):
    def test_a_0_2_x_store_is_refused_before_staging(self) -> None:
        self.fx.write_info("0.2.3")
        self.assert_refused("format '0.2.3'", "Nothing was written")

    def test_a_changed_store_writing_step_is_refused(self) -> None:
        self.fx.edit_recipe(lambda doc: doc["build"]["options"].update({"source-assembly": "hg19"}))
        with self.assertRaises(post_additions.PostAdditionRefused) as raised:
            self.fx.assess()
        self.assertIn("`--source-assembly hg38` became `--source-assembly hg19`", str(raised.exception))

    def test_a_filesystem_with_no_reflink_is_refused_naming_it(self) -> None:
        stub = self.tmp / "no-reflink"
        stub.mkdir()
        (stub / "cp").write_text("#!/bin/sh\necho 'cp: failed to clone: Operation not supported' >&2\nexit 1\n")
        (stub / "cp").chmod(0o755)
        filesystem = post_additions.filesystem_type(self.fx.store_dir)
        with mock.patch.dict(os.environ, {"PATH": f"{stub}{os.pathsep}{os.environ['PATH']}"}):
            self.assert_refused(f"on a {filesystem} filesystem", "never by a full copy")

    def test_a_symlinked_store_is_refused_before_anything_is_copied(self) -> None:
        elsewhere = self.tmp / "elsewhere"
        self.fx.store.rename(elsewhere)
        self.fx.store.symlink_to(elsewhere)
        before = self.fx.state()

        with self.assertRaises(post_additions.PostAdditionRefused) as raised:
            self.add()

        self.assertIn("symlink", str(raised.exception))
        self.assertEqual(self.fx.state(), before)
        self.assertFalse(os.path.lexists(paths.partial_store_path(STORE_ID, root=self.fx.root)))


class TestRewrittenStores(PostAdditionsCase):
    """A Store `publish_rewrite` published is held to ADR 0043's deviation rule when it is republished."""

    def rewritten(self, *, convert_legacy: bool) -> None:
        rewrite_p = paths.record_path(STORE_ID, run.REWRITE_RELEASE_STEP, root=self.fx.root)
        self.fx.write_record(run.REWRITE_RELEASE_STEP, ["opengwasdb", "rewrite-release"])
        rewrite = {"record": str(rewrite_p), "convert_legacy": convert_legacy, "release_id": RELEASE_ID}
        self.fx.edit_register(lambda r: r.update({"inputs": [*r["inputs"], str(rewrite_p)], "rewrite": rewrite}))
        document = yaml.safe_load(self.fx.validation_yaml.read_text(encoding="utf-8"))
        self.fx.validation_yaml.write_text(yaml.safe_dump({**document, "rewrite": rewrite}, sort_keys=False))

    def test_a_native_warning_refuses_a_converted_store(self) -> None:
        self.rewritten(convert_legacy=True)
        self.fx.write_verdict(verdict(NATIVE_WARNING))
        self.assert_refused("eaf_orientation.unverified", "neither a deviation its rewrite records")

    def test_a_recorded_deviation_is_accepted_and_the_rewrite_kept(self) -> None:
        self.rewritten(convert_legacy=True)
        self.fx.write_verdict(verdict(DEVIATION_WARNING))

        written = self.add()

        self.assertEqual(written["status"], "passed_with_warnings")
        self.assertTrue(written["rewrite"]["convert_legacy"])
        register_record = run.read_record(paths.record_path(STORE_ID, "register", root=self.fx.root))
        self.assertIn("rewrite", register_record)
        self.assertIn(run.REWRITE_RELEASE_STEP, [Path(p).stem for p in register_record["inputs"]])

    def test_a_carried_warning_is_accepted_and_a_new_one_refused(self) -> None:
        self.rewritten(convert_legacy=False)
        self.fx.edit_register(
            lambda r: r["rewrite"].update({"carried_warnings": [NATIVE_WARNING["message"]]})
        )
        new = finding("eaf_orientation.unverified", "warning", "analysis 'b' stores EAF whose orientation is unverified")

        self.fx.write_verdict(verdict(NATIVE_WARNING, new))
        self.assert_refused("analysis 'b'")
        self.fx.write_verdict(verdict(NATIVE_WARNING))
        written = self.add()

        self.assertEqual(written["status"], "passed_with_warnings")

    def test_a_rewrite_record_of_another_store_is_refused(self) -> None:
        """#527's rewrite identity check (`amendment._rewrite_changes`) is the shared walk's."""
        self.rewritten(convert_legacy=True)
        record_p = paths.record_path(STORE_ID, run.REWRITE_RELEASE_STEP, root=self.fx.root)
        record = json.loads(record_p.read_text(encoding="utf-8"))
        run.write_record_atomically({**record, "store_id": "OGS-00001"}, record_p)

        with self.assertRaises(post_additions.PostAdditionRefused) as raised:
            self.fx.assess()
        self.assertIn("'OGS-00001'", str(raised.exception))

    def test_a_conversion_recording_indel_order_still_gains_post_steps(self) -> None:
        """#527 refuses *completing* such a Store; adding a Rho Matrix or Clumps is not completion."""
        self.rewritten(convert_legacy=True)
        self.fx.edit_register(lambda r: r["rewrite"].update({"deviations": ["variant_table.indel_order"]}))

        self.assertEqual([s.name for s in self.fx.assess().steps], ["rho", "clump", "overview", "validate"])


if __name__ == "__main__":
    unittest.main()

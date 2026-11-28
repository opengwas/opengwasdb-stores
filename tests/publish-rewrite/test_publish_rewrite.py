#!/usr/bin/env python3
"""Publishing a rewritten Store Release through the registry (#533, ADR 0043).

`ogstores.publish_rewrite` publishes a 0.3.0 Store Release that `opengwasdb
rewrite-release` already wrote beside a published 0.2.x one. These tests hold it
to the issue's acceptance criteria:

1. One call publishes it: the old Store goes under `replaced/<UTC>/` with its
   records and Validation Record, the records hold the rewrite and validate
   steps, and `validation.yaml` names format 0.3.0, the source release and the
   conversion's deviations, with the validator's warnings exactly.
2. Each refusal leaves the published Store, `records/` and `validation.yaml`
   byte-identical and the staged release where it was: a staged release that
   is not 0.3.0, one that fails validation, one with a warning the conversion
   did not record, and a missing staged path.
3. A second call after success does nothing.
4. An interrupted publication recovers through the existing marker path
   (`register.complete_publication`).
5. A published 0.3.0 Observed-Only release, rebuilt or converted, completes under
   its own id (#527, ADR 0037 amended): `TestRebuiltParent`, `TestConvertedParent`.

The Stores are directories of files, as in `tests/register`, and `opengwasdb`
is a stub on PATH that answers `info` and `validate --format json` in the
engine's own JSON shapes, from files each fixture Store carries. The shapes
were taken from engine c8ebe4ca's `rewrite-release --convert-legacy` output on
its own 0.2.0 Dense fixtures (25 Nov 2026). Store contents are tested once, in
opengwasdb (docs/spec/store-release-workflow.md, "Tests").

Run from the repository root:
    pixi run -e dev test --suite publish-rewrite
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import yaml

from ogstores import amendment, bundle, manifest, paths, plan, publish_rewrite, run, staged_publication
from ogstores.publish_rewrite import RewritePublicationRefused, publish_rewrite as publish

STORE_ID = "OGS-00042"
ENGINE_COMMIT = "e" * 40  # an engine install the stub stands for; not the locked commit (tests/engine)
OLD_RELEASE = "comp"
NEW_RELEASE = "9e6f075c-77de-4f9b-8c98-3db7ee6d2a6b"

# `opengwasdb info --format json` and `validate --format json`, answered from the
# Store's own `stub-info.json` and `stub-validate.json`: the stub reads nothing else.
STUB = """#!{python}
import json, sys
from pathlib import Path
command = sys.argv[1]
if sys.argv[2:] == ["--help"]:
    sys.exit(0 if command in ("info", "validate") else 2)
store = Path(sys.argv[2])
if command == "info":
    print((store / "stub-info.json").read_text().strip())
    sys.exit(0)
if command == "validate":
    payload = (store / "stub-validate.json").read_text().strip()
    print(payload)
    sys.exit(0 if json.loads(payload)["ok"] else 1)
sys.exit(2)
"""


def info(release_id: str, format_version: str, store_id: str = STORE_ID) -> dict[str, Any]:
    """What `opengwasdb info --format json` prints for a fixture release (engine c8ebe4ca)."""
    return {
        "association_coverage": "gwas",
        "completion_state": "reference_completed",
        "encoding": {},
        "format_version": format_version,
        "opengwasdb_version": "0.20.0",
        "primary_layout": "dense",
        "reference_assembly": "GRCh38",
        "release_id": release_id,
        "store_id": store_id,
    }


def finding(rule: str, severity: str, message: str) -> dict[str, str]:
    return {"message": message, "rule": rule, "severity": severity, "target": "."}


LIFTED_WARNING = finding(
    "variant_table.source_alid",
    "warning",
    "manifest.json records a liftover but none of the 600 rows of variants.tsv.gz carries a "
    "source_alid [a deviation recorded by ogdb rewrite-release --convert-legacy, which converted "
    "this release from format_version 0.2.0 (release obs); a rebuild from source fixes it]",
)
INDEL_WARNING = finding(
    "variant_table.indel_order",
    "warning",
    "3 indel ALIDs are not spelt reference first [a deviation recorded by ogdb rewrite-release "
    "--convert-legacy, which converted this release from format_version 0.2.0 (release comp); "
    "a rebuild from source fixes it]",
)
UNRECORDED_WARNING = finding(
    "eaf_orientation.unverified",
    "warning",
    "analysis 'a' stores EAF whose orientation is unverified: general Dense source supplied no "
    "orientation reference",
)
SKIP = finding("rho.symmetric", "skip", "no Rho Matrix to judge")


def verdict(*findings: dict[str, str]) -> dict[str, Any]:
    """`validate --format json`'s object: `errors` and `warnings` projected from `findings`."""
    errors = [f["message"] for f in findings if f["severity"] == "error"]
    return {
        "errors": errors,
        "findings": list(findings),
        "ok": not errors,
        "warnings": [f["message"] for f in findings if f["severity"] == "warning"],
    }


PREVIOUS_OBSERVED = {
    "format_version": "0.2.0",
    "n_analyses": 2,
    "n_variants": 201,
    "n_associations": 380,
    "association_coverage": "gwas",
    "reference_assembly": None,
    "store_bytes": 11,
    "has_hm3": True,
    "has_rho": None,
    "n_top_hits": None,
    "build_elapsed_s": 15.0,
    "validate_status": "passed",
    "completion_state": "reference_completed",
}
ACCEPTANCE = {"recorded_at": None, "commit": "fa2ea2c", "warnings": ["Input accepted."]}


class Fixture:
    """A registry with one published 0.2.0 Dense release and its rewrite staged beside it."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.registry = tmp / "stores"
        self.root = tmp / "artifacts"
        self.store_dir = paths.store_dir(STORE_ID, root=self.root)
        self.store = paths.store_path(STORE_ID, root=self.root)
        self.staged = self.store_dir / "store.opengwasdb.0.3.0"
        self.evidence = tmp / "logs" / STORE_ID
        self.engine = tmp / "engine"
        self._bundle()
        self._published()
        self._engine()

    # -- the registry ------------------------------------------------------
    def _bundle(self) -> None:
        bundle_dir = self.registry / STORE_ID
        bundle_dir.mkdir(parents=True)
        (bundle_dir / "release.yaml").write_text(yaml.safe_dump({
            "store_id": STORE_ID, "label": "test", "status": "validated",
            "source_collection_id": "test", "association_coverage": "gwas", "derived_from": None,
            "created_at": "2026-08-18T08:51:59Z", "description": "Test release",
            "source_snapshot_id": "snap", "release_kind": "pilot", "generator": {"command": "g"},
        }))
        (bundle_dir / "build.yaml").write_text(yaml.safe_dump({
            "store_id": STORE_ID, "layout": "dense", "completion_state": "observed_only",
            "build": {"command": "build-dense-vcf", "options": {"source-assembly": "GRCh38", "n-workers": 16}},
            "post": {"top_hits": True, "overview": True},
        }))
        (bundle_dir / "analyses.tsv").write_text("analysis_id\tsource_file\na\t/a.vcf.gz\nb\t/b.vcf.gz\n")
        self.validation_yaml = bundle_dir / "validation.yaml"
        self.validation_yaml.write_text(yaml.safe_dump({
            "status": "passed", "validated_at": "2026-11-02T10:41:20Z",
            "validator": {"name": "opengwasdb validate", "version": "opengwasdb@" + "4" * 40},
            "build_environment": {"opengwasdb_version": "0.3.2", "opengwasdb_commit": "4" * 40,
                                  "python_version": "3.12", "platform": "linux"},
            "observed": PREVIOUS_OBSERVED, "checks": {"store": "passed"}, "warnings": [], "errors": [],
            "acceptance": ACCEPTANCE,
        }, sort_keys=False))

    # -- the Artifact Root -------------------------------------------------
    def _published(self) -> None:
        self.store.mkdir(parents=True)
        (self.store / "data").write_text("old release")
        self.write_info(self.store, info(OLD_RELEASE, "0.2.0"))
        records = paths.records_dir(STORE_ID, root=self.root)
        records.mkdir(parents=True)
        for step in ("build", "top-hits", "overview", "validate"):
            run.write_record_atomically({"step": step, "stdout": "old"}, records / f"{step}.json")
        run.write_record_atomically({
            "step": "register", "store_id": STORE_ID, "exit_code": 0, "success": True,
            "inputs": [str(records / f"{s}.json") for s in ("build", "top-hits", "overview", "validate")],
            "published_store": str(self.store), "replaced_archive": None,
        }, records / "register.json")

    def _engine(self) -> None:
        """A stub engine install: its executable on PATH, its commit in its dist-info."""
        bin_dir = self.engine / "bin"
        bin_dir.mkdir(parents=True)
        self.exe = bin_dir / "opengwasdb"
        self.exe.write_text(STUB.format(python=sys.executable))
        self.exe.chmod(0o755)
        dist = self.engine / "lib" / "python3.12" / "site-packages" / "opengwasdb-0.20.0.dist-info"
        dist.mkdir(parents=True)
        (dist / "direct_url.json").write_text(json.dumps(
            {"url": "https://github.com/opengwas/opengwasdb", "vcs_info": {"vcs": "git", "commit_id": ENGINE_COMMIT}}
        ))
        (dist / "METADATA").write_text("Name: opengwasdb\nVersion: 0.20.0\n")
        # The `pixi.lock` the validating runtime is held to: it pins the stub's commit.
        self.lock = self.tmp / "pixi.lock"
        self.lock.write_text(
            "version: 6\nenvironments:\n  dev:\n    packages:\n      linux-64:\n"
            f"      - pypi: git+https://github.com/opengwas/opengwasdb?branch=main#{ENGINE_COMMIT}\n"
        )

    @staticmethod
    def write_info(release: Path, payload: dict[str, Any]) -> None:
        (release / "stub-info.json").write_text(json.dumps(payload, sort_keys=True))

    def stage(
        self,
        *findings: dict[str, str],
        format_version: str = "0.3.0",
        flags: tuple[str, ...] = ("--convert-legacy", "--not-lifted"),
    ) -> None:
        """Stage a rewritten release and the operator's evidence of the run that wrote it."""
        self.staged.mkdir()
        (self.staged / "data").write_text("new release, rewritten")
        self.write_info(self.staged, info(NEW_RELEASE, format_version))
        (self.staged / "stub-validate.json").write_text(json.dumps(verdict(*findings), sort_keys=True))
        argv = [str(self.exe), "rewrite-release", str(self.store), str(self.staged), *flags, "--n-workers", "16"]
        self.evidence.parent.mkdir(parents=True, exist_ok=True)
        # As the #525 queue script writes them (`scripts/convert_queue.sh` in its run).
        self.evidence.with_suffix(".cmd").write_text(" ".join(argv) + " (budget 4320s)\n")
        # The rewrite starts after the published Store and the engine install were written.
        self.start = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0) + datetime.timedelta(seconds=1)
        self.end = self.start + datetime.timedelta(seconds=2166)
        self.evidence.with_suffix(".start").write_text(run.utc_timestamp(self.start) + "\n")
        self.evidence.with_suffix(".end").write_text(run.utc_timestamp(self.end) + "\n")
        self.evidence.with_suffix(".exit").write_text("0\n")
        self.evidence.with_suffix(".log").write_text(
            "  validated the staged release: 0.7s\nPublished " + str(self.staged) + " as 0.3.0\n"
        )
        self.evidence.with_suffix(".time").write_text(
            '\tCommand being timed: "timeout --kill-after=300 4320 ' + " ".join(argv) + '"\n'
            "\tElapsed (wall clock) time (h:mm:ss or m:ss): 36:06.07\n"
            "\tMaximum resident set size (kbytes): 26214400\n"
            "\tExit status: 0\n"
        )

    def env(self) -> dict[str, str]:
        return {**os.environ, "PATH": f"{self.engine / 'bin'}{os.pathsep}{os.environ['PATH']}"}

    def publish(self) -> dict[str, Any] | None:
        with patch.dict(os.environ, {"PATH": self.env()["PATH"]}):
            return publish(
                STORE_ID, self.staged, self.evidence,
                registry_root=self.registry, artifact_root=self.root, lock=self.lock,
            )

    def kill_at(self, boundary: str) -> subprocess.CompletedProcess[str]:
        """A call killed as SIGKILL would leave it, at `boundary` (`run.fault_boundary`)."""
        code = (
            "from pathlib import Path; from ogstores.publish_rewrite import publish_rewrite; "
            f"publish_rewrite({STORE_ID!r}, Path({str(self.staged)!r}), Path({str(self.evidence)!r}), "
            f"registry_root={str(self.registry)!r}, artifact_root={str(self.root)!r}, lock=Path({str(self.lock)!r}))"
        )
        return subprocess.run(
            [sys.executable, "-c", code], env={**self.env(), run.KILL_AT_ENV: boundary},
            capture_output=True, text=True,
        )

    def snapshot(self) -> dict[str, bytes]:
        """Every file of the published release, the records, the bundle and the staged one, by path.

        `work/` is left out: a refused validation's verdict is kept there
        (`test_a_refused_validation_keeps_its_verdict`).
        """
        work = paths.work_dir(STORE_ID, root=self.root)
        files: dict[str, bytes] = {}
        for base in (self.store_dir, self.registry):
            for p in sorted(base.rglob("*")):
                if p.is_file() and work not in p.parents:
                    files[str(p.relative_to(self.tmp))] = p.read_bytes()
        return files


class PublishRewriteCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self._tmp.cleanup)
        self.fx = Fixture(Path(self._tmp.name))


class TestPublishing(PublishRewriteCase):
    """Criterion 1: one call publishes a rewritten release over the published one."""

    def test_the_resolver_mismatch_verdict_agrees_with_register(self) -> None:
        """One warning naming both engines, `status` raised, `checks.store` validate's own (`register`'s shape, #520)."""
        fx = self.fx
        record = yaml.safe_load(fx.validation_yaml.read_text())
        record["acceptance"] = {"resolver_engine": {"opengwasdb_commit": "6" * 40, "opengwasdb_version": "0.19.0"}}
        fx.validation_yaml.write_text(yaml.safe_dump(record, sort_keys=False))
        fx.stage(INDEL_WARNING)

        written = fx.publish()

        assert written is not None
        mismatch = [w for w in written["warnings"] if "6" * 40 in w]
        self.assertEqual(len(mismatch), 1, written["warnings"])
        self.assertIn(ENGINE_COMMIT, mismatch[0])
        self.assertEqual(written["status"], "passed_with_warnings")
        self.assertEqual(written["checks"], {"store": "passed_with_warnings"}, "validate's own verdict: the deviation")

    def test_one_call_publishes_and_archives_the_replaced_release(self) -> None:
        fx = self.fx
        fx.stage(INDEL_WARNING, SKIP)

        data = fx.publish()

        assert data is not None
        self.assertEqual((fx.store / "data").read_text(), "new release, rewritten")
        self.assertFalse(fx.staged.exists(), "the staged release is what was published")
        self.assertFalse(paths.publication_marker(STORE_ID, root=fx.root).exists())
        (archive,) = (fx.store_dir / "replaced").iterdir()
        self.assertRegex(archive.name, r"\A\d{8}T\d{6}Z\Z")
        self.assertEqual((archive / "store.opengwasdb" / "data").read_text(), "old release")
        self.assertEqual(
            sorted(p.name for p in (archive / "records").iterdir()),
            ["build.json", "overview.json", "register.json", "top-hits.json", "validate.json"],
            "the replaced release's records, as they were",
        )
        self.assertEqual(yaml.safe_load((archive / "validation.yaml").read_text())["observed"]["format_version"], "0.2.0")
        self.assertEqual(list(fx.store_dir.glob(f"{paths.FORCE_SNAPSHOT_PREFIX}*")), [])

        record = yaml.safe_load(fx.validation_yaml.read_text())
        self.assertEqual(record, data)
        self.assertEqual(record["observed"]["format_version"], "0.3.0")
        self.assertEqual(record["status"], "passed_with_warnings")
        self.assertEqual(record["checks"], {"store": "passed_with_warnings"})
        self.assertEqual(record["warnings"], [INDEL_WARNING["message"]], "the validator's warnings, exactly")
        self.assertEqual(record["errors"], [])
        self.assertEqual(record["replaced"]["archive"], str(archive))
        self.assertEqual(record["acceptance"], ACCEPTANCE, "the accepted bundle's evidence is passed on")
        rewrite = record["rewrite"]
        self.assertEqual(rewrite["source_release_id"], OLD_RELEASE)
        self.assertEqual(rewrite["source_format_version"], "0.2.0")
        self.assertEqual(rewrite["release_id"], NEW_RELEASE)
        self.assertTrue(rewrite["convert_legacy"])
        self.assertEqual(rewrite["deviations"], ["variant_table.indel_order"])
        self.assertEqual(record["build_environment"]["opengwasdb_commit"], ENGINE_COMMIT)
        self.assertEqual(record["validator"]["version"], f"opengwasdb@{ENGINE_COMMIT}")

    def test_the_records_hold_the_rewrite_and_its_validation(self) -> None:
        fx = self.fx
        fx.stage(INDEL_WARNING)

        fx.publish()

        rewrite = run.load_record(STORE_ID, "rewrite-release", root=fx.root)
        assert rewrite is not None
        argv = fx.evidence.with_suffix(".cmd").read_text().split(" (budget")[0].split()
        self.assertEqual(rewrite["argv"], argv)
        self.assertEqual((rewrite["exit_code"], rewrite["success"]), (0, True))
        self.assertEqual((rewrite["start_time"], rewrite["end_time"]), (run.utc_timestamp(fx.start), run.utc_timestamp(fx.end)))
        self.assertEqual(rewrite["elapsed_seconds"], 2166.0)
        self.assertEqual(rewrite["peak_rss_kb"], 26214400)
        self.assertEqual(rewrite["budget_seconds"], 4320)
        self.assertEqual(rewrite["log_path"], str(fx.evidence.with_suffix(".log")))
        self.assertIn("as 0.3.0", rewrite["stdout"], "the log is kept in the record")
        self.assertEqual(rewrite["opengwasdb_rev"], ENGINE_COMMIT)
        self.assertEqual(rewrite["opengwasdb_version"], "0.20.0")
        self.assertEqual(rewrite["opengwasdb_executable"], str(fx.exe))

        validate = run.load_record(STORE_ID, "validate", root=fx.root)
        assert validate is not None
        self.assertEqual(
            validate["argv"],
            ["opengwasdb", "validate", str(paths.partial_store_path(STORE_ID, root=fx.root)),
             "--format", "json", "--n-workers", "16"],
            "the planned validate step, run on the staged release before it is published",
        )
        self.assertEqual(validate["exit_code"], 0)

        register = run.load_record(STORE_ID, "register", root=fx.root)
        assert register is not None
        records = paths.records_dir(STORE_ID, root=fx.root)
        self.assertEqual(
            register["inputs"],
            [str(records / f"{s}.json") for s in ("build", "top-hits", "overview", "validate", "rewrite-release")],
            "the replaced release's lineage, then the rewrite",
        )
        self.assertEqual(register["published_store"], str(fx.store))
        self.assertTrue(register["replaced_archive"].startswith(str(fx.store_dir / "replaced")))
        self.assertEqual((records / "build.json").read_text(), run.record_text({"step": "build", "stdout": "old"}))

    def test_observed_keeps_what_the_rewrite_cannot_change(self) -> None:
        """A conversion masks associations, so its count is unknown; the rest is carried."""
        fx = self.fx
        fx.stage(INDEL_WARNING)

        observed = (fx.publish() or {})["observed"]

        self.assertEqual(observed["format_version"], "0.3.0")
        self.assertEqual(observed["validate_status"], "passed_with_warnings")
        self.assertEqual(observed["store_bytes"], len("new release, rewritten") + len(
            (fx.store / "stub-info.json").read_bytes()) + len((fx.store / "stub-validate.json").read_bytes()))
        self.assertIsNone(observed["n_associations"])
        self.assertIsNone(observed["n_top_hits"])
        for field in ("n_analyses", "n_variants", "association_coverage", "reference_assembly",
                      "has_hm3", "has_rho", "completion_state"):
            self.assertEqual(observed[field], PREVIOUS_OBSERVED[field], field)
        validate = run.load_record(STORE_ID, "validate", root=fx.root) or {}
        self.assertEqual(
            observed["build_elapsed_s"],
            round(PREVIOUS_OBSERVED["build_elapsed_s"] + 2166.0 + validate["elapsed_seconds"], 3),
            "the schema's sum of step times: the replaced release's, the rewrite's and this validation's",
        )

    def replaced_completion_state(self, state: str | None) -> None:
        record = yaml.safe_load(self.fx.validation_yaml.read_text())
        if state is None:
            record["observed"].pop("completion_state")
        else:
            record["observed"]["completion_state"] = state
        self.fx.validation_yaml.write_text(yaml.safe_dump(record, sort_keys=False))

    def test_a_replaced_record_with_no_completion_state_publishes_the_measured_one(self) -> None:
        """OGS-00016's 0.2.0 record predates #307 (#525): the state is measured, not carried."""
        fx = self.fx
        self.replaced_completion_state(None)
        fx.stage(INDEL_WARNING)
        fx.write_info(fx.staged, {**info(NEW_RELEASE, "0.3.0"), "completion_state": "observed_only"})

        observed = (fx.publish() or {})["observed"]

        self.assertEqual(observed["completion_state"], "observed_only")

    def test_a_lifted_conversion_records_its_source_alid_deviation(self) -> None:
        fx = self.fx
        fx.stage(INDEL_WARNING, LIFTED_WARNING, flags=("--convert-legacy", "--lifted"))

        record = fx.publish() or {}

        self.assertEqual(record["rewrite"]["deviations"], ["variant_table.indel_order", "variant_table.source_alid"])
        self.assertEqual(record["warnings"], [INDEL_WARNING["message"], LIFTED_WARNING["message"]])

    def test_a_plain_rewrite_records_no_deviation_and_keeps_its_counts(self) -> None:
        fx = self.fx
        fx.stage(flags=())

        record = fx.publish() or {}

        self.assertEqual(record["status"], "passed")
        self.assertFalse(record["rewrite"]["convert_legacy"])
        self.assertEqual(record["rewrite"]["deviations"], [])
        self.assertEqual(record["observed"]["n_associations"], PREVIOUS_OBSERVED["n_associations"])


class TestRefusals(PublishRewriteCase):
    """Criterion 2: each refusal leaves the published release as it was."""

    def assertRefusedUnchanged(self, *phrases: str) -> None:
        fx = self.fx
        before = fx.snapshot()
        with self.assertRaises(RewritePublicationRefused) as caught:
            fx.publish()
        for phrase in phrases:
            self.assertIn(phrase, str(caught.exception))
        self.assertEqual(fx.snapshot(), before, "a refusal changed a file")
        self.assertFalse(paths.partial_store_path(STORE_ID, root=fx.root).exists())
        self.assertFalse(paths.publication_marker(STORE_ID, root=fx.root).exists())
        self.assertFalse((fx.store_dir / "replaced").exists())
        self.assertEqual(list(fx.store_dir.glob(f"*{paths.FORCE_SNAPSHOT_PREFIX}*")), [])

    def test_a_completion_state_that_differs_from_the_replaced_record_is_refused(self) -> None:
        fx = self.fx
        fx.stage(INDEL_WARNING)
        fx.write_info(fx.staged, {**info(NEW_RELEASE, "0.3.0"), "completion_state": "observed_only"})

        self.assertRefusedUnchanged("'reference_completed'", "'observed_only'")

    def test_a_staged_release_that_is_not_0_3_0_is_refused(self) -> None:
        self.fx.stage(format_version="0.2.3")
        self.assertRefusedUnchanged("0.2.3", "0.3.0")

    def test_a_release_that_fails_validation_is_refused(self) -> None:
        self.fx.stage(INDEL_WARNING, finding("variant_table.alid_sorted", "error", "rows out of order"))
        self.assertRefusedUnchanged("variant_table.alid_sorted", "rows out of order")

    def test_a_warning_the_conversion_did_not_record_is_refused(self) -> None:
        self.fx.stage(INDEL_WARNING, UNRECORDED_WARNING)
        self.assertRefusedUnchanged("eaf_orientation.unverified", "not a deviation")

    def carry(self, *messages: str) -> None:
        """The replaced Validation Record carries these warnings."""
        record = yaml.safe_load(self.fx.validation_yaml.read_text())
        record["warnings"] = list(messages)
        self.fx.validation_yaml.write_text(yaml.safe_dump(record, sort_keys=False))

    def test_a_warning_the_replaced_record_carried_unchanged_publishes_and_is_named(self) -> None:
        self.carry(UNRECORDED_WARNING["message"])
        self.fx.stage(UNRECORDED_WARNING, flags=())
        record = self.fx.publish()
        assert record is not None
        self.assertEqual(record["rewrite"]["carried_warnings"], [UNRECORDED_WARNING["message"]])
        self.assertEqual(record["rewrite"]["deviations"], [])
        self.assertEqual(record["warnings"], [UNRECORDED_WARNING["message"]])

    def test_judge_findings_table(self) -> None:
        w = UNRECORDED_WARNING["message"]
        warn = lambda rule, message: finding(rule, "warning", message)  # noqa: E731
        base = warn("eaf_orientation.unverified", w)
        cases = {
            "carried": ([base], dict(replaced_warnings=[w]), [w], 0),
            "changed count": ([warn(base["rule"], w + "; only 12 variants")], dict(replaced_warnings=[w]), [], 1),
            "changed subject": ([warn(base["rule"], w.replace("'a'", "'b'"))], dict(replaced_warnings=[w]), [], 1),
            "fired twice": ([base, base], dict(replaced_warnings=[w]), [w], 1),
            "new": ([base], dict(replaced_warnings=["another"]), [], 1),
            "empty record": ([base], dict(replaced_warnings=[]), [], 1),
            "null record": ([base], dict(replaced_warnings=None), [], 1),
            "non-list record": ([base], dict(replaced_warnings=w), [], 1),
            "deviation rule, recorded text, plain rewrite": (
                [INDEL_WARNING], dict(replaced_warnings=[INDEL_WARNING["message"]]), [], 1),
        }
        for name, (findings, kwargs, carried, unrecorded) in cases.items():
            with self.subTest(name):
                deviations, got, refused = publish_rewrite.judge_findings(findings, convert_legacy=False, **kwargs)
                self.assertEqual((deviations, got, len(refused)), ([], carried, unrecorded))
        deviations, got, refused = publish_rewrite.judge_findings(
            [INDEL_WARNING], convert_legacy=True, replaced_warnings=[INDEL_WARNING["message"]])
        self.assertEqual((deviations, got, refused), (["variant_table.indel_order"], [], []))

    def test_a_deviation_needs_the_conversions_note_on_all_four_rules(self) -> None:
        note = INDEL_WARNING["message"][INDEL_WARNING["message"].index(" ["):]
        rules = sorted(publish_rewrite.DEVIATION_RULES)
        self.assertEqual(len(rules), 4)
        self.assertIn("variant_table.lifted_contig", rules)
        for rule in rules:
            plain = finding(rule, "warning", "1271 not judged (no source ALID)")
            noted = finding(rule, "warning", "1271 not judged (no source ALID)" + note)
            lookalike = finding(rule, "warning", "1271 not judged [a deviation recorded by someone else]")
            with self.subTest(rule):
                self.assertEqual(publish_rewrite.judge_findings([noted], convert_legacy=True)[0], [rule])
                for refused in (plain, lookalike):
                    deviations, _, unrecorded = publish_rewrite.judge_findings([refused], convert_legacy=True)
                    self.assertEqual((deviations, unrecorded), ([], [refused]))
                    # The text being recorded in the replaced record does not make it one.
                    deviations, carried, unrecorded = publish_rewrite.judge_findings(
                        [refused], convert_legacy=True, replaced_warnings=[refused["message"]])
                    self.assertEqual((deviations, carried, unrecorded), ([], [], [refused]))

    def test_a_new_warning_the_replaced_record_lacks_is_refused(self) -> None:
        self.carry(UNRECORDED_WARNING["message"])
        new = finding("rho.unverified", "warning", "a new warning V")
        self.fx.stage(UNRECORDED_WARNING, new, flags=())
        self.assertRefusedUnchanged("rho.unverified", "a new warning V")

    def test_a_conversion_names_its_deviations_and_its_carried_warning(self) -> None:
        self.carry(UNRECORDED_WARNING["message"])
        self.fx.stage(INDEL_WARNING, UNRECORDED_WARNING)
        record = self.fx.publish()
        assert record is not None
        self.assertEqual(record["rewrite"]["deviations"], ["variant_table.indel_order"])
        self.assertEqual(record["rewrite"]["carried_warnings"], [UNRECORDED_WARNING["message"]])

    def test_the_legacy_list_is_pinned(self) -> None:
        """The list cannot grow silently: a rule joins it by an edit here, and a ruling."""
        self.assertEqual(
            [(e.rule, e.ticket) for e in publish_rewrite.LEGACY_WARNINGS],
            [
                ("effect_consistency.unchecked", "opengwasdb#510"),
                ("completion.ld_panel_unrecorded", "opengwasdb#327"),
                ("hybrid.overflow_associated", "opengwasdb#257"),
            ],
        )
        self.assertEqual(set(publish_rewrite.LEGACY_RULES), {e.rule for e in publish_rewrite.LEGACY_WARNINGS})

    def test_a_pre_510_rewrite_carrying_only_the_legacy_warnings_publishes_and_names_them(self) -> None:
        legacy = [
            finding("effect_consistency.unchecked", "warning", "analyses.tsv has none of the effect-consistency columns"),
            finding("completion.ld_panel_unrecorded", "warning", "the completion records no LD panel fields"),
            finding("hybrid.overflow_associated", "warning", "3 shared rows off the Dense Component hold an association"),
        ]
        # The replaced record carries none of them, as one validated by the engine before each rule.
        self.fx.stage(*legacy, flags=())
        record = self.fx.publish()
        assert record is not None
        accepted = record["rewrite"]["legacy_warnings"]
        self.assertEqual([e["rule"] for e in accepted], [e.rule for e in publish_rewrite.LEGACY_WARNINGS])
        self.assertEqual({e["status"] for e in accepted}, {"legacy, accepted"})
        self.assertEqual([e["ticket"] for e in accepted], ["opengwasdb#510", "opengwasdb#327", "opengwasdb#257"])
        self.assertTrue(all(e["why"] and e["decided_by"] for e in accepted))
        self.assertEqual(record["rewrite"]["carried_warnings"], [])
        self.assertEqual(len(record["warnings"]), 3)

    def test_a_rewrite_with_any_other_new_warning_still_refuses(self) -> None:
        legacy = finding("effect_consistency.unchecked", "warning", "none of the columns")
        self.fx.stage(legacy, UNRECORDED_WARNING, flags=())
        self.assertRefusedUnchanged("eaf_orientation.unverified", "not a deviation")

    def test_a_look_alike_rule_is_not_on_the_legacy_list(self) -> None:
        self.fx.stage(finding("effect_consistency.evidence", "warning", "some columns"), flags=())
        self.assertRefusedUnchanged("effect_consistency.evidence", "some columns")

    def test_a_republication_accepts_only_the_legacy_warnings_its_publication_recorded(self) -> None:
        w = finding("hybrid.overflow_associated", "warning", "3 rows")
        legacy, refused = publish_rewrite.accept_legacy([w], [])
        self.assertEqual((legacy, refused), ([], [w]))
        legacy, refused = publish_rewrite.accept_legacy([w], ["hybrid.overflow_associated", "not.listed"])
        self.assertEqual(([e["rule"] for e in legacy], refused), (["hybrid.overflow_associated"], []))
        other = finding("not.listed", "warning", "x")
        self.assertEqual(publish_rewrite.accept_legacy([other], ["not.listed"]), ([], [other]))

    def test_a_plain_rewrite_accepts_no_deviation(self) -> None:
        """#499's 0.2.3 rewrite records none, so every warning refuses it."""
        self.fx.stage(INDEL_WARNING, flags=())
        self.assertRefusedUnchanged("variant_table.indel_order")

    def test_a_missing_staged_path_is_refused(self) -> None:
        self.assertRefusedUnchanged(str(self.fx.staged), "no staged release")

    def test_evidence_naming_another_source_is_refused(self) -> None:
        fx = self.fx
        fx.stage(INDEL_WARNING)
        cmd = fx.evidence.with_suffix(".cmd")
        cmd.write_text(cmd.read_text().replace(str(fx.store), "/elsewhere/store.opengwasdb"))
        self.assertRefusedUnchanged("/elsewhere/store.opengwasdb")

    def test_evidence_that_disagrees_about_the_exit_is_refused(self) -> None:
        fx = self.fx
        fx.stage(INDEL_WARNING)
        fx.evidence.with_suffix(".exit").write_text("124\n")
        self.assertRefusedUnchanged("124")

    def test_a_rewrite_that_exited_nonzero_is_refused(self) -> None:
        """Evidence that agrees with itself on a failed run: `.exit` and the time report both say 1."""
        fx = self.fx
        fx.stage(INDEL_WARNING)
        fx.evidence.with_suffix(".exit").write_text("1\n")
        time_p = fx.evidence.with_suffix(".time")
        time_p.write_text(time_p.read_text().replace("Exit status: 0", "Exit status: 1"))
        self.assertRefusedUnchanged("exited 1")

    def test_malformed_shell_quoting_in_the_command_is_refused(self) -> None:
        fx = self.fx
        fx.stage(INDEL_WARNING)
        cmd = fx.evidence.with_suffix(".cmd")
        cmd.write_text(cmd.read_text().replace("rewrite-release", "rewrite-release 'unclosed"))
        self.assertRefusedUnchanged("is not one shell command", "No closing quotation")

    def test_evidence_that_is_not_utf8_is_refused(self) -> None:
        fx = self.fx
        fx.stage(INDEL_WARNING)
        fx.evidence.with_suffix(".log").write_bytes(b"validated \xff\xfe\n")
        self.assertRefusedUnchanged(str(fx.evidence.with_suffix(".log")), "cannot be read")

    def test_a_published_store_changed_after_the_rewrite_started_is_refused(self) -> None:
        """A path is not a release: the Store at the source path must be the one the rewrite read."""
        fx = self.fx
        fx.stage(INDEL_WARNING)
        later = fx.start.timestamp() + 60
        os.utime(fx.store, (later, later))
        self.assertRefusedUnchanged("changed after the rewrite started")

    def test_a_register_record_changed_after_the_rewrite_started_is_refused(self) -> None:
        fx = self.fx
        fx.stage(INDEL_WARNING)
        later = fx.start.timestamp() + 60
        os.utime(paths.record_path(STORE_ID, "register", root=fx.root), (later, later))
        self.assertRefusedUnchanged("register record", "changed after the rewrite started")

    def test_an_engine_reinstalled_after_the_rewrite_started_is_refused(self) -> None:
        """`info` names no writer, so the install's commit is trusted only if it predates the run."""
        fx = self.fx
        fx.stage(INDEL_WARNING)
        later = fx.start.timestamp() + 60
        for record in fx.engine.rglob("direct_url.json"):
            os.utime(record, (later, later))
        self.assertRefusedUnchanged("engine install", "was written after the rewrite started")

    def test_a_staged_release_of_another_store_is_refused(self) -> None:
        fx = self.fx
        fx.stage(INDEL_WARNING)
        fx.write_info(fx.staged, info(NEW_RELEASE, "0.3.0", store_id="OGS-00043"))
        self.assertRefusedUnchanged("OGS-00043", STORE_ID)

    def test_a_staged_release_info_cannot_identify_is_refused(self) -> None:
        """An absent release_id is refused, never compared equal to another absent one."""
        fx = self.fx
        fx.stage(INDEL_WARNING)
        payload = info(NEW_RELEASE, "0.3.0")
        del payload["release_id"]
        fx.write_info(fx.staged, payload)
        self.assertRefusedUnchanged("reports no release_id")

    def test_a_register_record_with_no_lineage_is_refused(self) -> None:
        fx = self.fx
        fx.stage(INDEL_WARNING)
        register_p = paths.record_path(STORE_ID, "register", root=fx.root)
        record = json.loads(register_p.read_text())
        del record["inputs"]
        run.write_record_atomically(record, register_p)
        os.utime(register_p, (fx.start.timestamp() - 60,) * 2)
        self.assertRefusedUnchanged("names no records it registered")

    def test_a_runtime_that_is_not_the_locked_engine_is_refused(self) -> None:
        fx = self.fx
        fx.stage(INDEL_WARNING)
        fx.lock.write_text(fx.lock.read_text().replace(ENGINE_COMMIT, "f" * 40))
        self.assertRefusedUnchanged("runtime", "f" * 40)

    def test_an_unfinished_completion_is_not_discarded(self) -> None:
        """A forced run's preflight would delete it; this route refuses before writing anything."""
        fx = self.fx
        fx.stage(INDEL_WARNING)
        checkpoint = paths.completion_checkpoint_path(STORE_ID, root=fx.root)
        checkpoint.mkdir()
        (checkpoint / "state.json").write_text("{}")
        self.assertRefusedUnchanged(str(checkpoint), "unfinished completion")
        self.assertTrue((checkpoint / "state.json").is_file())

    def test_an_unfinished_completion_start_record_is_not_discarded(self) -> None:
        fx = self.fx
        fx.stage(INDEL_WARNING)
        started = paths.completion_start_path(STORE_ID, root=fx.root)
        started.parent.mkdir(parents=True, exist_ok=True)
        started.write_text("{}")
        self.assertRefusedUnchanged(str(started), "unfinished completion")

    def test_a_refusal_before_validation_keeps_no_verdict(self) -> None:
        """Only a validation this call ran is kept: the published release's own record is not one."""
        fx = self.fx
        fx.stage(INDEL_WARNING)
        with patch.object(run, "execute_step", side_effect=RewritePublicationRefused("stopped before validate")):
            with self.assertRaises(RewritePublicationRefused):
                fx.publish()
        self.assertEqual(list(paths.work_dir(STORE_ID, root=fx.root).glob("publish-rewrite-refused-*")), [])
        self.assertTrue(fx.staged.is_dir())

    def test_another_runs_pending_publication_is_left_to_the_entry_point(self) -> None:
        """A register job's marker is finished by `pixi run release`, which also clears Snakemake's marks."""
        fx = self.fx
        fx.stage(INDEL_WARNING)
        marker = paths.publication_marker(STORE_ID, root=fx.root)
        run.write_record_atomically({
            "store_id": STORE_ID, "archive": None, "snapshot": None, "replaced_store": None,
            "bundle_validation": str(fx.validation_yaml), "previous_validation": None,
            "validation_yaml": "status: passed\n", "register_record": {"step": "register"},
            "written_at": "2026-11-25T00:00:00Z",
        }, marker)
        before = fx.snapshot()
        with self.assertRaises(RewritePublicationRefused) as caught:
            fx.publish()
        self.assertIn(str(marker), str(caught.exception))
        self.assertIn(f"pixi run release {STORE_ID}", str(caught.exception))
        self.assertEqual(fx.snapshot(), before, "the marker, the Store and the records are as they were")

    def test_a_refused_validation_keeps_its_verdict(self) -> None:
        """Validation of a large Store takes hours: its record survives the refusal, outside records/."""
        fx = self.fx
        fx.stage(UNRECORDED_WARNING)
        with self.assertRaises(RewritePublicationRefused) as caught:
            fx.publish()
        (kept,) = paths.work_dir(STORE_ID, root=fx.root).glob("publish-rewrite-refused-*-validate.json")
        self.assertIn(str(kept), str(caught.exception))
        self.assertIn("eaf_orientation.unverified", json.loads(kept.read_text())["stdout"])


_REAL_COPYFILE = shutil.copyfile


def failing_kept_copy(error: BaseException) -> Any:
    """`shutil.copyfile` that raises `error` only when keeping a refused verdict in `work/`."""

    def copyfile(src: Any, dst: Any, *args: Any, **kwargs: Any) -> Any:
        if Path(dst).name.startswith("publish-rewrite-refused-"):
            raise error
        return _REAL_COPYFILE(src, dst, *args, **kwargs)

    return copyfile


class TestRollback(PublishRewriteCase):
    """Until `publication.json` exists every failure is undone; from then on the marker recovers it."""

    def assertRolledBack(self, before: dict[str, bytes]) -> None:
        fx = self.fx
        self.assertEqual(fx.snapshot(), before, "records/, the Store and validation.yaml are as they were")
        self.assertTrue(fx.staged.is_dir(), "the staged release is back at its path")
        self.assertFalse(paths.partial_store_path(STORE_ID, root=fx.root).exists())
        self.assertFalse(paths.publication_marker(STORE_ID, root=fx.root).exists())
        self.assertEqual(run.pending_force_snapshots(STORE_ID, fx.root), [])

    def test_a_failure_just_before_the_marker_is_rolled_back(self) -> None:
        fx = self.fx
        fx.stage(INDEL_WARNING)
        before = fx.snapshot()
        with patch.object(publish_rewrite.register, "begin_publication", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                fx.publish()
        self.assertRolledBack(before)

    def test_a_failure_assembling_the_record_is_rolled_back(self) -> None:
        fx = self.fx
        fx.stage(INDEL_WARNING)
        before = fx.snapshot()
        with patch.object(publish_rewrite, "store_size_on_disk", side_effect=OSError("stat failed")):
            with self.assertRaisesRegex(OSError, "stat failed"):
                fx.publish()
        self.assertRolledBack(before)

    def test_sigterm_before_the_marker_is_rolled_back(self) -> None:
        fx = self.fx
        fx.stage(INDEL_WARNING)
        before = fx.snapshot()
        with patch.object(publish_rewrite, "_judge_validation", side_effect=KeyboardInterrupt("signal 15")):
            with self.assertRaises(KeyboardInterrupt):
                fx.publish()
        self.assertRolledBack(before)

    def test_a_verdict_that_cannot_be_kept_does_not_stop_the_rollback(self) -> None:
        fx = self.fx
        fx.stage(UNRECORDED_WARNING)
        before = fx.snapshot()
        with patch.object(publish_rewrite.shutil, "copyfile", side_effect=failing_kept_copy(OSError("read-only"))):
            with self.assertRaises(RewritePublicationRefused) as caught:
                fx.publish()
        self.assertIn("could not be kept", str(caught.exception))
        self.assertRolledBack(before)

    def test_a_validate_record_of_bad_bytes_does_not_stop_the_rollback(self) -> None:
        """The refused verdict is evidence, kept byte for byte; bytes no codec reads still roll back."""
        fx = self.fx
        fx.stage(INDEL_WARNING)
        before = fx.snapshot()
        validate_p = paths.record_path(STORE_ID, "validate", root=fx.root)

        def bad_bytes(*_args: Any, **_kwargs: Any) -> None:
            validate_p.write_bytes(b"\xff\xfe not a record")

        with patch.object(run, "execute_step", side_effect=bad_bytes):
            with self.assertRaises(RewritePublicationRefused) as caught:
                fx.publish()
        self.assertIn("left no record", str(caught.exception))
        self.assertRolledBack(before)
        (kept,) = paths.work_dir(STORE_ID, root=fx.root).glob("publish-rewrite-refused-*-validate.json")
        self.assertEqual(kept.read_bytes(), b"\xff\xfe not a record")

    def test_any_failure_keeping_the_verdict_does_not_stop_the_rollback(self) -> None:
        fx = self.fx
        fx.stage(UNRECORDED_WARNING)
        before = fx.snapshot()
        with patch.object(publish_rewrite.shutil, "copyfile", side_effect=failing_kept_copy(ValueError("odd"))):
            with self.assertRaises(RewritePublicationRefused) as caught:
                fx.publish()
        self.assertIn("could not be kept", str(caught.exception))
        self.assertRolledBack(before)

    def test_a_failed_records_snapshot_leaves_no_partial_copy(self) -> None:
        fx = self.fx
        fx.stage(INDEL_WARNING)
        before = fx.snapshot()
        real_copytree = run.shutil.copytree

        def half_copied(src: Any, dst: Any, **kwargs: Any) -> None:
            real_copytree(src, dst, **kwargs)
            raise OSError("no space left on device")

        with patch.object(run.shutil, "copytree", side_effect=half_copied):
            with self.assertRaisesRegex(OSError, "no space left"):
                fx.publish()
        self.assertRolledBack(before)
        self.assertEqual(list(fx.store_dir.glob(f".{paths.FORCE_SNAPSHOT_PREFIX}*")), [], "no .copying left")

    def test_a_failure_after_the_marker_is_left_to_the_marker(self) -> None:
        fx = self.fx
        fx.stage(INDEL_WARNING)
        with patch.object(publish_rewrite.register, "complete_publication", side_effect=OSError("crash")):
            with self.assertRaisesRegex(OSError, "crash"):
                fx.publish()
        self.assertTrue(paths.publication_marker(STORE_ID, root=fx.root).exists(), "the marker is kept")
        self.assertTrue(paths.partial_store_path(STORE_ID, root=fx.root).is_dir(), "nothing was rolled back")
        self.assertFalse(fx.staged.exists())

        self.assertIsNone(fx.publish(), "the next call finishes it from the marker")
        self.assertFalse(paths.publication_marker(STORE_ID, root=fx.root).exists())
        self.assertEqual((fx.store / "data").read_text(), "new release, rewritten")

    def test_sigterm_to_a_library_call_rolls_back(self) -> None:
        """`staged_publication.publish` owns the handler, so `publish_rewrite()` called as a library unwinds too (#520)."""
        fx = self.fx
        fx.stage(INDEL_WARNING)
        before = fx.snapshot()
        real = run.fault_boundary

        def term(boundary: str) -> None:
            if boundary == "rewrite-snapshot-taken":
                os.kill(os.getpid(), signal.SIGTERM)
            real(boundary)

        with patch.object(run, "fault_boundary", side_effect=term):
            with self.assertRaises(staged_publication.Interrupted) as caught:
                fx.publish()
        self.assertEqual(caught.exception.state, staged_publication.ROLLED_BACK)
        self.assertEqual(fx.snapshot(), before, "records/, the Store and the staged release are as they were")
        self.assertTrue(fx.staged.is_dir())
        self.assertEqual(run.pending_force_snapshots(STORE_ID, fx.root), [])

    def test_the_two_routes_refuse_a_leftover_in_one_wording(self) -> None:
        fx = self.fx
        fx.stage(INDEL_WARNING)
        partial = paths.partial_store_path(STORE_ID, root=fx.root)
        partial.mkdir()
        with self.assertRaises(RewritePublicationRefused) as caught:
            fx.publish()
        message = str(caught.exception)
        self.assertIn(f"{partial} exists: a staged copy no run is publishing", message)
        self.assertIn("Nothing was written.", message)

    def test_a_kill_after_the_snapshot_names_the_snapshot_remedy(self) -> None:
        fx = self.fx
        fx.stage(INDEL_WARNING)
        self.assertEqual(fx.kill_at("rewrite-snapshot-taken").returncode, 137)
        (snapshot,) = run.pending_force_snapshots(STORE_ID, fx.root)
        with self.assertRaises(RewritePublicationRefused) as caught:
            fx.publish()
        message = str(caught.exception)
        self.assertIn(str(snapshot), message)
        self.assertIn(f"pixi run release {STORE_ID} --resolve-snapshot restore", message)
        self.assertNotIn("move it back", message, "the staged release never moved")

    def test_a_kill_after_staging_names_the_snapshot_and_the_staged_release(self) -> None:
        """The `.partial` leftover and the missing staged path must not hide the snapshot's remedy."""
        fx = self.fx
        fx.stage(INDEL_WARNING)
        self.assertEqual(fx.kill_at("rewrite-staged").returncode, 137)
        self.assertFalse(fx.staged.exists())
        (snapshot,) = run.pending_force_snapshots(STORE_ID, fx.root)
        with self.assertRaises(RewritePublicationRefused) as caught:
            fx.publish()
        message = str(caught.exception)
        self.assertIn(str(snapshot), message)
        self.assertIn("--resolve-snapshot restore", message)
        self.assertIn(f"{paths.partial_store_path(STORE_ID, root=fx.root)}: move it back to {fx.staged}", message)


class TestRepeatAndRecovery(PublishRewriteCase):
    """Criteria 3 and 4."""

    def test_a_second_call_after_success_does_nothing(self) -> None:
        fx = self.fx
        fx.stage(INDEL_WARNING)
        fx.publish()
        before = fx.snapshot()
        mtimes = {p: p.stat().st_mtime_ns for p in fx.store_dir.rglob("*")}

        self.assertIsNone(fx.publish())

        self.assertEqual(fx.snapshot(), before)
        self.assertEqual({p: p.stat().st_mtime_ns for p in fx.store_dir.rglob("*")}, mtimes)

    def test_an_interrupted_publication_recovers_through_the_marker(self) -> None:
        fx = self.fx
        fx.stage(INDEL_WARNING)
        # Killed as SIGKILL would leave it, once the staged release is in place
        # and before the old one is archived (`run.fault_boundary`).
        killed = fx.kill_at("store-published")
        self.assertEqual(killed.returncode, 137, killed.stderr)
        self.assertTrue(paths.publication_marker(STORE_ID, root=fx.root).exists())
        self.assertTrue(paths.backup_store_path(STORE_ID, root=fx.root).exists())

        self.assertIsNone(fx.publish(), "recovery finishes the publication; nothing is left to publish")

        self.assertFalse(paths.publication_marker(STORE_ID, root=fx.root).exists())
        self.assertFalse(paths.backup_store_path(STORE_ID, root=fx.root).exists())
        self.assertEqual((fx.store / "data").read_text(), "new release, rewritten")
        (archive,) = (fx.store_dir / "replaced").iterdir()
        self.assertEqual((archive / "store.opengwasdb" / "data").read_text(), "old release")
        self.assertEqual(yaml.safe_load(fx.validation_yaml.read_text())["rewrite"]["release_id"], NEW_RELEASE)


FIXTURE_TARBALL = Path(__file__).resolve().parent / "fixtures" / "dense-0.2.0.tar.gz"
GNU_TIME = Path("/usr/bin/time")


@unittest.skipUnless(GNU_TIME.is_file(), "needs GNU time at /usr/bin/time, as the operator's evidence does")
class TestRealEngine(unittest.TestCase):
    """The route end to end with the pinned engine: a real 0.2.0 Dense release, converted by the
    real `rewrite-release --convert-legacy` under `/usr/bin/time -v`, published with the real
    `info` and `validate` and no gate relaxed (`fixtures/README.md`)."""

    def test_a_real_conversion_publishes_with_its_recorded_deviation(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp_name:
            tmp = Path(tmp_name)
            registry, root = tmp / "stores", tmp / "artifacts"
            bundle_dir = registry / STORE_ID
            bundle_dir.mkdir(parents=True)
            (bundle_dir / "release.yaml").write_text(yaml.safe_dump({
                "store_id": STORE_ID, "label": "real", "status": "validated", "source_collection_id": "t",
                "association_coverage": "gwas", "derived_from": None, "created_at": "2026-11-25T00:00:00Z",
                "description": "real engine", "source_snapshot_id": "s", "release_kind": "pilot",
                "generator": {"command": "g"},
            }))
            (bundle_dir / "build.yaml").write_text(yaml.safe_dump({
                "store_id": STORE_ID, "layout": "dense", "completion_state": "observed_only",
                "build": {"command": "build-dense-vcf", "options": {"source-assembly": "GRCh38"}},
            }))
            (bundle_dir / "analyses.tsv").write_text("analysis_id\tsource_file\na\t/a\nb\t/b\n")
            validation_yaml = bundle_dir / "validation.yaml"
            validation_yaml.write_text(yaml.safe_dump({
                "status": "passed", "validator": {"name": "opengwasdb validate", "version": "x"},
                # The fixture tarball is an Observed-Only Store, as `info` reports it.
                "observed": {**PREVIOUS_OBSERVED, "completion_state": "observed_only"},
                "checks": {"store": "passed"}, "warnings": [], "errors": [],
            }, sort_keys=False))
            store_dir = paths.store_dir(STORE_ID, root=root)
            store_dir.mkdir(parents=True)
            with tarfile.open(FIXTURE_TARBALL) as tar:
                tar.extractall(store_dir, filter="data")
            records = paths.records_dir(STORE_ID, root=root)
            run.write_record_atomically({"step": "build"}, records / "build.json")
            run.write_record_atomically(
                {"step": "register", "inputs": [str(records / "build.json")]}, records / "register.json"
            )
            store, staged = paths.store_path(STORE_ID, root=root), store_dir / "store.opengwasdb.0.3.0"

            # The operator's run, as #525's queue script records it.
            exe = shutil.which("opengwasdb")
            assert exe is not None, "the dev environment puts the pinned engine on PATH"
            argv = [exe, "rewrite-release", str(store), str(staged), "--convert-legacy", "--lifted"]
            prefix = tmp / "logs" / STORE_ID
            prefix.parent.mkdir()
            prefix.with_suffix(".cmd").write_text(" ".join(argv) + " (budget 600s)\n")
            prefix.with_suffix(".start").write_text(run.utc_timestamp() + "\n")
            with open(prefix.with_suffix(".log"), "w") as log:
                code = subprocess.call(
                    [str(GNU_TIME), "-v", "-o", str(prefix.with_suffix(".time")), "timeout", "600", *argv],
                    stdout=log, stderr=subprocess.STDOUT,
                )
            prefix.with_suffix(".exit").write_text(f"{code}\n")
            prefix.with_suffix(".end").write_text(run.utc_timestamp() + "\n")
            self.assertEqual(code, 0, prefix.with_suffix(".log").read_text()[-2000:])

            record = publish(STORE_ID, staged, prefix, registry_root=registry, artifact_root=root)

            assert record is not None
            self.assertEqual(record["status"], "passed_with_warnings")
            self.assertEqual(record["rewrite"]["deviations"], ["variant_table.source_alid"])
            self.assertEqual(record["rewrite"]["source_format_version"], "0.2.0")
            self.assertEqual(record["observed"]["format_version"], "0.3.0")
            # The deviation, and `effect_consistency.unchecked` (opengwasdb#510): a conversion keeps a
            # pre-#510 release's effect-consistency columns absent, so the engine warns of every
            # one, and the route accepts it as a named legacy warning (`LEGACY_WARNINGS`).
            self.assertEqual(len(record["warnings"]), 2)
            self.assertEqual(record["rewrite"]["carried_warnings"], [])
            self.assertEqual(
                [(e["rule"], e["status"]) for e in record["rewrite"]["legacy_warnings"]],
                [("effect_consistency.unchecked", "legacy, accepted")],
            )
            # Read directly: each `opengwasdb info` costs about 2 s, and the route asked it already.
            manifest = json.loads((store / "manifest.json").read_text())
            self.assertEqual(manifest["release_id"], record["rewrite"]["release_id"])
            self.assertEqual(manifest["provenance"]["converted_from"]["deviations"],
                             ["variant_table.indel_order", "variant_table.source_alid"])
            (archive,) = (store_dir / "replaced").iterdir()
            archived = json.loads((archive / "store.opengwasdb" / "manifest.json").read_text())
            self.assertEqual(archived["format_version"], "0.2.0")
            rewrite = run.load_record(STORE_ID, "rewrite-release", root=root) or {}
            self.assertEqual(rewrite["argv"], argv)
            self.assertGreater(rewrite["peak_rss_kb"], 0)


class TestEvidence(unittest.TestCase):
    """The operator's evidence is read exactly, and refused when it disagrees with itself."""

    def test_the_command_must_be_the_one_timed(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            fx = Fixture(Path(tmp))
            fx.stage()
            time_p = fx.evidence.with_suffix(".time")
            time_p.write_text(time_p.read_text().replace("--n-workers 16", "--n-workers 8"))
            with self.assertRaisesRegex(RewritePublicationRefused, "Command being timed"):
                publish_rewrite.read_evidence(fx.evidence)


def amend_to_complete(fx: Fixture) -> None:
    """ADR 0037's one amendment: the accepted bundle gains a `complete` block."""
    build_yaml = fx.registry / STORE_ID / "build.yaml"
    document = yaml.safe_load(build_yaml.read_text())
    document["completion_state"] = "reference_completed"
    document["complete"] = {
        "command": "complete-dense",
        "options": {"ld-panel": str(fx.tmp / "ld-panel"), "ancestry": "EUR", "min-cor": 0.0},
    }
    build_yaml.write_text(yaml.safe_dump(document))


def record_build_as_run(fx: Fixture) -> None:
    """Make the published release's `build` record and manifest what a registry run of the bundle wrote."""
    loaded = bundle.load(STORE_ID, registry_root=fx.registry)
    build = next(s for s in plan.plan(loaded, artifact_root=fx.root) if s.name == "build")
    records = paths.records_dir(STORE_ID, root=fx.root)
    run.write_record_atomically(
        {
            "step": "build", "store_id": STORE_ID, "exit_code": 0, "success": True,
            "argv": build.argv, "planned_argv": build.argv,
            "record_path": str(records / "build.json"),
        },
        records / "build.json",
    )
    manifest_p = paths.build_manifest_path(STORE_ID, root=fx.root)
    manifest_p.parent.mkdir(parents=True, exist_ok=True)
    manifest.materialise_build_manifest(
        loaded.analyses_path, manifest_p, paths.build_manifest_sidecar_path(STORE_ID, root=fx.root)
    )


class OwnIdCompletionCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self._tmp.cleanup)
        self.fx = Fixture(Path(self._tmp.name))
        record_build_as_run(self.fx)

    def changes(self) -> list[str]:
        loaded = bundle.load(STORE_ID, registry_root=self.fx.registry)
        return amendment.bundle_changes(loaded, self.fx.root)

    def refuse(self) -> None:
        amendment.refuse_changed_bundles([STORE_ID], registry_root=self.fx.registry, artifact_root=self.fx.root)

    def convert(self, *findings: dict[str, str]) -> None:
        self.fx.stage(*(findings or (LIFTED_WARNING, SKIP)))
        self.assertIsNotNone(self.fx.publish())

    def rewrite_record_path(self) -> Path:
        return paths.record_path(STORE_ID, "rewrite-release", root=self.fx.root)


class TestConvertedParent(OwnIdCompletionCase):
    """The amendment check on a release `publish_rewrite` published (#527).

    These prove the check's acceptance and refusals; the scheduler's skip of
    the build phase is proved for a published Store in `test_release_faults`
    (`test_a_published_0_3_0_store_completes_with_no_build_and_no_force`).
    """

    def test_a_converted_release_with_no_indel_deviation_passes_the_amendment_check_unforced(self) -> None:
        self.convert()
        amend_to_complete(self.fx)

        self.assertEqual(self.changes(), [])
        self.refuse()

    def test_a_converted_release_recording_indel_order_is_refused_naming_the_remedy(self) -> None:
        self.convert(INDEL_WARNING, SKIP)
        amend_to_complete(self.fx)

        (change,) = self.changes()
        self.assertIn("variant_table.indel_order", change)
        self.assertIn("rebuild from source", change)
        with self.assertRaises(amendment.AmendmentError):
            self.refuse()

    def test_the_indel_order_refusal_ends_once_the_completion_phase_has_published(self) -> None:
        self.convert(INDEL_WARNING, SKIP)
        amend_to_complete(self.fx)
        completion = paths.record_path(STORE_ID, "completion-register", root=self.fx.root)
        run.write_record_atomically({"step": "completion-register", "inputs": []}, completion)

        self.assertNotIn("indel_order", "".join(self.changes()))

    def edit_register(self, edit) -> None:
        register_p = paths.record_path(STORE_ID, "register", root=self.fx.root)
        record = run.read_record(register_p) or {}
        edit(record)
        run.write_record_atomically(record, register_p)

    def test_a_null_rewrite_block_is_refused(self) -> None:
        self.convert()
        amend_to_complete(self.fx)
        self.edit_register(lambda r: r.update(rewrite=None))

        self.assertIn("`rewrite` block", "".join(self.changes()))

    def test_a_rewrite_block_without_its_record_is_refused(self) -> None:
        self.convert()
        amend_to_complete(self.fx)
        self.edit_register(lambda r: r["rewrite"].pop("record"))

        self.assertIn("rewrite.record", "".join(self.changes()))

    def test_a_rewrite_block_naming_another_stores_record_is_refused(self) -> None:
        self.convert()
        amend_to_complete(self.fx)
        self.edit_register(lambda r: r["rewrite"].update(record=str(self.fx.root / "OGS-00043/records/rewrite-release.json")))

        self.assertIn("rewrite.record", "".join(self.changes()))

    def test_an_input_naming_another_stores_rewrite_record_under_any_filename_is_refused(self) -> None:
        self.convert()
        amend_to_complete(self.fx)
        other = str(self.fx.root / "OGS-00043/records/rewrite-release.txt")
        self.edit_register(lambda r: r["inputs"].append(other))

        (change,) = self.changes()
        self.assertIn(other, change)

    def test_a_relocated_artifact_root_is_still_accepted(self) -> None:
        self.convert()
        amend_to_complete(self.fx)
        moved = str(self.fx.tmp / "elsewhere" / STORE_ID / "records" / "rewrite-release.json")

        def relocate(record: dict) -> None:
            record["rewrite"]["record"] = moved
            record["inputs"] = [moved if i.endswith("rewrite-release.json") else i for i in record["inputs"]]

        self.edit_register(relocate)
        self.assertEqual(self.changes(), [])

    def test_a_converted_release_is_still_held_to_the_build_it_converted(self) -> None:
        self.convert()
        amend_to_complete(self.fx)
        build_yaml = self.fx.registry / STORE_ID / "build.yaml"
        document = yaml.safe_load(build_yaml.read_text())
        document["build"]["options"]["source-assembly"] = "hg19"
        build_yaml.write_text(yaml.safe_dump(document))

        (change,) = self.changes()
        self.assertIn("build phase step 'build'", change)

    def test_a_converted_release_without_its_rewrite_record_is_refused(self) -> None:
        self.convert()
        amend_to_complete(self.fx)
        self.rewrite_record_path().unlink()

        (change,) = self.changes()
        self.assertIn("rewrite-release", change)
        self.assertIn("no record", change)
        with self.assertRaises(amendment.AmendmentError):
            self.refuse()

    def test_a_rewrite_record_naming_another_store_is_refused(self) -> None:
        self.convert()
        amend_to_complete(self.fx)
        record = run.read_record(self.rewrite_record_path()) or {}
        record["store_id"] = "OGS-00043"
        run.write_record_atomically(record, self.rewrite_record_path())

        (change,) = self.changes()
        self.assertIn("OGS-00043", change)
        with self.assertRaises(amendment.AmendmentError):
            self.refuse()

    def test_an_unreadable_rewrite_record_is_refused(self) -> None:
        self.convert()
        amend_to_complete(self.fx)
        self.rewrite_record_path().write_text("not json")

        (change,) = self.changes()
        self.assertIn("rewrite-release", change)

    def test_a_register_record_that_claims_a_rewrite_without_listing_it_is_refused(self) -> None:
        self.convert()
        amend_to_complete(self.fx)
        register_p = paths.record_path(STORE_ID, "register", root=self.fx.root)
        record = run.read_record(register_p) or {}
        record["inputs"] = [i for i in record["inputs"] if not i.endswith("rewrite-release.json")]
        run.write_record_atomically(record, register_p)

        (change,) = self.changes()
        self.assertIn("rewrite-release", change)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Tests for the one candidate emitter every Manifest Generator uses (#248).

The emitter is the only code that writes a candidate Release Bundle into the
Store Registry (ADR 0035's *Emit*). Tested at its two interfaces: `emit()`,
which the Python generators call, and its command line, which the R generators
call with a directory of rendered files.

Verifies:
- A new candidate lands in `stores/<id>/` with `status: candidate`, the
  generator commands it was given, its sidecars and a generated
  `summary.yaml`, and passes the Release Bundle contract.
- Re-emitting over an existing candidate replaces it whole, and the same
  input gives byte-identical files.
- Emitting over a bundle whose Release Status is past `candidate` is refused,
  and the registry is left byte-for-byte as it was.
- A candidate that fails the contract is refused, and a prior candidate
  survives untouched.
- Lineage resolves against the real registry, not the staging area.
- Nothing is left in the staging area after success or refusal.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

from ogstores import bundle
from manifest_generation import emit as emitter

STORE_ID = "OGS-00090"
COMMANDS = ("python3 fixtures/generate.py OGS-00090", "opengwasdb resolve-analyses fixture")

ANALYSES_TSV = (
    "analysis_id\tstored_effect_scale\tsample_size_kind\tsample_size_scope\tsample_size\t"
    "original_effect_scale\toriginal_sd_method\tassigned_ancestry\tancestry_assignment_method\t"
    "checksum\tchecksum_algorithm\tsource_file\n"
    "FIXTURE_1\tsd\ttotal\tanalysis_level\t1000\tsd\tdeclared_standardised\tEUR\taf_assigned\t"
    f"{'a' * 64}\tsha256\t/data/source/fixture.tsv\n"
)


#: What the fixture candidate emits to.
BUNDLE_FILES = ["analyses.tsv", "build.yaml", "release.yaml", "sidecars/selection.tsv", "summary.yaml", "validation.yaml"]


def candidate(store_id: str = STORE_ID, **changes) -> emitter.CandidateBundle:
    """A small candidate that passes the Release Bundle contract."""
    fields = {
        "store_id": store_id,
        "release": {
            "store_id": store_id,
            "label": "fixture",
            "status": "candidate",
            "derived_from": None,
            "created_at": "2026-10-14T00:00:00Z",
            "source_snapshot_id": "fixture-snapshot-v1",
            "generator": {"version": "sha256:" + "b" * 64},
            "description": "Fixture candidate Release Bundle",
        },
        "build": {
            "store_id": store_id,
            "layout": "dense",
            "completion_state": "observed_only",
            "build": {"command": "build-dense-vcf", "options": {}},
            "post": {"top_hits": False, "rho": False, "overview": True, "validate": True},
        },
        "analyses_tsv": ANALYSES_TSV,
        "validation": {"status": "passed", "checks": {"schema": "passed"}},
        "sidecars": {"selection.tsv": "analysis_id\tselected\nFIXTURE_1\ttrue\n"},
        "commands": COMMANDS,
    }
    fields.update(changes)
    return emitter.CandidateBundle(**fields)


def tree(root: Path) -> dict[str, bytes]:
    """Every file under `root`, by relative path."""
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


class EmitTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.registry = Path(self._tmp.name) / "stores"
        self.registry.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def release_yaml(self, store_id: str = STORE_ID) -> dict:
        return yaml.safe_load((self.registry / store_id / "release.yaml").read_text(encoding="utf-8"))

    def assert_nothing_staged(self) -> None:
        """No staged or stashed bundle is left; only the per-id lock files stay."""
        staging = self.registry / emitter.STAGING_DIRNAME
        left = sorted(p.name for p in staging.iterdir() if not p.name.endswith(".lock")) if staging.exists() else []
        self.assertEqual(left, [], "the staging area was left with a bundle in it")


class TestEmitNewCandidate(EmitTestCase):
    def test_a_new_candidate_lands_complete_with_status_and_commands(self) -> None:
        emitted = emitter.emit(candidate(), self.registry)

        self.assertEqual(emitted, self.registry / STORE_ID)
        self.assertEqual(
            sorted(tree(emitted)),
            BUNDLE_FILES,
        )
        release = self.release_yaml()
        self.assertEqual(release["status"], "candidate")
        self.assertEqual(release["generator"]["commands"], list(COMMANDS))
        self.assertEqual((emitted / "analyses.tsv").read_text(encoding="utf-8"), ANALYSES_TSV)
        loaded = bundle.load(STORE_ID, registry_root=self.registry)
        self.assertEqual(list(bundle.check(loaded, registry_root=self.registry)), [])
        self.assert_nothing_staged()


class TestLineage(EmitTestCase):
    def child(self, parent: str) -> emitter.CandidateBundle:
        base = candidate(store_id="OGS-00092")
        return candidate(
            store_id="OGS-00092",
            release={**base.release, "derived_from": parent},
            build={
                **base.build,
                "completion_state": "reference_completed",
                "complete": {"command": "complete-dense", "options": {"ld-panel": "/ld", "ancestry": "EUR"}},
            },
        )

    def test_a_child_resolves_its_parent_in_the_real_registry(self) -> None:
        emitter.emit(candidate(store_id="OGS-00091"), self.registry)

        emitter.emit(self.child("OGS-00091"), self.registry)

        self.assertEqual(self.release_yaml("OGS-00092")["derived_from"], "OGS-00091")

    def test_a_child_of_an_unregistered_parent_is_refused(self) -> None:
        with self.assertRaises(emitter.EmitError) as raised:
            emitter.emit(self.child("OGS-00093"), self.registry)

        self.assertIn("OGS-00093", str(raised.exception))
        self.assertFalse((self.registry / "OGS-00092").exists())


class TestInterruptedSwap(EmitTestCase):
    def test_a_candidate_stashed_by_an_interrupted_swap_is_put_back_first(self) -> None:
        """A swap killed between its two moves leaves `stores/<id>` missing and the old candidate stashed."""
        emitter.emit(candidate(), self.registry)
        stash = self.registry / emitter.STAGING_DIRNAME / f"{STORE_ID}.previous"
        os.replace(self.registry / STORE_ID, stash)
        stashed = tree(stash)

        with self.assertRaises(emitter.EmitError):
            emitter.emit(candidate(build={"store_id": STORE_ID, "layout": "dense"}), self.registry)

        self.assertEqual(tree(self.registry / STORE_ID), stashed)
        self.assert_nothing_staged()


class TestReEmit(EmitTestCase):
    def test_re_emitting_over_a_candidate_replaces_it_whole(self) -> None:
        emitter.emit(candidate(sidecars={"selection.tsv": "old\n", "dropped.tsv": "old\n"}), self.registry)
        revised = candidate(sidecars={"selection.tsv": "new\n"}, commands=("python3 fixtures/generate.py --again",))

        emitter.emit(revised, self.registry)

        self.assertEqual(
            sorted(tree(self.registry / STORE_ID)),
            BUNDLE_FILES,
        )
        self.assertEqual((self.registry / STORE_ID / "sidecars" / "selection.tsv").read_text(), "new\n")
        self.assertEqual(self.release_yaml()["generator"]["commands"], ["python3 fixtures/generate.py --again"])
        self.assert_nothing_staged()

    def test_a_filesystem_that_cannot_exchange_directories_still_replaces_whole(self) -> None:
        emitter.emit(candidate(sidecars={"dropped.tsv": "old\n"}), self.registry)

        with mock.patch.object(emitter, "_exchange", return_value=False):
            emitter.emit(candidate(), self.registry)

        self.assertEqual(
            sorted(tree(self.registry / STORE_ID)),
            BUNDLE_FILES,
        )
        self.assert_nothing_staged()

    def test_the_same_candidate_re_emits_byte_identically(self) -> None:
        emitter.emit(candidate(), self.registry)
        first = tree(self.registry)

        emitter.emit(candidate(), self.registry)

        self.assertEqual(tree(self.registry), first)


class TestEmitRefusals(EmitTestCase):
    def test_emitting_over_a_bundle_past_candidate_is_refused(self) -> None:
        for status in sorted(bundle.VALID_STATUSES - {"candidate"}):
            with self.subTest(status=status):
                emitter.emit(candidate(), self.registry)
                release_p = self.registry / STORE_ID / "release.yaml"
                release_p.write_text(release_p.read_text().replace("status: candidate", f"status: {status}"))
                before = tree(self.registry)

                with self.assertRaises(emitter.EmitError) as raised:
                    emitter.emit(candidate(), self.registry)

                self.assertIn(f"Release Status {status!r}", str(raised.exception))
                self.assertEqual(tree(self.registry), before)
                self.assert_nothing_staged()
                release_p.write_text(release_p.read_text().replace(f"status: {status}", "status: candidate"))

    def test_inputs_that_could_escape_the_bundle_or_drop_provenance_are_refused(self) -> None:
        cases = {
            "invalid store id": candidate(store_id="../OGS-00090"),
            "sidecar outside sidecars/": candidate(sidecars={"../release.yaml": "x"}),
            "nested sidecar name": candidate(sidecars={"deep/x.tsv": "x"}),
            "no generator commands": candidate(commands=()),
            "blank generator command": candidate(commands=("",)),
            "a renderer's other status": candidate(release={**candidate().release, "status": "accepted"}),
            "a renderer's other commands": candidate(
                release={**candidate().release, "generator": {"commands": ["something else"]}}
            ),
        }
        for name, bad in cases.items():
            with self.subTest(name):
                with self.assertRaises(emitter.EmitError):
                    emitter.emit(bad, self.registry)
                self.assertEqual(tree(self.registry), {})

    def test_a_concurrent_emit_of_the_same_id_is_refused(self) -> None:
        emitter.emit(candidate(), self.registry)
        before = tree(self.registry)
        lock_p = self.registry / emitter.STAGING_DIRNAME / f"{STORE_ID}.lock"
        fd = os.open(lock_p, os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(emitter.EmitError) as raised:
                emitter.emit(candidate(sidecars={"other.tsv": "x\n"}), self.registry)
        finally:
            os.close(fd)

        self.assertIn("in progress", str(raised.exception))
        self.assertEqual(tree(self.registry), before)

    def test_a_candidate_breaking_a_manifest_generation_rule_is_refused(self) -> None:
        """The emitter runs Manifest Generation's own rules as well as the contract (#250)."""
        names, values = (line.split("\t") for line in ANALYSES_TSV.strip().split("\n"))
        keep = [i for i, name in enumerate(names) if name != "assigned_ancestry"]
        without = "\t".join(names[i] for i in keep) + "\n" + "\t".join(values[i] for i in keep) + "\n"

        with self.assertRaises(emitter.EmitError) as raised:
            emitter.emit(candidate(analyses_tsv=without), self.registry)

        self.assertIn("assigned_ancestry", str(raised.exception))
        self.assertFalse((self.registry / STORE_ID).exists())
        self.assert_nothing_staged()

    def test_a_directory_that_is_not_a_bundle_is_never_replaced(self) -> None:
        stray = self.registry / STORE_ID
        stray.mkdir()
        (stray / "notes.txt").write_text("not a bundle\n")

        with self.assertRaises(emitter.EmitError):
            emitter.emit(candidate(), self.registry)

        self.assertEqual(tree(stray), {"notes.txt": b"not a bundle\n"})
        self.assert_nothing_staged()

    def test_a_candidate_failing_the_contract_is_refused_and_the_prior_one_kept(self) -> None:
        emitter.emit(candidate(), self.registry)
        before = tree(self.registry)
        broken = candidate(build={"store_id": STORE_ID, "layout": "dense"})

        with self.assertRaises(emitter.EmitError) as raised:
            emitter.emit(broken, self.registry)

        self.assertIn("completion_state", str(raised.exception))
        self.assertEqual(tree(self.registry), before)
        self.assert_nothing_staged()


class TestCommandLine(EmitTestCase):
    """The emitter's command line, which the R generators call with a directory of rendered files."""

    def write_rendered(self, directory: Path, rendered: emitter.CandidateBundle) -> Path:
        directory.mkdir(parents=True)
        # Another renderer's YAML style, keys in the same order: the emitter
        # rewrites the style in its own and keeps the order.
        def dump(document) -> str:
            return yaml.safe_dump(dict(document), sort_keys=False, indent=4, default_flow_style=True)

        (directory / "release.yaml").write_text(dump(rendered.release))
        (directory / "build.yaml").write_text(dump(rendered.build))
        (directory / "validation.yaml").write_text(dump(rendered.validation))
        (directory / "analyses.tsv").write_text(rendered.analyses_tsv)
        (directory / "sidecars").mkdir()
        for name, text in rendered.sidecars.items():
            (directory / "sidecars" / name).write_text(text)
        return directory

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "manifest_generation.emit", *args],
            capture_output=True,
            text=True,
        )

    def test_a_rendered_directory_emits_what_emit_would(self) -> None:
        rendered = self.write_rendered(Path(self._tmp.name) / "rendered", candidate())
        expected_root = Path(self._tmp.name) / "expected"
        expected_root.mkdir()
        emitter.emit(candidate(), expected_root)

        res = self.run_cli(
            STORE_ID, "--from", str(rendered), "--registry-root", str(self.registry),
            *(arg for command in COMMANDS for arg in ("--command", command)),
        )

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(tree(self.registry), tree(expected_root))

    def test_a_refusal_exits_non_zero_and_changes_nothing(self) -> None:
        emitter.emit(candidate(), self.registry)
        release_p = self.registry / STORE_ID / "release.yaml"
        release_p.write_text(release_p.read_text().replace("status: candidate", "status: accepted"))
        before = tree(self.registry)
        rendered = self.write_rendered(Path(self._tmp.name) / "rendered", candidate())

        res = self.run_cli(STORE_ID, "--from", str(rendered), "--registry-root", str(self.registry), "--command", "x")

        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Release Status 'accepted'", res.stderr)
        self.assertEqual(tree(self.registry), before)

    def test_rendered_entries_of_the_wrong_kind_are_refused(self) -> None:
        for name, make in (
            ("sidecars", lambda d: (shutil.rmtree(d / "sidecars"), (d / "sidecars").write_text("x"))),
            ("validation.yaml", lambda d: ((d / "validation.yaml").unlink(), (d / "validation.yaml").mkdir())),
        ):
            with self.subTest(name):
                rendered = self.write_rendered(Path(self._tmp.name) / f"rendered-{name}", candidate())
                make(rendered)

                res = self.run_cli(STORE_ID, "--from", str(rendered), "--registry-root", str(self.registry), "--command", "x")

                self.assertNotEqual(res.returncode, 0)
                self.assertIn(name, res.stderr)
                self.assertEqual(tree(self.registry), {})

    def test_an_unexpected_file_in_the_rendered_directory_is_refused(self) -> None:
        rendered = self.write_rendered(Path(self._tmp.name) / "rendered", candidate())
        (rendered / "filtered.tsv.gz").write_bytes(b"not part of a bundle")

        res = self.run_cli(STORE_ID, "--from", str(rendered), "--registry-root", str(self.registry), "--command", "x")

        self.assertNotEqual(res.returncode, 0)
        self.assertIn("filtered.tsv.gz", res.stderr)
        self.assertEqual(tree(self.registry), {})


class TestProjectWorkingBundle(EmitTestCase):
    """A legacy generator's working bundle, projected into the current contract (#248)."""

    def working_bundle(self) -> Path:
        working = Path(self._tmp.name) / "working"
        (working / "sidecars").mkdir(parents=True)
        (working / "analyses.tsv").write_text(ANALYSES_TSV)
        (working / "sidecars" / "selection.tsv").write_text("analysis_id\tselected\nFIXTURE_1\ttrue\n")
        (working / "validation.yaml").write_text(yaml.safe_dump({"status": "passed", "validated_at": None}))
        # The family-shaped files a legacy generator also keeps there are not
        # projected, but the command log its release.yaml records is carried.
        (working / "release.yaml").write_text(
            yaml.safe_dump({"store_family_id": "legacy", "generator": {"command": "Rscript legacy.R --mode=emit"}})
        )
        (working / "pilot_report.md").write_text("# report\n")
        return working

    def config(self, layout: str = "dense", **extra) -> dict:
        return {
            "family_release_id": "fixture-legacy-working",
            "label": "fixture-legacy",
            "access_posture": "public",
            "description": "A legacy generator's fixture candidate",
            "notes": "Fixture notes",
            "source": {"source_snapshot_id": "fixture-snapshot-v1"},
            "output": {"store_id": STORE_ID},
            "build": {
                "layout": layout,
                "completion_state": "observed_only",
                "command": "build-dense-vcf" if layout == "dense" else "build-ragged-ssf",
                "options": {"source-assembly": "hg38"},
                "post": {"top_hits": True, "overview": layout != "ragged"},
            },
            **extra,
        }

    def test_a_working_bundle_projects_to_a_current_candidate(self) -> None:
        projected = emitter.project_working_bundle(
            self.config(),
            self.working_bundle(),
            generator_version="sha256:" + "b" * 64,
            commands=COMMANDS,
            created_at="2026-10-14T00:00:00Z",
            source_snapshot={"manifest_sha256": "c" * 64},
        )

        self.assertEqual(projected.store_id, STORE_ID)
        self.assertEqual(projected.release["label"], "fixture-legacy")
        self.assertEqual(projected.release["source_snapshot_id"], "fixture-snapshot-v1")
        self.assertEqual(projected.release["source_snapshot"], {"manifest_sha256": "c" * 64})
        self.assertEqual(projected.build["build"], {"command": "build-dense-vcf", "options": {"source-assembly": "hg38"}})
        self.assertEqual(projected.build["indexed_subsets"][0]["name"], "hm3")
        self.assertEqual(projected.analyses_tsv, ANALYSES_TSV)
        self.assertEqual(dict(projected.sidecars), {"selection.tsv": "analysis_id\tselected\nFIXTURE_1\ttrue\n"})
        self.assertEqual(projected.validation["validated_at"], "2026-10-14T00:00:00Z")
        self.assertEqual(tuple(projected.commands), ("Rscript legacy.R --mode=emit", *COMMANDS))
        emitter.emit(projected, self.registry)
        self.assertEqual(self.release_yaml()["status"], "candidate")

    def test_a_reviewed_opt_out_declares_no_subset_and_records_why(self) -> None:
        """An explicit empty list plus a written reason omits the default hm3 subset (#210)."""
        config = self.config(indexed_subsets=[], indexed_subsets_opt_out="fixture predates the maintained list")

        projected = emitter.project_working_bundle(config, self.working_bundle(), generator_version="v", commands=COMMANDS)

        self.assertEqual(projected.build["indexed_subsets"], [])
        self.assertIn("intentionally omitted: fixture predates the maintained list", projected.release["notes"])

    def test_a_ragged_projection_declares_no_indexed_subset(self) -> None:
        projected = emitter.project_working_bundle(
            self.config(layout="ragged"), self.working_bundle(), generator_version="v", commands=COMMANDS
        )

        self.assertNotIn("indexed_subsets", projected.build)

    def test_a_config_missing_what_a_bundle_states_cannot_be_projected(self) -> None:
        """Nothing a bundle states is guessed: no default label, access posture or recipe."""
        cases = {
            "no build block": lambda c: c.pop("build"),
            "no layout": lambda c: c["build"].pop("layout"),
            "no post": lambda c: c["build"].pop("post"),
            "no label": lambda c: c.pop("label"),
            "no access posture": lambda c: c.pop("access_posture"),
        }
        working = self.working_bundle()
        for name, remove in cases.items():
            with self.subTest(name):
                config = self.config()
                remove(config)
                with self.assertRaises(emitter.EmitError):
                    emitter.project_working_bundle(config, working, generator_version="v", commands=COMMANDS)

    def test_re_emitting_a_working_bundle_at_a_pinned_time_is_byte_identical(self) -> None:
        artifacts = Path(self._tmp.name) / "artifacts"
        work = artifacts / STORE_ID / "generate"
        shutil.copytree(self.working_bundle(), work)
        config = {**self.config(), "output": {"store_id": STORE_ID, "registry_root": str(self.registry)}}

        with mock.patch.dict(os.environ, {"OPENGWASDB_ARTIFACT_ROOT": str(artifacts)}):
            emitter.emit_working_bundle(config, generator_version="v", commands=COMMANDS, created_at="2026-10-14T00:00:00Z")
            first = tree(self.registry / STORE_ID)
            emitter.emit_working_bundle(config, generator_version="v", commands=COMMANDS, created_at="2026-10-14T00:00:00Z")

        self.assertEqual(tree(self.registry / STORE_ID), first)


class TestGeneratorConfigs(unittest.TestCase):
    """Every generator emits into stores/, with its working files under the Artifact Root (#248)."""

    #: The one config with its own work root outside the Artifact Root. The QC-panel
    #: concordance study's preregistration and `run_concordance_study.py` bind
    #: this default, so it moves only with that study.
    OWN_WORK_ROOT = {"gwas-catalog-eur-hybrid/config-full.yaml"}

    def test_no_generator_config_writes_or_works_under_families(self) -> None:
        configs = sorted((REPO_ROOT / "resources" / "generators").glob("*/config*.yaml"))
        self.assertTrue(configs)
        for path in configs:
            name = str(path.relative_to(REPO_ROOT / "resources" / "generators"))
            with self.subTest(config=name):
                document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                output = document.get("output") or {}
                self.assertNotIn("release_dir", output)
                for key, value in output.items():
                    self.assertFalse(str(value).startswith("families/"), f"output.{key} names {value}")
                self.assertEqual("work_root" in output, name in self.OWN_WORK_ROOT)
                if "build" in document and name not in self.OWN_WORK_ROOT:
                    self.assertTrue(
                        str(output.get("store_id", "")).startswith("OGS-"),
                        "a legacy generator config names the Store Release id it emits",
                    )


class TestWorkRoot(unittest.TestCase):
    """`python3 -m manifest_generation.emit work-root <id>`: where a generator keeps its working files (#248)."""

    def run_work_root(self, *args: str, env_root: str | None = None) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if k != "OPENGWASDB_ARTIFACT_ROOT"}
        if env_root is not None:
            env["OPENGWASDB_ARTIFACT_ROOT"] = env_root
        return subprocess.run(
            [sys.executable, "-m", "manifest_generation.emit", "work-root", *args],
            capture_output=True,
            text=True,
            env=env,
        )

    def test_the_work_root_follows_the_artifact_root_precedence(self) -> None:
        from_env = self.run_work_root(STORE_ID, env_root="/env/artifacts")
        self.assertEqual(from_env.returncode, 0, from_env.stderr)
        self.assertEqual(from_env.stdout.strip(), f"/env/artifacts/{STORE_ID}/generate")

        overridden = self.run_work_root(STORE_ID, "--artifact-root", "/given/artifacts", env_root="/env/artifacts")
        self.assertEqual(overridden.returncode, 0, overridden.stderr)
        self.assertEqual(overridden.stdout.strip(), f"/given/artifacts/{STORE_ID}/generate")

    def test_an_invalid_store_id_has_no_work_root(self) -> None:
        res = self.run_work_root("families/x", env_root="/env/artifacts")

        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(res.stdout, "")


if __name__ == "__main__":
    unittest.main()

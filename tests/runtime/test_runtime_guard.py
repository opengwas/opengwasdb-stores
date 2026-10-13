#!/usr/bin/env python3
"""Pre-write runtime guard for the supported release entry point (#218).

Covers the contract `ogstores.runtime.verify_runtime_for_releases` adds to
`pixi run release` (and, as defence in depth, the Snakefile `onstart`):

* the tracked pin is explicit and the same revision in `pixi.toml`,
  `pixi.lock`, and `ogstores.runtime.PINNED_OPENGWASDB_REVISION`, and it fails
  closed on an unknown or non-exact revision;
* the version and revision come from the executable that will run, not from
  the ambient interpreter;
* only the subcommands the *scheduled steps* need are probed, each once per
  run: `onstart` does not re-probe what the entry point handed off for the
  same executable at the same revision;
* the completion-parent gate is precise about scheduled work: a same-run
  `build`/`complete` of the parent skips the stale on-disk artifact, a missing
  parent that is not scheduled is refused, and an enhancement-only job (HM3,
  validate, overview) never consults its unused completion recipe.

Fixtures are a shell `opengwasdb` stub, a `direct_url.json` and a dist-info
`METADATA`, so no real `opengwasdb` install or Store is opened.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import tomllib
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ogstores import runtime  # noqa: E402

n_checks = 0


def record_check() -> None:
    global n_checks
    n_checks += 1


def _pin_from_pixi_toml(toml_path: Path) -> str:
    document = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    pypi = document["feature"]["store-build"]["pypi-dependencies"]
    spec = pypi["opengwasdb"]
    return spec["rev"]


class _FakeRuntime:
    """A shell `opengwasdb` stub plus the metadata a real install carries."""

    def __init__(
        self,
        root: Path,
        *,
        commands: set[str],
        commit: str | None,
        version: str = "0.3.0",
        format_version: str = "0.2.0",
        probe_log: Path | None = None,
    ):
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir(parents=True)
        self.executable = self.bin / "opengwasdb"
        supported = "|".join(sorted(commands))
        # Each call appends its subcommand, so a test can count the probes.
        log_handler = f'echo "$1" >> "{probe_log}"\n' if probe_log is not None else ""
        info_handler = (
            'if [ "$1" = "info" ]; then\n'
            f'  printf \'{{"format_version": "%s"}}\\n\' "{format_version}"\n'
            "  exit 0\n"
            "fi\n"
            if "info" in commands
            else ""
        )
        self.executable.write_text(
            "#!/bin/sh\n"
            f"{log_handler}"
            f"{info_handler}"
            f'case "$1" in {supported}) exit 0 ;; *) exit 2 ;; esac\n',
            encoding="utf-8",
        )
        self.executable.chmod(0o755)
        if commit is not None:
            dist = root / "lib" / "python3.12" / "site-packages" / "opengwasdb-0.3.0.dist-info"
            dist.mkdir(parents=True)
            (dist / "direct_url.json").write_text(
                json.dumps(
                    {
                        "url": runtime.OPENGWASDB_GIT_URL,
                        "vcs_info": {"vcs": "git", "commit_id": commit},
                    }
                ),
                encoding="utf-8",
            )
            (dist / "METADATA").write_text(
                f"Metadata-Version: 2.1\nName: opengwasdb\nVersion: {version}\n",
                encoding="utf-8",
            )


class RuntimeGuardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ogstores_runtime_"))
        self.registry = self.tmp / "stores"
        self.artifact_root = self.tmp / "artifacts"
        self.registry.mkdir()
        self.artifact_root.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- fixtures ---------------------------------------------------------

    def _write_bundle(
        self,
        store_id: str,
        *,
        completion: bool = False,
        derived_from: str | None = None,
        indexed_subset: bool = False,
    ) -> None:
        bundle_root = self.registry / store_id
        bundle_root.mkdir(parents=True, exist_ok=True)
        release: dict = {
            "store_id": store_id,
            "label": store_id,
            "status": "candidate",
            "created_at": "2026-01-01T00:00:00Z",
            "derived_from": derived_from,
            "description": "fixture",
            "generator": {"commands": ["fixture"]},
        }
        build: dict = {
            "store_id": store_id,
            "layout": "dense",
            "completion_state": "reference_completed" if completion else "observed_only",
            "post": {"top_hits": not completion, "rho": False, "overview": True},
        }
        if completion:
            build["complete"] = {
                "command": "complete-dense",
                "options": {"ld-panel": "/p", "ancestry": "EUR"},
            }
        else:
            build["build"] = {"command": "build-dense-vcf", "options": {}}
        if indexed_subset:
            build["indexed_subsets"] = [
                {
                    "name": "hm3",
                    "resource_id": "hapmap3-hg38",
                    "variant_list": str(self.tmp / "hm3.alid.txt"),
                    "reference_assembly": "GRCh38",
                }
            ]
        (bundle_root / "release.yaml").write_text(yaml.safe_dump(release), encoding="utf-8")
        (bundle_root / "build.yaml").write_text(yaml.safe_dump(build), encoding="utf-8")

    def _runnable(self, name: str, **kwargs) -> _FakeRuntime:
        kwargs.setdefault("commit", runtime.PINNED_OPENGWASDB_REVISION)
        return _FakeRuntime(self.tmp / name, **kwargs)

    def _job(self, store_id: str, step: str | None) -> tuple[str, str, str | None]:
        return (str(self.artifact_root), store_id, step)

    # --- pin & identity ---------------------------------------------------

    def test_pin_is_explicit_and_consistent(self) -> None:
        """The tracked pin is one immutable revision across toml, lock and module."""
        toml_pin = _pin_from_pixi_toml(REPO_ROOT / "pixi.toml")
        record_check()
        self.assertEqual(toml_pin, runtime.PINNED_OPENGWASDB_REVISION)
        self.assertEqual(len(toml_pin), 40)
        lock_text = (REPO_ROOT / "pixi.lock").read_text(encoding="utf-8")
        record_check()
        self.assertIn(f"rev={runtime.PINNED_OPENGWASDB_REVISION}", lock_text)

    def test_missing_executable_is_refused(self) -> None:
        self._write_bundle("OGS-00090")
        with self.assertRaises(runtime.RuntimeCapabilityError) as ctx:
            runtime.verify_runtime_for_releases(
                [self._job("OGS-00090", "build")],
                registry_root=self.registry,
                executable=runtime.run.UNAVAILABLE,
            )
        record_check()
        self.assertIn("no opengwasdb executable", str(ctx.exception))

    def test_unprovable_revision_is_refused(self) -> None:
        """An install whose identity cannot be proven is not the pin: fail closed."""
        self._write_bundle("OGS-00090")
        anonymous = self._runnable(
            "anonymous",
            commands={"build-dense-vcf"},
            commit=None,
        )
        with self.assertRaises(runtime.RuntimeRevisionError) as ctx:
            runtime.verify_runtime_for_releases(
                [self._job("OGS-00090", "build")],
                registry_root=self.registry,
                executable=str(anonymous.executable),
            )
        record_check()
        self.assertIn("not the tracked pin", str(ctx.exception))

    def test_known_other_revision_is_refused(self) -> None:
        self._write_bundle("OGS-00090")
        other = self._runnable("other", commands={"build-dense-vcf"}, commit="deadbeef" * 5)
        with self.assertRaises(runtime.RuntimeRevisionError) as ctx:
            runtime.verify_runtime_for_releases(
                [self._job("OGS-00090", "build")],
                registry_root=self.registry,
                executable=str(other.executable),
            )
        record_check()
        self.assertIn(runtime.PINNED_OPENGWASDB_REVISION, str(ctx.exception))

    def test_version_comes_from_the_executable_not_the_ambient_interpreter(self) -> None:
        self._write_bundle("OGS-00090")
        versioned = self._runnable(
            "versioned",
            commands={"build-dense-vcf"},
            version="9.9.9-from-executable",
        )
        identity = runtime.verify_runtime_for_releases(
            [self._job("OGS-00090", "build")],
            registry_root=self.registry,
            executable=str(versioned.executable),
        )
        record_check()
        self.assertEqual(identity.version, "9.9.9-from-executable")

    # --- scheduled capabilities ------------------------------------------

    def test_only_scheduled_step_commands_are_probed(self) -> None:
        """A run that schedules only validate must not need the build commands."""
        self._write_bundle("OGS-00090")
        only_validate = self._runnable("only-validate", commands={"validate"})
        identity = runtime.verify_runtime_for_releases(
            [self._job("OGS-00090", "validate")],
            registry_root=self.registry,
            executable=str(only_validate.executable),
        )
        record_check()
        self.assertEqual(identity.revision, runtime.PINNED_OPENGWASDB_REVISION)

    def test_missing_scheduled_capability_is_refused(self) -> None:
        self._write_bundle("OGS-00090")
        partial = self._runnable("partial", commands={"build-dense-vcf"})
        with self.assertRaises(runtime.RuntimeCapabilityError) as ctx:
            runtime.verify_runtime_for_releases(
                [
                    self._job("OGS-00090", "build"),
                    self._job("OGS-00090", "top-hits"),
                    self._job("OGS-00090", "overview"),
                    self._job("OGS-00090", "validate"),
                ],
                registry_root=self.registry,
                executable=str(partial.executable),
            )
        record_check()
        message = str(ctx.exception)
        self.assertIn("build-dense-top-hits", message)
        self.assertIn("regenerate-overview", message)
        self.assertIn("validate", message)

    def test_capable_pinned_runtime_reports_identity(self) -> None:
        self._write_bundle("OGS-00090")
        capable = self._runnable("capable", commands={"build-dense-vcf", "validate"})
        identity = runtime.verify_runtime_for_releases(
            [self._job("OGS-00090", "build"), self._job("OGS-00090", "validate")],
            registry_root=self.registry,
            executable=str(capable.executable),
        )
        record_check()
        self.assertEqual(identity.revision, runtime.PINNED_OPENGWASDB_REVISION)
        self.assertIn(runtime.PINNED_OPENGWASDB_REVISION, identity.describe())

    # --- one probe per command per run -----------------------------------

    def _probes(self, log: Path) -> list[str]:
        """The subcommands probed since the last call, sorted; the log is then cleared."""
        probed = sorted(log.read_text(encoding="utf-8").split()) if log.exists() else []
        log.unlink(missing_ok=True)
        return probed

    def test_each_needed_command_is_probed_once(self) -> None:
        """Two releases that need the same commands probe each of them once, not once per release."""
        self._write_bundle("OGS-00090")
        self._write_bundle("OGS-00091")
        log = self.tmp / "probes.log"
        capable = self._runnable("capable", commands={"build-dense-vcf", "validate"}, probe_log=log)
        identity = runtime.verify_runtime_for_releases(
            [self._job(sid, step) for sid in ("OGS-00090", "OGS-00091") for step in ("build", "validate")],
            registry_root=self.registry,
            executable=str(capable.executable),
        )
        record_check()
        self.assertEqual(self._probes(log), ["build-dense-vcf", "validate"])
        self.assertEqual(identity.commands, ("build-dense-vcf", "validate"))

    def test_a_hand_off_for_this_runtime_is_not_probed_again(self) -> None:
        """`onstart` re-probes nothing the entry point probed on the same runtime, only what it adds."""
        self._write_bundle("OGS-00090")
        log = self.tmp / "probes.log"
        capable = self._runnable(
            "capable", commands={"build-dense-vcf", "build-dense-top-hits", "validate"}, probe_log=log
        )
        jobs = [self._job("OGS-00090", "build"), self._job("OGS-00090", "validate")]
        entry_point = runtime.verify_runtime_for_releases(
            jobs, registry_root=self.registry, executable=str(capable.executable)
        )
        self._probes(log)

        onstart = runtime.verify_runtime_for_releases(
            jobs,
            registry_root=self.registry,
            executable=str(capable.executable),
            handoff=entry_point.handoff(),
        )
        record_check()
        self.assertEqual(self._probes(log), [])
        self.assertEqual(onstart.commands, entry_point.commands)

        runtime.verify_runtime_for_releases(
            [*jobs, self._job("OGS-00090", "top-hits")],
            registry_root=self.registry,
            executable=str(capable.executable),
            handoff=entry_point.handoff(),
        )
        record_check()
        self.assertEqual(self._probes(log), ["build-dense-top-hits"])

    def test_a_hand_off_for_another_runtime_verifies_nothing(self) -> None:
        """A hand-off naming another executable or revision, or not parsing, leaves every command probed."""
        self._write_bundle("OGS-00090")
        jobs = [self._job("OGS-00090", "build"), self._job("OGS-00090", "validate")]
        capable = self._runnable("capable", commands={"build-dense-vcf", "validate"})
        handoff = runtime.verify_runtime_for_releases(
            jobs, registry_root=self.registry, executable=str(capable.executable)
        ).handoff()

        partial = self._runnable("partial", commands={"build-dense-vcf"})
        with self.assertRaises(runtime.RuntimeCapabilityError) as ctx:
            runtime.verify_runtime_for_releases(
                jobs, registry_root=self.registry, executable=str(partial.executable), handoff=handoff
            )
        record_check()
        self.assertIn("validate", str(ctx.exception))

        log = self.tmp / "probes.log"
        logged = self._runnable("logged", commands={"build-dense-vcf", "validate"}, probe_log=log)
        other_revision = json.dumps({
            "executable": str(logged.executable),
            "revision": "deadbeef" * 5,
            "commands": ["build-dense-vcf", "validate"],
        })
        for stale in (other_revision, "not json", json.dumps(["a", "list"])):
            with self.subTest(handoff=stale):
                runtime.verify_runtime_for_releases(
                    jobs, registry_root=self.registry, executable=str(logged.executable), handoff=stale
                )
                record_check()
                self.assertEqual(self._probes(log), ["build-dense-vcf", "validate"])

    # --- completion-parent format gate -----------------------------------

    def _capable_for_completion(self, name: str, **kwargs) -> _FakeRuntime:
        return self._runnable(
            name,
            commands={
                "build-dense-vcf",
                "build-dense-top-hits",
                "complete-dense",
                "build-indexed-subset",
                "regenerate-overview",
                "validate",
                "info",
            },
            **kwargs,
        )

    def _capable_for_parent_build(self, name: str, **kwargs) -> _FakeRuntime:
        return self._capable_for_completion(name, **kwargs)

    def test_completion_parent_0_1_0_is_refused(self) -> None:
        self._write_bundle("OGS-00091", completion=True, derived_from="OGS-00090")
        self._write_bundle("OGS-00090")
        capable = self._capable_for_completion("capable", format_version="0.1.0")
        (self.artifact_root / "OGS-00090" / "store.opengwasdb").mkdir(parents=True)
        with self.assertRaises(runtime.CompletionParentFormatError) as ctx:
            runtime.verify_runtime_for_releases(
                [self._job("OGS-00091", "complete")],
                registry_root=self.registry,
                executable=str(capable.executable),
            )
        record_check()
        self.assertIn("0.1.0", str(ctx.exception))

    def test_completion_parent_0_2_0_passes(self) -> None:
        self._write_bundle("OGS-00091", completion=True, derived_from="OGS-00090")
        self._write_bundle("OGS-00090")
        capable = self._capable_for_completion("capable", format_version="0.2.0")
        (self.artifact_root / "OGS-00090" / "store.opengwasdb").mkdir(parents=True)
        identity = runtime.verify_runtime_for_releases(
            [self._job("OGS-00091", "complete")],
            registry_root=self.registry,
            executable=str(capable.executable),
        )
        record_check()
        self.assertEqual(identity.format_version, "0.2.0")

    def test_enhancement_only_child_is_not_gated(self) -> None:
        """Adding HM3 to an existing 0.1.0 completed Store does not run completion."""
        self._write_bundle("OGS-00091", completion=True, derived_from="OGS-00090", indexed_subset=True)
        capable = self._runnable("subset", commands={"build-indexed-subset"})
        (self.artifact_root / "OGS-00090" / "store.opengwasdb").mkdir(parents=True)
        runtime.verify_runtime_for_releases(
            [self._job("OGS-00091", "indexed-subset-hm3")],
            registry_root=self.registry,
            executable=str(capable.executable),
        )
        record_check()

    def test_same_run_parent_build_skips_the_stale_parent_gate(self) -> None:
        self._write_bundle("OGS-00091", completion=True, derived_from="OGS-00090")
        self._write_bundle("OGS-00090", indexed_subset=True)
        capable = self._capable_for_parent_build("parent-build", format_version="0.1.0")
        (self.artifact_root / "OGS-00090" / "store.opengwasdb").mkdir(parents=True)
        runtime.verify_runtime_for_releases(
            [
                self._job("OGS-00090", "build"),
                self._job("OGS-00091", "complete"),
            ],
            registry_root=self.registry,
            executable=str(capable.executable),
        )
        record_check()

    def test_same_run_parent_enhancement_only_still_gates_the_stale_parent(self) -> None:
        """An HM3/validate job on the parent is not a rebuild, so its 0.1.0 Store is consumed."""
        self._write_bundle("OGS-00091", completion=True, derived_from="OGS-00090")
        self._write_bundle("OGS-00090", indexed_subset=True)
        capable = self._capable_for_completion("capable", format_version="0.1.0")
        (self.artifact_root / "OGS-00090" / "store.opengwasdb").mkdir(parents=True)
        with self.assertRaises(runtime.CompletionParentFormatError):
            runtime.verify_runtime_for_releases(
                [
                    self._job("OGS-00090", "indexed-subset-hm3"),
                    self._job("OGS-00091", "complete"),
                ],
                registry_root=self.registry,
                executable=str(capable.executable),
            )
        record_check()

    def test_missing_parent_not_scheduled_is_refused(self) -> None:
        self._write_bundle("OGS-00091", completion=True, derived_from="OGS-00090")
        capable = self._capable_for_completion("capable")
        with self.assertRaises(runtime.CompletionParentFormatError) as ctx:
            runtime.verify_runtime_for_releases(
                [self._job("OGS-00091", "complete")],
                registry_root=self.registry,
                executable=str(capable.executable),
            )
        record_check()
        self.assertIn("no Store exists", str(ctx.exception))
        self.assertIn("scheduled in this run", str(ctx.exception))

    def test_missing_parent_scheduled_build_passes(self) -> None:
        self._write_bundle("OGS-00091", completion=True, derived_from="OGS-00090")
        self._write_bundle("OGS-00090", indexed_subset=True)
        capable = self._capable_for_parent_build("parent-build")
        runtime.verify_runtime_for_releases(
            [
                self._job("OGS-00090", "build"),
                self._job("OGS-00091", "complete"),
            ],
            registry_root=self.registry,
            executable=str(capable.executable),
        )
        record_check()

    def test_completion_missing_info_capability_is_refused(self) -> None:
        """`info` is required once a parent is inspected, so its absence is a capability refusal."""
        self._write_bundle("OGS-00091", completion=True, derived_from="OGS-00090")
        self._write_bundle("OGS-00090")
        no_info = self._runnable(
            "no-info",
            commands={"complete-dense", "build-indexed-subset", "regenerate-overview", "validate"},
        )
        (self.artifact_root / "OGS-00090" / "store.opengwasdb").mkdir(parents=True)
        with self.assertRaises(runtime.RuntimeCapabilityError) as ctx:
            runtime.verify_runtime_for_releases(
                [self._job("OGS-00091", "complete")],
                registry_root=self.registry,
                executable=str(no_info.executable),
            )
        record_check()
        self.assertIn("info", str(ctx.exception))

    def test_unreadable_parent_info_is_refused(self) -> None:
        """A runtime that cannot report the parent format refuses rather than guessing."""
        self._write_bundle("OGS-00091", completion=True, derived_from="OGS-00090")
        self._write_bundle("OGS-00090")
        # `info` exists (so the capability probe passes) but exits non-zero.
        stub = self.tmp / "bad-info"
        stub.mkdir(parents=True)
        exe = stub / "bin" / "opengwasdb"
        exe.parent.mkdir(parents=True)
        exe.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"info\" ]; then\n"
            "  if [ \"$2\" = \"--help\" ]; then exit 0; fi\n"
            "  exit 3\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        exe.chmod(0o755)
        dist = stub / "lib" / "python3.12" / "site-packages" / "opengwasdb-0.3.0.dist-info"
        dist.mkdir(parents=True)
        (dist / "direct_url.json").write_text(
            json.dumps(
                {
                    "url": runtime.OPENGWASDB_GIT_URL,
                    "vcs_info": {"vcs": "git", "commit_id": runtime.PINNED_OPENGWASDB_REVISION},
                }
            ),
            encoding="utf-8",
        )
        (dist / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: opengwasdb\nVersion: 0.3.0\n", encoding="utf-8"
        )
        (self.artifact_root / "OGS-00090" / "store.opengwasdb").mkdir(parents=True)
        with self.assertRaises(runtime.CompletionParentFormatError) as ctx:
            runtime.verify_runtime_for_releases(
                [self._job("OGS-00091", "complete")],
                registry_root=self.registry,
                executable=str(exe),
            )
        record_check()
        self.assertIn("exited 3", str(ctx.exception))


class ScheduledJobsParserTestCase(unittest.TestCase):
    """`release.scheduled_jobs` must expose the step, not just the Store id (#218)."""

    SAMPLE = """Job stats:
rule build_manifest:
    jobid: 7
    wildcards: root=/AR, store_id=OGS-00003
rule step:
    jobid: 6
    wildcards: root=/AR, store_id=OGS-00003, step=build
rule step:
    jobid: 5
    wildcards: root=/AR, store_id=OGS-00003, step=indexed-subset-hm3
rule register:
    jobid: 1
    wildcards: root=/AR, store_id=OGS-00003
"""

    def test_step_is_captured_and_none_for_stepless_jobs(self) -> None:
        from ogstores import release

        jobs = release.scheduled_jobs(self.SAMPLE)
        record_check()
        self.assertEqual(
            jobs,
            [
                ("/AR", "OGS-00003", None),
                ("/AR", "OGS-00003", "build"),
                ("/AR", "OGS-00003", "indexed-subset-hm3"),
            ],
        )
        record_check()
        self.assertEqual(release.scheduled_releases(self.SAMPLE), [("/AR", "OGS-00003")])

    def test_under_reported_wildcards_fail_loudly(self) -> None:
        from ogstores import release

        with self.assertRaises(RuntimeError):
            release.scheduled_jobs("rule step:\n    jobid: 1\n    wildcards: root=/AR\n")
        record_check()


def main() -> None:
    suite = unittest.TestSuite()
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(RuntimeGuardTestCase))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(ScheduledJobsParserTestCase))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if not result.wasSuccessful():
        sys.exit(1)
    print(f"ALL {n_checks} CHECKS PASSED")


if __name__ == "__main__":
    main()

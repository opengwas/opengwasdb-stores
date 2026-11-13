#!/usr/bin/env python3
"""Pre-write runtime guard for the supported release entry point (#218).

Covers the contract `ogstores.runtime.verify_runtime_for_releases` adds to
`pixi run release` (and, as defence in depth, the Snakefile `onstart`):

* `pixi.toml` follows opengwasdb's `main`, and `pixi.lock` alone holds the
  commit (#253): every `opengwasdb` reference the lock carries requests `main`
  and resolves that one commit as its `#<commit>`. The guard reads it from the
  lock and fails closed on an unknown or non-exact revision;
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
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tomllib
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

from ogstores import engine, resources, runtime  # noqa: E402

LOCKED = engine.locked_commit()

n_checks = 0


def record_check() -> None:
    global n_checks
    n_checks += 1


def _pin_spec_from_pixi_toml(toml_path: Path) -> dict:
    document = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    pypi = document["feature"]["store-build"]["pypi-dependencies"]
    return pypi["opengwasdb"]


#: The one shape a lock reference to the pin may take: the request (the release
#: tag, since #387) and the commit the solve resolved (`#`), with no other URL
#: parts that could select something else.
#: The branches `pixi.toml` may follow: `main`, and for the life of the branch
#: that makes this repository consume opengwasdb epic #307's engine, that epic's
#: integration branch. The run's finish moves the pin to the epic's merge commit
#: on `main`; the lock then spells `branch=main`.
PIN_BRANCHES = ("main", "orch/307-build-speed")

_LOCK_REFERENCE = re.compile(
    r"\Agit\+https://github\.com/opengwas/opengwasdb"
    r"\?branch=(?:main|orch%2F307-build-speed)#(?P<resolved>[0-9a-f]{40})\Z"
)


def _environments_carrying_the_pin(toml_path: Path) -> set[str]:
    """Every environment whose features include `store-build`, which declares the pin."""
    document = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    return {
        name
        for name, spec in document["environments"].items()
        if "store-build" in spec.get("features", [])
    }


def _lock_references(
    lock_path: Path,
) -> tuple[dict[str, list[str]], list[tuple[str, str]]]:
    """Every `opengwasdb` reference the lock carries: per environment, and package.

    The lock names one reference per environment, per platform, plus one package
    entry describing the solve. Each is a candidate for `pixi install` to build
    from, so a stale one selects a runtime that is not the pin for that
    environment even when `pixi.toml` and the module agree.
    """
    document = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    per_environment: dict[str, list[str]] = {}
    for name, environment in document["environments"].items():
        for packages in environment.get("packages", {}).values():
            for entry in packages:
                spec = entry.get("pypi")
                if isinstance(spec, str) and "opengwas/opengwasdb" in spec:
                    per_environment.setdefault(name, []).append(spec)
    package_entries = [
        (str(entry.get("name")), entry["pypi"])
        for entry in document["packages"]
        if isinstance(entry.get("pypi"), str) and "opengwas/opengwasdb" in entry["pypi"]
    ]
    return per_environment, package_entries


class _FakeRuntime:
    """A shell `opengwasdb` stub plus the metadata a real install carries."""

    def __init__(
        self,
        root: Path,
        *,
        commands: set[str],
        commit: str | None,
        version: str = "0.3.0",
        format_version: str = "0.2.2",
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
        single_id: bool = False,
    ) -> None:
        bundle_root = self.registry / store_id
        bundle_root.mkdir(parents=True, exist_ok=True)
        release: dict = {
            "store_id": store_id,
            "label": store_id,
            "status": "accepted",
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
        if completion or single_id:
            build["complete"] = {
                "command": "complete-dense",
                "options": {"ld-panel": "/p", "ancestry": "EUR"},
            }
        if single_id:
            build["completion_state"] = "reference_completed"
        if not completion:
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
        kwargs.setdefault("commit", LOCKED)
        return _FakeRuntime(self.tmp / name, **kwargs)

    def _job(self, store_id: str, step: str | None) -> tuple[str, str, str | None]:
        return (str(self.artifact_root), store_id, step)

    # --- pin & identity ---------------------------------------------------

    def test_pixi_toml_follows_main(self) -> None:
        """`pixi.toml` names opengwasdb's `main`; the lock alone holds the commit (#253)."""
        spec = _pin_spec_from_pixi_toml(REPO_ROOT / "pixi.toml")
        record_check()
        self.assertEqual(spec["git"], runtime.OPENGWASDB_GIT_URL)
        self.assertIn(spec["branch"], PIN_BRANCHES)
        self.assertEqual(set(spec), {"git", "branch"})

    def test_every_store_build_environment_resolves_the_locked_commit(self) -> None:
        """One stale lock entry selects another runtime, so every one must be the locked commit.

        Every environment that depends on the `store-build` feature carries an
        opengwasdb reference, and the lock has one package entry for it. Each
        must request `main` and resolve the one commit `engine.locked_commit`
        returns.
        """
        expected = _environments_carrying_the_pin(REPO_ROOT / "pixi.toml")
        record_check()
        self.assertTrue(expected, "no environment declares the store-build feature")
        per_environment, package_entries = _lock_references(REPO_ROOT / "pixi.lock")
        record_check()
        self.assertEqual(set(per_environment), expected)
        references: list[tuple[str, str]] = [
            (f"environment {name}", spec)
            for name, specs in per_environment.items()
            for spec in specs
        ]
        references.extend((f"package {name}", spec) for name, spec in package_entries)
        record_check()
        self.assertEqual(len(references), len(expected) + 1)
        self.assertEqual([name for name, _ in package_entries], ["opengwasdb"])
        for where, spec in references:
            with self.subTest(where):
                match = _LOCK_REFERENCE.match(spec)
                record_check()
                self.assertIsNotNone(match, f"{where}: not an exact branch=main#commit reference: {spec}")
                self.assertEqual(match["resolved"], LOCKED, where)

    def test_the_guard_reads_the_commit_from_the_lock(self) -> None:
        """An engine at the repository's locked commit is refused against a lock naming another."""
        self._write_bundle("OGS-00090")
        locked = self._runnable("locked", commands={"build-dense-vcf"})
        other_lock = self.tmp / "pixi.lock"
        other_lock.write_text(
            "packages:\n- pypi: git+https://github.com/opengwas/opengwasdb?branch=main#" + "d" * 40 + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(runtime.RuntimeRevisionError) as ctx:
            runtime.verify_runtime_for_releases(
                [self._job("OGS-00090", "build")],
                registry_root=self.registry,
                executable=str(locked.executable),
                lock=other_lock,
            )
        record_check()
        self.assertIn("d" * 40, str(ctx.exception))
        self.assertIn(LOCKED, str(ctx.exception))

    def test_a_lock_without_one_commit_is_refused(self) -> None:
        self._write_bundle("OGS-00090")
        locked = self._runnable("locked", commands={"build-dense-vcf"})
        empty_lock = self.tmp / "pixi.lock"
        empty_lock.write_text("packages: []\n", encoding="utf-8")
        with self.assertRaises(runtime.RuntimeRevisionError):
            runtime.verify_runtime_for_releases(
                [self._job("OGS-00090", "build")],
                registry_root=self.registry,
                executable=str(locked.executable),
                lock=empty_lock,
            )
        record_check()

    # --- one engine per run (#253) ---------------------------------------

    def _record(self, store_id: str, step: str, commit: str) -> None:
        records = self.artifact_root / store_id / "records"
        records.mkdir(parents=True, exist_ok=True)
        (records / f"{step}.json").write_text(
            json.dumps({"step": step, "success": True, "exit_code": 0, "opengwasdb_rev": commit}),
            encoding="utf-8",
        )

    def test_validate_rerun_over_another_engines_records_is_accepted(self) -> None:
        """`build` ran under `e…e`; re-running `validate` under the locked engine is accepted (#410).

        Only a step that continues another engine's output, a completion
        checkpoint, is held to that engine (`run.resume_argv_for_completion`).
        """
        self._write_bundle("OGS-00090")
        self._record("OGS-00090", "build", "e" * 40)
        self.assertNotEqual("e" * 40, LOCKED, "the record must name another engine")
        capable = self._runnable("capable", commands={"validate"})
        identity = runtime.verify_runtime_for_releases(
            [self._job("OGS-00090", "validate")],
            registry_root=self.registry,
            executable=str(capable.executable),
        )
        self.assertEqual(identity.revision, LOCKED)
        record_check()

    def test_a_phase_resumed_under_the_same_engine_passes(self) -> None:
        self._write_bundle("OGS-00090")
        self._record("OGS-00090", "build", LOCKED)
        capable = self._runnable("capable", commands={"validate"})
        runtime.verify_runtime_for_releases(
            [self._job("OGS-00090", "validate")],
            registry_root=self.registry,
            executable=str(capable.executable),
        )
        record_check()

    def test_a_step_run_again_is_not_a_resumed_one(self) -> None:
        """A scheduled step's old record is replaced, not reused, so its engine does not matter."""
        self._write_bundle("OGS-00090")
        self._record("OGS-00090", "build", "e" * 40)
        capable = self._runnable("capable", commands={"build-dense-vcf"})
        runtime.verify_runtime_for_releases(
            [self._job("OGS-00090", "build")],
            registry_root=self.registry,
            executable=str(capable.executable),
        )
        record_check()

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
        self.assertIn("not the commit pixi.lock resolves", str(ctx.exception))

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
        self.assertIn(LOCKED, str(ctx.exception))

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
        self.assertEqual(identity.revision, LOCKED)

    def test_missing_scheduled_capability_is_refused(self) -> None:
        self._write_bundle("OGS-00090")
        partial = self._runnable("partial", commands={"build-dense-vcf"})
        with self.assertRaises(runtime.RuntimeCapabilityError) as ctx:
            runtime.verify_runtime_for_releases(
                [
                    self._job("OGS-00090", "build"),
                    self._job("OGS-00090", "overview"),
                    self._job("OGS-00090", "validate"),
                ],
                registry_root=self.registry,
                executable=str(partial.executable),
            )
        record_check()
        message = str(ctx.exception)
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
        self.assertEqual(identity.revision, LOCKED)
        self.assertIn(LOCKED, identity.describe())

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
            "capable", commands={"build-dense-vcf", "regenerate-overview", "validate"}, probe_log=log
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
            [*jobs, self._job("OGS-00090", "overview")],
            registry_root=self.registry,
            executable=str(capable.executable),
            handoff=entry_point.handoff(),
        )
        record_check()
        self.assertEqual(self._probes(log), ["regenerate-overview"])

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

    def test_completion_parent_0_2_2_passes(self) -> None:
        self._write_bundle("OGS-00091", completion=True, derived_from="OGS-00090")
        self._write_bundle("OGS-00090")
        capable = self._capable_for_completion("capable", format_version="0.2.2")
        (self.artifact_root / "OGS-00090" / "store.opengwasdb").mkdir(parents=True)
        identity = runtime.verify_runtime_for_releases(
            [self._job("OGS-00091", "complete")],
            registry_root=self.registry,
            executable=str(capable.executable),
        )
        record_check()
        self.assertEqual(identity.format_version, "0.2.2")

    def test_completion_parent_below_0_2_2_is_refused_and_rebuilt_not_converted(self) -> None:
        """0.2.0 and 0.2.1 parents are refused: the pinned engine's completion consumes 0.2.2 only.

        Indel ALIDs spell the reference first from 0.2.2 (opengwasdb#381), so
        completing an older parent would sort the panel's indels beside the
        Store's own. The remedy is a rebuild with this engine; the converter
        writes 0.2.0, which completion refuses too, so the message must not
        send the operator to it.
        """
        for old in ("0.2.0", "0.2.1"):
            with self.subTest(parent_format=old):
                self._write_bundle("OGS-00091", completion=True, derived_from="OGS-00090")
                self._write_bundle("OGS-00090")
                capable = self._capable_for_completion(f"capable-{old}", format_version=old)
                (self.artifact_root / "OGS-00090" / "store.opengwasdb").mkdir(parents=True, exist_ok=True)
                with self.assertRaises(runtime.CompletionParentFormatError) as ctx:
                    runtime.verify_runtime_for_releases(
                        [self._job("OGS-00091", "complete")],
                        registry_root=self.registry,
                        executable=str(capable.executable),
                    )
                record_check()
                self.assertIn(old, str(ctx.exception))
                self.assertIn("rebuild", str(ctx.exception))
                self.assertNotIn("Convert the parent", str(ctx.exception))

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
                    "vcs_info": {"vcs": "git", "commit_id": LOCKED},
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

    # --- Observed-View Check capability (#306) ----------------------------

    def _stub(self, name: str, *, exit_code: int) -> Path:
        stub = self.tmp / name
        stub.write_text(f"#!/bin/sh\nexit {exit_code}\n", encoding="utf-8")
        stub.chmod(0o755)
        return stub

    def test_a_single_id_completion_on_a_runtime_without_the_command_is_refused(self) -> None:
        self._write_bundle("OGS-00090", single_id=True)
        without_command = self._stub("without-command", exit_code=2)
        before = sorted(self.tmp.rglob("*"))
        with self.assertRaises(resources.ObservedViewCheckRuntimeError) as ctx:
            resources.verify_observed_view_check_for_releases(
                [(str(self.artifact_root), "OGS-00090")],
                registry_root=self.registry,
                executable=str(without_command),
            )
        record_check()
        message = str(ctx.exception)
        self.assertIn("check-observed-view", message)
        self.assertIn("pixi.lock", message)
        self.assertIn("OGS-00090", message)
        self.assertEqual(sorted(self.tmp.rglob("*")), before, "a refusal writes nothing")

    def test_a_runtime_with_the_command_passes(self) -> None:
        self._write_bundle("OGS-00090", single_id=True)
        resources.verify_observed_view_check_for_releases(
            [(str(self.artifact_root), "OGS-00090")],
            registry_root=self.registry,
            executable=str(self._stub("with-command", exit_code=0)),
        )
        record_check()

    def test_a_plan_with_no_completion_phase_is_not_gated(self) -> None:
        """An Observed-Only release and a two-id child need no Observed-View Check."""
        self._write_bundle("OGS-00090")
        self._write_bundle("OGS-00091", completion=True, derived_from="OGS-00090")
        resources.verify_observed_view_check_for_releases(
            [(str(self.artifact_root), "OGS-00090"), (str(self.artifact_root), "OGS-00091")],
            registry_root=self.registry,
            executable="/definitely/missing",
        )
        record_check()

    # --- Recorded regions capability (#459) -------------------------------

    REGIONS_HEADER = (
        "analysis_id\tregion_kind\tchromosome\tstart\tend\tsource_genome_build\n"
        "GCST000001\tcis\t1\t100\t200\tGRCh38\n"
    )

    def _ragged_build(self, store_id: str, *, regions: bool, completes: bool = False) -> None:
        """A Ragged release whose build is `build-ragged-ssf`, with or without its recorded regions.

        `completes` adds the `complete` block a Reference-Completed release has:
        the table belongs to the build, so whether the release also completes
        its own Store must not change the answer (#459).
        """
        self._write_bundle(store_id, single_id=completes)
        build_path = self.registry / store_id / "build.yaml"
        build = yaml.safe_load(build_path.read_text(encoding="utf-8"))
        build.update(
            layout="ragged",
            build={"command": "build-ragged-ssf", "options": {}},
            post={"top_hits": False, "rho": False, "overview": False, "validate": True},
        )
        if completes:
            build["complete"] = {
                "command": "complete-ragged",
                "options": {"ld-panel": "/p", "ancestry": "EUR"},
            }
        build_path.write_text(yaml.safe_dump(build), encoding="utf-8")
        release_path = self.registry / store_id / "release.yaml"
        release = yaml.safe_load(release_path.read_text(encoding="utf-8"))
        release["association_coverage"] = "signals_only"
        release_path.write_text(yaml.safe_dump(release), encoding="utf-8")
        if regions:
            sidecars = self.registry / store_id / "sidecars"
            sidecars.mkdir(exist_ok=True)
            (sidecars / "sparse_regions.tsv").write_text(self.REGIONS_HEADER, encoding="utf-8")

    def _help_stub(self, name: str, help_text: str) -> Path:
        """A stub that answers `--help` with `help_text` and every other call with exit 2."""
        stub = self.tmp / name
        stub.write_text(
            "#!/bin/sh\n"
            'if [ "$2" = "--help" ]; then\n'
            f"  printf '%s\\n' '{help_text}'\n"
            "  exit 0\n"
            "fi\n"
            "exit 2\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)
        return stub

    def test_a_ragged_build_of_recorded_regions_on_a_runtime_without_the_flag_is_refused(self) -> None:
        """A runtime whose `build-ragged-ssf` takes no `--regions` cannot build the Store (#459)."""
        self._ragged_build("OGS-00090", regions=True)
        without_flag = self._help_stub(
            "without-regions",
            "Usage: opengwasdb build-ragged-ssf [OPTIONS] MANIFEST SOURCE STORE\n"
            "  --store-id TEXT\n  --admission-record DIR",
        )
        before = sorted(self.tmp.rglob("*"))
        with self.assertRaises(resources.RegionsTableRuntimeError) as ctx:
            resources.verify_regions_for_releases(
                [(str(self.artifact_root), "OGS-00090")],
                registry_root=self.registry,
                executable=str(without_flag),
            )
        record_check()
        message = str(ctx.exception)
        self.assertIn("OGS-00090", message)
        self.assertIn("build-ragged-ssf", message)
        self.assertIn("--regions", message)
        self.assertIn("opengwas/opengwasdb#430", message)
        self.assertEqual(sorted(self.tmp.rglob("*")), before, "a refusal writes nothing")

    def test_a_two_id_child_carrying_the_table_is_not_gated(self) -> None:
        """A completion plans no build, so it is given no flag and needs no capability (#459).

        The child here is a real two-id Ragged completion of a published parent:
        it carries a table of its own, and its plan still names no `--regions`.
        """
        self._write_bundle("OGS-00090")
        bundle_root = self.registry / "OGS-00091"
        bundle_root.mkdir(parents=True)
        (bundle_root / "release.yaml").write_text(
            yaml.safe_dump(
                {
                    "store_id": "OGS-00091",
                    "label": "OGS-00091",
                    "association_coverage": "signals_only",
                    "status": "accepted",
                    "derived_from": "OGS-00090",
                    "created_at": "2026-01-01T00:00:00Z",
                    "description": "fixture child",
                    "generator": {"commands": ["fixture"]},
                }
            ),
            encoding="utf-8",
        )
        (bundle_root / "build.yaml").write_text(
            yaml.safe_dump(
                {
                    "store_id": "OGS-00091",
                    "layout": "ragged",
                    "completion_state": "reference_completed",
                    "post": {"top_hits": False, "rho": False, "overview": False, "validate": True},
                    "complete": {"command": "complete-ragged", "options": {"ld-panel": "/p", "ancestry": "EUR"}},
                }
            ),
            encoding="utf-8",
        )
        (bundle_root / "analyses.tsv").write_text("analysis_id\n", encoding="utf-8")
        sidecars = bundle_root / "sidecars"
        sidecars.mkdir()
        (sidecars / "sparse_regions.tsv").write_text(self.REGIONS_HEADER, encoding="utf-8")
        resources.verify_regions_for_releases(
            [(str(self.artifact_root), "OGS-00091")],
            registry_root=self.registry,
            executable="/definitely/missing",
        )
        record_check()

    def test_a_runtime_taking_the_flag_passes(self) -> None:
        self._ragged_build("OGS-00090", regions=True)
        resources.verify_regions_for_releases(
            [(str(self.artifact_root), "OGS-00090")],
            registry_root=self.registry,
            executable=str(
                self._help_stub(
                    "with-regions",
                    "Usage: opengwasdb build-ragged-ssf [OPTIONS] MANIFEST SOURCE STORE\n"
                    "  --regions PATH\n  --admission-record DIR",
                )
            ),
        )
        record_check()

    def test_a_release_with_no_recorded_regions_is_not_gated(self) -> None:
        """A Ragged bundle without the table, and every Dense one, passes whatever the runtime."""
        self._ragged_build("OGS-00090", regions=False, completes=True)
        self._write_bundle("OGS-00091", single_id=True)
        resources.verify_regions_for_releases(
            [(str(self.artifact_root), "OGS-00090"), (str(self.artifact_root), "OGS-00091")],
            registry_root=self.registry,
            executable="/definitely/missing",
        )
        record_check()


class HelpProbeRenderingTestCase(unittest.TestCase):
    """A capability probe reads the help however the host draws it (#459).

    GitHub Actions sets `GITHUB_ACTIONS`, and the engine's typer/rich help then
    forces terminal styling: the option is drawn as
    `ESC[1;36m-ESC[0mESC[1;36m-regionsESC[0m`, which a plain match never finds.
    CI then refused every Ragged release carrying a regions table, because the
    run-time guard could not see `--regions` on the pinned engine (run
    43059062259). These cases use that engine, not a stub.
    """

    def test_the_regions_flag_is_read_under_the_styling_github_actions_forces(self) -> None:
        """The probe finds `--regions` with `GITHUB_ACTIONS` and `CI` set, as CI runs it."""
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "CI": "true"}):
            self.assertTrue(resources.probe_opengwasdb_regions_capability())
        record_check()

    def test_styling_around_an_option_does_not_hide_it(self) -> None:
        """An option drawn in pieces across escape sequences is still that option, and no other."""
        styled = "\x1b[1;36m-\x1b[0m\x1b[1;36m-regions\x1b[0m  \x1b[2mPATH\x1b[0m\n"
        self.assertTrue(resources._help_lists_flag(styled, "regions"))
        self.assertFalse(resources._help_lists_flag(styled, "region"))
        record_check()


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
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(HelpProbeRenderingTestCase))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if not result.wasSuccessful():
        sys.exit(1)
    print(f"ALL {n_checks} CHECKS PASSED")


if __name__ == "__main__":
    main()

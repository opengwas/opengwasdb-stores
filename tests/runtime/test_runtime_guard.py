#!/usr/bin/env python3
"""Pre-write runtime guard for the supported release entry point (#218).

Covers the contract `ogstores.runtime.verify_runtime_for_releases` adds to
`pixi run release` (and, as defence in depth, the Snakefile `onstart`):

* the tracked pin is explicit and the same revision in `pixi.toml`,
  `pixi.lock`, and `ogstores.runtime.PINNED_OPENGWASDB_REVISION`;
* a missing executable, a known different revision, or a runtime missing any
  subcommand a scheduled plan names is refused before any write;
* a scheduled Reference-Completed release whose existing parent Store is
  format 0.1.0 is refused, while a 0.2.0 parent or a parent that will be built
  in the same run passes;
* a capable pinned runtime reports its identity and is not gated when it is
  missing nothing.

The fixtures are tiny shell stubs plus a `direct_url.json`, so no real
`opengwasdb` install or Store is opened.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tomllib
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ogstores import runtime  # noqa: E402
from ogstores.plan import plan  # noqa: E402
from ogstores import bundle as bundle_module  # noqa: E402

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
    """A shell `opengwasdb` stub plus the direct_url.json metadata beside it."""

    def __init__(self, root: Path, *, commands: set[str], commit: str | None, format_version: str = "0.2.0"):
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir(parents=True)
        self.executable = self.bin / "opengwasdb"
        supported = "|".join(sorted(commands))
        self.executable.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "info" ]; then\n'
            f'  printf \'{{"format_version": "%s"}}\\n\' "{format_version}"\n'
            "  exit 0\n"
            "fi\n"
            f'case "$1" in {supported}) exit 0 ;; *) exit 2 ;; esac\n',
            encoding="utf-8",
        )
        self.executable.chmod(0o755)
        self.format_version = format_version
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


class RuntimeGuardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ogstores_runtime_"))
        self.registry = self.tmp / "stores"
        self.artifact_root = self.tmp / "artifacts"
        self.registry.mkdir()
        self.artifact_root.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_bundle(
        self,
        store_id: str,
        *,
        completion: bool = False,
        derived_from: str | None = None,
        commands: tuple[str, ...] = ("build-dense-vcf", "build-dense-top-hits", "regenerate-overview", "validate"),
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
            build["complete"] = {"command": "complete-dense", "options": {"ld-panel": "/p", "ancestry": "EUR"}}
        else:
            build["build"] = {"command": "build-dense-vcf", "options": {}}
        (bundle_root / "release.yaml").write_text(yaml.safe_dump(release), encoding="utf-8")
        (bundle_root / "build.yaml").write_text(yaml.safe_dump(build), encoding="utf-8")

    def _scheduled(self, store_id: str) -> list[tuple[str, str]]:
        return [(str(self.artifact_root), store_id)]

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
        with mock.patch.object(runtime.run, "get_opengwasdb_executable", return_value=runtime.run.UNAVAILABLE):
            with self.assertRaises(runtime.RuntimeCapabilityError) as ctx:
                runtime.verify_runtime_for_releases(
                    self._scheduled("OGS-00090"),
                    registry_root=self.registry,
                    executable=runtime.run.UNAVAILABLE,
                )
        record_check()
        self.assertIn("no opengwasdb executable", str(ctx.exception))

    def test_known_other_revision_is_refused(self) -> None:
        self._write_bundle("OGS-00090")
        other = _FakeRuntime(
            self.tmp / "other",
            commands={"build-dense-vcf", "build-dense-top-hits", "regenerate-overview", "validate"},
            commit="deadbeef" * 5,
        )
        with self.assertRaises(runtime.RuntimeRevisionError) as ctx:
            runtime.verify_runtime_for_releases(
                self._scheduled("OGS-00090"),
                registry_root=self.registry,
                executable=str(other.executable),
            )
        record_check()
        self.assertIn(runtime.PINNED_OPENGWASDB_REVISION, str(ctx.exception))

    def test_missing_capability_is_refused(self) -> None:
        self._write_bundle("OGS-00090")
        partial = _FakeRuntime(
            self.tmp / "partial",
            commands={"build-dense-vcf"},
            commit=runtime.PINNED_OPENGWASDB_REVISION,
        )
        with self.assertRaises(runtime.RuntimeCapabilityError) as ctx:
            runtime.verify_runtime_for_releases(
                self._scheduled("OGS-00090"),
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
        capable = _FakeRuntime(
            self.tmp / "capable",
            commands={"build-dense-vcf", "build-dense-top-hits", "regenerate-overview", "validate"},
            commit=runtime.PINNED_OPENGWASDB_REVISION,
        )
        identity = runtime.verify_runtime_for_releases(
            self._scheduled("OGS-00090"),
            registry_root=self.registry,
            executable=str(capable.executable),
        )
        record_check()
        self.assertEqual(identity.revision, runtime.PINNED_OPENGWASDB_REVISION)
        self.assertIn(runtime.PINNED_OPENGWASDB_REVISION, identity.describe())

    def test_completion_parent_0_1_0_is_refused(self) -> None:
        self._write_bundle("OGS-00091", completion=True, derived_from="OGS-00090")
        self._write_bundle("OGS-00090")
        _FakeRuntime(
            self.tmp / "capable",
            commands={"complete-dense", "build-indexed-subset", "regenerate-overview", "validate", "info"},
            commit=runtime.PINNED_OPENGWASDB_REVISION,
            format_version="0.1.0",
        )
        # The parent Store must exist on disk for the format gate to inspect it.
        (self.artifact_root / "OGS-00090" / "store.opengwasdb").mkdir(parents=True)
        with self.assertRaises(runtime.CompletionParentFormatError) as ctx:
            runtime.verify_runtime_for_releases(
                self._scheduled("OGS-00091"),
                registry_root=self.registry,
                executable=str(self.tmp / "capable" / "bin" / "opengwasdb"),
            )
        record_check()
        message = str(ctx.exception)
        self.assertIn("0.1.0", message)
        self.assertIn("convert_store_to_0_2_0.py", message)

    def test_completion_parent_0_2_0_passes(self) -> None:
        self._write_bundle("OGS-00091", completion=True, derived_from="OGS-00090")
        self._write_bundle("OGS-00090")
        capable = _FakeRuntime(
            self.tmp / "capable",
            commands={"complete-dense", "build-indexed-subset", "regenerate-overview", "validate", "info"},
            commit=runtime.PINNED_OPENGWASDB_REVISION,
            format_version="0.2.0",
        )
        (self.artifact_root / "OGS-00090" / "store.opengwasdb").mkdir(parents=True)
        identity = runtime.verify_runtime_for_releases(
            self._scheduled("OGS-00091"),
            registry_root=self.registry,
            executable=str(capable.executable),
        )
        record_check()
        self.assertEqual(identity.format_version, "0.2.0")

    def test_completion_parent_absent_is_not_gated(self) -> None:
        """A parent scheduled in the same run is a fresh 0.2.0 build, not a Store to gate."""
        self._write_bundle("OGS-00091", completion=True, derived_from="OGS-00090")
        capable = _FakeRuntime(
            self.tmp / "capable",
            commands={"complete-dense", "build-indexed-subset", "regenerate-overview", "validate", "info"},
            commit=runtime.PINNED_OPENGWASDB_REVISION,
        )
        runtime.verify_runtime_for_releases(
            self._scheduled("OGS-00091"),
            registry_root=self.registry,
            executable=str(capable.executable),
        )
        record_check()

    def test_commands_are_derived_from_the_plan_not_hardcoded(self) -> None:
        self._write_bundle("OGS-00090")
        commands = runtime.required_commands_for_release("OGS-00090", registry_root=self.registry)
        record_check()
        self.assertIn("build-dense-vcf", commands)
        self.assertIn("build-dense-top-hits", commands)
        self.assertIn("regenerate-overview", commands)
        self.assertIn("validate", commands)
        self.assertNotIn("info", commands)


def main() -> None:
    suite = unittest.TestSuite()
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(RuntimeGuardTestCase))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if not result.wasSuccessful():
        sys.exit(1)
    print(f"ALL {n_checks} CHECKS PASSED")


if __name__ == "__main__":
    main()

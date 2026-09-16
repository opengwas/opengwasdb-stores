#!/usr/bin/env python3
"""Release Bundle and paths tests (issue #108).

Validates:
  1. All seven committed bundles (OGS-00001..OGS-00007) load with the fields
     the workflow reads off them.
  2. The status transition vocabulary accepts every legal move and rejects
     every illegal one.
  3. Malformed YAML in a bundle file is recorded on the loaded Bundle rather
     than raised, so a caller can report on a broken bundle.
  4. load() opens registry files only, and never touches an artifact path.
  5. Every artifact path in paths.py is a pure function of the Store Release id.

Run from repository root:
    pixi run python tests/bundle/test_bundle.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ogstores import bundle, paths

n_checks = 0


def record_check() -> None:
    global n_checks
    n_checks += 1


class TestBundleAndPaths(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="ogstores_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_all_seven_bundles_load(self) -> None:
        """Every committed bundle loads with the identity fields the workflow reads."""
        for i in range(1, 8):
            store_id = f"OGS-{i:05d}"
            b = bundle.load(store_id, registry_root=REPO_ROOT / "stores")
            record_check()
            self.assertEqual(b.store_id, store_id)
            self.assertEqual(b.root, REPO_ROOT / "stores" / store_id)
            self.assertTrue(b.analyses_path.is_file(), f"{store_id} analyses.tsv exists")
            self.assertEqual(b.release.get("store_id"), store_id)
            self.assertEqual(b.build.get("store_id"), store_id)
            self.assertIn(b.layout, ("dense", "ragged", "hybrid"))
            self.assertIn(b.completion_state, ("observed_only", "reference_completed"))
            self.assertTrue(b.family, f"{store_id} declares a family")

    def test_status_transition_vocabulary(self) -> None:
        """Legal moves are accepted and illegal ones are rejected with an explanation."""
        legal_pairs = [
            ("candidate", "accepted"),
            ("candidate", "withdrawn"),
            ("candidate", "superseded"),
            ("accepted", "built"),
            ("accepted", "withdrawn"),
            ("built", "validated"),
            ("built", "superseded"),
            ("built", "withdrawn"),
            ("validated", "superseded"),
            ("validated", "withdrawn"),
        ]
        for src, dst in legal_pairs:
            record_check()
            self.assertTrue(bundle.is_legal_status_transition(src, dst))
            self.assertEqual(bundle.validate_status_transition(src, dst), [])

        illegal_pairs = [
            ("candidate", "validated"),
            ("candidate", "built"),
            ("accepted", "candidate"),
            ("validated", "candidate"),
            ("validated", "accepted"),
            ("withdrawn", "built"),
            ("withdrawn", "candidate"),
            ("unknown_state", "built"),
            ("candidate", "invalid_target"),
        ]
        for src, dst in illegal_pairs:
            record_check()
            self.assertFalse(bundle.is_legal_status_transition(src, dst))
            errs = bundle.validate_status_transition(src, dst)
            self.assertTrue(len(errs) > 0, f"Expected transition {src}->{dst} to fail, got: {errs}")

    def test_malformed_yaml_is_recorded_not_raised(self) -> None:
        """A bundle file that is not valid YAML loads with the parse error recorded."""
        store_dir = self.tmp_dir / "OGS-00050"
        store_dir.mkdir(parents=True)
        (store_dir / "release.yaml").write_text("store_id: OGS-00050\n  bad: [indent\n")
        (store_dir / "build.yaml").write_text("layout: dense\n")

        b = bundle.load("OGS-00050", registry_root=self.tmp_dir)
        record_check()
        self.assertIn("__yaml_error__", b.release)
        self.assertEqual(b.build.get("layout"), "dense")
        self.assertIsNone(b.validation)

    def test_load_opens_no_artifact_paths(self) -> None:
        """load() reads registry files only and never stats an artifact path."""
        forbidden = str(paths.DEFAULT_ARTIFACT_ROOT)
        real_is_file = Path.is_file

        def guarded_is_file(self_path: Path) -> bool:
            assert forbidden not in str(self_path), (
                f"Forbidden artifact path accessed during load(): {self_path}"
            )
            return real_is_file(self_path)

        with mock.patch.object(Path, "is_file", guarded_is_file):
            b = bundle.load("OGS-00001", registry_root=REPO_ROOT / "stores")
        record_check()
        self.assertEqual(b.store_id, "OGS-00001")

    def test_artifact_paths_pure_functions(self) -> None:
        """Every artifact path is derived from the Store Release id and root alone."""
        test_cases = [
            ("OGS-00001", paths.DEFAULT_ARTIFACT_ROOT),
            ("OGS-00042", paths.DEFAULT_ARTIFACT_ROOT),
            ("OGS-00099", Path("/custom/storage/root")),
        ]
        for sid, root in test_cases:
            root_p = Path(root)
            record_check()
            self.assertEqual(paths.store_dir(sid, root), root_p / sid)
            self.assertEqual(paths.release_root(sid, root), root_p / sid)
            self.assertEqual(paths.source_dir(sid, root), root_p / sid / "source")
            self.assertEqual(paths.work_dir(sid, root), root_p / sid / "work")
            self.assertEqual(paths.records_dir(sid, root), root_p / sid / "records")
            self.assertEqual(paths.record_path(sid, "build", root), root_p / sid / "records" / "build.json")
            self.assertEqual(paths.record_path(sid, "validate", root), root_p / sid / "records" / "validate.json")
            self.assertEqual(paths.store_path(sid, root), root_p / sid / "store.opengwasdb")
            self.assertEqual(paths.partial_store_path(sid, root), root_p / sid / "store.opengwasdb.partial")
            self.assertEqual(paths.by_label_dir(root), root_p / "by-label")
            self.assertEqual(paths.by_label_link(sid, "my-label", root), root_p / "by-label" / "my-label")
            self.assertEqual(paths.parent_store_path(sid, root), root_p / sid / "store.opengwasdb")


class TestArtifactRootResolution(unittest.TestCase):
    """`paths.artifact_root()` resolves configuration, not the Release Bundle (issue #126)."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="ogstores_config_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _write_config(self, text: str) -> Path:
        config_p = self.tmp_dir / paths.REPO_CONFIG_FILENAME
        config_p.write_text(text, encoding="utf-8")
        return config_p

    def test_workflow_config_override_wins(self) -> None:
        """A workflow config override beats the environment and the config file."""
        self._write_config("artifact_root: /from/file\n")
        resolved = paths.artifact_root(
            config_override="/from/override",
            env={paths.ARTIFACT_ROOT_ENV_VAR: "/from/env"},
            repo_root=self.tmp_dir,
        )
        record_check()
        self.assertEqual(resolved, Path("/from/override"))

    def test_environment_variable_beats_repo_config_file(self) -> None:
        """The environment variable beats the config file and the default."""
        self._write_config("artifact_root: /from/file\n")
        resolved = paths.artifact_root(
            env={paths.ARTIFACT_ROOT_ENV_VAR: "/from/env"},
            repo_root=self.tmp_dir,
        )
        record_check()
        self.assertEqual(resolved, Path("/from/env"))

    def test_repository_config_file_beats_default(self) -> None:
        """With no override or environment variable, the config file wins."""
        self._write_config("artifact_root: /from/file\n")
        resolved = paths.artifact_root(env={}, repo_root=self.tmp_dir)
        record_check()
        self.assertEqual(resolved, Path("/from/file"))

    def test_built_in_default_when_unconfigured(self) -> None:
        """With no override, environment variable or config file, the default is used."""
        resolved = paths.artifact_root(env={}, repo_root=self.tmp_dir)
        record_check()
        self.assertEqual(resolved, paths.DEFAULT_ARTIFACT_ROOT)

    def test_environment_variable_reads_process_environment(self) -> None:
        """When no mapping is supplied, the process environment is consulted."""
        with mock.patch.dict(os.environ, {paths.ARTIFACT_ROOT_ENV_VAR: "/from/process"}, clear=False):
            resolved = paths.artifact_root(repo_root=self.tmp_dir)
        record_check()
        self.assertEqual(resolved, Path("/from/process"))

    def test_tracked_repo_config_is_the_production_root(self) -> None:
        """The committed ogstores.yaml supplies the default production artifact root."""
        record_check()
        self.assertTrue(paths.repo_config_path(REPO_ROOT).is_file())
        self.assertEqual(paths.artifact_root(env={}), paths.DEFAULT_ARTIFACT_ROOT)

    def test_config_file_without_key_falls_back_to_default(self) -> None:
        """A config file that names no artifact_root does not override the default."""
        self._write_config("unrelated: true\n")
        resolved = paths.artifact_root(env={}, repo_root=self.tmp_dir)
        record_check()
        self.assertEqual(resolved, paths.DEFAULT_ARTIFACT_ROOT)

    def test_malformed_config_file_raises(self) -> None:
        """A malformed config file fails loudly rather than building in the wrong place."""
        self._write_config("- not\n- a\n- mapping\n")
        with self.assertRaises(ValueError):
            paths.artifact_root(env={}, repo_root=self.tmp_dir)
        record_check()
        self._write_config("artifact_root: 42\n")
        with self.assertRaises(ValueError):
            paths.artifact_root(env={}, repo_root=self.tmp_dir)
        record_check()


def main() -> None:
    suite = unittest.TestSuite()
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestBundleAndPaths))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestArtifactRootResolution))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if not result.wasSuccessful():
        sys.exit(1)
    print(f"ALL {n_checks} CHECKS PASSED")


if __name__ == "__main__":
    main()

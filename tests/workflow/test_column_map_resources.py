#!/usr/bin/env python3
"""Run-time verification of a bundle's carried Column Map (`ogstores.resources`, #292).

The bundle's `column_maps/<file>` must still have the binding's sha256 before
any build, the staged copy must be the verified bytes, and a build step reads
the staged copy only if it still is. These are the module-level cases; the
Snakemake integrations are in `test_workflow.py`.

Run from the repository root:
    python3 tests/workflow/test_column_map_resources.py
"""

from __future__ import annotations

import hashlib
import importlib.resources
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from ogstores import bundle, paths, resources

MAP = b'column_map_version = 1\nname = "mvp-map"\n'
FILE = "mvp-map.toml"
STORE_ID = "OGS-00096"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ColumnMapResourcesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="column-map-resources-")
        root = Path(self._tmp.name)
        self.stores, self.artifacts = root / "stores", root / "artifacts"
        self.directory = self.stores / STORE_ID
        (self.directory / "column_maps").mkdir(parents=True)
        self.artifacts.mkdir()
        (self.directory / "release.yaml").write_text(
            yaml.safe_dump({"store_id": STORE_ID, "status": "accepted"}), encoding="utf-8"
        )
        (self.directory / "analyses.tsv").write_text("analysis_id\n", encoding="utf-8")
        self.carried = self.directory / "column_maps" / FILE
        self.carried.write_bytes(MAP)
        self.bind({"name": "mvp-map", "sha256": _sha256(MAP), "file": FILE})
        self.staged = paths.column_map_path(STORE_ID, FILE, root=self.artifacts)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def bind(self, binding: dict) -> bundle.Bundle:
        (self.directory / "build.yaml").write_text(
            yaml.safe_dump({"store_id": STORE_ID, "layout": "dense", "column_map": binding}), encoding="utf-8"
        )
        return self.load()

    def load(self) -> bundle.Bundle:
        return bundle.load(STORE_ID, registry_root=self.stores)

    # -- the bundle's own copy ------------------------------------------------------

    def test_a_matching_map_verifies_and_a_changed_one_is_refused_naming_the_release_and_map(self) -> None:
        self.assertEqual(resources.verify_column_map(self.load()), self.carried)
        scheduled = [(str(self.artifacts), STORE_ID)]
        resources.verify_column_maps_for_releases(scheduled, registry_root=self.stores)
        self.carried.write_bytes(MAP + b"# edited\n")
        with self.assertRaisesRegex(
            resources.ColumnMapChecksumError,
            rf"Release {STORE_ID}: Column Map 'mvp-map' \(column_maps/mvp-map.toml\) has sha256 .*{_sha256(MAP)}",
        ):
            resources.verify_column_maps_for_releases(scheduled, registry_root=self.stores)

    def test_a_missing_or_symlinked_map_is_refused(self) -> None:
        self.carried.unlink()
        with self.assertRaisesRegex(resources.ColumnMapChecksumError, "does not carry as a regular file"):
            resources.verify_column_map(self.load())

    def test_a_built_in_binding_has_nothing_to_hash(self) -> None:
        shipped = importlib.resources.files("opengwasdb.readers").joinpath("column_maps/gwas-ssf.toml")
        loaded = self.bind({"name": "gwas-ssf", "sha256": _sha256(shipped.read_bytes())})
        self.assertIsNone(resources.verify_column_map(loaded))

    # -- staging ----------------------------------------------------------------------

    def test_staging_copies_the_verified_bytes(self) -> None:
        resources.stage_column_map(self.load(), self.staged)
        self.assertEqual(self.staged.read_bytes(), MAP)

    def test_a_changed_map_is_not_staged(self) -> None:
        self.carried.write_bytes(MAP + b"# edited\n")
        with self.assertRaises(resources.ColumnMapChecksumError):
            resources.stage_column_map(self.load(), self.staged)
        self.assertFalse(self.staged.exists())

    def test_a_map_edited_after_the_check_is_refused_at_the_copy(self) -> None:
        with mock.patch.object(resources, "verify_column_map", return_value=self.carried):
            self.carried.write_bytes(MAP + b"# edited\n")
            with self.assertRaisesRegex(resources.ColumnMapChecksumError, "changed while it was staged"):
                resources.stage_column_map(self.load(), self.staged)
        self.assertFalse(self.staged.exists())

    # -- the staged copy, before a step reads it -------------------------------------------

    def check_staged(self, inputs: list[Path] | None = None) -> None:
        resources.verify_staged_column_map(
            self.load(), self.artifacts, [self.staged] if inputs is None else inputs
        )

    def test_a_staged_copy_with_the_bound_bytes_passes(self) -> None:
        resources.stage_column_map(self.load(), self.staged)
        self.check_staged()

    def test_a_staged_copy_changed_since_staging_is_refused_by_name(self) -> None:
        resources.stage_column_map(self.load(), self.staged)
        self.staged.write_bytes(MAP + b"# edited after staging\n")
        with self.assertRaisesRegex(
            resources.ColumnMapChecksumError, rf"Release {STORE_ID}: the staged Column Map 'mvp-map' .*has sha256"
        ):
            self.check_staged()

    def test_a_missing_or_symlinked_staged_copy_is_refused(self) -> None:
        with self.assertRaisesRegex(resources.ColumnMapChecksumError, "is not a regular file"):
            self.check_staged()
        resources.stage_column_map(self.load(), self.staged)
        self.staged.unlink()
        self.staged.symlink_to(self.carried)
        with self.assertRaisesRegex(resources.ColumnMapChecksumError, "is not a regular file"):
            self.check_staged()

    def test_a_step_that_does_not_read_the_map_is_not_held_to_it(self) -> None:
        self.staged.parent.mkdir(parents=True)
        self.staged.write_bytes(b"other")
        self.check_staged(inputs=[paths.build_manifest_path(STORE_ID, root=self.artifacts)])

    def test_a_built_in_binding_stages_and_checks_nothing(self) -> None:
        shipped = importlib.resources.files("opengwasdb.readers").joinpath("column_maps/gwas-ssf.toml")
        self.bind({"name": "gwas-ssf", "sha256": _sha256(shipped.read_bytes())})
        self.check_staged(inputs=[self.staged])


if __name__ == "__main__":
    unittest.main()

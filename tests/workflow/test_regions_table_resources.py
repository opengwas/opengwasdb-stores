#!/usr/bin/env python3
"""Staging a bundle's recorded regions (`ogstores.resources`, #459).

`build-ragged-ssf` is given the table as `--regions`, from a copy under the
artifact root, so the planned argv is the same from any checkout
(opengwas/opengwasdb#430). These are the module-level cases: the copy is the
bundle's bytes, a copy whose bytes have drifted is detected by content rather
than by mtime, and a bundle whose entry at that path is not a carried file is
refused by name. The Snakemake integration and the entry point's scheduling are
in `test_workflow.py`.

Run from the repository root:
    python3 tests/workflow/test_regions_table_resources.py
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from ogstores import bundle, paths, resources

STORE_ID = "OGS-00095"

REGIONS = (
    "analysis_id\tregion_kind\tchromosome\tstart\tend\tsource_genome_build\n"
    "FIXTURE_1\tcis\t1\t100\t200\tGRCh38\n"
)
EDITED = REGIONS + "FIXTURE_1\tsignificant_trans\t2\t300\t400\tGRCh38\n"


def _write_bundle(stores: Path, *, sidecar: str | None = REGIONS) -> Path:
    """A Ragged bundle's registry files, and its sidecar when it records regions."""
    directory = stores / STORE_ID
    directory.mkdir(parents=True)
    (directory / "release.yaml").write_text(
        "store_id: {}\nlabel: fixture\nassociation_coverage: signals_only\n".format(STORE_ID),
        encoding="utf-8",
    )
    (directory / "build.yaml").write_text(
        "store_id: {}\nlayout: ragged\ncompletion_state: observed_only\n"
        "build:\n  command: build-ragged-ssf\n  options: {{}}\n"
        "post:\n  top_hits: false\n  overview: false\n  validate: true\n".format(STORE_ID),
        encoding="utf-8",
    )
    (directory / "analyses.tsv").write_text("analysis_id\nFIXTURE_1\n", encoding="utf-8")
    if sidecar is not None:
        sidecars = directory / "sidecars"
        sidecars.mkdir()
        (sidecars / "sparse_regions.tsv").write_text(sidecar, encoding="utf-8")
    return directory


class RegionsTableResourcesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="regions-table-resources-")
        root = Path(self._tmp.name)
        self.stores, self.artifacts = root / "stores", root / "artifacts"
        self.artifacts.mkdir()
        self.directory = _write_bundle(self.stores)
        self.carried = self.directory / bundle.SPARSE_REGIONS_SIDECAR
        self.staged = paths.regions_table_path(STORE_ID, root=self.artifacts)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def load(self) -> bundle.Bundle:
        return bundle.load(STORE_ID, registry_root=self.stores)

    # -- staging ----------------------------------------------------------------------

    def test_staging_copies_the_bundle_bytes_unchanged(self) -> None:
        resources.stage_regions_table(self.load(), self.staged)
        self.assertEqual(self.staged.read_bytes(), REGIONS.encode("utf-8"))
        # A partial file beside it would mean a reader could see half a table.
        self.assertEqual(sorted(self.staged.parent.iterdir()), [self.staged])

    def test_a_bundle_that_records_none_has_nothing_to_stage(self) -> None:
        self.carried.unlink()
        with self.assertRaisesRegex(resources.ReferenceResourceError, "records no"):
            resources.stage_regions_table(self.load(), self.staged)
        self.assertFalse(self.staged.exists())

    def test_a_staged_copy_of_the_bundle_bytes_is_current_by_content(self) -> None:
        resources.stage_regions_table(self.load(), self.staged)
        self.assertTrue(resources.staged_regions_is_current(self.load(), self.staged))

    # -- the staged copy, before the build reads it ---------------------------------------

    def test_a_staged_copy_whose_bytes_drifted_is_not_current_whatever_its_mtime(self) -> None:
        """Snakemake decides by mtime; a newer copy of older bytes would be read as current."""
        resources.stage_regions_table(self.load(), self.staged)
        self.staged.write_text(EDITED, encoding="utf-8")
        later = self.carried.stat().st_mtime + 86_400
        os.utime(self.staged, (later, later))
        self.assertFalse(resources.staged_regions_is_current(self.load(), self.staged))
        record = self.staged.stat()
        self.assertEqual(record.st_mtime, later, "the case is only meaningful as a newer file")

    def test_a_missing_or_symlinked_staged_copy_is_not_current(self) -> None:
        self.assertFalse(resources.staged_regions_is_current(self.load(), self.staged))
        resources.stage_regions_table(self.load(), self.staged)
        self.staged.unlink()
        self.staged.symlink_to(self.carried)
        self.assertFalse(resources.staged_regions_is_current(self.load(), self.staged))

    def test_a_bundle_that_records_none_has_no_current_copy(self) -> None:
        self.carried.unlink()
        self.assertFalse(resources.staged_regions_is_current(self.load(), self.staged))


if __name__ == "__main__":
    unittest.main()

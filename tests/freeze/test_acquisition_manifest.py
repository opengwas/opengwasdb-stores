#!/usr/bin/env python3
"""Loading an Acquisition Manifest (issue #283, ADR 0036).

An Acquisition Manifest records what acquisition produced for a Source
Collection: per file, its location, size and checksum, and for a file
acquisition had to reformat, the original it came from with that original's
checksum and the tool that changed it. It records availability, never intent.
A valid manifest loads; a missing field, or a lineage entry without its
original checksum or tool, is refused.

Run from the repository root:
    python3 tests/freeze/test_acquisition_manifest.py
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from manifest_generation.acquisition_manifest import (
    ACQUISITION_MANIFEST,
    AcquisitionManifestError,
    load_acquisition_manifest,
)
from manifest_generation.connector_tsv import FreezeError, write_connector
from manifest_generation.source_inventory import sha256_file

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_ORIGINAL = "c" * 64

HEADER = (
    "source_analysis_id", "status", "location", "bytes", "sha256", "source_url",
    "sidecar_location", "sidecar_url", "sidecar_bytes", "error",
    "original_location", "original_sha256", "reformat_tool",
)

READY = {
    "source_analysis_id": "A1",
    "status": "ok",
    "location": "/mirror/A1.h.tsv.gz",
    "bytes": "100",
    "sha256": SHA_A,
    "source_url": "https://example.org/A1.h.tsv.gz",
    "sidecar_location": "/mirror/A1.h.tsv.gz-meta.yaml",
    "sidecar_url": "https://example.org/A1.h.tsv.gz-meta.yaml",
    "sidecar_bytes": "10",
}
#: Acquisition tried and recorded why there is no file: an availability fact.
UNAVAILABLE = {
    "source_analysis_id": "A2",
    "status": "missing_remote_harmonised_yaml",
}
#: A file acquisition split out of a provider's multi-Analysis original.
REFORMATTED = {
    "source_analysis_id": "A3",
    "status": "ok",
    "location": "/mirror/A3.tsv.gz",
    "bytes": "300",
    "sha256": SHA_B,
    "original_location": "/mirror/original/all-analyses.tsv.gz",
    "original_sha256": SHA_ORIGINAL,
    "reformat_tool": "split-by-analysis 1.2.0",
}


def write_manifest(directory: Path, rows, header=HEADER) -> Path:
    path = directory / "acquisition.tsv"
    lines = ["\t".join(header)] + ["\t".join(row.get(column, "") for column in header) for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class AcquisitionManifestTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def assertRefused(self, rows, *fragments: str, header=HEADER) -> None:
        with self.assertRaises(AcquisitionManifestError) as caught:
            load_acquisition_manifest(write_manifest(self.tmp, rows, header=header))
        for fragment in fragments:
            self.assertIn(fragment, str(caught.exception))

    # -- a valid manifest ---------------------------------------------------

    def test_valid_manifest_loads_with_its_identity(self) -> None:
        path = write_manifest(self.tmp, [READY, UNAVAILABLE, REFORMATTED])
        manifest = load_acquisition_manifest(path)
        self.assertEqual(manifest.sha256, sha256_file(path))
        self.assertEqual(manifest.bytes, path.stat().st_size)
        self.assertEqual([entry.source_analysis_id for entry in manifest.entries], ["A1", "A2", "A3"])
        ready = manifest.entry("A1")
        self.assertTrue(ready.ready)
        self.assertEqual((ready.location, ready.bytes, ready.sha256), ("/mirror/A1.h.tsv.gz", 100, SHA_A))
        self.assertEqual(ready.sidecar_bytes, 10)
        self.assertIsNone(manifest.entry("A2").bytes)
        self.assertIsNone(ready.lineage)
        self.assertFalse(manifest.entry("A2").ready)
        lineage = manifest.entry("A3").lineage
        self.assertEqual(
            (lineage.original_location, lineage.original_sha256, lineage.reformat_tool),
            ("/mirror/original/all-analyses.tsv.gz", SHA_ORIGINAL, "split-by-analysis 1.2.0"),
        )
        self.assertEqual(manifest.readiness_counts, {"missing_remote_harmonised_yaml": 1, "ok": 2})

    def test_only_the_required_columns_are_needed(self) -> None:
        header = ("source_analysis_id", "status", "location", "bytes", "sha256")
        manifest = load_acquisition_manifest(write_manifest(self.tmp, [READY], header=header))
        self.assertEqual(manifest.entry("A1").sidecar_location, "")
        self.assertIsNone(manifest.entry("A1").sidecar_bytes)

    def test_the_writer_refuses_a_missing_required_column(self) -> None:
        """One writer rule for both connector files: no required column is filled in silently."""
        with self.assertRaises(AcquisitionManifestError) as caught:
            write_connector(self.tmp / "w.tsv", ACQUISITION_MANIFEST, ("source_analysis_id", "status"), [UNAVAILABLE])
        self.assertIn("location", str(caught.exception))

    def test_refusal_is_a_freeze_error(self) -> None:
        self.assertTrue(issubclass(AcquisitionManifestError, FreezeError))

    # -- refusals: missing fields ---------------------------------------------

    def test_missing_required_column_is_refused(self) -> None:
        header = tuple(column for column in HEADER if column != "sha256")
        self.assertRefused([UNAVAILABLE], "sha256", header=header)

    def test_ready_file_missing_a_field_is_refused(self) -> None:
        for column in ("location", "bytes", "sha256"):
            with self.subTest(column=column):
                self.assertRefused([dict(READY, **{column: ""})], column, "A1")

    def test_entry_without_analysis_or_status_is_refused(self) -> None:
        self.assertRefused([dict(READY, source_analysis_id="")], "source_analysis_id")
        self.assertRefused([dict(READY, status="")], "status")

    def test_unknown_status_is_refused(self) -> None:
        self.assertRefused([dict(READY, status="downloaded")], "downloaded")

    def test_unknown_column_is_refused(self) -> None:
        self.assertRefused([READY], "seconds", header=HEADER + ("seconds",))

    def test_bad_size_or_checksum_is_refused(self) -> None:
        self.assertRefused([dict(READY, bytes="1.5e3")], "bytes")
        # A leading zero would not survive the round trip to an integer and back.
        self.assertRefused([dict(READY, bytes="0100")], "bytes")
        self.assertRefused([dict(READY, sha256="xyz")], "sha256")
        self.assertRefused([dict(READY, sidecar_bytes="ten")], "sidecar_bytes")

    # -- refusals: lineage ----------------------------------------------------

    def test_lineage_without_original_checksum_is_refused(self) -> None:
        self.assertRefused([dict(REFORMATTED, original_sha256="")], "original_sha256", "A3")

    def test_lineage_without_tool_is_refused(self) -> None:
        self.assertRefused([dict(REFORMATTED, reformat_tool="")], "reformat_tool", "A3")

    def test_lineage_without_original_location_is_refused(self) -> None:
        self.assertRefused([dict(REFORMATTED, original_location="")], "original_location", "A3")

    def test_lineage_on_a_file_acquisition_did_not_produce_is_refused(self) -> None:
        unavailable = dict(REFORMATTED, status="data_failed", location="", bytes="", sha256="")
        self.assertRefused([unavailable], "lineage", "A3")

    # -- refusals: duplicates -------------------------------------------------

    def test_duplicate_analysis_is_refused(self) -> None:
        self.assertRefused([READY, dict(READY, location="/mirror/other.tsv.gz")], "duplicate", "A1")

    def test_duplicate_file_is_refused(self) -> None:
        self.assertRefused(
            [READY, dict(REFORMATTED, location=READY["location"])], "duplicate", READY["location"]
        )

    def test_a_sidecar_that_is_another_entrys_file_is_refused(self) -> None:
        self.assertRefused(
            [READY, dict(REFORMATTED, location=READY["sidecar_location"])],
            "duplicate", READY["sidecar_location"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

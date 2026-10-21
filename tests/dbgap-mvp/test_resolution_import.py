#!/usr/bin/env python3
"""Seed a Resolution Area from pre-#284 Resolution Records (#290).

The MVP's five accepted Resolution Records sets were written before #284, when
a run kept its records in its own Work Root. They are still the measurements of
unchanged files under the current pin, so a regeneration imports them into the
Source Collection's Resolution Area instead of re-resolving 6,022 Analyses.

Run from the repository root:
    python3 tests/dbgap-mvp/test_resolution_import.py
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from manifest_generation.resolution_area import ResolutionArea
from manifest_generation.resolution_area_import import (
    ResolutionImportError,
    import_records,
)

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def record(analysis_id: str, digest: str) -> dict:
    return {
        "analysis_id": analysis_id,
        "status": "success",
        "record_schema_version": 1,
        "fingerprints": {"fingerprint_digest": digest},
    }


def write_records(directory: Path, records: dict[str, dict]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for analysis_id, document in records.items():
        (directory / f"{analysis_id}.json").write_text(
            json.dumps(document, indent=2) + "\n", encoding="utf-8"
        )


class ResolutionAreaImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="resolution-import-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "old-work-root"
        self.records = {
            "GCST90000001": record("GCST90000001", DIGEST_A),
            "GCST90000002": record("GCST90000002", DIGEST_B),
        }
        write_records(self.source, self.records)
        # An old run's index describes the last run only; it is never imported.
        (self.source / "index.json").write_text("{\"analyses\": []}\n", encoding="utf-8")
        self.area = ResolutionArea.for_collection(self.root / "resolution", "dbgap-phs002453")
        self.checkpoint = "c" * 64

    def test_records_are_copied_into_the_checkpoint_and_kept(self) -> None:
        result = import_records(
            self.source, self.area, settings_digest=self.checkpoint
        )
        self.assertEqual(result.imported, 2)
        self.assertEqual(result.skipped, 0)
        checkpoint = self.area.checkpoint_dir(self.checkpoint)
        for analysis_id, document in self.records.items():
            copied = (checkpoint / f"{analysis_id}.json").read_bytes()
            self.assertEqual(copied, (self.source / f"{analysis_id}.json").read_bytes())
            kept = result.kept[analysis_id]
            self.assertEqual(
                self.area.read_kept(kept),
                copied,
            )
        self.assertFalse((checkpoint / "index.json").exists())

    def test_import_is_idempotent_and_never_replaces_a_different_record(self) -> None:
        import_records(self.source, self.area, settings_digest=self.checkpoint)
        again = import_records(self.source, self.area, settings_digest=self.checkpoint)
        self.assertEqual((again.imported, again.skipped), (0, 2))

        (self.source / "GCST90000001.json").write_text(
            json.dumps(record("GCST90000001", DIGEST_B), indent=2) + "\n", encoding="utf-8"
        )
        with self.assertRaises(ResolutionImportError):
            import_records(self.source, self.area, settings_digest=self.checkpoint)

    def test_a_record_without_a_fingerprint_digest_is_refused(self) -> None:
        document = record("GCST90000003", DIGEST_A)
        document["fingerprints"] = {}
        write_records(self.source, {"GCST90000003": document})
        with self.assertRaisesRegex(ResolutionImportError, "fingerprint digest"):
            import_records(self.source, self.area, settings_digest=self.checkpoint)

    def test_a_record_whose_analysis_id_disagrees_with_its_file_name_is_refused(self) -> None:
        document = record("GCST90000009", DIGEST_A)
        write_records(self.source, {"GCST90000004": document})
        with self.assertRaisesRegex(ResolutionImportError, "analysis_id"):
            import_records(self.source, self.area, settings_digest=self.checkpoint)

    def test_an_analysis_filter_imports_only_that_selection(self) -> None:
        result = import_records(
            self.source,
            self.area,
            settings_digest=self.checkpoint,
            analysis_ids={"GCST90000002"},
        )
        self.assertEqual(result.imported, 1)
        self.assertEqual(sorted(result.kept), ["GCST90000002"])


if __name__ == "__main__":
    unittest.main()

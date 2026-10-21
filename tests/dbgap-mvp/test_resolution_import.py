#!/usr/bin/env python3
"""Seed a Resolution Area from pre-#284 Resolution Records (#290).

The MVP's five accepted Resolution Records sets were written before #284, when
a run kept its records in its own Work Root. They are still the measurements of
unchanged files under the current pin, so a regeneration imports them into the
Source Collection's Resolution Area instead of re-resolving 6,022 Analyses.

Importing is a gate, not a copy (#290 review): a record must belong to the
collection's checksummed intent, its fingerprint must digest to the value it
stores, the file identity it was resolved against must be the one the
collection acquired, and the run's own index must name it.

Run from the repository root:
    python3 tests/dbgap-mvp/test_resolution_import.py
"""
from __future__ import annotations

import copy
import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from manifest_generation.acquisition_manifest import ACQUISITION_MANIFEST, load_acquisition_manifest
from manifest_generation.connector_tsv import write_connector
from manifest_generation.freeze import CollectionIntent
from manifest_generation.resolution_area import ResolutionArea
from manifest_generation.resolution_area_import import (
    ResolutionImportError,
    import_records,
)
from manifest_generation.source_metadata_table import (
    SOURCE_METADATA_TABLE,
    load_source_metadata_table,
)

SOURCE_SHA256 = "1" * 64
OTHER_SHA256 = "2" * 64


def fingerprint_digest(fingerprints: dict) -> str:
    """The resolver's canonical self-digest, computed independently of the code under test."""
    clean = {key: value for key, value in fingerprints.items() if key != "fingerprint_digest"}
    payload = json.dumps(clean, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def fingerprints(analysis_id: str, *, sha256: str = SOURCE_SHA256, size: int = 100) -> dict:
    document = {
        "source_file": f"/mirror/{analysis_id}.tsv.gz",
        "source_recorded_sha256": sha256,
        "source_recorded_bytes": size,
        "source_file_bytes": size,
        "opengwasdb_version": "0.3.0",
        "opengwasdb_git_hash": "4e922566d7fa4c13a11be3637069da63e48be28c",
    }
    document["fingerprint_digest"] = fingerprint_digest(document)
    return document


def record(analysis_id: str, **overrides) -> dict:
    document = {
        "analysis_id": analysis_id,
        "status": "success",
        "record_schema_version": 1,
        "ancestry": {"assigned_ancestry": "EUR", "gate_reason": "ok"},
        "fingerprints": fingerprints(analysis_id),
    }
    document.update(overrides)
    return document


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
        self.analysis_ids = ["GCST90000001", "GCST90000002"]
        self.records = {analysis_id: record(analysis_id) for analysis_id in self.analysis_ids}
        write_records(self.source, self.records)
        self.intent = self.collection_intent(self.analysis_ids)
        self.area = ResolutionArea.for_collection(self.root / "resolution", "dbgap-phs002453")
        self.checkpoint = "c" * 64

    def collection_intent(self, analysis_ids: list[str]) -> CollectionIntent:
        """The collection's two checksummed connectors, over ``analysis_ids``."""
        table = self.root / "source-metadata.tsv"
        write_connector(
            table,
            SOURCE_METADATA_TABLE,
            SOURCE_METADATA_TABLE.columns,
            [
                {
                    "source_analysis_id": analysis_id,
                    "source_trait_label": f"trait {analysis_id}",
                    "study_design": "case-control",
                    "sample_size": "1000",
                }
                for analysis_id in analysis_ids
            ],
        )
        manifest = self.root / "acquisition-manifest.tsv"
        write_connector(
            manifest,
            ACQUISITION_MANIFEST,
            ACQUISITION_MANIFEST.columns,
            [
                {
                    "source_analysis_id": analysis_id,
                    "status": "ok",
                    "location": f"/mirror/{analysis_id}.tsv.gz",
                    "bytes": "100",
                    "sha256": SOURCE_SHA256,
                }
                for analysis_id in analysis_ids
            ],
        )
        return CollectionIntent(
            provenance_path=self.root / "collection.meta.yaml",
            source_metadata_table=load_source_metadata_table(table),
            acquisition_manifest=load_acquisition_manifest(manifest),
        )

    def import_records(self, **kwargs):
        return import_records(
            self.source, self.area, settings_digest=self.checkpoint, intent=self.intent, **kwargs
        )

    def test_records_are_copied_into_the_checkpoint_and_kept(self) -> None:
        result = self.import_records()
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
        self.import_records()
        again = self.import_records()
        self.assertEqual((again.imported, again.skipped), (0, 2))

        edited = record("GCST90000001", ancestry={"assigned_ancestry": "AFR", "gate_reason": "ok"})
        (self.source / "GCST90000001.json").write_text(
            json.dumps(edited, indent=2) + "\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ResolutionImportError, "different bytes"):
            self.import_records()

    def test_a_record_without_a_fingerprint_digest_is_refused(self) -> None:
        document = record("GCST90000001")
        document["fingerprints"] = {}
        write_records(self.source, {"GCST90000001": document})
        with self.assertRaisesRegex(ResolutionImportError, "fingerprint"):
            self.import_records()

    def test_an_unrelated_record_with_an_arbitrary_digest_is_refused(self) -> None:
        """The reviewer's probe: an id, a schema number and 64 hex characters (#290 review)."""
        document = {
            "analysis_id": "GCST90000001",
            "status": "success",
            "record_schema_version": 1,
            "fingerprints": {"fingerprint_digest": "a" * 64},
        }
        write_records(self.source, {"GCST90000001": document})
        with self.assertRaisesRegex(ResolutionImportError, "fingerprint"):
            self.import_records()

    def test_a_record_whose_fingerprint_was_edited_is_refused(self) -> None:
        document = record("GCST90000001")
        document["fingerprints"]["source_recorded_sha256"] = OTHER_SHA256
        write_records(self.source, {"GCST90000001": document})
        with self.assertRaisesRegex(ResolutionImportError, "self-digest|digest"):
            self.import_records()

    def test_a_record_bound_to_no_file_the_collection_acquired_is_refused(self) -> None:
        document = record("GCST90000001", fingerprints=fingerprints("GCST90000001", sha256=OTHER_SHA256))
        write_records(self.source, {"GCST90000001": document})
        with self.assertRaisesRegex(ResolutionImportError, "acquired|source_recorded_sha256"):
            self.import_records()

    def test_a_record_outside_the_collection_is_refused_by_name(self) -> None:
        document = record("GCST90009999")
        write_records(self.source, {"GCST90009999": document})
        with self.assertRaisesRegex(
            ResolutionImportError, "GCST90009999 is not an Analysis of the Source Collection"
        ):
            self.import_records()

    def test_a_record_the_runs_own_index_does_not_name_is_refused(self) -> None:
        (self.source / "index.json").write_text(
            json.dumps({
                "record_schema_version": 1,
                "analyses": [
                    {
                        "analysis_id": "GCST90000001",
                        "record_file": "GCST90000001.json",
                        "status": "success",
                    }
                ],
            }),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ResolutionImportError, "index"):
            self.import_records()

    def test_an_index_the_runs_receipt_contradicts_is_refused(self) -> None:
        index = {
            "record_schema_version": 1,
            "analyses": [
                {"analysis_id": analysis_id, "record_file": f"{analysis_id}.json", "status": "success"}
                for analysis_id in self.analysis_ids
            ],
        }
        (self.source / "index.json").write_text(json.dumps(index), encoding="utf-8")
        receipt = self.source / "receipt" / "resolution-receipt.json"
        receipt.parent.mkdir()
        receipt.write_text(
            json.dumps({"records": {"index": copy.deepcopy(index) | {"n_total": 99}}}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ResolutionImportError, "receipt"):
            self.import_records()

    def test_the_runs_index_and_receipt_bind_the_records_it_imports(self) -> None:
        index = {
            "record_schema_version": 1,
            "analyses": [
                {"analysis_id": analysis_id, "record_file": f"{analysis_id}.json", "status": "success"}
                for analysis_id in self.analysis_ids
            ],
        }
        (self.source / "index.json").write_text(json.dumps(index), encoding="utf-8")
        receipt = self.source / "receipt" / "resolution-receipt.json"
        receipt.parent.mkdir()
        receipt.write_text(json.dumps({"records": {"index": copy.deepcopy(index)}}), encoding="utf-8")
        result = self.import_records()
        self.assertEqual(result.imported, 2)

    def test_a_record_whose_analysis_id_disagrees_with_its_file_name_is_refused(self) -> None:
        document = record("GCST90000009")
        write_records(self.source, {"GCST90000004": document})
        with self.assertRaisesRegex(ResolutionImportError, "analysis_id"):
            self.import_records()

    def test_an_analysis_filter_imports_only_that_selection(self) -> None:
        result = self.import_records(analysis_ids={"GCST90000002"})
        self.assertEqual(result.imported, 1)
        self.assertEqual(sorted(result.kept), ["GCST90000002"])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Seed a Resolution Area from pre-#284 Resolution Records (#290).

The MVP's five accepted Resolution Records sets were written before #284, when
a run kept its records in its own Work Root. They are still the measurements of
unchanged files under the current pin, so a regeneration imports them into the
Source Collection's Resolution Area instead of re-resolving 6,022 Analyses.

Importing is a gate, not a copy (#290 review): every record must be one the run
can prove it wrote. The run's own `index.json` must exist and name the Analysis,
the receipt in the run's sibling `receipt/` must corroborate that index (or bind
each record's sha256), and the record's measurements must be the index's; its
fingerprint must digest to the value it stores; and the file identity it was
resolved against must be the one the collection acquired. Missing evidence is a
refusal, never "nothing to check".

Run from the repository root:
    python3 tests/dbgap-mvp/test_resolution_import.py
"""
from __future__ import annotations

import copy
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
        "resolution_config": {"original_sd_method": "binary_trait"},
    }
    document["fingerprint_digest"] = fingerprint_digest(document)
    return document


def record(analysis_id: str, *, ancestry: str = "EUR", **overrides) -> dict:
    document = {
        "analysis_id": analysis_id,
        "status": "success",
        "record_schema_version": 1,
        "ancestry": {"assigned_ancestry": ancestry, "gate_reason": "ok"},
        "diagnostics": {"build_eligible_rows": 100},
        "phenotype_sd": {"estimate": None, "status": "skipped", "reason": "non_case_control"},
        "fingerprints": fingerprints(analysis_id),
    }
    document.update(overrides)
    return document


def index_entry(document: dict) -> dict:
    """The run's index entry for a record, as the resolver summarises it."""
    ancestry = document.get("ancestry") or {}
    estimate = (document.get("phenotype_sd") or {}).get("estimate") or {}
    return {
        "analysis_id": document["analysis_id"],
        "assigned_ancestry": ancestry.get("assigned_ancestry"),
        "error": None,
        "gate_reason": ancestry.get("gate_reason"),
        "manifest_index": 0,
        "original_sd": estimate.get("sd"),
        "original_sd_method": document["fingerprints"]["resolution_config"]["original_sd_method"],
        "record_file": f"{document['analysis_id']}.json",
        "status": document["status"],
    }


def _safe_index_entry(document: dict) -> dict:
    """The run's index entry, tolerating a deliberately malformed probe record."""
    try:
        return index_entry(document)
    except (KeyError, TypeError):
        return {
            "analysis_id": document.get("analysis_id"),
            "assigned_ancestry": None,
            "error": None,
            "gate_reason": None,
            "manifest_index": 0,
            "original_sd": None,
            "original_sd_method": None,
            "record_file": f"{document.get('analysis_id')}.json",
            "status": document.get("status"),
        }


class ResolutionAreaImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="resolution-import-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.run_root = self.root / "old-run"
        self.source = self.run_root / "records"
        self.analysis_ids = ["GCST90000001", "GCST90000002"]
        self.records = {analysis_id: record(analysis_id) for analysis_id in self.analysis_ids}
        self.intent = self.collection_intent(self.analysis_ids)
        self.area = ResolutionArea.for_collection(self.root / "resolution", "dbgap-phs002453")
        self.checkpoint = "c" * 64

    # -- fixtures ---------------------------------------------------------
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

    def write_run(
        self,
        *,
        records: dict[str, dict] | None = None,
        entries: list[dict] | None = None,
        receipt: dict | None | str = "default",
        index: bool = True,
    ) -> None:
        """A run's Work Root: ``records/``, its ``index.json`` and the sibling ``receipt/``.

        The default receipt corroborates the index and carries the run's
        per-record statuses; ``"default"`` builds it, ``None`` omits the receipt,
        a mapping is written as given, and ``"none"`` writes no receipt.
        """
        records = self.records if records is None else records
        self.source.mkdir(parents=True, exist_ok=True)
        for analysis_id, document in records.items():
            (self.source / f"{analysis_id}.json").write_text(
                json.dumps(document, indent=2) + "\n", encoding="utf-8"
            )
        if not index:
            return
        entries = [_safe_index_entry(document) for document in records.values()] if entries is None else entries
        document = {"record_schema_version": 1, "analyses": entries}
        (self.source / "index.json").write_text(json.dumps(document), encoding="utf-8")
        if receipt is None or receipt == "none":
            return
        if receipt == "default":
            receipt = {
                "records": {
                    "index": copy.deepcopy(document),
                    "statuses": {
                        entry["analysis_id"]: entry["status"] for entry in entries
                    },
                }
            }
        receipt_path = self.run_root / "receipt" / "resolution-receipt.json"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    def import_records(self, **kwargs):
        return import_records(
            self.source, self.area, settings_digest=self.checkpoint, intent=self.intent, **kwargs
        )

    # -- the happy path ---------------------------------------------------
    def test_records_are_copied_into_the_checkpoint_and_kept(self) -> None:
        self.write_run()
        result = self.import_records()
        self.assertEqual(result.imported, 2)
        self.assertEqual(result.skipped, 0)
        checkpoint = self.area.checkpoint_dir(self.checkpoint)
        for analysis_id, document in self.records.items():
            copied = (checkpoint / f"{analysis_id}.json").read_bytes()
            self.assertEqual(copied, (self.source / f"{analysis_id}.json").read_bytes())
            kept = result.kept[analysis_id]
            self.assertEqual(self.area.read_kept(kept), copied)
        self.assertFalse((checkpoint / "index.json").exists())

    def test_import_is_idempotent_and_never_replaces_a_checkpoint_record(self) -> None:
        self.write_run()
        self.import_records()
        again = self.import_records()
        self.assertEqual((again.imported, again.skipped), (0, 2))

        # A checkpoint record is write-once, whatever the source says later.
        checkpoint = self.area.checkpoint_dir(self.checkpoint) / "GCST90000001.json"
        checkpoint.write_text('{"analysis_id": "GCST90000001"}\n', encoding="utf-8")
        with self.assertRaisesRegex(ResolutionImportError, "different bytes"):
            self.import_records()

    def test_a_receipt_that_binds_each_records_sha256_is_honoured(self) -> None:
        """A v1 pipeline receipt binds the record bytes; a matching one imports."""
        self.write_run(receipt="none")
        digests = {
            analysis_id: hashlib.sha256((self.source / f"{analysis_id}.json").read_bytes()).hexdigest()
            for analysis_id in self.analysis_ids
        }
        receipt_path = self.run_root / "receipt" / "resolution-receipt.json"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps({"record_digests": digests}), encoding="utf-8")
        self.assertEqual(self.import_records().imported, 2)

    def test_a_record_whose_sha256_the_receipt_contradicts_is_refused(self) -> None:
        self.write_run(receipt="none")
        receipt_path = self.run_root / "receipt" / "resolution-receipt.json"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(
            json.dumps({"record_digests": {analysis_id: "d" * 64 for analysis_id in self.analysis_ids}}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ResolutionImportError, "record_digests|sha256"):
            self.import_records()

    # -- evidence ---------------------------------------------------------
    def test_an_edited_measurement_the_index_contradicts_is_refused(self) -> None:
        """The reviewer's probe: ancestry EUR -> AFR, fingerprints untouched (#290 review)."""
        self.write_run()
        probe = record("GCST90000001", ancestry="AFR")
        (self.source / "GCST90000001.json").write_text(
            json.dumps(probe, indent=2) + "\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ResolutionImportError, "assigned_ancestry"):
            self.import_records()

    def test_an_empty_index_is_refused(self) -> None:
        self.write_run(entries=[])
        with self.assertRaisesRegex(ResolutionImportError, "index"):
            self.import_records()

    def test_a_run_without_an_index_is_refused(self) -> None:
        self.write_run(index=False)
        with self.assertRaisesRegex(ResolutionImportError, "index"):
            self.import_records()

    def test_a_run_without_a_receipt_is_refused(self) -> None:
        self.write_run(receipt=None)
        with self.assertRaisesRegex(ResolutionImportError, "receipt"):
            self.import_records()

    def test_a_contradictory_sibling_receipt_is_refused(self) -> None:
        self.write_run()
        receipt_path = self.run_root / "receipt" / "resolution-receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["records"]["index"]["analyses"].reverse()
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        with self.assertRaisesRegex(ResolutionImportError, "receipt"):
            self.import_records()

    def test_a_receipt_that_corroborates_nothing_is_refused(self) -> None:
        self.write_run(receipt={"records": {}})
        with self.assertRaisesRegex(ResolutionImportError, "receipt"):
            self.import_records()

    def test_a_record_the_index_does_not_name_is_refused(self) -> None:
        self.write_run(entries=[index_entry(self.records["GCST90000001"])])
        with self.assertRaisesRegex(ResolutionImportError, "index"):
            self.import_records()

    # -- the record itself ------------------------------------------------
    def test_a_record_without_a_fingerprint_digest_is_refused(self) -> None:
        document = record("GCST90000001")
        document["fingerprints"] = {}
        self.write_run(records={"GCST90000001": document}, entries=None)
        with self.assertRaisesRegex(ResolutionImportError, "fingerprint"):
            self.import_records()

    def test_an_unrelated_record_with_an_arbitrary_digest_is_refused(self) -> None:
        """The reviewer's earlier probe: an id, a schema number and 64 hex characters."""
        document = {
            "analysis_id": "GCST90000001",
            "status": "success",
            "record_schema_version": 1,
            "fingerprints": {"fingerprint_digest": "a" * 64},
        }
        self.write_run(records={"GCST90000001": document})
        with self.assertRaisesRegex(ResolutionImportError, "fingerprint"):
            self.import_records()

    def test_a_record_whose_fingerprint_was_edited_is_refused(self) -> None:
        document = record("GCST90000001")
        document["fingerprints"]["source_recorded_sha256"] = OTHER_SHA256
        self.write_run(records={"GCST90000001": document})
        with self.assertRaisesRegex(ResolutionImportError, "self-digest|digest"):
            self.import_records()

    def test_a_record_bound_to_no_file_the_collection_acquired_is_refused(self) -> None:
        document = record("GCST90000001")
        document["fingerprints"] = fingerprints("GCST90000001", sha256=OTHER_SHA256)
        self.write_run(records={"GCST90000001": document})
        with self.assertRaisesRegex(ResolutionImportError, "acquired|source_recorded_sha256"):
            self.import_records()

    def test_a_record_outside_the_collection_is_refused_by_name(self) -> None:
        document = record("GCST90009999")
        self.write_run(records={"GCST90009999": document})
        with self.assertRaisesRegex(
            ResolutionImportError, "GCST90009999 is not an Analysis of the Source Collection"
        ):
            self.import_records()

    def test_a_record_whose_analysis_id_disagrees_with_its_file_name_is_refused(self) -> None:
        document = record("GCST90000009")
        self.write_run(records={"GCST90000004": document})
        with self.assertRaisesRegex(ResolutionImportError, "analysis_id"):
            self.import_records()

    def test_an_analysis_filter_imports_only_that_selection(self) -> None:
        self.write_run()
        result = self.import_records(analysis_ids={"GCST90000002"})
        self.assertEqual(result.imported, 1)
        self.assertEqual(sorted(result.kept), ["GCST90000002"])


if __name__ == "__main__":
    unittest.main()

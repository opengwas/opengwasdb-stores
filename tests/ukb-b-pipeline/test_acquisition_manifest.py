#!/usr/bin/env python3
"""The ukb-b Acquisition Manifest writer: identity and integrity receipts (#289).

The seam is the shared Acquisition Manifest format: from the recorded identity
(checksums) and integrity receipts, the writer emits one row per declared
Analysis with its VCF, its provider JSON sidecar and a readiness status. A
missing ``.tbi``, a size mismatch and a checksum mismatch are each a non-ready
row with its reason -- the explained absence the shared freeze keeps as a
non-ready inventory fact and never a release member.

The expected statuses and errors are the receipts' own facts (a deleted index
file, a truncated file, a reported checksum mismatch), never the writer's
arithmetic: each case changes the evidence, not the code.

Run from the repository root:
    python3 tests/ukb-b-pipeline/test_acquisition_manifest.py
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import yaml

from manifest_generation.acquisition_manifest import load_acquisition_manifest
from manifest_generation.providers.ukb_b import write_acquisition_manifest as writer
from manifest_generation.source_inventory import InventoryError
from manifest_generation.source_inventory import (
    KNOWN_READINESS_STATUSES,
    READY_STATUSES,
    SOURCE_VERIFICATION_STATUSES,
)

from ukb_b_fixture import ANALYSES, build_fixture

class AcquisitionManifestWriterTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ukb-b-manifest-")
        self.root = Path(self._tmp.name)
        self.fixture = build_fixture(self.root)
        self.out = self.root / "manifest.tsv"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self) -> Path:
        return writer.write_from_evidence(
            self.fixture.declared_selection,
            self.fixture.source_dir,
            writer.Evidence(
                self.fixture.checksums, self.fixture.integrity, self.fixture.verification
            ),
            self.out,
        )

    def rows(self) -> dict[str, dict[str, str]]:
        manifest = load_acquisition_manifest(self.out)
        return {entry.source_analysis_id: row for row, entry in zip(manifest.rows, manifest.entries)}

    def rewrite_json(self, path: Path, edit) -> None:
        document = json.loads(path.read_text(encoding="utf-8"))
        edit(document)
        path.write_text(json.dumps(document), encoding="utf-8")

    # -- the seam ---------------------------------------------------------

    def test_every_declared_analysis_is_one_ready_row_with_its_sidecar(self) -> None:
        self.write()
        manifest = load_acquisition_manifest(self.out)
        self.assertEqual(manifest.readiness_counts, {"ok": len(ANALYSES)})
        rows = self.rows()
        for analysis_id, _document, body in ANALYSES:
            row = rows[analysis_id]
            self.assertEqual(row["status"], "ok")
            self.assertEqual(row["location"], str(self.fixture.source_dir / f"{analysis_id}.vcf.gz"))
            self.assertEqual(row["bytes"], str(len(body)))
            self.assertEqual(row["source_analysis_id"], analysis_id)
            self.assertEqual(
                row["sidecar_location"], str(self.fixture.source_dir / f"{analysis_id}.json")
            )
            self.assertEqual(
                row["sidecar_bytes"],
                str((self.fixture.source_dir / f"{analysis_id}.json").stat().st_size),
            )
            self.assertEqual(row["error"], "")
            # The checksum is the fixture's own bytes, computed here.
            self.assertEqual(row["sha256"], hashlib.sha256(body).hexdigest())
            self.assertEqual(manifest.entry(analysis_id).sha256, hashlib.sha256(body).hexdigest())

    def test_the_statuses_are_shared_readiness_vocabulary(self) -> None:
        self.assertTrue(SOURCE_VERIFICATION_STATUSES <= KNOWN_READINESS_STATUSES)
        self.assertEqual(SOURCE_VERIFICATION_STATUSES & READY_STATUSES, frozenset())

    def test_the_writer_records_the_evidence_it_read(self) -> None:
        lineage = self.write()
        document = yaml.safe_load(lineage.read_text(encoding="utf-8"))
        self.assertEqual(document["rows"], len(ANALYSES))
        self.assertEqual(document["readiness_counts"], {"ok": len(ANALYSES)})
        self.assertEqual(
            sorted(document["evidence"]),
            ["checksums", "integrity", "verification"],
        )
        for name, path, rows in (
            ("checksums", self.fixture.checksums, len(ANALYSES)),
            ("integrity", self.fixture.integrity, len(ANALYSES)),
            # A verification report records one row per verified file and names
            # no failures, so no Analysis is counted as a refused one.
            ("verification", self.fixture.verification, len(ANALYSES)),
        ):
            recorded = document["evidence"][name]
            self.assertEqual(recorded["path"], str(path))
            self.assertEqual(recorded["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(recorded["rows"], rows)
        # The verification receipt is the only one that can refuse an Analysis,
        # so it alone records how many it did.
        self.assertEqual(document["evidence"]["verification"]["failures"], 0)
        self.assertNotIn("failures", document["evidence"]["checksums"])

    def test_the_command_line_writes_the_manifest(self) -> None:
        self.assertEqual(
            writer.main(
                [
                    "--declared-selection",
                    str(self.fixture.declared_selection),
                    "--source-dir",
                    str(self.fixture.source_dir),
                    "--checksums",
                    str(self.fixture.checksums),
                    "--integrity",
                    str(self.fixture.integrity),
                    "--verification",
                    str(self.fixture.verification),
                    "--out",
                    str(self.out),
                ]
            ),
            0,
        )
        self.assertEqual(
            load_acquisition_manifest(self.out).readiness_counts, {"ok": len(ANALYSES)}
        )

    # -- the verification report is evidence, or it is refused -------------

    def write_verification(self, document) -> None:
        self.fixture.verification.write_text(
            document if isinstance(document, str) else json.dumps(document), encoding="utf-8"
        )

    def assertRefused(self, *fragments: str) -> None:
        with self.assertRaises(InventoryError) as caught:
            self.write()
        for fragment in fragments:
            self.assertIn(fragment, str(caught.exception))

    def test_an_absent_verification_report_is_refused(self) -> None:
        self.fixture.verification.unlink()
        self.assertRefused("verification report", "not found")

    def test_an_empty_verification_report_is_refused(self) -> None:
        # `{}` is a report that verified nothing: not three ready rows.
        self.write_verification({})
        self.assertRefused("ok", "deep", "rows")

    def test_a_shallow_verification_report_is_refused(self) -> None:
        # A shallow report compares sizes and mtimes, never a checksum.
        self.write_verification(
            {
                "ok": True,
                "deep": False,
                "rows": len(ANALYSES),
                "missing": [],
                "size_mismatch": [],
                "checksum_mismatch": [],
                "index_mismatch": [],
            }
        )
        self.assertRefused("deep")

    def test_a_failed_verification_report_is_refused(self) -> None:
        self.write_verification({"ok": False, "deep": False, "rows": 0})
        self.assertRefused("deep", "rows")

    def test_a_report_that_does_not_cover_the_selection_is_refused(self) -> None:
        self.write_verification(
            {
                "ok": True,
                "deep": True,
                "rows": len(ANALYSES) - 1,
                "missing": [],
                "size_mismatch": [],
                "checksum_mismatch": [],
                "index_mismatch": [],
            }
        )
        self.assertRefused("rows", str(len(ANALYSES) - 1), str(len(ANALYSES)))

    def test_a_report_naming_an_analysis_outside_the_selection_is_refused(self) -> None:
        self.write_verification(
            {
                "ok": False,
                "deep": True,
                "rows": len(ANALYSES),
                "missing": ["ukb-b-99999"],
                "size_mismatch": [],
                "checksum_mismatch": [],
                "index_mismatch": [],
            }
        )
        self.assertRefused("ukb-b-99999", "not selected")

    def test_a_report_that_says_ok_while_listing_a_failure_is_refused(self) -> None:
        self.write_verification(
            {
                "ok": True,
                "deep": True,
                "rows": len(ANALYSES),
                "missing": ["ukb-b-10054"],
                "size_mismatch": [],
                "checksum_mismatch": [],
                "index_mismatch": [],
            }
        )
        self.assertRefused("ok", "ukb-b-10054")

    def test_a_report_listing_a_missing_file_makes_its_row_non_ready(self) -> None:
        # The deep verification found it gone; the row records that, with the
        # reason, instead of claiming the file verified.
        self.write_verification(
            {
                "ok": False,
                "deep": True,
                "rows": len(ANALYSES),
                "missing": ["ukb-b-10054"],
                "size_mismatch": [],
                "checksum_mismatch": [],
                "index_mismatch": [],
            }
        )
        self.write()
        row = self.rows()["ukb-b-10054"]
        self.assertEqual(row["status"], "missing_file")
        self.assertIn("ukb-b-10054", row["error"])
        self.assertEqual(self.rows()["ukb-b-10001"]["status"], "ok")

    def test_the_verification_report_is_required_on_the_command_line(self) -> None:
        with self.assertRaises(SystemExit):
            writer.main(
                [
                    "--declared-selection",
                    str(self.fixture.declared_selection),
                    "--source-dir",
                    str(self.fixture.source_dir),
                    "--checksums",
                    str(self.fixture.checksums),
                    "--integrity",
                    str(self.fixture.integrity),
                    "--out",
                    str(self.out),
                ]
            )

    # -- the integrity failures -------------------------------------------

    def assertNotReady(self, analysis_id: str, status: str, *fragments: str) -> None:
        self.write()
        row = self.rows()[analysis_id]
        self.assertEqual(row["status"], status)
        for fragment in fragments:
            self.assertIn(fragment, row["error"])
        # The manifest is written either way: a refusal of one file is still an
        # availability fact, and freeze reads it as one.
        self.assertTrue(self.out.is_file())

    def test_a_missing_vcf_is_a_missing_file_row(self) -> None:
        (self.fixture.source_dir / "ukb-b-10054.vcf.gz").unlink()
        self.assertNotReady("ukb-b-10054", "missing_file", "ukb-b-10054.vcf.gz")

    def test_a_missing_index_is_a_missing_index_row(self) -> None:
        (self.fixture.source_dir / "ukb-b-10054.vcf.gz.tbi").unlink()
        self.assertNotReady("ukb-b-10054", "missing_index", "ukb-b-10054.vcf.gz.tbi")

    def test_a_changed_size_is_a_size_mismatch_row(self) -> None:
        (self.fixture.source_dir / "ukb-b-10054.vcf.gz").write_bytes(b"truncated\n")
        self.assertNotReady("ukb-b-10054", "size_mismatch", "the recorded size is")

    def test_a_reported_checksum_mismatch_is_its_own_row(self) -> None:
        # The writer does not re-read 425 GB: the deep verification says so.
        self.rewrite_json(
            self.fixture.verification,
            lambda document: document.update(
                {"checksum_mismatch": ["ukb-b-10054 (" + "0" * 64 + ")"], "ok": False}
            ),
        )
        self.assertNotReady("ukb-b-10054", "checksum_mismatch", "recorded sha256")

    def test_a_reported_index_mismatch_is_its_own_row(self) -> None:
        self.write_verification(
            {
                "ok": False,
                "deep": True,
                "rows": len(ANALYSES),
                "missing": [],
                "size_mismatch": [],
                "checksum_mismatch": [],
                "index_mismatch": ["ukb-b-10054 (index bytes changed)"],
            }
        )
        self.assertNotReady("ukb-b-10054", "index_mismatch", "recorded index")

    def test_a_bad_index_in_the_integrity_receipt_is_a_bad_index_row(self) -> None:
        self.rewrite_json(
            self.fixture.integrity,
            lambda document: document["entries"]["ukb-b-10054"].update(
                {"status": "bad_index", "detail": "tabix -l listed no contig"}
            ),
        )
        self.assertNotReady("ukb-b-10054", "bad_index", "tabix -l listed no contig")

    def test_a_failed_bgzf_stream_is_an_integrity_failed_row(self) -> None:
        self.rewrite_json(
            self.fixture.integrity,
            lambda document: document["entries"]["ukb-b-10054"].update(
                {"status": "integrity_failed", "detail": "bgzip -t exited 1"}
            ),
        )
        self.assertNotReady("ukb-b-10054", "integrity_failed", "bgzip -t exited 1")

    def test_a_non_ready_row_keeps_the_identity_it_was_held_to(self) -> None:
        checksums = json.loads(self.fixture.checksums.read_text(encoding="utf-8"))
        (self.fixture.source_dir / "ukb-b-10054.vcf.gz.tbi").unlink()
        self.write()
        row = self.rows()["ukb-b-10054"]
        self.assertEqual(row["bytes"], str(checksums["entries"]["ukb-b-10054"]["data_bytes"]))
        self.assertEqual(row["sha256"], checksums["entries"]["ukb-b-10054"]["sha256"])

    # -- refusals ---------------------------------------------------------

    def assertRefused(self, *fragments: str) -> None:
        with self.assertRaises(Exception) as caught:
            self.write()
        for fragment in fragments:
            self.assertIn(fragment, str(caught.exception))

    def test_a_declared_analysis_the_receipts_do_not_cover_is_refused(self) -> None:
        self.rewrite_json(
            self.fixture.checksums,
            lambda document: document["entries"].pop("ukb-b-10054"),
        )
        self.assertRefused("ukb-b-10054", "no entry for it")

    def test_a_missing_provider_json_sidecar_is_refused(self) -> None:
        (self.fixture.source_dir / "ukb-b-10054.json").unlink()
        self.assertRefused("ukb-b-10054", "sidecar")

    def test_a_recorded_file_outside_the_source_directory_is_refused(self) -> None:
        self.rewrite_json(
            self.fixture.checksums,
            lambda document: document["entries"]["ukb-b-10054"].update(
                {"data_file": "/elsewhere/ukb-b-10054.vcf.gz"}
            ),
        )
        self.assertRefused("ukb-b-10054", "is not in")


if __name__ == "__main__":
    unittest.main()

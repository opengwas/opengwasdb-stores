#!/usr/bin/env python3
"""The shared freeze catches what ukb-b's own inventory caught (#289, acceptance criterion 2).

ukb-b's retired ``inventory.py`` refused a freeze when a declared Analysis had no
file, when the mirror held a file no declared Analysis claimed, and when an
Analysis or a file was duplicated (``tests/ukb-b/test_ukb_inventory.py``). This
suite ports those three cases onto the shared freeze (``manifest_generation.freeze``)
with ukb-b-shaped connectors -- the Source Metadata Table the ukb-b adapter
writes and the Acquisition Manifest the ukb-b writer writes -- so the guarantee
survives the retirement instead of retiring with it.

Two shapes are worth keeping apart:

- **Intent the acquisition never accounted for** is refused by name: a declared
  Analysis with no Acquisition Manifest entry, an entry no Analysis claims, a
  duplicated Analysis or file.
- **A recorded reason there is no usable file** is an *explained* absence: it
  stays in the frozen inventory as a non-ready row and never becomes a release
  member. That is a difference from the retired inventory, which treated a
  missing index as a hard block, not a regression: the pipeline records why.

Run from the repository root:
    python3 tests/freeze/test_ukb_b_reconciliation.py
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from manifest_generation.acquisition_manifest import AcquisitionManifestError
from manifest_generation.freeze import ReconciliationError, freeze
from manifest_generation.providers.ukb_b.source_metadata_adapter import convert
from manifest_generation.providers.ukb_b.write_acquisition_manifest import (
    Evidence,
    write_from_evidence,
)
from manifest_generation.selection import parse_selection
from manifest_generation.source_metadata_table import SourceMetadataTableError

#: The declared membership, in the mirror's id shape.
ANALYSES = ("ukb-b-1", "ukb-b-2", "ukb-b-3")
TRAITS = {
    "ukb-b-1": "Operative procedures - secondary OPCS: Z84.6 Knee joint",
    "ukb-b-2": "Cheese consumers",
    "ukb-b-3": "Standing height",
}
SELECT_ALL = {"all": True}


def write_tsv(path: Path, columns: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    lines = ["\t".join(columns)]
    lines.extend("\t".join(row.get(column, "") for column in columns) for row in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class UkbFreezeFixture(unittest.TestCase):
    """A three-Analysis ukb-b Source Collection, written by the two ukb-b writers."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ukb-b-freeze-")
        self.root = Path(self._tmp.name)
        self.source_dir = self.root / "source"
        self.source_dir.mkdir()
        declared_rows = []
        checksums: dict[str, dict[str, object]] = {}
        integrity: dict[str, dict[str, object]] = {}
        for analysis_id in ANALYSES:
            body = f"fixture {analysis_id}\n".encode()
            data = self.source_dir / f"{analysis_id}.vcf.gz"
            data.write_bytes(body)
            data.with_name(data.name + ".tbi").write_bytes(f"index-{analysis_id}".encode())
            (self.source_dir / f"{analysis_id}.json").write_text(
                json.dumps(
                    {
                        "id": analysis_id.upper(),
                        "trait": TRAITS[analysis_id],
                        "population": "European",
                        "sample_size": 1000,
                        "ncase": "NA",
                        "ncontrol": "NA",
                        "units": "SD",
                        "batch": "UKB-b",
                    }
                ),
                encoding="utf-8",
            )
            declared_rows.append(
                {
                    "analysis_id": analysis_id,
                    "source_analysis_id": analysis_id,
                    "source_label": TRAITS[analysis_id],
                }
            )
            checksums[analysis_id] = {
                "data_file": str(data),
                "data_bytes": len(body),
                "mtime_ns": data.stat().st_mtime_ns,
                "sha256": hashlib.sha256(body).hexdigest(),
            }
            integrity[analysis_id] = {"status": "ok", "detail": "contigs=22"}
        self.declared_selection = self.root / "declared-selection.tsv"
        write_tsv(
            self.declared_selection,
            ("analysis_id", "source_analysis_id", "source_label"),
            declared_rows,
        )
        self.checksums = self.root / "checksums.json"
        self.checksums.write_text(json.dumps({"schema_version": 1, "entries": checksums}), encoding="utf-8")
        self.integrity = self.root / "integrity.json"
        self.integrity.write_text(json.dumps({"schema_version": 1, "entries": integrity}), encoding="utf-8")
        self.verification = self.root / "verify-deep.json"
        self.verification.write_text(json.dumps({"missing": [], "ok": True, "rows": len(ANALYSES)}), encoding="utf-8")
        self.table = self.root / "source-metadata-table.tsv"
        convert(self.declared_selection, self.source_dir, self.table)
        self.manifest = self.root / "acquisition-manifest.tsv"
        self.write_manifest()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_manifest(self, declared: Path | None = None) -> None:
        write_from_evidence(
            declared or self.declared_selection,
            self.source_dir,
            Evidence(self.checksums, self.integrity, self.verification),
            self.manifest,
        )

    def freeze(self):
        return freeze(
            snapshot_id="ukb-b-fixture",
            source_collection_id="opengwas-gwas-vcf",
            metadata_table=self.table,
            acquisition_manifest=self.manifest,
            selection=parse_selection(SELECT_ALL, where="selection"),
            frozen_at="2026-10-22T00:00:00Z",
        )

    def assertFreezeFails(self, error: type[Exception], *fragments: str) -> None:
        with self.assertRaises(error) as caught:
            self.freeze()
        for fragment in fragments:
            self.assertIn(fragment, str(caught.exception))

    def manifest_rows(self) -> list[dict[str, str]]:
        lines = self.manifest.read_text(encoding="utf-8").splitlines()
        header = lines[0].split("\t")
        return [dict(zip(header, line.split("\t"))) for line in lines[1:]]


class UkbFreezeReconciliationTest(UkbFreezeFixture):
    def test_the_three_declared_analyses_freeze_ready(self) -> None:
        frozen = self.freeze()
        self.assertEqual(
            sorted(row.analysis_id for row in frozen.rows), [*ANALYSES]
        )
        self.assertTrue(all(row.ready for row in frozen.rows))

    def test_a_declared_analysis_with_no_manifest_entry_fails(self) -> None:
        # Acquisition covered the first two and never attempted the third: the
        # retired inventory called this "missing", and refused the freeze.
        write_tsv(
            self.root / "partial-selection.tsv",
            ("analysis_id", "source_analysis_id", "source_label"),
            [
                {"analysis_id": analysis_id, "source_analysis_id": analysis_id, "source_label": TRAITS[analysis_id]}
                for analysis_id in ANALYSES[:2]
            ],
        )
        self.write_manifest(self.root / "partial-selection.tsv")
        self.assertFreezeFails(
            ReconciliationError, "no Acquisition Manifest entry", "ukb-b-3"
        )

    def test_a_manifest_entry_no_analysis_claims_fails(self) -> None:
        rows = self.manifest_rows()
        rows.append(
            {
                "source_analysis_id": "ukb-b-404",
                "status": "ok",
                "location": str(self.source_dir / "ukb-b-404.vcf.gz"),
                "bytes": "10",
                "sha256": "a" * 64,
                "sidecar_location": "",
                "sidecar_bytes": "",
                "error": "",
            }
        )
        write_tsv(self.manifest, tuple(rows[0]), rows)
        self.assertFreezeFails(ReconciliationError, "no Analysis", "ukb-b-404")

    def test_a_duplicate_analysis_in_the_source_metadata_table_fails(self) -> None:
        lines = self.table.read_text(encoding="utf-8").splitlines()
        duplicate = lines[1]
        self.table.write_text("\n".join([*lines, duplicate]) + "\n", encoding="utf-8")
        self.assertFreezeFails(SourceMetadataTableError, "duplicate", "ukb-b-1")

    def test_a_duplicate_analysis_in_the_manifest_fails(self) -> None:
        rows = self.manifest_rows()
        repeated = dict(rows[0])
        repeated["location"] = str(self.source_dir / "ukb-b-1-again.vcf.gz")
        rows.append(repeated)
        write_tsv(self.manifest, tuple(rows[0]), rows)
        self.assertFreezeFails(AcquisitionManifestError, "duplicate", "ukb-b-1")

    def test_a_file_claimed_by_two_entries_fails(self) -> None:
        rows = self.manifest_rows()
        rows[1]["location"] = rows[0]["location"]
        write_tsv(self.manifest, tuple(rows[0]), rows)
        self.assertFreezeFails(AcquisitionManifestError, "duplicate", rows[0]["location"])


class UkbExplainedAbsenceTest(UkbFreezeFixture):
    def test_a_missing_index_row_freezes_non_ready_and_is_never_a_member(self) -> None:
        # Acquisition recorded the fact: the VCF is there, its .tbi is gone.
        (self.source_dir / "ukb-b-2.vcf.gz.tbi").unlink()
        self.write_manifest()
        frozen = self.freeze()
        by_id = {row.analysis_id: row for row in frozen.rows}
        self.assertEqual(by_id["ukb-b-2"].readiness_status, "missing_index")
        self.assertFalse(by_id["ukb-b-2"].ready)
        self.assertTrue(by_id["ukb-b-1"].ready)
        self.assertEqual(
            sorted(row.analysis_id for row in frozen.rows if row.ready),
            ["ukb-b-1", "ukb-b-3"],
        )
        # The reason travels into the frozen inventory row, so the absence is
        # auditable rather than silent.
        self.assertIn("ukb-b-2.vcf.gz.tbi", by_id["ukb-b-2"].error)
        self.assertEqual(by_id["ukb-b-2"].data_file, str(self.source_dir / "ukb-b-2.vcf.gz"))


if __name__ == "__main__":
    unittest.main()

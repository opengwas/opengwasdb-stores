#!/usr/bin/env python3
"""Hermetic tests for the ukb-b Source Inventory freeze (issue #219).

Fixture-scale and network-free. They exercise the failure classes the ticket
names explicitly: missing and partial/truncated inputs, duplicates, checksum
mismatch, and exact membership reconciliation against the old built baseline.

Run from the repository root:

    python3 tests/ukb-b/test_ukb_inventory.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "resources" / "generators" / "ukb-b" / "inventory.py"

_spec = importlib.util.spec_from_file_location("ukb_inventory", MODULE_PATH)
assert _spec and _spec.loader
ukb = importlib.util.module_from_spec(_spec)
sys.modules["ukb_inventory"] = ukb
_spec.loader.exec_module(ukb)

DECLARED_COLUMNS = (
    "analysis_id",
    "source_label",
    "source_file",
    "source_genome_build",
)


def write_declared(path: Path, rows: list[tuple[str, str, str, str]]) -> None:
    lines = ["\t".join(DECLARED_COLUMNS)]
    for row in rows:
        lines.append("\t".join(row))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_source(directory: Path, analysis_id: str, *, index: bool = True, content: bytes | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    data = directory / f"{analysis_id}.vcf.gz"
    data.write_bytes(content if content is not None else f"data-{analysis_id}".encode())
    if index:
        data.with_name(data.name + ".tbi").write_bytes(b"index")
    return data


def declared_row(analysis_id: str) -> tuple[str, str, str, str]:
    return (analysis_id, f"label {analysis_id}", f"/declared/{analysis_id}.vcf.gz", "GRCh37")


class UkbInventoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source_dir = self.root / "source"
        self.declared = self.root / "analyses.tsv"
        self.ids = ["ukb-b-1", "ukb-b-2", "ukb-b-3"]

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _exact_fixture(self, ids: list[str] | None = None) -> tuple[ukb.DeclaredSelection, dict[str, ukb.AvailableSource], tuple[str, ...]]:
        ids = ids or self.ids
        write_declared(self.declared, [declared_row(i) for i in ids])
        for analysis_id in ids:
            make_source(self.source_dir, analysis_id)
        selection = ukb.read_declared_selection(self.declared)
        sources, unexpected = ukb.discover_source_collection(self.source_dir)
        return selection, sources, unexpected

    def test_reconcile_exact_and_restored_delta(self) -> None:
        selection, sources, unexpected = self._exact_fixture()
        baseline_path = self.root / "built.tsv"
        baseline_path.write_text("analysis_id\nukb-b-1\nukb-b-2\n", encoding="utf-8")
        baseline = ukb.read_built_baseline(baseline_path, "OGS-00009")
        report = ukb.reconcile(selection, sources, unexpected, baseline)
        self.assertTrue(report.exact)
        self.assertEqual(report.intended, 3)
        self.assertEqual(report.available, 3)
        self.assertEqual(report.built, 2)
        self.assertEqual(report.restored_not_built, ("ukb-b-3",))
        self.assertEqual(report.built_only, ())

    def test_missing_file_is_reported_and_blocks_freeze(self) -> None:
        selection, sources, unexpected = self._exact_fixture()
        (self.source_dir / "ukb-b-2.vcf.gz").unlink()
        sources, unexpected = ukb.discover_source_collection(self.source_dir)
        report = ukb.reconcile(selection, sources, unexpected)
        self.assertFalse(report.exact)
        self.assertEqual(report.missing, ("ukb-b-2",))
        with self.assertRaises(ukb.InventoryError):
            ukb.freeze(
                selection=selection,
                sources=sources,
                unexpected_files=unexpected,
                snapshot_id="fixture",
            )

    def test_missing_index_blocks_freeze(self) -> None:
        selection, sources, unexpected = self._exact_fixture()
        (self.source_dir / "ukb-b-2.vcf.gz.tbi").unlink()
        sources, unexpected = ukb.discover_source_collection(self.source_dir)
        report = ukb.reconcile(selection, sources, unexpected)
        self.assertEqual(report.index_missing, ("ukb-b-2",))
        self.assertFalse(report.exact)
        with self.assertRaises(ukb.InventoryError):
            ukb.freeze(selection=selection, sources=sources, unexpected_files=unexpected, snapshot_id="fixture")

    def test_extra_file_outside_intent_is_an_error_not_a_member(self) -> None:
        selection, sources, unexpected = self._exact_fixture()
        make_source(self.source_dir, "ukb-b-999")
        sources, unexpected = ukb.discover_source_collection(self.source_dir)
        report = ukb.reconcile(selection, sources, unexpected)
        self.assertEqual(report.outside_intent, ("ukb-b-999",))
        self.assertFalse(report.exact)
        with self.assertRaises(ukb.InventoryError):
            ukb.freeze(selection=selection, sources=sources, unexpected_files=unexpected, snapshot_id="fixture")

    def test_unexpected_filename_is_reported_not_parsed(self) -> None:
        selection, sources, unexpected = self._exact_fixture()
        (self.source_dir / "not-a-ukb-id.vcf.gz").write_bytes(b"x")
        sources, unexpected = ukb.discover_source_collection(self.source_dir)
        self.assertIn("not-a-ukb-id.vcf.gz", unexpected)
        report = ukb.reconcile(selection, sources, unexpected)
        self.assertFalse(report.exact)

    def test_duplicate_declared_ids_are_a_defect(self) -> None:
        ids = ["ukb-b-1", "ukb-b-1", "ukb-b-2"]
        write_declared(self.declared, [declared_row(i) for i in ids])
        for analysis_id in ids:
            make_source(self.source_dir, analysis_id)
        selection = ukb.read_declared_selection(self.declared)
        sources, unexpected = ukb.discover_source_collection(self.source_dir)
        report = ukb.reconcile(selection, sources, unexpected)
        self.assertEqual(report.duplicates_in_selection, ("ukb-b-1",))
        self.assertFalse(report.exact)
        with self.assertRaises(ukb.InventoryError):
            ukb.freeze(selection=selection, sources=sources, unexpected_files=unexpected, snapshot_id="fixture")

    def test_freeze_writes_inventory_and_provenance(self) -> None:
        selection, sources, unexpected = self._exact_fixture()
        inventory, report, reused = ukb.freeze(
            selection=selection, sources=sources, unexpected_files=unexpected, snapshot_id="fixture-2026-10-11"
        )
        self.assertEqual(len(inventory.rows), 3)
        self.assertEqual(reused, 0)
        by_id = {row["analysis_id"]: row for row in inventory.rows}
        self.assertEqual(by_id["ukb-b-1"]["checksum_algorithm"], "sha256")
        self.assertEqual(by_id["ukb-b-1"]["readiness_status"], "ok")
        self.assertEqual(inventory.meta["source_collection_id"], "opengwas-gwas-vcf")
        self.assertEqual(inventory.meta["rows"], 3)
        self.assertEqual(inventory.meta["ready_bytes"], sum(Path(r["source_file"]).stat().st_size for r in inventory.rows))
        tsv_path, meta_path = ukb.write_inventory(inventory, self.root / "inventories")
        self.assertTrue(tsv_path.is_file())
        meta = json.loads(json.dumps(inventory.meta))
        self.assertEqual(meta["inventory_tsv_sha256"], ukb.sha256_file(tsv_path))
        self.assertEqual(meta_path.name, "fixture-2026-10-11.meta.yaml")

    def test_duplicate_content_is_surfaced_never_collapsed(self) -> None:
        ids = ["ukb-b-1", "ukb-b-2"]
        write_declared(self.declared, [declared_row(i) for i in ids])
        make_source(self.source_dir, "ukb-b-1", content=b"identical")
        make_source(self.source_dir, "ukb-b-2", content=b"identical")
        selection = ukb.read_declared_selection(self.declared)
        sources, unexpected = ukb.discover_source_collection(self.source_dir)
        inventory, _report, _ = ukb.freeze(
            selection=selection, sources=sources, unexpected_files=unexpected, snapshot_id="fixture"
        )
        groups = inventory.meta["duplicate_content_groups"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["analysis_ids"], ["ukb-b-1", "ukb-b-2"])

    def test_upstream_batch_count_mismatch_blocks_freeze(self) -> None:
        selection, sources, unexpected = self._exact_fixture()
        evidence = self.root / "batches.json"
        evidence.write_text(json.dumps([{"id": "ukb-b", "count": 2524, "description": "x"}]), encoding="utf-8")
        with self.assertRaises(ukb.InventoryError):
            ukb.freeze(
                selection=selection,
                sources=sources,
                unexpected_files=unexpected,
                snapshot_id="fixture",
                upstream_batch_evidence=evidence,
            )

    def test_upstream_batch_count_match_is_recorded(self) -> None:
        selection, sources, unexpected = self._exact_fixture()
        evidence = self.root / "batches.json"
        evidence.write_text(json.dumps([{"id": "ukb-b", "count": 3, "description": "x"}]), encoding="utf-8")
        inventory, _report, _ = ukb.freeze(
            selection=selection,
            sources=sources,
            unexpected_files=unexpected,
            snapshot_id="fixture",
            upstream_batch_evidence=evidence,
        )
        self.assertEqual(inventory.meta["upstream_batch"]["count"], 3)
        self.assertEqual(inventory.meta["upstream_batch"]["evidence_sha256"], ukb.sha256_file(evidence))

    def test_verify_detects_truncation_and_checksum_mismatch(self) -> None:
        selection, sources, unexpected = self._exact_fixture()
        inventory, _report, _ = ukb.freeze(
            selection=selection, sources=sources, unexpected_files=unexpected, snapshot_id="fixture"
        )
        tsv_path, _meta = ukb.write_inventory(inventory, self.root / "inventories")

        clean = ukb.verify_inventory(tsv_path, deep=True)
        self.assertTrue(clean["ok"])

        # Truncated copy: a different size is caught shallow.
        (self.source_dir / "ukb-b-2.vcf.gz").write_bytes(b"x")
        truncated = ukb.verify_inventory(tsv_path, deep=True)
        self.assertFalse(truncated["ok"])
        self.assertEqual(len(truncated["size_mismatch"]), 1)

        # Same-size different content: only --deep catches it.
        (self.source_dir / "ukb-b-2.vcf.gz").write_bytes(b"data-ukb-b-X")
        shallow = ukb.verify_inventory(tsv_path, deep=False)
        self.assertTrue(shallow["ok"])
        deep = ukb.verify_inventory(tsv_path, deep=True)
        self.assertFalse(deep["ok"])
        self.assertEqual(len(deep["checksum_mismatch"]), 1)

    def test_resumable_receipt_reuses_unchanged_checksums(self) -> None:
        _selection, sources, _unexpected = self._exact_fixture()
        receipt = self.root / "receipt.json"
        _identities, reused = ukb.hash_sources(sources, receipt=receipt)
        self.assertEqual(reused, 0)
        self.assertTrue(receipt.is_file())
        _again, reused_again = ukb.hash_sources(sources, receipt=receipt)
        self.assertEqual(reused_again, 3)

        # A changed file invalidates exactly its own reuse.
        target = self.source_dir / "ukb-b-1.vcf.gz"
        target.write_bytes(b"changed-content")
        sources = ukb.discover_source_collection(self.source_dir)[0]
        _third, reused_third = ukb.hash_sources(sources, receipt=receipt)
        self.assertEqual(reused_third, 2)

    def test_reader_thread_cap_is_enforced(self) -> None:
        _selection, sources, _unexpected = self._exact_fixture()
        with self.assertRaises(ukb.InventoryError):
            ukb.hash_sources(sources, workers=3)

    def test_declared_source_file_path_is_not_trusted_as_local(self) -> None:
        # The declared path points at another host's directory; the freeze
        # resolves by id in --source-dir instead.
        selection, sources, unexpected = self._exact_fixture()
        self.assertNotEqual(selection.rows[0].source_file, str(sources["ukb-b-1"].data_file))
        inventory, _report, _ = ukb.freeze(
            selection=selection, sources=sources, unexpected_files=unexpected, snapshot_id="fixture"
        )
        row = next(r for r in inventory.rows if r["analysis_id"] == "ukb-b-1")
        self.assertEqual(row["source_file"], str(sources["ukb-b-1"].data_file))

    def _bgzf_fixture(self, analysis_id: str) -> Path:
        import pysam

        self.source_dir.mkdir(parents=True, exist_ok=True)
        vcf = self.source_dir / f"{analysis_id}.vcf"
        vcf.write_text(
            "##fileformat=VCFv4.2\n##contig=<ID=1,length=1000>\n"
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n1\t100\t.\tA\tC\t.\t.\t.\n",
            encoding="utf-8",
        )
        gz = self.source_dir / f"{analysis_id}.vcf.gz"
        pysam.tabix_compress(str(vcf), str(gz), force=True)
        pysam.tabix_index(str(gz), preset="vcf", force=True)
        return gz

    def test_integrity_ok_then_corrupt_then_missing(self) -> None:
        try:
            import pysam  # noqa: F401
        except ImportError:  # pragma: no cover - env-dependent
            self.skipTest("pysam not available")
        gz = self._bgzf_fixture("ukb-b-1")
        rows = [{"analysis_id": "ukb-b-1", "source_file": str(gz)}]
        result, _reused = ukb.check_source_integrity(rows, workers=1)
        self.assertEqual(result["ukb-b-1"]["status"], "ok")
        self.assertEqual(ukb.report_source_integrity(result)["passed"], True)

        data = gz.read_bytes()
        gz.write_bytes(data[: len(data) // 2])  # truncated BGZF stream
        corrupt, _ = ukb.check_source_integrity(rows, workers=1)
        self.assertEqual(corrupt["ukb-b-1"]["status"], "integrity_failed")
        self.assertFalse(ukb.report_source_integrity(corrupt)["passed"])

        gz.unlink()
        missing, _ = ukb.check_source_integrity(rows, workers=1)
        self.assertEqual(missing["ukb-b-1"]["status"], "missing_file")

    def test_integrity_reader_thread_cap_is_enforced(self) -> None:
        with self.assertRaises(ukb.InventoryError):
            ukb.check_source_integrity([], workers=3)


if __name__ == "__main__":
    unittest.main(verbosity=2)

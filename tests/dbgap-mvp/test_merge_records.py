"""Hermetic tests for wiring resolver records into observed bundle emission."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[1]
_GEN_DIR = _REPO_ROOT / "resources" / "generators" / "dbgap-mvp"
_PILOT_DIR = _GEN_DIR / "pilot"
for path in (_HERE, _PILOT_DIR, str(_GEN_DIR), str(_REPO_ROOT / "src")):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import generate_candidate  # noqa: E402
import merge_records  # noqa: E402
import selection  # noqa: E402
import test_pilot  # noqa: E402
from ogstores import bundle as bundle_module  # noqa: E402


def _write_tsv(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _record(
    analysis_id: str,
    *,
    assigned: str,
    status: str = "success",
    stored_effect_scale: str = "sd",
    original_sd_method: str = "declared_standardised",
    sample_size: str = "1000",
    sd_estimate: float | None = None,
    source_file: str = "",
    source_sha: str = "b" * 64,
    source_bytes: int = 999,
) -> dict:
    return {
        "record_schema_version": 1,
        "analysis_id": analysis_id,
        "status": status,
        "ancestry": {
            "assigned_ancestry": assigned,
            "dominant_superpop": assigned or None,
            "gate_reason": "ok" if assigned else "no_cell_overlap",
            "eaf_orientation": 1.0,
            "af_overlap": 0.9,
        },
        "diagnostics": {"source_file": source_file},
        "fingerprints": {
            "resolution_config": {
                "stored_effect_scale": stored_effect_scale,
                "original_sd_method": original_sd_method,
                "original_effect_scale": stored_effect_scale,
                "sample_size": sample_size,
            },
            "source_recorded_sha256": source_sha,
            "source_file_bytes": source_bytes,
        },
        "phenotype_sd": {
            "status": "skipped" if sd_estimate is None else "estimated",
            "estimate": sd_estimate,
            "reason": "scale_already_declared",
        },
    }


def _write_records(records_dir: Path, records: list[dict]) -> None:
    records_dir.mkdir(parents=True, exist_ok=True)
    for record in records:
        (records_dir / f"{record['analysis_id']}.json").write_text(
            json.dumps(record), encoding="utf-8"
        )


class MergeRecordsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.meta, self.mapping, self.effect, self.candidates = test_pilot.make_pilot_fixture(
            self.tmp
        )
        self.content_sha, _ = generate_candidate.metadata_content.metadata_content_sha256(
            self.meta
        )
        # An immutable source snapshot binds the emit to the frozen metadata.
        snapshot = {
            str(path): {"sha256": generate_candidate.sha256_file(path), "size": path.stat().st_size}
            for path in sorted((self.meta / "manifests").glob("*.tsv"))
            + [self.meta / "refresh-receipt.tsv"]
        }
        (self.meta / "input-metadata-before.json").write_text(
            json.dumps(snapshot), encoding="utf-8"
        )
        self.config_path = self.tmp / "config.yaml"
        self.config_path.write_text(
            yaml.safe_dump(self._config(), sort_keys=False), encoding="utf-8"
        )
        self.selection_rows = selection.build_selection(
            metadata_root=self.meta,
            mapping_path=self.mapping,
            metadata_content_sha256=self.content_sha,
            candidate_table_path=self.candidates,
            scope="bucket:EAS",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _config(self) -> dict:
        manifest = {
            bucket: f"{bucket.lower()}.tsv" for bucket in test_pilot.PILOT_META
        }
        stores = {
            bucket: {
                "observed_store_id": store_id,
                "completed_store_id": None,
                "candidate_store_key": f"dense__fixture__{bucket}",
                "label": f"fixture-{bucket.lower()}",
                "assigned_ancestry": None if bucket == "META" else bucket,
                "source_ancestry_label": bucket,
                "intended_members": len(test_pilot.PILOT_META[bucket]),
                "planned_manifest": manifest[bucket],
                "resolution": "pending",
                "completion_reference": {
                    "resource_id": None,
                    "declared_status": "absent",
                    "qualification": "hold",
                    "activation": "blocked",
                    "suggested_ld_panel": None,
                },
            }
            for bucket, store_id in (
                ("EUR", "OGS-00012"),
                ("AFR", "OGS-00013"),
                ("AMR", "OGS-00014"),
                ("EAS", "OGS-00015"),
                ("META", "OGS-00016"),
            )
        }
        return {
            "publication_pmid": "39024449",
            "source_snapshot_id": "fixture",
            "generator_commands": ["python3 resources/generators/dbgap-mvp/generate_candidate.py"],
            "metadata": {
                "root": str(self.meta),
                "code_sha": test_pilot.FIXTURE_CODE_SHA,
                "output_metadata_content_sha256": self.content_sha,
                "membership": len(test_pilot.ALL_IDS),
                "candidates_table": str(self.candidates),
            },
            "partition_expected": {
                bucket: len(entries) for bucket, entries in test_pilot.PILOT_META.items()
            },
            "stores": stores,
        }

    def _eas_records(self) -> list[dict]:
        manifest_rows = {
            row["analysis_id"]: row
            for row in selection.read_tsv(self.meta / "manifests" / "eas.tsv")
        }
        records: list[dict] = []
        for row in self.selection_rows:
            manifest = manifest_rows[row.analysis_id]
            case_control = row.stored_effect_scale == "log_or"
            records.append(
                _record(
                    row.analysis_id,
                    assigned="EAS",
                    stored_effect_scale=row.stored_effect_scale,
                    original_sd_method=row.original_sd_method,
                    sample_size=row.mapping["sample_size"],
                    sd_estimate=None if case_control else 1.25,
                    source_file=manifest["data_file"],
                    source_sha=manifest["sha256"],
                    source_bytes=int(manifest["data_bytes"]),
                )
            )
        return records

    def test_merge_maps_records_to_canonical_rows(self) -> None:
        records_dir = self.tmp / "records"
        _write_records(records_dir, self._eas_records())
        result = merge_records.merge_records(records_dir, self.selection_rows)
        self.assertEqual(len(result.rows), 3)
        self.assertEqual(result.report["pending_provenance"], 0)
        by_id = {row["analysis_id"]: row for row in result.rows}
        for row in self.selection_rows:
            canonical = by_id[row.analysis_id]
            self.assertEqual(canonical["assigned_ancestry"], "EAS")
            self.assertEqual(canonical["ancestry_assignment_method"], "af_assigned")
            self.assertEqual(canonical["stored_effect_scale"], row.stored_effect_scale)
            self.assertEqual(canonical["original_sd_method"], row.original_sd_method)
            self.assertEqual(canonical["sample_size"], row.mapping["sample_size"])
            self.assertEqual(canonical["source_analysis_id"], row.analysis_id)
            self.assertTrue(canonical["source_file"])
        case_control = by_id["GCST90475203"]
        self.assertEqual(case_control["sample_size_kind"], "case_control")
        self.assertEqual(case_control["original_sd"], "")
        quantitative = by_id["GCST90475346"]
        self.assertEqual(quantitative["sample_size_kind"], "total")
        self.assertEqual(quantitative["original_sd"], "1.25")

    def test_merge_refuses_missing_record(self) -> None:
        records_dir = self.tmp / "records"
        records = self._eas_records()
        _write_records(records_dir, records[:-1])
        with self.assertRaises(merge_records.MergeRecordsError) as ctx:
            merge_records.merge_records(records_dir, self.selection_rows)
        self.assertIn("incomplete", str(ctx.exception))

    def test_merge_refuses_non_success_record(self) -> None:
        records_dir = self.tmp / "records"
        records = self._eas_records()
        records[0]["status"] = "controlled_failure"
        _write_records(records_dir, records)
        with self.assertRaises(merge_records.MergeRecordsError):
            merge_records.merge_records(records_dir, self.selection_rows)

    def test_unassigned_stays_unassigned(self) -> None:
        records_dir = self.tmp / "records"
        records = self._eas_records()
        records[0]["ancestry"]["assigned_ancestry"] = ""
        records[0]["ancestry"]["gate_reason"] = "no_cell_overlap"
        _write_records(records_dir, records)
        result = merge_records.merge_records(records_dir, self.selection_rows)
        by_id = {row["analysis_id"]: row for row in result.rows}
        unassigned = by_id[records[0]["analysis_id"]]
        self.assertEqual(unassigned["assigned_ancestry"], "")
        self.assertEqual(unassigned["ancestry_assignment_method"], "unassigned")
        # The registry gate preserves the explicit unassigned value...
        generate_candidate.validate_resolution_row(
            unassigned, "EAS", "EAS", None
        )
        # ...but still rejects a non-empty cross-ancestry mismatch.
        leaked = dict(unassigned)
        leaked["assigned_ancestry"] = "AFR"
        with self.assertRaises(generate_candidate.CandidateError):
            generate_candidate.validate_resolution_row(leaked, "EAS", "EAS", None)

    def test_emit_with_partial_resolution_refuses(self) -> None:
        records_dir = self.tmp / "records"
        _write_records(records_dir, self._eas_records()[:-1])
        out_dir = self.tmp / "out"
        resolution = out_dir / "resolution.tsv"
        resolution.parent.mkdir(parents=True, exist_ok=True)
        resolution.write_text("analysis_id\n", encoding="utf-8")
        with self.assertRaises(generate_candidate.CandidateError):
            generate_candidate.run_emit(
                generate_candidate.load_config(self.config_path),
                self.mapping,
                resolution,
                out_dir / "bundles",
                created_at="2026-01-01T00:00:00+00:00",
                only_buckets=["EAS"],
            )

    def test_merge_then_emit_eas_bundle(self) -> None:
        records_dir = self.tmp / "records"
        _write_records(records_dir, self._eas_records())
        result = merge_records.merge_records(records_dir, self.selection_rows)
        out_dir = self.tmp / "out"
        resolution = out_dir / "merge" / "resolution.tsv"
        resolution.parent.mkdir(parents=True, exist_ok=True)
        resolution.write_text(
            merge_records.render_resolution_tsv(result.rows), encoding="utf-8"
        )
        emitted = generate_candidate.run_emit(
            generate_candidate.load_config(self.config_path),
            self.mapping,
            resolution,
            out_dir / "bundles",
            created_at="2026-01-01T00:00:00+00:00",
            only_buckets=["EAS"],
        )
        self.assertEqual(emitted["emitted"], {"EAS": "OGS-00015"})
        self.assertEqual(emitted["checked"], ["OGS-00015"])
        bundle_dir = out_dir / "bundles" / "OGS-00015"
        rows = selection.read_tsv(bundle_dir / "analyses.tsv")
        self.assertEqual(len(rows), 3)
        self.assertEqual({row["assigned_ancestry"] for row in rows}, {"EAS"})
        self.assertEqual(
            {row["ancestry_assignment_method"] for row in rows}, {"af_assigned"}
        )
        release = yaml.safe_load((bundle_dir / "release.yaml").read_text())
        self.assertEqual(release["status"], "accepted")
        self.assertEqual(release["accepted_at"], "2026-01-01T00:00:00+00:00")
        self.assertIn("not scientific release acceptance", release["notes"])
        loaded = bundle_module.load("OGS-00015", registry_root=out_dir / "bundles")
        self.assertTrue(bundle_module.is_buildable(loaded))
        buildable, refusals = bundle_module.partition_buildable([loaded])
        self.assertEqual([b.store_id for b in buildable], ["OGS-00015"])
        self.assertEqual(refusals, [])
        self.assertEqual(
            list(bundle_module.check(loaded, registry_root=out_dir / "bundles")), []
        )

    def test_emit_can_select_only_one_bucket(self) -> None:
        records_dir = self.tmp / "records"
        _write_records(records_dir, self._eas_records())
        result = merge_records.merge_records(records_dir, self.selection_rows)
        out_dir = self.tmp / "out"
        resolution = out_dir / "merge" / "resolution.tsv"
        resolution.parent.mkdir(parents=True, exist_ok=True)
        resolution.write_text(
            merge_records.render_resolution_tsv(result.rows), encoding="utf-8"
        )
        with self.assertRaises(generate_candidate.CandidateError):
            generate_candidate.run_emit(
                generate_candidate.load_config(self.config_path),
                self.mapping,
                resolution,
                out_dir / "bundles",
                created_at="2026-01-01T00:00:00+00:00",
                only_buckets=["BOGUS"],
            )
    def test_cli_writes_resolution_and_merge_receipt(self) -> None:
        records_dir = self.tmp / "records"
        _write_records(records_dir, self._eas_records())
        out_dir = self.tmp / "cli-out"
        out_dir.mkdir(parents=True, exist_ok=True)
        rc = merge_records.main(
            [
                "--scope",
                "bucket:EAS",
                "--records-dir",
                str(records_dir),
                "--metadata-root",
                str(self.meta),
                "--mapping",
                str(self.mapping),
                "--out-dir",
                str(out_dir),
            ]
        )
        self.assertEqual(rc, 0)
        resolution = out_dir / "merge" / "resolution.tsv"
        self.assertTrue(resolution.is_file())
        rows = selection.read_tsv(resolution)
        self.assertEqual(len(rows), 3)
        receipt = json.loads((out_dir / "merge" / "merge-receipt.json").read_text())
        self.assertEqual(receipt["resolved_records"], 3)
        self.assertEqual(receipt["missing_records"], [])

    def test_cli_refuses_to_emit_incomplete_records(self) -> None:
        records_dir = self.tmp / "records"
        _write_records(records_dir, self._eas_records()[:-1])
        out_dir = self.tmp / "cli-out"
        out_dir.mkdir(parents=True, exist_ok=True)
        rc = merge_records.main(
            [
                "--scope",
                "bucket:EAS",
                "--records-dir",
                str(records_dir),
                "--metadata-root",
                str(self.meta),
                "--mapping",
                str(self.mapping),
                "--out-dir",
                str(out_dir),
            ]
        )
        self.assertEqual(rc, 2)
        self.assertFalse((out_dir / "merge" / "resolution.tsv").exists())


if __name__ == "__main__":
    unittest.main()

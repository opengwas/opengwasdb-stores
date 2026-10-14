#!/usr/bin/env python3
"""Hermetic tests for the restored ukb-b candidate emitter (issue #219).

Fixture-scale and network-free. They pin the contracts the ticket names:
exact membership reconciliation, identity (path/size/sha256) taken from the
frozen inventory rather than the resolution table, preserved meaningful
exclusions, and refusal to emit a schema-invalid or duplicate bundle. How the
bundle lands in the registry is the shared emitter's, tested in tests/emit.

Run from the repository root:

    python3 tests/ukb-b/test_ukb_generate_candidate.py
"""

from __future__ import annotations

import csv
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


import yaml  # noqa: E402

from ogstores import bundle  # noqa: E402

ukb_inventory = _load("ukb_inventory_test", REPO_ROOT / "resources" / "generators" / "ukb-b" / "inventory.py")
candidate = _load("ukb_generate_candidate_test", REPO_ROOT / "resources" / "generators" / "ukb-b" / "generate_candidate.py")

INVENTORY_COLUMNS = ukb_inventory.INVENTORY_COLUMNS


def write_tsv(path: Path, columns, rows) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(columns), delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def make_source(directory: Path, analysis_id: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{analysis_id}.vcf.gz"
    path.write_bytes(f"content-{analysis_id}".encode())
    return path


def inventory_row(source: Path, analysis_id: str) -> dict[str, str]:
    stat = source.stat()
    return {
        "analysis_id": analysis_id,
        "source_label": f"label {analysis_id}",
        "source_genome_build": "GRCh37",
        "source_file": str(source),
        "data_bytes": str(stat.st_size),
        "sha256": ukb_inventory.sha256_file(source),
        "checksum_algorithm": "sha256",
        "mtime_ns": str(stat.st_mtime_ns),
        "index_file": str(source) + ".tbi",
        "index_status": "ok",
        "readiness_status": "ok",
        "exclusion_reason": "",
    }


def resolution_row(analysis_id: str, *, excluded: bool = False) -> dict[str, str]:
    row = {name: "" for name in candidate.ANALYSES_COLUMNS}
    row.update(
        {
            "analysis_id": analysis_id,
            "source_analysis_id": analysis_id,
            "source_label": f"label {analysis_id}",
            "analysis_label": f"label {analysis_id}",
            "trait_ontology_mapping_method": "unmapped",
            # A recorded resolver path from another host must NOT survive into the bundle.
            "source_file": f"/local-scratch/opengwas/igd/{analysis_id}/{analysis_id}.vcf.gz",
            "original_effect_scale": "sd",
            "original_sd_method": "declared_standardised",
            "stored_effect_scale": "sd",
            "sample_size_kind": "case_control",
            "sample_size_scope": "analysis_level",
            "sample_size": "1000",
            "n_cases": "100",
            "n_controls": "900",
            "inclusion_reason": "declared intent",
        }
    )
    if excluded:
        row["exclude_from_build"] = "true"
        row["inclusion_reason"] = "declared intent; excluded_reason: no gwasinfo record"
    return row


def base_config() -> dict:
    return {
        "label": "ukb-b-test",
        "description": "fixture candidate",
        "access_posture": "public",
        "source_snapshot_id": "fixture",
        "source_collection_id": "opengwas-gwas-vcf",
        "resolution_snapshot_id": "fixture-resolution",
        "build_options": {"source-reader-capability": "opengwasdb.gwas-vcf", "source-assembly": "hg19", "n-workers": 1},
        "post": {"top_hits": True, "overview": True},
        "defaults": {
            "license": "UK Biobank terms of access",
            "consortium": "MRC-IEU",
            "first_author": "Ben Elsworth",
            "analysis_group_id": "ukb-b",
            "source_genome_build": "GRCh37",
            "source_ancestry_label": "European",
            "assigned_ancestry": "EUR",
            "ancestry_assignment_method": "source_fallback",
        },
    }


class UkbCandidateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source_dir = self.root / "source"
        self.ids = ["ukb-b-1", "ukb-b-2", "ukb-b-3"]
        self.sources = {i: make_source(self.source_dir, i) for i in self.ids}
        self.inventory = [inventory_row(self.sources[i], i) for i in self.ids]
        self.declared = {
            row["analysis_id"]: {"analysis_id": row["analysis_id"], "source_label": row["source_label"], "source_genome_build": "GRCh37", "inclusion_reason": "declared intent"}
            for row in self.inventory
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _build(self, resolution=None, ancestry=None):
        resolution = resolution if resolution is not None else {i: resolution_row(i) for i in self.ids}
        return candidate.build_analyses_rows(
            inventory=self.inventory,
            resolution=resolution,
            declared=self.declared,
            config=base_config(),
            ancestry_records=ancestry,
        )

    def test_exact_membership_and_identity_from_inventory(self) -> None:
        rows, warnings = self._build()
        self.assertEqual([r["analysis_id"] for r in rows], self.ids)
        self.assertEqual(warnings, [])
        candidate.validate_membership(rows, self.inventory)
        for row in rows:
            inv = next(i for i in self.inventory if i["analysis_id"] == row["analysis_id"])
            self.assertEqual(row["source_file"], inv["source_file"])
            self.assertEqual(row["checksum"], inv["sha256"])
            self.assertEqual(row["size_bytes"], inv["data_bytes"])
            self.assertNotIn("/local-scratch/", row["source_file"])
            self.assertEqual(row["assigned_ancestry"], "EUR")
            self.assertEqual(row["ancestry_assignment_method"], "source_fallback")

    def test_missing_resolution_record_is_an_explicit_exclusion(self) -> None:
        resolution = {i: resolution_row(i) for i in self.ids if i != "ukb-b-2"}
        rows, warnings = self._build(resolution=resolution)
        candidate.validate_membership(rows, self.inventory)
        excluded = [r for r in rows if r["exclude_from_build"] == "true"]
        self.assertEqual([r["analysis_id"] for r in excluded], ["ukb-b-2"])
        self.assertIn("no recorded resolution record", excluded[0]["inclusion_reason"])
        self.assertEqual(len(warnings), 1)

    def test_recorded_exclusion_is_preserved(self) -> None:
        resolution = {i: resolution_row(i) for i in self.ids}
        resolution["ukb-b-3"] = resolution_row("ukb-b-3", excluded=True)
        rows, warnings = self._build(resolution=resolution)
        row = next(r for r in rows if r["analysis_id"] == "ukb-b-3")
        self.assertEqual(row["exclude_from_build"], "true")
        self.assertIn("no gwasinfo record", row["inclusion_reason"])
        self.assertEqual(len(warnings), 1)

    def test_resolution_record_outside_inventory_is_refused(self) -> None:
        resolution = {i: resolution_row(i) for i in self.ids}
        resolution["ukb-b-999"] = resolution_row("ukb-b-999")
        with self.assertRaises(candidate.CandidateError):
            self._build(resolution=resolution)

    def test_ancestry_records_override_the_batch_fallback(self) -> None:
        ancestry = {"ukb-b-1": {"analysis_id": "ukb-b-1", "assigned_ancestry": "AFR", "ancestry_assignment_method": "af_assigned"}}
        rows, _ = self._build(ancestry=ancestry)
        row = next(r for r in rows if r["analysis_id"] == "ukb-b-1")
        self.assertEqual(row["assigned_ancestry"], "AFR")
        self.assertEqual(row["ancestry_assignment_method"], "af_assigned")
        other = next(r for r in rows if r["analysis_id"] == "ukb-b-2")
        self.assertEqual(other["ancestry_assignment_method"], "source_fallback")

    def test_validate_membership_rejects_a_leaked_identity(self) -> None:
        rows, _ = self._build()
        rows[0]["source_file"] = "/local-scratch/elsewhere.vcf.gz"
        with self.assertRaises(candidate.CandidateError):
            candidate.validate_membership(rows, self.inventory)

    def test_validate_membership_rejects_a_mismatched_checksum(self) -> None:
        rows, _ = self._build()
        rows[0]["checksum"] = "0" * 64
        with self.assertRaises(candidate.CandidateError):
            candidate.validate_membership(rows, self.inventory)

    def _write_inputs(self) -> Path:
        """The frozen inventory, resolution, declared selection and config, as main() reads them."""
        inventory_p = self.root / "inventory.tsv"
        write_tsv(inventory_p, INVENTORY_COLUMNS, self.inventory)
        resolution = [resolution_row(i) for i in self.ids]
        resolution_p = self.root / "resolution.tsv"
        write_tsv(resolution_p, list(resolution[0]), resolution)
        declared = list(self.declared.values())
        declared_p = self.root / "declared.tsv"
        write_tsv(declared_p, list(declared[0]), declared)
        config_p = self.root / "config.yaml"
        config_p.write_text(
            yaml.safe_dump(
                {**base_config(), "inventory": str(inventory_p), "resolution": str(resolution_p), "declared": str(declared_p)}
            ),
            encoding="utf-8",
        )
        return config_p

    def test_main_emits_the_candidate_and_re_emits_over_it(self) -> None:
        config_p = self._write_inputs()
        registry = self.root / "stores"
        argv = ["OGS-99001", "--config", str(config_p), "--registry-root", str(registry), "--created-at", "2026-10-12T00:00:00Z"]

        self.assertEqual(candidate.main(argv), 0)
        loaded = bundle.load("OGS-99001", registry_root=registry)
        self.assertEqual(list(bundle.check(loaded, registry_root=registry)), [])
        self.assertEqual(loaded.status, "candidate")
        self.assertEqual(
            loaded.release["generator"]["commands"],
            ["python3 resources/generators/ukb-b/generate_candidate.py " + " ".join(argv)],
        )

        # A candidate is re-emitted in place, not refused (#248).
        self.assertEqual(candidate.main(argv), 0)
        self.assertEqual(bundle.load("OGS-99001", registry_root=registry).status, "candidate")

    def test_shared_schema_skips_excluded_rows(self) -> None:
        resolution = {i: resolution_row(i) for i in self.ids}
        resolution["ukb-b-2"] = resolution_row("ukb-b-2", excluded=True)
        rows, _ = self._build(resolution=resolution)
        # an excluded row may legitimately carry blank required metadata
        excluded = next(r for r in rows if r["analysis_id"] == "ukb-b-2")
        excluded["stored_effect_scale"] = ""
        candidate._validate_shared_schema(rows)

    def _af(self, analysis_id: str) -> dict[str, str]:
        return {"analysis_id": analysis_id, "assigned_ancestry": "EUR", "ancestry_assignment_method": "af_assigned"}

    def test_ancestry_validation_full_af_coverage_passes(self) -> None:
        records = {i: self._af(i) for i in self.ids}
        ok, warnings = candidate.ancestry_validation(self.ids, records)
        self.assertTrue(ok)
        self.assertEqual(warnings, [])

    def test_ancestry_validation_partial_coverage_is_not_run(self) -> None:
        records = {i: self._af(i) for i in self.ids if i != "ukb-b-2"}
        ok, warnings = candidate.ancestry_validation(self.ids, records)
        self.assertFalse(ok)
        self.assertEqual(len(warnings), 1)
        self.assertIn("not fully AF-assigned", warnings[0])
        self.assertIn("1 of 3", warnings[0])

    def test_ancestry_validation_source_fallback_is_not_run(self) -> None:
        records = {i: self._af(i) for i in self.ids}
        records["ukb-b-3"] = {"analysis_id": "ukb-b-3", "assigned_ancestry": "EUR", "ancestry_assignment_method": "source_fallback"}
        ok, warnings = candidate.ancestry_validation(self.ids, records)
        self.assertFalse(ok)
        self.assertIn("non-af_assigned or unassigned", warnings[0])

    def test_ancestry_validation_unassigned_is_not_run(self) -> None:
        records = {i: self._af(i) for i in self.ids}
        records["ukb-b-3"] = {"analysis_id": "ukb-b-3", "assigned_ancestry": "", "ancestry_assignment_method": "unassigned"}
        ok, _warnings = candidate.ancestry_validation(self.ids, records)
        self.assertFalse(ok)

    def test_ancestry_validation_no_records_is_not_run(self) -> None:
        ok, warnings = candidate.ancestry_validation(self.ids, None)
        self.assertFalse(ok)
        self.assertIn("source_fallback", warnings[0])

    def test_ancestry_records_outside_inventory_are_rejected(self) -> None:
        records = {i: self._af(i) for i in self.ids}
        records["ukb-b-999"] = self._af("ukb-b-999")
        with self.assertRaises(candidate.CandidateError):
            self._build(ancestry=records)

    def test_render_validation_only_passes_ancestry_when_checked(self) -> None:
        passed = candidate.render_validation_yaml(
            n_rows=3, n_buildable=3, warnings=[], ancestry_checked=True, generated_at="t"
        )
        self.assertEqual(passed["checks"]["ancestry"], "passed")
        not_run = candidate.render_validation_yaml(
            n_rows=3, n_buildable=3, warnings=["w"], ancestry_checked=False, generated_at="t", reports={"ancestry": "x.tsv"}
        )
        self.assertEqual(not_run["checks"]["ancestry"], "not_run")
        self.assertEqual(not_run["reports"], {"ancestry": "x.tsv"})

    def test_release_yaml_records_ancestry_records_provenance_when_supplied(self) -> None:
        config = base_config()
        identity = {"path": "ancestry.tsv", "sha256": "12" * 32, "rows": 3, "admitted_rows": 3}
        release = candidate.render_release_yaml(
            store_id="OGS-99001",
            config=config,
            inventory_path=Path("inventory.tsv"),
            inventory_sha256="ab" * 32,
            resolution_path=Path("resolution.tsv"),
            resolution_sha256="cd" * 32,
            n_rows=3,
            n_buildable=3,
            generator_version="sha256:" + "ef" * 32,
            created_at="2026-10-12T00:00:00Z",
            ancestry_records_identity=identity,
        )
        self.assertEqual(release["source_snapshot"]["ancestry_records"], identity)
        without = candidate.render_release_yaml(
            store_id="OGS-99001",
            config=config,
            inventory_path=Path("inventory.tsv"),
            inventory_sha256="ab" * 32,
            resolution_path=Path("resolution.tsv"),
            resolution_sha256="cd" * 32,
            n_rows=3,
            n_buildable=3,
            generator_version="sha256:" + "ef" * 32,
            created_at="2026-10-12T00:00:00Z",
        )
        self.assertNotIn("ancestry_records", without["source_snapshot"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

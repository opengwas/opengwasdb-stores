#!/usr/bin/env python3
"""Release Bundle and paths tests (issue #108).

Validates:
  1. All seven committed bundles (OGS-00001..OGS-00007) load and pass check().
  2. Each failure class is covered:
     - missing key (in release.yaml, build.yaml, analyses.tsv)
     - id/directory mismatch
     - malformed id (including trailing newlines)
     - absent declared file (analyses.tsv, declared sidecar)
     - bad checksum (source_snapshot sha256, generator version sha256, analyses.tsv hex)
     - unresolvable derived_from (reference_completed + observed_only, self-referential, malformed, non-existent)
     - illegal status transition (tested via check(bundle, previous_status=...))
     - malformed YAML syntax in bundle files
  3. analyses.tsv validation delegates to opengwasdb.model.analyses (no restated vocabulary, public API only).
  4. Candidate releases with no validation.yaml and unresolved rows pass.
  5. BESD / complete-ragged probe overlays with blank shared-core values pass, while vocabulary errors still fail.
  6. check() and load() open registry files only and never touch artifact paths, inspect stores, or call artifact helpers.
  7. Every artifact path in paths.py is a pure function of the Store Release id.

Run from repository root:
    pixi run python tests/bundle/test_bundle.py
"""

from __future__ import annotations

import builtins
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

import ogstores.bundle
from ogstores import bundle, paths

n_checks = 0


def record_check() -> None:
    global n_checks
    n_checks += 1


class TestBundleAndPaths(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="ogstores_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # AC1: All seven bundles load and pass check()
    # -------------------------------------------------------------------------
    def test_all_seven_bundles_load_and_pass(self) -> None:
        for i in range(1, 8):
            store_id = f"OGS-{i:05d}"
            b = bundle.load(store_id, registry_root=REPO_ROOT / "stores")
            record_check()
            self.assertEqual(b.store_id, store_id)
            self.assertEqual(b.root, REPO_ROOT / "stores" / store_id)
            self.assertIsInstance(b.release, dict)
            self.assertIsInstance(b.build, dict)
            self.assertTrue(b.analyses_path.is_file(), f"{store_id} analyses.tsv exists")

            errors = bundle.check(b, registry_root=REPO_ROOT / "stores")
            record_check()
            self.assertEqual(
                errors,
                [],
                f"Expected {store_id} to pass bundle.check(), but got errors: {errors}",
            )

    # -------------------------------------------------------------------------
    # AC2: Failure classes
    # -------------------------------------------------------------------------
    def _create_temp_bundle_dir(
        self,
        store_id: str = "OGS-00099",
        release_override: dict | None = None,
        build_override: dict | None = None,
        analyses_content: str | None = None,
        sidecars: dict[str, str] | None = None,
        write_validation: bool = True,
        raw_release_yaml: str | None = None,
        raw_build_yaml: str | None = None,
    ) -> Path:
        """Helper to create a temporary bundle directory for failure testing."""
        bundle_dir = self.tmp_dir / store_id
        bundle_dir.mkdir(parents=True, exist_ok=True)

        if raw_release_yaml is not None:
            (bundle_dir / "release.yaml").write_text(raw_release_yaml, encoding="utf-8")
        else:
            rel_data = {
                "store_id": store_id,
                "label": "test-pilot",
                "family": "test-family",
                "status": "built",
                "source_collection_id": "test-source",
                "association_coverage": "full_gwas",
                "derived_from": None,
                "created_at": "2026-09-01T00:00:00Z",
                "description": "Test release bundle",
                "source_snapshot_id": "test-snapshot-v1",
                "release_kind": "one-off",
                "generator": {
                    "name": "resources/generators/test/generate.py",
                    "command": "python3 generate.py",
                },
            }
            if release_override:
                for k, v in release_override.items():
                    if v is None and k in rel_data:
                        del rel_data[k]
                    else:
                        rel_data[k] = v

            if sidecars:
                rel_data["sidecars"] = sidecars
                for name, rel_path in sidecars.items():
                    sp = bundle_dir / rel_path
                    sp.parent.mkdir(parents=True, exist_ok=True)
                    sp.write_text(f"# dummy sidecar {name}\n", encoding="utf-8")

            with open(bundle_dir / "release.yaml", "w", encoding="utf-8") as f:
                yaml.safe_dump(rel_data, f)

        if raw_build_yaml is not None:
            (bundle_dir / "build.yaml").write_text(raw_build_yaml, encoding="utf-8")
        else:
            build_data = {
                "store_id": store_id,
                "layout": "dense",
                "completion_state": "observed_only",
                "build": {
                    "command": "build-dense-vcf",
                    "options": {"source-assembly": "hg38"},
                },
                "post": {"top_hits": True, "rho": False, "overview": True, "validate": True},
                "artifacts": {"root": "/data/opengwasdb/stores"},
            }
            if build_override:
                for k, v in build_override.items():
                    if v is None and k in build_data:
                        del build_data[k]
                    else:
                        build_data[k] = v

            with open(bundle_dir / "build.yaml", "w", encoding="utf-8") as f:
                yaml.safe_dump(build_data, f)

        if analyses_content is None:
            analyses_content = (
                "analysis_id\tsource_analysis_id\tsource_label\tanalysis_label\t"
                "trait_ontology_label\ttrait_ontology_id\ttrait_ontology_mapping_method\t"
                "source_file\tchecksum\tchecksum_algorithm\tsource_genome_build\t"
                "license\tassigned_ancestry\tancestry_assignment_method\t"
                "original_effect_scale\toriginal_sd\toriginal_sd_method\t"
                "stored_effect_scale\tsample_size_kind\tsample_size_scope\tsample_size\t"
                "n_cases\tn_controls\n"
                "TEST_001\tsrc_001\tTrait 1\tTrait 1\tEFO\tEFO:0001\tsource_provided\t"
                "/data/src/test1.tsv\t" + "a" * 64 + "\tsha256\thg38\t"
                "Open Access\tEUR\taf_assigned\tsd\t1.0\tdeclared_standardised\t"
                "sd\ttotal\tanalysis_level\t10000\t\t\n"
            )

        if analyses_content != "__OMIT__":
            with open(bundle_dir / "analyses.tsv", "w", encoding="utf-8") as f:
                f.write(analyses_content)

        if write_validation:
            val_data = {
                "status": "passed",
                "checks": {"schema": "passed", "files": "passed"},
            }
            with open(bundle_dir / "validation.yaml", "w", encoding="utf-8") as f:
                yaml.safe_dump(val_data, f)

        return bundle_dir

    def test_failure_missing_keys(self) -> None:
        # Missing key in release.yaml
        for req_key in ("family", "label", "status", "source_collection_id", "association_coverage", "description", "generator"):
            b_dir = self._create_temp_bundle_dir(store_id="OGS-00010", release_override={req_key: None})
            b = bundle.load("OGS-00010", registry_root=self.tmp_dir)
            errs = bundle.check(b, registry_root=self.tmp_dir)
            record_check()
            self.assertTrue(
                any(req_key in e and "missing" in e for e in errs),
                f"Expected error for missing release.yaml key {req_key!r}, got: {errs}",
            )
            shutil.rmtree(b_dir)

        # Missing key in build.yaml
        for req_key in ("layout", "completion_state", "artifacts", "post"):
            b_dir = self._create_temp_bundle_dir(store_id="OGS-00011", build_override={req_key: None})
            b = bundle.load("OGS-00011", registry_root=self.tmp_dir)
            errs = bundle.check(b, registry_root=self.tmp_dir)
            record_check()
            self.assertTrue(
                any(req_key in e and "missing" in e for e in errs),
                f"Expected error for missing build.yaml key {req_key!r}, got: {errs}",
            )
            shutil.rmtree(b_dir)

        # Missing Phase B columns in analyses.tsv
        for col in bundle.PHASE_B_REQUIRED_COLUMNS:
            content = (
                "analysis_id\tsample_size_kind\tsample_size_scope\tsample_size\toriginal_effect_scale\n"
                "TEST_001\ttotal\tanalysis_level\t10000\tsd\n"
            )
            b_dir = self._create_temp_bundle_dir(store_id="OGS-00012", analyses_content=content)
            b = bundle.load("OGS-00012", registry_root=self.tmp_dir)
            errs = bundle.check(b, registry_root=self.tmp_dir)
            record_check()
            self.assertTrue(
                any(col in e and "Phase B" in e for e in errs),
                f"Expected error for missing Phase B column {col!r}, got: {errs}",
            )
            shutil.rmtree(b_dir)

    def test_failure_id_directory_mismatch(self) -> None:
        # Directory is OGS-00013 but release.yaml says OGS-00014
        b_dir = self._create_temp_bundle_dir(store_id="OGS-00013", release_override={"store_id": "OGS-00014"})
        b = bundle.load("OGS-00013", registry_root=self.tmp_dir)
        errs = bundle.check(b, registry_root=self.tmp_dir)
        record_check()
        self.assertTrue(
            any("store_id" in e and "does not match" in e for e in errs),
            f"Expected error for release.yaml store_id mismatch, got: {errs}",
        )
        shutil.rmtree(b_dir)

        # Directory is OGS-00015 but build.yaml says OGS-00016
        b_dir = self._create_temp_bundle_dir(store_id="OGS-00015", build_override={"store_id": "OGS-00016"})
        b = bundle.load("OGS-00015", registry_root=self.tmp_dir)
        errs = bundle.check(b, registry_root=self.tmp_dir)
        record_check()
        self.assertTrue(
            any("store_id" in e and "does not match" in e for e in errs),
            f"Expected error for build.yaml store_id mismatch, got: {errs}",
        )
        shutil.rmtree(b_dir)

    def test_failure_malformed_id(self) -> None:
        malformed_ids = [
            "OGS-1",
            "OGS-0001",
            "OGS-000001",
            "OGS-ABCDE",
            "finngen-r13-pilot-20",
            "ogs-00001",
            "OGS_00001",
            "OGS-00001\n",
            "OGS-00001 ",
            " OGS-00001",
            "",
        ]
        for bad_id in malformed_ids:
            record_check()
            self.assertFalse(
                paths.is_valid_store_id(bad_id),
                f"Expected {bad_id!r} to be invalid store_id",
            )
            with self.assertRaises(ValueError):
                paths.require_valid_store_id(bad_id)

            if bad_id and "\n" not in bad_id:
                b_dir = self._create_temp_bundle_dir(store_id=bad_id)
                b = bundle.load(bad_id, registry_root=self.tmp_dir)
                errs = bundle.check(b, registry_root=self.tmp_dir)
                record_check()
                self.assertTrue(
                    any("malformed store_id" in e for e in errs),
                    f"Expected malformed store_id error for {bad_id!r}, got: {errs}",
                )
                shutil.rmtree(b_dir)

    def test_failure_absent_declared_file(self) -> None:
        # Absent analyses.tsv
        b_dir = self._create_temp_bundle_dir(store_id="OGS-00020", analyses_content="__OMIT__")
        b = bundle.load("OGS-00020", registry_root=self.tmp_dir)
        errs = bundle.check(b, registry_root=self.tmp_dir)
        record_check()
        self.assertTrue(
            any("analyses file does not exist" in e for e in errs),
            f"Expected absent analyses.tsv error, got: {errs}",
        )
        shutil.rmtree(b_dir)

        # Absent declared sidecar
        b_dir = self._create_temp_bundle_dir(
            store_id="OGS-00021",
            sidecars={"missing_sidecar": "sidecars/does_not_exist.tsv"},
        )
        (b_dir / "sidecars" / "does_not_exist.tsv").unlink()
        b = bundle.load("OGS-00021", registry_root=self.tmp_dir)
        errs = bundle.check(b, registry_root=self.tmp_dir)
        record_check()
        self.assertTrue(
            any("declared sidecar file 'missing_sidecar' does not exist" in e for e in errs),
            f"Expected absent declared sidecar error, got: {errs}",
        )
        shutil.rmtree(b_dir)

    def test_failure_bad_checksum(self) -> None:
        # Bad sha256 in source_snapshot
        b_dir = self._create_temp_bundle_dir(
            store_id="OGS-00030",
            release_override={"source_snapshot": {"manifest_sha256": "not_a_valid_hex_sha256"}},
        )
        b = bundle.load("OGS-00030", registry_root=self.tmp_dir)
        errs = bundle.check(b, registry_root=self.tmp_dir)
        record_check()
        self.assertTrue(
            any("source_snapshot manifest_sha256 is invalid" in e for e in errs),
            f"Expected bad source_snapshot checksum error, got: {errs}",
        )
        shutil.rmtree(b_dir)

        # Bad sha256 in generator.version
        b_dir = self._create_temp_bundle_dir(
            store_id="OGS-00031",
            release_override={"generator": {"name": "gen.py", "version": "sha256:short_hex"}},
        )
        b = bundle.load("OGS-00031", registry_root=self.tmp_dir)
        errs = bundle.check(b, registry_root=self.tmp_dir)
        record_check()
        self.assertTrue(
            any("generator version has invalid sha256" in e for e in errs),
            f"Expected bad generator version sha256 error, got: {errs}",
        )
        shutil.rmtree(b_dir)

        # Bad checksum algorithm in analyses.tsv
        bad_alg_analyses = (
            "analysis_id\tsource_analysis_id\tsource_label\tanalysis_label\t"
            "trait_ontology_label\ttrait_ontology_id\ttrait_ontology_mapping_method\t"
            "source_file\tchecksum\tchecksum_algorithm\tsource_genome_build\t"
            "license\tassigned_ancestry\tancestry_assignment_method\t"
            "original_effect_scale\toriginal_sd\toriginal_sd_method\t"
            "stored_effect_scale\tsample_size_kind\tsample_size_scope\tsample_size\t"
            "n_cases\tn_controls\n"
            "TEST_001\tsrc_001\tTrait 1\tTrait 1\tEFO\tEFO:0001\tsource_provided\t"
            "/data/src/test1.tsv\t" + "a" * 64 + "\tunsupported_crc32\thg38\t"
            "Open Access\tEUR\taf_assigned\tsd\t1.0\tdeclared_standardised\t"
            "sd\ttotal\tanalysis_level\t10000\t\t\n"
        )
        b_dir = self._create_temp_bundle_dir(store_id="OGS-00032", analyses_content=bad_alg_analyses)
        b = bundle.load("OGS-00032", registry_root=self.tmp_dir)
        errs = bundle.check(b, registry_root=self.tmp_dir)
        record_check()
        self.assertTrue(
            any("unsupported checksum_algorithm" in e for e in errs),
            f"Expected unsupported checksum algorithm error, got: {errs}",
        )
        shutil.rmtree(b_dir)

        # Bad hex checksum string in analyses.tsv
        bad_hex_analyses = (
            "analysis_id\tsource_analysis_id\tsource_label\tanalysis_label\t"
            "trait_ontology_label\ttrait_ontology_id\ttrait_ontology_mapping_method\t"
            "source_file\tchecksum\tchecksum_algorithm\tsource_genome_build\t"
            "license\tassigned_ancestry\tancestry_assignment_method\t"
            "original_effect_scale\toriginal_sd\toriginal_sd_method\t"
            "stored_effect_scale\tsample_size_kind\tsample_size_scope\tsample_size\t"
            "n_cases\tn_controls\n"
            "TEST_001\tsrc_001\tTrait 1\tTrait 1\tEFO\tEFO:0001\tsource_provided\t"
            "/data/src/test1.tsv\tzzzz_not_hex\tsha256\thg38\t"
            "Open Access\tEUR\taf_assigned\tsd\t1.0\tdeclared_standardised\t"
            "sd\ttotal\tanalysis_level\t10000\t\t\n"
        )
        b_dir = self._create_temp_bundle_dir(store_id="OGS-00033", analyses_content=bad_hex_analyses)
        b = bundle.load("OGS-00033", registry_root=self.tmp_dir)
        errs = bundle.check(b, registry_root=self.tmp_dir)
        record_check()
        self.assertTrue(
            any("invalid sha256 checksum" in e for e in errs),
            f"Expected invalid sha256 checksum hex error, got: {errs}",
        )
        shutil.rmtree(b_dir)

    def test_failure_unresolvable_derived_from(self) -> None:
        # Reference-completed release with missing derived_from
        b_dir = self._create_temp_bundle_dir(
            store_id="OGS-00040",
            release_override={"derived_from": None},
            build_override={
                "completion_state": "reference_completed",
                "build": None,
                "complete": {"command": "complete-ragged", "options": {"ld-panel": "/panel"}},
            },
        )
        b = bundle.load("OGS-00040", registry_root=self.tmp_dir)
        errs = bundle.check(b, registry_root=self.tmp_dir)
        record_check()
        self.assertTrue(
            any("requires non-empty derived_from" in e for e in errs),
            f"Expected missing derived_from error, got: {errs}",
        )
        shutil.rmtree(b_dir)

        # Self-referential derived_from
        b_dir = self._create_temp_bundle_dir(
            store_id="OGS-00041",
            release_override={"derived_from": "OGS-00041"},
            build_override={
                "completion_state": "reference_completed",
                "build": None,
                "complete": {"command": "complete-ragged", "options": {"ld-panel": "/panel"}},
            },
        )
        b = bundle.load("OGS-00041", registry_root=self.tmp_dir)
        errs = bundle.check(b, registry_root=self.tmp_dir)
        record_check()
        self.assertTrue(
            any("cannot reference itself" in e for e in errs),
            f"Expected self-referential derived_from error, got: {errs}",
        )
        shutil.rmtree(b_dir)

        # Unresolvable derived_from for reference_completed (parent does not exist in registry)
        b_dir = self._create_temp_bundle_dir(
            store_id="OGS-00042",
            release_override={"derived_from": "OGS-99999"},
            build_override={
                "completion_state": "reference_completed",
                "build": None,
                "complete": {"command": "complete-ragged", "options": {"ld-panel": "/panel"}},
            },
        )
        b = bundle.load("OGS-00042", registry_root=self.tmp_dir)
        errs = bundle.check(b, registry_root=self.tmp_dir)
        record_check()
        self.assertTrue(
            any("unresolvable derived_from 'OGS-99999'" in e for e in errs),
            f"Expected unresolvable derived_from error, got: {errs}",
        )
        shutil.rmtree(b_dir)

        # Unresolvable derived_from on observed_only (resolves for every bundle that declares it)
        b_dir = self._create_temp_bundle_dir(
            store_id="OGS-00043",
            release_override={"derived_from": "OGS-88888"},
            build_override={"completion_state": "observed_only"},
        )
        b = bundle.load("OGS-00043", registry_root=self.tmp_dir)
        errs = bundle.check(b, registry_root=self.tmp_dir)
        record_check()
        self.assertTrue(
            any("unresolvable derived_from 'OGS-88888'" in e for e in errs),
            f"Expected unresolvable derived_from error for observed_only, got: {errs}",
        )
        shutil.rmtree(b_dir)

    def test_failure_illegal_status_transitions(self) -> None:
        # 1. Check transition validation helper directly
        legal_pairs = [
            ("candidate", "accepted"),
            ("candidate", "withdrawn"),
            ("candidate", "superseded"),
            ("accepted", "built"),
            ("accepted", "withdrawn"),
            ("built", "validated"),
            ("built", "superseded"),
            ("built", "withdrawn"),
            ("validated", "superseded"),
            ("validated", "withdrawn"),
        ]
        for src, dst in legal_pairs:
            record_check()
            self.assertTrue(bundle.is_legal_status_transition(src, dst))
            self.assertEqual(bundle.validate_status_transition(src, dst), [])

        illegal_pairs = [
            ("candidate", "validated"),
            ("candidate", "built"),
            ("accepted", "candidate"),
            ("validated", "candidate"),
            ("validated", "accepted"),
            ("withdrawn", "built"),
            ("withdrawn", "candidate"),
            ("unknown_state", "built"),
            ("candidate", "invalid_target"),
        ]
        for src, dst in illegal_pairs:
            record_check()
            self.assertFalse(bundle.is_legal_status_transition(src, dst))
            errs = bundle.validate_status_transition(src, dst)
            self.assertTrue(len(errs) > 0, f"Expected transition {src}->{dst} to fail, got: {errs}")

        # 2. Check illegal status transition enforced directly via check(bundle, previous_status=...)
        b_dir = self._create_temp_bundle_dir(store_id="OGS-00050", release_override={"status": "validated"})
        b = bundle.load("OGS-00050", registry_root=self.tmp_dir)

        # candidate -> validated is illegal
        errs_illegal = bundle.check(b, previous_status="candidate", registry_root=self.tmp_dir)
        record_check()
        self.assertTrue(
            any("illegal status transition from 'candidate' to 'validated'" in e for e in errs_illegal),
            f"Expected illegal status transition error from check(), got: {errs_illegal}",
        )

        # built -> validated is legal
        errs_legal = bundle.check(b, previous_status="built", registry_root=self.tmp_dir)
        record_check()
        self.assertEqual(
            errs_legal,
            [],
            f"Expected legal transition built -> validated to pass, got: {errs_legal}",
        )

        # In-bundle invalid status value
        b_dir_inv = self._create_temp_bundle_dir(store_id="OGS-00051", release_override={"status": "in_progress"})
        b_inv = bundle.load("OGS-00051", registry_root=self.tmp_dir)
        errs_inv = bundle.check(b_inv, registry_root=self.tmp_dir)
        record_check()
        self.assertTrue(
            any("invalid status 'in_progress'" in e for e in errs_inv),
            f"Expected invalid status error, got: {errs_inv}",
        )
        shutil.rmtree(b_dir)
        shutil.rmtree(b_dir_inv)

    def test_malformed_yaml_handling(self) -> None:
        # Malformed release.yaml
        b_dir = self._create_temp_bundle_dir(store_id="OGS-00052", raw_release_yaml="store_id: [unclosed")
        b = bundle.load("OGS-00052", registry_root=self.tmp_dir)
        errs = bundle.check(b, registry_root=self.tmp_dir)
        record_check()
        self.assertTrue(
            any("release.yaml is malformed YAML" in e for e in errs),
            f"Expected malformed YAML error for release.yaml, got: {errs}",
        )
        shutil.rmtree(b_dir)

        # Malformed build.yaml
        b_dir = self._create_temp_bundle_dir(store_id="OGS-00053", raw_build_yaml="layout: {unclosed")
        b = bundle.load("OGS-00053", registry_root=self.tmp_dir)
        errs = bundle.check(b, registry_root=self.tmp_dir)
        record_check()
        self.assertTrue(
            any("build.yaml is malformed YAML" in e for e in errs),
            f"Expected malformed YAML error for build.yaml, got: {errs}",
        )
        shutil.rmtree(b_dir)

    # -------------------------------------------------------------------------
    # AC3: Delegation of analyses.tsv validation to opengwasdb.model.analyses
    # -------------------------------------------------------------------------
    def test_analyses_tsv_validation_delegation(self) -> None:
        # Out-of-vocabulary stored_effect_scale rejected by delegated validation
        bad_scale_analyses = (
            "analysis_id\tsource_analysis_id\tsource_label\tanalysis_label\t"
            "trait_ontology_label\ttrait_ontology_id\ttrait_ontology_mapping_method\t"
            "source_file\tchecksum\tchecksum_algorithm\tsource_genome_build\t"
            "license\tassigned_ancestry\tancestry_assignment_method\t"
            "original_effect_scale\toriginal_sd\toriginal_sd_method\t"
            "stored_effect_scale\tsample_size_kind\tsample_size_scope\tsample_size\t"
            "n_cases\tn_controls\n"
            "TEST_001\tsrc_001\tTrait 1\tTrait 1\tEFO\tEFO:0001\tsource_provided\t"
            "/data/src/test1.tsv\t" + "a" * 64 + "\tsha256\thg38\t"
            "Open Access\tEUR\taf_assigned\tsd\t1.0\tdeclared_standardised\t"
            "invalid_scale\ttotal\tanalysis_level\t10000\t\t\n"
        )
        b_dir = self._create_temp_bundle_dir(store_id="OGS-00060", analyses_content=bad_scale_analyses)
        b = bundle.load("OGS-00060", registry_root=self.tmp_dir)
        errs = bundle.check(b, registry_root=self.tmp_dir)
        record_check()
        self.assertTrue(
            any("invalid stored_effect_scale 'invalid_scale'" in e for e in errs),
            f"Expected delegated vocabulary error for stored_effect_scale, got: {errs}",
        )
        shutil.rmtree(b_dir)

        # Out-of-vocabulary original_sd_method rejected by delegated validation
        bad_sd_analyses = (
            "analysis_id\tsource_analysis_id\tsource_label\tanalysis_label\t"
            "trait_ontology_label\ttrait_ontology_id\ttrait_ontology_mapping_method\t"
            "source_file\tchecksum\tchecksum_algorithm\tsource_genome_build\t"
            "license\tassigned_ancestry\tancestry_assignment_method\t"
            "original_effect_scale\toriginal_sd\toriginal_sd_method\t"
            "stored_effect_scale\tsample_size_kind\tsample_size_scope\tsample_size\t"
            "n_cases\tn_controls\n"
            "TEST_001\tsrc_001\tTrait 1\tTrait 1\tEFO\tEFO:0001\tsource_provided\t"
            "/data/src/test1.tsv\t" + "a" * 64 + "\tsha256\thg38\t"
            "Open Access\tEUR\taf_assigned\tsd\t1.0\tinvented_sd_method\t"
            "sd\ttotal\tanalysis_level\t10000\t\t\n"
        )
        b_dir = self._create_temp_bundle_dir(store_id="OGS-00061", analyses_content=bad_sd_analyses)
        b = bundle.load("OGS-00061", registry_root=self.tmp_dir)
        errs = bundle.check(b, registry_root=self.tmp_dir)
        record_check()
        self.assertTrue(
            any("invalid original_sd_method 'invented_sd_method'" in e for e in errs),
            f"Expected delegated vocabulary error for original_sd_method, got: {errs}",
        )
        shutil.rmtree(b_dir)

    # -------------------------------------------------------------------------
    # AC4: Candidate bundle with no validation.yaml passes
    # -------------------------------------------------------------------------
    def test_candidate_bundle_with_no_validation_yaml(self) -> None:
        candidate_analyses = (
            "analysis_id\tsource_analysis_id\tsource_label\tanalysis_label\t"
            "trait_ontology_label\ttrait_ontology_id\ttrait_ontology_mapping_method\t"
            "source_file\tchecksum\tchecksum_algorithm\tsource_genome_build\t"
            "license\tassigned_ancestry\tancestry_assignment_method\t"
            "original_effect_scale\toriginal_sd\toriginal_sd_method\t"
            "stored_effect_scale\tsample_size_kind\tsample_size_scope\tsample_size\t"
            "n_cases\tn_controls\n"
            "CANDIDATE_001\tsrc_001\tCandidate Trait\tCandidate Trait\tEFO\tEFO:0001\tsource_provided\t"
            "/data/src/cand1.tsv\t" + "a" * 64 + "\tsha256\thg38\t"
            "Open Access\tEUR\taf_assigned\tsd\t\t\t"
            "\t\t\t\t\t\n"
        )
        b_dir = self._create_temp_bundle_dir(
            store_id="OGS-00070",
            release_override={"status": "candidate"},
            analyses_content=candidate_analyses,
            write_validation=False,
        )
        b = bundle.load("OGS-00070", registry_root=self.tmp_dir)
        record_check()
        self.assertIsNone(b.validation)
        self.assertIsNone(b.validation_path)

        errs = bundle.check(b, registry_root=self.tmp_dir)
        record_check()
        self.assertEqual(
            errs,
            [],
            f"Expected candidate bundle with no validation.yaml to pass, got: {errs}",
        )
        shutil.rmtree(b_dir)

    # -------------------------------------------------------------------------
    # AC5: BESD / complete-ragged probe overlays
    # -------------------------------------------------------------------------
    def test_besd_complete_ragged_blank_shared_core_values(self) -> None:
        # 1. Validated BESD release with unpopulated shared-core analytical cells passes
        besd_analyses = (
            "analysis_index\tanalysis_id\tanalysis_label\ttrait_ontology_id\ttrait_ontology_label\t"
            "tissue\tcontext\ttrait_chr\ttrait_bp\tstored_effect_scale\tassigned_ancestry\t"
            "ancestry_assignment_method\tsample_size_kind\tsample_size_scope\tsample_size\t"
            "n_cases\tn_controls\toriginal_effect_scale\toriginal_sd\toriginal_sd_method\n"
            "0\tENSG00000175445::whole_blood\tENSG00000175445\tENSEMBL:ENSG00000175445\tEnsembl\t"
            "whole_blood\t\t8\t19791998\t\t\t\t\t\t\t\t\t\t\t\n"
        )
        b_dir = self._create_temp_bundle_dir(
            store_id="OGS-00080",
            release_override={"status": "validated"},
            build_override={
                "layout": "ragged",
                "completion_state": "observed_only",
                "build": {"command": "build-ragged-besd", "options": {"source-build": "hg19", "tissue": "whole_blood"}},
            },
            analyses_content=besd_analyses,
        )
        b = bundle.load("OGS-00080", registry_root=self.tmp_dir)
        errs = bundle.check(b, registry_root=self.tmp_dir)
        record_check()
        self.assertEqual(
            errs,
            [],
            f"Expected validated BESD bundle with blank shared-core values to pass, got: {errs}",
        )
        shutil.rmtree(b_dir)

        # 2. BESD release with populated invalid vocabulary MUST fail
        bad_besd_analyses = (
            "analysis_index\tanalysis_id\tanalysis_label\ttrait_ontology_id\ttrait_ontology_label\t"
            "tissue\tcontext\ttrait_chr\ttrait_bp\tstored_effect_scale\tassigned_ancestry\t"
            "ancestry_assignment_method\tsample_size_kind\tsample_size_scope\tsample_size\t"
            "n_cases\tn_controls\toriginal_effect_scale\toriginal_sd\toriginal_sd_method\n"
            "0\tENSG00000175445::whole_blood\tENSG00000175445\tENSEMBL:ENSG00000175445\tEnsembl\t"
            "whole_blood\t\t8\t19791998\tinvalid_scale\t\t\t\t\t\t\t\t\t\t\n"
        )
        b_dir = self._create_temp_bundle_dir(
            store_id="OGS-00081",
            release_override={"status": "validated"},
            build_override={
                "layout": "ragged",
                "completion_state": "observed_only",
                "build": {"command": "build-ragged-besd", "options": {"source-build": "hg19", "tissue": "whole_blood"}},
            },
            analyses_content=bad_besd_analyses,
        )
        b = bundle.load("OGS-00081", registry_root=self.tmp_dir)
        errs = bundle.check(b, registry_root=self.tmp_dir)
        record_check()
        self.assertTrue(
            any("invalid stored_effect_scale 'invalid_scale'" in e for e in errs),
            f"Expected invalid vocabulary in BESD to be caught, got: {errs}",
        )
        shutil.rmtree(b_dir)

    # -------------------------------------------------------------------------
    # AC6: check() opens no Store and accesses no artifact paths
    # -------------------------------------------------------------------------
    def test_check_opens_no_store_or_artifact_paths(self) -> None:
        # 1. Structural check: ogstores.bundle must not import forbidden Store opening symbols
        forbidden_symbols = ("open_store", "OpenGWASDBStore", "open_dense", "open_ragged", "open_hybrid")
        for sym in forbidden_symbols:
            record_check()
            self.assertFalse(
                hasattr(ogstores.bundle, sym),
                f"ogstores.bundle must not import {sym!r}",
            )

        # 2. Spy on ogstores.paths artifact functions: none may be called during load/check
        artifact_helpers = [
            "store_path",
            "source_dir",
            "work_dir",
            "records_dir",
            "record_path",
            "partial_store_path",
            "by_label_dir",
            "by_label_link",
            "parent_store_path",
        ]
        spies = {h: mock.patch.object(paths, h, wraps=getattr(paths, h)) for h in artifact_helpers}
        active_spies = [spy.start() for spy in spies.values()]

        # 3. Guard filesystem calls: exists, is_file, is_dir, stat, open
        stores_dir = (REPO_ROOT / "stores").resolve()
        accessed_paths: list[str] = []

        orig_open = builtins.open
        orig_exists = Path.exists
        orig_is_file = Path.is_file
        orig_is_dir = Path.is_dir
        orig_stat = Path.stat

        def check_path_tripwire(p: Path | str) -> None:
            path_str = str(p)
            accessed_paths.append(path_str)
            if path_str.startswith("/data/opengwasdb") or path_str.endswith(".opengwasdb") or ".zarr" in path_str:
                raise AssertionError(f"Forbidden artifact path accessed during bundle load/check: {path_str}")

        def guarded_open(file: Any, *args: Any, **kwargs: Any) -> Any:
            check_path_tripwire(file)
            return orig_open(file, *args, **kwargs)

        def guarded_exists(self: Path) -> bool:
            check_path_tripwire(self)
            return orig_exists(self)

        def guarded_is_file(self: Path) -> bool:
            check_path_tripwire(self)
            return orig_is_file(self)

        def guarded_is_dir(self: Path) -> bool:
            check_path_tripwire(self)
            return orig_is_dir(self)

        def guarded_stat(self: Path, **kwargs: Any) -> Any:
            check_path_tripwire(self)
            return orig_stat(self, **kwargs)

        try:
            with mock.patch("builtins.open", side_effect=guarded_open):
                with mock.patch.object(Path, "exists", guarded_exists):
                    with mock.patch.object(Path, "is_file", guarded_is_file):
                        with mock.patch.object(Path, "is_dir", guarded_is_dir):
                            with mock.patch.object(Path, "stat", guarded_stat):
                                for i in range(1, 8):
                                    store_id = f"OGS-{i:05d}"
                                    b = bundle.load(store_id, registry_root=stores_dir)
                                    errs = bundle.check(b, registry_root=stores_dir)
                                    record_check()
                                    self.assertEqual(errs, [])

            # Assert no artifact helper was ever invoked by bundle.py
            for spy in active_spies:
                record_check()
                self.assertEqual(spy.call_count, 0, f"Artifact helper was invoked: {spy}")

            # Assert all accessed paths were within stores/ or repository root
            for p_str in accessed_paths:
                p = Path(p_str).resolve()
                record_check()
                self.assertTrue(
                    p.is_relative_to(stores_dir) or p.is_relative_to(REPO_ROOT),
                    f"Accessed path {p_str} is outside repository/registry",
                )
        finally:
            for spy in spies.values():
                spy.stop()

        # 4. Prove tripwires actively detect and fail on artifact access
        with mock.patch("builtins.open", side_effect=guarded_open):
            with self.assertRaises(AssertionError) as ctx:
                open("/data/opengwasdb/stores/OGS-00001/store.opengwasdb", "r")
            self.assertIn("Forbidden artifact path accessed", str(ctx.exception))

        with mock.patch.object(Path, "exists", guarded_exists):
            with self.assertRaises(AssertionError) as ctx:
                Path("/data/opengwasdb/stores/OGS-00001/store.opengwasdb").exists()
            self.assertIn("Forbidden artifact path accessed", str(ctx.exception))

    # -------------------------------------------------------------------------
    # AC7: Every artifact path is a pure function of the Store Release id
    # -------------------------------------------------------------------------
    def test_artifact_paths_pure_functions(self) -> None:
        test_cases = [
            ("OGS-00001", paths.DEFAULT_ARTIFACT_ROOT),
            ("OGS-00042", paths.DEFAULT_ARTIFACT_ROOT),
            ("OGS-00099", Path("/custom/storage/root")),
        ]
        for sid, root in test_cases:
            root_p = Path(root)
            record_check()
            self.assertEqual(paths.store_dir(sid, root), root_p / sid)
            self.assertEqual(paths.release_root(sid, root), root_p / sid)
            self.assertEqual(paths.source_dir(sid, root), root_p / sid / "source")
            self.assertEqual(paths.work_dir(sid, root), root_p / sid / "work")
            self.assertEqual(paths.records_dir(sid, root), root_p / sid / "records")
            self.assertEqual(paths.record_path(sid, "build", root), root_p / sid / "records" / "build.json")
            self.assertEqual(paths.record_path(sid, "validate", root), root_p / sid / "records" / "validate.json")
            self.assertEqual(paths.store_path(sid, root), root_p / sid / "store.opengwasdb")
            self.assertEqual(paths.partial_store_path(sid, root), root_p / sid / "store.opengwasdb.partial")
            self.assertEqual(paths.by_label_dir(root), root_p / "by-label")
            self.assertEqual(paths.by_label_link(sid, "my-label", root), root_p / "by-label" / "my-label")
            self.assertEqual(paths.parent_store_path(sid, root), root_p / sid / "store.opengwasdb")


def main() -> None:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestBundleAndPaths)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if not result.wasSuccessful():
        sys.exit(1)
    print(f"ALL {n_checks} CHECKS PASSED")


if __name__ == "__main__":
    main()

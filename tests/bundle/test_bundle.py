#!/usr/bin/env python3
"""Executable Release Bundle contract and summary tests (#127 and #131)."""

from __future__ import annotations

import builtins
import csv
import os
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
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


class TestBundleContract(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="ogstores_bundle_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def make_bundle(
        self,
        store_id: str = "OGS-00090",
        *,
        release: dict[str, Any] | None = None,
        build: dict[str, Any] | None = None,
        analyses: str | None = None,
        status: str = "accepted",
    ) -> bundle.Bundle:
        root = self.tmp_dir / store_id
        root.mkdir(parents=True, exist_ok=True)
        release_data: dict[str, Any] = {
            "store_id": store_id,
            "label": "fixture",
            "status": status,
            "source_collection_id": "fixture-source",
            "source_snapshot_id": "fixture-snapshot-v1",
            "association_coverage": "full_gwas",
            "derived_from": None,
            "release_kind": "one-off",
            "created_at": "2026-09-17T00:00:00Z",
            "description": "Fixture Release Bundle",
            "generator": {"name": "fixtures/generate.py"},
        }
        if release:
            release_data.update(release)
        build_data: dict[str, Any] = {
            "store_id": store_id,
            "layout": "dense",
            "completion_state": "observed_only",
            "build": {"command": "build-dense-vcf", "options": {}},
            "post": {
                "top_hits": False,
                "rho": False,
                "overview": True,
                "validate": True,
            },
        }
        if build:
            build_data.update(build)
        if analyses is None:
            analyses = (
                "analysis_id\tstored_effect_scale\tsample_size_kind\t"
                "sample_size_scope\tsample_size\toriginal_effect_scale\t"
                "original_sd_method\tassigned_ancestry\t"
                "ancestry_assignment_method\tchecksum\tchecksum_algorithm\t"
                "source_file\n"
                "FIXTURE_1\tsd\ttotal\tanalysis_level\t1000\tsd\t"
                "declared_standardised\tEUR\taf_assigned\t"
                f"{'a' * 64}\tsha256\t/data/source/fixture.tsv\n"
            )

        (root / "release.yaml").write_text(
            yaml.safe_dump(release_data), encoding="utf-8"
        )
        (root / "build.yaml").write_text(
            yaml.safe_dump(build_data), encoding="utf-8"
        )
        (root / "analyses.tsv").write_text(analyses, encoding="utf-8")
        return bundle.load(store_id, registry_root=self.tmp_dir)

    def test_ci_population_is_all_seven_registered_bundles_and_each_passes(self) -> None:
        registry = REPO_ROOT / "stores"
        store_ids = sorted(
            path.name
            for path in registry.iterdir()
            if path.is_dir() and paths.is_valid_store_id(path.name)
        )
        self.assertEqual(store_ids, [f"OGS-{number:05d}" for number in range(1, 8)])
        for store_id in store_ids:
            errors = bundle.check(
                bundle.load(store_id, registry_root=registry),
                registry_root=registry,
            )
            record_check()
            self.assertEqual(errors, [], f"{store_id}: {errors}")

    def test_ogs_00007_uses_unified_gene_identity_columns(self) -> None:
        release_root = REPO_ROOT / "stores" / "OGS-00007"
        analyses = ogstores.bundle.opengwasdb_analyses.read_analyses(
            release_root / "analyses.tsv"
        )
        targets = ogstores.bundle.opengwasdb_analyses.read_analyses(
            release_root / "sidecars" / "analysis_targets.tsv"
        )
        targets_by_id = {row["source_analysis_id"]: row for row in targets.rows}

        self.assertTrue(
            set(analyses.fieldnames).isdisjoint(
                ogstores.bundle.opengwasdb_analyses.RETIRED_ANALYSIS_COLUMNS
            )
        )
        for row in analyses.rows:
            target = targets_by_id[row["source_analysis_id"]]
            record_check()
            self.assertEqual(row["analysis_label"], target["gene_name"])
            self.assertEqual(
                row["trait_ontology_id"],
                f"ENSEMBL:{target['ensembl_gene_id']}",
            )
            self.assertEqual(row["trait_ontology_label"], "Ensembl")
            self.assertEqual(
                row["trait_ontology_mapping_method"],
                "external_authority_lookup",
            )

    def test_one_pass_accumulates_independent_errors(self) -> None:
        checked = self.make_bundle()
        bad_release = dict(checked.release)
        for key in ("label", "source_collection_id", "generator"):
            bad_release.pop(key)
        bad_release.update({"store_id": "OGS-99999", "status": "invented"})
        bad_build = dict(checked.build)
        bad_build.update({"store_id": "OGS-99998", "layout": "other"})
        checked = replace(
            checked,
            release=bad_release,
            build=bad_build,
            analyses_path=checked.root / "absent.tsv",
        )

        errors = bundle.check(checked, previous_status="also-invented", registry_root=self.tmp_dir)
        record_check()
        self.assertIsInstance(errors, list)
        for fragment in (
            "'label'",
            "'source_collection_id'",
            "'generator'",
            "invalid status",
            "release.yaml store_id",
            "build.yaml store_id",
            "invalid layout",
            "invalid current status",
            "analyses file does not exist",
        ):
            self.assertTrue(any(fragment in error for error in errors), (fragment, errors))

    def test_check_never_raises_for_wrong_document_shapes_or_validator_failure(self) -> None:
        checked = self.make_bundle()
        hostile = replace(
            checked,
            release=["not", "a", "mapping"],  # type: ignore[arg-type]
            build="not a mapping",  # type: ignore[arg-type]
        )
        with mock.patch.object(
            ogstores.bundle.opengwasdb_analyses,
            "validate_analyses",
            side_effect=RuntimeError("validator exploded"),
        ):
            errors = bundle.check(hostile, previous_status=hostile, registry_root=self.tmp_dir)
        record_check()
        self.assertIsInstance(errors, list)
        self.assertGreaterEqual(len(errors), 4)
        self.assertTrue(any("release.yaml must contain a mapping" in error for error in errors))
        self.assertTrue(any("build.yaml must contain a mapping" in error for error in errors))
        self.assertTrue(any("validator exploded" in error for error in errors))

    def test_required_identity_and_provenance_keys(self) -> None:
        checked = self.make_bundle()
        release = dict(checked.release)
        for key in bundle.RELEASE_REQUIRED_KEYS:
            release.pop(key, None)
        errors = bundle.check(replace(checked, release=release), registry_root=self.tmp_dir)
        record_check()
        for key in bundle.RELEASE_REQUIRED_KEYS:
            self.assertTrue(any(repr(key) in error for error in errors), (key, errors))

    def test_artifact_root_is_not_part_of_the_bundle_contract(self) -> None:
        checked = self.make_bundle()
        self.assertNotIn("artifacts", bundle.BUILD_REQUIRED_KEYS)
        self.assertEqual(bundle.check(checked, registry_root=self.tmp_dir), [])

        errors = bundle.check(
            replace(
                checked,
                build={
                    **checked.build,
                    "artifacts": {"root": "/data/opengwasdb/stores"},
                },
            ),
            registry_root=self.tmp_dir,
        )
        record_check()
        self.assertTrue(any("must not declare 'artifacts'" in error for error in errors))

    def test_retired_analysis_columns_come_from_upstream_and_name_the_release(self) -> None:
        checked = self.make_bundle()
        lines = checked.analyses_path.read_text(encoding="utf-8").splitlines()
        retired = ogstores.bundle.opengwasdb_analyses.RETIRED_ANALYSIS_COLUMNS
        retired_header = "\t".join(retired)
        retired_values = "\t".join("retired" for _ in retired)
        checked.analyses_path.write_text(
            f"{lines[0]}\t{retired_header}\n"
            f"{lines[1]}\t{retired_values}\n",
            encoding="utf-8",
        )

        errors = bundle.check(checked, registry_root=self.tmp_dir)
        record_check()
        for column in retired:
            self.assertIn(
                f"Store Release {checked.store_id} analyses.tsv contains retired "
                f"Analysis column {column!r}",
                errors,
            )

        sentinel = "upstream_only_retired"
        with mock.patch.object(
            ogstores.bundle.opengwasdb_analyses,
            "RETIRED_ANALYSIS_COLUMNS",
            (sentinel,),
        ):
            sentinel_bundle = self.make_bundle(
                store_id="OGS-00091",
                analyses=f"{lines[0]}\t{sentinel}\n{lines[1]}\tretired\n",
            )
            sentinel_errors = bundle.check(
                sentinel_bundle,
                registry_root=self.tmp_dir,
            )
        self.assertIn(
            f"Store Release OGS-00091 analyses.tsv contains retired "
            f"Analysis column {sentinel!r}",
            sentinel_errors,
        )

    def test_check_enforces_required_analysis_values_on_standard_releases(self) -> None:
        """Required Analysis columns must carry values, not merely exist in the header."""
        checked = self.make_bundle()
        blank_row = (
            "analysis_id\tstored_effect_scale\tsample_size_kind\t"
            "sample_size_scope\tsample_size\toriginal_effect_scale\t"
            "original_sd_method\tassigned_ancestry\t"
            "ancestry_assignment_method\tchecksum\tchecksum_algorithm\t"
            "source_file\n"
            "FIXTURE_1\t\ttotal\tanalysis_level\t\tsd\t"
            "declared_standardised\tEUR\taf_assigned\t"
            f"{'a' * 64}\tsha256\t/data/source/fixture.tsv\n"
        )
        checked.analyses_path.write_text(blank_row, encoding="utf-8")
        errors = bundle.check(checked, registry_root=self.tmp_dir)
        record_check()
        self.assertIn(
            "analysis 'FIXTURE_1' has no value for required column 'stored_effect_scale'",
            errors,
        )
        self.assertIn(
            "analysis 'FIXTURE_1' has no value for required column 'sample_size'",
            errors,
        )

    def test_new_ragged_besd_bundle_with_blank_required_values_is_rejected(self) -> None:
        """A new ragged-BESD release cannot silently inherit blank required values."""
        blank_analyses = (
            "analysis_id\tstored_effect_scale\tsample_size_kind\t"
            "sample_size_scope\tsample_size\toriginal_effect_scale\t"
            "original_sd_method\tassigned_ancestry\t"
            "ancestry_assignment_method\n"
            "FIXTURE_1\t\t\t\t\t\t\t\t\n"
        )
        new_besd = self.make_bundle(
            store_id="OGS-00095",
            release={
                "source_snapshot": {
                    "besd_prefix": "/data/test/pilot",
                    "source_genome_build": "hg19",
                }
            },
            build={
                "layout": "ragged",
                "completion_state": "observed_only",
                "build": {"command": "build-ragged-besd", "options": {}},
                "post": {
                    "top_hits": False,
                    "rho": False,
                    "overview": False,
                    "validate": True,
                },
            },
            analyses=blank_analyses,
        )
        errors = bundle.check(new_besd, registry_root=self.tmp_dir)
        record_check()
        self.assertIn(
            "analysis 'FIXTURE_1' has no value for required column 'stored_effect_scale'",
            errors,
        )
        self.assertIn(
            "analysis 'FIXTURE_1' has no value for required column 'sample_size'",
            errors,
        )
        self.assertEqual(
            bundle.LEGACY_BLANK_ANALYSIS_RELEASES,
            frozenset({"OGS-00001", "OGS-00002"}),
        )

    def test_assigned_ancestry_must_use_the_super_population_vocabulary(self) -> None:
        header = (
            "analysis_id\tstored_effect_scale\tsample_size_kind\t"
            "sample_size_scope\tsample_size\toriginal_effect_scale\t"
            "original_sd_method\tassigned_ancestry\t"
            "ancestry_assignment_method\tchecksum\tchecksum_algorithm\t"
            "source_file\n"
        )

        def analyses_with(ancestry: str) -> str:
            return header + (
                "FIXTURE_1\tsd\ttotal\tanalysis_level\t1000\tsd\t"
                f"declared_standardised\t{ancestry}\taf_assigned\t"
                f"{'a' * 64}\tsha256\t/data/source/fixture.tsv\n"
            )

        # A free-text Source Ancestry Label is not an Assigned Ancestry. The
        # two vocabularies are related but distinct, so "European" is rejected
        # in exactly the same way an unknown string would be.
        for source_label in ("European", "eur", "Finnish"):
            checked = self.make_bundle(analyses=analyses_with(source_label))
            errors = bundle.check(checked, registry_root=self.tmp_dir)
            record_check()
            self.assertTrue(
                any(
                    f"has assigned_ancestry {source_label!r}" in error
                    for error in errors
                ),
                (source_label, errors),
            )

        # The super-population code, and empty for unassigned, are both valid.
        for valid in ("EUR", "AFR", ""):
            checked = self.make_bundle(analyses=analyses_with(valid))
            errors = bundle.check(checked, registry_root=self.tmp_dir)
            record_check()
            self.assertFalse(
                any("assigned_ancestry" in error for error in errors), errors
            )

    def test_non_case_control_counts_must_be_absent_not_zero(self) -> None:
        header = (
            "analysis_id\tstored_effect_scale\tsample_size_kind\t"
            "sample_size_scope\tsample_size\tn_cases\tn_controls\t"
            "original_effect_scale\toriginal_sd_method\tassigned_ancestry\t"
            "ancestry_assignment_method\tchecksum\tchecksum_algorithm\t"
            "source_file\n"
        )

        def analyses_row(scale: str, kind: str, cases: str, controls: str,
                         sd_method: str) -> str:
            return header + (
                f"FIXTURE_1\t{scale}\t{kind}\tanalysis_level\t1000\t{cases}\t"
                f"{controls}\tsd\t{sd_method}\tEUR\taf_assigned\t"
                f"{'a' * 64}\tsha256\t/data/source/fixture.tsv\n"
            )

        # Zero is a fabricated case count on a non-case-control Analysis.
        zeroed = self.make_bundle(
            analyses=analyses_row("sd", "total", "0", "0", "declared_standardised")
        )
        errors = bundle.check(zeroed, registry_root=self.tmp_dir)
        record_check()
        self.assertTrue(any("n_cases='0'" in error for error in errors), errors)
        self.assertTrue(any("n_controls='0'" in error for error in errors), errors)

        # Blank is the honest value when the counts do not apply...
        blank = self.make_bundle(
            analyses=analyses_row("sd", "total", "", "", "declared_standardised")
        )
        blank_errors = bundle.check(blank, registry_root=self.tmp_dir)
        record_check()
        self.assertFalse(
            any("n_cases" in error or "n_controls" in error for error in blank_errors),
            blank_errors,
        )

        # ...and a real case-control Analysis keeps its counts.
        case_control = self.make_bundle(
            analyses=analyses_row("log_or", "case_control", "10", "20", "binary_trait")
        )
        case_control_errors = bundle.check(case_control, registry_root=self.tmp_dir)
        record_check()
        self.assertFalse(
            any(
                "n_cases" in error or "n_controls" in error
                for error in case_control_errors
            ),
            case_control_errors,
        )

    def test_super_populations_match_the_tracked_ancestry_reference_resource(self) -> None:
        resource = yaml.safe_load(
            (
                REPO_ROOT
                / "resources"
                / "reference-resources"
                / "ukb-ancestry-mixture-hg38"
                / "resource.yaml"
            ).read_text(encoding="utf-8")
        )
        record_check()
        self.assertEqual(
            set(ogstores.bundle.SUPERPOPULATIONS), set(resource["super_populations"])
        )

    def test_source_ancestry_label_map_targets_only_super_populations(self) -> None:
        map_path = (
            REPO_ROOT
            / "resources"
            / "reference-resources"
            / "ukb-ancestry-mixture-hg38"
            / "source_label_map.tsv"
        )
        with map_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle, delimiter="\t"))
        record_check()
        self.assertTrue(rows, "the source-label map is the tracked vocabulary evidence")
        self.assertTrue(
            all(row["super_population"] in ogstores.bundle.SUPERPOPULATIONS for row in rows),
            rows,
        )
        self.assertIn(
            {"source_label": "European", "super_population": "EUR"}, rows
        )

    def test_store_id_must_match_format_directory_and_both_documents(self) -> None:
        checked = self.make_bundle()
        for malformed in ("OGS-1", "ogs-00090", "OGS-00090\n"):
            errors = bundle.check(replace(checked, store_id=malformed), registry_root=self.tmp_dir)
            record_check()
            self.assertTrue(any("malformed store_id" in error for error in errors))

        mismatch = replace(
            checked,
            root=self.tmp_dir / "different-directory",
            release={**checked.release, "store_id": "OGS-00091"},
            build={**checked.build, "store_id": "OGS-00092"},
        )
        errors = bundle.check(mismatch, registry_root=self.tmp_dir)
        record_check()
        self.assertTrue(any("store directory" in error for error in errors))
        self.assertTrue(any("release.yaml store_id" in error for error in errors))
        self.assertTrue(any("build.yaml store_id" in error for error in errors))

    def test_release_status_vocabulary_and_transitions(self) -> None:
        for status in bundle.VALID_STATUSES:
            checked = self.make_bundle(
                store_id=f"OGS-{91 + len(list(self.tmp_dir.iterdir())):05d}",
                status=status,
            )
            errors = bundle.check(checked, registry_root=self.tmp_dir)
            record_check()
            self.assertFalse(any("invalid status" in error for error in errors), errors)

        checked = self.make_bundle(store_id="OGS-00100")
        errors = bundle.check(
            replace(checked, release={**checked.release, "status": "ready"}),
            previous_status="validated",
            registry_root=self.tmp_dir,
        )
        record_check()
        self.assertTrue(any("invalid status" in error for error in errors))
        self.assertTrue(any("invalid target status" in error for error in errors))

        errors = bundle.check(checked, previous_status="validated", registry_root=self.tmp_dir)
        record_check()
        self.assertTrue(any("illegal status transition" in error for error in errors))

    def test_candidate_allows_unresolved_rows_without_validation_record(self) -> None:
        unresolved = (
            "analysis_id\tstored_effect_scale\tsample_size_kind\t"
            "sample_size_scope\tsample_size\toriginal_effect_scale\t"
            "original_sd_method\tassigned_ancestry\t"
            "ancestry_assignment_method\n"
            "FIXTURE_1\t\t\t\t\t\t\t\t\n"
        )
        checked = self.make_bundle(status="candidate", analyses=unresolved)
        errors = bundle.check(checked, registry_root=self.tmp_dir)
        record_check()
        self.assertEqual(errors, [])

    def test_derived_from_resolves_to_registered_release(self) -> None:
        parent = self.make_bundle(store_id="OGS-00101")
        child = self.make_bundle(
            store_id="OGS-00102",
            release={"derived_from": parent.store_id},
            build={
                "layout": "ragged",
                "completion_state": "reference_completed",
                "complete": {"command": "complete-ragged", "options": {}},
                "post": {
                    "top_hits": False,
                    "rho": False,
                    "overview": False,
                    "validate": True,
                },
            },
        )
        errors = bundle.check(child, registry_root=self.tmp_dir)
        record_check()
        self.assertEqual(errors, [])

        cases = (
            (None, "requires non-empty derived_from"),
            (child.store_id, "cannot reference itself"),
            ("bad-parent", "not a valid store_id"),
            ("OGS-00999", "unresolvable derived_from"),
        )
        for parent_id, fragment in cases:
            errors = bundle.check(
                replace(child, release={**child.release, "derived_from": parent_id}),
                registry_root=self.tmp_dir,
            )
            record_check()
            self.assertTrue(any(fragment in error for error in errors), errors)

    def test_besd_prefix_is_checked_as_metadata_without_being_inspected(self) -> None:
        checked = self.make_bundle(
            build={
                "layout": "ragged",
                "build": {"command": "build-ragged-besd", "options": {}},
                "post": {
                    "top_hits": False,
                    "rho": False,
                    "overview": False,
                    "validate": True,
                },
            },
        )
        errors = bundle.check(checked, registry_root=self.tmp_dir)
        record_check()
        self.assertTrue(any("source_snapshot.besd_prefix" in error for error in errors))

        checked = replace(
            checked,
            release={
                **checked.release,
                "source_snapshot": {
                    "besd_prefix": "/data/opengwasdb/raw/fixture/missing-prefix"
                },
            },
        )
        errors = bundle.check(checked, registry_root=self.tmp_dir)
        record_check()
        self.assertEqual(errors, [])

    def test_declared_bundle_files_checksum_syntax_and_shared_schema(self) -> None:
        checked = self.make_bundle(
            release={
                "sidecars": {"evidence": "sidecars/missing.tsv"},
                "source_snapshot": {"manifest_sha256": "not-a-checksum"},
                "generator": {"name": "fixture", "version": "sha256:short"},
            },
            analyses=(
                "analysis_id\tstored_effect_scale\tassigned_ancestry\t"
                "ancestry_assignment_method\toriginal_sd_method\tchecksum\t"
                "checksum_algorithm\n"
                "FIXTURE_1\twrong\tEUR\taf_assigned\tunavailable\tbad\tsha256\n"
            ),
        )
        with mock.patch.object(
            ogstores.bundle.opengwasdb_analyses,
            "validate_analyses",
            wraps=ogstores.bundle.opengwasdb_analyses.validate_analyses,
        ) as delegated:
            errors = bundle.check(checked, registry_root=self.tmp_dir)
        record_check()
        delegated.assert_called_once()
        for fragment in (
            "declared sidecar file",
            "manifest_sha256",
            "generator version",
            "missing required column(s)",
            "invalid stored_effect_scale",
            "invalid sha256 checksum",
        ):
            self.assertTrue(any(fragment in error for error in errors), (fragment, errors))

    def test_malformed_yaml_is_reported_by_check(self) -> None:
        checked = self.make_bundle()
        (checked.root / "release.yaml").write_text("store_id: [unterminated\n", encoding="utf-8")
        (checked.root / "build.yaml").write_text("- not\n- a\n- mapping\n", encoding="utf-8")
        loaded = bundle.load(checked.store_id, registry_root=self.tmp_dir)
        errors = bundle.check(loaded, registry_root=self.tmp_dir)
        record_check()
        self.assertTrue(any("release.yaml is malformed YAML" in error for error in errors))
        self.assertTrue(any("expected a YAML mapping" in error for error in errors))

    def test_check_never_opens_a_store_or_inspects_an_artifact_path(self) -> None:
        registry = REPO_ROOT / "stores"
        loaded = [
            bundle.load(path.name, registry_root=registry)
            for path in sorted(registry.glob("OGS-*"))
        ]
        original_open = builtins.open
        original_is_file = Path.is_file
        original_is_dir = Path.is_dir

        def reject_artifact(path: object) -> None:
            text = str(path)
            if text.startswith("/data/opengwasdb") or ".opengwasdb" in text:
                raise AssertionError(f"artifact path inspected: {text}")

        def guarded_open(path: object, *args: Any, **kwargs: Any) -> Any:
            reject_artifact(path)
            return original_open(path, *args, **kwargs)

        def guarded_is_file(path: Path) -> bool:
            reject_artifact(path)
            return original_is_file(path)

        def guarded_is_dir(path: Path) -> bool:
            reject_artifact(path)
            return original_is_dir(path)

        artifact_helpers = (
            "store_path",
            "source_dir",
            "work_dir",
            "records_dir",
            "partial_store_path",
            "parent_store_path",
        )
        helper_patches = [
            mock.patch.object(paths, name, wraps=getattr(paths, name))
            for name in artifact_helpers
        ]
        helper_spies = [patch.start() for patch in helper_patches]
        try:
            with mock.patch("builtins.open", side_effect=guarded_open), \
                 mock.patch.object(Path, "is_file", guarded_is_file), \
                 mock.patch.object(Path, "is_dir", guarded_is_dir):
                for checked in loaded:
                    self.assertEqual(
                        bundle.check(checked, registry_root=registry), [], checked.store_id
                    )
            record_check()
            self.assertTrue(all(spy.call_count == 0 for spy in helper_spies))
        finally:
            for patch in helper_patches:
                patch.stop()


class TestBundleSummary(unittest.TestCase):
    """Value-level collapse rules for the issue #131 bundle summary."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="ogstores_summary_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def make_bundle(self, fieldnames: list[str], rows: list[dict[str, str]]) -> bundle.Bundle:
        analyses_path = self.tmp_dir / "analyses.tsv"
        with analyses_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=fieldnames,
                delimiter="\t",
                lineterminator="\n",
            )
            writer.writeheader()
            writer.writerows(rows)
        return bundle.Bundle(
            store_id="OGS-00990",
            root=self.tmp_dir,
            release={},
            build={},
            analyses_path=analyses_path,
        )

    def test_constant_values_are_preserved_verbatim(self) -> None:
        checked = self.make_bundle(
            list(bundle.SUMMARY_COLUMNS),
            [
                {column: "recorded value" for column in bundle.SUMMARY_COLUMNS},
                {column: "recorded value" for column in bundle.SUMMARY_COLUMNS},
            ],
        )
        summary = bundle.summarise(checked)
        record_check()
        self.assertEqual(summary["n_analyses"], 2)
        for column in bundle.SUMMARY_COLUMNS:
            self.assertEqual(summary[column], "recorded value")

    def test_varying_identifiers_are_mixed_counts_never_ranges(self) -> None:
        checked = self.make_bundle(
            ["publication_pmid"],
            [
                {"publication_pmid": "100"},
                {"publication_pmid": "300"},
                {"publication_pmid": "300"},
            ],
        )
        summary = bundle.summarise(checked)
        record_check()
        self.assertEqual(summary["publication_pmid"], "mixed (2)")
        self.assertNotIn("100-300", str(summary["publication_pmid"]))

    def test_varying_quantity_uses_numeric_minimum_and_maximum(self) -> None:
        checked = self.make_bundle(
            ["sample_size"],
            [{"sample_size": "100"}, {"sample_size": "9"}, {"sample_size": "20"}],
        )
        record_check()
        self.assertEqual(bundle.summarise(checked)["sample_size"], "9-100")

    def test_varying_urls_use_common_host_and_component_prefix(self) -> None:
        checked = self.make_bundle(
            ["source_url"],
            [
                {"source_url": "https://example.org/releases/a/one.tsv"},
                {"source_url": "https://example.org/releases/a/two.tsv"},
            ],
        )
        record_check()
        self.assertEqual(
            bundle.summarise(checked)["source_url"],
            "https://example.org/releases/a/",
        )

    def test_urls_without_a_common_authority_are_mixed(self) -> None:
        checked = self.make_bundle(
            ["source_url"],
            [
                {"source_url": "https://one.example/a.tsv"},
                {"source_url": "https://two.example/b.tsv"},
            ],
        )
        record_check()
        self.assertEqual(bundle.summarise(checked)["source_url"], "mixed (2)")

    def test_absent_empty_and_header_only_metadata_are_na(self) -> None:
        absent = self.make_bundle(["analysis_id"], [{"analysis_id": "one"}])
        self.assertEqual(bundle.summarise(absent)["first_author"], "NA")

        sparse = self.make_bundle(
            ["first_author"],
            [{"first_author": "One A"}, {"first_author": ""}],
        )
        self.assertEqual(bundle.summarise(sparse)["first_author"], "NA")

        empty = self.make_bundle(list(bundle.SUMMARY_COLUMNS), [])
        summary = bundle.summarise(empty)
        record_check()
        self.assertEqual(summary["n_analyses"], 0)
        self.assertTrue(all(summary[column] == "NA" for column in bundle.SUMMARY_COLUMNS))

    def test_zero_is_not_treated_as_absence(self) -> None:
        checked = self.make_bundle(["sample_size"], [{"sample_size": "0"}])
        record_check()
        self.assertEqual(bundle.summarise(checked)["sample_size"], "0")

    def test_invalid_quantity_and_url_fail_instead_of_being_coerced(self) -> None:
        bad_quantity = self.make_bundle(
            ["sample_size"],
            [{"sample_size": "10"}, {"sample_size": "unknown"}],
        )
        with self.assertRaisesRegex(ValueError, "non-numeric sample_size"):
            bundle.summarise(bad_quantity)

        bad_url = self.make_bundle(
            ["source_url"],
            [{"source_url": "https://example.org/a"}, {"source_url": "not-a-url"}],
        )
        record_check()
        with self.assertRaisesRegex(ValueError, "invalid source_url URL"):
            bundle.summarise(bad_url)

    def test_pooled_publications_remain_visibly_mixed(self) -> None:
        checked = self.make_bundle(
            ["first_author", "publication_pmid"],
            [
                {"first_author": "One A", "publication_pmid": "111"},
                {"first_author": "Two B", "publication_pmid": "222"},
            ],
        )
        summary = bundle.summarise(checked)
        record_check()
        self.assertEqual(summary["first_author"], "mixed (2)")
        self.assertEqual(summary["publication_pmid"], "mixed (2)")

    def test_all_seven_trial_release_summaries_match_regression_golden(self) -> None:
        expected = yaml.safe_load(
            (REPO_ROOT / "tests" / "bundle" / "golden" / "summaries.yaml").read_text(
                encoding="utf-8"
            )
        )
        actual = {
            path.name: bundle.summarise(
                bundle.load(path.name, registry_root=REPO_ROOT / "stores")
            )
            for path in sorted((REPO_ROOT / "stores").glob("OGS-*"))
            if paths.is_valid_store_id(path.name)
        }
        record_check()
        self.assertEqual(actual, expected)
        self.assertGreaterEqual(
            sum(value == "NA" for value in actual["OGS-00001"].values()),
            5,
            "missing Analytical Metadata must remain visibly sparse",
        )

    def test_summarise_opens_only_the_membership_table(self) -> None:
        checked = self.make_bundle(["analysis_id"], [{"analysis_id": "one"}])
        real_open = builtins.open

        def guarded_open(path: object, *args: Any, **kwargs: Any) -> Any:
            self.assertEqual(Path(path), checked.analyses_path)
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=guarded_open):
            summary = bundle.summarise(checked)
        record_check()
        self.assertEqual(summary["n_analyses"], 1)


class TestArtifactPaths(unittest.TestCase):
    def test_artifact_paths_remain_pure_functions(self) -> None:
        root = Path("/custom/artifacts")
        store_id = "OGS-00042"
        self.assertEqual(paths.store_dir(store_id, root), root / store_id)
        self.assertEqual(paths.source_dir(store_id, root), root / store_id / "source")
        self.assertEqual(paths.work_dir(store_id, root), root / store_id / "work")
        self.assertEqual(
            paths.store_path(store_id, root), root / store_id / "store.opengwasdb"
        )
        record_check()


class TestArtifactRootResolution(unittest.TestCase):
    """`paths.artifact_root()` resolves configuration, not the Release Bundle (issue #126)."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="ogstores_config_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _write_config(self, text: str) -> Path:
        config_p = self.tmp_dir / paths.REPO_CONFIG_FILENAME
        config_p.write_text(text, encoding="utf-8")
        return config_p

    def test_workflow_config_override_wins(self) -> None:
        """A workflow config override beats the environment and the config file."""
        self._write_config("artifact_root: /from/file\n")
        resolved = paths.artifact_root(
            config_override="/from/override",
            env={paths.ARTIFACT_ROOT_ENV_VAR: "/from/env"},
            repo_root=self.tmp_dir,
        )
        record_check()
        self.assertEqual(resolved, Path("/from/override"))

    def test_environment_variable_beats_repo_config_file(self) -> None:
        """The environment variable beats the config file and the default."""
        self._write_config("artifact_root: /from/file\n")
        resolved = paths.artifact_root(
            env={paths.ARTIFACT_ROOT_ENV_VAR: "/from/env"},
            repo_root=self.tmp_dir,
        )
        record_check()
        self.assertEqual(resolved, Path("/from/env"))

    def test_repository_config_file_beats_default(self) -> None:
        """With no override or environment variable, the config file wins."""
        self._write_config("artifact_root: /from/file\n")
        resolved = paths.artifact_root(env={}, repo_root=self.tmp_dir)
        record_check()
        self.assertEqual(resolved, Path("/from/file"))

    def test_built_in_default_when_unconfigured(self) -> None:
        """With no override, environment variable or config file, the default is used."""
        resolved = paths.artifact_root(env={}, repo_root=self.tmp_dir)
        record_check()
        self.assertEqual(resolved, paths.DEFAULT_ARTIFACT_ROOT)

    def test_environment_variable_reads_process_environment(self) -> None:
        """When no mapping is supplied, the process environment is consulted."""
        with mock.patch.dict(os.environ, {paths.ARTIFACT_ROOT_ENV_VAR: "/from/process"}, clear=False):
            resolved = paths.artifact_root(repo_root=self.tmp_dir)
        record_check()
        self.assertEqual(resolved, Path("/from/process"))

    def test_tracked_repo_config_is_the_production_root(self) -> None:
        """The committed ogstores.yaml supplies the default production artifact root."""
        record_check()
        self.assertTrue(paths.repo_config_path(REPO_ROOT).is_file())
        self.assertEqual(paths.artifact_root(env={}), paths.DEFAULT_ARTIFACT_ROOT)

    def test_config_file_without_key_falls_back_to_default(self) -> None:
        """A config file that names no artifact_root does not override the default."""
        self._write_config("unrelated: true\n")
        resolved = paths.artifact_root(env={}, repo_root=self.tmp_dir)
        record_check()
        self.assertEqual(resolved, paths.DEFAULT_ARTIFACT_ROOT)

    def test_malformed_config_file_raises(self) -> None:
        """A malformed config file fails loudly rather than building in the wrong place."""
        self._write_config("- not\n- a\n- mapping\n")
        with self.assertRaises(ValueError):
            paths.artifact_root(env={}, repo_root=self.tmp_dir)
        record_check()
        self._write_config("artifact_root: 42\n")
        with self.assertRaises(ValueError):
            paths.artifact_root(env={}, repo_root=self.tmp_dir)
        record_check()


def main() -> None:
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
    print(f"ALL {n_checks} CHECKS PASSED")


if __name__ == "__main__":
    main()

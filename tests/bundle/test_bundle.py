#!/usr/bin/env python3
"""Executable Release Bundle contract tests for issue #127."""

from __future__ import annotations

import builtins
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
            "family": "fixture-family",
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
            "artifacts": {"root": "/data/opengwasdb/stores"},
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

    def test_one_pass_accumulates_independent_errors(self) -> None:
        checked = self.make_bundle()
        bad_release = dict(checked.release)
        for key in ("label", "family", "source_collection_id", "generator"):
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
            "'family'",
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
            ogstores.bundle, "validate_analyses", side_effect=RuntimeError("validator exploded")
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
            ogstores.bundle,
            "validate_analyses",
            wraps=ogstores.bundle.validate_analyses,
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


def main() -> None:
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
    print(f"ALL {n_checks} CHECKS PASSED")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Indexed Variant Subset runtime verification (stores #209, epic #214).

Covers the parts of #209 that are *not* pure planning:

* a declared subset's maintained Reference Resource is resolved and its
  `genome_build`/`location`/`checksums` must agree with the declaration;
* the declared ALID list must hash to the maintained sha256, byte size and row
  count, and a wrong list or a wrong declaration is refused, not substituted;
* a run that declares a subset needs an `opengwasdb` runtime providing
  `build-indexed-subset`, and is refused with the required capability when it
  does not (the repository pin deliberately predates it until #197);
* a run with no declaration is never gated on the runtime version.

Fixtures are tiny and hermetic; no real HapMap3 data or opengwasdb CLI is
required (the capability probe is exercised against a stub executable).
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ogstores import resources  # noqa: E402
from ogstores.plan import IndexedSubsetDeclaration  # noqa: E402

n_checks = 0


def record_check() -> None:
    global n_checks
    n_checks += 1


ALID_BYTES = b"1:100:A:G\n1:200:C:T\n8_KI270821v1_alt:559250:G:T\n"


class TestIndexedSubsetResource(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ogstores_subsets_"))
        self.repo = self.tmp / "repo"
        self.resource_dir = (
            self.repo / "resources" / "reference-resources" / "hapmap3-hg38"
        )
        self.resource_dir.mkdir(parents=True)
        self.alid = self.tmp / "hm3.grch38.alid.txt"
        self.alid.write_bytes(ALID_BYTES)
        self.declaration = IndexedSubsetDeclaration(
            name="hm3",
            resource_id="hapmap3-hg38",
            variant_list=self.alid,
            reference_assembly="GRCh38",
            checksum=None,
            options={},
        )
        self.write_resource()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_resource(self, **overrides: object) -> None:
        document = {
            "resource_id": "hapmap3-hg38",
            "kind": "variant_list",
            "genome_build": "GRCh38",
            "location": str(self.alid),
            "row_count": 3,
            "checksums": {
                "alid_sha256": hashlib.sha256(ALID_BYTES).hexdigest(),
                "alid_bytes": len(ALID_BYTES),
            },
        }
        document.update(overrides)
        (self.resource_dir / "resource.yaml").write_text(
            yaml.safe_dump(document), encoding="utf-8"
        )

    def test_accepts_the_maintained_resource(self) -> None:
        resources.verify_variant_list_resource(self.declaration, repo_root=self.repo)
        record_check()

    def test_missing_resource_is_refused(self) -> None:
        declaration = IndexedSubsetDeclaration(
            name="hm3",
            resource_id="not-a-resource",
            variant_list=self.alid,
            reference_assembly="GRCh38",
            checksum=None,
            options={},
        )
        with self.assertRaises(resources.ReferenceResourceError):
            resources.verify_variant_list_resource(declaration, repo_root=self.repo)
        record_check()

    def test_assembly_location_and_kind_must_agree(self) -> None:
        for overrides in (
            {"kind": "ld_panel"},
            {"genome_build": "hg19"},
            {"location": "/somewhere/else.alid.txt"},
        ):
            self.write_resource(**overrides)
            with self.assertRaises(resources.ReferenceResourceError):
                resources.verify_variant_list_resource(
                    self.declaration, repo_root=self.repo
                )
            record_check()
        self.write_resource()

    def test_checksum_byte_and_row_mismatches_are_refused(self) -> None:
        # A declaration checksum that disagrees with the resource declaration.
        wrong_declaration = IndexedSubsetDeclaration(
            name="hm3",
            resource_id="hapmap3-hg38",
            variant_list=self.alid,
            reference_assembly="GRCh38",
            checksum="a" * 64,
            options={},
        )
        with self.assertRaises(resources.ReferenceResourceError):
            resources.verify_variant_list_resource(wrong_declaration, repo_root=self.repo)
        record_check()

        # A list whose bytes no longer match the maintained declaration.
        self.alid.write_bytes(ALID_BYTES + b"1:300:A:G\n")
        with self.assertRaises(resources.ReferenceResourceError):
            resources.verify_variant_list_resource(self.declaration, repo_root=self.repo)
        record_check()

        # A declared byte size / row count that disagrees with the file.
        self.alid.write_bytes(ALID_BYTES)
        for overrides in (
            {"checksums": {"alid_sha256": hashlib.sha256(ALID_BYTES).hexdigest(), "alid_bytes": 1}},
            {"row_count": 99},
        ):
            self.write_resource(**overrides)
            with self.assertRaises(resources.ReferenceResourceError):
                resources.verify_variant_list_resource(
                    self.declaration, repo_root=self.repo
                )
            record_check()
        self.write_resource()

    def test_missing_variant_list_is_refused(self) -> None:
        self.alid.unlink()
        with self.assertRaises(resources.ReferenceResourceError):
            resources.verify_variant_list_resource(self.declaration, repo_root=self.repo)
        record_check()


class TestIndexedSubsetRuntimeGate(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ogstores_subsets_gate_"))
        self.registry = self.tmp / "stores"
        self.store_id = "OGS-00090"
        self.bundle_root = self.registry / self.store_id
        self.bundle_root.mkdir(parents=True)
        self.repo = self.tmp / "repo"
        resource_dir = (
            self.repo / "resources" / "reference-resources" / "hapmap3-hg38"
        )
        resource_dir.mkdir(parents=True)
        self.alid = self.tmp / "hm3.grch38.alid.txt"
        self.alid.write_bytes(ALID_BYTES)
        (resource_dir / "resource.yaml").write_text(
            yaml.safe_dump(
                {
                    "resource_id": "hapmap3-hg38",
                    "kind": "variant_list",
                    "genome_build": "GRCh38",
                    "location": str(self.alid),
                    "row_count": 3,
                    "checksums": {
                        "alid_sha256": hashlib.sha256(ALID_BYTES).hexdigest(),
                        "alid_bytes": len(ALID_BYTES),
                    },
                }
            ),
            encoding="utf-8",
        )
        self._write_bundle(declare=True)
        self.capable = self.tmp / "opengwasdb-capable"
        self.capable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.capable.chmod(0o755)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_bundle(self, *, declare: bool) -> None:
        build: dict = {
            "store_id": self.store_id,
            "layout": "dense",
            "completion_state": "observed_only",
            "build": {"command": "build-dense-vcf", "options": {}},
            "post": {"top_hits": False, "rho": False, "overview": True, "validate": True},
        }
        if declare:
            build["indexed_subsets"] = [
                {
                    "name": "hm3",
                    "resource_id": "hapmap3-hg38",
                    "variant_list": str(self.alid),
                    "reference_assembly": "GRCh38",
                }
            ]
        (self.bundle_root / "release.yaml").write_text(
            yaml.safe_dump(
                {
                    "store_id": self.store_id,
                    "label": "fixture",
                    "status": "accepted",
                    "created_at": "2026-01-01T00:00:00Z",
                    "description": "fixture",
                    "generator": {"commands": ["fixture"]},
                }
            ),
            encoding="utf-8",
        )
        (self.bundle_root / "build.yaml").write_text(
            yaml.safe_dump(build), encoding="utf-8"
        )

    def test_no_declaration_is_never_gated_on_the_runtime(self) -> None:
        self._write_bundle(declare=False)
        resources.verify_indexed_subsets_for_releases(
            [("/AR", self.store_id)],
            registry_root=self.registry,
            repo_root=self.repo,
            executable="/definitely/missing",
        )
        record_check()

    def test_declaration_without_capable_runtime_is_refused(self) -> None:
        with self.assertRaises(resources.IndexedSubsetRuntimeError) as ctx:
            resources.verify_indexed_subsets_for_releases(
                [("/AR", self.store_id)],
                registry_root=self.registry,
                repo_root=self.repo,
                executable="/definitely/missing",
            )
        record_check()
        message = str(ctx.exception)
        self.assertIn("build-indexed-subset", message)
        self.assertIn("4819a2ee", message)
        self.assertIn("stores#197", message)

    def test_declaration_with_capable_runtime_and_valid_input_passes(self) -> None:
        resources.verify_indexed_subsets_for_releases(
            [("/AR", self.store_id)],
            registry_root=self.registry,
            repo_root=self.repo,
            executable=str(self.capable),
        )
        record_check()

    def test_declaration_with_capable_runtime_but_wrong_input_is_refused(self) -> None:
        self.alid.write_bytes(ALID_BYTES + b"1:300:A:G\n")
        with self.assertRaises(resources.ReferenceResourceError):
            resources.verify_indexed_subsets_for_releases(
                [("/AR", self.store_id)],
                registry_root=self.registry,
                repo_root=self.repo,
                executable=str(self.capable),
            )
        record_check()

    def test_malformed_declaration_is_a_clean_refusal(self) -> None:
        build_path = self.bundle_root / "build.yaml"
        build = yaml.safe_load(build_path.read_text(encoding="utf-8"))
        build["indexed_subsets"] = [{"name": "hm3"}]
        build_path.write_text(yaml.safe_dump(build), encoding="utf-8")
        with self.assertRaises(resources.ReferenceResourceError):
            resources.verify_indexed_subsets_for_releases(
                [("/AR", self.store_id)],
                registry_root=self.registry,
                repo_root=self.repo,
                executable=str(self.capable),
            )
        record_check()

    def test_capability_probe_distinguishes_executables(self) -> None:
        incapable = self.tmp / "opengwasdb-incapable"
        incapable.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
        incapable.chmod(0o755)
        self.assertTrue(
            resources.probe_opengwasdb_subset_capability(str(self.capable))
        )
        record_check()
        self.assertFalse(
            resources.probe_opengwasdb_subset_capability(str(incapable))
        )
        record_check()
        self.assertFalse(
            resources.probe_opengwasdb_subset_capability("/definitely/missing")
        )
        record_check()


def main() -> None:
    suite = unittest.TestSuite()
    suite.addTests(
        unittest.defaultTestLoader.loadTestsFromTestCase(TestIndexedSubsetResource)
    )
    suite.addTests(
        unittest.defaultTestLoader.loadTestsFromTestCase(TestIndexedSubsetRuntimeGate)
    )
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if not result.wasSuccessful():
        sys.exit(1)
    print(f"ALL {n_checks} CHECKS PASSED")


if __name__ == "__main__":
    main()

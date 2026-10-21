#!/usr/bin/env python3
"""Manifest Generation's own rules for a candidate's analyses.tsv (#250).

They are not part of the Release Bundle contract: Store Release Build does not
need them to consume a bundle. The emitter runs them before a candidate lands,
and `pixi run bundle-check` runs them over every committed bundle beside the
contract.

Run from the repository root:
    python3 tests/generation-rules/test_generation_rules.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from ogstores import bundle
from manifest_generation import generation_rules

HEADER = (
    "analysis_id\tstored_effect_scale\tsample_size_kind\tsample_size_scope\tsample_size\t"
    "original_effect_scale\toriginal_sd_method\tassigned_ancestry\tancestry_assignment_method\t"
    "checksum\tchecksum_algorithm\tsource_file\tsource_reader_capability"
)
ROW = (
    "FIXTURE_1\tsd\ttotal\tanalysis_level\t1000\tsd\tdeclared_standardised\tEUR\taf_assigned\t"
    f"{'a' * 64}\tsha256\t/data/source/fixture.tsv\topengwasdb.gwas-ssf"
)
INFO_COLUMNS = "\tinfo_score_threshold\timputation_score_column\timputation_score_kind\timputation_score_provenance"
FULL_DECLARATION = "\timputation_INFO\timputation_info\tprovider specification: INFO quality"


def without(column: str) -> str:
    """The one-row analyses.tsv with `column` left out of the header and the row."""
    names, values = HEADER.split("\t"), ROW.split("\t")
    keep = [i for i, name in enumerate(names) if name != column]
    return "\t".join(names[i] for i in keep) + "\n" + "\t".join(values[i] for i in keep) + "\n"


class GenerationRulesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.registry = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def errors(self, analyses: str) -> list[str]:
        root = self.registry / "OGS-00090"
        root.mkdir(exist_ok=True)
        (root / "release.yaml").write_text(yaml.safe_dump({"store_id": "OGS-00090", "status": "candidate"}))
        (root / "build.yaml").write_text(yaml.safe_dump({"store_id": "OGS-00090"}))
        (root / "analyses.tsv").write_text(analyses, encoding="utf-8")
        return list(generation_rules.check(bundle.load("OGS-00090", registry_root=self.registry)))

    def test_a_conforming_candidate_passes(self) -> None:
        self.assertEqual(self.errors(f"{HEADER}\n{ROW}\n"), [])

    def test_every_manifest_generation_column_is_required(self) -> None:
        for column in generation_rules.REQUIRED_COLUMNS:
            with self.subTest(column=column):
                self.assertTrue(any(column in error for error in self.errors(without(column))))

    def test_an_info_threshold_is_nan_or_a_number_in_zero_to_one(self) -> None:
        self.assertEqual(self.errors(f"{HEADER}{INFO_COLUMNS}\n{ROW}\tNaN\t\t\t\n"), [])
        for value in ("0", "0.6", "1"):
            with self.subTest(value=value):
                self.assertEqual(self.errors(f"{HEADER}{INFO_COLUMNS}\n{ROW}\t{value}{FULL_DECLARATION}\n"), [])
        for value in ("", "nan", "NA", "inf", "-0.01", "1.01", "missing"):
            with self.subTest(value=value):
                errors = self.errors(f"{HEADER}{INFO_COLUMNS}\n{ROW}\t{value}{FULL_DECLARATION}\n")
                self.assertTrue(any("info_score_threshold" in error for error in errors), errors)

    def test_an_info_threshold_and_its_score_declaration_go_together(self) -> None:
        for partial in ("\t\t\t", "\timputation_INFO\t\t", "\t\timputation_info\t"):
            with self.subTest(partial=partial):
                errors = self.errors(f"{HEADER}{INFO_COLUMNS}\n{ROW}\t0.6{partial}\n")
                self.assertTrue(any("incomplete imputation score declaration" in e for e in errors), errors)
        for threshold in ("NaN", ""):
            with self.subTest(threshold=threshold):
                errors = self.errors(f"{HEADER}{INFO_COLUMNS}\n{ROW}\t{threshold}{FULL_DECLARATION}\n")
                self.assertTrue(any("no numeric info_score_threshold" in e for e in errors), errors)
        errors = self.errors(f"{HEADER}{INFO_COLUMNS}\n{ROW}\t0.6\timputation_INFO\tMAF\tprovider specification\n")
        self.assertTrue(any("imputation_score_kind" in e for e in errors), errors)

    def test_a_maf_threshold_is_nan_or_a_number_in_zero_to_one_half(self) -> None:
        for value in ("NaN", "0", "0.005", "0.5"):
            with self.subTest(value=value):
                self.assertEqual(self.errors(f"{HEADER}\tmaf_threshold\n{ROW}\t{value}\n"), [])
        for value in ("", "nan", "NA", "inf", "-0.01", "0.5001", "missing"):
            with self.subTest(value=value):
                errors = self.errors(f"{HEADER}\tmaf_threshold\n{ROW}\t{value}\n")
                self.assertTrue(any("maf_threshold" in error for error in errors), errors)

    def test_an_excluded_audit_rows_requests_are_checked_too(self) -> None:
        """The shared schema reads only the rows that will be built; these rules read every row."""
        audit = ROW.replace("FIXTURE_1", "FIXTURE_2") + "\ttrue\t0.9"
        errors = self.errors(f"{HEADER}\texclude_from_build\tmaf_threshold\n{ROW}\t\tNaN\n{audit}\n")

        self.assertTrue(any("FIXTURE_2" in error and "maf_threshold" in error for error in errors), errors)

    def test_an_unreadable_analyses_table_is_an_error_not_an_exception(self) -> None:
        root = self.registry / "OGS-00091"
        root.mkdir()
        (root / "release.yaml").write_text(yaml.safe_dump({"store_id": "OGS-00091"}))

        errors = list(generation_rules.check(bundle.load("OGS-00091", registry_root=self.registry)))

        self.assertTrue(errors)


class CheckWithContractTest(unittest.TestCase):
    """Every caller holds a generated bundle to the contract and these rules at once (#250)."""

    def test_both_sets_of_errors_and_the_contracts_tolerated_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp)
            root = registry / "OGS-00090"
            root.mkdir()
            (root / "release.yaml").write_text(yaml.safe_dump({"store_id": "OGS-00090", "status": "candidate"}))
            (root / "build.yaml").write_text(yaml.safe_dump({"store_id": "OGS-00090"}))
            blank_n = ROW.replace("\t1000\t", "\t\t")
            (root / "analyses.tsv").write_text(f"{HEADER}\tmaf_threshold\n{blank_n}\t0.9\n", encoding="utf-8")
            loaded = bundle.load("OGS-00090", registry_root=registry)

            result = generation_rules.check_with_contract(loaded, registry_root=registry)

        self.assertTrue(any("label" in error for error in result), result)
        self.assertTrue(any("maf_threshold" in error for error in result), result)
        self.assertEqual([gap.citation for gap in result.tolerated], ["not buildable"])


class BundleCheckTest(unittest.TestCase):
    """`pixi run bundle-check` runs the contract and Manifest Generation's rules (#250)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.registry = Path(self._tmp.name) / "stores"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def bundle_check(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "manifest_generation.check_bundles", "--registry-root", str(self.registry)],
            capture_output=True, text=True,
        )

    def write(self, store_id: str, analyses: str, **release) -> None:
        root = self.registry / store_id
        root.mkdir(parents=True)
        (root / "release.yaml").write_text(yaml.safe_dump({
            "store_id": store_id, "label": store_id, "status": "accepted", "derived_from": None,
            "source_snapshot_id": "fixture", "created_at": "2026-10-14T00:00:00Z",
            "description": "fixture", "generator": {"commands": ["fixture"]}, **release,
        }))
        (root / "build.yaml").write_text(yaml.safe_dump({
            "store_id": store_id, "layout": "dense", "completion_state": "observed_only",
            "build": {"command": "build-dense-vcf", "options": {}},
            "post": {"top_hits": False, "rho": False, "overview": True, "validate": True},
        }))
        (root / "analyses.tsv").write_text(analyses, encoding="utf-8")

    def test_bundle_check_reports_a_generation_rule_and_a_contract_error(self) -> None:
        self.write("OGS-00090", f"{HEADER}\n{ROW}\n")
        self.write("OGS-00091", without("assigned_ancestry"))
        self.write("OGS-00092", f"{HEADER}\n{ROW}\n", label="")

        res = self.bundle_check()

        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("PASS OGS-00090", res.stdout)
        self.assertIn("FAIL OGS-00091", res.stdout)
        self.assertIn("assigned_ancestry", res.stdout)
        self.assertIn("FAIL OGS-00092", res.stdout)
        self.assertIn("label", res.stdout)


if __name__ == "__main__":
    unittest.main()

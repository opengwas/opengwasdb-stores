#!/usr/bin/env python3
"""Hermetic tests for the ukb-b provider-JSON metadata resolver (issue #219).

Fixture-scale and network-free: they pin the exact-ID/batch gates, the shared
resolution contract's scale/count vocabulary, the source-ancestry fallback, and
that cross-check disagreements are surfaced rather than hidden.

Run from the repository root:

    python3 tests/ukb-b/test_ukb_provider_metadata.py
"""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "resources" / "generators" / "ukb-b" / "provider_metadata.py"
_spec = importlib.util.spec_from_file_location("ukb_provider_metadata", MODULE_PATH)
assert _spec and _spec.loader
provider = importlib.util.module_from_spec(_spec)
sys.modules["ukb_provider_metadata"] = provider
_spec.loader.exec_module(provider)


def record(analysis_id: str, **overrides) -> dict:
    document = {
        "id": analysis_id.upper(),
        "trait": f"trait {analysis_id}",
        "batch": "UKB-b",
        "population": "European",
        "units": "SD",
        "author": "Ben Elsworth",
        "consortium": "MRC-IEU",
        "build": "HG19/GRCh37",
        "sample_size": 1000,
        "ncase": "100",
        "ncontrol": "900",
    }
    document.update(overrides)
    return document


class ProviderMetadataTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, analysis_id: str, document: dict) -> Path:
        path = self.source / f"{analysis_id}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_resolves_case_control_and_total(self) -> None:
        self._write("ukb-b-1", record("ukb-b-1"))
        self._write("ukb-b-2", record("ukb-b-2", ncase=None, ncontrol=None, sample_size=500))
        records, problems = provider.read_provider_records(["ukb-b-1", "ukb-b-2"], self.source)
        self.assertEqual(problems, [])
        self.assertEqual(records["ukb-b-1"].sample_size_kind, "case_control")
        self.assertEqual(records["ukb-b-1"].n_cases, "100")
        self.assertEqual(records["ukb-b-2"].sample_size_kind, "total")
        self.assertEqual(records["ukb-b-2"].n_cases, "")
        self.assertEqual(records["ukb-b-2"].sample_size, "500")

    def test_unit_vocabulary_and_ncase_fallback(self) -> None:
        self.assertEqual(provider._classify_stored_effect_scale("SD (kg/m^2)"), "sd")
        self.assertEqual(provider._classify_stored_effect_scale("log odds"), "log_or")
        self.assertEqual(provider._classify_stored_effect_scale("logOR"), "log_or")
        self.assertEqual(provider._classify_stored_effect_scale("hazard ratio"), "log_hazard")
        self.assertIsNone(provider._classify_stored_effect_scale("NA"))
        self.assertIsNone(provider._classify_stored_effect_scale(None))
        # Unusable unit with a case/control pair -> log_or, per the live resolver contract.
        resolved = provider.resolve_record("ukb-b-1", record("ukb-b-1", units="NA"), sha256="a" * 64, size=1)
        self.assertEqual(resolved.stored_effect_scale, "log_or")

    def test_exact_id_and_batch_are_enforced(self) -> None:
        self._write("ukb-b-1", record("ukb-b-1", id="ukb-b-999"))
        _records, problems = provider.read_provider_records(["ukb-b-1"], self.source)
        self.assertEqual(len(problems), 1)
        self.assertIn("does not match", problems[0])

        self._write("ukb-b-2", record("ukb-b-2", batch="ukb-a"))
        _records, problems = provider.read_provider_records(["ukb-b-2"], self.source)
        self.assertEqual(len(problems), 1)
        self.assertIn("batch", problems[0])

    def test_missing_json_is_a_problem_not_a_default(self) -> None:
        _records, problems = provider.read_provider_records(["ukb-b-404"], self.source)
        self.assertEqual(len(problems), 1)
        self.assertIn("not found", problems[0])

    def test_unresolvable_record_is_explicit(self) -> None:
        self._write("ukb-b-3", record("ukb-b-3", units="NA", ncase=None, ncontrol=None, sample_size=None))
        records, problems = provider.read_provider_records(["ukb-b-3"], self.source)
        self.assertEqual(problems, [])
        self.assertEqual(records["ukb-b-3"].resolution_status, "unresolved")

    def test_ancestry_is_the_provider_population_source_fallback(self) -> None:
        self._write("ukb-b-1", record("ukb-b-1"))
        records, _ = provider.read_provider_records(["ukb-b-1"], self.source)
        rows = provider.build_resolution_rows(records, inclusion_reason="declared intent")
        row = rows[0]
        self.assertEqual(row["source_ancestry_label"], "European")
        self.assertEqual(row["assigned_ancestry"], "EUR")
        self.assertEqual(row["ancestry_assignment_method"], "source_fallback")
        self.assertEqual(row["original_sd_method"], "declared_standardised")
        self.assertEqual(row["stored_effect_scale"], "sd")

    def test_cross_check_surfaces_disagreements(self) -> None:
        self._write("ukb-b-1", record("ukb-b-1"))
        records, _ = provider.read_provider_records(["ukb-b-1"], self.source)
        recorded = {
            "ukb-b-1": {
                "stored_effect_scale": "log_or",
                "sample_size_kind": "case_control",
                "sample_size": "1000",
                "n_cases": "100",
                "n_controls": "900",
                "exclude_from_build": "",
            }
        }
        check = provider.cross_check(records, recorded)
        self.assertEqual(check["compared"], 1)
        self.assertEqual([d["field"] for d in check["disagreements"]], ["stored_effect_scale"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

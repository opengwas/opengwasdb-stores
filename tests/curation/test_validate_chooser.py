#!/usr/bin/env python3
"""Tests for the chooser validation runner: curation.validate_chooser (#168).

The user-visible contract under test is that choice accuracy is measured
*conditional on retrieval* -- pairs whose correct term was not in the shortlist
are excluded and counted rather than folded into the chooser's score -- and that
the report stratifies accuracy, produces a probability reliability curve, states
its caveats, and records cost when the chooser reports it.

Everything is hermetic: the chooser is fixture-backed and no network is touched.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from curation import candidates, validate_chooser
from curation.chooser import Candidate
from curation.harvest import (
    STRATUM_ANALYTE_MEASUREMENT,
    STRATUM_DISEASE,
)
from curation.jev_chooser import FixtureJevClient, JevChooser, JevResponse
from curation.recall import ValidationPair
from curation.stub_chooser import StubChooser
from curation.validate_chooser import (
    evaluate_chooser,
    reliability_bins,
    render_report,
    report_to_json,
)

RELEASE = "efo/v3.78.0"


def make_candidate(ontology_id: str, ontology_label: str | None = None) -> Candidate:
    return Candidate(
        ontology_id=ontology_id,
        ontology_label=ontology_label if ontology_label is not None else ontology_id,
        definition="",
        parent_id="",
        parent_label="",
        channels=("exact",),
        channel_ranks=(("exact", 1),),
        is_obsolete=False,
        ontology_release=RELEASE,
    )


def pair(
    trait_label: str,
    ontology_id: str,
    stratum: str,
    *,
    is_obsolete: bool = False,
) -> ValidationPair:
    return ValidationPair(
        trait_label=trait_label,
        ontology_id=ontology_id,
        ontology_label=ontology_id,
        stratum=stratum,
        store_families=("gwas-ssf-ragged",),
        is_obsolete=is_obsolete,
    )


def write_table(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["\t".join(columns)]
    lines.extend("\t".join(row.get(column, "") for column in columns) for row in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


VALIDATION_COLUMNS = [
    "trait_label",
    "ontology_id",
    "ontology_label",
    "stratum",
    "store_families",
    "is_obsolete",
]


def shortlist_row(trait_label: str, rank: int, ontology_id: str) -> dict[str, str]:
    return {
        "trait_label": trait_label,
        "ontology_release": RELEASE,
        "shortlist_rank": str(rank),
        "ontology_id": ontology_id,
        "ontology_label": ontology_id.lower(),
        "definition": "",
        "parent_id": "",
        "parent_label": "",
        "channels": "exact",
        "channel_ranks": "exact=1",
        "is_obsolete": "false",
        "embedding_model": "",
        "embedding_index_build": "",
    }


class ValidateChooserTestCase(unittest.TestCase):
    """Shared fixture: two scored choices and one retrieval miss."""

    def setUp(self) -> None:
        self.pairs = [
            pair("Body mass index", "EFO:1", STRATUM_ANALYTE_MEASUREMENT),
            pair("Height", "EFO:3", STRATUM_ANALYTE_MEASUREMENT),
            # The shortlist exists but lacks the correct term: a retrieval miss,
            # never a choice miss.
            pair("Type 2 diabetes", "MONDO:1", STRATUM_DISEASE),
            # Obsolete pairs are excluded entirely.
            pair("Old term", "EFO:9", STRATUM_ANALYTE_MEASUREMENT, is_obsolete=True),
        ]
        self.shortlists = {
            "body mass index": [make_candidate("EFO:1"), make_candidate("EFO:2")],
            "height": [make_candidate("EFO:3")],
            "type 2 diabetes": [make_candidate("MONDO:2")],
        }
        self.chooser = StubChooser(
            {
                "body mass index": {
                    "selected_ontology_id": "EFO:1",
                    "probabilities": {"EFO:1": 0.8, "EFO:2": 0.2},
                },
                "height": {
                    "selected_ontology_id": "EFO:3",
                    "probabilities": {"EFO:3": 1.0},
                },
            }
        )

    def evaluate(self, **kwargs):
        return evaluate_chooser(
            self.pairs, self.shortlists, self.chooser, min_stratum_sample=1, **kwargs
        )


class TestConditionalAccuracy(ValidateChooserTestCase):
    def test_retrieval_misses_are_excluded_and_counted(self) -> None:
        report = self.evaluate()
        self.assertEqual(report.validation_size, 4)
        self.assertEqual(report.skipped_obsolete, 1)
        self.assertEqual(report.skipped_not_retrieved, 1)
        self.assertEqual(report.skipped_no_shortlist, 0)
        self.assertEqual(report.eligible, 2)
        self.assertEqual(report.evaluated, 2)
        self.assertAlmostEqual(report.accuracy, 1.0)

    def test_accuracy_is_per_stratum_and_aggregate(self) -> None:
        report = self.evaluate()
        analyte = report.stratum(STRATUM_ANALYTE_MEASUREMENT)
        assert analyte is not None
        self.assertEqual(analyte.evaluated, 2)
        self.assertEqual(analyte.correct, 2)
        self.assertAlmostEqual(analyte.accuracy, 1.0)
        self.assertIsNone(report.stratum(STRATUM_DISEASE))
        self.assertEqual(report.aggregate.evaluated, 2)

    def test_wrong_choice_counts_against_accuracy(self) -> None:
        shortlists = {
            **self.shortlists,
            "body mass index": [make_candidate("EFO:1"), make_candidate("EFO:2")],
        }
        chooser = StubChooser(
            {
                "body mass index": {
                    "selected_ontology_id": "EFO:2",
                    "probabilities": {"EFO:1": 0.2, "EFO:2": 0.8},
                },
                "height": {
                    "selected_ontology_id": "EFO:3",
                    "probabilities": {"EFO:3": 1.0},
                },
            }
        )
        report = evaluate_chooser(
            self.pairs, shortlists, chooser, min_stratum_sample=1
        )
        self.assertEqual(report.aggregate.correct, 1)
        self.assertAlmostEqual(report.accuracy, 0.5)

    def test_chooser_is_called_once_per_label(self) -> None:
        calls: list[str] = []

        class CountingChooser(StubChooser):
            def select(self, trait_label, candidates):  # type: ignore[override]
                calls.append(trait_label)
                return super().select(trait_label, candidates)

        chooser = CountingChooser(
            {
                "body mass index": {
                    "selected_ontology_id": "EFO:1",
                    "probabilities": {"EFO:1": 0.8, "EFO:2": 0.2},
                },
                "height": {
                    "selected_ontology_id": "EFO:3",
                    "probabilities": {"EFO:3": 1.0},
                },
            }
        )
        evaluate_chooser(self.pairs, self.shortlists, chooser, min_stratum_sample=1)
        self.assertEqual(calls, ["body mass index", "height"])


class TestReliabilityCurve(ValidateChooserTestCase):
    def test_bins_partition_probability_space(self) -> None:
        report = self.evaluate(bins=10)
        self.assertEqual(len(report.reliability), 10)
        self.assertAlmostEqual(report.reliability[0].lower, 0.0)
        self.assertAlmostEqual(report.reliability[-1].upper, 1.0)
        self.assertEqual(sum(bin_.count for bin_ in report.reliability), 2)

    def test_observed_accuracy_per_bin(self) -> None:
        report = self.evaluate(bins=10)
        for bin_ in report.reliability:
            if bin_.count:
                self.assertAlmostEqual(bin_.observed_accuracy, 1.0)

    def test_empty_records_give_empty_bins(self) -> None:
        bins = reliability_bins([], bins=5)
        self.assertEqual(len(bins), 5)
        self.assertTrue(all(b.count == 0 for b in bins))
        self.assertTrue(all(b.observed_accuracy == 0.0 for b in bins))


class TestCaveatsAndRecommendation(ValidateChooserTestCase):
    def test_caveats_state_analyte_dominance_and_missing_disease(self) -> None:
        report = self.evaluate()
        text = " ".join(report.caveats)
        self.assertIn("analyte", text)
        self.assertIn("disease stratum is unmeasured", text)
        self.assertIn("conditional on retrieval", text)

    def test_small_disease_stratum_is_flagged(self) -> None:
        pairs = self.pairs[:2] + [
            pair("Type 2 diabetes", "MONDO:1", STRATUM_DISEASE),
        ]
        shortlists = {
            **self.shortlists,
            "type 2 diabetes": [make_candidate("MONDO:1")],
        }
        chooser = StubChooser(
            {
                "body mass index": {
                    "selected_ontology_id": "EFO:1",
                    "probabilities": {"EFO:1": 0.8, "EFO:2": 0.2},
                },
                "height": {
                    "selected_ontology_id": "EFO:3",
                    "probabilities": {"EFO:3": 1.0},
                },
                "type 2 diabetes": {
                    "selected_ontology_id": "MONDO:1",
                    "probabilities": {"MONDO:1": 1.0},
                },
            }
        )
        report = evaluate_chooser(
            pairs, shortlists, chooser, min_stratum_sample=30
        )
        disease = report.stratum(STRATUM_DISEASE)
        assert disease is not None
        self.assertTrue(disease.too_small)
        self.assertIn("too small", " ".join(report.caveats))

    def test_threshold_recommendation_uses_target_accuracy(self) -> None:
        report = self.evaluate(target_accuracy=0.95)
        self.assertIsNotNone(report.recommendation.confidence_threshold)
        self.assertIn("confidence", report.recommendation.confidence_evidence)

    def test_render_and_json_reports(self) -> None:
        report = self.evaluate()
        text = render_report(report)
        self.assertIn("Chooser validation report", text)
        self.assertIn("Reliability curve", text)
        self.assertIn("Caveats", text)
        payload = json.loads(report_to_json(report))
        self.assertEqual(payload["evaluated"], 2)
        self.assertEqual(payload["aggregate"]["correct"], 2)


class TestCostTracking(unittest.TestCase):
    def test_cost_is_recorded_per_label(self) -> None:
        pairs = [pair("Body mass index", "EFO:1", STRATUM_ANALYTE_MEASUREMENT)]
        shortlists = {"body mass index": [make_candidate("EFO:1")]}
        client = FixtureJevClient(
            {
                "body mass index": JevResponse(
                    probabilities={"EFO:1": 1.0}, cost_usd=0.0125
                )
            }
        )
        chooser = JevChooser(client)
        report = evaluate_chooser(pairs, shortlists, chooser, min_stratum_sample=1)
        self.assertTrue(report.cost.tracked)
        self.assertEqual(report.cost.labels, 1)
        self.assertAlmostEqual(report.cost.total_usd, 0.0125)
        self.assertAlmostEqual(report.cost.cost_per_label, 0.0125)

    def test_offline_stub_chooser_reports_no_cost(self) -> None:
        pairs = [pair("Body mass index", "EFO:1", STRATUM_ANALYTE_MEASUREMENT)]
        shortlists = {"body mass index": [make_candidate("EFO:1")]}
        chooser = StubChooser(
            {
                "body mass index": {
                    "selected_ontology_id": "EFO:1",
                    "probabilities": {"EFO:1": 1.0},
                }
            }
        )
        report = evaluate_chooser(pairs, shortlists, chooser, min_stratum_sample=1)
        self.assertFalse(report.cost.tracked)


class TestCli(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)
        self.validation = self.td / "validation.tsv"
        write_table(
            self.validation,
            VALIDATION_COLUMNS,
            [
                {
                    "trait_label": "Body mass index",
                    "ontology_id": "EFO:1",
                    "ontology_label": "body mass index",
                    "stratum": STRATUM_ANALYTE_MEASUREMENT,
                    "store_families": "gwas-ssf-ragged",
                    "is_obsolete": "false",
                }
            ],
        )
        self.shortlists = self.td / "shortlists.tsv"
        write_table(
            self.shortlists,
            list(candidates.SHORTLIST_COLUMNS),
            [shortlist_row("body mass index", 1, "EFO:1")],
        )
        self.fixture = self.td / "stub.json"
        self.fixture.write_text(
            json.dumps(
                {
                    "body mass index": {
                        "selected_ontology_id": "EFO:1",
                        "probabilities": {"EFO:1": 1.0},
                    }
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = validate_chooser.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_offline_cli_writes_report(self) -> None:
        output = self.td / "report.txt"
        code, out, err = self.run_cli(
            [
                "--validation", str(self.validation),
                "--shortlists", str(self.shortlists),
                "--chooser", "stub",
                "--fixture", str(self.fixture),
                "--output", str(output),
            ]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "")
        text = output.read_text(encoding="utf-8")
        self.assertIn("Chooser validation report", text)
        self.assertIn("aggregate", text)

    def test_live_endpoint_without_live_flag_is_refused(self) -> None:
        code, out, err = self.run_cli(
            [
                "--validation", str(self.validation),
                "--shortlists", str(self.shortlists),
                "--chooser", "jev",
                "--jev-endpoint", "https://jev.example/decide",
            ]
        )
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("--live", err)

    def test_json_format(self) -> None:
        code, out, err = self.run_cli(
            [
                "--validation", str(self.validation),
                "--shortlists", str(self.shortlists),
                "--chooser", "stub",
                "--fixture", str(self.fixture),
                "--format", "json",
            ]
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["evaluated"], 1)


if __name__ == "__main__":
    unittest.main()

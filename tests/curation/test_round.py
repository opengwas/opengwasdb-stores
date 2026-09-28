#!/usr/bin/env python3
"""Hermetic tests for the resumable curation round (curation.round).

The contract under test is that a real round over the ukb-b queue is robust:
every trait is an independent unit whose chooser result is written atomically
to its own file, a rerun skips finished work (or repeats it when the shortlist
changed), failures are recorded and retried, a reduce step reconciles every
queued label into exactly one bucket, promotion never writes an abstention as a
term, and coverage reads the after state from the whole mapping table.

The suite is hermetic: a tiny in-memory OBO/index fixture, an injected chooser,
temporary Reference Resource copies, and no network.
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

from curation import choice, coverage, promotion, round as round_mod
from curation.chooser import NONE_SUITABLE, ChoiceResult, Chooser
from curation.jev_chooser import DEFAULT_JEV_MODEL, HttpJevClient, JevChooser
from curation.ontology import build_index_from_obo, write_index
from curation.promotion import MAPPING_COLUMNS

RELEASE = "efo/v3.94.0"

FIXTURE_OBO = """\
format-version: 1.2
ontology: efo

[Term]
id: EFO:0004340
name: body mass index
def: "A measurement of body mass index." [PMID:123]
synonym: "BMI" EXACT []

[Term]
id: EFO:0004324
name: body height
def: "A measurement of standing height." []
synonym: "height" EXACT []

[Term]
id: EFO:0004338
name: body weights and measures
def: "Any measurement of body weight." []
"""

BMI_LABEL = "body mass index"
HEIGHT_LABEL = "body height"
MYSTERY_LABEL = "mystery trait"
BMI_ID = "EFO:0004340"
HEIGHT_ID = "EFO:0004324"
OTHER_ID = "EFO:0004338"

RESOURCE_YAML = """\
resource_id: canonical-trait-mapping-efo
label: Canonical trait label to ontology-term mapping table
kind: trait_ontology_mapping
version: {version}
location: resources/reference-resources/canonical-trait-mapping-efo/mapping.tsv
location_kind: tracked_file
description: >
  Round-suite fixture resource.
status: available
"""

ANALYSES_COLUMNS = ["analysis_id", "source_label", "trait_ontology_mapping_method"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_table(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["\t".join(columns)]
    lines.extend("\t".join(row.get(column, "") for column in columns) for row in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_tsv(text: str) -> tuple[list[str], list[dict[str, str]]]:
    lines = text.splitlines()
    header = lines[0].split("\t") if lines else []
    rows = [dict(zip(header, line.split("\t"))) for line in lines[1:] if line]
    return header, rows


def shortlist_row(
    trait_label: str,
    ontology_id: str,
    ontology_label: str,
    *,
    rank: int = 1,
) -> dict[str, str]:
    from curation.candidates import SHORTLIST_COLUMNS

    row = {column: "" for column in SHORTLIST_COLUMNS}
    row.update(
        {
            "trait_label": trait_label,
            "ontology_release": RELEASE,
            "shortlist_rank": str(rank),
            "ontology_id": ontology_id,
            "ontology_label": ontology_label,
            "definition": f"Definition for {ontology_id}",
            "parent_id": f"PARENT:{ontology_id}",
            "parent_label": f"Parent of {ontology_id}",
            "channels": "exact",
            "channel_ranks": "exact=1",
            "is_obsolete": "false",
        }
    )
    return row


class RecordingChooser(Chooser):
    """A deterministic in-process chooser with recorded calls and optional cost."""

    def __init__(
        self,
        choices: dict[str, tuple[str, dict[str, float]]],
        *,
        costs: dict[str, float] | None = None,
    ) -> None:
        self.choices = choices
        self.costs = costs or {}
        self.calls: list[str] = []
        self.chooser_id = "test"
        self.chooser_version = "1"

    def select(self, trait_label: str, candidates: list) -> ChoiceResult:
        self.calls.append(trait_label)
        selected, probabilities = self.choices[trait_label]
        cost = self.costs.get(trait_label)
        return ChoiceResult(
            selected_ontology_id=selected,
            probabilities=dict(probabilities),
            chooser_id=self.chooser_id,
            chooser_version=self.chooser_version,
            input_tokens=10 if cost is not None else None,
            cost_usd=cost,
        )

    def estimate_cost_usd(self, trait_label: str, candidates: list) -> float | None:
        return self.costs.get(trait_label)


class RoundTestCase(unittest.TestCase):
    """Shared temporary workspace with an index, manifests, and a resource."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)

        self.obo = self.base / "efo.obo"
        self.obo.write_text(FIXTURE_OBO, encoding="utf-8")
        self.index = self.base / "efo.index.json"
        write_index(build_index_from_obo(self.obo, RELEASE), self.index)

        self.resource_dir = self.base / "canonical-trait-mapping-efo"
        self.resource_dir.mkdir()
        self.mapping_path = self.resource_dir / "mapping.tsv"
        write_table(self.mapping_path, list(MAPPING_COLUMNS), [])
        (self.resource_dir / "resource.yaml").write_text(
            RESOURCE_YAML.format(version=1), encoding="utf-8"
        )

        self.round_dir = self.base / "round"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def manifest(self, family: str, rows: list[dict[str, str]]) -> Path:
        path = self.base / "families" / family / "releases" / "rel" / "analyses.tsv"
        write_table(path, ANALYSES_COLUMNS, rows)
        return path

    def queue_tsv(self, labels: list[str]) -> Path:
        path = self.base / "explicit-queue.tsv"
        write_table(
            path,
            ["trait_label", "occurrence_count", "store_families"],
            [
                {"trait_label": label, "occurrence_count": "1", "store_families": "fam"}
                for label in labels
            ],
        )
        return path

    def init_round(self, **overrides: object) -> round_mod.RoundConfig:
        kwargs: dict[str, object] = {
            "index_path": self.index,
            "chooser_id": "test",
            "chooser_version": "1",
            "resource_dir": self.resource_dir,
            "shortlist_size": 10,
        }
        kwargs.update(overrides)
        return round_mod.init_round(self.round_dir, **kwargs)

    def write_shortlists(self, rows: list[dict[str, str]]) -> None:
        from curation.candidates import SHORTLIST_COLUMNS

        write_table(self.round_dir / "shortlists.tsv", list(SHORTLIST_COLUMNS), rows)

    def write_choice_result(
        self,
        label: str,
        selected: str,
        probabilities: dict[str, float],
        *,
        fingerprint: str | None = None,
    ) -> None:
        """Write a result YAML with the fingerprint reduce would compute."""
        grouped = choice.read_shortlists(self.round_dir / "shortlists.tsv")
        candidates = list(grouped[label])
        expected = round_mod.choice_fingerprint(
            "test", "1", "", round_mod.DEFAULT_JEV_CONTEXT, label, candidates
        )
        normalised = round_mod.gap_scan.normalize_trait_label(label)
        result_path, _ = round_mod.choice_file_paths(self.round_dir, normalised)
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            round_mod._yaml_dump(
                {
                    "trait_label": label,
                    "normalised_label": normalised,
                    "fingerprint": fingerprint or expected,
                    "selected_ontology_id": selected,
                    "probabilities": probabilities,
                    "chooser_id": "test",
                    "chooser_version": "1",
                }
            ),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# round-init
# ---------------------------------------------------------------------------


class RoundInitTest(RoundTestCase):
    def test_writes_pins_and_snapshots_the_mapping_table(self) -> None:
        manifest = self.manifest(
            "fam",
            [
                {
                    "analysis_id": "1",
                    "source_label": BMI_LABEL,
                    "trait_ontology_mapping_method": "unmapped",
                }
            ],
        )
        config = self.init_round(manifests=[manifest])

        self.assertEqual(config.ontology_release, RELEASE)
        self.assertEqual(config.chooser.chooser_id, "test")
        self.assertTrue((self.round_dir / "round.yaml").is_file())
        self.assertTrue(config.mapping_before_path.is_file())
        # The snapshot is header-only when the table starts empty.
        _, rows = parse_tsv(config.mapping_before_path.read_text(encoding="utf-8"))
        self.assertEqual(rows, [])

    def test_rerun_with_same_pins_is_a_noop(self) -> None:
        manifest = self.manifest("fam", [])
        first = self.init_round(manifests=[manifest])
        second = self.init_round(manifests=[manifest])
        self.assertEqual(first.created_at, second.created_at)
        self.assertEqual(first.core_signature(), second.core_signature())

    def test_rerun_with_different_pins_is_refused(self) -> None:
        manifest = self.manifest("fam", [])
        self.init_round(manifests=[manifest])
        other = self.base / "other.index.json"
        write_index(build_index_from_obo(self.obo, "efo/v9.99.0"), other)
        with self.assertRaises(round_mod.RoundStateError):
            self.init_round(manifests=[manifest], index_path=other)

    def test_exactly_one_input_kind_is_required(self) -> None:
        with self.assertRaises(round_mod.RoundConfigError):
            self.init_round()
        with self.assertRaises(round_mod.RoundConfigError):
            self.init_round(
                manifests=[self.manifest("fam", [])],
                queue_tsv=self.queue_tsv(["x"]),
            )


# ---------------------------------------------------------------------------
# gap-scan
# ---------------------------------------------------------------------------


class GapScanTest(RoundTestCase):
    def test_excludes_already_mapped_labels(self) -> None:
        write_table(
            self.mapping_path,
            list(MAPPING_COLUMNS),
            [{"trait_label": BMI_LABEL}],
        )
        queue = self.queue_tsv([BMI_LABEL, "  MYSTERY TRAIT  "])
        self.init_round(queue_tsv=queue)
        outcome = round_mod.run_gap_scan(self.round_dir)

        _, rows = parse_tsv(outcome.queue_path.read_text(encoding="utf-8"))
        self.assertEqual([row["trait_label"] for row in rows], [MYSTERY_LABEL])
        self.assertEqual(outcome.already_mapped_excluded, 1)


# ---------------------------------------------------------------------------
# candidates / pin refusal
# ---------------------------------------------------------------------------


class CandidatesTest(RoundTestCase):
    def test_pin_mismatch_refuses_to_run(self) -> None:
        queue = self.queue_tsv([BMI_LABEL])
        self.init_round(queue_tsv=queue)
        round_mod.run_gap_scan(self.round_dir)

        # Replace the pinned index with one built for another release.
        write_index(build_index_from_obo(self.obo, "efo/v9.99.0"), self.index)
        with self.assertRaises(round_mod.RoundPinError):
            round_mod.run_candidates(self.round_dir)

    def test_shortlists_from_the_pinned_index(self) -> None:
        queue = self.queue_tsv([BMI_LABEL, MYSTERY_LABEL])
        self.init_round(queue_tsv=queue)
        round_mod.run_gap_scan(self.round_dir)
        outcome = round_mod.run_candidates(self.round_dir)

        self.assertEqual(outcome.labels, 2)
        self.assertEqual(outcome.no_candidate_labels, 1)
        _, rows = parse_tsv(outcome.shortlists_path.read_text(encoding="utf-8"))
        self.assertEqual({row["trait_label"] for row in rows}, {BMI_LABEL})


# ---------------------------------------------------------------------------
# choose (map)
# ---------------------------------------------------------------------------


class ChooseTest(RoundTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.queue = self.queue_tsv([BMI_LABEL, HEIGHT_LABEL])
        self.init_round(queue_tsv=self.queue)
        round_mod.run_gap_scan(self.round_dir)
        self.write_shortlists(
            [
                shortlist_row(BMI_LABEL, BMI_ID, "body mass index"),
                shortlist_row(HEIGHT_LABEL, HEIGHT_ID, "body height"),
            ]
        )
        self.chooser = RecordingChooser(
            {
                BMI_LABEL: (BMI_ID, {BMI_ID: 1.0}),
                HEIGHT_LABEL: (HEIGHT_ID, {HEIGHT_ID: 1.0}),
            }
        )

    def _choice_files(self) -> list[Path]:
        choices = self.round_dir / "choices"
        if not choices.exists():
            return []
        return sorted(choices.rglob("*.yaml"))

    def test_writes_one_file_per_trait_and_skips_identical_fingerprint(self) -> None:
        first = round_mod.run_choose(self.round_dir, chooser=self.chooser, workers=1)
        self.assertEqual(first.chosen, 2)
        self.assertEqual(len(self._choice_files()), 2)

        second = round_mod.run_choose(self.round_dir, chooser=self.chooser, workers=1)
        self.assertEqual(second.skipped, 2)
        self.assertEqual(second.chosen, 0)
        # The chooser was not called again for the unchanged shortlists.
        self.assertEqual(len(self.chooser.calls), 2)

    def test_reruns_when_the_shortlist_changes(self) -> None:
        round_mod.run_choose(self.round_dir, chooser=self.chooser, workers=1)
        self.write_shortlists(
            [
                shortlist_row(BMI_LABEL, BMI_ID, "body mass index"),
                shortlist_row(HEIGHT_LABEL, HEIGHT_ID, "body height"),
                shortlist_row(HEIGHT_LABEL, OTHER_ID, "body weights", rank=2),
            ]
        )
        self.chooser.choices[HEIGHT_LABEL] = (
            HEIGHT_ID,
            {HEIGHT_ID: 0.9, OTHER_ID: 0.1},
        )
        outcome = round_mod.run_choose(self.round_dir, chooser=self.chooser, workers=1)
        # BMI is unchanged and skipped; HEIGHT's shortlist changed and reruns.
        self.assertEqual(outcome.skipped, 1)
        self.assertEqual(outcome.chosen, 1)

    def test_error_file_then_success_on_rerun(self) -> None:
        class FlakyChooser(RecordingChooser):
            def __init__(self, *args, fail: bool, **kwargs):
                super().__init__(*args, **kwargs)
                self.fail = fail

            def select(self, trait_label, candidates):
                if self.fail and trait_label == BMI_LABEL:
                    raise ValueError("server said no")
                return super().select(trait_label, candidates)

        flaky = FlakyChooser(self.chooser.choices, fail=True)
        outcome = round_mod.run_choose(self.round_dir, chooser=flaky, workers=1)
        self.assertEqual(outcome.failed, 1)
        normalised = round_mod.gap_scan.normalize_trait_label(BMI_LABEL)
        _, error_path = round_mod.choice_file_paths(self.round_dir, normalised)
        self.assertTrue(error_path.is_file())
        error_data = round_mod._read_result(error_path)
        assert error_data is not None
        self.assertEqual(error_data["error_class"], "ValueError")
        self.assertIn("server said no", error_data["message"])

        fixed = FlakyChooser(self.chooser.choices, fail=False)
        outcome = round_mod.run_choose(self.round_dir, chooser=fixed, workers=1)
        self.assertEqual(outcome.failed, 0)
        self.assertFalse(error_path.is_file())

    def test_api_key_never_reaches_the_error_file(self) -> None:
        import os

        os.environ["OPENGWASDB_JEV_API_KEY"] = "super-secret-key"

        class LeakyChooser(RecordingChooser):
            def select(self, trait_label, candidates):
                raise ValueError("bad key super-secret-key rejected")

        try:
            round_mod.run_choose(
                self.round_dir,
                chooser=LeakyChooser(self.chooser.choices),
                workers=1,
            )
        finally:
            del os.environ["OPENGWASDB_JEV_API_KEY"]
        _, error_path = round_mod.choice_file_paths(
            self.round_dir, round_mod.gap_scan.normalize_trait_label(HEIGHT_LABEL)
        )
        self.assertIn("***", error_path.read_text(encoding="utf-8"))
        self.assertNotIn("super-secret-key", error_path.read_text(encoding="utf-8"))

    def test_error_file_records_the_raw_response_of_a_paid_answer(self) -> None:
        class PaidFailure(ValueError):
            pass

        class PaidChooser(RecordingChooser):
            def select(self, trait_label, candidates):
                exc = PaidFailure("distribution rejected after the call")
                exc.raw_response = {
                    "answers": {"term": {"choice": "EFO:1", "confidence": 0.5}}
                }
                raise exc

        round_mod.run_choose(
            self.round_dir, chooser=PaidChooser(self.chooser.choices), workers=1
        )
        _, error_path = round_mod.choice_file_paths(
            self.round_dir, round_mod.gap_scan.normalize_trait_label(BMI_LABEL)
        )
        error_data = round_mod._read_result(error_path)
        assert error_data is not None
        self.assertEqual(
            error_data["raw_response"]["answers"]["term"]["choice"], "EFO:1"
        )

    def test_max_cost_stops_new_requests(self) -> None:
        chooser = RecordingChooser(
            self.chooser.choices,
            costs={BMI_LABEL: 0.1, HEIGHT_LABEL: 0.1},
        )
        outcome = round_mod.run_choose(
            self.round_dir, chooser=chooser, workers=1, max_cost_usd=0.05
        )
        self.assertTrue(outcome.cost_cap_reached)
        self.assertEqual(outcome.processed, 1)
        self.assertEqual(len(chooser.calls), 1)

    def test_limit_caps_a_pilot(self) -> None:
        outcome = round_mod.run_choose(
            self.round_dir, chooser=self.chooser, workers=1, limit=1
        )
        self.assertTrue(outcome.limit_reached)
        self.assertEqual(outcome.processed, 1)

    def test_limit_counts_new_work_not_already_finished_labels(self) -> None:
        round_mod.run_choose(
            self.round_dir, chooser=self.chooser, workers=1, limit=1
        )
        # BMI is already done; a second pilot must not spend its budget
        # skipping it -- it should choose HEIGHT.
        outcome = round_mod.run_choose(
            self.round_dir, chooser=self.chooser, workers=1, limit=1
        )
        self.assertEqual(outcome.processed, 1)
        self.assertEqual(outcome.chosen, 1)
        self.assertEqual(outcome.skipped, 1)

    def test_interrupt_leaves_only_complete_files(self) -> None:
        class InterruptingChooser(RecordingChooser):
            def select(self, trait_label, candidates):
                raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            round_mod.run_choose(
                self.round_dir,
                chooser=InterruptingChooser(self.chooser.choices),
                workers=1,
            )
        # No result file and no temp file survives the interrupt.
        self.assertEqual(self._choice_files(), [])
        temp_files = list((self.round_dir / "choices").rglob("*.tmp*")) if (
            self.round_dir / "choices"
        ).exists() else []
        self.assertEqual(temp_files, [])


# ---------------------------------------------------------------------------
# --max-cost-usd caps the whole round
# ---------------------------------------------------------------------------


class CostCapTest(RoundTestCase):
    """The spend cap is cumulative and accounts for in-flight requests."""

    def _build(self, labels: list[str]) -> RecordingChooser:
        queue = self.queue_tsv(labels)
        self.init_round(queue_tsv=queue)
        round_mod.run_gap_scan(self.round_dir)
        self.write_shortlists(
            [shortlist_row(label, f"EFO:{index + 1}", label) for index, label in enumerate(labels)]
        )
        return RecordingChooser(
            {label: (f"EFO:{index + 1}", {f"EFO:{index + 1}": 1.0}) for index, label in enumerate(labels)},
            costs={label: 0.10 for label in labels},
        )

    def test_concurrent_requests_cannot_overshoot_the_cap(self) -> None:
        labels = ["alpha", "beta", "gamma", "delta"]
        chooser = self._build(labels)
        outcome = round_mod.run_choose(
            self.round_dir, chooser=chooser, workers=4, max_cost_usd=0.05
        )
        self.assertTrue(outcome.cost_cap_reached)
        # Only the first in-flight request may be submitted; the rest are
        # blocked by its reservation.
        self.assertEqual(outcome.processed, 1)
        self.assertEqual(len(chooser.calls), 1)
        self.assertAlmostEqual(outcome.total_cost_usd, 0.10)

    def test_resume_does_not_overshoot_the_cap(self) -> None:
        labels = ["alpha", "beta", "gamma", "delta"]
        chooser = self._build(labels)
        first = round_mod.run_choose(
            self.round_dir, chooser=chooser, workers=1, limit=1
        )
        self.assertEqual(first.processed, 1)
        self.assertAlmostEqual(first.total_cost_usd, 0.10)

        # The remaining labels start from the recorded $0.10, already over the
        # $0.05 cap, so nothing new is submitted.
        resumed = round_mod.run_choose(
            self.round_dir, chooser=chooser, workers=1, max_cost_usd=0.05
        )
        self.assertTrue(resumed.cost_cap_reached)
        self.assertEqual(resumed.processed, 0)
        self.assertEqual(len(chooser.calls), 1)
        self.assertAlmostEqual(resumed.total_cost_usd, 0.10)


# ---------------------------------------------------------------------------
# reduce
# ---------------------------------------------------------------------------


class ReduceTest(RoundTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.queue = self.queue_tsv(
            ["alpha", "beta", "gamma", "delta", "epsilon", "zeta"]
        )
        self.init_round(queue_tsv=self.queue)
        round_mod.run_gap_scan(self.round_dir)
        self.write_shortlists(
            [
                shortlist_row("alpha", "EFO:1", "one"),
                shortlist_row("beta", "EFO:2", "two"),
                shortlist_row("gamma", "EFO:3", "three"),
                shortlist_row("delta", "EFO:4", "four"),
                shortlist_row("epsilon", "EFO:5", "five"),
            ]
        )
        self.write_choice_result("alpha", "EFO:1", {"EFO:1": 1.0})
        self.write_choice_result(
            "beta", NONE_SUITABLE, {"EFO:2": 0.0, NONE_SUITABLE: 1.0}
        )
        self.write_choice_result(
            "epsilon", "EFO:5", {"EFO:5": 1.0}, fingerprint="stale"
        )
        # gamma has an error file only; delta has no result.
        _, error_path = round_mod.choice_file_paths(self.round_dir, "gamma")
        error_path.parent.mkdir(parents=True, exist_ok=True)
        error_path.write_text(
            round_mod._yaml_dump(
                {"error_class": "JevError", "message": "boom", "attempts": 1}
            ),
            encoding="utf-8",
        )

    def test_reconciles_every_label_into_one_bucket(self) -> None:
        outcome = round_mod.run_reduce(self.round_dir)
        recon = outcome.reconciliation
        self.assertEqual(recon.queue_total, 6)
        self.assertEqual(recon.proposed, 1)
        self.assertEqual(recon.none_suitable, 1)
        self.assertEqual(recon.no_candidate, 1)
        self.assertEqual(recon.error, 1)
        self.assertEqual(recon.pending, 2)
        self.assertEqual(recon.stale, 1)
        self.assertFalse(outcome.complete)

        _, rows = parse_tsv(outcome.reconciliation_path.read_text(encoding="utf-8"))
        values = {row["metric"]: row["value"] for row in rows}
        bucket_sum = sum(
            int(values[bucket])
            for bucket in (
                round_mod.BUCKET_NO_CANDIDATE,
                round_mod.BUCKET_PENDING,
                round_mod.BUCKET_ERROR,
                round_mod.BUCKET_NONE_SUITABLE,
                round_mod.BUCKET_PROPOSED,
            )
        )
        self.assertEqual(bucket_sum, int(values["queue_total"]))

    def test_proposals_include_none_suitable_rows(self) -> None:
        outcome = round_mod.run_reduce(self.round_dir)
        _, rows = parse_tsv(outcome.proposals_path.read_text(encoding="utf-8"))
        selected = {row["trait_label"]: row["selected_ontology_id"] for row in rows}
        self.assertEqual(selected["alpha"], "EFO:1")
        self.assertEqual(selected["beta"], NONE_SUITABLE)

    def test_allow_incomplete_is_a_cli_exit_policy(self) -> None:
        # The library always reports the true completeness; a CLI caller opts
        # into proceeding with --allow-incomplete.
        outcome = round_mod.run_reduce(self.round_dir, allow_incomplete=True)
        self.assertFalse(outcome.complete)

        code = round_mod.main([
            "reduce", "--round-dir", str(self.round_dir), "--allow-incomplete"
        ])
        self.assertEqual(code, 0)


# ---------------------------------------------------------------------------
# promote: none_suitable handling
# ---------------------------------------------------------------------------


class PromoteTest(RoundTestCase):
    def _build_round(self, confidence: float, margin: float) -> None:
        queue = self.queue_tsv([BMI_LABEL])
        self.init_round(queue_tsv=queue)
        round_mod.run_gap_scan(self.round_dir)
        self.write_shortlists([shortlist_row(BMI_LABEL, BMI_ID, "body mass index")])
        from curation.choice import PROPOSAL_COLUMNS

        write_table(
            self.round_dir / "proposals.tsv",
            list(PROPOSAL_COLUMNS),
            [
                {
                    "trait_label": BMI_LABEL,
                    "selected_ontology_id": NONE_SUITABLE,
                    "selected_ontology_label": "",
                    "confidence": f"{confidence:.6f}",
                    "runner_up_id": BMI_ID,
                    "runner_up_label": "body mass index",
                    "runner_up_confidence": f"{1.0 - confidence:.6f}",
                    "runner_up_margin": f"{margin:.6f}",
                    "probabilities": json.dumps(
                        {BMI_ID: 1.0 - confidence, NONE_SUITABLE: confidence}
                    ),
                    "chooser_id": "test",
                    "chooser_version": "1",
                    "ontology_release": RELEASE,
                }
            ],
        )

    def test_confident_abstention_is_recorded_not_promoted(self) -> None:
        self._build_round(0.99, 0.98)
        outcome = round_mod.run_promote(self.round_dir)

        self.assertEqual(outcome.promotion.plan.promoted, ())
        self.assertEqual(outcome.promotion.plan.queued, ())
        self.assertEqual(len(outcome.promotion.plan.no_suitable), 1)
        self.assertTrue(outcome.no_suitable_path.is_file())
        mapping_text = self.mapping_path.read_text(encoding="utf-8")
        self.assertNotIn(BMI_LABEL, mapping_text)

    def test_uncertain_abstention_goes_to_the_review_queue(self) -> None:
        self._build_round(0.50, 0.10)
        outcome = round_mod.run_promote(self.round_dir)

        self.assertEqual(outcome.promotion.plan.promoted, ())
        self.assertEqual(outcome.promotion.plan.no_suitable, ())
        self.assertEqual(len(outcome.promotion.plan.queued), 1)
        _, rows = parse_tsv(outcome.review_queue_path.read_text(encoding="utf-8"))
        self.assertEqual(rows[0]["selected_ontology_id"], NONE_SUITABLE)


# ---------------------------------------------------------------------------
# coverage: after state from the full table
# ---------------------------------------------------------------------------


class CoverageTest(RoundTestCase):
    def test_after_state_uses_pre_existing_rows(self) -> None:
        write_table(
            self.mapping_path,
            list(MAPPING_COLUMNS),
            [{"trait_label": BMI_LABEL}],
        )
        manifest = self.manifest(
            "fam-a",
            [
                {
                    "analysis_id": "1",
                    "source_label": BMI_LABEL,
                    "trait_ontology_mapping_method": "unmapped",
                },
                {
                    "analysis_id": "2",
                    "source_label": HEIGHT_LABEL,
                    "trait_ontology_mapping_method": "unmapped",
                },
            ],
        )
        self.init_round(manifests=[manifest])
        outcome = round_mod.run_coverage(self.round_dir)

        report = outcome.report
        # No rows were added by this round (BMI already existed).
        self.assertEqual(report.rows_added, 0)
        self.assertEqual(report.analyses_resolved, 0)
        # The after state still resolves BMI from the pre-existing row.
        family = report.family("fam-a")
        assert family is not None
        self.assertEqual(family.unmapped_before, 2)
        self.assertEqual(family.unmapped_after, 1)


# ---------------------------------------------------------------------------
# Thin convenience runner
# ---------------------------------------------------------------------------


class RunnerTest(RoundTestCase):
    def test_stops_after_reduce_when_incomplete(self) -> None:
        queue = self.queue_tsv([BMI_LABEL])
        self.init_round(queue_tsv=queue)

        class FailingChooser(RecordingChooser):
            def select(self, trait_label, candidates):
                raise ValueError("upstream unavailable")

        outcome = round_mod.run_round(
            self.round_dir,
            chooser=FailingChooser({BMI_LABEL: (BMI_ID, {BMI_ID: 1.0})}),
            workers=1,
        )
        self.assertIsNone(outcome.promote)
        self.assertIsNone(outcome.coverage)
        self.assertFalse(outcome.reduce.complete)
        self.assertTrue((self.round_dir / "queue.tsv").is_file())
        self.assertTrue((self.round_dir / "proposals.tsv").is_file())
        # The mapping table was not touched by an incomplete round.
        _, rows = parse_tsv(self.mapping_path.read_text(encoding="utf-8"))
        self.assertEqual(rows, [])


class _FakeJevResponse:
    def __init__(self, data: dict, status_code: int = 200) -> None:
        self._data = data
        self.status_code = status_code
        self.headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP status {self.status_code}")

    def json(self) -> dict:
        return self._data


class _FakeJevHttpClient:
    def __init__(self, responses: list) -> None:
        self._responses = list(responses)

    def post(self, url: str, json: dict, headers: dict):
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        pass


class RealJevIdentityTest(RoundTestCase):
    """init -> choose -> reduce with a real Jev chooser and no --chooser-model."""

    def test_choose_and_reduce_agree_without_chooser_model(self) -> None:
        queue = self.queue_tsv([BMI_LABEL])
        # chooser_version defaults to the stub's "1"; init_round must derive a
        # Jev version from the model so choose and reduce fingerprints agree.
        config = self.init_round(queue_tsv=queue, chooser_id="jev")
        self.assertEqual(config.chooser.model, DEFAULT_JEV_MODEL)
        self.assertEqual(config.chooser.version, DEFAULT_JEV_MODEL)
        self.assertEqual(config.chooser.resolved_version(), DEFAULT_JEV_MODEL)

        round_mod.run_gap_scan(self.round_dir)
        self.write_shortlists([shortlist_row(BMI_LABEL, BMI_ID, "body mass index")])

        response = {
            "model": DEFAULT_JEV_MODEL,
            "answers": {
                "term": {
                    "type": "choice",
                    "choice": BMI_ID,
                    "confidence": 0.9,
                    "probabilities": {BMI_ID: 0.9, NONE_SUITABLE: 0.1},
                }
            },
            "usage": {"input_tokens": 100, "output_tokens": 5},
        }
        fake = _FakeJevHttpClient([_FakeJevResponse(response)])
        client = HttpJevClient(
            endpoint="https://example.invalid/v1/systemone",
            model=DEFAULT_JEV_MODEL,
            api_key="test-key",
            client_factory=lambda: fake,
            sleep=lambda _seconds: None,
        )
        chooser = JevChooser(
            client, chooser_id="jev", chooser_version=DEFAULT_JEV_MODEL
        )
        chosen = round_mod.run_choose(self.round_dir, chooser=chooser, workers=1)
        self.assertEqual(chosen.chosen, 1)

        reduced = round_mod.run_reduce(self.round_dir)
        self.assertTrue(reduced.complete)
        self.assertEqual(reduced.reconciliation.proposed, 1)
        self.assertEqual(reduced.reconciliation.pending, 0)


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------


class RoundCliTest(RoundTestCase):
    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = round_mod.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_round_init_and_gap_scan_cli(self) -> None:
        queue = self.queue_tsv([BMI_LABEL])
        code, _, err = self._run(
            [
                "round-init",
                "--round-dir", str(self.round_dir),
                "--index", str(self.index),
                "--chooser", "test",
                "--queue-tsv", str(queue),
                "--resource-dir", str(self.resource_dir),
            ]
        )
        self.assertEqual(code, 0, err)
        code, _, err = self._run(
            ["gap-scan", "--round-dir", str(self.round_dir)]
        )
        self.assertEqual(code, 0, err)
        self.assertTrue((self.round_dir / "queue.tsv").is_file())


if __name__ == "__main__":
    unittest.main()

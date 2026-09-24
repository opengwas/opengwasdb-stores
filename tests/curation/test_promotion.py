#!/usr/bin/env python3
"""Tests for the promotion stage: curation.promotion (issue #169).

The user-visible contract under test is that the promotion stage is the only
place a proposal becomes a committed Canonical Trait Mapping Table row, and
that it never does so on evidence too weak for a human to have skipped:

* a proposal is auto-accepted only when *both* its confidence and its
  runner-up margin clear their thresholds -- a confident winner over a near-tie
  still goes to a reviewer;
* everything below a bound is written to a review queue that carries the
  proposal, its full candidate shortlist and evidence, and the empty decision
  columns a curator fills in;
* a previously rejected ``(trait_label, ontology_id)`` pair is suppressed and
  never re-proposed or auto-accepted on later runs;
* promoting new rows bumps the Reference Resource's integer ``version``;
* the only things promotion writes are the Reference Resource directory and the
  review queue file -- no Release Manifest, bundle, or store is touched;
* promoted rows resolve through the R resolver as ``canonical_table_lookup``.

The suite is hermetic: it runs against temporary fixture tables, never the real
curated data, and invokes the real R resolver only against its own fixture.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from curation import candidates, promotion
from curation.choice import PROPOSAL_COLUMNS
from curation.chooser import Candidate
from curation.promotion import (
    AUTO_ACCEPTED,
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_MARGIN_THRESHOLD,
    MAPPING_COLUMNS,
    REASON_BELOW_BOTH,
    REASON_BELOW_CONFIDENCE,
    REASON_BELOW_MARGIN,
    REVIEW_DECISION_COLUMNS,
    REVIEW_QUEUE_COLUMNS,
    HUMAN_REVIEWED,
    MissingShortlistEvidenceError,
    ProposalFormatError,
    ProposalRecord,
    RejectionFormatError,
    ResourceYamlError,
    ReviewDecision,
    ReviewQueueFormatError,
    build_candidate_evidence,
    build_promotion_plan,
    bump_resource_version,
    format_chooser_provenance,
    gate_reason,
    load_mapping,
    main,
    read_proposals,
    read_rejections,
    render_mapping,
    run_promotion,
)

RELEASE = "efo/v3.78.0"
SHORTLIST_COLUMNS = list(candidates.SHORTLIST_COLUMNS)

RESOURCE_YAML = """\
resource_id: canonical-trait-mapping-efo
label: Canonical trait label to ontology-term mapping table
kind: trait_ontology_mapping
version: {version}
location: resources/reference-resources/canonical-trait-mapping-efo/mapping.tsv
location_kind: tracked_file
description: >
  Fixture resource for the promotion test.
status: available
"""


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def proposal_row(
    trait_label: str,
    selected_id: str,
    selected_label: str,
    confidence: float,
    runner_up_id: str = "",
    runner_up_label: str = "",
    runner_up_confidence: str = "",
    runner_up_margin: float = 1.0,
    probabilities: dict[str, float] | None = None,
    chooser_id: str = "stub",
    chooser_version: str = "1",
    ontology_release: str = RELEASE,
) -> dict[str, str]:
    """Build one proposals TSV row matching curation.choice's schema."""
    if probabilities is None:
        probabilities = {selected_id: 1.0}
    return {
        "trait_label": trait_label,
        "selected_ontology_id": selected_id,
        "selected_ontology_label": selected_label,
        "confidence": f"{confidence:.6f}",
        "runner_up_id": runner_up_id,
        "runner_up_label": runner_up_label,
        "runner_up_confidence": runner_up_confidence,
        "runner_up_margin": f"{runner_up_margin:.6f}",
        "probabilities": json.dumps(probabilities, sort_keys=True, separators=(",", ":")),
        "chooser_id": chooser_id,
        "chooser_version": chooser_version,
        "ontology_release": ontology_release,
    }


def shortlist_row(
    trait_label: str,
    rank: int,
    ontology_id: str,
    ontology_label: str,
    definition: str = "",
    parent_id: str = "",
    parent_label: str = "",
    channels: str = "exact",
    channel_ranks: str = "exact=1",
    is_obsolete: str = "false",
) -> dict[str, str]:
    """Build one shortlist TSV row matching curation.candidates' schema."""
    return {
        "trait_label": trait_label,
        "ontology_release": RELEASE,
        "shortlist_rank": str(rank),
        "ontology_id": ontology_id,
        "ontology_label": ontology_label,
        "definition": definition,
        "parent_id": parent_id,
        "parent_label": parent_label,
        "channels": channels,
        "channel_ranks": channel_ranks,
        "is_obsolete": is_obsolete,
    }


def shortlists_from_proposals(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Derive a full shortlist TSV row set from proposals rows.

    Every candidate in each proposal's distribution gets a shortlist row with
    the evidence a review entry must carry: a label (the selected/runner-up
    labels where known), a definition, a parent term, and a channel.
    """
    shortlists: list[dict[str, str]] = []
    for row in rows:
        label = row["trait_label"]
        probabilities = json.loads(row["probabilities"])
        for ontology_id in probabilities:
            if ontology_id == row["selected_ontology_id"]:
                ontology_label = row["selected_ontology_label"]
            elif ontology_id == row["runner_up_id"]:
                ontology_label = row["runner_up_label"]
            else:
                ontology_label = f"{ontology_id} label"
            shortlists.append(
                shortlist_row(
                    label,
                    len(shortlists) + 1,
                    ontology_id,
                    ontology_label,
                    definition=f"Definition for {ontology_id}",
                    parent_id=f"PARENT:{ontology_id}",
                    parent_label=f"Parent of {ontology_id}",
                    channels="exact,normalised",
                    channel_ranks="exact=1,normalised=1",
                )
            )
    return shortlists


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


def proposal_record(
    trait_label: str,
    selected_id: str,
    selected_label: str,
    confidence: float,
    runner_up_margin: float,
    runner_up_id: str = "",
    runner_up_label: str = "",
    probabilities: dict[str, float] | None = None,
) -> ProposalRecord:
    """Build a ProposalRecord directly for unit-testing the gate and plan."""
    if probabilities is None:
        probabilities = {selected_id: 1.0}
    return ProposalRecord(
        trait_label=trait_label,
        selected_ontology_id=selected_id,
        selected_ontology_label=selected_label,
        confidence=confidence,
        runner_up_id=runner_up_id,
        runner_up_label=runner_up_label,
        runner_up_confidence=None,
        runner_up_margin=runner_up_margin,
        probabilities=probabilities,
        chooser_id="stub",
        chooser_version="1",
        ontology_release=RELEASE,
    )


class PromotionTestCase(unittest.TestCase):
    """Shared temporary workspace with a fixture Reference Resource."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.resource_dir = self.base / "canonical-trait-mapping-efo"
        self.resource_dir.mkdir(parents=True)
        self.mapping_path = self.resource_dir / "mapping.tsv"
        self.resource_yaml_path = self.resource_dir / "resource.yaml"
        self.write_mapping([])
        self.write_resource_yaml(1)
        self.review_queue = self.base / "review-queue.tsv"
        self.proposals = self.base / "proposals.tsv"
        self.shortlists = self.base / "shortlists.tsv"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_mapping(self, rows: list[dict[str, str]]) -> None:
        write_table(self.mapping_path, list(MAPPING_COLUMNS), rows)

    def write_resource_yaml(self, version: int) -> None:
        self.resource_yaml_path.write_text(
            RESOURCE_YAML.format(version=version), encoding="utf-8"
        )

    def write_proposals(self, rows: list[dict[str, str]]) -> None:
        write_table(self.proposals, list(PROPOSAL_COLUMNS), rows)

    def write_shortlists(self, rows: list[dict[str, str]]) -> None:
        write_table(self.shortlists, SHORTLIST_COLUMNS, rows)

    def promote(self, **overrides: object) -> promotion.PromotionOutcome:
        kwargs: dict[str, object] = {
            "proposals_path": self.proposals,
            "review_queue_path": self.review_queue,
            "resource_dir": self.resource_dir,
            "as_of": "2026-01-02",
        }
        # A queued proposal needs a shortlist. Use the fixture written by
        # write_shortlists(), or derive one from the proposals so a test that
        # only cares about the gate still gets full evidence. Passing
        # shortlists_path=None explicitly tests the missing-evidence error.
        if "shortlists_path" not in overrides:
            if not self.shortlists.is_file() and self.proposals.is_file():
                _, rows = parse_tsv(self.proposals.read_text(encoding="utf-8"))
                if rows:
                    self.write_shortlists(shortlists_from_proposals(rows))
            if self.shortlists.is_file():
                kwargs["shortlists_path"] = self.shortlists
        kwargs.update(overrides)
        return run_promotion(**kwargs)  # type: ignore[arg-type]

    def mapping_rows(self) -> list[dict[str, str]]:
        _, rows = parse_tsv(self.mapping_path.read_text(encoding="utf-8"))
        return rows

    def review_rows(self) -> list[dict[str, str]]:
        _, rows = parse_tsv(self.review_queue.read_text(encoding="utf-8"))
        return rows

    def decide_review_row(self, **decision: str) -> None:
        """Fill decision columns in the (single-row) review queue fixture."""
        header, rows = parse_tsv(self.review_queue.read_text(encoding="utf-8"))
        self.assertEqual(len(rows), 1)
        rows[0].update(decision)
        write_table(self.review_queue, header, rows)


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


class TestGateReason(unittest.TestCase):
    """The gate is an inclusive AND over confidence and margin."""

    def test_both_bounds_are_inclusive(self) -> None:
        proposal = proposal_record("x", "EFO:1", "one", 0.85, 0.20)
        self.assertIsNone(gate_reason(proposal, 0.85, 0.20))

    def test_above_both_is_eligible(self) -> None:
        proposal = proposal_record("x", "EFO:1", "one", 0.99, 0.50)
        self.assertIsNone(gate_reason(proposal, 0.85, 0.20))

    def test_below_confidence_is_queued(self) -> None:
        proposal = proposal_record("x", "EFO:1", "one", 0.84, 0.50)
        self.assertEqual(gate_reason(proposal, 0.85, 0.20), REASON_BELOW_CONFIDENCE)

    def test_below_margin_is_queued(self) -> None:
        proposal = proposal_record("x", "EFO:1", "one", 0.99, 0.19)
        self.assertEqual(gate_reason(proposal, 0.85, 0.20), REASON_BELOW_MARGIN)

    def test_below_both_is_queued(self) -> None:
        proposal = proposal_record("x", "EFO:1", "one", 0.10, 0.01)
        self.assertEqual(gate_reason(proposal, 0.85, 0.20), REASON_BELOW_BOTH)

    def test_single_candidate_full_margin_is_eligible(self) -> None:
        proposal = proposal_record("x", "EFO:1", "one", 1.0, 1.0)
        self.assertIsNone(gate_reason(proposal, 0.85, 0.20))


class TestInvalidEvidence(PromotionTestCase):
    """NaN/inf/out-of-range evidence is rejected before it reaches the gate.

    ``NaN`` compares false against every threshold, so an unvalidated NaN
    confidence would satisfy neither ``< threshold`` nor ``>= threshold`` and
    be treated as eligible -- the exact bypass these tests pin down.
    """

    def test_nan_confidence_cannot_bypass_gate(self) -> None:
        row = proposal_row("Height", "EFO:1", "one", float("nan"), runner_up_margin=1.0)
        self.write_proposals([row])
        with self.assertRaises(ProposalFormatError):
            self.promote(shortlists_path=None)
        # Nothing was promoted or queued.
        self.assertEqual(self.mapping_rows(), [])
        self.assertFalse(self.review_queue.exists())

    def test_infinite_margin_cannot_bypass_gate(self) -> None:
        row = proposal_row("Height", "EFO:1", "one", 0.99, runner_up_margin=float("inf"))
        self.write_proposals([row])
        with self.assertRaises(ProposalFormatError):
            self.promote(shortlists_path=None)
        self.assertEqual(self.mapping_rows(), [])

    def test_negative_infinite_confidence_raises(self) -> None:
        row = proposal_row("Height", "EFO:1", "one", float("-inf"), runner_up_margin=1.0)
        self.write_proposals([row])
        with self.assertRaises(ProposalFormatError):
            self.promote(shortlists_path=None)

    def test_out_of_range_confidence_raises(self) -> None:
        row = proposal_row("Height", "EFO:1", "one", 1.5, runner_up_margin=1.0)
        self.write_proposals([row])
        with self.assertRaises(ProposalFormatError):
            self.promote(shortlists_path=None)

    def test_nan_probability_raises(self) -> None:
        row = proposal_row("Height", "EFO:1", "one", 0.99, runner_up_margin=1.0)
        row["probabilities"] = '{"EFO:1": NaN}'
        self.write_proposals([row])
        with self.assertRaises(ProposalFormatError):
            self.promote(shortlists_path=None)

    def test_direct_construction_rejects_nan_confidence(self) -> None:
        with self.assertRaises(ProposalFormatError):
            proposal_record("x", "EFO:1", "one", float("nan"), 1.0)

    def test_direct_construction_rejects_infinite_margin(self) -> None:
        with self.assertRaises(ProposalFormatError):
            proposal_record("x", "EFO:1", "one", 0.99, float("inf"))

    def test_direct_construction_rejects_out_of_range_probability(self) -> None:
        with self.assertRaises(ProposalFormatError):
            proposal_record(
                "x", "EFO:1", "one", 0.99, 1.0, probabilities={"EFO:1": 1.2}
            )


# ---------------------------------------------------------------------------
# Reading the proposals table
# ---------------------------------------------------------------------------


class TestReadProposals(PromotionTestCase):
    """The choice-stage contract is enforced on the proposals table."""

    def test_parses_every_field(self) -> None:
        self.write_proposals(
            [
                proposal_row(
                    "Body mass index",
                    "EFO:0004340",
                    "body mass index",
                    0.7,
                    runner_up_id="EFO:0004338",
                    runner_up_label="body weights and measures",
                    runner_up_confidence="0.200000",
                    runner_up_margin=0.5,
                    probabilities={"EFO:0004340": 0.7, "EFO:0004338": 0.3},
                )
            ]
        )
        records = read_proposals(self.proposals)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.trait_label, "Body mass index")
        self.assertEqual(record.selected_ontology_id, "EFO:0004340")
        self.assertEqual(record.confidence, 0.7)
        self.assertEqual(record.runner_up_id, "EFO:0004338")
        self.assertAlmostEqual(record.runner_up_margin, 0.5)
        self.assertEqual(record.probabilities, {"EFO:0004340": 0.7, "EFO:0004338": 0.3})
        self.assertEqual(record.chooser_id, "stub")
        self.assertEqual(record.ontology_release, RELEASE)

    def test_missing_column_raises(self) -> None:
        write_table(self.proposals, ["trait_label", "confidence"], [])
        with self.assertRaises(ProposalFormatError):
            read_proposals(self.proposals)

    def test_ragged_row_raises(self) -> None:
        self.proposals.write_text(
            "\t".join(PROPOSAL_COLUMNS) + "\n" + "too\tshort\n", encoding="utf-8"
        )
        with self.assertRaises(ProposalFormatError):
            read_proposals(self.proposals)

    def test_empty_selected_id_raises(self) -> None:
        self.write_proposals([proposal_row("x", "", "one", 0.9)])
        with self.assertRaises(ProposalFormatError):
            read_proposals(self.proposals)

    def test_malformed_probabilities_raises(self) -> None:
        row = proposal_row("x", "EFO:1", "one", 0.9)
        row["probabilities"] = "not json"
        self.write_proposals([row])
        with self.assertRaises(ProposalFormatError):
            read_proposals(self.proposals)


# ---------------------------------------------------------------------------
# Promotion split and schema
# ---------------------------------------------------------------------------


class TestPromotionSplit(PromotionTestCase):
    """Rows land on the correct side of both thresholds."""

    def test_above_bound_promoted_below_bound_queued(self) -> None:
        self.write_proposals(
            [
                proposal_row(
                    "Body mass index",
                    "EFO:0004340",
                    "body mass index",
                    0.95,
                    runner_up_id="EFO:0004338",
                    runner_up_label="body weights and measures",
                    runner_up_confidence="0.030000",
                    runner_up_margin=0.40,
                    probabilities={"EFO:0004340": 0.95, "EFO:0004338": 0.05},
                ),
                proposal_row(
                    "Height",
                    "EFO:0004339",
                    "body height",
                    0.60,
                    runner_up_id="EFO:0004338",
                    runner_up_label="body weights and measures",
                    runner_up_confidence="0.300000",
                    runner_up_margin=0.30,
                    probabilities={"EFO:0004339": 0.60, "EFO:0004338": 0.40},
                ),
                proposal_row(
                    "Weight",
                    "EFO:0004338",
                    "body weight",
                    0.95,
                    runner_up_id="EFO:0004340",
                    runner_up_label="body mass index",
                    runner_up_confidence="0.050000",
                    runner_up_margin=0.05,
                    probabilities={"EFO:0004338": 0.95, "EFO:0004340": 0.05},
                ),
            ]
        )

        outcome = self.promote()

        self.assertEqual(
            [row.trait_label for row in outcome.plan.promoted], ["Body mass index"]
        )
        self.assertEqual(
            {entry.proposal.trait_label for entry in outcome.plan.queued},
            {"Height", "Weight"},
        )

        promoted = self.mapping_rows()
        self.assertEqual([row["trait_label"] for row in promoted], ["Body mass index"])
        self.assertEqual(promoted[0]["trait_ontology_id"], "EFO:0004340")
        self.assertEqual(promoted[0]["review_status"], AUTO_ACCEPTED)
        self.assertEqual(promoted[0]["reviewer"], "")
        self.assertEqual(promoted[0]["reviewed_at"], "2026-01-02")
        # The exact chooser version rides in the chooser_id cell.
        self.assertEqual(promoted[0]["chooser_id"], "stub:1")

        queued = {row["trait_label"]: row for row in self.review_rows()}
        self.assertEqual(queued["Height"]["review_reason"], REASON_BELOW_CONFIDENCE)
        self.assertEqual(queued["Weight"]["review_reason"], REASON_BELOW_MARGIN)

    def test_mapping_header_is_the_resolver_contract(self) -> None:
        self.write_proposals([proposal_row("x", "EFO:1", "one", 0.99, runner_up_margin=1.0)])
        self.promote()
        header, _ = parse_tsv(self.mapping_path.read_text(encoding="utf-8"))
        self.assertEqual(header[:3], ["trait_label", "trait_ontology_id", "trait_ontology_label"])
        self.assertEqual(header, list(MAPPING_COLUMNS))

    def test_default_thresholds(self) -> None:
        self.assertEqual(DEFAULT_CONFIDENCE_THRESHOLD, 0.85)
        self.assertEqual(DEFAULT_MARGIN_THRESHOLD, 0.20)


class TestChooserProvenance(PromotionTestCase):
    """The exact chooser version is never lost on a promoted row."""

    def test_promoted_row_records_chooser_id_and_version(self) -> None:
        self.write_proposals(
            [
                proposal_row(
                    "x",
                    "EFO:1",
                    "one",
                    0.99,
                    runner_up_margin=1.0,
                    chooser_id="gpt-4o",
                    chooser_version="2024-08-06",
                )
            ]
        )
        self.promote()
        self.assertEqual(self.mapping_rows()[0]["chooser_id"], "gpt-4o:2024-08-06")

    def test_format_chooser_provenance_handles_missing_parts(self) -> None:
        self.assertEqual(format_chooser_provenance("stub", "1"), "stub:1")
        self.assertEqual(format_chooser_provenance("stub", ""), "stub")
        self.assertEqual(format_chooser_provenance("", "1"), "1")
        self.assertEqual(format_chooser_provenance("", ""), "")
        self.assertEqual(format_chooser_provenance("  stub  ", "  1  "), "stub:1")

    def test_review_queue_keeps_separate_version_column(self) -> None:
        self.write_proposals(
            [
                proposal_row(
                    "x",
                    "EFO:1",
                    "one",
                    0.10,
                    chooser_id="gpt-4o",
                    chooser_version="2024-08-06",
                )
            ]
        )
        self.promote()
        row = self.review_rows()[0]
        self.assertEqual(row["chooser_id"], "gpt-4o")
        self.assertEqual(row["chooser_version"], "2024-08-06")


class TestMarginGating(PromotionTestCase):
    """A high-confidence winner over a near-tie is still queued."""

    def test_close_tie_is_queued(self) -> None:
        self.write_proposals(
            [
                proposal_row(
                    "Close call",
                    "EFO:1",
                    "one",
                    0.90,
                    runner_up_id="EFO:2",
                    runner_up_label="two",
                    runner_up_confidence="0.100000",
                    runner_up_margin=0.01,
                    probabilities={"EFO:1": 0.51, "EFO:2": 0.49},
                )
            ]
        )
        outcome = self.promote()
        self.assertEqual(outcome.plan.promoted, ())
        self.assertEqual(len(outcome.plan.queued), 1)
        self.assertEqual(outcome.plan.queued[0].review_reason, REASON_BELOW_MARGIN)
        self.assertEqual(self.mapping_rows(), [])

    def test_wide_margin_is_promoted(self) -> None:
        self.write_proposals(
            [
                proposal_row(
                    "Clear call",
                    "EFO:1",
                    "one",
                    0.90,
                    runner_up_id="EFO:2",
                    runner_up_label="two",
                    runner_up_confidence="0.100000",
                    runner_up_margin=0.80,
                    probabilities={"EFO:1": 0.90, "EFO:2": 0.10},
                )
            ]
        )
        outcome = self.promote()
        self.assertEqual(len(outcome.plan.promoted), 1)
        self.assertEqual(outcome.plan.queued, ())


# ---------------------------------------------------------------------------
# Review queue schema and evidence
# ---------------------------------------------------------------------------


class TestReviewQueue(PromotionTestCase):
    """The queue carries the proposal, evidence, and empty decision columns."""

    def test_schema_and_empty_decisions(self) -> None:
        self.write_proposals([proposal_row("Height", "EFO:1", "one", 0.10)])
        self.promote()
        header, rows = parse_tsv(self.review_queue.read_text(encoding="utf-8"))
        self.assertEqual(header, list(REVIEW_QUEUE_COLUMNS))
        self.assertEqual(len(rows), 1)
        for column in REVIEW_DECISION_COLUMNS:
            self.assertIn(column, header)
            self.assertEqual(rows[0][column], "")

    def test_header_only_when_nothing_queued(self) -> None:
        self.write_proposals([proposal_row("x", "EFO:1", "one", 0.99, runner_up_margin=1.0)])
        self.promote()
        self.assertEqual(
            self.review_queue.read_text(encoding="utf-8"),
            "\t".join(REVIEW_QUEUE_COLUMNS) + "\n",
        )

    def test_candidates_json_carries_full_evidence(self) -> None:
        self.write_proposals(
            [
                proposal_row(
                    "Body mass index",
                    "EFO:1",
                    "body mass index",
                    0.55,
                    runner_up_id="EFO:2",
                    runner_up_label="body weights",
                    runner_up_confidence="0.450000",
                    runner_up_margin=0.10,
                    probabilities={"EFO:1": 0.55, "EFO:2": 0.45},
                )
            ]
        )
        self.write_shortlists(
            [
                shortlist_row(
                    "Body mass index",
                    1,
                    "EFO:1",
                    "body mass index",
                    definition="A measurement.",
                    parent_id="EFO:2",
                    parent_label="body weights",
                    channels="exact,normalised",
                    channel_ranks="exact=1,normalised=1",
                ),
                shortlist_row(
                    "Body mass index",
                    2,
                    "EFO:2",
                    "body weights",
                    definition="A related measurement.",
                    channels="token_overlap",
                    channel_ranks="token_overlap=1",
                ),
            ]
        )

        self.promote(shortlists_path=self.shortlists)

        _, rows = parse_tsv(self.review_queue.read_text(encoding="utf-8"))
        evidence = json.loads(rows[0]["candidates"])
        self.assertEqual([item["ontology_id"] for item in evidence], ["EFO:1", "EFO:2"])
        top = evidence[0]
        self.assertEqual(top["ontology_label"], "body mass index")
        self.assertEqual(top["definition"], "A measurement.")
        self.assertEqual(top["parent_id"], "EFO:2")
        self.assertEqual(top["parent_label"], "body weights")
        self.assertEqual(top["channels"], ["exact", "normalised"])
        self.assertEqual(top["channel_ranks"], {"exact": 1, "normalised": 1})
        self.assertFalse(top["is_obsolete"])
        self.assertAlmostEqual(top["probability"], 0.55)

    def test_missing_shortlist_raises_for_queued_proposal(self) -> None:
        self.write_proposals(
            [
                proposal_row(
                    "Body mass index",
                    "EFO:1",
                    "body mass index",
                    0.55,
                    runner_up_margin=0.10,
                )
            ]
        )
        with self.assertRaises(MissingShortlistEvidenceError):
            self.promote(shortlists_path=None)
        # Planning fails before anything is written.
        self.assertFalse(self.review_queue.exists())
        self.assertEqual(self.mapping_rows(), [])

    def test_full_shortlist_includes_unscored_candidates(self) -> None:
        self.write_proposals(
            [
                proposal_row(
                    "Body mass index",
                    "EFO:1",
                    "body mass index",
                    0.55,
                    runner_up_id="EFO:2",
                    runner_up_label="body weights",
                    runner_up_confidence="0.450000",
                    runner_up_margin=0.10,
                    probabilities={"EFO:1": 0.55, "EFO:2": 0.45},
                )
            ]
        )
        self.write_shortlists(
            [
                shortlist_row("Body mass index", 1, "EFO:1", "body mass index"),
                shortlist_row("Body mass index", 2, "EFO:2", "body weights"),
                shortlist_row("Body mass index", 3, "EFO:3", "a term the chooser did not score"),
            ]
        )
        self.promote()
        _, rows = parse_tsv(self.review_queue.read_text(encoding="utf-8"))
        evidence = json.loads(rows[0]["candidates"])
        self.assertEqual(
            [item["ontology_id"] for item in evidence], ["EFO:1", "EFO:2", "EFO:3"]
        )
        self.assertEqual(evidence[2]["ontology_label"], "a term the chooser did not score")
        self.assertEqual(evidence[2]["probability"], 0.0)

    def test_blank_candidate_label_raises(self) -> None:
        self.write_proposals([proposal_row("Body mass index", "EFO:1", "one", 0.55, runner_up_margin=0.10)])
        self.write_shortlists([shortlist_row("Body mass index", 1, "EFO:1", "")])
        with self.assertRaises(MissingShortlistEvidenceError):
            self.promote()

    def test_distribution_term_outside_shortlist_raises(self) -> None:
        self.write_proposals(
            [
                proposal_row(
                    "Body mass index",
                    "EFO:1",
                    "one",
                    0.55,
                    runner_up_margin=0.10,
                    probabilities={"EFO:1": 0.55, "EFO:2": 0.45},
                )
            ]
        )
        self.write_shortlists([shortlist_row("Body mass index", 1, "EFO:1", "one")])
        with self.assertRaises(MissingShortlistEvidenceError):
            self.promote()

    def test_build_candidate_evidence_orders_by_probability(self) -> None:
        proposal = proposal_record(
            "x",
            "EFO:1",
            "one",
            0.4,
            0.2,
            probabilities={"EFO:3": 0.2, "EFO:1": 0.4, "EFO:2": 0.4},
        )
        shortlist = [
            Candidate(
                ontology_id=ontology_id,
                ontology_label=ontology_id.lower(),
                definition="",
                parent_id="",
                parent_label="",
                channels=("exact",),
                channel_ranks=(("exact", 1),),
                is_obsolete=False,
                ontology_release=RELEASE,
            )
            for ontology_id in ("EFO:3", "EFO:1", "EFO:2")
        ]
        evidence = build_candidate_evidence(proposal, shortlist)
        # Equal probabilities fall back to shortlist order for determinism.
        self.assertEqual([item.ontology_id for item in evidence], ["EFO:1", "EFO:2", "EFO:3"])


# ---------------------------------------------------------------------------
# Rejection retention
# ---------------------------------------------------------------------------


class TestRejectionRetention(PromotionTestCase):
    """A rejected pair is suppressed and never re-proposed."""

    def setUp(self) -> None:
        super().setUp()
        self.rejections = self.base / "rejections.tsv"

    def write_rejections(self, rows: list[dict[str, str]]) -> None:
        write_table(self.rejections, ["trait_label", "ontology_id"], rows)

    def test_rejected_pair_is_not_promoted_or_queued(self) -> None:
        self.write_proposals(
            [
                proposal_row("Height", "EFO:1", "one", 0.99, runner_up_margin=1.0),
                proposal_row("Weight", "EFO:2", "two", 0.10),
            ]
        )
        self.write_rejections([{"trait_label": "Height", "ontology_id": "EFO:1"}])
        before = self.rejections.read_text(encoding="utf-8")

        outcome = self.promote(rejections_path=self.rejections)

        self.assertEqual(len(outcome.plan.suppressed), 1)
        self.assertEqual(outcome.plan.suppressed[0].trait_label, "Height")
        self.assertEqual(self.mapping_rows(), [])
        queued_labels = {row["trait_label"] for row in self.review_rows()}
        self.assertNotIn("Height", queued_labels)
        self.assertIn("Weight", queued_labels)
        # The registry is read, never rewritten.
        self.assertEqual(self.rejections.read_text(encoding="utf-8"), before)

    def test_rejection_matches_case_and_whitespace_insensitively(self) -> None:
        self.write_proposals(
            [proposal_row("  HEIGHT  ", "EFO:1", "one", 0.99, runner_up_margin=1.0)]
        )
        self.write_rejections([{"trait_label": "height", "ontology_id": "EFO:1"}])
        outcome = self.promote(rejections_path=self.rejections)
        self.assertEqual(outcome.plan.promoted, ())
        self.assertEqual(self.mapping_rows(), [])

    def test_a_different_pair_is_not_suppressed(self) -> None:
        self.write_proposals(
            [proposal_row("Height", "EFO:2", "two", 0.99, runner_up_margin=1.0)]
        )
        self.write_rejections([{"trait_label": "Height", "ontology_id": "EFO:1"}])
        outcome = self.promote(rejections_path=self.rejections)
        self.assertEqual(len(outcome.plan.promoted), 1)
        self.assertEqual(self.mapping_rows()[0]["trait_ontology_id"], "EFO:2")

    def test_reviewed_queue_reject_decisions_are_rejections(self) -> None:
        self.write_proposals(
            [proposal_row("Height", "EFO:1", "one", 0.99, runner_up_margin=1.0)]
        )
        reviewed = self.base / "reviewed.tsv"
        write_table(
            reviewed,
            ["trait_label", "selected_ontology_id", "review_decision"],
            [
                {"trait_label": "Height", "selected_ontology_id": "EFO:1", "review_decision": "reject"},
                {"trait_label": "Weight", "selected_ontology_id": "EFO:9", "review_decision": "accept"},
            ],
        )
        outcome = self.promote(rejections_path=reviewed)
        self.assertEqual(outcome.plan.promoted, ())
        self.assertEqual(len(outcome.plan.suppressed), 1)

    def test_amend_decision_suppresses_the_original_selection(self) -> None:
        self.write_proposals(
            [proposal_row("Height", "EFO:1", "one", 0.99, runner_up_margin=1.0)]
        )
        reviewed = self.base / "reviewed.tsv"
        write_table(
            reviewed,
            ["trait_label", "selected_ontology_id", "review_decision", "override_ontology_id"],
            [
                {
                    "trait_label": "Height",
                    "selected_ontology_id": "EFO:1",
                    "review_decision": "amend",
                    "override_ontology_id": "EFO:7",
                }
            ],
        )
        outcome = self.promote(rejections_path=reviewed)
        self.assertEqual(outcome.plan.promoted, ())
        self.assertEqual(len(outcome.plan.suppressed), 1)

    def test_missing_rejections_file_is_empty_not_an_error(self) -> None:
        self.assertEqual(read_rejections(self.base / "nope.tsv"), set())

    def test_rejection_registry_without_id_column_raises(self) -> None:
        bad = self.base / "bad.tsv"
        write_table(bad, ["trait_label", "notes"], [{"trait_label": "x", "notes": "y"}])
        with self.assertRaises(RejectionFormatError):
            read_rejections(bad)

    def test_suppression_persists_across_runs(self) -> None:
        self.write_proposals(
            [proposal_row("Height", "EFO:1", "one", 0.99, runner_up_margin=1.0)]
        )
        self.write_rejections([{"trait_label": "Height", "ontology_id": "EFO:1"}])
        self.promote(rejections_path=self.rejections)
        second = self.promote(rejections_path=self.rejections)
        self.assertEqual(second.plan.promoted, ())
        self.assertEqual(self.mapping_rows(), [])


# ---------------------------------------------------------------------------
# Human review round-trip
# ---------------------------------------------------------------------------


class TestHumanReviewRoundTrip(PromotionTestCase):
    """Curator decisions are applied and preserved across runs."""

    def queue_one_subthreshold_proposal(self) -> None:
        rows = [
            proposal_row(
                "Height",
                "EFO:1",
                "body height",
                0.60,
                runner_up_id="EFO:2",
                runner_up_label="body size",
                runner_up_confidence="0.300000",
                runner_up_margin=0.30,
                probabilities={"EFO:1": 0.60, "EFO:2": 0.40},
            )
        ]
        self.write_proposals(rows)
        self.write_shortlists(shortlists_from_proposals(rows))
        self.promote()

    def test_accept_promotes_human_reviewed_and_bumps_version(self) -> None:
        self.queue_one_subthreshold_proposal()
        self.decide_review_row(
            review_decision="accept", curator="Alice", curated_at="2026-03-04"
        )

        outcome = self.promote()

        self.assertEqual(outcome.version, 2)
        self.assertTrue(outcome.mapping_written)
        self.assertEqual(len(outcome.plan.human_reviewed), 1)
        mapped = self.mapping_rows()
        self.assertEqual(len(mapped), 1)
        self.assertEqual(mapped[0]["trait_label"], "Height")
        self.assertEqual(mapped[0]["trait_ontology_id"], "EFO:1")
        self.assertEqual(mapped[0]["trait_ontology_label"], "body height")
        self.assertEqual(mapped[0]["review_status"], HUMAN_REVIEWED)
        self.assertEqual(mapped[0]["reviewer"], "Alice")
        self.assertEqual(mapped[0]["reviewed_at"], "2026-03-04")
        self.assertIn("version: 2", self.resource_yaml_path.read_text(encoding="utf-8"))

    def test_amend_promotes_override_with_curator_provenance(self) -> None:
        self.queue_one_subthreshold_proposal()
        self.decide_review_row(
            review_decision="amend",
            override_ontology_id="EFO:7",
            override_ontology_label="curator preferred term",
            curator="Bob",
            curated_at="2026-03-05",
        )

        outcome = self.promote()

        self.assertEqual(outcome.version, 2)
        mapped = self.mapping_rows()
        self.assertEqual(mapped[0]["trait_ontology_id"], "EFO:7")
        self.assertEqual(mapped[0]["trait_ontology_label"], "curator preferred term")
        self.assertEqual(mapped[0]["review_status"], HUMAN_REVIEWED)
        self.assertEqual(mapped[0]["reviewer"], "Bob")
        self.assertEqual(mapped[0]["reviewed_at"], "2026-03-05")

    def test_curated_at_falls_back_to_as_of(self) -> None:
        self.queue_one_subthreshold_proposal()
        self.decide_review_row(review_decision="accept", curator="Alice")
        self.promote()
        self.assertEqual(self.mapping_rows()[0]["reviewed_at"], "2026-01-02")

    def test_reject_suppresses_and_is_preserved(self) -> None:
        self.queue_one_subthreshold_proposal()
        self.decide_review_row(
            review_decision="reject", curator="Carol", curated_at="2026-03-06"
        )

        outcome = self.promote()

        self.assertEqual(outcome.plan.promoted, ())
        self.assertEqual(self.mapping_rows(), [])
        # The rejection is preserved in the rewritten queue, not discarded.
        preserved = self.review_rows()[0]
        self.assertEqual(preserved["review_decision"], "reject")
        self.assertEqual(preserved["curator"], "Carol")
        self.assertEqual(preserved["curated_at"], "2026-03-06")
        # And it keeps suppressing on the next run.
        self.promote()
        self.assertEqual(self.mapping_rows(), [])

    def test_decided_row_is_preserved_not_duplicated(self) -> None:
        self.queue_one_subthreshold_proposal()
        self.decide_review_row(review_decision="accept", curator="Alice")
        self.promote()
        rows = self.review_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["review_decision"], "accept")
        self.assertEqual(rows[0]["curator"], "Alice")

    def test_separate_reviewed_queue_applies_and_preserves_decisions(self) -> None:
        self.queue_one_subthreshold_proposal()
        reviewed = self.base / "reviewed.tsv"
        shutil.copyfile(self.review_queue, reviewed)
        header, rows = parse_tsv(reviewed.read_text(encoding="utf-8"))
        rows[0]["review_decision"] = "accept"
        rows[0]["curator"] = "Dave"
        rows[0]["curated_at"] = "2026-03-07"
        write_table(reviewed, header, rows)

        outcome = self.promote(reviewed_queue_path=reviewed)

        self.assertEqual(outcome.version, 2)
        self.assertEqual(self.mapping_rows()[0]["reviewer"], "Dave")
        # The decision is copied into the output queue.
        self.assertEqual(self.review_rows()[0]["review_decision"], "accept")

    def test_unknown_decision_raises(self) -> None:
        self.queue_one_subthreshold_proposal()
        self.decide_review_row(review_decision="maybe")
        with self.assertRaises(ReviewQueueFormatError):
            self.promote()

    def test_amend_without_override_id_raises(self) -> None:
        self.queue_one_subthreshold_proposal()
        self.decide_review_row(review_decision="amend", override_ontology_label="x")
        with self.assertRaises(ReviewQueueFormatError):
            self.promote()

    def test_missing_reviewed_queue_raises(self) -> None:
        self.write_proposals([proposal_row("x", "EFO:1", "one", 0.99, runner_up_margin=1.0)])
        with self.assertRaises(ReviewQueueFormatError):
            self.promote(reviewed_queue_path=self.base / "nope.tsv")

    def test_accept_is_idempotent_across_runs(self) -> None:
        self.queue_one_subthreshold_proposal()
        self.decide_review_row(review_decision="accept", curator="Alice")
        self.promote()
        before = self.mapping_path.read_text(encoding="utf-8")
        second = self.promote()
        self.assertFalse(second.mapping_written)
        self.assertEqual(self.mapping_path.read_text(encoding="utf-8"), before)
        self.assertEqual(len(self.mapping_rows()), 1)

    def test_cli_applies_reviewed_queue_decisions(self) -> None:
        self.queue_one_subthreshold_proposal()
        self.decide_review_row(
            review_decision="amend",
            override_ontology_id="EFO:9",
            override_ontology_label="cli override",
            curator="Erin",
            curated_at="2026-03-08",
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(
                [
                    "--proposals", str(self.proposals),
                    "--review-queue", str(self.review_queue),
                    "--resource-dir", str(self.resource_dir),
                    "--shortlists", str(self.shortlists),
                    "--as-of", "2026-01-02",
                ]
            )
        self.assertEqual(code, 0, stderr.getvalue())
        mapped = self.mapping_rows()
        self.assertEqual(mapped[0]["trait_ontology_id"], "EFO:9")
        self.assertEqual(mapped[0]["review_status"], HUMAN_REVIEWED)
        self.assertEqual(mapped[0]["reviewer"], "Erin")


# ---------------------------------------------------------------------------
# Version bump and idempotency
# ---------------------------------------------------------------------------


class TestVersionBump(PromotionTestCase):
    """New rows bump the integer version; no new rows leave it alone."""

    def test_version_is_bumped_when_rows_promoted(self) -> None:
        self.write_proposals([proposal_row("x", "EFO:1", "one", 0.99, runner_up_margin=1.0)])
        outcome = self.promote()
        self.assertTrue(outcome.mapping_written)
        self.assertEqual(outcome.version, 2)
        self.assertIn("version: 2\n", self.resource_yaml_path.read_text(encoding="utf-8"))

    def test_second_run_does_not_duplicate_or_bump_again(self) -> None:
        self.write_proposals([proposal_row("x", "EFO:1", "one", 0.99, runner_up_margin=1.0)])
        self.promote()
        before = self.mapping_path.read_text(encoding="utf-8")
        second = self.promote()
        self.assertFalse(second.mapping_written)
        self.assertEqual(second.version, 2)
        self.assertEqual(self.mapping_path.read_text(encoding="utf-8"), before)
        self.assertEqual(len(self.mapping_rows()), 1)

    def test_no_promotions_leave_version_untouched(self) -> None:
        self.write_proposals([proposal_row("x", "EFO:1", "one", 0.10)])
        outcome = self.promote()
        self.assertFalse(outcome.mapping_written)
        self.assertEqual(outcome.version, 1)
        self.assertIn("version: 1\n", self.resource_yaml_path.read_text(encoding="utf-8"))

    def test_existing_row_is_preserved_and_duplicate_label_skipped(self) -> None:
        self.write_mapping(
            [
                {
                    "trait_label": "Body mass index",
                    "trait_ontology_id": "EFO:0004340",
                    "trait_ontology_label": "body mass index",
                    "ontology_release": RELEASE,
                    "chooser_id": "hand",
                    "confidence": "",
                    "runner_up_margin": "",
                    "review_status": "human_reviewed",
                    "reviewer": "A Curator",
                    "reviewed_at": "2025-01-01",
                }
            ]
        )
        self.write_resource_yaml(5)
        self.write_proposals(
            [
                proposal_row(
                    "Body mass index", "EFO:9999", "different", 0.99, runner_up_margin=1.0
                )
            ]
        )
        outcome = self.promote()
        self.assertEqual(outcome.plan.promoted, ())
        self.assertEqual(outcome.version, 5)
        rows = self.mapping_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trait_ontology_id"], "EFO:0004340")
        self.assertEqual(rows[0]["reviewer"], "A Curator")

    def test_bump_resource_version_is_a_targeted_rewrite(self) -> None:
        text = "resource_id: x\n# keep this comment\nversion: 7\nother: 1\n"
        bumped, version = bump_resource_version(text)
        self.assertEqual(version, 8)
        self.assertIn("# keep this comment", bumped)
        self.assertIn("version: 8\n", bumped)
        self.assertIn("other: 1\n", bumped)

    def test_bump_without_version_field_raises(self) -> None:
        with self.assertRaises(ResourceYamlError):
            bump_resource_version("resource_id: x\n")

    def test_promotion_without_resource_yaml_raises(self) -> None:
        self.resource_yaml_path.unlink()
        self.write_proposals([proposal_row("x", "EFO:1", "one", 0.99, runner_up_margin=1.0)])
        with self.assertRaises(ResourceYamlError):
            self.promote()


# ---------------------------------------------------------------------------
# Legacy mapping widening
# ---------------------------------------------------------------------------


class TestLegacyMapping(PromotionTestCase):
    """A legacy three-column table is widened rather than rejected."""

    def test_legacy_header_is_widened_and_row_preserved(self) -> None:
        self.mapping_path.write_text(
            "trait_label\ttrait_ontology_id\ttrait_ontology_label\n"
            "Legacy trait\tEFO:999\tlegacy label\n",
            encoding="utf-8",
        )
        self.write_proposals([proposal_row("New trait", "EFO:1", "one", 0.99, runner_up_margin=1.0)])
        self.promote()
        header, rows = parse_tsv(self.mapping_path.read_text(encoding="utf-8"))
        self.assertEqual(header, list(MAPPING_COLUMNS))
        self.assertEqual([row["trait_label"] for row in rows], ["Legacy trait", "New trait"])
        self.assertEqual(rows[0]["review_status"], "")
        self.assertEqual(rows[1]["review_status"], AUTO_ACCEPTED)

    def test_render_mapping_canonical_preserves_raw_rows(self) -> None:
        self.write_mapping(
            [
                {
                    "trait_label": "x",
                    "trait_ontology_id": "EFO:1",
                    "trait_ontology_label": "one",
                    "ontology_release": "",
                    "chooser_id": "",
                    "confidence": "",
                    "runner_up_margin": "",
                    "review_status": "",
                    "reviewer": "",
                    "reviewed_at": "",
                }
            ]
        )
        table = load_mapping(self.mapping_path)
        self.assertTrue(table.canonical)
        rendered = render_mapping(table, [])
        self.assertIn("x\tEFO:1\tone", rendered)


# ---------------------------------------------------------------------------
# Strict boundaries
# ---------------------------------------------------------------------------


def snapshot(root: Path) -> dict[str, bytes]:
    """Content snapshot of every file under ``root``, keyed by relative path."""
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class TestStrictBoundaries(PromotionTestCase):
    """Only the Reference Resource directory and the review queue change."""

    def test_nothing_else_in_the_workspace_is_modified(self) -> None:
        stores = self.base / "stores" / "OGS-00001"
        stores.mkdir(parents=True)
        (stores / "analyses.tsv").write_text("analysis_id\ttrait\n", encoding="utf-8")
        bundle = self.base / "families" / "fam" / "releases" / "rel"
        bundle.mkdir(parents=True)
        (bundle / "analyses.tsv").write_text("analysis_id\ttrait\n", encoding="utf-8")
        rows = [
            proposal_row("Promoted", "EFO:1", "one", 0.99, runner_up_margin=1.0),
            proposal_row("Queued", "EFO:2", "two", 0.10),
        ]
        self.write_proposals(rows)
        self.write_shortlists(shortlists_from_proposals(rows))

        before = snapshot(self.base)
        self.promote()
        after = snapshot(self.base)

        changed = {
            path
            for path in set(before) | set(after)
            if before.get(path) != after.get(path)
        }
        expected = {
            str(self.mapping_path.relative_to(self.base)),
            str(self.resource_yaml_path.relative_to(self.base)),
            str(self.review_queue.relative_to(self.base)),
        }
        self.assertEqual(changed, expected)

    def test_real_repository_resource_is_untouched(self) -> None:
        real_mapping = (
            REPO_ROOT
            / "resources/reference-resources/canonical-trait-mapping-efo/mapping.tsv"
        )
        real_yaml = (
            REPO_ROOT
            / "resources/reference-resources/canonical-trait-mapping-efo/resource.yaml"
        )
        mapping_before = real_mapping.read_bytes()
        yaml_before = real_yaml.read_bytes()

        self.write_proposals([proposal_row("x", "EFO:1", "one", 0.99, runner_up_margin=1.0)])
        self.promote()

        self.assertEqual(real_mapping.read_bytes(), mapping_before)
        self.assertEqual(real_yaml.read_bytes(), yaml_before)

    def test_no_store_path_is_written(self) -> None:
        stores = self.base / "stores"
        stores.mkdir()
        sentinel = stores / "sentinel.txt"
        sentinel.write_text("keep me", encoding="utf-8")
        self.write_proposals([proposal_row("x", "EFO:1", "one", 0.99, runner_up_margin=1.0)])
        self.promote()
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep me")


# ---------------------------------------------------------------------------
# Plan unit tests
# ---------------------------------------------------------------------------


class TestBuildPromotionPlan(unittest.TestCase):
    """The plan is the pure gate; it writes nothing."""

    def test_suppressed_queued_promoted_split(self) -> None:
        proposals = [
            proposal_record("Good", "EFO:1", "one", 0.99, 0.9),
            proposal_record("Weak", "EFO:2", "two", 0.10, 0.9),
            proposal_record("Rejected", "EFO:3", "three", 0.99, 0.9),
        ]
        evidence = {
            "weak": [
                Candidate(
                    ontology_id="EFO:2",
                    ontology_label="two",
                    definition="",
                    parent_id="",
                    parent_label="",
                    channels=("exact",),
                    channel_ranks=(("exact", 1),),
                    is_obsolete=False,
                    ontology_release=RELEASE,
                )
            ]
        }
        plan = build_promotion_plan(
            proposals,
            confidence_threshold=0.85,
            margin_threshold=0.20,
            rejections={("rejected", "EFO:3")},
            shortlist_evidence=evidence,
            reviewed_at="2026-01-02",
        )
        self.assertEqual([row.trait_label for row in plan.promoted], ["Good"])
        self.assertEqual([entry.proposal.trait_label for entry in plan.queued], ["Weak"])
        self.assertEqual([record.trait_label for record in plan.suppressed], ["Rejected"])

    def test_queued_without_evidence_raises(self) -> None:
        proposals = [proposal_record("Weak", "EFO:2", "two", 0.10, 0.9)]
        with self.assertRaises(MissingShortlistEvidenceError):
            build_promotion_plan(
                proposals,
                confidence_threshold=0.85,
                margin_threshold=0.20,
                reviewed_at="2026-01-02",
            )

    def test_accept_decision_promotes_human_reviewed(self) -> None:
        proposal = proposal_record("Weak", "EFO:2", "two", 0.10, 0.9)
        decision = ReviewDecision(
            proposal=proposal,
            decision="accept",
            override_ontology_id="",
            override_ontology_label="",
            curator="Alice",
            curated_at="2026-03-04",
            row={},
        )
        plan = build_promotion_plan(
            [proposal],
            confidence_threshold=0.85,
            margin_threshold=0.20,
            reviewed_decisions={decision.key: decision},
            reviewed_at="2026-01-02",
        )
        self.assertEqual(len(plan.promoted), 1)
        self.assertEqual(plan.promoted[0].review_status, HUMAN_REVIEWED)
        self.assertEqual(plan.promoted[0].reviewer, "Alice")
        self.assertEqual(plan.promoted[0].trait_ontology_id, "EFO:2")
        self.assertEqual(plan.promoted[0].reviewed_at, "2026-03-04")
        self.assertEqual(plan.queued, ())

    def test_amend_decision_promotes_the_override(self) -> None:
        proposal = proposal_record("Weak", "EFO:2", "two", 0.10, 0.9)
        decision = ReviewDecision(
            proposal=proposal,
            decision="amend",
            override_ontology_id="EFO:7",
            override_ontology_label="seven",
            curator="Bob",
            curated_at="2026-03-05",
            row={},
        )
        plan = build_promotion_plan(
            [proposal],
            confidence_threshold=0.85,
            margin_threshold=0.20,
            reviewed_decisions={decision.key: decision},
            reviewed_at="2026-01-02",
        )
        self.assertEqual(plan.promoted[0].trait_ontology_id, "EFO:7")
        self.assertEqual(plan.promoted[0].trait_ontology_label, "seven")
        self.assertEqual(plan.promoted[0].review_status, HUMAN_REVIEWED)

    def test_reject_decision_suppresses_without_a_rejection_set(self) -> None:
        proposal = proposal_record("Weak", "EFO:2", "two", 0.10, 0.9)
        decision = ReviewDecision(
            proposal=proposal,
            decision="reject",
            override_ontology_id="",
            override_ontology_label="",
            curator="Carol",
            curated_at="",
            row={},
        )
        plan = build_promotion_plan(
            [proposal],
            confidence_threshold=0.85,
            margin_threshold=0.20,
            reviewed_decisions={decision.key: decision},
            reviewed_at="2026-01-02",
        )
        self.assertEqual(plan.promoted, ())
        self.assertEqual(plan.queued, ())
        self.assertEqual([record.trait_label for record in plan.suppressed], ["Weak"])

    def test_existing_label_is_skipped(self) -> None:
        proposals = [proposal_record("Mapped", "EFO:1", "one", 0.99, 0.9)]
        plan = build_promotion_plan(
            proposals,
            confidence_threshold=0.85,
            margin_threshold=0.20,
            existing_labels={"mapped"},
            reviewed_at="2026-01-02",
        )
        self.assertEqual(plan.promoted, ())
        self.assertEqual(plan.queued, ())

    def test_duplicate_label_within_run_keeps_first(self) -> None:
        proposals = [
            proposal_record("Dup", "EFO:1", "one", 0.99, 0.9),
            proposal_record("Dup", "EFO:2", "two", 0.99, 0.9),
        ]
        plan = build_promotion_plan(
            proposals,
            confidence_threshold=0.85,
            margin_threshold=0.20,
            reviewed_at="2026-01-02",
        )
        self.assertEqual(len(plan.promoted), 1)
        self.assertEqual(plan.promoted[0].trait_ontology_id, "EFO:1")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestPromotionCli(PromotionTestCase):
    """The command gates proposals and writes the two outputs."""

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_cli_promotes_and_queues(self) -> None:
        rows = [
            proposal_row("Promoted", "EFO:1", "one", 0.99, runner_up_margin=1.0),
            proposal_row("Queued", "EFO:2", "two", 0.10),
        ]
        self.write_proposals(rows)
        self.write_shortlists(shortlists_from_proposals(rows))
        code, out, err = self.run_cli(
            [
                "--proposals", str(self.proposals),
                "--review-queue", str(self.review_queue),
                "--resource-dir", str(self.resource_dir),
                "--shortlists", str(self.shortlists),
                "--as-of", "2026-01-02",
            ]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "")
        self.assertIn("1 promoted", err)
        self.assertEqual(len(self.mapping_rows()), 1)
        self.assertEqual(len(self.review_rows()), 1)
        self.assertIn("version: 2", self.resource_yaml_path.read_text(encoding="utf-8"))

    def test_cli_returns_one_when_queued_without_shortlists(self) -> None:
        self.write_proposals(
            [proposal_row("Queued", "EFO:2", "two", 0.10)]
        )
        code, _, err = self.run_cli(
            [
                "--proposals", str(self.proposals),
                "--review-queue", str(self.review_queue),
                "--resource-dir", str(self.resource_dir),
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("promotion: error:", err)
        self.assertIn("shortlist", err)
        self.assertFalse(self.review_queue.exists())

    def test_cli_returns_one_on_bad_proposals(self) -> None:
        code, out, err = self.run_cli(
            [
                "--proposals", str(self.base / "missing.tsv"),
                "--review-queue", str(self.review_queue),
                "--resource-dir", str(self.resource_dir),
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("promotion: error:", err)
        self.assertFalse(self.review_queue.exists())

    def test_cli_returns_one_on_bad_shortlists(self) -> None:
        self.write_proposals([proposal_row("x", "EFO:1", "one", 0.10)])
        bad = self.base / "bad-shortlists.tsv"
        write_table(bad, ["trait_label", "ontology_id"], [])
        code, _, err = self.run_cli(
            [
                "--proposals", str(self.proposals),
                "--review-queue", str(self.review_queue),
                "--resource-dir", str(self.resource_dir),
                "--shortlists", str(bad),
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("promotion: error:", err)

    def test_cli_rejects_out_of_range_threshold(self) -> None:
        code, _, err = self.run_cli(
            [
                "--proposals", str(self.proposals),
                "--review-queue", str(self.review_queue),
                "--confidence-threshold", "1.5",
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("--confidence-threshold", err)

    def test_cli_honours_custom_thresholds(self) -> None:
        self.write_proposals(
            [proposal_row("Borderline", "EFO:1", "one", 0.70, runner_up_margin=0.10)]
        )
        code, _, err = self.run_cli(
            [
                "--proposals", str(self.proposals),
                "--review-queue", str(self.review_queue),
                "--resource-dir", str(self.resource_dir),
                "--confidence-threshold", "0.60",
                "--margin-threshold", "0.05",
                "--as-of", "2026-01-02",
            ]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.mapping_rows()), 1)


# ---------------------------------------------------------------------------
# Resolver integration
# ---------------------------------------------------------------------------


RESOLVER_SCRIPT = """\
suppressPackageStartupMessages(library(data.table))
source("resources/generators/lib/metadata_resolvers/canonical_trait_table.R")
args <- commandArgs(trailingOnly = TRUE)
mapping <- args[1]
label <- args[2]
expected_id <- args[3]
expected_label <- args[4]
table <- load_canonical_trait_table(mapping)
r <- resolve_trait_ontology_mapping(
  trait_label = label, source_ontology_id = NA_character_,
  source_ontology_label = NA_character_, canonical_table = table
)
stopifnot(identical(r$resolution_status, "resolved"))
stopifnot(identical(r$trait_ontology_mapping_method, "canonical_table_lookup"))
stopifnot(identical(r$trait_ontology_id, expected_id))
stopifnot(identical(r$trait_ontology_label, expected_label))
cat("RESOLVER_OK\\n")
"""


class TestResolverIntegration(PromotionTestCase):
    """Promoted rows resolve through the real R resolver."""

    def test_promoted_row_resolves_as_canonical_table_lookup(self) -> None:
        rscript = shutil.which("Rscript")
        if rscript is None:
            self.skipTest("Rscript not found on PATH")

        self.write_proposals(
            [
                proposal_row(
                    "Body mass index",
                    "EFO:0004340",
                    "body mass index",
                    0.97,
                    runner_up_id="EFO:0004338",
                    runner_up_label="body weights and measures",
                    runner_up_confidence="0.020000",
                    runner_up_margin=0.95,
                    probabilities={"EFO:0004340": 0.97, "EFO:0004338": 0.03},
                )
            ]
        )
        self.promote()

        script = self.base / "check_resolver.R"
        script.write_text(RESOLVER_SCRIPT, encoding="utf-8")
        result = subprocess.run(
            [
                rscript,
                str(script),
                str(self.mapping_path),
                "  BODY MASS INDEX  ",
                "EFO:0004340",
                "body mass index",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("RESOLVER_OK", result.stdout)

    def test_resolver_ignores_provenance_columns(self) -> None:
        rscript = shutil.which("Rscript")
        if rscript is None:
            self.skipTest("Rscript not found on PATH")

        self.write_proposals([proposal_row("Height", "EFO:1", "body height", 0.99, runner_up_margin=1.0)])
        self.promote()
        script = self.base / "check_columns.R"
        script.write_text(
            "suppressPackageStartupMessages(library(data.table))\n"
            'source("resources/generators/lib/metadata_resolvers/canonical_trait_table.R")\n'
            "args <- commandArgs(trailingOnly = TRUE)\n"
            "table <- load_canonical_trait_table(args[1])\n"
            'stopifnot("review_status" %in% names(table))\n'
            "r <- resolve_trait_ontology_mapping(args[2], NA_character_, NA_character_, table)\n"
            'stopifnot(identical(r$trait_ontology_mapping_method, "canonical_table_lookup"))\n'
            'cat("COLUMNS_OK\\n")\n',
            encoding="utf-8",
        )
        result = subprocess.run(
            [rscript, str(script), str(self.mapping_path), "height"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("COLUMNS_OK", result.stdout)


if __name__ == "__main__":
    unittest.main()

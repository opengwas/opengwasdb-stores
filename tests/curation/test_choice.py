#!/usr/bin/env python3
"""Tests for the choice stage: curation.chooser, curation.stub_chooser, and
curation.choice (issue #167).

The user-visible contract under test is that the choice stage turns a candidate
shortlist into a proposal that a reviewer can audit, and that it can never
propose a term candidate generation did not retrieve. Candidate generation is
the ceiling on the pipeline; the chooser must not raise that ceiling by
inventing a term.

The suite is hermetic: the only chooser exercised is the fixture-backed
:class:`~curation.stub_chooser.StubChooser`, so there is no model, no network,
and no non-determinism.

Verifies:
- the :class:`Chooser` interface returns the explicit no-proposal outcome for an
  empty shortlist and enforces shortlist membership structurally;
- a selection outside the shortlist raises ``SelectionNotInShortlistError`` and
  the CLI writes no proposal;
- the stub chooser replays selections and distributions from a mapping, JSON, or
  TSV fixture deterministically, and fails loudly on an unrecorded label;
- the proposal's winner, runner-up, confidence, and runner-up margin are
  calculated correctly, including the single-candidate margin of 1.0;
- the proposals table has the documented columns and carries chooser id,
  chooser version, and ontology release;
- the CLI reads a shortlist, runs the stub chooser, and writes the table.
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

from curation import candidates, choice
from curation.choice import (
    PROPOSAL_COLUMNS,
    ShortlistFormatError,
    build_chooser,
    build_proposal,
    build_proposals,
    format_proposals_tsv,
    read_shortlists,
)
from curation.chooser import (
    Candidate,
    ChoiceResult,
    Chooser,
    InconsistentChoiceError,
    InvalidProbabilityDistributionError,
    SelectionNotInShortlistError,
    validate_choice_result,
)
from curation.stub_chooser import (
    StubChooser,
    StubChooserError,
    load_fixture,
    parse_fixture,
    parse_fixture_tsv,
)

RELEASE = "efo/v3.78.0"

SHORTLIST_COLUMNS = list(candidates.SHORTLIST_COLUMNS)


def make_candidate(
    ontology_id: str,
    ontology_label: str | None = None,
    release: str = RELEASE,
    definition: str = "",
    parent_id: str = "",
    parent_label: str = "",
    channels: tuple[str, ...] = ("exact",),
    channel_ranks: tuple[tuple[str, int], ...] = (("exact", 1),),
    is_obsolete: bool = False,
) -> Candidate:
    """Build a shortlist candidate for a unit test."""
    return Candidate(
        ontology_id=ontology_id,
        ontology_label=ontology_label if ontology_label is not None else ontology_id,
        definition=definition,
        parent_id=parent_id,
        parent_label=parent_label,
        channels=channels,
        channel_ranks=channel_ranks,
        is_obsolete=is_obsolete,
        ontology_release=release,
    )


def shortlist_row(
    trait_label: str,
    rank: int,
    ontology_id: str,
    ontology_label: str,
    release: str = RELEASE,
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
        "ontology_release": release,
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


def write_table(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["\t".join(columns)]
    lines.extend("\t".join(row.get(column, "") for column in columns) for row in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_tsv(text: str) -> tuple[list[str], list[dict[str, str]]]:
    lines = text.splitlines()
    header = lines[0].split("\t")
    rows = [dict(zip(header, line.split("\t"))) for line in lines[1:] if line]
    return header, rows


BMI_CANDIDATES = [
    shortlist_row("Body mass index", 1, "EFO:0004340", "body mass index"),
    shortlist_row("Body mass index", 2, "EFO:0004338", "body weights and measures"),
    shortlist_row("Body mass index", 3, "EFO:0004324", "measurement"),
]


def bmi_shortlist_rows() -> list[dict[str, str]]:
    return [dict(row) for row in BMI_CANDIDATES]


class FixedChooser(Chooser):
    """A chooser that returns a canned result, for testing the interface."""

    def __init__(self, result: ChoiceResult) -> None:
        self.result = result

    def select(self, trait_label: str, candidates: list[Candidate]) -> ChoiceResult:
        return self.result


class TestCandidateFromRow(unittest.TestCase):
    """A shortlist row is parsed into the chooser's view without loss."""

    def test_parses_every_field(self) -> None:
        candidate = Candidate.from_row(
            shortlist_row(
                "Body mass index",
                1,
                "EFO:0004340",
                "body mass index",
                definition="A measurement.",
                parent_id="EFO:0004338",
                parent_label="body weights and measures",
                channels="exact,normalised",
                channel_ranks="exact=1,normalised=1",
                is_obsolete="true",
            )
        )
        self.assertEqual(candidate.ontology_id, "EFO:0004340")
        self.assertEqual(candidate.ontology_label, "body mass index")
        self.assertEqual(candidate.definition, "A measurement.")
        self.assertEqual(candidate.parent_id, "EFO:0004338")
        self.assertEqual(candidate.parent_label, "body weights and measures")
        self.assertEqual(candidate.channels, ("exact", "normalised"))
        self.assertEqual(candidate.channel_ranks, (("exact", 1), ("normalised", 1)))
        self.assertTrue(candidate.is_obsolete)
        self.assertEqual(candidate.ontology_release, RELEASE)

    def test_empty_optional_fields(self) -> None:
        candidate = Candidate.from_row(
            shortlist_row("x", 1, "EFO:1", "term", channels="", channel_ranks="")
        )
        self.assertEqual(candidate.channels, ())
        self.assertEqual(candidate.channel_ranks, ())
        self.assertFalse(candidate.is_obsolete)


class TestChooserInterface(unittest.TestCase):
    """The interface's two structural rules: no empty choice, no invented term."""

    def setUp(self) -> None:
        self.candidates = [make_candidate("EFO:1"), make_candidate("EFO:2")]

    def test_chooser_is_abstract(self) -> None:
        with self.assertRaises(TypeError):
            Chooser()  # type: ignore[abstract]

    def test_empty_shortlist_returns_no_proposal(self) -> None:
        chooser = FixedChooser(
            ChoiceResult("EFO:1", {"EFO:1": 1.0}, "fixed", "1")
        )
        self.assertIsNone(chooser.choose("anything", []))

    def test_selection_outside_shortlist_raises(self) -> None:
        chooser = FixedChooser(
            ChoiceResult(
                "EFO:999",
                {"EFO:1": 0.5, "EFO:2": 0.5},
                "fixed",
                "1",
            )
        )
        with self.assertRaises(SelectionNotInShortlistError):
            chooser.choose("Body mass index", self.candidates)

    def test_invented_probability_key_raises(self) -> None:
        chooser = FixedChooser(
            ChoiceResult(
                "EFO:1",
                {"EFO:1": 0.5, "EFO:2": 0.4, "EFO:999": 0.1},
                "fixed",
                "1",
            )
        )
        with self.assertRaises(SelectionNotInShortlistError):
            chooser.choose("Body mass index", self.candidates)

    def test_missing_candidate_probability_raises(self) -> None:
        chooser = FixedChooser(
            ChoiceResult("EFO:1", {"EFO:1": 1.0}, "fixed", "1")
        )
        with self.assertRaises(InvalidProbabilityDistributionError):
            chooser.choose("Body mass index", self.candidates)

    def test_distribution_must_sum_to_one(self) -> None:
        chooser = FixedChooser(
            ChoiceResult("EFO:1", {"EFO:1": 0.4, "EFO:2": 0.4}, "fixed", "1")
        )
        with self.assertRaises(InvalidProbabilityDistributionError):
            chooser.choose("Body mass index", self.candidates)

    def test_negative_probability_raises(self) -> None:
        chooser = FixedChooser(
            ChoiceResult("EFO:1", {"EFO:1": 1.5, "EFO:2": -0.5}, "fixed", "1")
        )
        with self.assertRaises(InvalidProbabilityDistributionError):
            chooser.choose("Body mass index", self.candidates)

    def test_selection_must_be_the_maximum(self) -> None:
        chooser = FixedChooser(
            ChoiceResult("EFO:1", {"EFO:1": 0.2, "EFO:2": 0.8}, "fixed", "1")
        )
        with self.assertRaises(InconsistentChoiceError):
            chooser.choose("Body mass index", self.candidates)

    def test_valid_result_passes(self) -> None:
        chooser = FixedChooser(
            ChoiceResult("EFO:1", {"EFO:1": 0.7, "EFO:2": 0.3}, "fixed", "1")
        )
        result = chooser.choose("Body mass index", self.candidates)
        assert result is not None
        self.assertEqual(result.selected_ontology_id, "EFO:1")

    def test_validate_accepts_a_tied_selection(self) -> None:
        result = ChoiceResult("EFO:2", {"EFO:1": 0.5, "EFO:2": 0.5}, "fixed", "1")
        validate_choice_result(result, self.candidates)


class TestStubChooser(unittest.TestCase):
    """Fixture replay is deterministic and strict about unknown labels."""

    def setUp(self) -> None:
        self.candidates = [
            make_candidate("EFO:1"),
            make_candidate("EFO:2"),
            make_candidate("EFO:3"),
        ]

    def test_replays_selection_and_distribution(self) -> None:
        chooser = StubChooser(
            {"Body mass index": {"selected_ontology_id": "EFO:1",
                                 "probabilities": {"EFO:1": 0.6, "EFO:2": 0.3, "EFO:3": 0.1}}}
        )
        result = chooser.choose("Body mass index", self.candidates)
        assert result is not None
        self.assertEqual(result.selected_ontology_id, "EFO:1")
        self.assertEqual(result.probabilities, {"EFO:1": 0.6, "EFO:2": 0.3, "EFO:3": 0.1})
        self.assertEqual(result.chooser_id, "stub")
        self.assertEqual(result.chooser_version, "1")

    def test_unmentioned_candidate_gets_zero(self) -> None:
        chooser = StubChooser(
            {"Body mass index": {"selected_ontology_id": "EFO:1",
                                 "probabilities": {"EFO:1": 1.0}}}
        )
        result = chooser.choose("Body mass index", self.candidates)
        assert result is not None
        self.assertEqual(result.probabilities["EFO:3"], 0.0)

    def test_unrecorded_label_raises(self) -> None:
        chooser = StubChooser({"Body mass index": {"selected_ontology_id": "EFO:1",
                                                   "probabilities": {"EFO:1": 1.0}}})
        with self.assertRaises(StubChooserError):
            chooser.choose("height", self.candidates)

    def test_fixture_selecting_outside_shortlist_raises(self) -> None:
        chooser = StubChooser(
            {"Body mass index": {"selected_ontology_id": "EFO:999",
                                 "probabilities": {"EFO:999": 1.0}}}
        )
        with self.assertRaises(SelectionNotInShortlistError):
            chooser.choose("Body mass index", self.candidates)

    def test_fixture_with_invented_probability_key_raises(self) -> None:
        chooser = StubChooser(
            {"Body mass index": {"selected_ontology_id": "EFO:1",
                                 "probabilities": {"EFO:1": 0.5, "EFO:999": 0.5}}}
        )
        with self.assertRaises(SelectionNotInShortlistError):
            chooser.choose("Body mass index", self.candidates)

    def test_chooser_id_and_version_are_recorded(self) -> None:
        chooser = StubChooser(
            {"Body mass index": {"selected_ontology_id": "EFO:1",
                                 "probabilities": {"EFO:1": 1.0}}},
            chooser_id="stub-test",
            chooser_version="9.9",
        )
        result = chooser.choose("Body mass index", self.candidates)
        assert result is not None
        self.assertEqual(result.chooser_id, "stub-test")
        self.assertEqual(result.chooser_version, "9.9")

    def test_list_of_records_fixture(self) -> None:
        chooser = StubChooser(
            [
                {"trait_label": "Body mass index", "selected_ontology_id": "EFO:1",
                 "probabilities": {"EFO:1": 1.0}},
            ]
        )
        result = chooser.choose("Body mass index", self.candidates)
        assert result is not None
        self.assertEqual(result.selected_ontology_id, "EFO:1")

    def test_missing_selection_is_a_fixture_error(self) -> None:
        with self.assertRaises(StubChooserError):
            parse_fixture({"Body mass index": {"probabilities": {"EFO:1": 1.0}}})

    def test_malformed_fixture_shape_raises(self) -> None:
        with self.assertRaises(StubChooserError):
            parse_fixture("not a fixture")


class TestFixtureFiles(unittest.TestCase):
    """JSON and TSV fixture forms load into the same recorded choices."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_json_fixture(self) -> None:
        path = self.td / "fixture.json"
        path.write_text(
            json.dumps(
                {
                    "Body mass index": {
                        "selected_ontology_id": "EFO:1",
                        "probabilities": {"EFO:1": 0.8, "EFO:2": 0.2},
                    }
                }
            ),
            encoding="utf-8",
        )
        choices = load_fixture(path)
        self.assertEqual(choices["Body mass index"].selected_ontology_id, "EFO:1")
        self.assertEqual(choices["Body mass index"].probabilities["EFO:2"], 0.2)

    def test_tsv_fixture_with_json_probabilities(self) -> None:
        text = (
            "trait_label\tselected_ontology_id\tprobabilities\n"
            'Body mass index\tEFO:1\t{"EFO:1": 0.8, "EFO:2": 0.2}\n'
        )
        choices = parse_fixture_tsv(text)
        self.assertEqual(choices["Body mass index"].probabilities["EFO:1"], 0.8)

    def test_tsv_fixture_with_compact_probabilities(self) -> None:
        text = (
            "trait_label\tselected_ontology_id\tprobabilities\n"
            "Body mass index\tEFO:1\tEFO:1=0.8,EFO:2=0.2\n"
        )
        choices = parse_fixture_tsv(text)
        self.assertEqual(choices["Body mass index"].probabilities["EFO:2"], 0.2)

    def test_missing_fixture_file_raises(self) -> None:
        with self.assertRaises(StubChooserError):
            load_fixture(self.td / "nope.json")

    def test_tsv_fixture_missing_column_raises(self) -> None:
        with self.assertRaises(StubChooserError):
            parse_fixture_tsv("trait_label\tprobabilities\nx\t{}\n")


class TestProposalCalculations(unittest.TestCase):
    """Winner, runner-up, confidence, and margin arithmetic."""

    def setUp(self) -> None:
        self.candidates = [
            make_candidate("EFO:1", "body mass index"),
            make_candidate("EFO:2", "body weights and measures"),
            make_candidate("EFO:3", "measurement"),
        ]

    def test_winner_runner_up_and_margin(self) -> None:
        result = ChoiceResult(
            "EFO:1", {"EFO:1": 0.7, "EFO:2": 0.2, "EFO:3": 0.1}, "stub", "1"
        )
        proposal = build_proposal("Body mass index", self.candidates, result)
        self.assertEqual(proposal.selected_ontology_id, "EFO:1")
        self.assertEqual(proposal.selected_ontology_label, "body mass index")
        self.assertEqual(proposal.confidence, 0.7)
        self.assertEqual(proposal.runner_up_id, "EFO:2")
        self.assertEqual(proposal.runner_up_label, "body weights and measures")
        self.assertEqual(proposal.runner_up_confidence, 0.2)
        self.assertAlmostEqual(proposal.runner_up_margin, 0.5)

    def test_single_candidate_has_no_runner_up_and_full_margin(self) -> None:
        result = ChoiceResult("EFO:1", {"EFO:1": 1.0}, "stub", "1")
        proposal = build_proposal("Body mass index", [self.candidates[0]], result)
        self.assertEqual(proposal.runner_up_id, "")
        self.assertEqual(proposal.runner_up_label, "")
        self.assertIsNone(proposal.runner_up_confidence)
        self.assertEqual(proposal.runner_up_margin, 1.0)

    def test_runner_up_tie_is_broken_by_shortlist_order(self) -> None:
        result = ChoiceResult(
            "EFO:1", {"EFO:1": 0.6, "EFO:2": 0.2, "EFO:3": 0.2}, "stub", "1"
        )
        proposal = build_proposal("Body mass index", self.candidates, result)
        self.assertEqual(proposal.runner_up_id, "EFO:2")

    def test_selected_need_not_be_first_in_shortlist(self) -> None:
        # The chooser may promote a lower-ranked candidate; the winner is the
        # selected term, not the shortlist's rank 1.
        result = ChoiceResult(
            "EFO:2", {"EFO:1": 0.2, "EFO:2": 0.7, "EFO:3": 0.1}, "stub", "1"
        )
        proposal = build_proposal("Body mass index", self.candidates, result)
        self.assertEqual(proposal.selected_ontology_id, "EFO:2")
        self.assertEqual(proposal.confidence, 0.7)
        self.assertEqual(proposal.runner_up_id, "EFO:1")
        self.assertAlmostEqual(proposal.runner_up_margin, 0.5)

    def test_probabilities_are_serialized_as_sorted_json(self) -> None:
        result = ChoiceResult(
            "EFO:1", {"EFO:3": 0.1, "EFO:1": 0.7, "EFO:2": 0.2}, "stub", "1"
        )
        proposal = build_proposal("Body mass index", self.candidates, result)
        serialized = proposal.to_row()[PROPOSAL_COLUMNS.index("probabilities")]
        self.assertEqual(
            json.loads(serialized), {"EFO:1": 0.7, "EFO:2": 0.2, "EFO:3": 0.1}
        )
        self.assertEqual(
            serialized, '{"EFO:1":0.7,"EFO:2":0.2,"EFO:3":0.1}'
        )


class TestProposalTable(unittest.TestCase):
    """The rendered table has the documented shape and provenance."""

    def setUp(self) -> None:
        self.candidates = [
            make_candidate("EFO:1", "body mass index"),
            make_candidate("EFO:2", "body weights and measures"),
        ]

    def test_header_only_when_no_proposals(self) -> None:
        text = format_proposals_tsv([])
        self.assertEqual(text, "\t".join(PROPOSAL_COLUMNS) + "\n")

    def test_row_shape_and_metadata(self) -> None:
        result = ChoiceResult(
            "EFO:1", {"EFO:1": 0.7, "EFO:2": 0.3}, "stub-test", "2.0"
        )
        proposal = build_proposal("Body mass index", self.candidates, result)
        header, rows = parse_tsv(format_proposals_tsv([proposal]))
        self.assertEqual(header, list(PROPOSAL_COLUMNS))
        row = rows[0]
        self.assertEqual(row["trait_label"], "Body mass index")
        self.assertEqual(row["selected_ontology_id"], "EFO:1")
        self.assertEqual(row["selected_ontology_label"], "body mass index")
        self.assertEqual(row["confidence"], "0.700000")
        self.assertEqual(row["runner_up_id"], "EFO:2")
        self.assertEqual(row["runner_up_confidence"], "0.300000")
        self.assertEqual(row["runner_up_margin"], "0.400000")
        self.assertEqual(row["chooser_id"], "stub-test")
        self.assertEqual(row["chooser_version"], "2.0")
        self.assertEqual(row["ontology_release"], RELEASE)

    def test_single_candidate_row_leaves_runner_up_blank(self) -> None:
        result = ChoiceResult("EFO:1", {"EFO:1": 1.0}, "stub", "1")
        proposal = build_proposal("Body mass index", [self.candidates[0]], result)
        _, rows = parse_tsv(format_proposals_tsv([proposal]))
        row = rows[0]
        self.assertEqual(row["runner_up_id"], "")
        self.assertEqual(row["runner_up_label"], "")
        self.assertEqual(row["runner_up_confidence"], "")
        self.assertEqual(row["runner_up_margin"], "1.000000")


class TestBuildProposals(unittest.TestCase):
    """A shortlist with no candidates contributes no proposal."""

    def test_empty_shortlist_is_skipped(self) -> None:
        chooser = StubChooser(
            {"Body mass index": {"selected_ontology_id": "EFO:1",
                                 "probabilities": {"EFO:1": 1.0}}}
        )
        proposals = build_proposals(
            {"Body mass index": [make_candidate("EFO:1")], "empty": []},
            chooser,
        )
        self.assertEqual(
            [proposal.trait_label for proposal in proposals], ["Body mass index"]
        )


class TestReadShortlists(unittest.TestCase):
    """The shortlist contract from curation.candidates is enforced."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_groups_by_trait_label_preserving_order(self) -> None:
        path = self.td / "shortlists.tsv"
        write_table(
            path,
            SHORTLIST_COLUMNS,
            bmi_shortlist_rows()
            + [shortlist_row("height", 1, "EFO:4", "height")],
        )
        grouped = read_shortlists(path)
        self.assertEqual(list(grouped), ["Body mass index", "height"])
        self.assertEqual(len(grouped["Body mass index"]), 3)
        self.assertEqual(grouped["Body mass index"][0].ontology_id, "EFO:0004340")

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(ShortlistFormatError):
            read_shortlists(self.td / "nope.tsv")

    def test_missing_column_raises(self) -> None:
        path = self.td / "bad.tsv"
        write_table(path, ["trait_label", "ontology_id"], [{"trait_label": "x", "ontology_id": "EFO:1"}])
        with self.assertRaises(ShortlistFormatError):
            read_shortlists(path)

    def test_ragged_row_raises(self) -> None:
        path = self.td / "ragged.tsv"
        path.write_text(
            "\t".join(SHORTLIST_COLUMNS) + "\n" + "too\tshort\n", encoding="utf-8"
        )
        with self.assertRaises(ShortlistFormatError):
            read_shortlists(path)


class TestBuildChooser(unittest.TestCase):
    """The chooser factory resolves the stub and rejects unknown names."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)
        self.fixture = self.td / "fixture.json"
        self.fixture.write_text(
            json.dumps(
                {"Body mass index": {"selected_ontology_id": "EFO:1",
                                     "probabilities": {"EFO:1": 1.0}}}
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_stub_chooser_requires_fixture(self) -> None:
        with self.assertRaises(choice.ChoiceError):
            build_chooser("stub", None)

    def test_unknown_chooser_raises(self) -> None:
        with self.assertRaises(choice.ChoiceError):
            build_chooser("magic", self.fixture)

    def test_stub_chooser_is_built(self) -> None:
        self.assertIsInstance(build_chooser("stub", self.fixture), StubChooser)


class TestChoiceCli(unittest.TestCase):
    """The command reads a shortlist, runs the chooser, and writes the table."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)
        self.shortlists = self.td / "shortlists.tsv"
        write_table(
            self.shortlists,
            SHORTLIST_COLUMNS,
            bmi_shortlist_rows()
            + [shortlist_row("height", 1, "EFO:4", "height")],
        )
        self.fixture = self.td / "fixture.json"
        self.fixture.write_text(
            json.dumps(
                {
                    "Body mass index": {
                        "selected_ontology_id": "EFO:0004340",
                        "probabilities": {
                            "EFO:0004340": 0.7,
                            "EFO:0004338": 0.2,
                            "EFO:0004324": 0.1,
                        },
                    },
                    "height": {
                        "selected_ontology_id": "EFO:4",
                        "probabilities": {"EFO:4": 1.0},
                    },
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = choice.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_writes_proposals_to_output(self) -> None:
        output = self.td / "out" / "proposals.tsv"
        code, out, err = self.run_cli(
            [
                "--shortlists", str(self.shortlists),
                "--output", str(output),
                "--chooser", "stub",
                "--fixture", str(self.fixture),
            ]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "")
        header, rows = parse_tsv(output.read_text(encoding="utf-8"))
        self.assertEqual(header, list(PROPOSAL_COLUMNS))
        self.assertEqual({row["trait_label"] for row in rows}, {"Body mass index", "height"})
        by_label = {row["trait_label"]: row for row in rows}
        self.assertEqual(by_label["Body mass index"]["selected_ontology_id"], "EFO:0004340")
        self.assertEqual(by_label["Body mass index"]["runner_up_id"], "EFO:0004338")
        self.assertEqual(by_label["height"]["runner_up_margin"], "1.000000")
        self.assertTrue(all(row["chooser_id"] == "stub" for row in rows))
        self.assertTrue(all(row["ontology_release"] == RELEASE for row in rows))

    def test_selection_outside_shortlist_writes_no_proposal(self) -> None:
        bad_fixture = self.td / "bad.json"
        bad_fixture.write_text(
            json.dumps(
                {
                    "Body mass index": {
                        "selected_ontology_id": "EFO:9999999",
                        "probabilities": {"EFO:9999999": 1.0},
                    },
                    "height": {
                        "selected_ontology_id": "EFO:4",
                        "probabilities": {"EFO:4": 1.0},
                    },
                }
            ),
            encoding="utf-8",
        )
        output = self.td / "bad-proposals.tsv"
        code, out, err = self.run_cli(
            [
                "--shortlists", str(self.shortlists),
                "--output", str(output),
                "--fixture", str(bad_fixture),
            ]
        )
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("choice: error:", err)
        self.assertIn("not one of the", err)
        self.assertFalse(output.exists())

    def test_empty_shortlist_yields_no_proposal(self) -> None:
        empty = self.td / "empty.tsv"
        write_table(empty, SHORTLIST_COLUMNS, [])
        output = self.td / "empty-proposals.tsv"
        code, _, err = self.run_cli(
            [
                "--shortlists", str(empty),
                "--output", str(output),
                "--fixture", str(self.fixture),
            ]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(
            output.read_text(encoding="utf-8"),
            "\t".join(PROPOSAL_COLUMNS) + "\n",
        )

    def test_missing_fixture_exits_one(self) -> None:
        output = self.td / "nope.tsv"
        code, out, err = self.run_cli(
            [
                "--shortlists", str(self.shortlists),
                "--output", str(output),
                "--chooser", "stub",
                "--fixture", str(self.td / "missing.json"),
            ]
        )
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("choice: error:", err)
        self.assertFalse(output.exists())

    def test_writes_to_stdout_when_no_output(self) -> None:
        code, out, _ = self.run_cli(
            ["--shortlists", str(self.shortlists), "--fixture", str(self.fixture)]
        )
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("\t".join(PROPOSAL_COLUMNS)))

    def test_tsv_fixture_is_accepted(self) -> None:
        tsv_fixture = self.td / "fixture.tsv"
        tsv_fixture.write_text(
            "trait_label\tselected_ontology_id\tprobabilities\n"
            "Body mass index\tEFO:0004340\tEFO:0004340=0.7,EFO:0004338=0.2,EFO:0004324=0.1\n"
            "height\tEFO:4\tEFO:4=1.0\n",
            encoding="utf-8",
        )
        output = self.td / "tsv-proposals.tsv"
        code, _, err = self.run_cli(
            [
                "--shortlists", str(self.shortlists),
                "--output", str(output),
                "--fixture", str(tsv_fixture),
            ]
        )
        self.assertEqual(code, 0, err)
        _, rows = parse_tsv(output.read_text(encoding="utf-8"))
        self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()

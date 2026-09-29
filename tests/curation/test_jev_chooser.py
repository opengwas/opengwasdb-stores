#!/usr/bin/env python3
"""Tests for the Jev-backed chooser: curation.jev_chooser (issue #168).

The user-visible contract under test is that the Jev chooser speaks TypeSafe's
real ``choice`` API, can only ever propose a term from the shortlist it was
handed (or the reserved ``none_suitable`` abstention), that its hard
configuration limit (254 real candidates plus the abstention) fails loudly
*before* a client call, and that the model's calibrated probabilities reach
:class:`~curation.chooser.ChoiceResult` unmodified.

The suite is hermetic: the live HTTP client is exercised through an injected
fake transport and an injected sleep, and everything else uses the fixture
client. Nothing opens a socket and retry backoff never actually waits.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from curation.choice import (
    PROPOSAL_COLUMNS,
    build_chooser,
    build_parser,
    build_proposal,
)
from curation.chooser import (
    NONE_SUITABLE,
    Candidate,
    ChoiceError,
    InvalidProbabilityDistributionError,
)
from curation.jev_chooser import (
    DEFAULT_JEV_CONTEXT,
    DEFAULT_JEV_ENDPOINT,
    DEFAULT_JEV_MODEL,
    DEFAULT_MAX_INPUT_BYTES,
    DEFAULT_MAX_INPUT_TOKENS,
    DEFAULT_PRICE_PER_MTOK_INPUT,
    MAX_JEV_CANDIDATES,
    MAX_JEV_OPTIONS,
    FixtureJevClient,
    HttpJevClient,
    JevApiError,
    JevChooser,
    JevConfigurationError,
    JevResponse,
    JevResponseError,
    JevUnavailableError,
    build_request_payload,
    coerce_response,
    compute_request_fingerprint,
    estimate_tokens,
    load_fixture,
    parse_fixture,
    parse_fixture_tsv,
    resolve_api_key,
)

RELEASE = "efo/v3.94.0"


def make_candidate(
    ontology_id: str,
    ontology_label: str | None = None,
    definition: str = "",
    parent_id: str = "",
    parent_label: str = "",
    *,
    is_obsolete: bool = False,
) -> Candidate:
    """Build a chooser-stage candidate for a unit test."""
    return Candidate(
        ontology_id=ontology_id,
        ontology_label=ontology_label if ontology_label is not None else ontology_id,
        definition=definition,
        parent_id=parent_id,
        parent_label=parent_label,
        channels=("exact",),
        channel_ranks=(("exact", 1),),
        is_obsolete=is_obsolete,
        ontology_release=RELEASE,
    )


# Two unmodified responses recorded from the live API on 2026-09-28. They are
# embedded verbatim so the response parser is exercised against the real wire
# shape, not a hand-written approximation.
REAL_ANSWER_RESPONSE: dict[str, Any] = {
    "model": "jev-1.13.0",
    "answers": {
        "term": {
            "type": "choice",
            "choice": "MONDO:0006652",
            "confidence": 0.44,
            "probabilities": {
                "MONDO:0001090": 0.13,
                "efo:EFO_1001866": 0.0,
                "MONDO:0004781": 0.24000000000000002,
                "HP:0000830": 0.0,
                "HP:0001685": 0.0,
                "none_suitable": 0.06,
                "HP:0001700": 0.0,
                "MONDO:0006803": 0.01,
                "MONDO:0012039": 0.0,
                "MONDO:0005068": 0.05,
                "efo:EFO_0008585": 0.02,
                "efo:EFO_0008584": 0.01,
                "HP:0000520": 0.0,
                "efo:EFO_0009463": 0.0,
                "HP:0011009": 0.0,
                "MONDO:0006652": 0.48,
                "MONDO:0006649": 0.0,
                "efo:EFO_0009953": 0.0,
                "HP:0000485": 0.0,
                "MONDO:0006647": 0.0,
            },
        }
    },
    "usage": {"input_tokens": 1816, "output_tokens": 362},
}

REAL_ABSTAIN_RESPONSE: dict[str, Any] = {
    "model": "jev-1.13.0",
    "answers": {
        "term": {
            "type": "choice",
            "choice": "none_suitable",
            "confidence": 0.85,
            "probabilities": {
                "HP:0006702": 0.0,
                "MONDO:0005010": 0.0,
                "MONDO:0005355": 0.0,
                "MONDO:0005356": 0.0,
                "MONDO:0005542": 0.0,
                "MONDO:0006714": 0.0,
                "MONDO:0006715": 0.0,
                "MONDO:0006716": 0.0,
                "MONDO:0006805": 0.0,
                "MONDO:0021661": 0.0,
                "UBERON:0001621": 0.0,
                "UBERON:0005985": 0.0,
                "efo:EFO_0003776": 0.03,
                "efo:EFO_0004723": 0.0,
                "efo:EFO_0005637": 0.0,
                "efo:EFO_0005647": 0.0,
                "efo:EFO_0007938": 0.1,
                "none_suitable": 0.87,
                "efo:EFO_0010681": 0.0,
                "efo:EFO_0010722": 0.0,
                "efo:EFO_0009951": 0.0,
            },
        }
    },
    "usage": {"input_tokens": 1585, "output_tokens": 367},
}

ACUTE_MI_LABEL = (
    "diagnoses - main icd10: i21.0 acute transmural myocardial infarction of "
    "anterior wall"
)


def candidates_for_response(response: dict[str, Any]) -> list[Candidate]:
    """Real candidate ids taken from a recorded response's probability keys."""
    probabilities = response["answers"]["term"]["probabilities"]
    return [
        make_candidate(option_id)
        for option_id in probabilities
        if option_id != NONE_SUITABLE
    ]


class TestConfigurationLimits(unittest.TestCase):
    """The 254-candidate cap and the byte/token budgets fail before any call."""

    def setUp(self) -> None:
        self.client = FixtureJevClient({})
        self.chooser = JevChooser(self.client)

    def test_module_caps(self) -> None:
        self.assertEqual(MAX_JEV_OPTIONS, 255)
        self.assertEqual(MAX_JEV_CANDIDATES, 254)

    def test_over_254_candidates_is_rejected_at_configuration(self) -> None:
        candidates = [make_candidate(f"EFO:{index:07d}") for index in range(255)]
        with self.assertRaises(JevConfigurationError) as raised:
            self.chooser.configure("big label", candidates)
        message = str(raised.exception)
        self.assertIn("254", message)
        self.assertIn("big label", message)
        # The check must happen before any client call.
        self.assertEqual(self.client.calls, [])

    def test_exactly_254_candidates_is_accepted(self) -> None:
        candidates = [make_candidate(f"EFO:{index:07d}") for index in range(254)]
        request = self.chooser.configure("label", candidates)
        self.assertEqual(len(request.options), 254)
        self.assertEqual(len(request.option_ids), 254)
        # The abstention is an extra option in the criteria map.
        self.assertEqual(len(request.response_option_ids), 255)
        self.assertIn(NONE_SUITABLE, request.payload["questions"]["term"]["criteria"])

    def test_default_shortlist_size_stays_within_the_budgets(self) -> None:
        # The default shortlist size is 100 (issue #185). 100 candidates with
        # full 200-character definitions -- the shape Jev actually sees -- must
        # fit both the byte and the token budget at the module defaults.
        from curation.candidates import DEFAULT_SHORTLIST_SIZE

        self.assertEqual(DEFAULT_SHORTLIST_SIZE, 100)
        candidates = [
            make_candidate(
                f"EFO:{index:07d}",
                ontology_label=f"trait {index:02d}",
                definition="d" * 200,
            )
            for index in range(DEFAULT_SHORTLIST_SIZE)
        ]
        request = self.chooser.configure("waist circumference", candidates)
        self.assertEqual(len(request.options), DEFAULT_SHORTLIST_SIZE)
        self.assertLessEqual(request.payload_bytes, DEFAULT_MAX_INPUT_BYTES)
        self.assertLessEqual(request.estimated_input_tokens, DEFAULT_MAX_INPUT_TOKENS)
        # Every option's criteria text carries the full definition block.
        self.assertTrue(
            all(len(option.criteria_text()) >= 200 for option in request.options)
        )

    def test_max_options_cannot_exceed_candidate_cap(self) -> None:
        with self.assertRaises(JevConfigurationError):
            JevChooser(self.client, max_options=255)

    def test_byte_budget_is_enforced(self) -> None:
        chooser = JevChooser(self.client, max_input_bytes=10)
        with self.assertRaises(JevConfigurationError) as raised:
            chooser.configure("Body mass index", [make_candidate("EFO:1")])
        self.assertIn("budget", str(raised.exception))
        self.assertIn("bytes", str(raised.exception))
        self.assertEqual(self.client.calls, [])

    def test_token_budget_is_enforced(self) -> None:
        chooser = JevChooser(self.client, max_input_tokens=1)
        with self.assertRaises(JevConfigurationError) as raised:
            chooser.configure("Body mass index", [make_candidate("EFO:1")])
        self.assertIn("token budget", str(raised.exception))
        self.assertEqual(self.client.calls, [])

    def test_budgets_are_configurable_and_recorded(self) -> None:
        request = self.chooser.configure("Body mass index", [make_candidate("EFO:1")])
        self.assertGreater(request.payload_bytes, 0)
        self.assertEqual(
            request.estimated_input_tokens,
            estimate_tokens(
                json.dumps(
                    request.payload, ensure_ascii=False, separators=(",", ":")
                )
            ),
        )

    def test_invalid_budget_values_are_rejected(self) -> None:
        with self.assertRaises(JevConfigurationError):
            JevChooser(self.client, max_options=0)
        with self.assertRaises(JevConfigurationError):
            JevChooser(self.client, max_input_bytes=0)
        with self.assertRaises(JevConfigurationError):
            JevChooser(self.client, max_input_tokens=0)

    def test_duplicate_option_ids_are_rejected(self) -> None:
        with self.assertRaises(JevConfigurationError):
            self.chooser.configure(
                "label", [make_candidate("EFO:1"), make_candidate("EFO:1")]
            )

    def test_empty_ontology_id_is_rejected(self) -> None:
        with self.assertRaises(JevConfigurationError):
            self.chooser.configure("label", [make_candidate("")])

    def test_reserved_abstention_id_cannot_be_a_candidate(self) -> None:
        with self.assertRaises(JevConfigurationError):
            self.chooser.configure("label", [make_candidate(NONE_SUITABLE)])


class TestRequestPayload(unittest.TestCase):
    """The request is the real TypeSafe ``choice`` body, with none_suitable."""

    def setUp(self) -> None:
        self.chooser = JevChooser(FixtureJevClient({}))

    def test_payload_shape(self) -> None:
        candidates = [
            make_candidate("EFO:0004340", "body mass index"),
            make_candidate("EFO:0004338", "body weights and measures"),
        ]
        request = self.chooser.configure("Body mass index", candidates)
        payload = request.payload
        self.assertEqual(payload["model"], DEFAULT_JEV_MODEL)
        self.assertEqual(payload["state"]["trait_label"], "Body mass index")
        self.assertEqual(payload["state"]["context"], DEFAULT_JEV_CONTEXT)
        question = payload["questions"]["term"]
        self.assertEqual(question["type"], "choice")
        self.assertEqual(
            question["instructions"],
            "Which option is the EFO term for `trait_label`?",
        )
        criteria = question["criteria"]
        self.assertEqual(
            set(criteria), {"EFO:0004340", "EFO:0004338", NONE_SUITABLE}
        )
        self.assertEqual(criteria[NONE_SUITABLE], "None of the options denotes this trait")

    def test_default_context_is_generic(self) -> None:
        self.assertNotIn("UK Biobank", DEFAULT_JEV_CONTEXT)
        self.assertIn("do not pick a merely related term", DEFAULT_JEV_CONTEXT)
        self.assertIn("none_suitable", DEFAULT_JEV_CONTEXT)

    def test_criteria_text_format(self) -> None:
        candidate = make_candidate(
            "EFO:0000270",
            "asthma",
            definition="A chronic inflammatory disease.",
            parent_label="respiratory disease",
        )
        request = self.chooser.configure("asthma", [candidate])
        criteria = request.payload["questions"]["term"]["criteria"]
        self.assertEqual(
            criteria["EFO:0000270"],
            "asthma (parent: respiratory disease); A chronic inflammatory disease.",
        )

    def test_missing_parent_renders_empty(self) -> None:
        request = self.chooser.configure("trait", [make_candidate("EFO:1", "one")])
        criteria = request.payload["questions"]["term"]["criteria"]
        self.assertEqual(criteria["EFO:1"], "one (parent: )")

    def test_obsolete_is_flagged(self) -> None:
        candidate = make_candidate("EFO:1", "old term", is_obsolete=True)
        request = self.chooser.configure("trait", [candidate])
        criteria = request.payload["questions"]["term"]["criteria"]
        self.assertEqual(criteria["EFO:1"], "old term (parent: ) [obsolete]")

    def test_long_definition_is_truncated(self) -> None:
        definition = "word " * 200
        candidate = make_candidate("EFO:1", "one", definition=definition)
        request = self.chooser.configure("trait", [candidate])
        criteria = request.payload["questions"]["term"]["criteria"]
        self.assertLessEqual(len(criteria["EFO:1"]) - len("one (parent: )") - 2, 200)
        self.assertTrue(criteria["EFO:1"].endswith("..."))

    def test_custom_model_and_context_are_used(self) -> None:
        client = FixtureJevClient(
            {}, model="jev-test-9", context="A custom source description."
        )
        chooser = JevChooser(client)
        request = chooser.configure("trait", [make_candidate("EFO:1")])
        self.assertEqual(request.payload["model"], "jev-test-9")
        self.assertEqual(request.payload["state"]["context"], "A custom source description.")

    def test_trait_context_is_appended_after_the_base_context(self) -> None:
        candidates = [make_candidate("EFO:0004340", "body mass index")]
        trait_context = (
            "field 5141 'waist circumference'. Waist circumference was "
            "measured using a Seca 200 device."
        )
        request = self.chooser.configure(
            "waist circumference", candidates, trait_context=trait_context
        )
        self.assertEqual(
            request.payload["state"]["context"],
            DEFAULT_JEV_CONTEXT + "\n\n" + trait_context,
        )
        # The base context stays intact as a prefix; only the per-trait prose
        # is appended.
        self.assertTrue(
            request.payload["state"]["context"].startswith(DEFAULT_JEV_CONTEXT)
        )
        self.assertIn("field 5141", request.payload["state"]["context"])

    def test_empty_trait_context_leaves_the_base_context_unchanged(self) -> None:
        request = self.chooser.configure(
            "waist circumference", [make_candidate("EFO:0004340")], trait_context=""
        )
        self.assertEqual(request.payload["state"]["context"], DEFAULT_JEV_CONTEXT)

    def test_trait_context_reaches_the_payload_through_select(self) -> None:
        client = FixtureJevClient(
            {"waist circumference": {"probabilities": {"EFO:0004340": 1.0}}}
        )
        chooser = JevChooser(client)
        result = chooser.choose(
            "waist circumference",
            [make_candidate("EFO:0004340", "body mass index")],
            trait_context="field 5141 'waist circumference'",
        )
        assert result is not None
        self.assertEqual(result.selected_ontology_id, "EFO:0004340")
        request = client.calls[0]
        self.assertEqual(
            request.payload["state"]["context"],
            DEFAULT_JEV_CONTEXT + "\n\n" + "field 5141 'waist circumference'",
        )

    def test_build_request_payload_returns_structured_document(self) -> None:
        from curation.jev_chooser import JevOption

        options = (
            JevOption("EFO:1", "EFO:1", "one", "", "", "", False),
            JevOption("EFO:2", "EFO:2", "two", "", "", "", False),
        )
        payload = build_request_payload("label", options)
        self.assertEqual(payload["state"]["trait_label"], "label")
        self.assertEqual(
            set(payload["questions"]["term"]["criteria"]),
            {"EFO:1", "EFO:2", NONE_SUITABLE},
        )

    def test_request_fingerprint_is_stable_and_model_sensitive(self) -> None:
        options = [make_candidate("EFO:1", "one"), make_candidate("EFO:2", "two")]
        request_a = self.chooser.configure("label", options)
        request_b = self.chooser.configure("label", options)
        self.assertEqual(request_a.request_fingerprint, request_b.request_fingerprint)
        self.assertEqual(len(request_a.request_fingerprint), 64)

        other = JevChooser(FixtureJevClient({}, model="jev-other-1"))
        request_c = other.configure("label", options)
        self.assertNotEqual(
            request_a.request_fingerprint, request_c.request_fingerprint
        )
        self.assertEqual(
            request_a.request_fingerprint,
            compute_request_fingerprint(request_a.payload),
        )


class TestProbabilityPassThrough(unittest.TestCase):
    """Calibrated probabilities reach ChoiceResult unmodified."""

    def test_probabilities_and_argmax_selection(self) -> None:
        candidates = [make_candidate("EFO:1"), make_candidate("EFO:2")]
        client = FixtureJevClient(
            {"label": JevResponse(probabilities={"EFO:1": 0.03, "EFO:2": 0.97})}
        )
        chooser = JevChooser(client)
        result = chooser.choose("label", candidates)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.selected_ontology_id, "EFO:2")
        # The reserved abstention is always present in the distribution.
        self.assertEqual(
            result.probabilities,
            {"EFO:1": 0.03, "EFO:2": 0.97, NONE_SUITABLE: 0.0},
        )

    def test_explicit_chosen_option_is_respected(self) -> None:
        candidates = [make_candidate("EFO:1"), make_candidate("EFO:2")]
        client = FixtureJevClient(
            {
                "label": JevResponse(
                    probabilities={"EFO:1": 0.6, "EFO:2": 0.4},
                    chosen_option_id="EFO:1",
                )
            }
        )
        chooser = JevChooser(client)
        result = chooser.choose("label", candidates)
        assert result is not None
        self.assertEqual(result.selected_ontology_id, "EFO:1")

    def test_chosen_option_contradicting_distribution_is_rejected(self) -> None:
        candidates = [make_candidate("EFO:1"), make_candidate("EFO:2")]
        client = FixtureJevClient(
            {
                "label": JevResponse(
                    probabilities={"EFO:1": 0.6, "EFO:2": 0.4},
                    chosen_option_id="EFO:2",
                )
            }
        )
        chooser = JevChooser(client)
        with self.assertRaises(ChoiceError):
            chooser.choose("label", candidates)

    def test_tie_is_broken_by_shortlist_order(self) -> None:
        candidates = [make_candidate("EFO:1"), make_candidate("EFO:2")]
        client = FixtureJevClient(
            {"label": JevResponse(probabilities={"EFO:1": 0.5, "EFO:2": 0.5})}
        )
        chooser = JevChooser(client)
        result = chooser.choose("label", candidates)
        assert result is not None
        self.assertEqual(result.selected_ontology_id, "EFO:1")

    def test_response_outside_enum_is_rejected(self) -> None:
        candidates = [make_candidate("EFO:1")]
        client = FixtureJevClient(
            {"label": JevResponse(probabilities={"EFO:999": 1.0})}
        )
        chooser = JevChooser(client)
        with self.assertRaises(JevResponseError):
            chooser.choose("label", candidates)

    def test_missing_probability_coverage_is_rejected(self) -> None:
        candidates = [make_candidate("EFO:1"), make_candidate("EFO:2")]
        client = FixtureJevClient(
            {"label": JevResponse(probabilities={"EFO:1": 0.6})}
        )
        chooser = JevChooser(client)
        with self.assertRaises(InvalidProbabilityDistributionError):
            chooser.choose("label", candidates)

    def test_selection_outside_shortlist_is_rejected_by_base_class(self) -> None:
        candidates = [make_candidate("EFO:1")]
        client = FixtureJevClient(
            {
                "label": JevResponse(
                    probabilities={"EFO:2": 1.0}, chosen_option_id="EFO:2"
                )
            }
        )
        chooser = JevChooser(client)
        with self.assertRaises(JevResponseError):
            chooser.choose("label", candidates)

    def test_non_numeric_probability_is_rejected(self) -> None:
        candidates = [make_candidate("EFO:1")]
        client = FixtureJevClient(
            {"label": JevResponse(probabilities={"EFO:1": float("nan")})}
        )
        chooser = JevChooser(client)
        with self.assertRaises(JevResponseError):
            chooser.choose("label", candidates)

    def test_none_suitable_selection_is_allowed(self) -> None:
        candidates = [make_candidate("EFO:1"), make_candidate("EFO:2")]
        client = FixtureJevClient(
            {
                "label": JevResponse(
                    probabilities={
                        "EFO:1": 0.1,
                        "EFO:2": 0.1,
                        NONE_SUITABLE: 0.8,
                    },
                    chosen_option_id=NONE_SUITABLE,
                )
            }
        )
        chooser = JevChooser(client)
        result = chooser.choose("label", candidates)
        assert result is not None
        self.assertEqual(result.selected_ontology_id, NONE_SUITABLE)

    def test_high_precision_probabilities_reach_proposal_unmodified(self) -> None:
        # The acceptance criterion is that Jev's native calibrated distribution
        # is carried into the proposal table without rounding or truncation.
        candidates = [make_candidate("EFO:1"), make_candidate("EFO:2")]
        high_precision = 0.123456789123
        client = FixtureJevClient(
            {
                "label": JevResponse(
                    probabilities={
                        "EFO:1": high_precision,
                        "EFO:2": 1.0 - high_precision,
                    }
                )
            }
        )
        chooser = JevChooser(client)
        result = chooser.choose("label", candidates)
        assert result is not None
        self.assertEqual(result.probabilities["EFO:1"], high_precision)

        proposal = build_proposal("label", candidates, result)
        row = dict(zip(PROPOSAL_COLUMNS, proposal.to_row()))
        serialized = row["probabilities"]
        self.assertIn("0.123456789123", serialized)
        self.assertEqual(json.loads(serialized)["EFO:1"], high_precision)


class TestRealResponses(unittest.TestCase):
    """The parser and chooser are exercised against recorded live responses."""

    def test_coerce_real_answer_response(self) -> None:
        response = coerce_response(REAL_ANSWER_RESPONSE)
        self.assertEqual(response.model_version, "jev-1.13.0")
        self.assertEqual(response.chosen_option_id, "MONDO:0006652")
        self.assertEqual(response.confidence, 0.44)
        self.assertEqual(response.input_tokens, 1816)
        self.assertEqual(response.output_tokens, 362)
        self.assertEqual(response.probabilities["MONDO:0006652"], 0.48)
        self.assertIn(NONE_SUITABLE, response.probabilities)

    def test_coerce_real_abstention_response(self) -> None:
        response = coerce_response(REAL_ABSTAIN_RESPONSE)
        self.assertEqual(response.chosen_option_id, NONE_SUITABLE)
        self.assertEqual(response.confidence, 0.85)
        self.assertEqual(response.input_tokens, 1585)

    def test_real_abstention_flows_to_proposal_row(self) -> None:
        candidates = candidates_for_response(REAL_ABSTAIN_RESPONSE)
        client = FixtureJevClient({ACUTE_MI_LABEL: REAL_ABSTAIN_RESPONSE})
        chooser = JevChooser(client)
        result = chooser.choose(ACUTE_MI_LABEL, candidates)
        assert result is not None
        self.assertEqual(result.selected_ontology_id, NONE_SUITABLE)
        self.assertEqual(result.model_version, "jev-1.13.0")
        self.assertEqual(result.input_tokens, 1585)

        proposal = build_proposal(ACUTE_MI_LABEL, candidates, result)
        row = dict(zip(PROPOSAL_COLUMNS, proposal.to_row()))
        self.assertEqual(row["selected_ontology_id"], NONE_SUITABLE)
        self.assertEqual(row["selected_ontology_label"], "")
        self.assertEqual(row["confidence"], "0.870000")
        self.assertEqual(result.model_version, "jev-1.13.0")
        self.assertEqual(result.input_tokens, 1585)
        self.assertNotEqual(result.request_fingerprint, "")

    def test_real_answer_response_provenance(self) -> None:
        candidates = candidates_for_response(REAL_ANSWER_RESPONSE)
        client = FixtureJevClient({ACUTE_MI_LABEL: REAL_ANSWER_RESPONSE})
        chooser = JevChooser(client)
        result = chooser.choose(ACUTE_MI_LABEL, candidates)
        assert result is not None
        self.assertEqual(result.selected_ontology_id, "MONDO:0006652")
        self.assertEqual(result.model_version, "jev-1.13.0")
        self.assertEqual(result.model_confidence, 0.44)
        self.assertEqual(result.input_tokens, 1816)
        self.assertEqual(result.raw_response["model"], "jev-1.13.0")


class TestCostTracking(unittest.TestCase):
    """Per-label spend is recorded for live-run accounting."""

    def test_cost_records_and_total(self) -> None:
        candidates = [make_candidate("EFO:1")]
        client = FixtureJevClient(
            {
                "a": JevResponse(probabilities={"EFO:1": 1.0}, cost_usd=0.01),
                "b": JevResponse(probabilities={"EFO:1": 1.0}, cost_usd=0.02),
            }
        )
        chooser = JevChooser(client)
        chooser.choose("a", candidates)
        chooser.choose("b", candidates)
        self.assertEqual(len(chooser.cost_records), 2)
        self.assertAlmostEqual(chooser.total_cost_usd, 0.03)
        self.assertEqual(
            [record.trait_label for record in chooser.cost_records], ["a", "b"]
        )

    def test_no_cost_is_recorded_when_absent(self) -> None:
        candidates = [make_candidate("EFO:1")]
        client = FixtureJevClient(
            {"a": JevResponse(probabilities={"EFO:1": 1.0})}
        )
        chooser = JevChooser(client)
        chooser.choose("a", candidates)
        self.assertEqual(chooser.cost_records, [])
        self.assertEqual(chooser.total_cost_usd, 0.0)

    def test_paid_but_rejected_response_still_records_usage_and_cost(self) -> None:
        candidates = [make_candidate("EFO:1")]
        client = FixtureJevClient(
            {
                "label": JevResponse(
                    # An invented id makes the response invalid after receipt.
                    probabilities={"EFO:999": 1.0},
                    input_tokens=1234,
                    cost_usd=0.02,
                    raw={"usage": {"input_tokens": 1234}},
                )
            }
        )
        chooser = JevChooser(client)
        with self.assertRaises(JevResponseError) as raised:
            chooser.choose("label", candidates)
        self.assertEqual(raised.exception.input_tokens, 1234)
        self.assertAlmostEqual(raised.exception.cost_usd, 0.02)
        # The spend is still accounted for even though the answer was refused.
        self.assertEqual(len(chooser.cost_records), 1)
        self.assertAlmostEqual(chooser.total_cost_usd, 0.02)

    def test_cost_is_derived_from_input_tokens(self) -> None:
        client = HttpJevClient(
            api_key="k",
            client_factory=lambda: _FakeJsonClient(REAL_ANSWER_RESPONSE),
        )
        chooser = JevChooser(client)
        result = chooser.choose(
            ACUTE_MI_LABEL, candidates_for_response(REAL_ANSWER_RESPONSE)
        )
        assert result is not None
        expected = 1816 / 1e6 * DEFAULT_PRICE_PER_MTOK_INPUT
        self.assertAlmostEqual(result.cost_usd or 0.0, expected)
        self.assertAlmostEqual(chooser.total_cost_usd, expected)


class TestFixtureClient(unittest.TestCase):
    """The fixture client replays recorded decisions and fails loudly."""

    def test_mapping_fixture_is_replayed(self) -> None:
        client = FixtureJevClient(
            {"label": {"probabilities": {"EFO:1": 1.0}}}
        )
        chooser = JevChooser(client)
        result = chooser.choose("label", [make_candidate("EFO:1")])
        assert result is not None
        self.assertEqual(result.selected_ontology_id, "EFO:1")

    def test_unrecorded_label_raises(self) -> None:
        client = FixtureJevClient({"other": {"probabilities": {"EFO:1": 1.0}}})
        chooser = JevChooser(client)
        with self.assertRaises(JevResponseError):
            chooser.choose("label", [make_candidate("EFO:1")])

    def test_compact_probabilities_parse(self) -> None:
        client = FixtureJevClient(
            {"label": {"probabilities": "EFO:1=0.8,EFO:2=0.2"}}
        )
        chooser = JevChooser(client)
        result = chooser.choose(
            "label", [make_candidate("EFO:1"), make_candidate("EFO:2")]
        )
        assert result is not None
        self.assertEqual(
            result.probabilities,
            {"EFO:1": 0.8, "EFO:2": 0.2, NONE_SUITABLE: 0.0},
        )

    def test_json_and_tsv_fixtures_load(self) -> None:
        self.assertEqual(
            parse_fixture({"label": {"probabilities": {"EFO:1": 1.0}}})[
                "label"
            ].probabilities,
            {"EFO:1": 1.0},
        )
        parsed = parse_fixture_tsv(
            "trait_label\tprobabilities\tchosen_option_id\n"
            "label\tEFO:1=1.0\tEFO:1\n"
        )
        self.assertEqual(parsed["label"].chosen_option_id, "EFO:1")

    def test_load_fixture_from_disk(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fixture.json"
            path.write_text(
                json.dumps({"label": {"probabilities": {"EFO:1": 1.0}}}),
                encoding="utf-8",
            )
            loaded = load_fixture(path)
            self.assertEqual(loaded["label"].probabilities, {"EFO:1": 1.0})

    def test_fixture_response_coercion(self) -> None:
        response = coerce_response(
            {"probabilities": {"EFO:1": 0.9}, "chosen_option_id": "EFO:1", "cost_usd": 0.5}
        )
        self.assertEqual(response.chosen_option_id, "EFO:1")
        self.assertEqual(response.cost_usd, 0.5)

    def test_fixture_client_carries_model_and_context(self) -> None:
        client = FixtureJevClient({}, model="jev-x", context="ctx")
        self.assertEqual(client.model, "jev-x")
        self.assertEqual(client.context, "ctx")


class _FakeResponse:
    def __init__(
        self,
        data: dict[str, Any],
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._data = data
        self.status_code = status_code
        self.headers = headers or {}
        self.raised = False

    def raise_for_status(self) -> None:
        self.raised = True
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP status {self.status_code}")

    def json(self) -> dict[str, Any]:
        return self._data


class _FakeHttpClient:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.posts: list[tuple[str, dict[str, Any], dict[str, str]]] = []
        self.closed = False

    def post(self, url: str, json: dict[str, Any], headers: dict[str, str]) -> Any:
        self.posts.append((url, json, headers))
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


def _FakeJsonClient(data: dict[str, Any]) -> _FakeHttpClient:
    return _FakeHttpClient([_FakeResponse(data)])


def _build_request(
    label: str = "label", candidates: list[Candidate] | None = None
):
    chooser = JevChooser(FixtureJevClient({}))
    return chooser.configure(label, candidates or [make_candidate("EFO:1")])


class TestHttpClient(unittest.TestCase):
    """The hosted client is exercised through an injected fake transport."""

    def test_posts_real_request_and_parses_response(self) -> None:
        fake = _FakeJsonClient(REAL_ANSWER_RESPONSE)
        client = HttpJevClient(
            "https://api.typesafe.ai/v1/systemone",
            model="jev-1.13.0",
            api_key="secret",
            client_factory=lambda: fake,
        )
        request = client_request_for(
            ACUTE_MI_LABEL, candidates_for_response(REAL_ANSWER_RESPONSE), client
        )
        response = client.decide(request)
        self.assertEqual(response.chosen_option_id, "MONDO:0006652")
        self.assertEqual(response.model_version, "jev-1.13.0")
        self.assertEqual(response.input_tokens, 1816)
        self.assertEqual(len(fake.posts), 1)
        url, body, headers = fake.posts[0]
        self.assertEqual(url, "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(body["model"], "jev-1.13.0")
        self.assertIn("questions", body)
        self.assertEqual(headers["authorization"], "Bearer secret")
        self.assertTrue(fake.closed)

    def test_retry_on_429_honours_retry_after(self) -> None:
        sleeps: list[float] = []
        fake = _FakeHttpClient(
            [
                _FakeResponse({}, status_code=429, headers={"retry-after": "2"}),
                _FakeResponse(REAL_ANSWER_RESPONSE),
            ]
        )
        client = HttpJevClient(
            api_key="k",
            max_attempts=3,
            base_delay=100.0,
            sleep=sleeps.append,
            client_factory=lambda: fake,
        )
        response = client.decide(_build_request())
        self.assertEqual(response.model_version, "jev-1.13.0")
        self.assertEqual(len(fake.posts), 2)
        self.assertEqual(sleeps, [2.0])

    def test_retry_on_529_uses_exponential_backoff(self) -> None:
        sleeps: list[float] = []
        fake = _FakeHttpClient(
            [
                _FakeResponse({}, status_code=529),
                _FakeResponse({}, status_code=500),
                _FakeResponse({}, status_code=200),
            ]
        )
        client = HttpJevClient(
            api_key="k",
            max_attempts=4,
            base_delay=1.0,
            sleep=sleeps.append,
            client_factory=lambda: fake,
        )
        client.decide(_build_request())
        self.assertEqual(len(fake.posts), 3)
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_no_retry_on_401(self) -> None:
        sleeps: list[float] = []
        fake = _FakeHttpClient([_FakeResponse({}, status_code=401)])
        client = HttpJevClient(
            api_key="k",
            max_attempts=6,
            sleep=sleeps.append,
            client_factory=lambda: fake,
        )
        with self.assertRaises(JevApiError) as raised:
            client.decide(_build_request())
        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(len(fake.posts), 1)
        self.assertEqual(sleeps, [])

    def test_no_retry_on_422(self) -> None:
        sleeps: list[float] = []
        fake = _FakeHttpClient([_FakeResponse({}, status_code=422)])
        client = HttpJevClient(
            api_key="k",
            max_attempts=6,
            sleep=sleeps.append,
            client_factory=lambda: fake,
        )
        with self.assertRaises(JevApiError) as raised:
            client.decide(_build_request())
        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(len(fake.posts), 1)
        self.assertEqual(sleeps, [])

    def test_non_retryable_error_includes_a_redacted_body(self) -> None:
        response = _FakeResponse({}, status_code=422)
        response.text = '{"error": "invalid body for key secret-key"}'
        fake = _FakeHttpClient([response])
        client = HttpJevClient(
            api_key="secret-key",
            client_factory=lambda: fake,
        )
        with self.assertRaises(JevApiError) as raised:
            client.decide(_build_request())
        message = str(raised.exception)
        # The (key-free) body is diagnosable from the error, and the key is not.
        self.assertIn("invalid body", message)
        self.assertIn("***", message)
        self.assertNotIn("secret-key", message)

    def test_retry_exhaustion_raises_unavailable(self) -> None:
        sleeps: list[float] = []
        fake = _FakeHttpClient([_FakeResponse({}, status_code=529)] * 3)
        client = HttpJevClient(
            api_key="k",
            max_attempts=3,
            base_delay=1.0,
            sleep=sleeps.append,
            client_factory=lambda: fake,
        )
        with self.assertRaises(JevUnavailableError):
            client.decide(_build_request())
        self.assertEqual(len(fake.posts), 3)
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_transport_error_retries_then_exhausts(self) -> None:
        sleeps: list[float] = []
        fake = _FakeHttpClient([ConnectionError("boom")] * 2)
        client = HttpJevClient(
            api_key="k",
            max_attempts=2,
            base_delay=1.0,
            sleep=sleeps.append,
            client_factory=lambda: fake,
        )
        with self.assertRaises(JevUnavailableError):
            client.decide(_build_request())
        self.assertEqual(len(fake.posts), 2)
        self.assertEqual(sleeps, [1.0])

    def test_transport_error_is_not_retried_when_attempts_is_one(self) -> None:
        sleeps: list[float] = []
        fake = _FakeHttpClient([ConnectionError("no route to host")])
        client = HttpJevClient(
            api_key="k",
            max_attempts=1,
            sleep=sleeps.append,
            client_factory=lambda: fake,
        )
        with self.assertRaises(JevUnavailableError):
            client.decide(_build_request())
        self.assertEqual(len(fake.posts), 1)
        self.assertEqual(sleeps, [])

    def test_invalid_client_configuration_is_rejected(self) -> None:
        with self.assertRaises(JevConfigurationError):
            HttpJevClient(api_key="k", max_attempts=0)
        with self.assertRaises(JevConfigurationError):
            HttpJevClient(api_key="k", base_delay=-1)
        with self.assertRaises(JevConfigurationError):
            HttpJevClient(api_key="k", timeout=0)
        with self.assertRaises(JevConfigurationError):
            HttpJevClient(api_key="k", price_per_mtok_input=-1)

    def test_api_key_never_appears_in_errors(self) -> None:
        secret = "super-secret-key-value"
        fake = _FakeHttpClient([_FakeResponse({}, status_code=401)])
        client = HttpJevClient(
            api_key=secret,
            sleep=lambda _s: None,
            client_factory=lambda: fake,
        )
        with self.assertRaises(JevApiError) as raised:
            client.decide(_build_request())
        self.assertNotIn(secret, str(raised.exception))

        failing = _FakeHttpClient([ConnectionError("nope")])
        client = HttpJevClient(
            api_key=secret,
            max_attempts=1,
            sleep=lambda _s: None,
            client_factory=lambda: failing,
        )
        with self.assertRaises(JevUnavailableError) as raised:
            client.decide(_build_request())
        self.assertNotIn(secret, str(raised.exception))


def client_request_for(
    label: str,
    candidates: list[Candidate],
    client: HttpJevClient,
):
    """Build a request whose payload uses ``client``'s model and context."""
    return JevChooser(client).configure(label, candidates)


class TestKeyResolution(unittest.TestCase):
    """The key resolves explicit > TYPESAFE_API_KEY > ~/.typesafe."""

    def test_explicit_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            (home / ".typesafe").write_text('key="file-key"', encoding="utf-8")
            self.assertEqual(
                resolve_api_key("explicit", env={"TYPESAFE_API_KEY": "env-key"}, home=home),
                "explicit",
            )

    def test_env_wins_over_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            (home / ".typesafe").write_text('key="file-key"', encoding="utf-8")
            self.assertEqual(
                resolve_api_key(None, env={"TYPESAFE_API_KEY": "env-key"}, home=home),
                "env-key",
            )

    def test_file_is_used_last(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            (home / ".typesafe").write_text('key="file-key"\n', encoding="utf-8")
            self.assertEqual(resolve_api_key(None, env={}, home=home), "file-key")

    def test_missing_key_raises_without_leaking(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(JevConfigurationError) as raised:
                resolve_api_key(None, env={}, home=Path(temp))
            message = str(raised.exception)
            self.assertIn("TYPESAFE_API_KEY", message)
            self.assertIn(".typesafe", message)


class TestChooserFactory(unittest.TestCase):
    """`choice.build_chooser` registers the Jev chooser."""

    def test_jev_defaults_to_the_typesafe_endpoint(self) -> None:
        chooser = build_chooser("jev", None, jev_api_key="test-key")
        self.assertIsInstance(chooser, JevChooser)
        client = chooser.client
        self.assertIsInstance(client, HttpJevClient)
        self.assertEqual(client.endpoint, DEFAULT_JEV_ENDPOINT)
        self.assertEqual(client.model, DEFAULT_JEV_MODEL)

    def test_jev_fixture_builds_chooser(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "jev.json"
            path.write_text(
                json.dumps({"label": {"probabilities": {"EFO:1": 1.0}}}),
                encoding="utf-8",
            )
            chooser = build_chooser("jev", None, jev_fixture=path)
        self.assertIsInstance(chooser, JevChooser)
        result = chooser.choose("label", [make_candidate("EFO:1")])
        self.assertIsNotNone(result)


class TestChoiceCliFlags(unittest.TestCase):
    """The choice CLI exposes the TypeSafe ``--jev-*`` surface."""

    def test_defaults(self) -> None:
        args = build_parser().parse_args(["--shortlists", "x.tsv"])
        self.assertEqual(args.jev_model, DEFAULT_JEV_MODEL)
        self.assertEqual(args.jev_max_options, MAX_JEV_CANDIDATES)
        self.assertEqual(args.jev_context, DEFAULT_JEV_CONTEXT)
        self.assertIsNone(args.jev_api_key)

    def test_explicit_flags_parse(self) -> None:
        args = build_parser().parse_args(
            [
                "--shortlists", "x.tsv",
                "--chooser", "jev",
                "--jev-model", "jev-test-2",
                "--jev-context", "a source description",
                "--jev-max-attempts", "3",
                "--jev-base-delay", "0.5",
                "--jev-timeout", "10",
                "--jev-price-per-mtok-input", "0.1",
            ]
        )
        self.assertEqual(args.jev_model, "jev-test-2")
        self.assertEqual(args.jev_context, "a source description")
        self.assertEqual(args.jev_max_attempts, 3)
        self.assertEqual(args.jev_base_delay, 0.5)
        self.assertEqual(args.jev_timeout, 10)
        self.assertEqual(args.jev_price_per_mtok_input, 0.1)


if __name__ == "__main__":
    unittest.main()

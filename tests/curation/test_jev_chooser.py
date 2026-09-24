#!/usr/bin/env python3
"""Tests for the Jev-backed chooser: curation.jev_chooser (issue #168).

The user-visible contract under test is that the Jev chooser can only ever
propose a term from the shortlist it was handed, that its hard configuration
limits (255 enum options, input byte/token budgets) fail loudly *before* a
client call, and that the model's calibrated probabilities reach
:class:`~curation.chooser.ChoiceResult` unmodified.

The suite is hermetic: the live HTTP client is exercised through an injected
fake transport, and everything else uses the fixture client. Nothing opens a
socket.
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

from curation.choice import build_chooser
from curation.chooser import (
    Candidate,
    ChoiceError,
    InvalidProbabilityDistributionError,
)
from curation.jev_chooser import (
    MAX_JEV_OPTIONS,
    FixtureJevClient,
    HttpJevClient,
    JevChooser,
    JevConfigurationError,
    JevResponseError,
    JevResponse,
    build_request_payload,
    coerce_response,
    estimate_tokens,
    load_fixture,
    parse_fixture,
    parse_fixture_tsv,
)

RELEASE = "efo/v3.78.0"


def make_candidate(
    ontology_id: str,
    ontology_label: str | None = None,
    definition: str = "",
    parent_id: str = "",
    parent_label: str = "",
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
        is_obsolete=False,
        ontology_release=RELEASE,
    )


class TestConfigurationLimits(unittest.TestCase):
    """The 255-option cap and the byte/token budgets fail before any call."""

    def setUp(self) -> None:
        self.client = FixtureJevClient({})
        self.chooser = JevChooser(self.client)

    def test_module_cap_is_255(self) -> None:
        self.assertEqual(MAX_JEV_OPTIONS, 255)

    def test_over_255_options_is_rejected_at_configuration(self) -> None:
        candidates = [make_candidate(f"EFO:{index:07d}") for index in range(256)]
        with self.assertRaises(JevConfigurationError) as raised:
            self.chooser.configure("big label", candidates)
        message = str(raised.exception)
        self.assertIn("255", message)
        self.assertIn("big label", message)
        # The check must happen before any client call.
        self.assertEqual(self.client.calls, [])

    def test_exactly_255_options_is_accepted(self) -> None:
        candidates = [make_candidate(f"EFO:{index:07d}") for index in range(255)]
        request = self.chooser.configure("label", candidates)
        self.assertEqual(len(request.options), 255)
        self.assertEqual(len(request.option_ids), 255)

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
            estimate_tokens(json.dumps(request.payload, ensure_ascii=False, separators=(",", ":"))),
        )

    def test_invalid_budget_values_are_rejected(self) -> None:
        with self.assertRaises(JevConfigurationError):
            JevChooser(self.client, max_options=0)
        with self.assertRaises(JevConfigurationError):
            JevChooser(self.client, max_input_bytes=0)
        with self.assertRaises(JevConfigurationError):
            JevChooser(self.client, max_input_tokens=0)


class TestWirePayload(unittest.TestCase):
    """The request serializes candidate identifiers into the response enum."""

    def test_enum_exposes_exactly_the_shortlist(self) -> None:
        candidates = [
            make_candidate("EFO:0004340", "body mass index"),
            make_candidate("EFO:0004338", "body weights and measures"),
        ]
        chooser = JevChooser(FixtureJevClient({}))
        request = chooser.configure("Body mass index", candidates)
        schema = request.payload["response_schema"]
        enum = schema["properties"]["chosen_option_id"]["enum"]
        self.assertEqual(enum, ["EFO:0004340", "EFO:0004338"])
        self.assertEqual(
            sorted(schema["properties"]["probabilities"]["properties"]),
            ["EFO:0004338", "EFO:0004340"],
        )
        self.assertEqual(
            schema["properties"]["probabilities"]["required"],
            ["EFO:0004340", "EFO:0004338"],
        )

    def test_duplicate_option_ids_are_rejected(self) -> None:
        chooser = JevChooser(FixtureJevClient({}))
        with self.assertRaises(JevConfigurationError):
            chooser.configure(
                "label", [make_candidate("EFO:1"), make_candidate("EFO:1")]
            )

    def test_empty_ontology_id_is_rejected(self) -> None:
        chooser = JevChooser(FixtureJevClient({}))
        with self.assertRaises(JevConfigurationError):
            chooser.configure("label", [make_candidate("")])

    def test_build_request_payload_returns_structured_document(self) -> None:
        from curation.jev_chooser import JevOption

        options = (
            JevOption("EFO:1", "EFO:1", "one", "", "", "", False),
            JevOption("EFO:2", "EFO:2", "two", "", "", "", False),
        )
        payload = build_request_payload("label", options, ["EFO:1", "EFO:2"])
        self.assertEqual(payload["task"], "trait_ontology_mapping_choice")
        self.assertEqual(payload["trait_label"], "label")
        self.assertEqual(len(payload["options"]), 2)


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
        self.assertEqual(result.probabilities, {"EFO:1": 0.03, "EFO:2": 0.97})

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
        self.assertEqual(result.probabilities, {"EFO:1": 0.6, "EFO:2": 0.4})

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
        self.assertEqual(result.probabilities, {"EFO:1": 0.8, "EFO:2": 0.2})

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


class _FakeHttpResponse:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data
        self.raised = False

    def raise_for_status(self) -> None:
        self.raised = True

    def json(self) -> dict[str, Any]:
        return self._data


class _FakeHttpClient:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data
        self.posts: list[tuple[str, dict[str, Any], dict[str, str]]] = []
        self.closed = False

    def post(self, url: str, json: dict[str, Any], headers: dict[str, str]) -> _FakeHttpResponse:
        self.posts.append((url, json, headers))
        return _FakeHttpResponse(self._data)

    def close(self) -> None:
        self.closed = True


class TestHttpClient(unittest.TestCase):
    """The hosted client is exercised through an injected fake transport."""

    def test_posts_structured_request_and_parses_response(self) -> None:
        fake = _FakeHttpClient(
            {"probabilities": {"EFO:1": 0.9, "EFO:2": 0.1}, "cost_usd": 0.02}
        )
        client = HttpJevClient(
            "https://jev.example/decide",
            model="jev-typesafe-v1",
            api_key="secret",
            client_factory=lambda: fake,
        )
        chooser = JevChooser(client)
        result = chooser.choose(
            "label", [make_candidate("EFO:1"), make_candidate("EFO:2")]
        )
        assert result is not None
        self.assertEqual(result.selected_ontology_id, "EFO:1")
        self.assertEqual(len(fake.posts), 1)
        url, body, headers = fake.posts[0]
        self.assertEqual(url, "https://jev.example/decide")
        self.assertEqual(body["model"], "jev-typesafe-v1")
        self.assertEqual(headers["authorization"], "Bearer secret")
        self.assertIn("response_schema", body)
        self.assertTrue(fake.closed)
        self.assertAlmostEqual(chooser.total_cost_usd, 0.02)

    def test_http_failure_becomes_jev_unavailable(self) -> None:
        class _Boom:
            def post(self, *args: Any, **kwargs: Any) -> Any:
                raise ConnectionError("no route to host")

            def close(self) -> None:
                pass

        client = HttpJevClient("https://jev.example", client_factory=_Boom)
        with self.assertRaises(Exception):
            client.decide(
                JevChooser(FixtureJevClient({})).configure(
                    "label", [make_candidate("EFO:1")]
                )
            )


class TestChooserFactory(unittest.TestCase):
    """`choice.build_chooser` registers the Jev chooser."""

    def test_jev_requires_endpoint_or_fixture(self) -> None:
        with self.assertRaises(Exception):
            build_chooser("jev", None)

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


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""A ticket's ```architecture block, read from fixture bodies (#422, epic #416).

`plans.parse` turns a ticket body into the plan the board file carries: the block
parsed, or `{"invalid": reason}` when it cannot be used, or nothing when there is no
block. Expected values are literals written from the bodies below.

Run from the repository root:
    python3 tests/project-site-architecture/test_plan_parsing.py
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from project_site.collect import board, plans

FIXTURE = Path(__file__).resolve().parents[1] / "project-site-data" / "fixtures" / "board-graphql.json"
FENCE = "```"

VALID = f"""Some prose first.

{FENCE}architecture
adds:
  - paths: [opengwasdb/encoding/association.py]
    component: encoding
drops:
  - dense -> encoding
absorbs:
  - encoding/codec.py:932
{FENCE}

Prose after, with `architecture: not a key` and unknown: words.
"""
KNOWN = {"encoding", "dense", "resolution"}


def block(text: str) -> str:
    return f"{FENCE}architecture\n{text}\n{FENCE}\n"


class ParseTests(unittest.TestCase):
    def test_a_valid_block_becomes_the_plan(self) -> None:
        self.assertEqual(
            plans.parse(VALID, KNOWN),
            {
                "adds": [{"paths": ["opengwasdb/encoding/association.py"], "component": "encoding"}],
                "moves": [],
                "drops": [{"from": "dense", "to": "encoding"}],
                "absorbs": ["encoding/codec.py:932"],
            },
        )

    def test_a_move_and_a_new_component_with_its_name(self) -> None:
        text = block(
            "adds:\n  - paths: [a/b.py]\n    component: fresh\n    name: Fresh one\n"
            "moves:\n  - paths: [opengwasdb/build/eaf.py]\n    to: resolution\n"
        )
        self.assertEqual(
            plans.parse(text, KNOWN),
            {
                "adds": [{"paths": ["a/b.py"], "component": "fresh", "name": "Fresh one"}],
                "moves": [{"paths": ["opengwasdb/build/eaf.py"], "to": "resolution"}],
                "drops": [],
                "absorbs": [],
            },
        )

    def test_no_block_means_no_plan(self) -> None:
        self.assertIsNone(plans.parse("Just prose.\n\n```python\nadds: 1\n```\n", KNOWN))
        self.assertIsNone(plans.parse(None, KNOWN))
        self.assertIsNone(plans.parse("", KNOWN))

    def test_text_outside_the_fence_is_ignored(self) -> None:
        plan = plans.parse("adds: [nonsense\n\n" + block("drops:\n  - dense -> encoding") + "\nunknown: key\n", KNOWN)
        self.assertEqual(plan, {"adds": [], "moves": [], "drops": [{"from": "dense", "to": "encoding"}], "absorbs": []})

    def test_invalid_yaml_is_kept_with_a_reason(self) -> None:
        plan = plans.parse(block("adds: [unclosed"), KNOWN)
        self.assertEqual(list(plan), ["invalid"])
        self.assertTrue(plan["invalid"].startswith("the block is not valid YAML"), plan)

    def test_an_unknown_key_is_invalid(self) -> None:
        self.assertEqual(plans.parse(block("drops: []\nremoves:\n  - x"), KNOWN), {"invalid": "unknown key `removes`"})

    def test_an_empty_block_is_invalid(self) -> None:
        self.assertEqual(plans.parse(block(""), KNOWN), {"invalid": "the block is empty"})

    def test_an_unknown_component_is_invalid(self) -> None:
        self.assertEqual(
            plans.parse(block("moves:\n  - paths: [a.py]\n    to: nowhere"), KNOWN),
            {"invalid": "component `nowhere` is neither an existing component nor declared in `adds`"},
        )
        self.assertEqual(
            plans.parse(block("drops:\n  - dense -> ghost"), KNOWN),
            {"invalid": "component `ghost` is neither an existing component nor declared in `adds`"},
        )

    def test_a_new_component_needs_a_name(self) -> None:
        self.assertEqual(
            plans.parse(block("adds:\n  - paths: [a.py]\n    component: fresh"), KNOWN),
            {"invalid": "component `fresh` is new, so its add needs a `name`"},
        )

    def test_a_malformed_item_is_invalid(self) -> None:
        self.assertEqual(plans.parse(block("drops:\n  - dense to encoding"), KNOWN),
                         {"invalid": "drop `dense to encoding` is not `from -> to`"})
        self.assertEqual(plans.parse(block("adds:\n  - component: encoding"), KNOWN),
                         {"invalid": "an add needs a non-empty `paths` list"})

    def test_without_the_known_components_their_existence_is_not_judged(self) -> None:
        self.assertEqual(
            plans.parse(block("drops:\n  - dense -> ghost"), None),
            {"adds": [], "moves": [], "drops": [{"from": "dense", "to": "ghost"}], "absorbs": []},
        )

    def test_two_blocks_are_invalid_not_silently_one(self) -> None:
        self.assertEqual(plans.parse(block("drops: []") + "\n" + block("drops: []"), KNOWN),
                         {"invalid": "more than one `architecture` block"})


class BoardCarriesThePlanTests(unittest.TestCase):
    def test_the_plan_is_on_the_child_and_no_body_is_kept(self) -> None:
        raw = copy.deepcopy(json.loads(FIXTURE.read_text()))
        nodes = {n["number"]: n for n in raw["children"]["e0"]["issue"]["subIssues"]["nodes"]}
        number = 53
        nodes[number]["body"] = "Secret prose.\n\n" + block("drops:\n  - dense -> encoding")
        children = {e["key"]: e for e in board.parse(raw)["epics"]}["opengwasdb#58"]["children"]
        by_number = {c["key"].split("#")[1]: c for c in children}
        self.assertEqual(by_number[str(number)]["plan"],
                         {"adds": [], "moves": [], "drops": [{"from": "dense", "to": "encoding"}], "absorbs": []})
        self.assertEqual([c["key"] for c in children if "plan" in c], [by_number[str(number)]["key"]])
        self.assertNotIn("Secret prose", json.dumps(board.parse(raw)))


if __name__ == "__main__":
    unittest.main()

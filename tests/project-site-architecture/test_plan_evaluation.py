#!/usr/bin/env python3
"""The `work` collector's `plans`: a ticket's planned change, judged once it closes (#422).

Fixture board and architecture documents (`work_fixtures.py`): `enc` claims
`opengwasdb/encoding/`, `query` `opengwasdb/query/`, `store` `opengwasdb/store/`
(engine), `pages` claims `src/project_site/` (registry). Expected values are
literals written from them.

Run from the repository root:
    python3 tests/project-site-architecture/test_plan_evaluation.py
"""

from __future__ import annotations

import unittest
from pathlib import Path

import work_fixtures as fx

ARCH = {
    **fx.ARCHITECTURE,
    "edges": [
        {"from": "query", "to": "enc", "imports": 3, "scope": "engine", "upward": False},
        {"from": "pages", "to": "enc", "imports": 2, "scope": "registry_to_engine", "upward": False},
    ],
    "gaps": {"registry": {"unclaimed": [], "stale": []}, "engine": {"unclaimed": [], "stale": []}},
}
NO_MODEL = {**fx.NO_ENGINE_MODEL, "edges": [], "gaps": {"registry": {"unclaimed": [], "stale": []},
                                                          "engine": {"unclaimed": [], "stale": []}}}


def planned(plan: dict, *, state: str = "CLOSED", key: str = "opengwasdb#350") -> dict:
    t = fx.ticket(key, state=state)
    t["plan"] = plan
    return fx.board(fx.epic("opengwasdb#58", "Active", [t, fx.ticket("opengwasdb#51")]))


def plan(**kw) -> dict:
    return {"adds": [], "moves": [], "drops": [], "absorbs": [], **kw}


def only(result: dict) -> dict:
    (entry,) = result["plans"]
    return entry


class ClosedTicketTests(unittest.TestCase):
    def test_added_paths_are_held_only_when_the_named_component_claims_them(self) -> None:
        entry = only(fx.compute(planned(plan(adds=[{"paths": [
            "opengwasdb/encoding/association.py", "opengwasdb/query/association.py", "opengwasdb/nowhere/x.py",
        ], "component": "enc"}])), architecture=ARCH))
        self.assertEqual(entry["checks"], [
            {"kind": "adds", "path": "opengwasdb/encoding/association.py", "component": "enc", "held": True,
             "why": "claimed by `enc`"},
            {"kind": "adds", "path": "opengwasdb/query/association.py", "component": "enc", "held": False,
             "why": "claimed by `query`, not `enc`"},
            {"kind": "adds", "path": "opengwasdb/nowhere/x.py", "component": "enc", "held": False,
             "why": "no component claims it"},
        ])

    def test_a_moved_path_is_held_when_the_target_claims_it(self) -> None:
        entry = only(fx.compute(planned(plan(moves=[{"paths": ["opengwasdb/store/paths.py", "opengwasdb/query/facade.py"],
                                                     "to": "store"}])), architecture=ARCH))
        self.assertEqual([(c["path"], c["held"], c["why"]) for c in entry["checks"]], [
            ("opengwasdb/store/paths.py", True, "claimed by `store`"),
            ("opengwasdb/query/facade.py", False, "claimed by `query`, not `store`"),
        ])

    def test_a_dropped_edge_is_held_when_nothing_imports_across_it(self) -> None:
        entry = only(fx.compute(planned(plan(drops=[{"from": "query", "to": "store"}, {"from": "query", "to": "enc"}])),
                                architecture=ARCH))
        self.assertEqual(entry["checks"], [
            {"kind": "drops", "from": "query", "to": "store", "imports": 0, "held": True,
             "why": "no import from `query` into `store`"},
            {"kind": "drops", "from": "query", "to": "enc", "imports": 3, "held": False,
             "why": "3 imports from `query` into `enc` remain"},
        ])

    def test_one_remaining_import_is_singular(self) -> None:
        arch = {**ARCH, "edges": [{"from": "query", "to": "store", "imports": 1, "scope": "engine", "upward": False}]}
        entry = only(fx.compute(planned(plan(drops=[{"from": "query", "to": "store"}])), architecture=arch))
        self.assertEqual(entry["checks"][0]["why"], "1 import from `query` into `store` remains")

    def test_an_exact_claim_that_matches_no_file_is_not_held(self) -> None:
        arch = {**ARCH, "gaps": {**ARCH["gaps"], "engine": {"unclaimed": [], "stale": [
            {"path": "opengwasdb/store/gone.py", "component": "store"}]}},
                "components": [*ARCH["components"][:2], fx.component("store", "Store envelope", "engine",
                                                                     ["opengwasdb/store/gone.py"]), ARCH["components"][3]]}
        entry = only(fx.compute(planned(plan(adds=[{"paths": ["opengwasdb/store/gone.py"], "component": "store"}])),
                                architecture=arch))
        self.assertEqual((entry["checks"][0]["held"], entry["checks"][0]["why"]),
                         (False, "claimed by `store`, but the claim matches no tracked file"))

    def test_without_the_engine_model_engine_items_are_unknown_not_unmet(self) -> None:
        entry = only(fx.compute(planned(plan(
            adds=[{"paths": ["opengwasdb/encoding/association.py", "src/project_site/new.py"], "component": "pages"}],
            drops=[{"from": "pages", "to": "enc"}])), architecture=NO_MODEL))
        why = "the engine's model is missing at main (eeeeeeeeeeee)"
        self.assertEqual([(c.get("path") or c["from"], c["held"], c["why"]) for c in entry["checks"]], [
            ("opengwasdb/encoding/association.py", None, why),
            ("src/project_site/new.py", True, "claimed by `pages`"),
            ("pages", None, why),
        ])

    def test_a_component_the_plan_declares_is_not_unknown_to_it(self) -> None:
        entry = only(fx.compute(planned(plan(adds=[{"paths": ["opengwasdb/encoding/b.py"], "component": "fresh",
                                                     "name": "Fresh"}])), architecture=ARCH))
        self.assertEqual(entry["checks"][0]["held"], False)
        self.assertEqual(entry["checks"][0]["why"], "claimed by `enc`, not `fresh`")
        self.assertEqual(entry["components"], [{"id": "fresh", "name": "Fresh", "repo": None, "new": True}])


class OpenAndInvalidTests(unittest.TestCase):
    def test_an_open_ticket_is_carried_as_planned_with_nothing_checked(self) -> None:
        body = plan(adds=[{"paths": ["opengwasdb/encoding/a.py"], "component": "enc"}], drops=[{"from": "query", "to": "enc"}],
                    absorbs=["encoding/codec.py:932"])
        entry = only(fx.compute(planned(body, state="OPEN"), architecture=ARCH))
        self.assertEqual({k: entry[k] for k in ("ticket", "epic", "state", "plan", "checks")}, {
            "ticket": "opengwasdb#350", "epic": "opengwasdb#58", "state": "open", "plan": body, "checks": []})
        self.assertEqual([c["id"] for c in entry["components"]], ["enc", "query"])

    def test_absorbs_is_carried_never_checked(self) -> None:
        entry = only(fx.compute(planned(plan(absorbs=["encoding/codec.py:932"])), architecture=ARCH))
        self.assertEqual((entry["state"], entry["plan"]["absorbs"], entry["checks"]), ("closed", ["encoding/codec.py:932"], []))

    def test_an_invalid_plan_is_kept_and_checks_nothing(self) -> None:
        entry = only(fx.compute(planned({"invalid": "unknown key `removes`"}), architecture=ARCH))
        self.assertEqual((entry["plan"], entry["checks"], entry["components"]), ({"invalid": "unknown key `removes`"}, [], []))

    def test_an_unknown_component_is_invalid_once_the_model_is_known(self) -> None:
        entry = only(fx.compute(planned(plan(moves=[{"paths": ["a.py"], "to": "ghost"}])), architecture=ARCH))
        self.assertEqual(entry["plan"], {"invalid": "component `ghost` is neither an existing component nor declared in `adds`"})
        self.assertEqual(entry["checks"], [])

    def test_without_the_engine_model_an_unfound_component_is_not_called_unknown(self) -> None:
        entry = only(fx.compute(planned(plan(moves=[{"paths": ["opengwasdb/a.py"], "to": "ghost"}])), architecture=NO_MODEL))
        self.assertNotIn("invalid", entry["plan"])
        self.assertEqual(entry["checks"][0]["held"], None)

    def test_a_ticket_without_a_plan_is_not_listed(self) -> None:
        board = fx.board(fx.epic("opengwasdb#58", "Active", [fx.ticket("opengwasdb#51")]))
        self.assertEqual(fx.compute(board, architecture=ARCH)["plans"], [])

    def test_plans_are_listed_for_tickets_of_epics_that_are_not_live(self) -> None:
        t = fx.ticket("opengwasdb#350", state="CLOSED")
        t["plan"] = plan(drops=[{"from": "query", "to": "store"}])
        board = fx.board(fx.epic("opengwasdb#58", "Done", [t], state="CLOSED"))
        self.assertEqual(only(fx.compute(board, architecture=ARCH))["epic"], "opengwasdb#58")


if __name__ == "__main__":
    unittest.main()

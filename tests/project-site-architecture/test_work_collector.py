#!/usr/bin/env python3
"""The work collector: signals, states and meets from the board, the claims and the branches (#419).

Fixture `board` and `architecture` outputs are small literals; every expected
value is written by hand from them. Components: `enc` claims `opengwasdb/encoding/`,
`query` `opengwasdb/query/`, `store` `opengwasdb/store/` (engine), `pages` claims
`src/project_site/` (registry).

Run from the repository root:
    python3 tests/project-site-architecture/test_work_collector.py
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from work_fixtures import ARCHITECTURE, ENGINE, STORES, board, branch, branch_set, compute, epic, states, ticket

from project_site.collect import work
from project_site.collect.common import CollectError


class MeetTests(unittest.TestCase):
    def test_two_branches_changing_one_component_meet(self) -> None:
        result = compute(
            board(epic("opengwasdb#10", "Active", [ticket("opengwasdb#11")]),
                  epic("opengwasdb#20", "Active", [ticket("opengwasdb#21")])),
            branch_set(
                branch("orch/10/11", ["opengwasdb/encoding/a.py"], epic="opengwasdb#10", ticket="opengwasdb#11"),
                branch("orch/20/21", ["opengwasdb/encoding/b.py", "opengwasdb/query/q.py"], epic="opengwasdb#20", ticket="opengwasdb#21"),
            ),
        )
        self.assertEqual(
            result["meets"],
            [{
                "component": "enc",
                "owners": ["opengwasdb#10", "opengwasdb#20"],
                "items": {"opengwasdb#10": ["opengwasdb#11"], "opengwasdb#20": ["opengwasdb#21"]},
                "pairs": [{"owners": ["opengwasdb#10", "opengwasdb#20"], "coordinated": False, "edges": []}],
                "coordinated": False,
            }],
        )
        self.assertEqual(states(result, "query"), {("opengwasdb#20", "opengwasdb#21"): "building"})

    def test_a_branch_and_a_tickets_cited_files_alone_meet(self) -> None:
        """The opengwasdb#338 against #350 shape: #350 has no branch, only the file its text cites."""
        result = compute(
            board(epic("opengwasdb#58", "Active", [ticket("opengwasdb#350", cited=["opengwasdb/encoding/se_range.py"])]),
                  epic("opengwasdb#347", "Active", [ticket("opengwasdb#338")])),
            branch_set(branch("orch/347/338", ["opengwasdb/encoding/dense.py"], epic="opengwasdb#347", ticket="opengwasdb#338")),
        )
        self.assertEqual(len(result["meets"]), 1)
        meet = result["meets"][0]
        self.assertEqual(meet["component"], "enc")
        self.assertEqual(meet["items"], {"opengwasdb#347": ["opengwasdb#338"], "opengwasdb#58": ["opengwasdb#350"]})
        self.assertFalse(meet["coordinated"])
        self.assertEqual(result["branches"]["list"][0]["name"], "orch/347/338")
        self.assertEqual([b["name"] for b in result["branches"]["list"]], ["orch/347/338"])  # #350 has no branch

    def test_a_blocking_edge_between_the_meeting_tickets_coordinates_it(self) -> None:
        result = compute(
            board(epic("opengwasdb#58", "Active", [ticket("opengwasdb#350", cited=["opengwasdb/encoding/se_range.py"])]),
                  epic("opengwasdb#347", "Active", [ticket("opengwasdb#338", blocked_by=["opengwasdb#350"])])),
            branch_set(branch("orch/347/338", ["opengwasdb/encoding/dense.py"], epic="opengwasdb#347", ticket="opengwasdb#338")),
        )
        meet = result["meets"][0]
        self.assertTrue(meet["coordinated"])
        self.assertEqual(
            meet["pairs"],
            [{"owners": ["opengwasdb#347", "opengwasdb#58"], "coordinated": True,
              "edges": [{"waiting": "opengwasdb#338", "on": "opengwasdb#350"}]}],
        )

    def test_an_edge_in_the_other_direction_coordinates_it_too(self) -> None:
        """#350 waits for #338, and both are being built."""
        result = compute(
            board(epic("opengwasdb#58", "Active", [ticket("opengwasdb#350", blocked_by=["opengwasdb#338"])]),
                  epic("opengwasdb#347", "Active", [ticket("opengwasdb#338")])),
            branch_set(
                branch("orch/347/338", ["opengwasdb/encoding/dense.py"], epic="opengwasdb#347", ticket="opengwasdb#338"),
                branch("orch/58/350", ["opengwasdb/encoding/se_range.py"], epic="opengwasdb#58", ticket="opengwasdb#350"),
            ),
        )
        self.assertEqual(
            result["meets"][0]["pairs"][0]["edges"], [{"waiting": "opengwasdb#350", "on": "opengwasdb#338"}]
        )
        self.assertTrue(result["meets"][0]["coordinated"])

    def test_an_edge_between_other_tickets_of_the_same_epics_does_not_coordinate_it(self) -> None:
        result = compute(
            board(epic("opengwasdb#58", "Active", [ticket("opengwasdb#350", cited=["opengwasdb/encoding/se_range.py"]), ticket("opengwasdb#351")]),
                  epic("opengwasdb#347", "Active", [ticket("opengwasdb#338"), ticket("opengwasdb#339", blocked_by=["opengwasdb#351"])])),
            branch_set(branch("orch/347/338", ["opengwasdb/encoding/dense.py"], epic="opengwasdb#347", ticket="opengwasdb#338")),
        )
        self.assertEqual([(m["component"], m["coordinated"]) for m in result["meets"]], [("enc", False)])
        self.assertEqual(result["waits"], [{"item": "opengwasdb#339", "on": "opengwasdb#351"}])

    def test_a_closed_blocker_is_not_an_edge(self) -> None:
        blocked = ticket("opengwasdb#338")
        blocked["blocked_by"] = [{"key": "opengwasdb#350", "state": "CLOSED"}]
        result = compute(
            board(epic("opengwasdb#58", "Active", [ticket("opengwasdb#350", cited=["opengwasdb/encoding/se_range.py"])]),
                  epic("opengwasdb#347", "Active", [blocked])),
            branch_set(branch("orch/347/338", ["opengwasdb/encoding/dense.py"], epic="opengwasdb#347", ticket="opengwasdb#338")),
        )
        self.assertEqual(result["waits"], [])
        self.assertFalse(result["meets"][0]["coordinated"])

    def test_work_of_one_owner_on_a_component_is_not_a_meet(self) -> None:
        result = compute(
            board(epic("opengwasdb#10", "Active", [ticket("opengwasdb#11", cited=["opengwasdb/encoding/a.py"]), ticket("opengwasdb#12")])),
            branch_set(branch("orch/10/12", ["opengwasdb/encoding/b.py"], epic="opengwasdb#10", ticket="opengwasdb#12")),
        )
        self.assertEqual(result["meets"], [])


class StateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.result = compute(
            board(
                epic("opengwasdb#10", "Active", [
                    ticket("opengwasdb#11", cited=["opengwasdb/encoding/a.py"]),
                    ticket("opengwasdb#12", cited=["opengwasdb/query/q.py"], blocked_by=["opengwasdb#11"]),
                    ticket("opengwasdb#13", cited=["opengwasdb/store/s.py"], state="CLOSED"),
                ], cited=["opengwasdb/store/s.py"]),
                epic("opengwasdb#20", "Todo", [ticket("opengwasdb#21", cited=["src/project_site/pages.py"])]),
                epic("opengwasdb#30", "Paused", [ticket("opengwasdb#31", cited=["opengwasdb/encoding/z.py"])]),
                epic("opengwasdb#40", "Done", [ticket("opengwasdb#41", cited=["opengwasdb/query/y.py"])], state="CLOSED"),
            ),
            branch_set(branch("orch/10/11", ["opengwasdb/query/q.py"], epic="opengwasdb#10", ticket="opengwasdb#11")),
        )

    def test_an_open_ticket_of_an_active_epic_is_active(self) -> None:
        self.assertEqual(states(self.result, "enc")[("opengwasdb#10", "opengwasdb#11")], "active")

    def test_a_branch_diff_is_building(self) -> None:
        self.assertEqual(states(self.result, "query")[("opengwasdb#10", "opengwasdb#11")], "building")

    def test_an_epic_body_is_scope_and_a_closed_ticket_adds_nothing(self) -> None:
        self.assertEqual(states(self.result, "store"), {("opengwasdb#10", "opengwasdb#10"): "scope"})

    def test_a_ticket_blocked_by_an_open_ticket_is_waiting(self) -> None:
        self.assertEqual(states(self.result, "query")[("opengwasdb#10", "opengwasdb#12")], "waiting")

    def test_tickets_of_todo_and_paused_epics_are_planned_and_paused(self) -> None:
        self.assertEqual(states(self.result, "pages"), {("opengwasdb#20", "opengwasdb#21"): "planned"})
        self.assertEqual(states(self.result, "enc")[("opengwasdb#30", "opengwasdb#31")], "paused")

    def test_a_closed_epic_is_no_owner(self) -> None:
        self.assertNotIn("opengwasdb#40", [o["key"] for o in self.result["owners"]])

    def test_only_signals_of_building_or_active_work_make_a_meet(self) -> None:
        self.assertEqual(self.result["meets"], [])  # paused, planned, waiting and scope never meet

    def test_every_open_ticket_of_a_live_epic_is_listed_with_its_owner(self) -> None:
        self.assertEqual(
            self.result["tickets"],
            {"opengwasdb#11": "opengwasdb#10", "opengwasdb#12": "opengwasdb#10", "opengwasdb#21": "opengwasdb#20",
             "opengwasdb#31": "opengwasdb#30"},
        )

    def test_the_summary_counts_what_the_lists_hold(self) -> None:
        self.assertEqual(
            self.result["summary"],
            {"owners": 3, "meets": 0, "coordinated": 0, "uncoordinated": 0, "branches": 1},
        )


class BranchOwnerTests(unittest.TestCase):
    def test_a_lane_of_an_epic_in_the_other_repository_belongs_to_that_epic(self) -> None:
        result = compute(
            board(epic("opengwasdb-stores#416", "Active", [ticket("opengwasdb-stores#419")])),
            branch_set(branch("orch/416/419", ["src/project_site/pages.py"], repo=STORES, epic="opengwasdb-stores#416",
                              ticket="opengwasdb-stores#419"), repo=STORES),
        )
        self.assertEqual(states(result, "pages"), {("opengwasdb-stores#416", "opengwasdb-stores#419"): "building"})

    def test_a_branch_naming_only_a_ticket_belongs_to_that_tickets_epic(self) -> None:
        result = compute(
            board(epic("opengwasdb#10", "Active", [ticket("opengwasdb#11")])),
            branch_set(branch("fix/issue-11", ["opengwasdb/store/s.py"], ticket="opengwasdb#11")),
        )
        self.assertEqual(states(result, "store"), {("opengwasdb#10", "opengwasdb#11"): "building"})
        self.assertEqual(result["branches"]["list"][0]["owner"], "opengwasdb#10")

    def test_a_branch_that_maps_to_no_ticket_is_its_own_owner_labelled_by_its_name(self) -> None:
        result = compute(board(), branch_set(branch("scratch", ["opengwasdb/store/s.py"])))
        self.assertEqual(
            [(o["key"], o["kind"], o["title"]) for o in result["owners"]],
            [("branch:opengwasdb:scratch", "branch", "scratch")],
        )

    def test_a_file_no_component_claims_is_in_no_signal(self) -> None:
        result = compute(board(), branch_set(branch("scratch", ["README.md"])))
        self.assertEqual(result["signals"], [])
        self.assertEqual(result["branches"]["list"][0]["components"], [])

    def test_a_branch_read_from_two_clones_counts_once_and_the_one_further_ahead_wins(self) -> None:
        pushed = branch("orch/10/11", ["opengwasdb/encoding/a.py"], epic="opengwasdb#10", ticket="opengwasdb#11")
        local = {**pushed, "ahead": 3, "files": ["opengwasdb/encoding/a.py", "opengwasdb/query/q.py"], "local": True}
        result = compute(board(epic("opengwasdb#10", "Active", [ticket("opengwasdb#11")])), branch_set(pushed), branch_set(local))
        self.assertEqual([(b["name"], b["ahead"], b["local"]) for b in result["branches"]["list"]], [("orch/10/11", 3, True)])
        self.assertEqual(result["summary"]["branches"], 1)
        self.assertEqual(sorted({s["component"] for s in result["signals"]}), ["enc", "query"])

    def test_a_failed_fetch_is_carried_to_the_output(self) -> None:
        result = compute(board(), branch_set(fetch={"ok": False, "error": "no network"}))
        self.assertEqual(result["branches"]["sources"][0]["fetch"], {"ok": False, "error": "no network"})


class UnavailableTests(unittest.TestCase):
    def collect(self, files: dict[str, dict]) -> CollectError:
        directory = Path(tempfile.mkdtemp(prefix="work-"))
        self.addCleanup(__import__("shutil").rmtree, directory, True)
        for name, document in files.items():
            (directory / f"{name}.json").write_text(json.dumps(document))
        with self.assertRaises(CollectError) as caught:
            work.collect(directory)
        return caught.exception

    def test_work_is_unavailable_naming_the_board_when_it_is(self) -> None:
        message = str(self.collect({"board": {"source": "board", "available": False, "error": "gh failed"}, "architecture": ARCHITECTURE}))
        self.assertIn("board", message)
        self.assertNotIn("architecture", message)

    def test_work_is_unavailable_naming_architecture_when_it_is(self) -> None:
        message = str(self.collect({"board": board(), "architecture": {"source": "architecture", "available": False, "error": "no clone"}}))
        self.assertIn("architecture", message)
        self.assertNotIn("board", message)

    def test_work_names_both_and_a_file_that_was_never_written_counts_as_unavailable(self) -> None:
        message = str(self.collect({}))
        self.assertIn("board", message)
        self.assertIn("architecture", message)


if __name__ == "__main__":
    unittest.main()

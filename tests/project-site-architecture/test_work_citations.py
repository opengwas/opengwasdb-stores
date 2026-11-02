#!/usr/bin/env python3
"""Package-relative citations in the work collector, and the shared resolver (#416 final).

#350 of the engine cites `encoding/codec.py` and `layouts/ragged/zarr_csr.py:551`,
not `opengwasdb/...`. Expected values are written by hand.

Run from the repository root:
    python3 tests/project-site-architecture/test_work_citations.py
"""

from __future__ import annotations

import unittest

from work_fixtures import ARCHITECTURE, board, compute, component, epic, states, ticket

from project_site.collect import citations

FILES = {
    "engine": [
        "opengwasdb/encoding/codec.py",
        "opengwasdb/layouts/ragged/zarr_csr.py",
        "opengwasdb/a/x/dup.py",
        "opengwasdb/b/x/dup.py",
    ],
    "registry": ["src/project_site/pages.py", "src/other/dup.py"],
}
WITH_FILES = {
    **ARCHITECTURE,
    "files": FILES,
    "components": [*ARCHITECTURE["components"], component("ragged", "Ragged layout", "engine", ["opengwasdb/layouts/ragged/"])],
}


def work_of(*cited: str) -> dict:
    return compute(board(epic("opengwasdb#58", "Active", [ticket("opengwasdb#350", cited=list(cited))])), architecture=WITH_FILES)


class ResolverTests(unittest.TestCase):
    def test_an_exact_file_is_that_file(self) -> None:
        self.assertEqual(citations.resolve_path("opengwasdb/encoding/codec.py", "engine", FILES).found,
                         ("engine", "opengwasdb/encoding/codec.py"))

    def test_a_line_is_stripped(self) -> None:
        self.assertEqual(citations.resolve_path("layouts/ragged/zarr_csr.py:551", "engine", FILES).found,
                         ("engine", "opengwasdb/layouts/ragged/zarr_csr.py"))

    def test_a_suffix_matching_two_files_is_none_with_both_as_candidates(self) -> None:
        got = citations.resolve_path("x/dup.py", "engine", FILES)
        self.assertIsNone(got.found)
        self.assertEqual(got.candidates, (("engine", "opengwasdb/a/x/dup.py"), ("engine", "opengwasdb/b/x/dup.py")))


class WorkCitationTests(unittest.TestCase):
    def test_a_package_relative_citation_signals_the_component(self) -> None:
        self.assertEqual(states(work_of("encoding/codec.py"), "enc"), {("opengwasdb#58", "opengwasdb#350"): "active"})

    def test_a_package_relative_citation_with_a_line_signals_the_component(self) -> None:
        self.assertEqual(states(work_of("layouts/ragged/zarr_csr.py:551"), "ragged"), {("opengwasdb#58", "opengwasdb#350"): "active"})

    def test_an_ambiguous_suffix_places_nothing_and_is_listed_unresolved(self) -> None:
        result = work_of("x/dup.py")
        self.assertEqual(result["signals"], [])
        self.assertEqual(result["unresolved"], [{
            "owner": "opengwasdb#58", "item": "opengwasdb#350", "cited": "x/dup.py",
            "candidates": ["opengwasdb/a/x/dup.py", "opengwasdb/b/x/dup.py"]}])
        self.assertEqual(result["summary"]["unresolved"], 1)

    def test_a_suffix_unique_across_the_other_repository_resolves(self) -> None:
        self.assertEqual(states(work_of("project_site/pages.py"), "pages"), {("opengwasdb#58", "opengwasdb#350"): "active"})

    def test_a_resolved_citation_is_not_unresolved(self) -> None:
        result = work_of("encoding/codec.py")
        self.assertEqual((result["unresolved"], result["summary"]["unresolved"]), ([], 0))

    def test_a_nonexistent_code_path_keeps_its_claimed_signal_marked_not_found(self) -> None:
        result = work_of("opengwasdb/encoding/new_file.py")
        self.assertEqual(states(result, "enc"), {("opengwasdb#58", "opengwasdb#350"): "active"})
        self.assertEqual([s["path"] for s in result["signals"]], ["not found"])
        self.assertEqual(result["not_found"], [{"owner": "opengwasdb#58", "item": "opengwasdb#350",
                                                 "cited": "opengwasdb/encoding/new_file.py", "component": "enc"}])
        self.assertEqual(result["summary"]["not_found"], 1)

    def test_an_existing_file_is_an_unmarked_signal(self) -> None:
        result = work_of("opengwasdb/encoding/codec.py")
        self.assertEqual([s["path"] for s in result["signals"]], ["found"])
        self.assertEqual((result["not_found"], result["summary"]["not_found"]), ([], 0))

    def test_without_a_file_inventory_existence_is_unchecked_not_found(self) -> None:
        result = compute(board(epic("opengwasdb#58", "Active", [ticket("opengwasdb#350", cited=["opengwasdb/encoding/new_file.py"])])))
        self.assertEqual([s["path"] for s in result["signals"]], ["unchecked"])
        self.assertEqual(result["not_found"], [])

    def test_a_non_code_path_is_unchecked_because_only_code_files_are_listed(self) -> None:
        result = work_of("opengwasdb/encoding/README.md")
        self.assertEqual([s["path"] for s in result["signals"]], ["unchecked"])
        self.assertEqual(result["not_found"], [])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Five dbGaP MVP Selections and the retired partition's bound counts (#290).

The MVP must split into five disjoint ancestry Selections. Its adapter writes
the post-split `store_key`, so each Selection is a rule on a canonical column;
it also declares `expect_rows`, which turns the retired partition's bound counts
into a check that fails by name when the Source Metadata Table drifts.

Run from the repository root:
    python3 tests/dbgap-mvp/test_selection_partition.py
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from manifest_generation.connector_tsv import write_connector
from manifest_generation.selection import SelectionError, parse_selection, select
from manifest_generation.source_inventory import selection_partition_problems
from manifest_generation.source_metadata_table import SOURCE_METADATA_TABLE, load_source_metadata_table

#: bucket -> (store_key, expected bound counts, number of fixture rows).
BUCKETS = (
    ("EUR", "dense__pmid-39024449__European", 1_669, 2),
    ("AFR", "dense__pmid-39024449__African", 1_368, 2),
    ("AMR", "dense__pmid-39024449__Hispanic-or-Latin-American", 1_053, 1),
    ("EAS", "dense__pmid-39024449__East-Asian", 241, 1),
    ("META", "dense__pmid-39024449__Multi-ancestry", 1_691, 1),
)


def build_table(root: Path) -> Path:
    rows: list[dict[str, str]] = []
    number = 0
    for _bucket, store_key, _bound, count in BUCKETS:
        for _ in range(count):
            number += 1
            rows.append({
                "source_analysis_id": f"GCST9000{number:04d}",
                "source_ancestry_label": _bucket,
                "store_key": store_key,
                "study_design": "quantitative",
                "sample_size": "1000",
            })
    path = root / "source-metadata.tsv"
    write_connector(path, SOURCE_METADATA_TABLE, SOURCE_METADATA_TABLE.columns, rows)
    return path


class SelectionPartitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="dbgap-selection-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.table = load_source_metadata_table(build_table(self.root))

    def selections(self) -> list[tuple[str, object]]:
        return [
            (bucket, parse_selection({"where": {"store_key": store_key}, "expect_rows": bound}))
            for bucket, store_key, bound, _count in BUCKETS
        ]

    def test_five_selections_are_disjoint_and_cover_the_table(self) -> None:
        chosen: list[str] = []
        for bucket, selection in self.selections():
            # The fixture's expected count is its row count, not the real bound.
            count = next(count for name, _key, _bound, count in BUCKETS if name == bucket)
            selection = parse_selection({"where": {"store_key": selection.rule[0][1][0]}, "expect_rows": count})
            selected = select(self.table, selection)
            self.assertEqual(len(selected), count, bucket)
            chosen.extend(row["source_analysis_id"] for row in selected)
        self.assertEqual(len(chosen), len(set(chosen)))
        self.assertEqual(set(chosen), set(self.table.analysis_ids))

    def test_the_bound_count_is_part_of_the_selection_declaration(self) -> None:
        selection = parse_selection({"where": {"store_key": BUCKETS[0][1]}, "expect_rows": 1_669})
        self.assertEqual(selection.expect_rows, 1_669)
        self.assertEqual(
            selection.as_dict(),
            {"where": {"store_key": BUCKETS[0][1]}, "expect_rows": 1_669},
        )

    def test_a_count_that_drifts_fails_by_name(self) -> None:
        selection = parse_selection({"where": {"store_key": BUCKETS[0][1]}, "expect_rows": 1_668})
        with self.assertRaisesRegex(SelectionError, "EUR|European|1668|1669"):
            select(self.table, selection)

    def test_expect_rows_must_be_a_positive_integer(self) -> None:
        for bad in (0, -1, "1669", True, 1.5):
            with self.subTest(value=bad):
                with self.assertRaisesRegex(SelectionError, "expect_rows"):
                    parse_selection({"where": {"store_key": BUCKETS[0][1]}, "expect_rows": bad})

    def test_the_old_forms_are_unchanged(self) -> None:
        self.assertIsNone(parse_selection({"all": True}).expect_rows)
        self.assertEqual(parse_selection({"analyses": ["GCST90000001"]}).form, "analyses")


class MultiConfigPreflightTests(unittest.TestCase):
    """Five Selections of one collection must partition it, or fail by name."""

    def test_disjoint_selections_over_one_collection_are_accepted(self) -> None:
        releases = [
            ("EUR", "dbgap-phs002453", ["A", "B"]),
            ("AFR", "dbgap-phs002453", ["C"]),
        ]
        self.assertEqual(
            selection_partition_problems(releases, collection_ids={"A", "B", "C"}), []
        )

    def test_an_analysis_in_two_selections_is_named_with_both_labels(self) -> None:
        problems = selection_partition_problems([
            ("EUR", "dbgap-phs002453", ["A", "B"]),
            ("META", "dbgap-phs002453", ["B"]),
        ])
        self.assertTrue(any("B" in problem and "EUR" in problem and "META" in problem for problem in problems))

    def test_selections_of_two_collections_are_refused(self) -> None:
        problems = selection_partition_problems([
            ("EUR", "dbgap-phs002453", ["A"]),
            ("other", "gwas-catalog-ssf", ["B"]),
        ])
        self.assertTrue(any("one preflight is one collection" in problem for problem in problems))

    def test_a_collection_row_no_selection_takes_is_named(self) -> None:
        problems = selection_partition_problems(
            [("EUR", "dbgap-phs002453", ["A"])], collection_ids={"A", "B"}
        )
        self.assertTrue(any("selected by no Selection" in problem and "B" in problem for problem in problems))


if __name__ == "__main__":
    unittest.main()

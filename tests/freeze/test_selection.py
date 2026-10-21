#!/usr/bin/env python3
"""Selection: which of a Source Collection's intended Analyses one release takes (#283).

A release config declares exactly one of:

    selection:
      all: true                         # every row of the Source Metadata Table

    selection:
      analyses: [GCST1, GCST2]          # an explicit list

    selection:
      where:                            # a rule: AND over named canonical columns
        store_key: hybrid__European     #   equality
        study_design: [quantitative, case-control]   # membership

A list naming an Analysis the table does not intend is refused, and so is a
Selection that selects nothing.

Run from the repository root:
    python3 tests/freeze/test_selection.py
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from manifest_generation import REPO_ROOT
from manifest_generation.connector_tsv import FreezeError
from manifest_generation.selection import (
    Selection,
    SelectionError,
    load_selection,
    parse_selection,
    select,
)
from manifest_generation.source_metadata_table import load_source_metadata_table

HEADER = ("source_analysis_id", "source_trait_label", "study_design", "source_superpopulation", "store_key", "publication_pmid")
ROWS = (
    ("A1", "Height", "quantitative", "EUR", "hybrid__European", "11"),
    ("A2", "Asthma", "case-control", "EUR", "hybrid__European", "22"),
    ("A3", "BMI", "quantitative", "EAS", "hybrid__East-Asian", "33"),
    ("A4", "Gout", "case-control", "", "hybrid__Multiple-Mixed", "22"),
)


class SelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        path = self.tmp / "table.tsv"
        path.write_text(
            "\n".join("\t".join(row) for row in (HEADER,) + ROWS) + "\n", encoding="utf-8"
        )
        self.table = load_source_metadata_table(path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def ids(self, raw) -> list[str]:
        return [row["source_analysis_id"] for row in select(self.table, parse_selection(raw))]

    # -- the three forms ------------------------------------------------------

    def test_all_rows(self) -> None:
        self.assertEqual(self.ids({"all": True}), ["A1", "A2", "A3", "A4"])

    def test_a_list(self) -> None:
        # Selected rows come back in table order, whatever order the list names them.
        self.assertEqual(self.ids({"analyses": ["A3", "A1"]}), ["A1", "A3"])

    def test_a_rule_of_equality(self) -> None:
        self.assertEqual(self.ids({"where": {"store_key": "hybrid__European"}}), ["A1", "A2"])

    def test_a_rule_of_membership(self) -> None:
        self.assertEqual(self.ids({"where": {"source_superpopulation": ["EAS", "EUR"]}}), ["A1", "A2", "A3"])

    def test_a_rule_ands_its_columns(self) -> None:
        rule = {"where": {"source_superpopulation": "EUR", "study_design": ["case-control"]}}
        self.assertEqual(self.ids(rule), ["A2"])

    def test_a_yaml_integer_matches_its_text(self) -> None:
        self.assertEqual(self.ids({"where": {"publication_pmid": 22}}), ["A2", "A4"])

    def test_a_blank_cell_never_matches(self) -> None:
        self.assertEqual(self.ids({"where": {"source_superpopulation": ["EUR", "EAS"]}}), ["A1", "A2", "A3"])

    # -- the recorded form ----------------------------------------------------

    def test_selection_records_its_declaration(self) -> None:
        rule = parse_selection({"where": {"study_design": "quantitative", "publication_pmid": 11}})
        self.assertEqual(rule.as_dict(), {"where": {"study_design": "quantitative", "publication_pmid": "11"}})
        self.assertEqual(parse_selection({"all": True}).as_dict(), {"all": True})
        self.assertEqual(parse_selection({"analyses": ["A2", "A1"]}).as_dict(), {"analyses": ["A2", "A1"]})
        self.assertEqual(parse_selection(rule.as_dict()), rule)

    def test_load_reads_the_release_configs_selection_block(self) -> None:
        config = self.tmp / "release.yaml"
        config.write_text(
            "label: trial\nselection:\n  where:\n    store_key: hybrid__European\n", encoding="utf-8"
        )
        self.assertEqual(
            load_selection(config), Selection(form="where", rule=(("store_key", ("hybrid__European",)),))
        )

    # -- construction ---------------------------------------------------------

    def test_a_selection_is_exactly_one_form(self) -> None:
        self.assertEqual(Selection(form="all").as_dict(), {"all": True})
        for bad in (
            {},  # no form at all
            {"form": "where"},  # a rule with no columns
            {"form": "analyses"},  # a list with no Analyses
            {"form": "all", "analyses": ("A1",)},  # two forms at once
            {"form": "where", "analyses": ("A1",), "rule": (("study_design", ("quantitative",)),)},
            {"form": "except", "analyses": ("A1",)},
        ):
            with self.subTest(bad=bad), self.assertRaises(SelectionError):
                Selection(**bad)

    # -- the legacy grammar -----------------------------------------------------

    def test_every_legacy_release_config_selection_is_refused_by_name(self) -> None:
        """Today's generator configs use another grammar; #287 migrates them, nothing reads it here."""
        configs = (
            "resources/generators/eqtlgen-cis-pilot/config-pilot-10.yaml",
            "resources/generators/finngen-r13/config-full.yaml",
            "resources/generators/finngen-r13/config-pilot-20.yaml",
            "resources/generators/gwas-catalog-eur-hybrid/config-pilot-10.yaml",
            "resources/generators/gwas-catalog-eur-hybrid/config-quant-pilot-10.yaml",
            "resources/generators/metabolome-plasma-2023/config-european.yaml",
            "resources/generators/pqtl-interval-2018/config-pilot-10.yaml",
        )
        for config in configs:
            with self.subTest(config=config):
                with self.assertRaises(SelectionError) as caught:
                    load_selection(REPO_ROOT / config)
                self.assertIn("legacy Selection", str(caught.exception))
                self.assertIn("#287", str(caught.exception))

    def test_a_legacy_key_is_named(self) -> None:
        with self.assertRaises(SelectionError) as caught:
            parse_selection({"store_key": "hybrid__European", "n_analyses": 10})
        self.assertIn("n_analyses", str(caught.exception))
        self.assertIn("store_key", str(caught.exception))

    # -- refusals -------------------------------------------------------------

    def assertRefused(self, raw, *fragments: str) -> None:
        with self.assertRaises(SelectionError) as caught:
            select(self.table, parse_selection(raw))
        for fragment in fragments:
            self.assertIn(fragment, str(caught.exception))

    def test_a_list_naming_an_unknown_analysis_is_refused(self) -> None:
        self.assertRefused({"analyses": ["A1", "GCST404"]}, "GCST404")

    def test_a_list_naming_an_analysis_twice_is_refused(self) -> None:
        self.assertRefused({"analyses": ["A1", "A1"]}, "A1", "twice")

    def test_an_empty_list_is_refused(self) -> None:
        self.assertRefused({"analyses": []}, "analyses")

    def test_a_selection_of_nothing_is_refused(self) -> None:
        self.assertRefused({"where": {"store_key": "hybrid__Martian"}}, "no Analysis")

    def test_exactly_one_form(self) -> None:
        self.assertRefused({}, "exactly one")
        self.assertRefused({"all": True, "analyses": ["A1"]}, "exactly one")

    def test_unknown_keys_are_refused(self) -> None:
        self.assertRefused({"all": True, "except": ["A1"]}, "except")

    def test_all_must_be_true(self) -> None:
        self.assertRefused({"all": False}, "all")
        self.assertRefused({"all": "yes"}, "all")

    def test_a_rule_over_a_non_canonical_column_is_refused(self) -> None:
        self.assertRefused({"where": {"STUDY.ACCESSION": "A1"}}, "STUDY.ACCESSION")

    def test_a_rule_over_a_column_the_table_lacks_is_refused(self) -> None:
        self.assertRefused({"where": {"first_author": "Doe J"}}, "first_author", "does not carry")

    def test_bad_rule_values_are_refused(self) -> None:
        self.assertRefused({"where": {}}, "where")
        self.assertRefused({"where": {"study_design": []}}, "study_design")
        self.assertRefused({"where": {"study_design": ""}}, "study_design")
        self.assertRefused({"where": {"study_design": None}}, "study_design")
        self.assertRefused({"where": {"study_design": {"not": "quantitative"}}}, "study_design")
        self.assertRefused({"where": {"study_design": True}}, "study_design")

    def test_a_missing_selection_block_is_refused(self) -> None:
        config = self.tmp / "release.yaml"
        config.write_text("label: trial\n", encoding="utf-8")
        with self.assertRaises(SelectionError):
            load_selection(config)

    def test_refusal_is_a_freeze_error(self) -> None:
        self.assertTrue(issubclass(SelectionError, FreezeError))


if __name__ == "__main__":
    unittest.main(verbosity=2)

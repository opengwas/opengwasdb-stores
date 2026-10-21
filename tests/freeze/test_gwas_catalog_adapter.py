#!/usr/bin/env python3
"""The GWAS Catalog metadata adapter: candidate table -> Source Metadata Table (#283).

`providers/gwas_catalog/ebi-studies.r` writes the GWAS Catalog candidate table
(`STUDY.ACCESSION`, `DISEASE.TRAIT`, `MAPPED_TRAIT_URI`, ...). The adapter
converts it, unchanged, into the canonical Source Metadata Table the shared
freeze reads. The fixture is seven real rows of that table, chosen for their
edge cases: a case-control study, a trait label with trailing non-breaking
spaces, a label the source left empty, several mapped ontology terms, and
Source Ancestry Labels the tracked super-population map does and does not
translate.

Run from the repository root:
    python3 tests/freeze/test_gwas_catalog_adapter.py
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from manifest_generation.providers.gwas_catalog.source_metadata_adapter import (
    AdapterError,
    convert_candidate_table,
    main,
)
from manifest_generation.source_metadata_table import SourceMetadataTableError, load_source_metadata_table

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "gwas-catalog-candidates.tsv"


class GwasCatalogAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def convert(self, source: Path = FIXTURE):
        out = self.tmp / "source-metadata.tsv"
        convert_candidate_table(source, out)
        return load_source_metadata_table(out)

    def test_fixture_exercises_its_edge_cases(self) -> None:
        text = FIXTURE.read_text(encoding="utf-8")
        self.assertIn("\xa0\t", text)  # a trait label padded with non-breaking spaces
        self.assertIn('\t""\t', text)  # a label the source left empty, written by fwrite as ""
        self.assertIn("case-control", text)
        self.assertIn("NR/Unknown", text)

    def test_every_candidate_becomes_one_canonical_row(self) -> None:
        table = self.convert()
        self.assertEqual(
            table.analysis_ids,
            ("GCST90038595", "GCST90501976", "GCST90727297", "GCST000028",
             "GCST001526", "GCST90006908", "GCST90011771"),
        )
        self.assertNotIn("STUDY.ACCESSION", table.columns)

    def test_a_case_control_row(self) -> None:
        row = self.convert().row("GCST000028")
        self.assertEqual(
            {column: row[column] for column in (
                "source_trait_label", "source_trait_ontology_id", "source_trait_ontology_label",
                "source_ancestry_label", "source_superpopulation", "study_design", "sample_size",
                "n_cases", "n_controls", "publication_pmid", "first_author", "store_key",
            )},
            {
                "source_trait_label": "Type 2 diabetes",
                "source_trait_ontology_id": "http://purl.obolibrary.org/obo/MONDO_0005148",
                "source_trait_ontology_label": "type 2 diabetes mellitus",
                "source_ancestry_label": "European",
                "source_superpopulation": "EUR",
                "study_design": "case-control",
                "sample_size": "2931",
                "n_cases": "1464",
                "n_controls": "1467",
                "publication_pmid": "17463246",
                "first_author": "Saxena R",
                "store_key": "hybrid__European",
            },
        )

    def test_collection_wide_values_are_left_to_the_release_config(self) -> None:
        """Genome build and licence are not per-Analysis source facts here; #287's config supplies them."""
        table = self.convert()
        self.assertNotIn("source_genome_build", table.columns)
        self.assertNotIn("license", table.columns)

    def test_quantitative_case_counts_are_absent_not_zero(self) -> None:
        """ebi-studies.r writes 0/0 for a quantitative study; zero would claim a count (#133)."""
        row = self.convert().row("GCST90006908")
        self.assertEqual((row["study_design"], row["n_cases"], row["n_controls"]), ("quantitative", "", ""))

    def test_padding_is_trimmed_as_the_candidate_freeze_trimmed_it(self) -> None:
        row = self.convert().row("GCST90006908")
        self.assertEqual(row["source_trait_label"], "Anti-human herpes\xa0virus\xa07 IgG\xa0seropositivity")

    def test_an_empty_source_label_stays_absent(self) -> None:
        row = self.convert().row("GCST90727297")
        self.assertEqual(row["source_trait_label"], "")
        self.assertEqual(row["source_trait_ontology_label"], "alpha-fetoprotein level")

    def test_several_ontology_terms_are_carried_verbatim(self) -> None:
        """Normalising source ontology terms is Trait Ontology Mapping's job (#286), not the adapter's."""
        row = self.convert().row("GCST001526")
        self.assertEqual(
            row["source_trait_ontology_id"],
            "http://www.ebi.ac.uk/efo/EFO_0004340, http://purl.obolibrary.org/obo/OBA_VT0001560",
        )

    def test_superpopulation_comes_from_the_tracked_map(self) -> None:
        table = self.convert()
        self.assertEqual(table.row("GCST90011771")["source_superpopulation"], "EAS")  # South East Asian
        # Labels listed as deliberately imprecise keep their label and leave the code absent.
        for analysis_id, label in (("GCST90038595", "NR/Unknown"), ("GCST90501976", "Multiple/Mixed")):
            self.assertEqual(table.row(analysis_id)["source_ancestry_label"], label)
            self.assertEqual(table.row(analysis_id)["source_superpopulation"], "")

    def test_an_unknown_ancestry_label_is_refused_by_name(self) -> None:
        """Neither translated nor deliberately imprecise: a new or misspelt label is not silently blank."""
        message = self.refused("\tSouth East Asian\t", "\tSouth-East Asian\t", AdapterError)
        self.assertIn("South-East Asian", message)
        self.assertIn("GCST90011771", message)

    def test_a_quantitative_row_with_case_counts_is_refused(self) -> None:
        text = FIXTURE.read_text(encoding="utf-8").replace(
            "quantitative\t0\t0\t8735", "quantitative\t12\t0\t8735"
        )
        bad = self.tmp / "candidates.tsv"
        bad.write_text(text, encoding="utf-8")
        with self.assertRaises(AdapterError) as caught:
            self.convert(bad)
        self.assertIn("GCST90006908", str(caught.exception))

    def refused(self, old: str, new: str, error: type[Exception]) -> str:
        text = FIXTURE.read_text(encoding="utf-8")
        self.assertIn(old, text)
        bad = self.tmp / "candidates.tsv"
        bad.write_text(text.replace(old, new), encoding="utf-8")
        with self.assertRaises(error) as caught:
            self.convert(bad)
        self.assertFalse((self.tmp / "source-metadata.tsv").exists(), "a refused table is not left behind")
        return str(caught.exception)

    def test_an_r_missing_token_is_not_read_as_absent(self) -> None:
        """ebi-studies.r's fwrite writes NA as an empty field; a literal NA is not a missing value."""
        message = self.refused("quantitative\t0\t0\t8735", "quantitative\t0\t0\tNA", SourceMetadataTableError)
        self.assertIn("sample_size", message)

    def test_case_counts_without_a_study_design_are_refused_not_blanked(self) -> None:
        message = self.refused("case-control\t1464\t1467", "\t1464\t1467", AdapterError)
        self.assertIn("GCST000028", message)

    def test_a_candidate_table_missing_a_column_is_refused(self) -> None:
        lines = FIXTURE.read_text(encoding="utf-8").splitlines()
        header = lines[0].split("\t")
        keep = [i for i, name in enumerate(header) if name != "DISEASE.TRAIT"]
        bad = self.tmp / "candidates.tsv"
        bad.write_text(
            "\n".join("\t".join(line.split("\t")[i] for i in keep) for line in lines) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(AdapterError) as caught:
            self.convert(bad)
        self.assertIn("DISEASE.TRAIT", str(caught.exception))

    def test_the_command_writes_the_table(self) -> None:
        out = self.tmp / "cli.tsv"
        self.assertEqual(main(["--candidates", str(FIXTURE), "--out", str(out)]), 0)
        self.assertEqual(len(load_source_metadata_table(out).rows), 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)

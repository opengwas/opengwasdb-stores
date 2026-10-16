#!/usr/bin/env python3
"""Trait Ontology Mapping: the shared step after verification (#286, ADR 0036 step 5).

A source-provided term is kept; otherwise the Source Trait Label is looked up,
offline, in the Canonical Trait Mapping Table; otherwise the Analysis is left
unmapped. The method is recorded every time. Source terms are normalised to
CURIEs term by term; a gene-shaped identifier or an authority-name label is
never emitted (#141).

Run from the repository root:
    python3 tests/trait-ontology-mapping/test_trait_ontology_mapping.py
"""

from __future__ import annotations

import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from manifest_generation import REPO_ROOT
from manifest_generation.trait_ontology_mapping import (
    CANONICAL_TABLE_LOOKUP,
    CANONICAL_TRAIT_MAPPING_RESOURCE,
    SOURCE_PROVIDED,
    TRAIT_ONTOLOGY_MAPPING_METHODS,
    UNMAPPED,
    SourceTrait,
    TraitOntologyMapping,
    TraitOntologyMappingError,
    load_canonical_trait_mapping_table,
    map_trait_ontologies,
    map_trait_ontology,
    normalise_trait_ontology_id,
)

TABLE_HEADER = (
    "trait_label\ttrait_ontology_id\ttrait_ontology_label\tontology_release\t"
    "chooser_id\tconfidence\trunner_up_margin\treview_status\treviewer\treviewed_at\n"
)


def _write_resource(root: Path, rows: list[tuple[str, str, str]], *, version: int = 7) -> Path:
    """A Canonical Trait Mapping Table Reference Resource directory."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "resource.yaml").write_text(
        "resource_id: canonical-trait-mapping-efo\n"
        "kind: trait_ontology_mapping\n"
        f"version: {version}\n"
        "location: resources/reference-resources/canonical-trait-mapping-efo/mapping.tsv\n"
        "location_kind: tracked_file\n",
        encoding="utf-8",
    )
    body = "".join(f"{label}\t{curie}\t{term}\t\t\t\t\t\t\t\n" for label, curie, term in rows)
    (root / "mapping.tsv").write_text(TABLE_HEADER + body, encoding="utf-8")
    return root


class _TableCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.table = load_canonical_trait_mapping_table(
            _write_resource(
                self.tmp / "table",
                [
                    ("Body mass index (BMI)", "EFO:0004340", "body mass index"),
                    ("weight", "EFO:0004338", "body weight"),
                ],
            )
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def map(self, *, label: str = "", term: str = "", term_label: str = "") -> TraitOntologyMapping:
        return map_trait_ontology(
            SourceTrait(
                analysis_id="GCST1",
                label=label,
                ontology_id=term,
                ontology_label=term_label,
            ),
            self.table,
        )


class PrecedenceTests(_TableCase):
    """Seam 1: source wins; the table fills a gap; no hit leaves it unmapped."""

    def test_methods_are_the_contract_vocabulary(self) -> None:
        self.assertEqual(
            TRAIT_ONTOLOGY_MAPPING_METHODS,
            ("source_provided", "canonical_table_lookup", "unmapped"),
        )

    def test_a_source_term_wins_over_a_table_entry(self) -> None:
        mapping = self.map(label="weight", term="EFO:0009999", term_label="source weight")
        self.assertEqual(
            (mapping.trait_ontology_id, mapping.trait_ontology_label, mapping.method),
            ("EFO:0009999", "source weight", SOURCE_PROVIDED),
        )

    def test_a_table_hit_fills_a_gap(self) -> None:
        # Exact match on the trimmed, lower-cased Source Trait Label.
        mapping = self.map(label="  Body Mass Index (BMI) ")
        self.assertEqual(
            (mapping.trait_ontology_id, mapping.trait_ontology_label, mapping.method),
            ("EFO:0004340", "body mass index", CANONICAL_TABLE_LOOKUP),
        )

    def test_no_hit_leaves_the_analysis_unmapped(self) -> None:
        for label in ("body mass", ""):
            with self.subTest(label=label):
                mapping = self.map(label=label)
                self.assertEqual(
                    (mapping.trait_ontology_id, mapping.trait_ontology_label, mapping.method),
                    ("", "", UNMAPPED),
                )
                self.assertTrue(mapping.note)

    def test_a_source_term_without_a_label_is_kept(self) -> None:
        mapping = self.map(label="weight", term="EFO:0009999")
        self.assertEqual(
            (mapping.trait_ontology_id, mapping.trait_ontology_label, mapping.method),
            ("EFO:0009999", "", SOURCE_PROVIDED),
        )

    def test_a_run_counts_every_method_and_records_the_table(self) -> None:
        run = map_trait_ontologies(
            [
                SourceTrait("GCST1", "weight", "EFO:0009999", "w"),
                SourceTrait("GCST2", "weight", "", ""),
                SourceTrait("GCST3", "height", "", ""),
            ],
            self.table,
        )
        self.assertEqual(
            {key: mapping.method for key, mapping in run.mappings.items()},
            {"GCST1": SOURCE_PROVIDED, "GCST2": CANONICAL_TABLE_LOOKUP, "GCST3": UNMAPPED},
        )
        provenance = run.provenance()
        self.assertEqual(
            provenance["method_counts"],
            {SOURCE_PROVIDED: 1, CANONICAL_TABLE_LOOKUP: 1, UNMAPPED: 1},
        )
        table = provenance["canonical_trait_mapping_table"]
        self.assertEqual(table["resource_id"], "canonical-trait-mapping-efo")
        self.assertEqual(table["version"], 7)
        self.assertEqual(table["rows"], 2)
        self.assertRegex(table["sha256"], r"\A[0-9a-f]{64}\Z")
        self.assertEqual(provenance["rejected_source_terms"], [])

    def test_an_analysis_named_twice_is_refused(self) -> None:
        with self.assertRaisesRegex(TraitOntologyMappingError, "GCST1"):
            map_trait_ontologies(
                [SourceTrait("GCST1", "a", "", ""), SourceTrait("GCST1", "b", "", "")],
                self.table,
            )


class DeterminismAndOfflineTests(_TableCase):
    """Seam 2: same inputs, same output; the lookup reads only the named directory."""

    SOURCES = [
        SourceTrait("GCST1", "weight", "http://www.ebi.ac.uk/efo/EFO_0004338", "body weight"),
        SourceTrait("GCST2", "Body mass index (BMI)", "", ""),
        SourceTrait("GCST3", "unknown trait", "", ""),
    ]

    def test_the_same_inputs_give_the_same_output(self) -> None:
        directory = self.tmp / "table"
        first = map_trait_ontologies(self.SOURCES, load_canonical_trait_mapping_table(directory))
        second = map_trait_ontologies(
            list(reversed(self.SOURCES)), load_canonical_trait_mapping_table(directory)
        )
        self.assertEqual(first.mappings, second.mappings)
        self.assertEqual(first.provenance(), second.provenance())

    def test_loading_and_mapping_open_no_socket(self) -> None:
        def refuse(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("Trait Ontology Mapping opened a socket")

        with mock.patch.object(socket, "socket", side_effect=refuse), mock.patch.object(
            socket, "create_connection", side_effect=refuse
        ):
            table = load_canonical_trait_mapping_table(self.tmp / "table")
            run = map_trait_ontologies(self.SOURCES, table)
        self.assertEqual(run.mappings["GCST2"].method, CANONICAL_TABLE_LOOKUP)

    def test_the_table_is_read_from_the_directory_given(self) -> None:
        other = load_canonical_trait_mapping_table(
            _write_resource(self.tmp / "other", [("weight", "EFO:0000001", "x")], version=9)
        )
        mapping = map_trait_ontology(SourceTrait("GCST1", "weight", "", ""), other)
        self.assertEqual(mapping.trait_ontology_id, "EFO:0000001")
        self.assertEqual(other.version, 9)

    def test_the_tracked_table_loads(self) -> None:
        table = load_canonical_trait_mapping_table(REPO_ROOT / CANONICAL_TRAIT_MAPPING_RESOURCE)
        self.assertEqual(table.resource_id, "canonical-trait-mapping-efo")
        self.assertGreater(len(table.entries), 0)

    def test_a_malformed_table_is_refused(self) -> None:
        cases = {
            "duplicate label": [("weight", "EFO:1", "a"), ("WEIGHT ", "EFO:2", "b")],
            "not a CURIE": [("weight", "http://www.ebi.ac.uk/efo/EFO_1", "a")],
            "blank term": [("weight", "", "a")],
            "blank label": [("", "EFO:1", "a")],
        }
        for name, rows in cases.items():
            with self.subTest(name), self.assertRaises(TraitOntologyMappingError):
                load_canonical_trait_mapping_table(
                    _write_resource(self.tmp / name.replace(" ", "-"), rows)
                )

    def test_a_table_resource_of_another_kind_is_refused(self) -> None:
        directory = _write_resource(self.tmp / "kind", [("weight", "EFO:1", "a")])
        text = (directory / "resource.yaml").read_text(encoding="utf-8")
        (directory / "resource.yaml").write_text(
            text.replace("trait_ontology_mapping", "qc_panel"), encoding="utf-8"
        )
        with self.assertRaisesRegex(TraitOntologyMappingError, "kind"):
            load_canonical_trait_mapping_table(directory)


class NormalisationTests(unittest.TestCase):
    """Seam 3: each URI form becomes a CURIE; a list stays a ", "-joined list."""

    def test_each_uri_form_becomes_a_curie(self) -> None:
        cases = {
            "http://www.ebi.ac.uk/efo/EFO_0004839": "EFO:0004839",
            "http://purl.obolibrary.org/obo/MONDO_0005148": "MONDO:0005148",
            "http://purl.obolibrary.org/obo/OBA_VT0001253": "OBA:VT0001253",
            "http://purl.obolibrary.org/obo/GO_0072718": "GO:0072718",
            "http://www.orpha.net/ORDO/Orphanet_101960": "Orphanet:101960",
            "MONDO:0005150": "MONDO:0005150",
            "Orphanet:101960": "Orphanet:101960",
        }
        for source, curie in cases.items():
            with self.subTest(source=source):
                self.assertEqual(normalise_trait_ontology_id(source, analysis_id="GCST1"), curie)

    def test_a_list_keeps_its_order_and_is_joined_by_comma_space(self) -> None:
        self.assertEqual(
            normalise_trait_ontology_id(
                "MONDO:0005150, http://www.ebi.ac.uk/efo/EFO_1001492, "
                "http://purl.obolibrary.org/obo/MONDO_0005417",
                analysis_id="GCST003219",
            ),
            "MONDO:0005150, EFO:1001492, MONDO:0005417",
        )
        self.assertEqual(
            normalise_trait_ontology_id(
                "http://www.ebi.ac.uk/efo/EFO_0004471,http://www.ebi.ac.uk/efo/EFO_0004340",
                analysis_id="GCST1",
            ),
            "EFO:0004471, EFO:0004340",
        )

    def test_the_label_of_a_list_is_kept_verbatim(self) -> None:
        mapping = map_trait_ontology(
            SourceTrait(
                "GCST005812",
                "Osteoarthritis",
                "MONDO:0006629, http://purl.obolibrary.org/obo/MONDO_0005416",
                "osteoarthritis, hip, osteoarthritis, knee",
            ),
            None,
        )
        self.assertEqual(mapping.trait_ontology_id, "MONDO:0006629, MONDO:0005416")
        self.assertEqual(mapping.trait_ontology_label, "osteoarthritis, hip, osteoarthritis, knee")
        self.assertEqual(mapping.method, SOURCE_PROVIDED)

    def test_any_other_form_is_refused_naming_the_analysis_and_term(self) -> None:
        for term in (
            "https://www.ebi.ac.uk/efo/EFO_0004839",
            "http://www.ebi.ac.uk/efo/MONDO_0005148",
            "http://purl.obolibrary.org/obo/MONDO",
            "http://www.orpha.net/ORDO/Orphanet_",
            "http://example.org/trait/1",
            "EFO_0004839",
            "EFO:0004471, ",
        ):
            with self.subTest(term=term):
                with self.assertRaises(TraitOntologyMappingError) as raised:
                    normalise_trait_ontology_id(term, analysis_id="GCST42")
                self.assertIn("GCST42", str(raised.exception))
                self.assertIn(repr(term), str(raised.exception))


class GeneAndAuthorityGuardTests(_TableCase):
    """Seam 4: a gene-shaped identifier or an authority-name label is never emitted (#141)."""

    def test_a_gene_shaped_source_term_falls_through_to_the_table(self) -> None:
        for term in (
            "ENSEMBL:ENSG00000128245",
            "ENSG00000128245",
            "EFO:0020109, HGNC:7533",
            "UNIPROT:P61981",
        ):
            with self.subTest(term=term):
                hit = self.map(label="weight", term=term, term_label="14-3-3 protein eta")
                self.assertEqual(
                    (hit.trait_ontology_id, hit.method), ("EFO:0004338", CANONICAL_TABLE_LOOKUP)
                )
                self.assertEqual(hit.rejected_source_term, term)
                self.assertIn("gene", hit.note)
                miss = self.map(label="YWHAH", term=term, term_label="14-3-3 protein eta")
                self.assertEqual(
                    (miss.trait_ontology_id, miss.trait_ontology_label, miss.method),
                    ("", "", UNMAPPED),
                )
                self.assertEqual(miss.rejected_source_term, term)

    def test_an_authority_name_source_label_falls_through(self) -> None:
        for term_label in ("Ensembl", "body mass index, UniProt"):
            with self.subTest(term_label=term_label):
                mapping = self.map(label="weight", term="EFO:0009999", term_label=term_label)
                self.assertEqual(
                    (mapping.trait_ontology_id, mapping.method),
                    ("EFO:0004338", CANONICAL_TABLE_LOOKUP),
                )
                self.assertIn("authority", mapping.note)

    def test_a_rejected_source_term_is_recorded_in_provenance(self) -> None:
        run = map_trait_ontologies(
            [SourceTrait("GCST1", "YWHAH", "ENSEMBL:ENSG00000128245", "Ensembl")], self.table
        )
        self.assertEqual(
            run.provenance()["rejected_source_terms"],
            [
                {
                    "analysis_id": "GCST1",
                    "source_trait_ontology_id": "ENSEMBL:ENSG00000128245",
                    "source_trait_ontology_label": "Ensembl",
                    "reason": run.mappings["GCST1"].note,
                }
            ],
        )

    def test_a_table_never_supplies_a_gene_or_an_authority_name(self) -> None:
        for name, row in {
            "gene": ("ywhah", "ENSEMBL:ENSG00000128245", "14-3-3 protein eta"),
            "authority": ("ywhah", "EFO:0020109", "Ensembl"),
        }.items():
            with self.subTest(name), self.assertRaisesRegex(TraitOntologyMappingError, "#141"):
                load_canonical_trait_mapping_table(_write_resource(self.tmp / name, [row]))


if __name__ == "__main__":
    unittest.main()

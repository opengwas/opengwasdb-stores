#!/usr/bin/env python3
"""Canonical ontology identifiers (issue #161).

EFO v3.94.0's OBO spells its native terms ``efo:EFO_...``, already-CURIE
imports as ``MONDO:...``, and gene/dbpedia terms as bare IRIs. Every Release
Manifest and shortlist row uses the canonical CURIE form, so the retrieval index
must normalise before it stores or looks anything up, and every comparison of a
source-provided id against an index id must use the same normaliser.

The suite is hermetic: tiny inline OBO documents, no network.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from curation.ontology import (
    INDEX_FORMAT_VERSION,
    IndexFormatError,
    build_index_from_obo,
    index_from_dict,
    is_curie,
    normalise_ontology_id,
    parse_obo,
)

RELEASE = "efo/v3.94.0"

OBO = """\
format-version: 1.2
ontology: efo

[Term]
id: efo:EFO_0000270
name: asthma
alt_id: efo:EFO_0003939
alt_id: http://purl.obolibrary.org/obo/EFO_0000271
replaced_by: EFO:0000272

[Term]
id: MONDO:0004979
name: asthma
is_obsolete: true

[Term]
id: efo:EFO_0000272
name: obsolete asthma
is_a: efo:EFO_0000270 ! asthma

[Term]
id: http://www.genenames.org/cgi-bin/gene_symbol_report?hgnc_id=HGNC:1
name: a gene
"""


class NormaliseOntologyIdTest(unittest.TestCase):
    def test_lower_prefix_underscore_id(self) -> None:
        self.assertEqual(normalise_ontology_id("efo:EFO_0003939"), "EFO:0003939")

    def test_obo_purl(self) -> None:
        self.assertEqual(
            normalise_ontology_id("http://purl.obolibrary.org/obo/MONDO_0004979"),
            "MONDO:0004979",
        )
        self.assertEqual(
            normalise_ontology_id("http://purl.obolibrary.org/obo/MONDO:0004979"),
            "MONDO:0004979",
        )

    def test_ebi_efo_iri(self) -> None:
        self.assertEqual(
            normalise_ontology_id("http://www.ebi.ac.uk/efo/EFO_0000270"),
            "EFO:0000270",
        )

    def test_orpha_iri(self) -> None:
        self.assertEqual(
            normalise_ontology_id("http://www.orpha.net/ORDO/Orphanet_58"),
            "Orphanet:58",
        )

    def test_already_curie_is_unchanged(self) -> None:
        self.assertEqual(normalise_ontology_id("MONDO:0004979"), "MONDO:0004979")

    def test_unrecognised_iri_is_unchanged_and_not_a_curie(self) -> None:
        for gene in (
            "http://www.genenames.org/cgi-bin/gene_symbol_report?hgnc_id=HGNC:1",
            "http://dbpedia.org/resource/Albania",
            "http://www.ncbi.nlm.nih.gov/gene/1",
        ):
            self.assertEqual(normalise_ontology_id(gene), gene)
            self.assertFalse(is_curie(gene))
        self.assertTrue(is_curie("EFO:0000270"))

    def test_idempotent(self) -> None:
        value = "efo:EFO_0000270"
        self.assertEqual(
            normalise_ontology_id(normalise_ontology_id(value)),
            normalise_ontology_id(value),
        )


class ParseOboNormalisationTest(unittest.TestCase):
    def test_normalises_term_parent_replaced_by_and_alt_ids(self) -> None:
        terms = {term.ontology_id: term for term in parse_obo(OBO)}
        self.assertIn("EFO:0000270", terms)
        self.assertIn("MONDO:0004979", terms)
        asthma = terms["EFO:0000270"]
        self.assertEqual(asthma.alt_ids, ("EFO:0003939", "EFO:0000271"))
        self.assertEqual(asthma.replaced_by, "EFO:0000272")
        child = terms["EFO:0000272"]
        self.assertEqual(child.parent_id, "EFO:0000270")
        # The parent label resolves against the normalised id map.
        self.assertEqual(child.parent_label, "asthma")


class BuildIndexNormalisationTest(unittest.TestCase):
    def test_excludes_non_curie_terms_and_reports_the_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            obo = Path(tmp) / "efo.obo"
            obo.write_text(OBO, encoding="utf-8")
            index = build_index_from_obo(obo, RELEASE)

        self.assertEqual(index.ontology_release, RELEASE)
        self.assertEqual(index.excluded_non_curie_count, 1)
        ids = {term.ontology_id for term in index}
        self.assertIn("EFO:0000270", ids)
        self.assertIn("MONDO:0004979", ids)
        self.assertFalse(any(identifier.startswith("http") for identifier in ids))

    def test_index_round_trips_the_excluded_count(self) -> None:
        data = {
            "index_format_version": INDEX_FORMAT_VERSION,
            "ontology_release": RELEASE,
            "excluded_non_curie_count": 7,
            "terms": [],
        }
        index = index_from_dict(data)
        self.assertEqual(index.excluded_non_curie_count, 7)

    def test_version_one_index_is_refused(self) -> None:
        data = {
            "index_format_version": 1,
            "ontology_release": RELEASE,
            "terms": [],
        }
        with self.assertRaises(IndexFormatError):
            index_from_dict(data)


class EquivalentIdsTest(unittest.TestCase):
    """Replacement and alias closure for scoring a source id."""

    def setUp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            obo = Path(tmp) / "efo.obo"
            obo.write_text(OBO, encoding="utf-8")
            self.index = build_index_from_obo(obo, RELEASE)

    def test_term_aliases_and_replacement_are_equivalent(self) -> None:
        self.assertEqual(
            set(self.index.equivalent_ids("EFO:0000270")),
            {"EFO:0003939", "EFO:0000271", "EFO:0000272"},
        )

    def test_alt_id_resolves_to_its_owning_term(self) -> None:
        self.assertIn("EFO:0000270", self.index.equivalent_ids("EFO:0003939"))

    def test_unknown_or_unreplaced_id_has_no_equivalents(self) -> None:
        self.assertEqual(self.index.equivalent_ids("MONDO:0004979"), ())
        self.assertEqual(self.index.equivalent_ids("EFO:9999999"), ())


if __name__ == "__main__":
    unittest.main()

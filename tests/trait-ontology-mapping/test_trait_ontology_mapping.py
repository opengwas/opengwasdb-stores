#!/usr/bin/env python3
"""Trait Ontology Mapping: the shared step after verification (#286, ADR 0036 step 5).

A source-provided term from the Source Metadata Table is kept; otherwise the
Source Trait Label is looked up, offline, in the Canonical Trait Mapping Table;
otherwise the Analysis is left unmapped. The method is recorded every time.
Source terms are normalised to CURIEs term by term; a gene-shaped identifier or
an authority-name label is never emitted (#141).

Run from the repository root:
    python3 tests/trait-ontology-mapping/test_trait_ontology_mapping.py
"""

from __future__ import annotations

import ast
import json
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import manifest_generation.trait_ontology_mapping as trait_ontology_mapping
from manifest_generation import REPO_ROOT
from manifest_generation.source_metadata_table import load_source_metadata_table
from manifest_generation.trait_ontology_mapping import (
    CANONICAL_TRAIT_MAPPING_RESOURCE,
    CanonicalTraitTerm,
    SourceTrait,
    TraitOntologyMapping,
    TraitOntologyMappingError,
    TraitOntologyMappingMethod,
    load_canonical_trait_mapping_table,
    map_trait_ontologies,
    map_trait_ontology,
    normalise_trait_ontology_id,
    source_traits_from_source_metadata_table,
)

SOURCE_PROVIDED = TraitOntologyMappingMethod.SOURCE_PROVIDED
CANONICAL_TABLE_LOOKUP = TraitOntologyMappingMethod.CANONICAL_TABLE_LOOKUP
UNMAPPED = TraitOntologyMappingMethod.UNMAPPED

TABLE_HEADER = (
    "trait_label\ttrait_ontology_id\ttrait_ontology_label\tontology_release\t"
    "chooser_id\tconfidence\trunner_up_margin\treview_status\treviewer\treviewed_at\n"
)
TABLE_ROWS = [
    ("Body mass index (BMI)", "EFO:0004340", "body mass index"),
    ("weight", "EFO:0004338", "body weight"),
]
#: sha256 of the mapping.tsv _write_resource writes for TABLE_ROWS.
TABLE_ROWS_SHA256 = "d3f48cea6918e9cbd46db78f5b85bf7e591efd9b778417cddd20e4200dae773f"


def _write_resource(
    tmp: Path,
    rows: list[tuple[str, str, str]],
    *,
    name: str = "canonical-trait-mapping-efo",
    version: int = 7,
    location: str | None = None,
    location_kind: str | None = "tracked_file",
) -> Path:
    """A Canonical Trait Mapping Table Reference Resource, laid out as in the repository."""
    root = tmp / "resources" / "reference-resources" / name
    root.mkdir(parents=True, exist_ok=True)
    declared = location or f"resources/reference-resources/{name}/mapping.tsv"
    (root / "resource.yaml").write_text(
        f"resource_id: {name}\n"
        "kind: trait_ontology_mapping\n"
        f"version: {version}\n"
        f"location: {declared}\n"
        + (f"location_kind: {location_kind}\n" if location_kind else ""),
        encoding="utf-8",
    )
    body = "".join(f"{label}\t{curie}\t{term}\t\t\t\t\t\t\t\n" for label, curie, term in rows)
    (root / "mapping.tsv").write_text(TABLE_HEADER + body, encoding="utf-8")
    return root


class _TableCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.table = load_canonical_trait_mapping_table(_write_resource(self.tmp, TABLE_ROWS))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def map(self, *, label: str = "", term: str = "", term_label: str = "") -> TraitOntologyMapping:
        return map_trait_ontology(
            SourceTrait(analysis_id="GCST1", label=label, ontology_id=term, ontology_label=term_label),
            self.table,
        )


class PrecedenceTests(_TableCase):
    """Seam 1: source wins; the table fills a gap; no hit leaves it unmapped."""

    def test_methods_are_the_contract_vocabulary(self) -> None:
        self.assertEqual(
            [method.value for method in TraitOntologyMappingMethod],
            ["source_provided", "canonical_table_lookup", "unmapped"],
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
        for label, note in (
            (
                "body mass",
                "no acceptable source term; Source Trait Label 'body mass' is not in "
                "the Canonical Trait Mapping Table",
            ),
            ("", "no acceptable source term; no Source Trait Label to look up"),
        ):
            with self.subTest(label=label):
                mapping = self.map(label=label)
                self.assertEqual(
                    (mapping.trait_ontology_id, mapping.trait_ontology_label, mapping.method),
                    ("", "", UNMAPPED),
                )
                self.assertEqual(mapping.note, note)

    def test_a_source_term_without_a_label_is_kept_and_counted(self) -> None:
        mapping = self.map(label="weight", term="EFO:0009999")
        self.assertEqual(
            (mapping.trait_ontology_id, mapping.trait_ontology_label, mapping.method),
            ("EFO:0009999", "", SOURCE_PROVIDED),
        )
        run = map_trait_ontologies(
            [SourceTrait("GCST1", "weight", "EFO:0009999", ""),
             SourceTrait("GCST2", "weight", "EFO:0009998", "w")],
            self.table,
        )
        self.assertEqual(run.provenance()["source_id_without_label"], 1)

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
        self.assertEqual(
            run.provenance(),
            {
                "canonical_trait_mapping_table": {
                    "resource_id": "canonical-trait-mapping-efo",
                    "version": 7,
                    "location": "resources/reference-resources/canonical-trait-mapping-efo/mapping.tsv",
                    "sha256": TABLE_ROWS_SHA256,
                    "rows": 2,
                },
                "method_counts": {
                    "source_provided": 1,
                    "canonical_table_lookup": 1,
                    "unmapped": 1,
                },
                "source_id_without_label": 0,
                "rejected_source_terms": [],
            },
        )
        # The provenance is plain data, written into validation.yaml.
        self.assertEqual(json.loads(json.dumps(run.provenance())), run.provenance())
        self.assertIs(type(next(iter(run.provenance()["method_counts"]))), str)

    def test_an_analysis_named_twice_is_refused(self) -> None:
        with self.assertRaisesRegex(TraitOntologyMappingError, "GCST1"):
            map_trait_ontologies(
                [SourceTrait("GCST1", "a", "", ""), SourceTrait("GCST1", "b", "", "")],
                self.table,
            )


class SourceMetadataTableTests(_TableCase):
    """The step's input is the Source Metadata Table's trait columns (#283)."""

    def write_table(self, rows: list[tuple[str, ...]]) -> Path:
        header = (
            "source_analysis_id\tsource_trait_label\tsource_trait_ontology_id\t"
            "source_trait_ontology_label\n"
        )
        path = self.tmp / "source-metadata.tsv"
        path.write_text(header + "".join("\t".join(row) + "\n" for row in rows), encoding="utf-8")
        return path

    def test_source_traits_are_built_from_the_source_metadata_table(self) -> None:
        table = load_source_metadata_table(
            self.write_table(
                [
                    ("GCST1", "Height", "http://www.ebi.ac.uk/efo/EFO_0004339", "body height"),
                    ("GCST2", "weight", "", ""),
                    ("GCST3", "Gout", "", ""),
                    ("GCST9", "Lipids", "EFO:0004529", "lipid measurement"),
                ]
            )
        )
        sources = source_traits_from_source_metadata_table(table, ["GCST3", "GCST1", "GCST2"])
        self.assertEqual(
            sources,
            [
                SourceTrait("GCST1", "Height", "http://www.ebi.ac.uk/efo/EFO_0004339", "body height"),
                SourceTrait("GCST2", "weight", "", ""),
                SourceTrait("GCST3", "Gout", "", ""),
            ],
        )
        run = map_trait_ontologies(sources, self.table)
        self.assertEqual(
            {key: (m.trait_ontology_id, m.method) for key, m in run.mappings.items()},
            {
                "GCST1": ("EFO:0004339", SOURCE_PROVIDED),
                "GCST2": ("EFO:0004338", CANONICAL_TABLE_LOOKUP),
                "GCST3": ("", UNMAPPED),
            },
        )

    def test_a_table_without_trait_columns_gives_blank_sources(self) -> None:
        path = self.tmp / "ids-only.tsv"
        path.write_text("source_analysis_id\nGCST1\n", encoding="utf-8")
        sources = source_traits_from_source_metadata_table(load_source_metadata_table(path), ["GCST1"])
        self.assertEqual(sources, [SourceTrait("GCST1", "", "", "")])

    def test_an_analysis_the_table_does_not_intend_is_refused(self) -> None:
        table = load_source_metadata_table(self.write_table([("GCST1", "Height", "", "")]))
        with self.assertRaisesRegex(TraitOntologyMappingError, "GCST2"):
            source_traits_from_source_metadata_table(table, ["GCST1", "GCST2"])


class DeterminismAndOfflineTests(_TableCase):
    """Seam 2: same inputs, same output; the lookup reads only the named directory."""

    SOURCES = [
        SourceTrait("GCST1", "weight", "http://www.ebi.ac.uk/efo/EFO_0004338", "body weight"),
        SourceTrait("GCST2", "Body mass index (BMI)", "", ""),
        SourceTrait("GCST3", "unknown trait", "", ""),
    ]

    def resource_dir(self) -> Path:
        return self.tmp / "resources" / "reference-resources" / "canonical-trait-mapping-efo"

    def test_the_same_inputs_give_the_same_output(self) -> None:
        first = map_trait_ontologies(self.SOURCES, load_canonical_trait_mapping_table(self.resource_dir()))
        second = map_trait_ontologies(
            list(reversed(self.SOURCES)), load_canonical_trait_mapping_table(self.resource_dir())
        )
        self.assertEqual(first.mappings, second.mappings)
        self.assertEqual(first.provenance(), second.provenance())

    def test_loading_and_mapping_open_no_socket(self) -> None:
        def refuse(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("Trait Ontology Mapping opened a socket")

        with mock.patch.object(socket, "socket", side_effect=refuse), mock.patch.object(
            socket, "create_connection", side_effect=refuse
        ):
            table = load_canonical_trait_mapping_table(self.resource_dir())
            run = map_trait_ontologies(self.SOURCES, table)
        self.assertEqual(run.mappings["GCST2"].method, CANONICAL_TABLE_LOOKUP)

    def test_the_module_imports_no_network_module(self) -> None:
        network = {"socket", "ssl", "urllib", "http", "requests", "urllib3", "ftplib"}
        # Its own imports...
        tree = ast.parse(Path(trait_ontology_mapping.__file__).read_text(encoding="utf-8"))
        imported = {
            alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module.split(".")[0] for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        self.assertEqual(imported & network, set())
        # ...and everything importing it loads, in a fresh interpreter.
        probe = (
            "import sys\n"
            "before = set(sys.modules)\n"
            "import manifest_generation.trait_ontology_mapping\n"
            f"network = {sorted(network)!r}\n"
            "print(sorted(m for m in set(sys.modules) - before if m.split('.')[0] in network))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, check=True
        )
        self.assertEqual(result.stdout.strip(), "[]")

    def test_the_table_is_read_from_the_directory_given(self) -> None:
        other = load_canonical_trait_mapping_table(
            _write_resource(self.tmp, [("weight", "EFO:0000001", "x")], name="other", version=9)
        )
        mapping = map_trait_ontology(SourceTrait("GCST1", "weight", "", ""), other)
        self.assertEqual(mapping.trait_ontology_id, "EFO:0000001")
        self.assertEqual(other.version, 9)
        self.assertEqual(other.entries["weight"], CanonicalTraitTerm("EFO:0000001", "x"))

    def test_the_tracked_table_loads(self) -> None:
        table = load_canonical_trait_mapping_table(REPO_ROOT / CANONICAL_TRAIT_MAPPING_RESOURCE)
        self.assertEqual(table.resource_id, "canonical-trait-mapping-efo")
        self.assertGreater(len(table.entries), 0)

    def test_a_malformed_table_is_refused(self) -> None:
        cases = {
            "duplicate-label": [("weight", "EFO:1", "a"), ("WEIGHT ", "EFO:2", "b")],
            "not-a-curie": [("weight", "http://www.ebi.ac.uk/efo/EFO_1", "a")],
            "blank-term": [("weight", "", "a")],
            "blank-label": [("", "EFO:1", "a")],
        }
        for name, rows in cases.items():
            with self.subTest(name), self.assertRaises(TraitOntologyMappingError):
                load_canonical_trait_mapping_table(_write_resource(self.tmp, rows, name=name))

    def test_a_refusal_says_how_many_problems_it_omits(self) -> None:
        rows = [(f"label {n}", f"http://x/{n}", "a") for n in range(25)]
        with self.assertRaises(TraitOntologyMappingError) as raised:
            load_canonical_trait_mapping_table(_write_resource(self.tmp, rows, name="many"))
        self.assertIn("(25 problem(s))", str(raised.exception))
        self.assertIn("... and 5 more", str(raised.exception))

    def test_the_declared_location_must_be_this_resource_directory(self) -> None:
        directory = _write_resource(
            self.tmp, TABLE_ROWS, name="misplaced",
            location="resources/reference-resources/canonical-trait-mapping-efo/mapping.tsv",
        )
        with self.assertRaisesRegex(TraitOntologyMappingError, "location"):
            load_canonical_trait_mapping_table(directory)

    def test_only_a_tracked_file_location_is_read(self) -> None:
        directory = _write_resource(self.tmp, TABLE_ROWS, name="external", location_kind="external_file")
        with self.assertRaisesRegex(TraitOntologyMappingError, "location_kind"):
            load_canonical_trait_mapping_table(directory)
        undeclared = _write_resource(self.tmp, TABLE_ROWS, name="undeclared", location_kind=None)
        self.assertEqual(len(load_canonical_trait_mapping_table(undeclared).entries), 2)

    def test_a_table_resource_of_another_kind_is_refused(self) -> None:
        directory = _write_resource(self.tmp, [("weight", "EFO:1", "a")], name="kind")
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
        with tempfile.TemporaryDirectory() as tmp:
            table = load_canonical_trait_mapping_table(_write_resource(Path(tmp), TABLE_ROWS))
        mapping = map_trait_ontology(
            SourceTrait(
                "GCST005812",
                "Osteoarthritis",
                "MONDO:0006629, http://purl.obolibrary.org/obo/MONDO_0005416",
                "osteoarthritis, hip, osteoarthritis, knee",
            ),
            table,
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

    GENE_NOTE = "a gene or protein identifier is not a Trait Ontology Mapping (#141)"

    def test_a_gene_shaped_source_term_falls_through_to_the_table(self) -> None:
        for term in (
            "ENSEMBL:ENSG00000128245",
            "ENSG00000128245",
            "EFO:0020109, HGNC:7533",
            "UNIPROT:P61981",
            # Gene-shaped only once normalised: the guard runs on the CURIE too.
            "http://purl.obolibrary.org/obo/HGNC_1234",
            "http://purl.obolibrary.org/obo/NCBIGene_7533",
            "http://purl.obolibrary.org/obo/ENSEMBL_ENSG00000128245",
            "EFO:0020109, http://purl.obolibrary.org/obo/HGNC_1234",
        ):
            with self.subTest(term=term):
                hit = self.map(label="weight", term=term, term_label="14-3-3 protein eta")
                self.assertEqual(
                    (hit.trait_ontology_id, hit.method), ("EFO:0004338", CANONICAL_TABLE_LOOKUP)
                )
                self.assertEqual(hit.rejected_source_term, term)
                self.assertEqual(hit.note, f"source term {term!r} rejected: {self.GENE_NOTE}")
                miss = self.map(label="YWHAH", term=term, term_label="14-3-3 protein eta")
                self.assertEqual(
                    (miss.trait_ontology_id, miss.trait_ontology_label, miss.method),
                    ("", "", UNMAPPED),
                )
                self.assertEqual(miss.rejected_source_term, term)

    def test_an_authority_name_source_label_falls_through(self) -> None:
        mapping = self.map(label="weight", term="EFO:0009999", term_label="Ensembl")
        self.assertEqual(
            (mapping.trait_ontology_id, mapping.method), ("EFO:0004338", CANONICAL_TABLE_LOOKUP)
        )
        self.assertEqual(
            mapping.note,
            "source term 'EFO:0009999' rejected: its label 'Ensembl' names an identifier "
            "authority, not a Trait (#141)",
        )

    def test_an_authority_name_inside_a_longer_label_is_a_trait_label(self) -> None:
        # The label is checked whole, as #141 defines it: a label is never split.
        mapping = self.map(label="weight", term="EFO:0009999", term_label="Ensembl gene count")
        self.assertEqual(mapping.method, SOURCE_PROVIDED)

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
                    "reason": (
                        "source term 'ENSEMBL:ENSG00000128245' rejected: "
                        f"{self.GENE_NOTE}; Source Trait Label 'YWHAH' is not in the "
                        "Canonical Trait Mapping Table"
                    ),
                }
            ],
        )

    def test_a_table_never_supplies_a_gene_or_an_authority_name(self) -> None:
        for name, row in {
            "gene": ("ywhah", "ENSEMBL:ENSG00000128245", "14-3-3 protein eta"),
            "purl-gene": ("ywhah", "HGNC:1234", "14-3-3 protein eta"),
            "authority": ("ywhah", "EFO:0020109", "Ensembl"),
        }.items():
            with self.subTest(name), self.assertRaisesRegex(TraitOntologyMappingError, "#141"):
                load_canonical_trait_mapping_table(_write_resource(self.tmp, [row], name=name))


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""End-to-end pipeline test over hermetic fixtures (issue #168).

This suite wires the whole Canonical Trait Mapping Table curation pipeline
together over fixtures and proves the *round trip*: an unmapped Trait becomes a
work-queue entry, the work queue becomes a candidate shortlist, the shortlist
becomes a proposal, the proposal is promoted into a Canonical Trait Mapping
Table, and the pipeline's own Trait Ontology Mapping then maps that trait as
``canonical_table_lookup``.

Pipeline under test::

    analyses.tsv
      -> curation.gap_scan       (unmapped Trait work queue)
      -> curation.candidates     (multi-channel shortlist)
      -> curation.choice         (proposal, stub chooser)
      -> curation.promotion      (Canonical Trait Mapping Table row)
      -> manifest_generation.trait_ontology_mapping  (the mapping step)

Everything is hermetic and network-free: the ontology is a tiny in-memory OBO
document, the chooser is the fixture-backed stub chooser, and every stage runs
in this process -- there is no subprocess at all.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from curation import candidates, choice, gap_scan, promotion
from curation.choice import PROPOSAL_COLUMNS
from curation.gap_scan import OUTPUT_COLUMNS as QUEUE_COLUMNS
from curation.ontology import build_index_from_obo, write_index
from curation.promotion import MAPPING_COLUMNS
from manifest_generation.trait_ontology_mapping import (
    SourceTrait,
    TraitOntologyMappingMethod,
    load_canonical_trait_mapping_table,
    map_trait_ontology,
)

RELEASE = "efo/v3.94.0"

# A deliberately tiny ontology: the correct term and a near neighbour that the
# token-overlap channel also retrieves, so the shortlist is not a foregone
# single-candidate conclusion.
FIXTURE_OBO = """\
format-version: 1.2
ontology: efo

[Term]
id: EFO:0004340
name: body mass index
def: "A measurement of body mass index." [PMID:123]
is_a: EFO:0004338 ! body weights and measures
synonym: "BMI" EXACT []

[Term]
id: EFO:0004338
name: body weights and measures
def: "Any measurement of body weight." []
is_a: EFO:0004324 ! measurement
"""

TRAIT_LABEL = "Body mass index"
NORMALISED_LABEL = "body mass index"
CORRECT_ID = "EFO:0004340"
CORRECT_LABEL = "body mass index"

RESOURCE_YAML = """\
resource_id: canonical-trait-mapping-efo
label: Canonical trait label to ontology-term mapping table
kind: trait_ontology_mapping
version: {version}
location: resources/reference-resources/canonical-trait-mapping-efo/mapping.tsv
location_kind: tracked_file
description: >
  End-to-end fixture resource.
status: available
"""


def write_table(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["\t".join(columns)]
    lines.extend("\t".join(row.get(column, "") for column in columns) for row in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_tsv(text: str) -> tuple[list[str], list[dict[str, str]]]:
    lines = text.splitlines()
    header = lines[0].split("\t") if lines else []
    rows = [dict(zip(header, line.split("\t"))) for line in lines[1:] if line]
    return header, rows


class EndToEndPipelineTest(unittest.TestCase):
    """The whole pipeline, wired end to end over hermetic fixtures."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)

        # The Release Manifest fixture: one unmapped Trait, attributed to the
        # ragged GWAS-SSF family via a sibling release.yaml.
        self.bundle = self.base / "bundle"
        self.bundle.mkdir()
        self.manifest = self.bundle / "analyses.tsv"
        write_table(
            self.manifest,
            ["source_label", "trait_ontology_mapping_method"],
            [{"source_label": TRAIT_LABEL, "trait_ontology_mapping_method": "unmapped"}],
        )
        (self.bundle / "release.yaml").write_text(
            "store_family_id: gwas-ssf-ragged\n", encoding="utf-8"
        )

        # The pinned ontology release's retrieval index.
        self.obo = self.base / "efo.obo"
        self.obo.write_text(FIXTURE_OBO, encoding="utf-8")
        self.index = self.base / "efo.index.json"
        write_index(build_index_from_obo(self.obo, RELEASE), self.index)

        # The recorded stub-chooser decision, keyed by the normalised queue label.
        self.fixture = self.base / "chooser-fixture.json"
        self.fixture.write_text(
            json.dumps(
                {
                    NORMALISED_LABEL: {
                        "selected_ontology_id": CORRECT_ID,
                        "probabilities": {CORRECT_ID: 1.0},
                    }
                }
            ),
            encoding="utf-8",
        )

        # The Reference Resource promotion writes into. The pipeline resolves
        # the declared `location` against this directory's trailing path, so it
        # is laid out as in the repository: resources/reference-resources/<id>.
        self.resource_dir = self.base / "resources" / "reference-resources" / "canonical-trait-mapping-efo"
        self.resource_dir.mkdir(parents=True)
        self.mapping_path = self.resource_dir / "mapping.tsv"
        write_table(self.mapping_path, list(MAPPING_COLUMNS), [])
        (self.resource_dir / "resource.yaml").write_text(
            RESOURCE_YAML.format(version=1), encoding="utf-8"
        )

        # Pipeline artifacts.
        self.queue = self.base / "queue.tsv"
        self.shortlists = self.base / "shortlists.tsv"
        self.proposals = self.base / "proposals.tsv"
        self.review_queue = self.base / "review-queue.tsv"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # -- helpers ---------------------------------------------------------

    def run_cli(self, func, argv: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = func(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def run_pipeline(self) -> None:
        """Run gap_scan -> candidates -> choice -> promotion over the fixtures."""
        code, _, err = self.run_cli(
            gap_scan.main, [str(self.bundle), "--output", str(self.queue)]
        )
        self.assertEqual(code, 0, f"gap_scan failed: {err}")

        code, _, err = self.run_cli(
            candidates.main,
            [
                "--work-queue", str(self.queue),
                "--index", str(self.index),
                "--shortlist-size", "5",
                "--output", str(self.shortlists),
            ],
        )
        self.assertEqual(code, 0, f"candidates failed: {err}")

        code, _, err = self.run_cli(
            choice.main,
            [
                "--shortlists", str(self.shortlists),
                "--chooser", "stub",
                "--fixture", str(self.fixture),
                "--output", str(self.proposals),
            ],
        )
        self.assertEqual(code, 0, f"choice failed: {err}")

        code, _, err = self.run_cli(
            promotion.main,
            [
                "--proposals", str(self.proposals),
                "--review-queue", str(self.review_queue),
                "--shortlists", str(self.shortlists),
                "--resource-dir", str(self.resource_dir),
                "--as-of", "2026-01-02",
            ],
        )
        self.assertEqual(code, 0, f"promotion failed: {err}")

    # -- stage-level assertions -----------------------------------------

    def test_gap_scan_queues_the_unmapped_trait(self) -> None:
        code, _, err = self.run_cli(
            gap_scan.main, [str(self.bundle), "--output", str(self.queue)]
        )
        self.assertEqual(code, 0, err)
        header, rows = parse_tsv(self.queue.read_text(encoding="utf-8"))
        self.assertEqual(header, list(QUEUE_COLUMNS))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trait_label"], NORMALISED_LABEL)
        self.assertEqual(rows[0]["store_families"], "gwas-ssf-ragged")

    def test_candidates_shortlist_retrieves_the_correct_term(self) -> None:
        self.run_cli(gap_scan.main, [str(self.bundle), "--output", str(self.queue)])
        code, _, err = self.run_cli(
            candidates.main,
            [
                "--work-queue", str(self.queue),
                "--index", str(self.index),
                "--shortlist-size", "5",
                "--output", str(self.shortlists),
            ],
        )
        self.assertEqual(code, 0, err)
        header, rows = parse_tsv(self.shortlists.read_text(encoding="utf-8"))
        self.assertEqual(header, list(candidates.SHORTLIST_COLUMNS))
        ids = [row["ontology_id"] for row in rows]
        self.assertIn(CORRECT_ID, ids)
        # More than one candidate, so the choice is non-trivial.
        self.assertGreater(len(rows), 1)
        self.assertTrue(all(row["trait_label"] == NORMALISED_LABEL for row in rows))

    def test_choice_emits_a_proposal_for_the_correct_term(self) -> None:
        self.run_pipeline()
        header, rows = parse_tsv(self.proposals.read_text(encoding="utf-8"))
        self.assertEqual(header, list(PROPOSAL_COLUMNS))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["selected_ontology_id"], CORRECT_ID)
        self.assertEqual(rows[0]["selected_ontology_label"], CORRECT_LABEL)
        self.assertEqual(rows[0]["confidence"], "1.000000")

    def test_promotion_appends_the_canonical_row_and_bumps_version(self) -> None:
        self.run_pipeline()
        header, rows = parse_tsv(self.mapping_path.read_text(encoding="utf-8"))
        self.assertEqual(header, list(MAPPING_COLUMNS))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trait_label"], NORMALISED_LABEL)
        self.assertEqual(rows[0]["trait_ontology_id"], CORRECT_ID)
        self.assertEqual(rows[0]["trait_ontology_label"], CORRECT_LABEL)
        self.assertEqual(rows[0]["review_status"], promotion.AUTO_ACCEPTED)
        self.assertIn("version: 2", (self.resource_dir / "resource.yaml").read_text())

    # -- the decisive round trip ----------------------------------------

    def test_round_trip_maps_through_the_pipeline(self) -> None:
        """The promoted row is what the mapping step itself finds (#350)."""
        self.run_pipeline()

        table = load_canonical_trait_mapping_table(self.resource_dir)
        mapping = map_trait_ontology(SourceTrait("GCST1", TRAIT_LABEL, "", ""), table)
        self.assertIs(mapping.method, TraitOntologyMappingMethod.CANONICAL_TABLE_LOOKUP)
        self.assertEqual(mapping.trait_ontology_id, CORRECT_ID)
        self.assertEqual(mapping.trait_ontology_label, CORRECT_LABEL)

    def test_round_trip_is_network_free(self) -> None:
        """The pipeline must not require an embedding endpoint or a Jev call."""
        import os

        saved = {key: os.environ.pop(key) for key in list(os.environ) if key.startswith("OPENGWASDB_")}
        try:
            self.run_pipeline()
        finally:
            os.environ.update(saved)
        _, rows = parse_tsv(self.mapping_path.read_text(encoding="utf-8"))
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()

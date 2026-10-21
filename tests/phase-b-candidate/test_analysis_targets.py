#!/usr/bin/env python3
"""Target annotation renders from the Source Metadata Table (#300).

An Analysis of one molecular target (Sun 2018's SomaScan proteins) carries its
target in the Source Metadata Table's target columns. The candidate renders
them into ``analyses.tsv`` -- ``trait_chr``/``trait_bp`` from the target span,
``mhc``, ``target_resolution_method``, ``n_target_rows``, and the Analysis
Label: the gene symbol for a single target, the assay identifier for an
aggregate assay (issue #141) -- and lists every targeted Analysis in
``sidecars/analysis_targets.tsv``. ``tissue`` and ``context`` are the Analysis's
own, else the release config's defaults. An Analysis with no target leaves the
columns blank and has no sidecar row; a release with no target has neither the
columns nor the sidecar, and one with no tissue or context has no such columns.

The expected rows are written out by hand from OGS-00007's.

Run from the repository root:
    python3 tests/phase-b-candidate/test_analysis_targets.py
"""

from __future__ import annotations

import csv
import io
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from manifest_generation.candidate_workflow import (
    apply_release_policy,
    build_candidate_tables,
    load_candidate_configuration,
)

from test_phase_b_candidate import (  # sibling suite's fixture helpers
    REPO_ROOT,
    FixtureBuilder,
    _synthetic_analysis_metadata,
    _synthetic_inventory_row,
    _synthetic_resolver_record,
)

TARGETS = {
    "GCST005806": dict(
        source_label="Pyruvate dehydrogenase kinase isozyme 1 levels (PDK1.5227.60.3)",
        target_assay_id="5227-60", target_symbol="PDK1", target_ensembl_gene_id="ENSG00000152256",
        target_chromosome="2", target_start="172555373", target_end="172628598",
        target_mhc="false", target_is_aggregate="false",
        target_resolution_method="somascan_db_seqid", target_rows="1",
    ),
    # One SeqId for the seven-member 14-3-3 family; SomaScan.db maps it to YWHAH.
    "GCST90240123": dict(
        source_label="14-3-3 protein family levels (YWHAB.YWHAE.YWHAG.YWHAH.YWHAQ.YWHAZ.SFN.4707.50.2)",
        target_assay_id="4707-50", target_symbol="YWHAH", target_ensembl_gene_id="ENSG00000128245",
        target_chromosome="22", target_start="31944465", target_end="31957603",
        target_mhc="false", target_is_aggregate="true",
        target_resolution_method="somascan_db_seqid", target_rows="2", context="SomaScan v3",
    ),
    "GCST90199621": dict(source_label="Carnitine levels"),
}


class AnalysisTargetsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="analysis-targets-")
        config = load_candidate_configuration(FixtureBuilder(Path(self._tmp.name)).build().config_path, REPO_ROOT)
        self.config = replace(config, defaults={**config.defaults, "tissue": "plasma", "context": "SomaScan"})

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def render(self, targets=TARGETS, store_ids=None):
        rows, metadata, records = [], {}, {}
        for analysis_id, fields in targets.items():
            rows.append(replace(_synthetic_inventory_row(), analysis_id=analysis_id))
            metadata[analysis_id] = replace(
                _synthetic_analysis_metadata(),
                analysis_id=analysis_id,
                store_analysis_id=(store_ids or {}).get(analysis_id, ""),
                **fields,
            )
            records[analysis_id] = dict(_synthetic_resolver_record(), analysis_id=analysis_id)
        outcomes = apply_release_policy(rows, self.config, metadata, records)
        return build_candidate_tables(
            inventory_rows=rows, outcomes=outcomes, config=self.config, index_summary={}
        )

    def test_target_columns_render_into_analyses_tsv(self) -> None:
        analyses = list(csv.DictReader(io.StringIO(self.render().analyses_tsv), delimiter="\t"))
        columns = ("analysis_id", "analysis_label", "trait_chr", "trait_bp", "tissue", "context",
                   "mhc", "target_resolution_method", "n_target_rows")
        self.assertEqual(
            [tuple(row[column] for column in columns) for row in analyses],
            [
                ("GCST005806", "PDK1", "2", "172555373", "plasma", "SomaScan", "false",
                 "somascan_db_seqid", "1"),
                ("GCST90240123", "4707-50", "22", "31944465", "plasma", "SomaScan v3", "false",
                 "somascan_db_seqid", "2"),
                ("GCST90199621", "Carnitine levels", "", "", "plasma", "SomaScan", "", "", ""),
            ],
        )

    def test_a_release_without_targets_omits_the_target_columns_entirely(self) -> None:
        """docs/release-metadata-schema.md: absent columns, not blank ones, mark a release with no target.

        Chen 2023 has no target but declares its tissue and context; OGS-00011
        has neither.
        """
        untargeted = {"GCST90199621": TARGETS["GCST90199621"]}
        header = self.render(untargeted).analyses_tsv.splitlines()[0].split("\t")
        self.assertEqual(header[-3:], ["exclude_from_build", "tissue", "context"])
        self.config = replace(self.config, defaults={
            key: value for key, value in self.config.defaults.items() if key not in ("tissue", "context")
        })
        tables = self.render(untargeted)
        self.assertEqual(tables.analyses_tsv.splitlines()[0].split("\t")[-1], "exclude_from_build")
        self.assertEqual(tables.analysis_targets_tsv, "")

    def test_every_targeted_analysis_has_a_row_in_the_target_sidecar(self) -> None:
        self.assertEqual(
            self.render().analysis_targets_tsv,
            "analysis_id\ttarget_assay_id\ttarget_symbol\ttarget_ensembl_gene_id\ttarget_chromosome"
            "\ttarget_start\ttarget_end\ttarget_mhc\ttarget_is_aggregate\ttarget_resolution_method"
            "\ttarget_rows\n"
            "GCST005806\t5227-60\tPDK1\tENSG00000152256\t2\t172555373\t172628598\tfalse\tfalse"
            "\tsomascan_db_seqid\t1\n"
            "GCST90240123\t4707-50\tYWHAH\tENSG00000128245\t22\t31944465\t31957603\tfalse\ttrue"
            "\tsomascan_db_seqid\t2\n",
        )

    def test_the_target_sidecar_is_keyed_by_the_stores_analysis_id(self) -> None:
        """It joins to ``analyses.tsv``, so it names the Store's Analysis (#288).

        A Source Collection that declares its own ``analysis_id`` -- FinnGen
        namespaces every phenocode -- must find its targets under that id; keying
        the sidecar by ``source_analysis_id`` would leave it unable to join.
        """
        tables = self.render(
            {"GCST005806": TARGETS["GCST005806"]},
            store_ids={"GCST005806": "fixture-00001-GCST005806"},
        )
        analyses = list(csv.DictReader(io.StringIO(tables.analyses_tsv), delimiter="\t"))
        sidecar = list(csv.DictReader(io.StringIO(tables.analysis_targets_tsv), delimiter="\t"))
        self.assertEqual([row["source_analysis_id"] for row in analyses], ["GCST005806"])
        self.assertEqual([row["analysis_id"] for row in analyses], ["fixture-00001-GCST005806"])
        self.assertEqual(
            [row["analysis_id"] for row in sidecar], [row["analysis_id"] for row in analyses]
        )


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""A region-filtered release reads only files filtered to its own policy, and carries their evidence (#300).

Issue #300: ``sparse_regions.tsv`` and ``filter_summary.tsv`` are carried as
today. Acquisition records them beside the Acquisition Manifest and checksums
them in its run record (``<manifest-stem>.meta.yaml``), which freeze checksums
into the inventory's provenance. The candidate carries the Selection's rows of
each, verbatim and in file order, into ``sidecars/``, after checking both
checksums; a file changed since is refused. A release whose acquisition
recorded none carries neither.

The #282 final review: ADR 0036 filters a release's files to *its* region
policy, so preflight, and rendering however the stages are run, refuse a
frozen acquisition that is not the release config's -- a policy declared over
unfiltered files (the reviewer's reproduction), a changed flank, QC panel or
target span, region-filtered files under a config with no policy, and missing
evidence -- naming each Analysis and what differs.

Run from the repository root:
    python3 tests/phase-b-candidate/test_acquisition_evidence.py
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import yaml

from manifest_generation.acquisition_manifest import ACQUISITION_MANIFEST, pass_lineage_path
from manifest_generation.candidate_workflow import (
    CandidateError,
    acquisition_evidence,
    apply_release_policy,
    build_candidate_tables,
    load_candidate_configuration,
    render_validation_yaml,
)
from manifest_generation.connector_tsv import write_connector
from manifest_generation.region_filter import load_qc_panel
from manifest_generation.region_filtered_acquisition import (
    filter_summary_path,
    filter_tool,
    policy_record,
    sparse_regions_path,
    table_target_spans,
)
from manifest_generation.release_config import load_release_config
from manifest_generation.source_inventory import sha256_file
from manifest_generation.source_metadata_table import load_source_metadata_table

from test_phase_b_candidate import (  # sibling suite's fixture helpers
    READY_ANALYSES,
    REPO_ROOT,
    SNAPSHOT_ID,
    STORE_ID,
    FixtureBuilder,
    _read_tsv,
    _refreeze,
    _run_cli,
    _synthetic_analysis_metadata,
    _synthetic_inventory_row,
    _synthetic_resolver_record,
    _write_tsv,
)

READY = [row[0] for row in READY_ANALYSES]
QC_PANEL_ID = "fixture-qc-panel"
#: Chen 2023's policy (OGS-00006), with the fixture's QC panel.
POLICY = {
    "region_policy_id": "sig-trans-1mb_suggestive-leads",
    "association_coverage": "signals_only",
    "trans_flank_bp": 1_000_000,
    "significant_p": 5.0e-8,
    "suggestive_p": 1.0e-5,
    "suggestive_lead_min_distance_bp": 100_000,
    "qc_panel_resource_id": QC_PANEL_ID,
}
#: Sun 2018's (OGS-00007): a cis window around each Analysis's target.
CIS_POLICY = {
    **POLICY,
    "region_policy_id": "cis-1mb_sig-trans-1mb_suggestive-leads",
    "association_coverage": "cis_and_signals",
    "cis_flank_bp": 1_000_000,
}
TARGET_SPAN = {"target_chromosome": "2", "target_start": "171555373", "target_end": "173628598"}
SUMMARY_HEADER = "analysis_id\tstatus\tretained_rows\tchecksum\n"
REGIONS_HEADER = "analysis_id\tregion_id\tregion_kind\tchromosome\tstart\tend\tlead_variant_id\n"


def edit_config(fixture, edit) -> None:
    document = yaml.safe_load(fixture.config_path.read_text(encoding="utf-8"))
    edit(document)
    fixture.config_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def declare_region_policy(fixture, policy=POLICY) -> None:
    """Give the release config a region policy and the QC panel it keeps, changing no file."""
    panel = fixture.root / "qc_panel.tsv"
    if not panel.exists():
        panel.write_text("chromosome\tposition\n1\t15903\n", encoding="utf-8")

    def edit(document) -> None:
        document["region_policy"] = dict(policy)
        # A region-filtered release declares the coverage its policy filters to.
        document["association_coverage"] = policy["association_coverage"]
        document["reference_resources"].append(
            {"resource_id": QC_PANEL_ID, "kind": "qc_panel", "location": str(panel), "version": "1"}
        )

    edit_config(fixture, edit)


def acquire_region_filtered(fixture, policy=POLICY, *, evidence=True) -> None:
    """Leave the fixture's files as region-filtered acquisition does, then freeze it again.

    Each ready row gains its lineage: the config's policy with its QC panel's
    identity and, for a cis policy, its target span. The plan and summary go
    beside the manifest, bound by its run record.
    """
    declare_region_policy(fixture, policy)
    if policy["association_coverage"] == "cis_and_signals":
        rows = _read_tsv(fixture.source_metadata_table_path)
        columns = list(rows[0]) + list(TARGET_SPAN)
        _write_tsv(fixture.source_metadata_table_path, columns, [{**row, **TARGET_SPAN} for row in rows])
    config = load_release_config(fixture.config_path, REPO_ROOT)
    resource = config.reference_resources[QC_PANEL_ID]
    panel = load_qc_panel(Path(resource.location), QC_PANEL_ID)
    targets = table_target_spans(load_source_metadata_table(fixture.source_metadata_table_path))

    manifest = fixture.acquisition_manifest_path
    rows = _read_tsv(manifest)
    for row in rows:
        if row["source_analysis_id"] in READY:
            record = policy_record(config.region_policy, panel, targets.get(row["source_analysis_id"]))
            row.update(
                original_location=f"/originals/{row['source_analysis_id']}.h.tsv.gz",
                original_sha256="0" * 64,
                reformat_tool=filter_tool(),
                region_policy=json.dumps(record, sort_keys=True, separators=(",", ":")),
            )
    lineage = ["original_location", "original_sha256", "reformat_tool", "region_policy"]
    columns = list(rows[0]) + [column for column in lineage if column not in rows[0]]
    write_connector(manifest, ACQUISITION_MANIFEST, columns, rows)

    run = {"written_by": "manifest_generation.region_filtered_acquisition"}
    if evidence:
        summary, regions = filter_summary_path(manifest), sparse_regions_path(manifest)
        summary.write_text(
            SUMMARY_HEADER + "".join(f"{a}\tok\t{100 + n}\tc{n}\n" for n, a in enumerate(READY)),
            encoding="utf-8",
        )
        regions.write_text(
            REGIONS_HEADER
            + "".join(f"{a}\t{a}__qc_panel__001\tqc_panel\t1\t15903\t15903\t\n" for a in READY)
            + f"{READY[0]}\t{READY[0]}__suggestive__001\tsuggestive_trans\t3\t5000\t5000\trs1\n",
            encoding="utf-8",
        )
        run["evidence"] = {
            "filter_summary": {"path": str(summary), "sha256": sha256_file(summary)},
            "sparse_regions": {"path": str(regions), "sha256": sha256_file(regions)},
        }
    pass_lineage_path(manifest).write_text(yaml.safe_dump(run), encoding="utf-8")
    _refreeze(fixture)


class RegionLineageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="region-lineage-")
        # A Ragged release, because only a Ragged Store can hold a region
        # policy's coverage: a Dense or Hybrid one has a cell for every
        # (variant, Analysis), so it can carry only `gwas` (opengwas/opengwasdb
        # #298, ADR 0040). Every region-filtered Store Family is Ragged.
        self.fixture = FixtureBuilder(Path(self._tmp.name)).build(layout="ragged")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def config(self):
        return load_release_config(self.fixture.config_path, REPO_ROOT)

    def assert_refused(self, *expected: str) -> None:
        """Preflight fails naming each of ``expected``, and so does rendering the evidence."""
        result = _run_cli(self.fixture, "--stage", "preflight")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        report = json.loads(
            (self.fixture.work_root / STORE_ID / "preflight" / f"{SNAPSHOT_ID}.json").read_text()
        )
        failures = "\n".join(report["failures"])
        with self.assertRaises(CandidateError) as caught:
            acquisition_evidence(self.config(), READY)
        for text in expected:
            self.assertIn(text, failures)
            self.assertIn(text, result.stderr)
            self.assertIn(text, str(caught.exception))

    def test_a_region_filtered_release_is_generated_with_its_evidence(self) -> None:
        acquire_region_filtered(self.fixture)
        result = _run_cli(self.fixture)
        self.assertEqual(result.returncode, 0, result.stderr)
        sidecars = self.fixture.registry_root / STORE_ID / "sidecars"
        summary = (sidecars / "filter_summary.tsv").read_text(encoding="utf-8").splitlines()
        self.assertEqual(summary[0] + "\n", SUMMARY_HEADER)
        self.assertEqual([line.split("\t")[0] for line in summary[1:]], READY)
        self.assertTrue((sidecars / "sparse_regions.tsv").is_file())

    def test_a_policy_declared_over_unfiltered_files_is_refused(self) -> None:
        # The reviewer's reproduction: generation exited 0 and emitted without
        # either filtering sidecar.
        declare_region_policy(self.fixture)
        result = _run_cli(self.fixture)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse((self.fixture.registry_root / STORE_ID).exists())
        self.assert_refused(
            f"9 ready Analysis/Analyses were not filtered to region policy "
            f"sig-trans-1mb_suggestive-leads (no region-filtered lineage): {READY[0]}",
            "acquisition bound no filter_summary",
            "acquisition bound no sparse_regions",
        )

    def test_rendering_refuses_however_the_stages_are_run(self) -> None:
        self.assertEqual(_run_cli(self.fixture).returncode, 0)
        before = (self.fixture.registry_root / STORE_ID / "analyses.tsv").read_bytes()
        declare_region_policy(self.fixture)
        result = _run_cli(self.fixture, "--stage", "emit")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("no region-filtered lineage", result.stderr)
        self.assertEqual((self.fixture.registry_root / STORE_ID / "analyses.tsv").read_bytes(), before)

    def test_a_changed_flank_refuses_the_files_filtered_under_the_old_one(self) -> None:
        acquire_region_filtered(self.fixture)
        self.assertEqual(_run_cli(self.fixture, "--stage", "preflight").returncode, 0)
        edit_config(self.fixture, lambda document: document["region_policy"].update(trans_flank_bp=500_000))
        self.assert_refused(
            "9 ready Analysis/Analyses were not filtered to region policy sig-trans-1mb_suggestive-leads "
            f"(trans_flank_bp 1000000, not 500000): {', '.join(READY)}",
        )

    def test_a_changed_qc_panel_refuses_the_files_filtered_with_the_old_one(self) -> None:
        acquire_region_filtered(self.fixture)
        panel = self.fixture.root / "qc_panel.tsv"
        recorded = sha256_file(panel)
        panel.write_text("chromosome\tposition\n1\t15903\n2\t20000\n", encoding="utf-8")
        self.assert_refused(f"'sha256': '{recorded}'}}, not {{'resource_id': '{QC_PANEL_ID}', 'sha256': '{sha256_file(panel)}'")

    def test_a_changed_target_span_refuses_that_analysiss_file(self) -> None:
        acquire_region_filtered(self.fixture, CIS_POLICY)
        self.assertEqual(_run_cli(self.fixture, "--stage", "preflight").returncode, 0)
        rows = _read_tsv(self.fixture.source_metadata_table_path)
        for row in rows:
            if row["source_analysis_id"] == READY[2]:
                row["target_end"] = "173700000"
        _write_tsv(self.fixture.source_metadata_table_path, list(rows[0]), rows)
        _refreeze(self.fixture)
        self.assert_refused(f"(target {{'chromosome': '2', 'end': 173628598, 'start': 171555373}}, not "
                            f"{{'chromosome': '2', 'start': 171555373, 'end': 173700000}}): {READY[2]}")

    def test_region_filtered_files_under_a_config_without_a_policy_are_refused(self) -> None:
        acquire_region_filtered(self.fixture)
        edit_config(self.fixture, lambda document: document.pop("region_policy"))
        self.assert_refused(
            f"9 ready Analysis/Analyses were acquired region-filtered, but {self.fixture.config_path} "
            f"declares no region_policy: {READY[0]}",
            "acquisition recorded region-filtering evidence (filter_summary, sparse_regions)",
        )

    def test_a_region_filtered_release_without_its_evidence_is_refused(self) -> None:
        acquire_region_filtered(self.fixture, evidence=False)
        self.assert_refused("acquisition bound no filter_summary", "acquisition bound no sparse_regions")

    def test_a_ready_analysis_the_summary_does_not_cover_is_refused(self) -> None:
        acquire_region_filtered(self.fixture)
        summary = filter_summary_path(self.fixture.acquisition_manifest_path)
        summary.write_text(
            "".join(line for line in summary.read_text().splitlines(True) if not line.startswith(READY[4])),
            encoding="utf-8",
        )
        run = pass_lineage_path(self.fixture.acquisition_manifest_path)
        document = yaml.safe_load(run.read_text())
        document["evidence"]["filter_summary"]["sha256"] = sha256_file(summary)
        run.write_text(yaml.safe_dump(document), encoding="utf-8")
        _refreeze(self.fixture)
        self.assert_refused(f"records no filtering of 1 ready Analysis/Analyses: {READY[4]}")

    def test_evidence_changed_since_the_freeze_is_refused(self) -> None:
        acquire_region_filtered(self.fixture)
        regions = sparse_regions_path(self.fixture.acquisition_manifest_path)
        regions.write_text(regions.read_text().replace("rs1", "rs2"), encoding="utf-8")
        self.assert_refused(f"{regions} has sha256 {sha256_file(regions)}")


class AcquisitionEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="acquisition-evidence-")
        self.fixture = FixtureBuilder(Path(self._tmp.name)).build()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def config(self):
        return load_release_config(self.fixture.config_path, REPO_ROOT)

    def test_the_selections_rows_are_carried_verbatim(self) -> None:
        acquire_region_filtered(self.fixture)
        self.assertEqual(
            acquisition_evidence(self.config(), [READY[0]]),
            {
                "filter_summary.tsv": SUMMARY_HEADER + f"{READY[0]}\tok\t100\tc0\n",
                "sparse_regions.tsv": REGIONS_HEADER
                + f"{READY[0]}\t{READY[0]}__qc_panel__001\tqc_panel\t1\t15903\t15903\t\n"
                + f"{READY[0]}\t{READY[0]}__suggestive__001\tsuggestive_trans\t3\t5000\t5000\trs1\n",
            },
        )

    def test_the_bundle_carries_and_reports_them(self) -> None:
        acquire_region_filtered(self.fixture)
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        row = _synthetic_inventory_row()
        outcomes = apply_release_policy(
            [row], config, {row.analysis_id: replace(_synthetic_analysis_metadata())},
            {row.analysis_id: _synthetic_resolver_record()},
        )
        tables = build_candidate_tables(
            inventory_rows=[row], outcomes=outcomes, config=config, index_summary={},
            acquisition_evidence=acquisition_evidence(config.base, [row.analysis_id]),
        )
        self.assertEqual(sorted(tables.acquisition_evidence), ["filter_summary.tsv", "sparse_regions.tsv"])
        reports = render_validation_yaml(
            tables=tables, index_summary={}, validated_at="2026-10-21T00:00:00Z", validator_name="x",
            trait_ontology_mapping={},
        )["reports"]
        self.assertEqual(
            (reports["filter_summary"], reports["sparse_regions"]),
            ("sidecars/filter_summary.tsv", "sidecars/sparse_regions.tsv"),
        )

    def test_a_release_whose_acquisition_recorded_none_carries_none(self) -> None:
        self.assertEqual(acquisition_evidence(self.config(), READY), {})


if __name__ == "__main__":
    unittest.main()

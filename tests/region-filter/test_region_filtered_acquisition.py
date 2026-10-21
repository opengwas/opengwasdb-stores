#!/usr/bin/env python3
"""Acquisition for a region-filtered release: download, filter, delete the original (#300).

ADR 0036, clarified by #300: for scale, acquisition filters each file to the
release's region policy (always keeping the QC panel). The filtered file is what
the Acquisition Manifest lists, and its lineage records the original's location
and sha256, the filter tool and the region-policy parameters. The original is
deleted only after the filtered file's checksum is recorded, so a failure to
record leaves the original in place.

Every original here is a scratch copy in a temporary directory.

Run from the repository root:
    python3 tests/region-filter/test_region_filtered_acquisition.py
"""

from __future__ import annotations

import gzip
import os
import tempfile
import unittest
from dataclasses import replace

import yaml
from pathlib import Path

from manifest_generation.acquisition_manifest import (
    ACQUISITION_MANIFEST,
    load_acquisition_manifest,
    pass_lineage_path,
)
from manifest_generation.connector_tsv import write_connector
from manifest_generation.region_filter import QcPanel, RegionPolicy, TargetSpan
from manifest_generation.region_filtered_acquisition import (
    RegionFilteredAcquisitionError,
    acquire_region_filtered,
    filter_summary_path,
    sparse_regions_path,
)
from manifest_generation.source_inventory import sha256_file

HEADER = "chromosome\tbase_pair_location\teffect_allele\tother_allele\tbeta\tstandard_error\tp_value\n"
POLICY = RegionPolicy(
    region_policy_id="cis-1mb_sig-trans-1mb_suggestive-leads",
    association_coverage="cis_plus_signals",
    trans_flank_bp=1_000_000,
    significant_p=5e-8,
    suggestive_p=1e-5,
    suggestive_lead_min_distance_bp=100_000,
    cis_flank_bp=1_000_000,
)
PANEL = QcPanel("qc-panel-hg38", "e" * 64, frozenset({("1", 15_903)}))


class RegionFilteredAcquisitionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.downloads = self.tmp / "downloads"
        self.downloads.mkdir()
        self.original = self.downloads / "GCST1.h.tsv.gz"
        with gzip.open(self.original, "wt", encoding="utf-8") as fh:
            fh.write(
                HEADER
                + "1\t15903\tA\tG\t0.1\t0.02\t0.9\n"   # QC panel: kept
                + "2\t3500000\tA\tG\t0.1\t0.02\t0.5\n"  # cis: kept
                + "3\t100\tA\tG\t0.1\t0.02\t0.5\n"     # nothing: dropped
            )
        self.original_sha256 = sha256_file(self.original)
        self.original_bytes = self.original.read_bytes()
        self.originals = self.tmp / "originals.acquisition-manifest.tsv"
        write_connector(
            self.originals,
            ACQUISITION_MANIFEST,
            ("source_analysis_id", "status", "location", "bytes", "sha256", "source_url", "error"),
            [
                {
                    "source_analysis_id": "GCST1",
                    "status": "ok",
                    "location": str(self.original),
                    "bytes": str(self.original.stat().st_size),
                    "sha256": self.original_sha256,
                    "source_url": "https://example.org/GCST1.h.tsv.gz",
                },
                {"source_analysis_id": "GCST2", "status": "data_absent_upstream", "error": "404"},
            ],
        )
        self.filtered_dir = self.tmp / "filtered"
        self.manifest = self.tmp / "connectors" / "acquisition-manifest.tsv"
        self.targets = {
            "GCST1": TargetSpan("2", 3_000_000, 3_050_000, gene_id="ENSG00000152256", symbol="PDK1")
        }

    def tearDown(self) -> None:
        for directory, _dirs, _files in os.walk(self.tmp):
            os.chmod(directory, 0o755)
        self._tmp.cleanup()

    def acquire(self, policy=POLICY, panel=PANEL):
        return acquire_region_filtered(
            load_acquisition_manifest(self.originals),
            policy=policy,
            qc_panel=panel,
            targets=self.targets,
            out_dir=self.filtered_dir,
            manifest_path=self.manifest,
            source_genome_builds={"GCST1": "GRCh38"},
        )

    def test_the_filtered_file_and_its_lineage_are_recorded_and_the_original_deleted(self) -> None:
        self.acquire()
        manifest = load_acquisition_manifest(self.manifest)
        entry = manifest.entry("GCST1")
        filtered = Path(entry.location)
        self.assertEqual(filtered.parent, self.filtered_dir)
        self.assertEqual((entry.sha256, entry.bytes), (sha256_file(filtered), filtered.stat().st_size))
        self.assertEqual(entry.source_url, "https://example.org/GCST1.h.tsv.gz")
        with gzip.open(filtered, "rt", encoding="utf-8") as fh:
            self.assertEqual(len(fh.read().splitlines()), 3)
        lineage = entry.lineage
        self.assertEqual(lineage.original_location, str(self.original))
        self.assertEqual(lineage.original_sha256, self.original_sha256)
        self.assertTrue(lineage.reformat_tool.startswith("manifest_generation.region_filter sha256:"))
        self.assertEqual(
            lineage.region_policy,
            {
                "region_policy_id": "cis-1mb_sig-trans-1mb_suggestive-leads",
                "association_coverage": "cis_plus_signals",
                "cis_flank_bp": 1_000_000,
                "trans_flank_bp": 1_000_000,
                "significant_p": 5e-8,
                "suggestive_p": 1e-5,
                "suggestive_lead_min_distance_bp": 100_000,
                "qc_panel": {"resource_id": "qc-panel-hg38", "sha256": "e" * 64},
                "target": {"chromosome": "2", "start": 3_000_000, "end": 3_050_000},
            },
        )
        # The explained absence is carried, and only the recorded original is gone.
        self.assertEqual((manifest.entry("GCST2").status, manifest.entry("GCST2").error),
                         ("data_absent_upstream", "404"))
        self.assertFalse(self.original.exists())

    def test_the_plan_and_summary_are_recorded_beside_the_manifest(self) -> None:
        """R's sparse_regions.tsv and filter_summary.tsv, which the bundle carries (#300)."""
        self.acquire()
        filtered = self.filtered_dir / "GCST1.filtered.tsv.gz"
        self.assertEqual(
            filter_summary_path(self.manifest).read_text(encoding="utf-8").splitlines(),
            [
                "analysis_id\tstatus\terror\tsource_url\tdownloaded_bytes\tinput_rows\tretained_rows"
                "\tretained_fraction\tcis_rows\tsignificant_trans_regions\tsignificant_trans_rows"
                "\tsuggestive_leads\tqc_panel_rows\toutput_file\toutput_bytes\tchecksum_algorithm\tchecksum",
                f"GCST1\tok\t\thttps://example.org/GCST1.h.tsv.gz\t{len(self.original_bytes)}\t3\t2"
                f"\t0.666667\t1\t0\t0\t0\t1\t{filtered}\t{filtered.stat().st_size}\tsha256"
                f"\t{sha256_file(filtered)}",
            ],
        )
        self.assertEqual(
            sparse_regions_path(self.manifest).read_text(encoding="utf-8").splitlines(),
            [
                "analysis_id\tregion_id\tregion_kind\tchromosome\tstart\tend\tsource_genome_build"
                "\ttarget_id\ttarget_label\tlead_variant_id\tpvalue_threshold\tn_variants_retained"
                "\tregion_policy_id",
                "GCST1\tGCST1__cis__001\tcis\t2\t2000000\t4050000\tGRCh38\tENSG00000152256\tPDK1\t\t\t1"
                "\tcis-1mb_sig-trans-1mb_suggestive-leads",
                "GCST1\tGCST1__qc_panel__001\tqc_panel\t1\t15903\t15903\tGRCh38\t\t\t\t\t1"
                "\tcis-1mb_sig-trans-1mb_suggestive-leads",
            ],
        )
        run = yaml.safe_load(pass_lineage_path(self.manifest).read_text(encoding="utf-8"))
        for name, path in (("filter_summary", filter_summary_path(self.manifest)),
                           ("sparse_regions", sparse_regions_path(self.manifest))):
            self.assertEqual(run["evidence"][name]["sha256"], sha256_file(path))

    def test_a_failed_checksum_write_leaves_the_original_in_place(self) -> None:
        # The manifest's directory refuses the write that would record the
        # filtered file's checksum.
        self.manifest.parent.mkdir()
        os.chmod(self.manifest.parent, 0o555)
        with self.assertRaises(OSError):
            self.acquire()
        self.assertTrue(self.original.is_file())
        self.assertEqual(sha256_file(self.original), self.original_sha256)
        self.assertFalse(self.manifest.exists())

    def interrupted_after_recording(self) -> None:
        """A run that recorded GCST1's filtered file and was stopped before deleting the original."""
        self.acquire()
        self.original.write_bytes(self.original_bytes)

    def assertResumeRefused(self, *fragments: str, **changed) -> None:
        """The resume refuses, naming GCST1 and ``fragments``, and changes and deletes nothing."""
        recorded = self.manifest.read_bytes()
        evidence = {path: path.read_bytes() for path in (filter_summary_path(self.manifest),
                                                         sparse_regions_path(self.manifest)) if path.is_file()}
        with self.assertRaises(RegionFilteredAcquisitionError) as caught:
            self.acquire(**changed)
        for fragment in ("GCST1",) + fragments:
            self.assertIn(fragment, str(caught.exception))
        self.assertEqual(sha256_file(self.original), self.original_sha256)
        self.assertEqual(self.manifest.read_bytes(), recorded)
        self.assertEqual({path: path.read_bytes() for path in evidence if path.is_file()}, evidence)

    def test_a_resume_whose_filtered_file_is_gone_is_refused_and_keeps_the_original(self) -> None:
        self.interrupted_after_recording()
        (self.filtered_dir / "GCST1.filtered.tsv.gz").unlink()
        self.assertResumeRefused("filtered file")

    def test_a_resume_under_another_policy_is_refused_and_keeps_the_original(self) -> None:
        self.interrupted_after_recording()
        self.assertResumeRefused("trans_flank_bp", policy=replace(POLICY, trans_flank_bp=500_000))

    def test_a_resume_with_another_qc_panel_is_refused_and_keeps_the_original(self) -> None:
        self.interrupted_after_recording()
        self.assertResumeRefused("qc_panel", panel=replace(PANEL, sha256="d" * 64))

    def test_a_resume_whose_plan_is_gone_is_refused_and_keeps_the_original(self) -> None:
        self.interrupted_after_recording()
        sparse_regions_path(self.manifest).unlink()
        self.assertResumeRefused(sparse_regions_path(self.manifest).name)

    def test_a_resume_whose_plan_was_edited_is_refused_and_keeps_the_original(self) -> None:
        self.interrupted_after_recording()
        plan = sparse_regions_path(self.manifest)
        plan.write_text(plan.read_text(encoding="utf-8").replace("qc_panel\t1\t15903", "qc_panel\t1\t15904"))
        self.assertResumeRefused(plan.name, "sha256")

    def test_a_resume_whose_summary_was_edited_is_refused_and_keeps_the_original(self) -> None:
        self.interrupted_after_recording()
        summary = filter_summary_path(self.manifest)
        lines = summary.read_text(encoding="utf-8").splitlines(True)
        fields = lines[1].split("\t")
        fields[5] = "999"  # input_rows
        summary.write_text(lines[0] + "\t".join(fields), encoding="utf-8")
        self.assertResumeRefused(summary.name, "sha256")

    def test_a_resume_finishes_an_interrupted_deletion(self) -> None:
        self.interrupted_after_recording()
        recorded = self.manifest.read_bytes()
        self.acquire()
        self.assertFalse(self.original.exists())
        self.assertEqual(self.manifest.read_bytes(), recorded)

    def test_a_rerun_keeps_what_was_recorded_and_filters_nothing_again(self) -> None:
        self.acquire()
        recorded = self.manifest.read_bytes()
        self.acquire()
        self.assertEqual(self.manifest.read_bytes(), recorded)


if __name__ == "__main__":
    unittest.main()

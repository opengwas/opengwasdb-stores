#!/usr/bin/env python3
"""Acquiring the R generator's filtered files as they are (#300).

Chen 2023's originals were deleted after the R ``gwas-ssf-ragged`` generator
filtered them, so OGS-00006 regenerates from its filtered files as they are.
Their lineage comes from the bundle's ``filter_summary.tsv``: the original's
URL and size (the R filter recorded no checksum), the R filter's policy, and
the tool. A file whose sha256 is not the one R recorded is accepted only as
one of the reviewed re-materialisations of 15 Sep 2026 -- its (recorded,
on-disk) sha256 pair listed -- and its lineage says so; any other difference
is refused. The run record checksums the bundle's own sparse_regions.tsv and
filter_summary.tsv as the evidence its candidate carries, and the list.

Run from the repository root:
    python3 tests/freeze/test_gwas_catalog_legacy_ragged_acquisition.py
"""

from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path

import yaml

from manifest_generation.acquisition_manifest import load_acquisition_manifest, pass_lineage_path
from manifest_generation.providers.gwas_catalog.legacy_ragged_acquisition import write_legacy_manifest
from manifest_generation.source_inventory import InventoryError, sha256_file

URL = "http://ftp.ebi.ac.uk/pub/databases/gwas/summary_statistics/GCST90199001-GCST90200000/{0}/harmonised/{0}.h.tsv.gz"


class LegacyRaggedAcquisitionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.bundle = self.tmp / "OGS-00006"
        (self.bundle / "sidecars").mkdir(parents=True)
        files = {}
        for analysis_id in ("GCST90199621", "GCST90199622"):
            files[analysis_id] = self.tmp / f"{analysis_id}.filtered.tsv.gz"
            with gzip.open(files[analysis_id], "wt") as fh:
                fh.write(f"chromosome\tbase_pair_location\n1\t{analysis_id[-3:]}\n")
        r_checksum = {"GCST90199621": sha256_file(files["GCST90199621"]), "GCST90199622": "9" * 64}
        (self.bundle / "release.yaml").write_text("generator:\n  version: sha256:d48e94f4\n")
        (self.bundle / "analyses.tsv").write_text(
            "analysis_id\tsource_file\tsource_url\n"
            + "".join(f"{a}\t{files[a]}\t{URL.format(a)}\n" for a in files)
        )
        (self.bundle / "sidecars" / "filter_summary.tsv").write_text(
            "analysis_id\tstatus\tsource_url\tdownloaded_bytes\tchecksum\n"
            + "".join(f"{a}\tok\t{URL.format(a)}\t40418976{n}\t{r_checksum[a]}\n" for n, a in enumerate(files))
        )
        (self.bundle / "sidecars" / "sparse_regions.tsv").write_text(
            "analysis_id\tregion_id\nGCST90199621\tGCST90199621__qc_panel__001\n"
        )
        self.files = files
        self.reviewed = self.tmp / "rematerialised.tsv"
        self.reviewed.write_text(
            "store_id\tanalysis_id\trecorded_sha256\trematerialised_sha256\trecorded_rows"
            "\trematerialised_rows\tlead_sibling_rows\n"
            f"OGS-00006\tGCST90199622\t{'9' * 64}\t{sha256_file(files['GCST90199622'])}\t1\t2\t1\n"
        )
        self.out = self.tmp / "chen.acquisition-manifest.tsv"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_each_filtered_file_is_recorded_with_its_lineage_from_the_filter_summary(self) -> None:
        out = self.out
        write_legacy_manifest(self.bundle, "OGS-00006", out, self.reviewed)
        manifest = load_acquisition_manifest(out)
        same, rematerialised = manifest.entry("GCST90199621"), manifest.entry("GCST90199622")
        self.assertEqual((same.location, same.sha256), (str(self.files["GCST90199621"]),
                                                       sha256_file(self.files["GCST90199621"])))
        self.assertEqual(same.source_url, URL.format("GCST90199621"))
        self.assertEqual(
            (same.lineage.original_location, same.lineage.original_sha256, same.lineage.original_bytes),
            (URL.format("GCST90199621"), "", 404189760),
        )
        self.assertEqual(
            same.lineage.reformat_tool,
            "resources/generators/lib/source-formats/gwas-ssf-ragged/generate.R sha256:d48e94f4",
        )
        self.assertEqual(
            rematerialised.lineage.reformat_tool,
            "resources/scripts/materialise-gwas-ssf-ragged.R, from OGS-00006 sidecars/sparse_regions.tsv "
            f"(2026-09-15); generate.R recorded sha256 {'9' * 64}",
        )
        self.assertEqual(
            same.lineage.region_policy,
            {
                "region_policy_id": "sig-trans-1mb_suggestive-leads",
                "association_coverage": "signals_only",
                "trans_flank_bp": 1_000_000,
                "significant_p": 5e-8,
                "suggestive_p": 1e-5,
                "suggestive_lead_min_distance_bp": 100_000,
                "qc_panel": {
                    "resource_id": "qc-panel-hg38",
                    "sha256": sha256_file(
                        Path(__file__).resolve().parents[2]
                        / "resources/reference-resources/qc-panel-hg38/qc_panel.tsv"
                    ),
                },
            },
        )
        self.assertEqual(json.loads(out.read_text().splitlines()[1].split("\t")[-1]), same.lineage.region_policy)
        run = yaml.safe_load(pass_lineage_path(out).read_text(encoding="utf-8"))
        self.assertEqual(run["reviewed_rematerialisations"]["sha256"], sha256_file(self.reviewed))
        for name in ("filter_summary", "sparse_regions"):
            self.assertEqual(
                run["evidence"][name]["sha256"], sha256_file(self.bundle / "sidecars" / f"{name}.tsv")
            )

    def test_a_file_changed_any_other_way_is_refused(self) -> None:
        """A replacement no review explains -- here junk -- is never given a provenance."""
        self.files["GCST90199622"].write_bytes(b"not a GWAS-SSF file")
        with self.assertRaises(InventoryError) as caught:
            write_legacy_manifest(self.bundle, "OGS-00006", self.out, self.reviewed)
        self.assertIn("GCST90199622", str(caught.exception))
        self.assertFalse(self.out.exists())


if __name__ == "__main__":
    unittest.main()

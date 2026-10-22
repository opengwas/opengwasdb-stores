#!/usr/bin/env python3
"""The full eQTLGen cis release's shipped config, connectors and frozen inventory agree (#314).

Two seams, read from the committed files, not regenerated:

- the release config loads, and its Selection selects every one of the 19,250
  probes, in-process, from the frozen inventory's own copy of the Source Metadata
  Table;
- the Acquisition Manifest the release ships claims the file set the frozen
  Source Inventory records, and its members' sizes and sha256 are the ones the
  inventory and the pass lineage it checksums name -- read from them, never
  recomputed by the code under test.

Run from the repository root:
    python3 tests/eqtlgen-pipeline/test_full_release.py
"""

from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

import yaml

from manifest_generation.acquisition_manifest import load_acquisition_manifest, pass_lineage_path
from manifest_generation.release_config import load_release_config
from manifest_generation.selection import load_selection, select
from manifest_generation.source_inventory import read_inventory
from manifest_generation.source_metadata_table import load_source_metadata_table

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = REPO_ROOT / "resources" / "generators" / "eqtlgen-cis" / "release-full.yaml"
SNAPSHOT = REPO_ROOT / "resources" / "inventories" / "eqtlgen-cis-full-2026-10-22c"
PROBES = 19250


def inventory_meta() -> dict:
    return yaml.safe_load(Path(f"{SNAPSHOT}.meta.yaml").read_text(encoding="utf-8"))


class FullReleaseTest(unittest.TestCase):
    def test_the_config_loads_and_its_selection_selects_every_probe_from_the_frozen_inventory(self) -> None:
        config = load_release_config(CONFIG, REPO_ROOT)
        self.assertEqual(config.source_collection_id, "eqtlgen-cis")
        frozen = load_source_metadata_table(Path(f"{SNAPSHOT}.source-metadata.tsv"))
        rows = select(frozen, load_selection(CONFIG))
        self.assertEqual(len(rows), PROBES)
        self.assertEqual(len({row["source_probe_id"] for row in rows}), PROBES)
        # Each probe's maximum pair N, derived from the BESD (#314): literals, from the
        # published per-pair NrSamples (TNF's maximum is 14,263; the derived 14,267 is
        # within the stored values' 0.04% rounding), and the study total as the cap.
        sizes = {row["source_probe_id"]: int(row["sample_size"]) for row in rows}
        self.assertEqual(sizes["ENSG00000232810"], 14267)
        self.assertEqual(sizes["ENSG00000253755"], 5504)
        self.assertEqual(max(sizes.values()), 31684)
        self.assertEqual(sum(1 for n in sizes.values() if n == 31684), 5456)
        self.assertTrue(all(0 < n <= 31684 for n in sizes.values()))
        self.assertEqual({row["tissue"] for row in rows}, {"whole_blood"})
        self.assertTrue(all(":" not in row["source_analysis_id"] for row in rows))

    def test_the_shipped_acquisition_manifest_claims_the_file_set_the_frozen_inventory_records(self) -> None:
        meta = inventory_meta()
        shipped = REPO_ROOT / "resources" / "generators" / "eqtlgen-cis" / "connectors"
        manifest_path = shipped / "eqtlgen-full.acquisition-manifest.tsv"
        # The shipped file is the one the inventory checksums, and so is its pass lineage.
        recorded = meta["acquisition_manifest"]["original"]
        self.assertEqual(hashlib.sha256(manifest_path.read_bytes()).hexdigest(), recorded["sha256"])
        lineage_path = pass_lineage_path(manifest_path)
        self.assertEqual(
            hashlib.sha256(lineage_path.read_bytes()).hexdigest(), recorded["pass_lineage"]["sha256"]
        )
        # The members the pass lineage records for the file set.
        lineage = yaml.safe_load(lineage_path.read_text(encoding="utf-8"))
        subset = lineage["subset"]["members"]
        self.assertEqual(sorted(subset), [".besd", ".epi", ".esi"])

        inventory = {row.analysis_id: row for row in read_inventory(Path(f"{SNAPSHOT}.tsv"))}
        entries = load_acquisition_manifest(manifest_path).entries
        self.assertEqual(len(entries), PROBES)
        self.assertEqual({entry.source_analysis_id for entry in entries}, set(inventory))
        for entry in entries:
            row = inventory[entry.source_analysis_id]
            self.assertEqual(entry.location, row.data_file)
            self.assertEqual((str(entry.bytes), entry.sha256), (row.data_bytes, row.sha256))
            self.assertEqual(entry.members, {k: dict(v) for k, v in subset.items()})
            self.assertEqual(entry.members[".besd"]["sha256"], row.sha256)
        self.assertEqual(
            lineage["original"]["counts"],
            {"probes": PROBES, "snps": 8932843, "associations": 127339354},
        )
        self.assertEqual(
            lineage["subset"]["counts"],
            {"probes": PROBES, "snps": 8932823, "associations": 127339354},
        )


if __name__ == "__main__":
    unittest.main()

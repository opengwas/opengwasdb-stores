#!/usr/bin/env python3
"""Acquire the R generator's region-filtered files as they are (#300, ADR 0036).

OGS-00006 (Chen 2023) and OGS-00007 (Sun 2018) were built from files the
retired R ``gwas-ssf-ragged`` generator downloaded, filtered and kept, deleting
each original. Chen's originals are gone, so its release regenerates from the
filtered files as they are; Sun's committed release config uses its filtered
files the same way until it is acquired afresh with the shared region filter
(``manifest_generation.region_filtered_acquisition``).

This writes the Acquisition Manifest of a built bundle's filtered files --
each file's location, size and sha256 as it is now -- with lineage taken from
the bundle's ``sidecars/filter_summary.tsv``: the original's URL and size (the
R filter recorded no original checksum, so ``original_sha256`` is blank and
``original_bytes`` identifies it), the policy the R generator's config
declared, and the tool. A file whose sha256 is the one R recorded is its
output; one whose sha256 differs was re-materialised from the bundle's
``sparse_regions.tsv`` on 15 Sep 2026 by ``materialise-gwas-ssf-ragged.R``,
which keeps a suggestive lead's multi-allelic siblings too, and its lineage
says so. The policies are constants here because the R configs that declared
them are retired with the generator (their git history keeps them).

Usage:
    python3 -m manifest_generation.providers.gwas_catalog.legacy_ragged_acquisition \\
        OGS-00006 --out <Acquisition Manifest>
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from manifest_generation import REPO_ROOT
from manifest_generation.acquisition_manifest import ACQUISITION_MANIFEST, load_acquisition_manifest
from manifest_generation.connector_tsv import write_connector
from manifest_generation.source_inventory import InventoryError, sha256_file

COLUMNS: tuple[str, ...] = (
    "source_analysis_id", "status", "location", "bytes", "sha256", "source_url",
    "original_location", "reformat_tool", "original_bytes", "region_policy",
)

R_GENERATOR = "resources/generators/lib/source-formats/gwas-ssf-ragged/generate.R"
QC_PANEL = REPO_ROOT / "resources/reference-resources/qc-panel-hg38/qc_panel.tsv"

#: The ``filter:`` blocks of the retired R configs, as region-policy records:
#: metabolome-plasma-2023/config-european.yaml (OGS-00006) and
#: pqtl-interval-2018/config-pilot-10.yaml (OGS-00007, no QC panel).
LEGACY_POLICIES: Mapping[str, Mapping[str, Any]] = {
    "OGS-00006": {
        "region_policy_id": "sig-trans-1mb_suggestive-leads",
        "association_coverage": "signals_only",
        "trans_flank_bp": 1_000_000,
        "significant_p": 5e-8,
        "suggestive_p": 1e-5,
        "suggestive_lead_min_distance_bp": 100_000,
        "qc_panel": "qc-panel-hg38",
    },
    "OGS-00007": {
        "region_policy_id": "cis-1mb_sig-trans-1mb_suggestive-leads",
        "association_coverage": "cis_plus_signals",
        "cis_flank_bp": 1_000_000,
        "trans_flank_bp": 1_000_000,
        "significant_p": 5e-8,
        "suggestive_p": 1e-5,
        "suggestive_lead_min_distance_bp": 100_000,
        "qc_panel": None,
    },
}


def write_legacy_manifest(bundle: Path, store_id: str, out: Path) -> None:
    """Write the Acquisition Manifest of ``bundle``'s filtered files, with their R lineage."""
    bundle = Path(bundle)
    if store_id not in LEGACY_POLICIES:
        raise InventoryError(f"{store_id} is not a bundle the R gwas-ssf-ragged generator filtered")
    policy = dict(LEGACY_POLICIES[store_id])
    if policy["qc_panel"] is not None:
        policy["qc_panel"] = {"resource_id": policy["qc_panel"], "sha256": sha256_file(QC_PANEL)}
    else:
        del policy["qc_panel"]
    version = (yaml.safe_load((bundle / "release.yaml").read_text(encoding="utf-8")) or {})["generator"]["version"]
    summary = {row["analysis_id"]: row for row in _read(bundle / "sidecars" / "filter_summary.tsv")}
    targets = {
        row["source_analysis_id"]: row for row in _read(bundle / "sidecars" / "analysis_targets.tsv")
    } if (bundle / "sidecars" / "analysis_targets.tsv").is_file() else {}
    rows = []
    for analysis in _read(bundle / "analyses.tsv"):
        analysis_id = analysis["analysis_id"]
        filtered = Path(analysis["source_file"])
        recorded = summary[analysis_id]
        sha256 = sha256_file(filtered)
        tool = (
            f"{R_GENERATOR} {version}"
            if sha256 == recorded["checksum"]
            else "resources/scripts/materialise-gwas-ssf-ragged.R, from "
            f"{store_id} sidecars/sparse_regions.tsv (2026-09-15); generate.R recorded sha256 "
            f"{recorded['checksum']}"
        )
        record = dict(policy)
        if analysis_id in targets:
            target = targets[analysis_id]
            record["target"] = {
                "chromosome": target["chromosome"],
                "start": int(target["gene_start"]),
                "end": int(target["gene_end"]),
            }
        rows.append({
            "source_analysis_id": analysis_id,
            "status": "ok",
            "location": str(filtered),
            "bytes": str(filtered.stat().st_size),
            "sha256": sha256,
            "source_url": analysis["source_url"],
            "original_location": recorded["source_url"],
            "reformat_tool": tool,
            "original_bytes": recorded["downloaded_bytes"],
            "region_policy": json.dumps(record, sort_keys=True, separators=(",", ":")),
        })
    write_connector(out, ACQUISITION_MANIFEST, COLUMNS, rows)
    try:
        load_acquisition_manifest(out)
    except InventoryError:
        Path(out).unlink()
        raise


def _read(path: Path) -> list[dict[str, str]]:
    with Path(path).open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("store_id", choices=sorted(LEGACY_POLICIES))
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        write_legacy_manifest(REPO_ROOT / "stores" / args.store_id, args.store_id, args.out)
    except InventoryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Acquisition Manifest {args.out} written from stores/{args.store_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Acquire the R generator's region-filtered files as they are (#300, ADR 0036).

OGS-00006 (Chen 2023) was built from files the retired R ``gwas-ssf-ragged``
generator downloaded, filtered and kept, deleting each original. The
originals are gone, so its release regenerates from the filtered files as
they are.

This writes the Acquisition Manifest of the bundle's filtered files -- each
file's location, size and sha256 as it is now -- with lineage taken from the
bundle's ``sidecars/filter_summary.tsv``: the original's URL and size (the R
filter recorded no original checksum, so ``original_sha256`` is blank and
``original_bytes`` identifies it), the policy the R generator's config
declared, and the tool. A file whose sha256 is the one R recorded is its
output. A file whose sha256 differs is accepted only when the reviewed list
of re-materialisations names its (recorded, on-disk) sha256 pair: on 15 Sep
2026 ``materialise-gwas-ssf-ragged.R`` re-created 198 of them from the
bundle's ``sparse_regions.tsv``, keeping a suggestive lead's multi-allelic
siblings too, and its lineage says so. Any other difference is refused,
naming the Analysis, and no manifest is written.

The run record beside the manifest (``acquisition_manifest.pass_lineage_path``)
checksums the reviewed list, and the bundle's own ``sparse_regions.tsv`` and
``filter_summary.tsv`` as the evidence a candidate carries (#300). The policy
is a constant here because the R config that declared it is retired with the
generator (git history keeps it).

Usage:
    python3 -m manifest_generation.providers.gwas_catalog.legacy_ragged_acquisition \\
        OGS-00006 --out <Acquisition Manifest> [--rematerialised <reviewed list>]
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
from manifest_generation.acquisition_manifest import (
    ACQUISITION_MANIFEST,
    load_acquisition_manifest,
    pass_lineage_path,
)
from manifest_generation.connector_tsv import write_connector
from manifest_generation.freeze import recorded_path
from manifest_generation.source_inventory import InventoryError, atomic_write, sha256_file

COLUMNS: tuple[str, ...] = (
    "source_analysis_id", "status", "location", "bytes", "sha256", "source_url",
    "original_location", "reformat_tool", "original_bytes", "region_policy",
)

R_GENERATOR = "resources/generators/lib/source-formats/gwas-ssf-ragged/generate.R"
QC_PANEL = REPO_ROOT / "resources/reference-resources/qc-panel-hg38/qc_panel.tsv"
#: The reviewed re-materialisations of OGS-00006's filtered files.
REMATERIALISED = REPO_ROOT / "resources/generators/gwas-catalog-ragged/ogs-00006-rematerialised-2026-09-15.tsv"

#: The ``filter:`` block of the retired R config
#: metabolome-plasma-2023/config-european.yaml, as a region-policy record.
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
}

#: The reviewed list's columns: the pair that identifies a re-materialisation,
#: and the counts its review checked (rows less lead siblings = R's rows).
REMATERIALISED_COLUMNS: tuple[str, ...] = (
    "store_id", "analysis_id", "recorded_sha256", "rematerialised_sha256", "recorded_rows",
    "rematerialised_rows", "lead_sibling_rows",
)


def write_legacy_manifest(bundle: Path, store_id: str, out: Path, rematerialised: Path = REMATERIALISED) -> None:
    """Write the Acquisition Manifest of ``bundle``'s filtered files, with their R lineage.

    Refused, with nothing written, if a file's sha256 is neither R's recorded
    one nor a reviewed re-materialisation's.
    """
    bundle, out = Path(bundle), Path(out)
    if store_id not in LEGACY_POLICIES:
        raise InventoryError(f"{store_id} is not a bundle the R gwas-ssf-ragged generator filtered")
    policy = dict(LEGACY_POLICIES[store_id])
    policy["qc_panel"] = {"resource_id": policy["qc_panel"], "sha256": sha256_file(QC_PANEL)}
    version = (yaml.safe_load((bundle / "release.yaml").read_text(encoding="utf-8")) or {})["generator"]["version"]
    summary = {row["analysis_id"]: row for row in _read(bundle / "sidecars" / "filter_summary.tsv")}
    reviewed_rows = _read(rematerialised)
    if not reviewed_rows or tuple(reviewed_rows[0]) != REMATERIALISED_COLUMNS:
        raise InventoryError(f"{rematerialised} is not a reviewed list in {', '.join(REMATERIALISED_COLUMNS)}")
    reviewed = {
        (row["analysis_id"], row["recorded_sha256"], row["rematerialised_sha256"])
        for row in reviewed_rows
        if row["store_id"] == store_id
    }
    rows, unexplained = [], []
    for analysis in _read(bundle / "analyses.tsv"):
        analysis_id = analysis["analysis_id"]
        filtered = Path(analysis["source_file"])
        recorded = summary[analysis_id]
        sha256 = sha256_file(filtered)
        if sha256 == recorded["checksum"]:
            tool = f"{R_GENERATOR} {version}"
        elif (analysis_id, recorded["checksum"], sha256) in reviewed:
            tool = (
                "resources/scripts/materialise-gwas-ssf-ragged.R, from "
                f"{store_id} sidecars/sparse_regions.tsv (2026-09-15); generate.R recorded sha256 "
                f"{recorded['checksum']}"
            )
        else:
            unexplained.append(f"{analysis_id} ({filtered} has sha256 {sha256}; R recorded {recorded['checksum']})")
            continue
        record = dict(policy)
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
    if unexplained:
        raise InventoryError(
            f"{len(unexplained)} filtered file(s) of {store_id} are neither R's recorded output nor a "
            f"re-materialisation {rematerialised} reviewed: " + "; ".join(unexplained[:10])
        )
    write_connector(out, ACQUISITION_MANIFEST, COLUMNS, rows)
    try:
        load_acquisition_manifest(out)
    except InventoryError:
        out.unlink()
        raise
    document = {
        "acquisition_manifest": recorded_path(out),
        "written_by": "manifest_generation.providers.gwas_catalog.legacy_ragged_acquisition",
        "bundle": recorded_path(bundle),
        "reviewed_rematerialisations": {"path": recorded_path(rematerialised), "sha256": sha256_file(rematerialised)},
        "evidence": {
            name: {"path": recorded_path(path), "sha256": sha256_file(path)}
            for name, path in (
                ("filter_summary", bundle / "sidecars" / "filter_summary.tsv"),
                ("sparse_regions", bundle / "sidecars" / "sparse_regions.tsv"),
            )
        },
    }
    atomic_write(
        pass_lineage_path(out), yaml.safe_dump(document, sort_keys=False, default_flow_style=False, width=100)
    )


def _read(path: Path) -> list[dict[str, str]]:
    with Path(path).open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("store_id", choices=sorted(LEGACY_POLICIES))
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--rematerialised", type=Path, default=REMATERIALISED, help="the reviewed list")
    args = parser.parse_args(argv)
    try:
        write_legacy_manifest(REPO_ROOT / "stores" / args.store_id, args.store_id, args.out, args.rematerialised)
    except InventoryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Acquisition Manifest {args.out} written from stores/{args.store_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""CLI script to generate the frozen sample manifest for the QC Panel Concordance Study (#152).

Usage:
  python3 resources/generators/gwas-catalog-eur-hybrid/sample_concordance.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Add repo root to sys.path
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from resources.generators.lib.concordance_sampling import (
    build_concordance_sample,
    write_sample_manifest,
)
from resources.generators.lib.source_inventory import read_inventory

DEFAULT_INVENTORY = REPO_ROOT / "resources" / "inventories" / "gwas-catalog-ssf-eur-hybrid-2026-09-10.tsv"
DEFAULT_OUT_TSV = REPO_ROOT / "resources" / "inventories" / "gwas-catalog-ssf-eur-hybrid-qc-sample-2026-09-10.tsv"
DEFAULT_OUT_META = REPO_ROOT / "resources" / "inventories" / "gwas-catalog-ssf-eur-hybrid-qc-sample-2026-09-10.meta.yaml"
DEFAULT_SNAPSHOT_ID = "gwas-catalog-ssf-eur-hybrid-qc-sample-2026-09-10"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY, help="Path to Source Inventory TSV")
    parser.add_argument("--out-tsv", type=Path, default=DEFAULT_OUT_TSV, help="Output sample TSV path")
    parser.add_argument("--out-meta", type=Path, default=DEFAULT_OUT_META, help="Output sample meta.yaml path")
    parser.add_argument("--snapshot-id", default=DEFAULT_SNAPSHOT_ID, help="Snapshot identifier")
    args = parser.parse_args()

    print(f"Loading Source Inventory from {args.inventory}...")
    inventory_rows = read_inventory(args.inventory)
    print(f"Loaded {len(inventory_rows)} inventory rows.")

    sample = build_concordance_sample(inventory_rows)
    print(f"Generated stratified sample with {len(sample)} Analyses:")
    strata: dict[str, int] = {}
    for s in sample:
        strata[s.stratum] = strata.get(s.stratum, 0) + 1
    for k, v in sorted(strata.items()):
        print(f"  {k:20s}: {v}")

    write_sample_manifest(
        sample_rows=sample,
        output_tsv=args.out_tsv,
        output_meta=args.out_meta,
        inventory_path=args.inventory,
        snapshot_id=args.snapshot_id,
    )
    print(f"Wrote sample manifest to {args.out_tsv} and {args.out_meta}")


if __name__ == "__main__":
    main()

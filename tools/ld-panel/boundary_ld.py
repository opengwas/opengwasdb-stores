#!/usr/bin/env python3
"""Measure LD across one LD-block boundary in a genotype cohort (#241).

Reusing a block layout from another ancestry can cut LD the target cohort
carries across a boundary, which within-block checks cannot see. This takes a
window either side of one boundary, applies the panel's variant floor, and
summarises r^2 between every variant left of the boundary and every variant
right of it. A variant at the boundary position belongs to both blocks and is
left out. Running it for two cohorts at the same boundary compares them.
"""
from __future__ import annotations

import argparse, json
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from construct_block import _plink_extract, construct_block, standardised_ea_dosages
from scratch import scratch_dir

R2_CUTS = (.2, .5, .8)


def cross_boundary_ld(genotypes: np.ndarray, variants: list[dict[str, object]], edge: int, *,
                      maf_threshold: float, max_missing_rate: float | None = None) -> dict[str, object]:
    # construct_block applies exactly the panel's floor; its decomposition is unused.
    rows = construct_block(genotypes, variants, maf_threshold=maf_threshold, max_missing_rate=max_missing_rate,
                           component_floor=0).variant_table
    x, _ = standardised_ea_dosages(genotypes, variants, rows)
    position = np.array([int(r["BP"]) for r in rows])
    left, right = position < edge, position > edge
    r2 = (x[:, left].T @ x[:, right]) ** 2
    return {
        "edge": edge, "n_individuals": int(x.shape[0]), "n_left": int(left.sum()), "n_right": int(right.sum()),
        "n_pairs": int(r2.size), "max_r2": float(r2.max()) if r2.size else None,
        **{f"pairs_r2_ge_{cut}": int((r2 >= cut).sum()) for cut in R2_CUTS},
        **{f"left_variants_r2_ge_{cut}": int((r2 >= cut).any(axis=1).sum()) for cut in R2_CUTS},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pfile", type=Path, required=True); ap.add_argument("--keep", type=Path, required=True)
    ap.add_argument("--chrom", required=True); ap.add_argument("--edge", type=int, required=True)
    ap.add_argument("--window", type=int, required=True, help="Base pairs either side of the boundary")
    ap.add_argument("--maf", type=float, required=True); ap.add_argument("--max-missing", type=float)
    ap.add_argument("--plink2", default="plink2")
    ap.add_argument("--out", type=Path, required=True, help="Result JSON")
    args = ap.parse_args()
    source = SimpleNamespace(plink2=args.plink2, pfile=args.pfile, bfile=None, chrom=args.chrom,
                             start=max(1, args.edge - args.window), end=args.edge + args.window, keep=args.keep)
    with scratch_dir("ld-boundary-") as tmp:
        genotypes, variants, _ = _plink_extract(source, Path(tmp))
    result = cross_boundary_ld(genotypes, variants, args.edge, maf_threshold=args.maf,
                               max_missing_rate=args.max_missing)
    result.update(chrom=args.chrom, window=args.window, keep=str(args.keep))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__": raise SystemExit(main())

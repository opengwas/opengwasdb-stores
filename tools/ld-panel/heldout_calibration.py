#!/usr/bin/env python3
"""Genotype-held-out imputation calibration for one LD block (#241).

The public API is :func:`calibrate_block`. Individuals are split, stratified by
population, into a training half that builds the panel and a held-out half
that stands in for a GWAS: each replicate draws z-scores with the held-out
individuals' own LD (a sparse causal signal scaled to peak ``ncp``, plus
noise distributed as N(0, R_test)), masks a fraction of variants, and imputes
them with core's ``impute_z_block`` from the panel's eigendecomposition. A
panel built from the held-out individuals themselves is the in-sample upper
bound. Only reference genotypes are read; no GWAS association data is.
"""
from __future__ import annotations

import argparse, csv, json, tempfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from construct_block import _plink_extract, construct_block, standardised_ea_dosages
from find_underresolved_blocks import components_needed


def split_individuals(populations: list[str], *, test_fraction: float, seed: int) -> np.ndarray:
    """Boolean held-out mask holding ``test_fraction`` of each population."""
    labels = np.asarray(populations)
    rng = np.random.default_rng(seed)
    test = np.zeros(len(labels), dtype=bool)
    for population in sorted(set(populations)):
        members = np.flatnonzero(labels == population)
        test[rng.choice(members, int(round(len(members) * test_fraction)), replace=False)] = True
    return test


def calibrate_block(genotypes: np.ndarray, variants: list[dict[str, object]], populations: list[str], *,
                    maf_threshold: float, max_missing_rate: float | None = None,
                    test_fraction: float = 1 / 3, mask_fraction: float = .2,
                    replicates: int = 20, ncp: float = 8.0, thresh: float = .9, min_cor: float = .7,
                    component_floor: int = 250, seed: int = 20261022) -> dict[str, object]:
    from opengwasdb.completion.impute import impute_z_block

    g = np.asarray(genotypes, dtype=np.float64)
    test = split_individuals(populations, test_fraction=test_fraction, seed=seed)
    floor = {"maf_threshold": maf_threshold, "max_missing_rate": max_missing_rate, "component_floor": component_floor}
    built = {"held_out": construct_block(g[~test], variants, **floor),
             "test_in_sample": construct_block(g[test], variants, **floor)}
    # z-scores exist only where the variant passes the floor in both halves.
    in_test = {r["SNP"] for r in built["test_in_sample"].variant_table}
    shared = [r for r in built["held_out"].variant_table if r["SNP"] in in_test]
    settings = {"maf_threshold": maf_threshold, "max_missing_rate": max_missing_rate,
                "test_fraction": test_fraction, "mask_fraction": mask_fraction,
                "replicates": replicates, "ncp": ncp, "thresh": thresh, "min_cor": min_cor,
                "component_floor": component_floor, "seed": seed}
    counts = {"n_train": int((~test).sum()), "n_test": int(test.sum()), "n_shared_variants": len(shared)}
    if len(shared) < 10:  # too few to draw causal variants and a masked set from
        return {**counts, "panels": {}, "settings": settings, "replicates": [],
                "skipped": f"{len(shared)} variants pass the floor in both halves"}
    x, _ = standardised_ea_dosages(g[test], variants, shared)
    panels = {}
    for name, result in built.items():
        k = min(components_needed(result.eigenvalues, thresh), result.eigenvectors.shape[1])
        row_of = {r["SNP"]: i for i, r in enumerate(result.variant_table)}
        panels[name] = (result.eigenvalues[:k], result.eigenvectors[:, :k],
                        np.array([row_of[r["SNP"]] for r in shared]))
    rng = np.random.default_rng(seed + 1)
    m, rows = len(shared), []
    for replicate in range(replicates):
        causal = rng.choice(m, int(rng.integers(1, 4)), replace=False)
        signal = x.T @ (x[:, causal] @ rng.normal(size=len(causal)))
        z = signal * (ncp / np.abs(signal).max()) + x.T @ rng.normal(size=x.shape[0])
        masked = np.zeros(m, dtype=bool)
        masked[rng.choice(m, max(3, int(round(mask_fraction * m))), replace=False)] = True
        for name, (values, vectors, panel_rows) in panels.items():
            observed = np.full(vectors.shape[0], np.nan)
            observed[panel_rows[~masked]] = z[~masked]
            predicted, quality = impute_z_block(observed, vectors, values, min_cor=min_cor)
            row = {"replicate": replicate, "panel": name, "n_causal": len(causal),
                   "n_masked": int(masked.sum()), "quality_r": float(quality) if np.isfinite(quality) else None,
                   "accepted": predicted is not None, "pearson_r": None, "rmse": None, "slope": None}
            if predicted is not None:
                truth, guess = z[masked], predicted[panel_rows[masked]]
                row.update(pearson_r=float(np.corrcoef(truth, guess)[0, 1]),
                           rmse=float(np.sqrt(np.mean((guess - truth) ** 2))),
                           slope=float(np.polyfit(truth, guess, 1)[0]))
            rows.append(row)
    return {
        **counts,
        "panels": {name: {"n_variants": len(result.variant_table),
                          "components_at_thresh": int(panels[name][1].shape[1]),
                          "components_retained": int(result.eigenvectors.shape[1])}
                   for name, result in built.items()},
        "settings": settings, "replicates": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pfile", type=Path, required=True); ap.add_argument("--keep", type=Path, required=True)
    ap.add_argument("--chrom", required=True)
    ap.add_argument("--start", type=int, required=True); ap.add_argument("--end", type=int, required=True)
    ap.add_argument("--cohort", type=Path, required=True, help="TSV with sample and population columns")
    ap.add_argument("--maf", type=float, required=True); ap.add_argument("--max-missing", type=float)
    ap.add_argument("--replicates", type=int, default=20)
    ap.add_argument("--seed", type=int, default=20261022); ap.add_argument("--plink2", default="plink2")
    ap.add_argument("--out", type=Path, required=True, help="Result JSON")
    args = ap.parse_args()
    with args.cohort.open(newline="") as fh:
        population_of = {r["sample"]: r["population"] for r in csv.DictReader(fh, delimiter="\t")}
    source = SimpleNamespace(plink2=args.plink2, pfile=args.pfile, bfile=None, chrom=args.chrom,
                             start=args.start, end=args.end, keep=args.keep)
    with tempfile.TemporaryDirectory(prefix="ld-calibrate-") as tmp:
        genotypes, variants, ids = _plink_extract(source, Path(tmp))
    result = calibrate_block(genotypes, variants, [population_of[i] for i in ids],
                             maf_threshold=args.maf, max_missing_rate=args.max_missing,
                             replicates=args.replicates, seed=args.seed)
    result["block"] = f"{args.chrom}/{args.start}-{args.end}"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__": raise SystemExit(main())

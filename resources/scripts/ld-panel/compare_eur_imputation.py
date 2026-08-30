#!/usr/bin/env python3
"""Paired held-out beta comparison of UKB and HGDP+1kGP EUR LD panels."""
from __future__ import annotations

import argparse
import csv
import json
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

for variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(variable, "1")

import numpy as np
import pandas as pd
import pysam
from scipy.stats import spearmanr
from threadpoolctl import threadpool_limits

from opengwasdb.completion.impute import impute_z_block, scalar_n_se
from opengwasdb.encoding import DenseZPlane
from opengwasdb.store.open import open_store

BLOCKS = (
    ("1", "222798680-224041489"),
    ("6", "30106780-30425675"),
    ("11", "30827313-33937829"),
    ("16", "60049115-62536937"),
    ("22", "32439511-33959775"),
)


def canonical_alid(identifier: str) -> str:
    value = identifier.removeprefix("chr")
    parts = value.split(":")
    if len(parts) == 4:
        chrom, position, first, second = parts
    elif len(parts) == 2 and parts[1].count("_") == 2:
        chrom, rest = parts
        position, first, second = rest.split("_")
    else:
        raise ValueError(f"unrecognised panel identifier: {identifier}")
    first, second = sorted((first.upper(), second.upper()))
    return f"{chrom}:{int(position)}:{first}:{second}"


def load_panel_block(root: Path, chrom: str, block: str, thresh: float) -> dict[str, object]:
    base = root / "EUR" / chrom / block
    rows = list(csv.DictReader(base.with_suffix(".tsv").open(), delimiter="\t"))
    alids = [canonical_alid(row["SNP"]) for row in rows]
    eaf = np.array([float(row["EAF"]) for row in rows], dtype=np.float64)
    positions = np.array([int(row["BP"]) for row in rows], dtype=np.int64)
    data = np.load(base.with_suffix(".ldeig.npz"))
    values = data["values"].astype(np.float64)
    vectors = data["vectors"].astype(np.float64)
    cumulative = np.cumsum(np.maximum(values, 0.0))
    k = min(int(np.searchsorted(cumulative / cumulative[-1], thresh)) + 1, vectors.shape[1])
    return {"alids": alids, "eaf": eaf, "positions": positions,
            "values": values[:k], "vectors": vectors[:, :k]}


def panel_alids(root: Path, chrom: str, block: str) -> set[str]:
    path = root / "EUR" / chrom / f"{block}.tsv"
    with path.open() as handle:
        return {canonical_alid(row["SNP"]) for row in csv.DictReader(handle, delimiter="\t")}


def store_rows(store: Path, chrom: str, block: str) -> dict[str, int]:
    start, end = block.split("-")
    result: dict[str, int] = {}
    with pysam.TabixFile(str(store / "variants.tsv.gz")) as tabix:
        for line in tabix.fetch(chrom, int(start) - 1, int(end)):
            fields = line.split("\t")
            result[fields[5]] = int(fields[2])
    return result


def held_out_alids(store: Path, panels: dict[str, Path], chrom: str, block: str,
                   fraction: float, seed: int) -> tuple[set[str], int]:
    shared = set(store_rows(store, chrom, block))
    for root in panels.values():
        shared &= panel_alids(root, chrom, block)
    ordered = np.array(sorted(shared), dtype=object)
    rng = np.random.default_rng(seed + int(chrom) * 1_000_003)
    n = max(1, int(round(len(ordered) * fraction)))
    return set(rng.choice(ordered, size=n, replace=False).tolist()), len(ordered)


def correlations(truth: np.ndarray, predicted: np.ndarray) -> dict[str, float]:
    if len(truth) < 3 or np.std(truth) == 0 or np.std(predicted) == 0:
        return {key: float("nan") for key in
                ("pearson_r", "spearman_r", "rmse", "mae", "slope", "intercept")}
    slope, intercept = np.polyfit(truth, predicted, 1)
    return {
        "pearson_r": float(np.corrcoef(truth, predicted)[0, 1]),
        "spearman_r": float(spearmanr(truth, predicted).statistic),
        "rmse": float(np.sqrt(np.mean((predicted - truth) ** 2))),
        "mae": float(np.mean(np.abs(predicted - truth))),
        "slope": float(slope), "intercept": float(intercept),
    }


def evaluate(task: tuple[str, str, str, str, str, str, str, float, float, int]) -> list[dict[str, object]]:
    (panel_name, panel_root_s, ukb_root_s, hgdp_root_s, store_s,
     chrom, block, fraction, thresh, seed) = task
    threadpool_limits(limits=1)
    panel_root, store = Path(panel_root_s), Path(store_s)
    panel = load_panel_block(panel_root, chrom, block, thresh)
    panels = {
        "UKB EUR": Path(ukb_root_s),
        "HGDP+1kGP EUR": Path(hgdp_root_s),
    }
    heldout, n_shared = held_out_alids(store, panels, chrom, block, fraction, seed)
    mapping = store_rows(store, chrom, block)
    alids = panel["alids"]
    matched_local = [i for i, alid in enumerate(alids) if alid in mapping]
    matched_store = [mapping[alids[i]] for i in matched_local]
    # z is read through the store's declared encoding, never off the raw array:
    # since opengwasdb#114 the plane is int16 fixed point, so `.oindex[...]`
    # alone would hand back z-scores 1024x too large -- and this script's whole
    # output is a comparison of imputed z against observed z.
    opened = open_store(store)
    root = opened.arrays(mode="r")
    z_plane = DenseZPlane.open(root, opened.manifest.encoding)
    n_analyses = z_plane.n_analyses
    z = np.full((len(alids), n_analyses), np.nan, dtype=np.float64)
    se = np.full_like(z, np.nan)
    z[matched_local] = z_plane.rows(np.asarray(matched_store))
    se[matched_local] = root["se"].oindex[matched_store, :].astype(np.float64)
    target = np.array([alid in heldout for alid in alids])
    positions = panel["positions"]
    output: list[dict[str, object]] = []
    for analysis in range(n_analyses):
        truth_mask = target & np.isfinite(z[:, analysis]) & np.isfinite(se[:, analysis])
        n_truth = int(truth_mask.sum())
        training_z = z[:, analysis].copy()
        training_se = se[:, analysis].copy()
        training_z[target] = np.nan
        training_se[target] = np.nan
        observed = np.isfinite(training_z)
        predicted_z, quality_r = impute_z_block(
            training_z, panel["vectors"], panel["values"], min_cor=0.7
        )
        row: dict[str, object] = {
            "panel": panel_name, "chrom": chrom, "block": block,
            "analysis_index": analysis, "n_shared_variants": n_shared,
            "n_masked_variants": len(heldout), "n_truth": n_truth,
            "n_training": int(observed.sum()), "quality_r": quality_r,
            "accepted": predicted_z is not None,
        }
        if predicted_z is None or n_truth < 3:
            row.update({"n_predicted": 0, **correlations(np.array([]), np.array([]))})
        else:
            predicted_se = scalar_n_se(
                training_se[observed], panel["eaf"][observed], panel["eaf"]
            )
            # Match dense completion's +/-1 Mb observed-z cap.
            obs_positions = positions[observed]
            obs_abs_z = np.abs(training_z[observed])
            for i in np.flatnonzero(target):
                nearby = np.abs(obs_positions - positions[i]) <= 1_000_000
                if nearby.any():
                    cap = float(obs_abs_z[nearby].max())
                    predicted_z[i] = np.clip(predicted_z[i], -cap, cap)
            valid = truth_mask & np.isfinite(predicted_z) & np.isfinite(predicted_se)
            truth_beta = z[valid, analysis] * se[valid, analysis]
            predicted_beta = predicted_z[valid] * predicted_se[valid]
            row.update({"n_predicted": int(valid.sum()),
                        **correlations(truth_beta, predicted_beta)})
        output.append(row)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--ukb-panel", type=Path, required=True)
    parser.add_argument("--hgdp-panel", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--mask-fraction", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=20260806)
    parser.add_argument("--thresh", type=float, default=0.9)
    parser.add_argument("--jobs", type=int, default=10)
    args = parser.parse_args()
    panels = {"UKB EUR": args.ukb_panel, "HGDP+1kGP EUR": args.hgdp_panel}
    tasks = [(name, str(root), str(args.ukb_panel), str(args.hgdp_panel),
              str(args.store), chrom, block,
              args.mask_fraction, args.thresh, args.seed)
             for chrom, block in BLOCKS for name, root in panels.items()]
    rows: list[dict[str, object]] = []
    with ProcessPoolExecutor(max_workers=min(args.jobs, len(tasks))) as pool:
        futures = {pool.submit(evaluate, task): task for task in tasks}
        for future in as_completed(futures):
            task_rows = future.result()
            rows.extend(task_rows)
            print(f"finished {futures[future][0]} chr{futures[future][5]} {futures[future][6]}", flush=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).sort_values(["chrom", "block", "panel", "analysis_index"]).to_csv(args.out, index=False)
    metadata = {"store": str(args.store), "panels": {k: str(v) for k, v in panels.items()},
                "blocks": BLOCKS, "mask_fraction": args.mask_fraction, "seed": args.seed,
                "pca_threshold": args.thresh, "min_correlation": 0.7,
                "n_analyses": 2514}
    args.out.with_suffix(".json").write_text(json.dumps(metadata, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

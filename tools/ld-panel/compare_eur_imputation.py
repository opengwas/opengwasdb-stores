#!/usr/bin/env python3
"""Paired held-out beta comparison of UKB and HGDP+1kGP EUR LD panels.

The truth Store may be a Reference-Completed release, whose `imputed` cells are
panel-derived: counting one as truth, or feeding it to the imputer as training
data, would measure the panel against itself. Every cell this script reads is
therefore gated on the Store's own `imputed` flag -- truth and training are
restricted to cells whose flag is 0 -- and a Store with no `imputed` plane (an
Observed-Only release) is read with every cell observed, as before (#348).

The statistic planes are read through the core's decoders, never off the raw
array: a format-0.2.0 Store keeps `z` as int16 fixed point and, when the plan
chose it, `se` and `eaf` as int8 residuals against the cell's frequency, so a
raw read returns codes rather than z-scores and standard errors.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import NamedTuple

for variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(variable, "1")

import numpy as np
import pandas as pd
import pysam
from scipy.stats import spearmanr
from threadpoolctl import threadpool_limits

from opengwasdb.completion.impute import impute_z_block, scalar_n_se
from opengwasdb.encoding import DenseEafPlane, DenseSePlane, DenseZPlane
from opengwasdb.store.open import open_store

#: What the sidecar records about the Store a run measured against.
OBSERVED_ONLY_RULE = (
    "truth and training cells are restricted to cells whose `imputed` flag is 0; "
    "an imputed cell is never counted as truth nor used as training input"
)
NO_IMPUTED_PLANE_RULE = "this Store has no `imputed` plane; every cell it holds is observed"

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


class StoreCells(NamedTuple):
    """One set of variants' decoded `z`, decoded `se`, and observed-cell mask.

    `observed` is `True` where the cell is the Store's own observation -- i.e.
    where its `imputed` flag is 0, or everywhere for a Store with no `imputed`
    plane. It is the mask truth and training are both restricted to.
    """

    z: np.ndarray
    se: np.ndarray
    observed: np.ndarray


def read_store_cells(store: Path, rows: np.ndarray) -> StoreCells:
    """Decode the `z` and `se` planes at `rows`, and the observed-cell mask.

    Both planes go through the core's decoders under the Store's declared plan
    (`manifest.json`'s `encoding`), because a format-0.2.0 plane's stored bytes
    are not its values: `z` is int16 fixed point at 1024, and `se` is an int8
    residual against the cell's own frequency whenever the plan says so. The
    `imputed` flag is read with the frequency plane that owns it, which is the
    only supported route to it.
    """
    rows = np.asarray(rows, dtype=np.int64)
    opened = open_store(store)
    root = opened.arrays(mode="r")
    encoding = opened.manifest.encoding
    n_analyses = int(root["z"].shape[1])
    if len(rows) == 0:
        empty = np.empty((0, n_analyses), dtype=np.float64)
        return StoreCells(empty, empty.copy(), np.empty((0, n_analyses), dtype=bool))
    z = np.asarray(DenseZPlane.open(root, encoding).rows(rows), dtype=np.float64)
    se_plane = DenseSePlane.open(root, encoding)
    if "imputed" not in root:
        se = np.asarray(se_plane.rows(rows), dtype=np.float64)
        return StoreCells(z, se, np.ones(z.shape, dtype=bool))
    frequency = DenseEafPlane.open(root, encoding).read_block(
        rows, np.arange(n_analyses), want_imputed=True
    )
    # A residual `se` is coded against the frequency of its own cell, so the
    # decoded frequencies are what the decoder needs -- read once here rather
    # than a second time inside the plane.
    se = np.asarray(se_plane.rows(rows, eaf=frequency.values), dtype=np.float64)
    assert frequency.imputed is not None
    return StoreCells(z, se, frequency.imputed == 0)


def store_metadata(store: Path) -> dict[str, object]:
    """What a run must record about the Store: format, state, and the rule."""
    opened = open_store(store)
    root = opened.arrays(mode="r")
    has_imputed = "imputed" in root
    return {
        "path": str(store),
        "format_version": opened.manifest.format_version,
        "completion_state": opened.manifest.completion_state.value,
        "encoding": opened.manifest.encoding.to_manifest(),
        "n_analyses": int(root["z"].shape[1]),
        "has_imputed_plane": has_imputed,
        "observed_only": OBSERVED_ONLY_RULE if has_imputed else NO_IMPUTED_PLANE_RULE,
    }


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
    cells = read_store_cells(store, np.asarray([mapping[alids[i]] for i in matched_local]))
    n_analyses = cells.z.shape[1]
    z = np.full((len(alids), n_analyses), np.nan, dtype=np.float64)
    se = np.full_like(z, np.nan)
    observed_cell = np.zeros((len(alids), n_analyses), dtype=bool)
    z[matched_local] = cells.z
    se[matched_local] = cells.se
    observed_cell[matched_local] = cells.observed
    target = np.array([alid in heldout for alid in alids])
    positions = panel["positions"]
    output: list[dict[str, object]] = []
    for analysis in range(n_analyses):
        # A cell the Store imputed is not evidence: it is neither truth to score
        # against nor an observation to train on.
        available = observed_cell[:, analysis] & np.isfinite(z[:, analysis])
        truth_mask = target & available & np.isfinite(se[:, analysis])
        n_truth = int(truth_mask.sum())
        training_z = np.where(available, z[:, analysis], np.nan)
        training_se = np.where(available, se[:, analysis], np.nan)
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
    release = store_metadata(args.store)
    metadata = {"store": str(args.store), "store_release": release,
                "panels": {k: str(v) for k, v in panels.items()},
                "blocks": BLOCKS, "mask_fraction": args.mask_fraction, "seed": args.seed,
                "pca_threshold": args.thresh, "min_correlation": 0.7,
                "n_analyses": release["n_analyses"]}
    args.out.with_suffix(".json").write_text(json.dumps(metadata, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

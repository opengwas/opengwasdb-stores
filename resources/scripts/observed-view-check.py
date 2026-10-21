"""Do OGS-00010's observed cells reproduce OGS-00009 exactly?

Prototype for the observed-view check of ADR 0037 (#302). The production
check is an opengwasdb command (ADR 0023); this script reads opengwasdb's
planes directly and is kept as the evidence behind the ADR.

Exhaustive: the parent's variant axis maps into the child's, order preserved.
Sampled: whole parent chunk-rows (1000 variants x all analyses), because a
Dense read costs a whole chunk however few of its cells are wanted. Strata:
seeded uniform random chunk-rows, the first/last chunk-row of every
chromosome, the final partial chunk-row, and the parent chunk-rows whose
child span crosses a completion-writer band boundary (current _BAND_ROWS).

Per sampled band, against the child rows each parent row maps to:
  1. every parent-finite cell: child z, se, eaf equal, child imputed == 0
  2. every parent-missing cell: child missing or imputed (never "observed")
  3. inserted child rows inside the span: no observed cell
  4. parent rows with no finite cell (lost if the observed-only axis were
     inferred as "rows with >= 1 observed cell")
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np

from opengwasdb.encoding import DenseEafPlane, DenseSePlane, DenseZPlane
from opengwasdb.store.open import open_store

ROOT = Path("/data/opengwasdb/stores")
CHUNK_ROWS = 1000
COMPLETION_BAND_ROWS = 250_000
MAIN_CHROMS = {str(i) for i in range(1, 23)} | {"X", "Y", "MT", "M"}


def planes(path: Path):
    store = open_store(path)
    root = store.arrays(mode="r")
    enc = store.manifest.encoding
    imputed = root["imputed"] if "imputed" in root else None
    return (
        DenseZPlane.open(root, enc),
        DenseSePlane.open(root, enc),
        DenseEafPlane.open(root, enc),
        imputed,
    )


def axis_map(parent: Path, child: Path) -> tuple[np.ndarray, np.ndarray, dict]:
    pb = np.load(parent / "variant_alid_bytes.npy")
    pr = np.load(parent / "variant_alid_rows.npy")
    cb = np.load(child / "variant_alid_bytes.npy")
    cr = np.load(child / "variant_alid_rows.npy")
    n_parent = len(pr)
    idx = np.searchsorted(cb, pb)
    idx_c = np.minimum(idx, len(cb) - 1)
    found = cb[idx_c] == pb
    p2c = np.full(n_parent, -1, dtype=np.int64)
    p2c[pr[found]] = cr[idx_c[found]]
    chrom = np.empty(n_parent, dtype=object)
    chrom[pr] = np.char.partition(pb, b":")[:, 0].astype(str)
    mapped = p2c >= 0
    stats = {
        "parent_rows_indexed": int(len(pr)),
        "parent_rows_unmapped": int((~mapped).sum()),
        "child_rows_indexed": int(len(cr)),
        "order_preserved": bool(np.all(np.diff(p2c[mapped]) > 0)),
    }
    return p2c, chrom, stats


def choose_bands(p2c, chrom, n_random, seed, n_parent, n_child) -> dict[int, set[str]]:
    n_bands = -(-n_parent // CHUNK_ROWS)
    rng = np.random.default_rng(seed)
    bands: dict[int, set[str]] = {}

    def add(b: int, why: str) -> None:
        bands.setdefault(int(b), set()).add(why)

    for b in rng.choice(n_bands, size=n_random, replace=False):
        add(b, "random")
    starts = np.flatnonzero(np.r_[True, chrom[1:] != chrom[:-1]])
    ends = np.r_[starts[1:] - 1, n_parent - 1]
    other = [i for i, s in enumerate(starts) if chrom[s] not in MAIN_CHROMS]
    keep_other = set(rng.choice(other, size=min(5, len(other)), replace=False)) if other else set()
    for i, (s, e) in enumerate(zip(starts, ends)):
        if chrom[s] in MAIN_CHROMS or i in keep_other:
            add(s // CHUNK_ROWS, "chrom_edge")
            add(e // CHUNK_ROWS, "chrom_edge")
    add(n_bands - 1, "final_partial")
    for k in range(COMPLETION_BAND_ROWS, n_child, COMPLETION_BAND_ROWS):
        p = int(np.searchsorted(p2c, k))  # first parent row mapping at/after k
        if 0 < p < n_parent:
            add(p // CHUNK_ROWS, "writer_band_boundary")
            add((p - 1) // CHUNK_ROWS, "writer_band_boundary")
    return bands


def nan_equal(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return (a == b) | (np.isnan(a) & np.isnan(b))


def check_band(b, p2c, P, C, n_parent) -> dict:
    Pz, Pse, Peaf, _ = P
    Cz, Cse, Ceaf, Cimp = C
    r0, r1 = b * CHUNK_ROWS, min((b + 1) * CHUNK_ROWS, n_parent)
    rows = p2c[r0:r1]
    c0, c1 = int(rows[0]), int(rows[-1]) + 1
    local = rows - c0

    pe = Peaf.read_band(r0, r1).values
    pz, ps = Pz.band(r0, r1), Pse.band(r0, r1, eaf=pe)
    ce = Ceaf.read_band(c0, c1).values
    cz, cs = Cz.band(c0, c1), Cse.band(c0, c1, eaf=ce)
    ci = np.asarray(Cimp[c0:c1]).astype(np.uint8)

    czs, css, ces, cis = cz[local], cs[local], ce[local], ci[local]
    pfin = np.isfinite(pz) & np.isfinite(ps)
    cfin = np.isfinite(czs) & np.isfinite(css)
    inserted = np.ones(c1 - c0, dtype=bool)
    inserted[local] = False
    ins_obs = (np.isfinite(cz[inserted]) & np.isfinite(cs[inserted])) & (ci[inserted] == 0)

    def diff(a, b):
        d = np.abs(a[pfin].astype(np.float64) - b[pfin].astype(np.float64))
        return int((~nan_equal(a[pfin], b[pfin])).sum()), float(np.nanmax(d)) if d.size else 0.0

    z_mis, z_max = diff(pz, czs)
    se_mis, se_max = diff(ps, css)
    eaf_mis, eaf_max = diff(pe, ces)
    return {
        "band": int(b),
        "parent_rows": [r0, r1],
        "child_rows": [c0, c1],
        "cells_compared": int(pz.size),
        "parent_observed": int(pfin.sum()),
        "z_mismatch": z_mis,
        "z_max_abs_diff": z_max,
        "se_mismatch": se_mis,
        "se_max_abs_diff": se_max,
        "eaf_mismatch": eaf_mis,
        "eaf_max_abs_diff": eaf_max,
        "observed_marked_imputed": int((cis[pfin] != 0).sum()),
        "observed_missing_in_child": int((~cfin[pfin]).sum()),
        "missing_became_observed": int((cfin & ~pfin & (cis == 0)).sum()),
        "missing_now_imputed": int((cfin & ~pfin & (cis == 1)).sum()),
        "inserted_rows": int(inserted.sum()),
        "inserted_observed_cells": int(ins_obs.sum()),
        "parent_rows_without_finite": int((~pfin.any(axis=1)).sum()),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--parent", default="OGS-00009")
    ap.add_argument("--child", default="OGS-00010")
    ap.add_argument("--n-random", type=int, default=300)
    ap.add_argument("--seed", type=int, default=20261021)
    ap.add_argument("--pilot", type=int, default=0, help="check only this many random bands")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    parent = ROOT / args.parent / "store.opengwasdb"
    child = ROOT / args.child / "store.opengwasdb"

    t0 = time.time()
    P, C = planes(parent), planes(child)
    n_parent, n_child = P[0]._array.shape[0], C[0]._array.shape[0]
    assert P[0]._array.shape[1] == C[0]._array.shape[1]
    p2c, chrom, axis_stats = axis_map(parent, child)
    axis_stats.update(n_parent=int(n_parent), n_child=int(n_child), seconds=round(time.time() - t0, 1))
    print("axis", json.dumps(axis_stats), flush=True)
    if axis_stats["parent_rows_unmapped"] or not axis_stats["order_preserved"]:
        raise SystemExit("axis map failed; band comparison would be misaligned")

    bands = choose_bands(p2c, chrom, args.n_random, args.seed, n_parent, n_child)
    order = sorted(bands)
    if args.pilot:
        order = sorted(np.random.default_rng(args.seed).choice(order, args.pilot, replace=False))
    out = Path(args.out)
    results = []
    with out.with_suffix(".jsonl").open("w") as fh:
        for i, b in enumerate(order):
            t = time.time()
            r = check_band(int(b), p2c, P, C, n_parent)
            r["strata"] = sorted(bands[int(b)])
            r["seconds"] = round(time.time() - t, 2)
            fh.write(json.dumps(r) + "\n")
            fh.flush()
            results.append(r)
            if i % 25 == 0 or args.pilot:
                print(f"{i + 1}/{len(order)} band {b} {r['seconds']}s", flush=True)

    keys = [k for k in results[0] if isinstance(results[0][k], int) and k != "band"]
    summary = {
        "parent": args.parent,
        "child": args.child,
        "seed": args.seed,
        "axis": axis_stats,
        "bands_checked": len(results),
        "bands_by_stratum": {
            s: sum(s in r["strata"] for r in results)
            for s in ("random", "chrom_edge", "final_partial", "writer_band_boundary")
        },
        "totals": {k: int(sum(r[k] for r in results)) for k in keys},
        "max_abs_diff": {
            k: max(r[k] for r in results) for k in ("z_max_abs_diff", "se_max_abs_diff", "eaf_max_abs_diff")
        },
        "bands_with_any_mismatch": [
            r["band"]
            for r in results
            if r["z_mismatch"] or r["se_mismatch"] or r["eaf_mismatch"] or r["observed_marked_imputed"]
            or r["observed_missing_in_child"] or r["missing_became_observed"] or r["inserted_observed_cells"]
        ],
        "seconds_total": round(time.time() - t0, 1),
        "seconds_per_band_median": float(np.median([r["seconds"] for r in results])),
    }
    out.write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

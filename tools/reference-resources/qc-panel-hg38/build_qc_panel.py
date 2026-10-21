#!/usr/bin/env python3
"""Derive the QC panel from ukb-ancestry-mixture-hg38 (issue #29).

Selects a fixed, reproducible list of ~10,000 variants that are common
(non-degenerate frequency) in every super-population the ancestry-mixture
reference panel supports, and are spread out along the genome so they are
not redundant for either the ancestry NNLS mixture fit or per-variant
implied-SD estimation. Both consume whatever variants happen to survive an
Analysis's signal-driven sparse-region filter; this panel is retained
unconditionally instead (issue #28), so those stages have a stable variant
backbone even for Analyses with few or no significant hits.

Selection procedure:
  1. Load ukb-ancestry-mixture-hg38's fine-group frequencies and average them
     per super-population (AFR, AMR, EAS, EUR, MID, NAF, SAS).
  2. Keep a variant only if every super-population's minor allele frequency
     is at least --freq-floor (default 0.05) — informative everywhere, not
     just on average across groups.
  3. Walk the survivors in genome order and greedily keep one variant every
     --min-spacing-bp (default 250,000) per chromosome, so kept variants are
     not clustered by LD.
  4. If more than --target-size (default 10,000) variants survive step 3,
     evenly subsample down to the target so the final panel size is stable
     across reference-panel updates that only add density, not new regions.

Usage:
  pixi run python resources/reference-resources/qc-panel-hg38/build_qc_panel.py \
    --out=resources/reference-resources/qc-panel-hg38/qc_panel.tsv

Re-run with the same arguments any time ukb-ancestry-mixture-hg38 is rebuilt
(e.g. after a reference-panel update); the procedure is deterministic given
the same input files and parameters.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import numpy as np

from opengwasdb.ancestry.reference import load_reference

DEFAULT_FREQS = "/data/opengwasdb/reference/ancestry-mixture/ref_freqs.hg38.tsv.gz"
DEFAULT_GROUPS = "/data/opengwasdb/reference/ancestry-mixture/ancestry_groups.tsv"
DEFAULT_CHROMOSOMES = [str(i) for i in range(1, 23)]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freqs", default=DEFAULT_FREQS)
    parser.add_argument("--groups", default=DEFAULT_GROUPS)
    parser.add_argument("--out", required=True)
    parser.add_argument("--target-size", type=int, default=10_000)
    parser.add_argument("--min-spacing-bp", type=int, default=250_000)
    parser.add_argument("--freq-floor", type=float, default=0.05)
    parser.add_argument(
        "--chromosomes", default=",".join(DEFAULT_CHROMOSOMES),
        help="Comma-separated chromosome list to select from (default: autosomes 1-22)",
    )
    return parser.parse_args()


def superpop_frequencies(freqs: np.ndarray, group_superpop_index: np.ndarray, n_superpops: int) -> np.ndarray:
    """Average fine-group frequencies into one column per super-population."""
    sums = np.zeros((freqs.shape[0], n_superpops), dtype=np.float64)
    counts = np.zeros(n_superpops, dtype=np.float64)
    for group_col, sp_row in enumerate(group_superpop_index):
        sums[:, sp_row] += freqs[:, group_col]
        counts[sp_row] += 1
    return sums / counts


def greedy_spacing_select(chromosome: np.ndarray, position: np.ndarray, min_spacing_bp: int) -> np.ndarray:
    """Keep at most one row per `min_spacing_bp` window, per chromosome, in genome order."""
    order = np.lexsort((position, chromosome))
    keep = []
    last_chrom = None
    last_pos = -min_spacing_bp - 1
    for idx in order:
        chrom = chromosome[idx]
        pos = position[idx]
        if chrom != last_chrom or pos - last_pos >= min_spacing_bp:
            keep.append(idx)
            last_chrom = chrom
            last_pos = pos
    return np.asarray(keep, dtype=np.int64)


def evenly_subsample(indices: np.ndarray, target_size: int) -> np.ndarray:
    """Deterministically thin `indices` (already in genome order) to `target_size`."""
    if len(indices) <= target_size:
        return indices
    positions = np.linspace(0, len(indices) - 1, target_size)
    chosen = np.unique(np.round(positions).astype(np.int64))
    return indices[chosen]


def main() -> None:
    args = parse_args()
    chromosomes = set(args.chromosomes.split(","))

    ref = load_reference(args.freqs, args.groups, maf_floor=0.0)
    n_superpops = len(ref.superpops)
    sp_freq = superpop_frequencies(ref.freqs, ref.group_superpop_index, n_superpops)
    sp_maf = np.minimum(sp_freq, 1.0 - sp_freq)
    # Plain min, not nanmin: a variant missing frequency data for even one
    # super-population hasn't been shown informative "everywhere", so it
    # should fail the floor check (NaN comparisons are False) rather than
    # be judged only on the super-populations that do have data.
    min_maf_across_superpops = np.min(sp_maf, axis=1)

    # Re-parse the lead columns (chromosome/position/alleles) once, in the
    # same row order load_reference() used, rather than re-reading via a
    # second pass over the plain reader — load_reference() doesn't expose
    # them directly, so pull them back out of alids ("chr:pos:A1:A2").
    chrom = np.empty(ref.n_variants, dtype=object)
    pos = np.empty(ref.n_variants, dtype=np.int64)
    a1 = np.empty(ref.n_variants, dtype=object)
    a2 = np.empty(ref.n_variants, dtype=object)
    for i, alid in enumerate(ref.alids):
        c, p, x1, x2 = alid.split(":")
        chrom[i] = c
        pos[i] = int(p)
        a1[i] = x1
        a2[i] = x2

    candidate_mask = (min_maf_across_superpops >= args.freq_floor) & np.isin(chrom, list(chromosomes))
    candidates = np.nonzero(candidate_mask)[0]
    print(f"{len(candidates)} of {ref.n_variants} variants pass freq-floor={args.freq_floor} "
          f"in every super-population ({', '.join(ref.superpops)})")

    spaced = greedy_spacing_select(chrom[candidates], pos[candidates], args.min_spacing_bp)
    spaced_indices = candidates[spaced]
    print(f"{len(spaced_indices)} variants survive {args.min_spacing_bp}bp spacing pruning")

    final_indices = evenly_subsample(spaced_indices, args.target_size)
    print(f"Selected {len(final_indices)} panel variants (target {args.target_size})")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, delimiter="\t")
        writer.writerow([
            "alid", "chromosome", "position", "effect_allele", "other_allele",
            "min_superpop_maf",
        ])
        for idx in final_indices:
            writer.writerow([
                ref.alids[idx], chrom[idx], pos[idx], a1[idx], a2[idx],
                round(float(min_maf_across_superpops[idx]), 6),
            ])
    print(f"Wrote {len(final_indices)} rows to {out_path}")

    # Sanity check (issue #29): every selected variant must be informative
    # (non-degenerate MAF) in every super-population, by construction.
    final_min_maf = min_maf_across_superpops[final_indices]
    print(
        "Sanity check - min_superpop_maf across the selected panel: "
        f"min={final_min_maf.min():.4f} median={np.median(final_min_maf):.4f} "
        f"max={final_min_maf.max():.4f} (floor was {args.freq_floor})"
    )
    for i, sp in enumerate(ref.superpops):
        sp_maf_selected = sp_maf[final_indices, i]
        print(f"  {sp}: min MAF in panel = {sp_maf_selected.min():.4f}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Backfill missing ``.ldeig.npz`` eigendecompositions for an LD Reference Panel.

OpenGWASDB's reference completion consumes only the eigendecomposition of each LD
block (``load_ld_eigenvectors`` reads ``{block}.ldeig.npz``); the full
``{block}.unphased.vcor1.gz`` matrix is a fallback that is never read when the npz
is present. Panels are therefore stored as eigendecompositions only. Blocks that
predate that decision and still lack an npz must be backfilled before the matrix
fallback is removed, or they would be silently dropped from completion.

Components stored are variance-driven rather than a fixed count: enough to reach
``--cumvar`` of the total spectrum, with a floor of ``--min-k`` so the panel is
never *worse* resolved than the historical fixed-250 convention. This matters
because consumers truncate again at load time (default 0.9); if the stored count
bound first, imputation would silently use less variance than requested.

Each block's read+eigendecompose+write is independent (own matrix file, own npz
output), so blocks run across a small pool of worker processes (issue #34)
instead of one at a time. Unlike I/O-bound parallelism (e.g.
resources/generators/lib/source-formats/gwas-ssf-ragged/generate.R's per-analysis download pool),
this is memory-bound per block — a 15.6k-variant block's dense float64 matrix is
~1.9 GB — so pick ``--jobs`` from available RAM headroom, not core count. See
``find_underresolved_blocks.py`` in this directory for deriving the ``--blocks``
list this script is usually pointed at.

Usage:
  uv run python backfill_eigendecomposition.py \
      --panel /path/to/ld_reference_panel_hg38 --ancestry EUR
"""

from __future__ import annotations

import os

# Must run before numpy/scipy are imported, and unconditionally (not just when
# --jobs > 1): OpenBLAS reads these once, when its runtime initializes at
# library load, not lazily per call — setting them later (e.g. in a
# ProcessPoolExecutor `initializer`, which runs after each forked worker has
# already inherited the parent's already-loaded, already-threaded BLAS) has no
# effect. Without this, 20 worker *processes* each additionally fanning out to
# OpenBLAS's own default of one thread per core meant this script's first
# parallel run (issue #34, 20 jobs on a 224-core box with MAX_THREADS=64)
# oversubscribed to 1000+ concurrent threads and ran slower than the original
# sequential script it was replacing. Process-level parallelism via --jobs is
# the only kind of parallelism this script uses; `setdefault` leaves an
# operator's own pre-set thread env vars alone if they've deliberately
# configured something else.
for _blas_thread_var in (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "BLIS_NUM_THREADS",
):
    os.environ.setdefault(_blas_thread_var, "1")

import argparse
import gzip
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import scipy.linalg

# Deliberately conservative default (issue #34): each worker holds one block's
# full dense LD matrix in memory at once, so raising this scales peak memory
# roughly linearly, not throughput against a shared bottleneck. Meant to be
# raised explicitly via --jobs (reasonable up to ~20) on a machine with enough
# RAM headroom, rather than defaulting to os.cpu_count().
DEFAULT_JOBS = 4


def _read_ld_matrix(path: Path) -> np.ndarray:
    """Read a plink2 ``--r-unphased square`` matrix (tab-delimited, gzipped)."""
    with gzip.open(path, "rt") as fh:
        rows = [np.fromstring(line, sep="\t") for line in fh]  # noqa: NPY201
    return np.asarray(rows, dtype=np.float64)


def _eigendecompose(ld: np.ndarray, cumvar: float, min_k: int) -> tuple[np.ndarray, np.ndarray, int]:
    """Return (all eigenvalues descending, eigenvectors, number of eigenvectors to keep)."""
    vals, vecs = scipy.linalg.eigh(ld)
    vals = vals[::-1]
    vecs = vecs[:, ::-1]
    # plink2 computes pairwise correlations, so the matrix is not guaranteed PSD;
    # clamp negatives exactly as opengwasdb.completion.impute.ld_pca does.
    clamped = np.maximum(vals, 0.0)
    total = float(clamped.sum()) or 1.0
    k = int(np.searchsorted(np.cumsum(clamped) / total, cumvar)) + 1
    k = max(k, min_k)
    k = min(k, vecs.shape[1])
    return vals, vecs, k


def _write_npz(npz_path: Path, vals: np.ndarray, vecs: np.ndarray, k: int) -> None:
    """Atomic write: temp then rename, so a crash never leaves a half-written cache."""
    fd, tmp_base = tempfile.mkstemp(suffix=".tmp", dir=npz_path.parent)
    Path(tmp_base).unlink(missing_ok=True)
    os.close(fd)
    tmp_npz = Path(tmp_base + ".npz")
    try:
        np.savez_compressed(tmp_base, values=vals, vectors=vecs[:, :k])
        tmp_npz.replace(npz_path)
    except Exception:
        tmp_npz.unlink(missing_ok=True)
        raise


def _process_block(base: Path, cumvar: float, min_k: int, out_suffix: str) -> str:
    """Read+eigendecompose+write one block.

    Runs inside a worker process (issue #34): each block's matrix file and npz
    output are independent, so no state is shared across workers. Returns a
    status line rather than printing directly, since worker stdout would
    otherwise interleave across processes.
    """
    ld_path = base.with_suffix(".unphased.vcor1.gz")
    npz_path = base.with_suffix(f"{out_suffix}.npz")
    ld = _read_ld_matrix(ld_path)
    if ld.ndim != 2 or ld.shape[0] != ld.shape[1]:
        return f"!! not square {ld.shape} — skipping"
    vals, vecs, k = _eigendecompose(ld, cumvar, min_k)
    _write_npz(npz_path, vals, vecs, k)
    clamped = np.maximum(vals, 0.0)
    achieved = float(np.cumsum(clamped)[k - 1] / (clamped.sum() or 1.0))
    return f"p={ld.shape[0]} k={k} cumvar={achieved:.4f}"


def _run_sequential(missing: list[Path], cumvar: float, min_k: int, out_suffix: str) -> None:
    for i, base in enumerate(missing, 1):
        print(f"[{i}/{len(missing)}] {base}", flush=True)
        try:
            status = _process_block(base, cumvar, min_k, out_suffix)
        except Exception as e:
            status = f"!! failed: {e}"
        print(f"  {status}", flush=True)


def _run_parallel(missing: list[Path], cumvar: float, min_k: int, out_suffix: str, jobs: int) -> None:
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        futures = {
            pool.submit(_process_block, base, cumvar, min_k, out_suffix): base for base in missing
        }
        for i, future in enumerate(as_completed(futures), 1):
            base = futures[future]
            try:
                status = future.result()
            except Exception as e:
                # A worker that dies outright (e.g. OOM-killed) surfaces here as the
                # future's exception rather than corrupting another block's output —
                # degrade to a normal failed line and let the rest of the pool finish.
                status = f"!! failed: {e}"
            print(f"[{i}/{len(missing)}] {base}\n  {status}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--panel", type=Path, required=True, help="Panel root (contains {ancestry}/)")
    ap.add_argument("--ancestry", required=True)
    ap.add_argument("--cumvar", type=float, default=0.99)
    ap.add_argument("--min-k", type=int, default=250)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--force",
        action="store_true",
        help="Rewrite decompositions that already exist. Needed to re-resolve a panel "
             "whose stored component count is too small for the consumer's truncation "
             "(the UKB EUR panel stored a fixed 250, under-resolving 44%% of blocks).",
    )
    ap.add_argument(
        "--blocks",
        type=Path,
        help="File of block base paths (one per line) to restrict the run to.",
    )
    ap.add_argument(
        "--out-suffix",
        default=".ldeig",
        help="Artifact suffix; use e.g. '.ldeig-rebuilt' to write alongside the "
             "existing decomposition instead of replacing it.",
    )
    ap.add_argument(
        "--jobs",
        "-j",
        type=int,
        default=DEFAULT_JOBS,
        help=f"Blocks to eigendecompose in parallel, across worker processes (issue "
             f"#34; default {DEFAULT_JOBS}). Each worker holds one block's full dense "
             f"LD matrix in memory (a 15.6k-variant block is ~1.9 GB), so this is "
             f"memory-bound, not core-bound — size it to available RAM rather than "
             f"cpu_count(). Reasonable up to ~20 on a machine with sufficient "
             f"headroom. --jobs 1 runs sequentially in-process, matching the original "
             f"(pre-issue-#34) behavior exactly.",
    )
    args = ap.parse_args()

    if args.jobs < 1:
        print("--jobs must be >= 1", file=sys.stderr)
        return 1

    root = args.panel / args.ancestry
    if not root.is_dir():
        print(f"No such panel ancestry directory: {root}", file=sys.stderr)
        return 1

    if args.blocks:
        candidates = [Path(line.strip()) for line in args.blocks.read_text().splitlines() if line.strip()]
    else:
        candidates = [
            tsv.with_suffix("")
            for chrom_dir in sorted(root.iterdir())
            if chrom_dir.is_dir()
            for tsv in sorted(chrom_dir.glob("*.tsv"))
        ]

    missing = [
        base
        for base in candidates
        if base.with_suffix(".unphased.vcor1.gz").exists()
        and (args.force or not base.with_suffix(f"{args.out_suffix}.npz").exists())
    ]
    print(f"{len(missing)} block(s) to (re)build")
    if args.dry_run:
        for b in missing:
            print(f"  {b}")
        return 0

    jobs = min(args.jobs, len(missing)) or 1
    print(f"Running with {jobs} parallel job(s)", flush=True)
    if jobs == 1:
        _run_sequential(missing, args.cumvar, args.min_k, args.out_suffix)
    else:
        _run_parallel(missing, args.cumvar, args.min_k, args.out_suffix, jobs)

    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

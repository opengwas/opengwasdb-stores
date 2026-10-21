#!/usr/bin/env python3
"""List LD Reference Panel blocks whose stored eigendecomposition is under-resolved.

Reference completion truncates each block's stored eigenvectors to however many
reach ``--thresh`` cumulative variance (consumer default 0.9); if a block's
``.ldeig.npz`` stores fewer components than that requires, completion silently
uses less variance than requested instead of erroring (opengwasdb #10, ADR
0031). This scans a panel ancestry directory and lists the block base paths that
fall short, in the form ``backfill_eigendecomposition.py --blocks`` expects
(issue #34).

Usage:
  uv run python find_underresolved_blocks.py \
      --panel /path/to/ld_reference_panel_hg38 --ancestry EUR \
      --out underresolved_blocks.txt

Point ``--suffix`` at ``.ldeig-rebuilt`` to verify a backfill run instead of
scanning production: it should report zero under-resolved blocks.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np


def _components_needed(values: np.ndarray, thresh: float) -> int:
    vals = np.maximum(values.astype(float), 0.0)
    total = float(vals.sum()) or 1.0
    cum = np.cumsum(vals) / total
    return int(np.searchsorted(cum, thresh)) + 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--panel", type=Path, required=True, help="Panel root (contains {ancestry}/)")
    ap.add_argument("--ancestry", required=True)
    ap.add_argument(
        "--thresh", type=float, default=0.9,
        help="Cumulative-variance threshold the consumer truncates to (default 0.9).",
    )
    ap.add_argument(
        "--suffix", default=".ldeig",
        help="npz suffix to scan (default '.ldeig'); use '.ldeig-rebuilt' to verify a backfill.",
    )
    ap.add_argument(
        "--out", type=Path,
        help="Write block base paths here, one per line (backfill_eigendecomposition.py "
             "--blocks format). Defaults to stdout.",
    )
    args = ap.parse_args()

    root = args.panel / args.ancestry
    if not root.is_dir():
        print(f"No such panel ancestry directory: {root}", file=sys.stderr)
        return 1

    scanned = 0
    achieved_variances = []
    under_resolved: list[Path] = []
    for npz_path in sorted(root.glob(f"*/*{args.suffix}.npz")):
        scanned += 1
        d = np.load(npz_path)
        stored = d["vectors"].shape[1]
        needed = _components_needed(d["values"], args.thresh)
        vals = np.maximum(d["values"].astype(float), 0.0)
        total = float(vals.sum()) or 1.0
        achieved_variances.append(float(np.cumsum(vals)[min(stored, len(vals)) - 1] / total))
        if needed > stored:
            base_name = npz_path.name[: -len(f"{args.suffix}.npz")]
            under_resolved.append(npz_path.with_name(base_name))

    print(f"blocks scanned:                    {scanned}", file=sys.stderr)
    print(
        f"under-resolved at thresh={args.thresh}:  {len(under_resolved)}"
        f"  ({100 * len(under_resolved) / scanned:.1f}%)" if scanned else "",
        file=sys.stderr,
    )
    if achieved_variances:
        print(
            f"achieved variance:  min={min(achieved_variances):.4f}  "
            f"median={sorted(achieved_variances)[len(achieved_variances) // 2]:.4f}  "
            f"max={max(achieved_variances):.4f}",
            file=sys.stderr,
        )

    lines = "\n".join(str(p) for p in under_resolved)
    if args.out:
        args.out.write_text(lines + ("\n" if lines else ""))
        print(f"wrote {len(under_resolved)} block path(s) to {args.out}", file=sys.stderr)
    else:
        print(lines)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

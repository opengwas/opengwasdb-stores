#!/usr/bin/env python3
"""Create quantitative paired release evidence for the EUR calibration gate."""
from __future__ import annotations
import argparse, csv, statistics
from pathlib import Path

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--comparisons", type=Path, required=True,
        help="TSV with analysis_id, block_id, production_quality, calibration_quality")
    ap.add_argument("--out", type=Path, required=True); ap.add_argument("--max-mean-loss", type=float, required=True)
    args = ap.parse_args()
    with args.comparisons.open() as fh: rows = list(csv.DictReader(fh, delimiter="\t"))
    if not rows: raise SystemExit("comparison table is empty")
    required = {"analysis_id", "block_id", "production_quality", "calibration_quality"}
    if not required <= set(rows[0]): raise SystemExit(f"missing columns: {sorted(required-set(rows[0]))}")
    losses = [float(r["production_quality"]) - float(r["calibration_quality"]) for r in rows]
    mean = statistics.fmean(losses); median = statistics.median(losses)
    ship = mean <= args.max_mean_loss
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        "# HGDP+1kGP EUR calibration evidence\n\n"
        f"Paired analysis/block comparisons: {len(losses)}. Mean quality loss: {mean:.6g}; "
        f"median loss: {median:.6g}; allowed mean loss: {args.max_mean_loss:.6g}.\n\n"
        f"Recommendation: **{'SHIP' if ship else 'NO-SHIP'}** AFR/EAS/SAS panels.\n"
    )
    print("SHIP" if ship else "NO-SHIP")
    return 0 if ship else 2
if __name__ == "__main__": raise SystemExit(main())

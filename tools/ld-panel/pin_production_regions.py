#!/usr/bin/env python3
"""Replace candidate EUR rows with boundaries from a production panel tree."""
from __future__ import annotations
import argparse, csv
from pathlib import Path

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candidate", type=Path, required=True,
                    help="Four-ancestry candidate TSV; non-EUR rows are retained")
    ap.add_argument("--production-eur", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    with args.candidate.open() as fh:
        rows = [r for r in csv.DictReader(fh, delimiter="\t") if r["ancestry"] != "EUR"]
    eur = []
    for path in args.production_eur.glob("*/*.tsv"):
        try:
            start, end = map(int, path.stem.split("-"))
            chrom = int(path.parent.name)
        except ValueError as exc: raise SystemExit(f"unexpected production block name: {path}") from exc
        eur.append({"chr": str(chrom), "start": str(start), "end": str(end), "ancestry": "EUR"})
    if len(eur) != 1357: raise SystemExit(f"expected 1357 production EUR blocks, found {len(eur)}")
    rows.extend(eur); rows.sort(key=lambda r: (r["ancestry"], int(r["chr"]), int(r["start"]), int(r["end"])))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".tmp")
    with tmp.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["chr","start","end","ancestry"], delimiter="\t")
        writer.writeheader(); writer.writerows(rows)
    tmp.replace(args.out)
    print(f"wrote {len(rows)} rows ({len(eur)} EUR) to {args.out}")
    return 0
if __name__ == "__main__": raise SystemExit(main())

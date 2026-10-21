#!/usr/bin/env python3
"""Run block construction in a resumable, failure-isolated worker pool."""
from __future__ import annotations

import argparse, csv, json, subprocess, sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

DEFAULT_JOBS = 4  # memory-bound: each worker materialises one block


def valid(base: Path) -> bool:
    try:
        p = json.loads(base.with_suffix(".provenance.json").read_text())
        return (base.with_suffix(".tsv").stat().st_size > 0 and
                base.with_suffix(".ldeig.npz").stat().st_size > 0 and
                p["n_variants_retained"] > 0 and p["components_retained"] > 0)
    except (OSError, ValueError, KeyError): return False


def build(job: tuple[str, ...]) -> tuple[str, int, str]:
    label, *cmd = job
    result = subprocess.run(cmd, capture_output=True, text=True)
    return label, result.returncode, result.stderr.strip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--genotype-root", type=Path, required=True,
                    help="Directory of chrN pfile prefixes named chrN")
    ap.add_argument("--reference-root", type=Path, required=True)
    ap.add_argument("--regions", type=Path, required=True); ap.add_argument("--members", type=Path, required=True)
    ap.add_argument("--ancestries", nargs="+", default=["AFR", "EAS", "SAS"])
    ap.add_argument("--jobs", type=int, default=DEFAULT_JOBS); ap.add_argument("--mac", type=int, default=50)
    ap.add_argument("--cumvar", type=float, default=.99); ap.add_argument("--min-k", type=int, default=250)
    ap.add_argument("--force", action="store_true", help="Rebuild blocks with valid existing outputs")
    args = ap.parse_args()
    if args.jobs < 1: ap.error("--jobs must be >= 1")
    script = Path(__file__).with_name("construct_block.py")
    jobs = []
    with args.regions.open() as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            anc = row["ancestry"]
            if anc not in args.ancestries: continue
            base = args.reference_root / anc / row["chr"] / f'{row["start"]}-{row["end"]}'
            if valid(base) and not args.force: continue
            keep = args.members / f"{anc}.keep"
            ancestry_root = args.genotype_root / anc
            source = (ancestry_root if ancestry_root.is_dir() else args.genotype_root) / f'chr{row["chr"]}'
            cmd = (sys.executable, str(script), "--pfile", str(source),
                   "--keep", str(keep), "--chrom", row["chr"], "--start", row["start"],
                   "--end", row["end"], "--out", str(base), "--mac", str(args.mac),
                   "--cumvar", str(args.cumvar), "--min-k", str(args.min_k))
            jobs.append((f"{anc}/{row['chr']}/{base.name}", *cmd))
    failures = args.reference_root / "failures.tsv"
    failures.parent.mkdir(parents=True, exist_ok=True)
    with failures.open("a") as log, ProcessPoolExecutor(max_workers=min(args.jobs, len(jobs)) or 1) as pool:
        futures = [pool.submit(build, job) for job in jobs]
        for future in as_completed(futures):
            label, code, error = future.result()
            if code: log.write(f"{label}\t{code}\t{error.replace(chr(10), ' ')}\n"); log.flush()
            print(f"{'FAILED' if code else 'built'} {label}", flush=True)
    return 0


if __name__ == "__main__": raise SystemExit(main())

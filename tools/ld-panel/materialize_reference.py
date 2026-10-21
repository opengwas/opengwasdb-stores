#!/usr/bin/env python3
"""Resume download conversion and materialize AFR/EAS/SAS panels at a reference root."""
from __future__ import annotations
import argparse, subprocess, sys, time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

VCF = "gnomad.genomes.v3.1.2.hgdp_tgp.chr{}.vcf.bgz"

def conversion_command(plink2: Path, vcf: Path, keep: Path, out: Path, mac: int,
                       threads: int = 8, memory_mb: int = 32768) -> list[str]:
    return [str(plink2), "--vcf", str(vcf), "--keep", str(keep), "--nonfounders", "--mac", str(mac),
            "--threads", str(threads), "--memory", str(memory_mb),
            "--max-alleles", "2",
            "--set-all-var-ids", "@:#:$r:$a", "--new-id-max-allele-len", "100", "truncate",
            "--make-pgen", "vzs", "pvar-cols=xheader", "--out", str(out)]


def convert_one(plink2: Path, vcf: Path, keep: Path, out: Path, mac: int,
                threads: int, memory_mb: int) -> str:
    complete = Path(str(out) + ".complete")
    if complete.exists(): return f"skip {out}"
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(conversion_command(plink2, vcf, keep, out, mac, threads, memory_mb), check=True)
    for suffix in (".pgen", ".pvar.zst", ".psam"):
        if not Path(str(out) + suffix).exists(): raise RuntimeError(f"missing {out}{suffix}")
    complete.write_text(f"plink2 biallelic conversion; MAC >= {mac}\n")
    return f"built {out}"

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, required=True); ap.add_argument("--jobs", type=int, default=20,
                    help="Concurrent block eigendecompositions (memory-bound; default 20)")
    ap.add_argument("--poll-seconds", type=int, default=60)
    ap.add_argument("--mac", type=int, default=50); ap.add_argument("--ancestries", nargs="+", default=["AFR","EAS","SAS"])
    ap.add_argument("--pgen-jobs", type=int, default=4,
                    help="Concurrent ancestry/chromosome conversions (default 4)")
    ap.add_argument("--plink-threads", type=int, default=8); ap.add_argument("--plink-memory-mb", type=int, default=32768)
    ap.add_argument("--plink2", default="plink2", help="plink2 executable (default: resolved from PATH)")
    args = ap.parse_args(); source = args.root / "source"; pgen = args.root / "pgen"
    if min(args.jobs, args.pgen_jobs, args.plink_threads, args.plink_memory_mb) < 1:
        ap.error("job, thread, and memory settings must be >= 1")
    pgen.mkdir(parents=True, exist_ok=True)
    plink2 = args.plink2
    pending = [(chrom, ancestry) for chrom in range(1, 23) for ancestry in args.ancestries
               if not Path(str(pgen / ancestry / f"chr{chrom}") + ".complete").exists()]
    running = {}
    with ThreadPoolExecutor(max_workers=args.pgen_jobs) as pool:
        while pending or running:
            for job in list(pending):
                if len(running) >= args.pgen_jobs: break
                chrom, ancestry = job; vcf = source / VCF.format(chrom)
                if not Path(str(vcf) + ".complete").exists(): continue
                out = pgen / ancestry / f"chr{chrom}"
                future = pool.submit(convert_one, plink2, vcf,
                    args.root / "members" / f"{ancestry}.keep", out, args.mac,
                    args.plink_threads, args.plink_memory_mb)
                running[future] = job; pending.remove(job)
            if not running:
                print(f"waiting for downloads ({len(pending)} conversions pending)", flush=True)
                time.sleep(args.poll_seconds); continue
            done, _ = wait(running, timeout=args.poll_seconds, return_when=FIRST_COMPLETED)
            for future in done:
                print(future.result(), flush=True); del running[future]
    runner = Path(__file__).with_name("run_panels.py")
    return subprocess.run([sys.executable, str(runner), "--genotype-root", str(pgen),
        "--reference-root", str(args.root / "panel"), "--regions", str(args.root / "ld_regions_hg38.tsv"),
        "--members", str(args.root / "members"), "--ancestries", *args.ancestries,
        "--mac", str(args.mac), "--jobs", str(args.jobs)]).returncode
if __name__ == "__main__": raise SystemExit(main())
